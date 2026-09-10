"""停机窗（清炉/校准/检修）功能测试：

1. 装载→升温→保温→周转是不可拆分占用区间，撞上停机窗整体移到窗后，
   并返回因避让增加的等待分钟与逾期变化；
2. 停机窗校验：起止倒序 / 同炉重叠 / 未知炉号 / 未知类型一律 400；
3. 已签发/在炉炉次不得改时刻：新窗口撞上时响应列出重叠区间与冲突分钟；
4. 逐炉时间线区分生产占用 / 周转 / 停机，并比较各炉完工时刻与交期；
5. 停机窗写入版本快照，后续试算可改写；炉次详情 / 版本查询 / JSON 档案
   均保留关联停机窗与计算依据。

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from ovenline import create_app

OVEN = {
    "id": "OVEN-1", "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02, "ambient_c": 25,
    "turnaround_minutes": 15, "hanger_slots": 12, "hanger_spacing_mm": 500,
    "hanger_max_load_kg": 80,
}
# 10 kg 工件：升温 = (170-25)/4 + 10*0.02 = 36.45 min；保温 15；周转 15
# 无停历时：08:00 装载 → 08:36:27 固化 → 08:51:27 出炉 → 09:06:27 释放
HEATUP_MIN = 36.45


def _order(wid, due="2026-09-10T18:00:00", weight=10):
    return {"workpiece_id": wid, "order_id": "PO-T", "length_mm": 500,
            "width_mm": 400, "height_mm": 300, "weight_kg": weight,
            "powder_batch": "P1", "compat_group": None, "due_at": due}


def _payload(orders, blackouts=None, reason="t"):
    p = {"reason": reason, "start_at": "2026-09-10T08:00:00",
         "ovens": [OVEN],
         "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                      "hold_minutes": 15}],
         "forbidden_pairs": [], "orders": orders}
    if blackouts is not None:
        p["blackout_windows"] = blackouts
    return p


def _window(start, end, kind="MAINTENANCE", oven="OVEN-1", note=None):
    w = {"oven_id": oven, "kind": kind, "start_at": start, "end_at": end}
    if note is not None:
        w["note"] = note
    return w


class BlackoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _trial(self, payload, expect=201):
        r = self.c.post("/api/schedule/trial", json=payload)
        self.assertEqual(r.status_code, expect, r.get_data(as_text=True))
        return r.get_json()

    # ------------------------------------------------ 1. 整段避让与等待/逾期
    def test_block_shifts_entirely_after_blackout(self):
        d = self._trial(_payload(
            [_order("W-1", due="2026-09-10T09:30:00")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                               note="月度检修")]))
        b = d["new_batches"][0]
        # 原占用区间 [08:00, 09:06:27) 撞上 08:30-09:00 停机 → 整体移到 09:00
        self.assertEqual(b["planned_load_at"], "2026-09-10T09:00:00")
        self.assertEqual(b["planned_cure_start_at"], "2026-09-10T09:36:27")
        self.assertEqual(b["planned_unload_at"], "2026-09-10T09:51:27")
        self.assertEqual(b["turnaround_end_at"], "2026-09-10T10:06:27")
        # 避让增加的等待分钟 = 09:00 - 08:00
        self.assertEqual(b["blackout_wait_minutes"], 60.0)
        self.assertEqual(b["baseline_load_at"], "2026-09-10T08:00:00")
        self.assertEqual(b["baseline_unload_at"], "2026-09-10T08:51:27")
        self.assertEqual(len(b["avoided_windows"]), 1)
        self.assertEqual(b["avoided_windows"][0]["kind"], "MAINTENANCE")
        self.assertEqual(b["avoided_windows"][0]["note"], "月度检修")
        # 逾期变化：基准 08:51:27 不逾期，避让后 09:51:27 逾期 21.45 分钟
        self.assertEqual(b["earliest_due_at"], "2026-09-10T09:30:00")
        self.assertEqual(b["baseline_lateness_minutes"], 0.0)
        self.assertAlmostEqual(b["lateness_minutes"], 21.45, places=2)
        self.assertAlmostEqual(b["lateness_delta_minutes"], 21.45, places=2)
        self.assertTrue(b["items"][0]["late"])

    def test_no_blackout_keeps_baseline_and_zero_wait(self):
        d = self._trial(_payload([_order("W-1")], blackouts=[]))
        b = d["new_batches"][0]
        self.assertEqual(b["planned_load_at"], "2026-09-10T08:00:00")
        self.assertEqual(b["blackout_wait_minutes"], 0.0)
        self.assertEqual(b["avoided_windows"], [])
        self.assertEqual(b["lateness_delta_minutes"], 0.0)
        self.assertEqual(d["blackout_windows"], [])
        self.assertEqual(d["blackout_conflicts"], [])

    def test_turnaround_tail_also_avoids_blackout(self):
        # 停机窗只压住周转尾段（09:00-09:10 落在 [08:51:27, 09:06:27) 内）：
        # 周转同属不可拆分区间，整段仍要移到窗后
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T09:00:00", "2026-09-10T09:10:00",
                               kind="CLEANING")]))
        b = d["new_batches"][0]
        self.assertEqual(b["planned_load_at"], "2026-09-10T09:10:00")
        self.assertEqual(b["blackout_wait_minutes"], 70.0)

    def test_chained_blackouts_and_touching_windows(self):
        # 两段首尾相接的停机窗（不算重叠），区间连续避让到最后一窗之后
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[
                _window("2026-09-10T08:00:00", "2026-09-10T08:30:00",
                        kind="CLEANING"),
                _window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                        kind="CALIBRATION"),
            ]))
        b = d["new_batches"][0]
        self.assertEqual(b["planned_load_at"], "2026-09-10T09:00:00")
        self.assertEqual(b["blackout_wait_minutes"], 60.0)
        self.assertEqual([w["kind"] for w in b["avoided_windows"]],
                         ["CLEANING", "CALIBRATION"])

    def test_kind_accepts_chinese_alias(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                               kind="检修")]))
        self.assertEqual(d["blackout_windows"][0]["kind"], "MAINTENANCE")
        self.assertEqual(d["blackout_windows"][0]["kind_text"], "检修")

    # ------------------------------------------------ 2. 停机窗校验
    def test_reject_reversed_window(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T09:00:00", "2026-09-10T08:30:00")]),
            expect=400)
        self.assertIn("倒序", d["error"])

    def test_reject_zero_length_window(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T09:00:00", "2026-09-10T09:00:00")]),
            expect=400)
        self.assertIn("倒序", d["error"])

    def test_reject_same_oven_overlap(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[
                _window("2026-09-10T08:00:00", "2026-09-10T08:45:00"),
                _window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                        kind="CLEANING"),
            ]), expect=400)
        self.assertIn("重叠", d["error"])

    def test_reject_unknown_oven(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T08:00:00", "2026-09-10T09:00:00",
                               oven="OVEN-GHOST")]), expect=400)
        self.assertIn("未知炉号", d["error"])

    def test_reject_unknown_kind_and_bad_shape(self):
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T08:00:00", "2026-09-10T09:00:00",
                               kind="PARTY")]), expect=400)
        self.assertIn("未知停机类型", d["error"])
        d = self._trial(_payload([_order("W-1")], blackouts={"OVEN-1": []}),
                        expect=400)
        self.assertIn("数组", d["error"])
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[{"oven_id": "OVEN-1", "kind": "CLEANING",
                        "start_at": "2026-09-10T08:00:00"}]), expect=400)
        self.assertIn("缺少字段", d["error"])

    # ------------------------------------------------ 3. 已签发炉次冲突
    def test_conflict_with_issued_batch_listed_and_times_frozen(self):
        d1 = self._trial(_payload([_order("W-1")], reason="v1"))
        bid = d1["new_batches"][0]["batch_id"]
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200)

        # v2：新停机窗 08:30-09:00 撞上已签发炉次占用区间 [08:00, 09:06:27)
        d2 = self._trial(_payload(
            [_order("W-2")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00")],
            reason="v2 加停机"))
        self.assertEqual(len(d2["blackout_conflicts"]), 1)
        conflict = d2["blackout_conflicts"][0]
        self.assertEqual(conflict["batch_id"], bid)
        self.assertEqual(conflict["batch_state"], "ISSUED")
        self.assertEqual(conflict["overlap"]["start_at"], "2026-09-10T08:30:00")
        self.assertEqual(conflict["overlap"]["end_at"], "2026-09-10T09:00:00")
        self.assertEqual(conflict["overlap"]["minutes"], 30.0)
        self.assertEqual(conflict["occupied"]["end_at"], "2026-09-10T09:06:27")

        # 已签发炉次时刻不变
        detail = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(detail["planned"]["load_at"], "2026-09-10T08:00:00")
        self.assertEqual(detail["planned"]["unload_at"], "2026-09-10T08:51:27")

        # 新炉次接在已签发炉次之后（09:06:27），不再与停机窗冲突
        nb = d2["new_batches"][0]
        self.assertEqual(nb["planned_load_at"], "2026-09-10T09:06:27")
        self.assertEqual(nb["blackout_wait_minutes"], 0.0)

    # ------------------------------------------------ 4. 逐炉时间线
    def test_oven_timeline_distinguishes_segment_kinds(self):
        d1 = self._trial(_payload([_order("W-1")], reason="v1"))
        bid = d1["new_batches"][0]["batch_id"]
        self.c.post(f"/api/batches/{bid}/issue")
        d2 = self._trial(_payload(
            [_order("W-2", due="2026-09-10T10:00:00")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                               kind="CLEANING", note="清炉")],
            reason="v2"))
        self.assertEqual(len(d2["oven_timelines"]), 1)
        tl = d2["oven_timelines"][0]
        self.assertEqual(tl["oven_id"], "OVEN-1")
        kinds = [s["kind"] for s in tl["segments"]]
        self.assertEqual(kinds.count("PRODUCTION"), 2)   # 已签发 + 新炉次
        self.assertEqual(kinds.count("TURNAROUND"), 2)
        self.assertEqual(kinds.count("BLACKOUT"), 1)
        # 时间线按开始时刻升序
        starts = [s["start_at"] for s in tl["segments"]]
        self.assertEqual(starts, sorted(starts))
        blackout = next(s for s in tl["segments"] if s["kind"] == "BLACKOUT")
        self.assertEqual(blackout["blackout_kind"] if "blackout_kind" in blackout
                         else blackout["kind"], "CLEANING")
        self.assertEqual(blackout["note"], "清炉")
        carried_prod = next(s for s in tl["segments"]
                            if s["kind"] == "PRODUCTION" and s["batch_id"] == bid)
        self.assertEqual(carried_prod["state"], "ISSUED")
        # 各炉完工时刻与交期比较：新炉次 09:57:54 完工，交期 10:00 不逾期
        self.assertEqual(tl["completed_at"], "2026-09-10T09:57:54")
        self.assertEqual(tl["released_at"], "2026-09-10T10:12:54")
        self.assertEqual(tl["late_batches"], 0)
        self.assertEqual(tl["max_lateness_minutes"], 0.0)

    def test_oven_timeline_reports_late_batches(self):
        d = self._trial(_payload(
            [_order("W-1", due="2026-09-10T09:30:00")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00")]))
        tl = d["oven_timelines"][0]
        self.assertEqual(tl["late_batches"], 1)
        self.assertAlmostEqual(tl["max_lateness_minutes"], 21.45, places=2)

    # ------------------------------------------------ 5. 快照 / 详情 / 档案
    def test_blackouts_snapshot_in_version_and_queries(self):
        d1 = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00",
                               note="检修")], reason="v1"))
        vid = d1["version"]["id"]
        bid = d1["new_batches"][0]["batch_id"]

        # 版本列表带停机窗数量；版本详情保留停机窗与计算依据
        lst = self.c.get("/api/versions").get_json()["versions"]
        self.assertEqual(lst[-1]["blackout_count"], 1)
        v = self.c.get(f"/api/versions/{vid}").get_json()
        self.assertEqual(len(v["blackout_windows"]), 1)
        self.assertEqual(v["blackout_windows"][0]["kind"], "MAINTENANCE")
        self.assertEqual(v["blackout_windows"][0]["note"], "检修")
        self.assertEqual(len(v["params"]["blackout_windows"]), 1)
        self.assertEqual(v["batches"][0]["blackout_wait_minutes"], 60.0)
        r = self.c.get("/api/versions/9999")
        self.assertEqual(r.status_code, 404)

        # 炉次详情保留关联停机窗与计算依据
        detail = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(len(detail["blackout_windows"]), 1)
        self.assertEqual(detail["blackout_windows"][0]["start_at"],
                         "2026-09-10T08:30:00")
        self.assertEqual(detail["blackout_wait_minutes"], 60.0)
        basis = detail["schedule_basis"]
        self.assertEqual(basis["baseline_load_at"], "2026-09-10T08:00:00")
        self.assertEqual(basis["baseline_unload_at"], "2026-09-10T08:51:27")
        self.assertEqual(basis["blackout_wait_minutes"], 60.0)
        self.assertEqual(len(basis["avoided_windows"]), 1)
        self.assertAlmostEqual(basis["lateness_delta_minutes"],
                               basis["lateness_minutes"]
                               - basis["baseline_lateness_minutes"], places=2)

        # JSON 档案同样保留
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        self.assertEqual(len(arch["blackout_windows"]), 1)
        self.assertEqual(arch["schedule_basis"]["baseline_load_at"],
                         "2026-09-10T08:00:00")

    def test_later_trial_can_modify_blackouts(self):
        # v1：08:30-09:00 检修 → 炉次移到 09:00
        d1 = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T08:30:00", "2026-09-10T09:00:00")],
            reason="v1"))
        self.assertEqual(d1["new_batches"][0]["planned_load_at"],
                         "2026-09-10T09:00:00")
        # v2：停机窗改到 12:00-13:00 → 不再影响早班炉次
        d2 = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T12:00:00", "2026-09-10T13:00:00",
                               kind="CALIBRATION")], reason="v2 改停机"))
        b = d2["new_batches"][0]
        self.assertEqual(b["planned_load_at"], "2026-09-10T08:00:00")
        self.assertEqual(b["blackout_wait_minutes"], 0.0)
        self.assertEqual(d2["blackout_conflicts"], [])
        # 两个版本各自保留当时的停机窗快照
        v1 = self.c.get(f"/api/versions/{d1['version']['id']}").get_json()
        v2 = self.c.get(f"/api/versions/{d2['version']['id']}").get_json()
        self.assertEqual(v1["blackout_windows"][0]["kind"], "MAINTENANCE")
        self.assertEqual(v2["blackout_windows"][0]["kind"], "CALIBRATION")
        self.assertEqual(v2["parent_id"], v1["id"])

    def test_blackout_after_all_batches_does_not_shift(self):
        # 停机窗完全在炉次之后：不影响排产，但仍在时间线中
        d = self._trial(_payload(
            [_order("W-1")],
            blackouts=[_window("2026-09-10T12:00:00", "2026-09-10T13:00:00")]))
        b = d["new_batches"][0]
        self.assertEqual(b["planned_load_at"], "2026-09-10T08:00:00")
        self.assertEqual(b["blackout_wait_minutes"], 0.0)
        kinds = [s["kind"] for s in d["oven_timelines"][0]["segments"]]
        self.assertIn("BLACKOUT", kinds)


if __name__ == "__main__":
    unittest.main()
