"""吊具布置与载荷平衡功能测试（含三处已复现缺陷的回归）：

1. 左右偏载按**整组方案**判定：两个 10 kg 件分别挂 -1000/+1000、最终
   绝对力矩为 0 时必须同炉，单件放入时不得以 MOMENT/OVERWEIGHT 拒绝；
2. 吊点禁用窗只在与炉次排产占用时段重叠时封点：07:00 已结束的窗不得
   影响 08:00 开排炉次；正在重叠的窗必须封点并重排；
3. 净距检查同时计入相邻双方的 clearance_mm；
另覆盖人工校验端点、签发冻结、吊点故障只重排未签发炉次与版本拒绝原因。

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from ovenline import create_app

# 3 个吊点、间距 1000、对称坐标 (-1000, 0, +1000)
RACK = {
    "rods": [{
        "id": "R1", "axis": "L", "y_mm": 900, "z_mm": 1400,
        "point_count": 3, "point_spacing_mm": 1000,
        "default_point_load_kg": 80,
        "zones": [
            {"id": "ZL", "x_min_mm": -1500, "x_max_mm": -1,
             "max_load_kg": 100},
            {"id": "ZR", "x_min_mm": 0, "x_max_mm": 1500,
             "max_load_kg": 100}],
    }],
    "beam_max_load_kg": 500,
    # 缺陷 1 回归：容差为 0 时，只有整组力矩为 0 才平衡
    "moment_tolerance_kg_mm": 0,
}
# 只有两个吊点（±1000，无中心）：单件无论挂哪都偏载
RACK_TWO = {
    "rods": [{
        "id": "R1", "axis": "L", "y_mm": 900, "z_mm": 1400,
        "points": [
            {"index": 1, "x_mm": -1000, "max_load_kg": 80},
            {"index": 2, "x_mm": 1000, "max_load_kg": 80}],
        "point_spacing_mm": 1000,
    }],
    "beam_max_load_kg": 500,
    "moment_tolerance_kg_mm": 0,
}
OVEN = {
    "id": "OVEN-1", "chamber_l_mm": 3000, "chamber_w_mm": 1200,
    "chamber_h_mm": 1500, "heat_rate_c_per_min": 4.0,
    "mass_factor_min_per_kg": 0.02, "ambient_c": 25,
    "turnaround_minutes": 15, "hanger_slots": 3,
    "hanger_spacing_mm": 1000, "hanger_max_load_kg": 80,
    "hanger_rack": RACK,
}
# 无偏载容差的普通炉架（吊点 -1000/0/+1000），保持旧 first-fit 行为
PLAIN_OVEN = {k: v for k, v in OVEN.items() if k != "hanger_rack"}
POWDER = {"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
          "hold_minutes": 15}
DUE = "2026-09-10T18:00:00"


def _order(wid, weight=10, length=500, due=DUE, **rack_fields):
    o = {"workpiece_id": wid, "order_id": "O", "length_mm": length,
         "width_mm": 400, "height_mm": 300, "weight_kg": weight,
         "powder_batch": "P1", "compat_group": None, "due_at": due}
    o.update(rack_fields)
    return o


def _trial(client, orders, hanger_blackouts=None, reason="t", start=None,
          oven=None):
    payload = {"reason": reason, "start_at": start or "2026-09-10T08:00:00",
               "ovens": [oven or OVEN], "powders": [POWDER],
               "forbidden_pairs": [], "orders": orders}
    if hanger_blackouts is not None:
        payload["hanger_blackouts"] = hanger_blackouts
    r = client.post("/api/schedule/trial", json=payload)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()


class RackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True, "REQUIRE_PACK_LIMIT_AT_ISSUE": False})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------- 缺陷 1：力矩按整组方案判定 ----------------
    def test_balanced_pair_not_rejected(self):
        """两个 10 kg 件最终力矩 0：必须同炉且平衡，单件不被拒绝。"""
        d = _trial(self.c, [_order("A"), _order("B")])
        self.assertEqual(len(d["new_batches"]), 1)
        b = d["new_batches"][0]
        xs = {i["workpiece_id"]: [p["x_mm"] for p in
                                  i["placement"]["occupied_points"]]
              for i in b["items"]}
        self.assertEqual(sorted(xs["A"] + xs["B"]), [-1000.0, 1000.0])
        bal = b["rack_layout"]["load_balance"]
        self.assertEqual(bal["moment_abs_kg_mm"], 0.0)
        self.assertTrue(bal["moment_ok"])
        self.assertEqual(d["unscheduled"], [])

    def test_single_heavy_piece_unschedulable_when_always_off_balance(self):
        """单件无论挂哪都超力矩容差 → unscheduled，首冲突 MOMENT，给可选炉。

        使用只有 ±1000 两个吊点（无中心）的炉架：单件恒偏载。
        """
        two_oven = dict(OVEN, hanger_rack=RACK_TWO, hanger_slots=2)
        d = _trial(self.c, [_order("SOLO")], oven=two_oven)
        self.assertEqual(len(d["new_batches"]), 0)
        self.assertEqual(len(d["unscheduled"]), 1)
        u = d["unscheduled"][0]
        self.assertEqual(u["workpiece_id"], "SOLO")
        self.assertEqual(u["reason"], "LOAD_BALANCE")
        self.assertEqual(u["first_conflict"]["code"], "MOMENT")
        # 仍列出硬可行炉（尺寸/承重放得下，只是整组平衡不满足）
        self.assertEqual(u["alternative_ovens"], ["OVEN-1"])

    def test_no_moment_tolerance_keeps_first_fit(self):
        """未设偏载容差的旧炉架：保持 first-fit 确定性布置，不做整组改位。"""
        from ovenline import racking
        plain = {k: v for k, v in OVEN.items() if k != "hanger_rack"}
        rack = racking.build_rack(plain)
        self.assertIsNone(rack.moment_tolerance_kg_mm)
        payload = {"reason": "t", "start_at": "2026-09-10T08:00:00",
                   "ovens": [plain], "powders": [POWDER],
                   "forbidden_pairs": [],
                   "orders": [_order("A"), _order("B")]}
        d = self.c.post("/api/schedule/trial", json=payload).get_json()
        b = d["new_batches"][0]
        # 旧连续编号：A 在吊点 1、B 在吊点 2（从最左起）
        slots = {i["workpiece_id"]: i["hanger_slot"] for i in b["items"]}
        self.assertEqual(slots, {"A": 1, "B": 2})

    # ---------------- 缺陷 2：禁用窗只在占用重叠时封点 ----------------
    def test_ended_blackout_does_not_block(self):
        """07:00 已结束的禁用窗不影响 08:00 开排：BIG 可挂吊点 1。"""
        d = _trial(self.c, [_order("BIG")], oven=PLAIN_OVEN,
                   hanger_blackouts=[{
            "oven_id": "OVEN-1", "rod_id": "R1", "point_index": 1,
            "start_at": "2026-09-10T06:00:00",
            "end_at": "2026-09-10T07:00:00", "note": "早班封位已解除"}])
        b = d["new_batches"][0]
        idx = b["items"][0]["placement"]["occupied_points"][0]["index"]
        self.assertEqual(idx, 1)

    def test_overlapping_blackout_blocks_point(self):
        """与占用时段重叠的禁用窗封点：吊点 1 不可用，重排到吊点 2/3。"""
        d = _trial(self.c, [_order("A"), _order("B")], oven=PLAIN_OVEN,
                   hanger_blackouts=[{
            "oven_id": "OVEN-1", "rod_id": "R1", "point_index": 1,
            "start_at": "2026-09-10T08:00:00",
            "end_at": "2026-09-10T20:00:00", "note": "临时封位"}])
        used = {p["index"] for b in d["new_batches"] for i in b["items"]
                for p in i["placement"]["occupied_points"]}
        self.assertNotIn(1, used)

    def test_blackout_unknown_rod_or_point_rejected(self):
        r = self.c.post("/api/schedule/trial", json={
            "reason": "t", "start_at": "2026-09-10T08:00:00",
            "ovens": [OVEN], "powders": [POWDER], "forbidden_pairs": [],
            "orders": [_order("A")],
            "hanger_blackouts": [{
                "oven_id": "OVEN-1", "rod_id": "RX", "point_index": 1,
                "start_at": "2026-09-10T08:00:00",
                "end_at": "2026-09-10T09:00:00"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("吊点不存在", r.get_json()["error"])

    # ---------------- 缺陷 3：净距计入双方 clearance ----------------
    def test_clearance_counts_both_sides(self):
        from ovenline import racking
        rack = racking.build_rack(OVEN)
        layout = racking._Layout(rack)
        a = _order("A")                      # clearance 默认 0
        layout.commit(layout.try_place(a))
        b = _order("B", clearance_mm=1000)  # B 要求 1000 mm 净距
        res = layout.try_place(b)
        # A 占吊点 1(-1000)，相邻吊点 2(0) 净距仅 500 mm（B 半净距）→ 落到吊点 3
        self.assertEqual(res["run"][0]["index"], 3)

    def test_clearance_of_existing_piece_also_counts(self):
        from ovenline import racking
        rack = racking.build_rack(OVEN)
        layout = racking._Layout(rack)
        a = _order("A", clearance_mm=1000)  # A 要求净距
        layout.commit(layout.try_place(a))
        b = _order("B")                      # B 自身无净距要求
        res = layout.try_place(b)
        # A 的净距要求同样生效：B 不得落在相邻吊点 2
        self.assertEqual(res["run"][0]["index"], 3)

    # ---------------- 单点/分区/总载/吊耳/重心 ----------------
    def test_point_load_overload_rejected(self):
        # 吊点承重 80 kg，200 kg 门用两个吊耳也超 80/点 → 单点超限
        d = _trial(self.c, [_order("HUGE", weight=200, length=2200,
                                   lift_points_mm=[-1000, 1000])])
        u = d["unscheduled"][0]
        self.assertEqual(u["reason"], "OVERWEIGHT")
        self.assertEqual(u["first_conflict"]["code"], "POINT_LOAD")

    def test_lug_mapping_and_load_split(self):
        # 放宽力矩容差：单扇门板允许一定偏心，重点校验吊耳映射与杠杆分载
        rack = {**RACK, "moment_tolerance_kg_mm": 100_000}
        oven = dict(OVEN, hanger_rack=rack)
        door = _order("DOOR", weight=60, length=2200,
                      lift_points_mm=[-1000, 1000], cg_offset_x_mm=100)
        d = _trial(self.c, [door], oven=oven)
        b = d["new_batches"][0]
        plc = b["items"][0]["placement"]
        bearing = [p for p in plc["occupied_points"] if p["bearing"]]
        self.assertEqual(len(bearing), 2)
        # 重心偏右 100 mm：右吊耳承重更大（杠杆法）
        loads = {p["x_mm"]: p["load_kg"] for p in bearing}
        right = max(loads)
        self.assertGreater(loads[right], 30.0)
        self.assertTrue(b["rack_layout"]["load_balance"]["moment_ok"])

    # ---------------- 人工校验 / 签发冻结 ----------------
    def _draft_batch(self):
        d = _trial(self.c, [_order("A"), _order("B")])
        return d["new_batches"][0]["batch_id"]

    def test_manual_verify_check_only_and_apply(self):
        bid = self._draft_batch()
        assignments = [
            {"workpiece_id": "A", "rod_id": "R1", "point_indices": [1]},
            {"workpiece_id": "B", "rod_id": "R1", "point_indices": [3]}]
        # check_only：不落库
        r = self.c.post(f"/api/batches/{bid}/arrangement/verify",
                        json={"assignments": assignments})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["applied"])
        # apply=true：采用人工布置
        r = self.c.post(f"/api/batches/{bid}/arrangement/verify",
                        json={"assignments": assignments, "apply": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        detail = self.c.get(f"/api/batches/{bid}").get_json()
        slot = {i["workpiece_id"]: i["hanger_slot"]
                for i in detail["items"]}
        self.assertEqual(slot, {"A": 1, "B": 3})

    def test_manual_verify_reports_violation(self):
        bid = self._draft_batch()
        # 两件都指同一吊点 → 共享吊点/净距冲突；整组力矩也不为 0
        r = self.c.post(f"/api/batches/{bid}/arrangement/verify", json={
            "assignments": [
                {"workpiece_id": "A", "rod_id": "R1", "point_indices": [1]},
                {"workpiece_id": "B", "rod_id": "R1", "point_indices": [1]}]})
        self.assertEqual(r.status_code, 409)
        codes = {v["code"] for v in r.get_json()["violations"]}
        self.assertIn("POINT_TAKEN", codes)

    def test_issued_batch_arrangement_frozen(self):
        bid = self._draft_batch()
        self.assertEqual(self.c.post(f"/api/batches/{bid}/issue").status_code, 200)
        assignments = [
            {"workpiece_id": "A", "rod_id": "R1", "point_indices": [1]},
            {"workpiece_id": "B", "rod_id": "R1", "point_indices": [3]}]
        # 签发后 apply 被拒（409），check_only 仍可复核
        r = self.c.post(f"/api/batches/{bid}/arrangement/verify",
                        json={"assignments": assignments, "apply": True})
        self.assertEqual(r.status_code, 409)
        r = self.c.post(f"/api/batches/{bid}/arrangement/verify",
                        json={"assignments": assignments})
        self.assertEqual(r.status_code, 200)

    # ---------------- 吊点故障只重排未签发炉次 ----------------
    def test_point_fault_replans_only_drafts_and_reports_migration(self):
        # 普通炉架（无偏载容差）：first-fit 确定性布置 A=1,B=2,C=3
        d = _trial(self.c, [_order("A"), _order("B"),
                            _order("C", due="2026-09-11T18:00:00")],
                   oven=PLAIN_OVEN)
        bid = d["new_batches"][0]["batch_id"]
        # 签发整炉（无探头工件直接签发）
        self.assertEqual(self.c.post(f"/api/batches/{bid}/issue").status_code, 200)
        # 再来一版草稿（新工件 D 落吊点 1），用于验证只重排草稿
        d2 = _trial(self.c, [_order("D", due="2026-09-11T18:00:00")],
                    oven=PLAIN_OVEN, reason="二班")
        d_slot = d2["new_batches"][0]["items"][0]["hanger_slot"]
        r = self.c.post("/api/schedule/point-fault", json={
            "oven_id": "OVEN-1", "rod_id": "R1", "point_index": d_slot,
            "reason": "吊点裂纹", "started_at": "2026-09-10T08:00:00"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        # 已签发炉次占用该吊点 → frozen_conflicts 列出，炉次时刻/状态不变
        self.assertTrue(body["frozen_conflicts"])
        fc = body["frozen_conflicts"][0]
        self.assertEqual(fc["batch_id"], bid)
        self.assertEqual(fc["point_index"], d_slot)
        detail = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(detail["state"], "ISSUED")
        # 草稿炉次被重排：新批次不再使用故障吊点
        for b in body["new_batches"]:
            for i in b["items"]:
                self.assertNotIn(d_slot, [p["index"] for p in
                                         i["placement"]["occupied_points"]])
        # 迁移工件说明：D 的挂位发生变化
        moved = {m["workpiece_id"]: m for m in body["migrations"] if m["moved"]}
        self.assertIn("D", moved)
        self.assertEqual(moved["D"]["from_slot"], d_slot)

    def test_point_fault_duplicate_idempotent(self):
        _trial(self.c, [_order("A")], oven=PLAIN_OVEN)
        payload = {"oven_id": "OVEN-1", "rod_id": "R1", "point_index": 2,
                   "reason": "x"}
        self.assertEqual(self.c.post("/api/schedule/point-fault",
                                     json=payload).status_code, 201)
        r = self.c.post("/api/schedule/point-fault", json=payload)
        self.assertEqual(r.status_code, 409)

    def test_resolve_fault_then_trial_uses_point(self):
        _trial(self.c, [_order("A")], oven=PLAIN_OVEN)
        r = self.c.post("/api/schedule/point-fault", json={
            "oven_id": "OVEN-1", "rod_id": "R1", "point_index": 1,
            "reason": "临时", "started_at": "2026-09-10T08:00:00"})
        fid = r.get_json()["fault"]["fault_id"]
        self.assertEqual(self.c.post(
            f"/api/schedule/point-fault/{fid}/resolve",
            json={"note": "已更换"}).status_code, 200)
        d = _trial(self.c, [_order("Z")], oven=PLAIN_OVEN, reason="修复后")
        idx = d["new_batches"][0]["items"][0]["placement"][
            "occupied_points"][0]["index"]
        self.assertEqual(idx, 1)

    # ---------------- 档案 / 版本 / 随炉卡记录坐标/载荷/拒绝原因 ----------------
    def test_archive_version_card_carry_arrangement_and_rejections(self):
        d = _trial(self.c, [_order("A"), _order("B"),
                            _order("HUGE", weight=200)])
        bid = d["new_batches"][0]["batch_id"]
        vid = d["version"]["id"]
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        self.assertIn("rack_layout", arch)
        self.assertEqual(arch["rack_layout"]["load_balance"][
            "moment_abs_kg_mm"], 0.0)
        # 版本保留拒绝工件首个冲突约束与可选炉
        v = self.c.get(f"/api/versions/{vid}").get_json()
        rej = {r["workpiece_id"]: r for r in v["rejections"]}
        self.assertEqual(rej["HUGE"]["reason"], "OVERWEIGHT")
        self.assertEqual(rej["HUGE"]["first_conflict"]["code"], "POINT_LOAD")
        self.assertGreaterEqual(len(v["hanger_blackouts"]), 0)
        # 随炉卡含吊具布置小节
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("吊具布置与载荷平衡", card)
        self.assertIn("搬入顺序", card)


if __name__ == "__main__":
    unittest.main()
