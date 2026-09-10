"""逐件出炉功能测试：

1. 先达标工件可单独出炉：炉次保持 IN_OVEN，汇总只计在炉工件，
   已离炉工件保留首次达标/实际离炉时刻/最终判定/离炉时进度快照；
2. 未达安全条件的普通请求被 409 拒绝，返回累计、剩余时间与阻塞原因，
   且不留离炉记录；强制出炉必须填写原因，标记不合格并写审计；
3. 工件离炉后不再接收读数、不能停用探头、不能重复出炉；
4. 最后一件离炉后炉次自动 UNLOADED，actual_unload_at 取最后离炉时刻；
5. 整炉出炉复用同一判定，跳过已离炉工件（结果/顺序/标记不重写）；
6. 详情/JSON 档案/随炉卡同步离炉顺序、强制原因与当时进度快照；
7. 未签发 / 越序 / 无原因强制等校验。

运行：python3 -m unittest discover -s tests -v
"""
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
LOAD_AT = "2026-09-10T08:00:00"


def _order(wid, **kw):
    return {"workpiece_id": wid, "order_id": "O", "length_mm": 500,
            "width_mm": 400, "height_mm": 300, "weight_kg": 10,
            "powder_batch": kw.pop("powder", "P1"), "compat_group": None,
            "due_at": None, **kw}


def _ts(h, m):
    return f"2026-09-10T{h:02d}:{m:02d}:00"


class PieceUnloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _trial(self, wids, hold=10):
        r = self.c.post("/api/schedule/trial", json={
            "reason": "p", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                         "hold_minutes": hold}],
            "forbidden_pairs": [], "orders": [_order(w) for w in wids]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        return d["new_batches"][0]["batch_id"]

    def _issue_load(self, bid):
        self.assertEqual(self.c.post(f"/api/batches/{bid}/issue").status_code, 200)
        r = self.c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _post(self, bid, readings):
        r = self.c.post(f"/api/batches/{bid}/readings",
                        json={"readings": readings})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _in_window(self, wid, start_h, start_m, count):
        """08:00 起每 5 分钟一个读数，首个为升温点，其余在窗口内。"""
        out = [{"workpiece_id": wid, "ts": LOAD_AT, "metal_temp_c": 25}]
        total = start_h * 60 + start_m
        for i in range(count):
            t = total + i * 5
            out.append({"workpiece_id": wid, "ts": _ts(t // 60, t % 60),
                        "metal_temp_c": 170.0 + (i % 3 - 1)})
        return out

    def _unload_piece(self, bid, wid, **kw):
        return self.c.post(
            f"/api/batches/{bid}/workpieces/{wid}/unload", json=kw)

    def _item(self, payload, wid):
        return next(i for i in payload["items"] if i["workpiece_id"] == wid)

    # ---------------------------------------------------------- 1. 逐件安全出炉
    def test_safe_piece_unload_keeps_batch_in_oven(self):
        bid = self._trial(["W-1", "W-2"], hold=10)
        self._issue_load(bid)
        # W-1：08:05-08:15 连续在窗，08:15 达标；W-2：只到 08:10，尚在保温
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 15))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["state"], "IN_OVEN")
        self.assertIsNone(body["actual_unload_at"])
        res = body["result"]
        self.assertEqual(res["verdict"], "OK")
        self.assertFalse(res["forced"])
        self.assertEqual(res["sequence"], 1)
        self.assertEqual(res["unload_at"], _ts(8, 15))
        self.assertEqual(res["first_met_at"], _ts(8, 15))
        self.assertEqual(res["in_window_minutes"], 10.0)
        self.assertEqual(res["remaining_hold_minutes"], 0.0)
        self.assertEqual(res["flags"], [])
        # 炉次汇总只计在炉工件：W-1 已离炉，不进 items
        self.assertEqual([i["workpiece_id"] for i in body["progress"]["items"]],
                         ["W-2"])
        # 工件状态 DONE，炉次仍 IN_OVEN
        w = self.c.get("/api/workpieces/W-1").get_json()
        self.assertEqual(w["status"], "DONE")
        self.assertEqual(self.c.get(f"/api/batches/{bid}").get_json()["state"],
                         "IN_OVEN")

    def test_unloaded_piece_fields_frozen_in_detail(self):
        bid = self._trial(["W-1", "W-2"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._post(bid, self._in_window("W-2", 8, 5, 3))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        d = self.c.get(f"/api/batches/{bid}").get_json()
        it = self._item(d, "W-1")
        self.assertEqual(it["actual_unload_at"], _ts(8, 15))
        self.assertEqual(it["unload_sequence"], 1)
        self.assertEqual(it["first_met_at"], _ts(8, 15))
        self.assertEqual(it["final_verdict"], "OK")
        self.assertFalse(it["forced"])
        self.assertIsNone(it["force_reason"])
        # 离炉当时进度快照
        snap = it["progress_snapshot"]
        self.assertEqual(snap["status"], "MET")
        self.assertEqual(snap["first_met_at"], _ts(8, 15))
        self.assertEqual(snap["in_window_minutes"], 10.0)
        order = d["unload_order"]
        self.assertEqual([u["workpiece_id"] for u in order], ["W-1"])
        self.assertEqual(order[0]["verdict"], "OK")
        # 仍在炉的 W-2 无离炉字段
        it2 = self._item(d, "W-2")
        self.assertIsNone(it2["actual_unload_at"])
        self.assertIsNone(it2["final_verdict"])
        self.assertIsNone(it2["progress_snapshot"])

    # ---------------------------------------------------------- 2. 拒绝与强制
    def test_unsafe_unload_rejected_with_blockers(self):
        bid = self._trial(["W-1"], hold=30)
        self._issue_load(bid)
        # 08:05-08:10 在窗仅累计 5 分钟，08:10 请求出炉 → 欠时/保温中
        self._post(bid, self._in_window("W-1", 8, 5, 2))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 10))
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["in_window_minutes"], 5.0)
        self.assertEqual(body["remaining_hold_minutes"], 25.0)
        self.assertTrue(body["blockers"])
        self.assertIn("progress", body)
        # 拒绝不留离炉记录
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertIsNone(self._item(d, "W-1")["actual_unload_at"])
        self.assertEqual(self.c.get("/api/workpieces/W-1").get_json()["status"],
                         "IN_OVEN")

    def test_no_reading_unload_blocked_no_reading(self):
        bid = self._trial(["W-1"])
        self._issue_load(bid)
        r = self._unload_piece(bid, "W-1", at=_ts(8, 30))
        self.assertEqual(r.status_code, 409)
        codes = {x["code"] for x in r.get_json()["blockers"]}
        self.assertIn("NO_READING", codes)

    def test_over_temp_cannot_safely_unload(self):
        bid = self._trial(["W-1"], hold=10)
        self._issue_load(bid)
        # 累计达标但历史超温 → 安全条件不满足，阻塞原因含 OVER_TEMP 标记
        self._post(bid, [
            {"workpiece_id": "W-1", "ts": LOAD_AT, "metal_temp_c": 25},
            {"workpiece_id": "W-1", "ts": _ts(8, 5), "metal_temp_c": 170},
            {"workpiece_id": "W-1", "ts": _ts(8, 10), "metal_temp_c": 195},
            {"workpiece_id": "W-1", "ts": _ts(8, 15), "metal_temp_c": 172},
            {"workpiece_id": "W-1", "ts": _ts(8, 20), "metal_temp_c": 171},
        ])
        r = self._unload_piece(bid, "W-1", at=_ts(8, 20))
        self.assertEqual(r.status_code, 409)
        codes = {x["code"] for x in r.get_json()["blockers"]}
        self.assertIn("OVER_TEMP", codes)

    def test_force_requires_reason(self):
        bid = self._trial(["W-1"], hold=30)
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 2))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 10), force=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("reason", r.get_json()["error"])
        # 空白原因同样拒绝
        r = self._unload_piece(bid, "W-1", at=_ts(8, 10), force=True,
                               reason="   ")
        self.assertEqual(r.status_code, 400)
        # 工件仍在炉
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertIsNone(self._item(d, "W-1")["actual_unload_at"])

    def test_force_unload_marks_not_ok_and_audits(self):
        bid = self._trial(["W-1"], hold=30)
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 2))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 10), force=True,
                               reason="后工序急件，线长确认放行")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()["result"]
        self.assertEqual(res["verdict"], "NOT_OK")
        self.assertTrue(res["forced"])
        self.assertEqual(res["reason"], "后工序急件，线长确认放行")
        self.assertIn("UNDER_TIME", res["flags"])
        # 炉次最后一件 → 自动 UNLOADED
        self.assertEqual(r.get_json()["state"], "UNLOADED")
        self.assertEqual(r.get_json()["actual_unload_at"], _ts(8, 10))
        w = self.c.get("/api/workpieces/W-1").get_json()
        self.assertEqual(w["status"], "REWORK_PENDING")
        # 审计与详情
        d = self.c.get(f"/api/batches/{bid}").get_json()
        it = self._item(d, "W-1")
        self.assertEqual(it["final_verdict"], "NOT_OK")
        self.assertTrue(it["forced"])
        self.assertEqual(it["force_reason"], "后工序急件，线长确认放行")
        order = d["unload_order"][0]
        self.assertTrue(order["forced"])
        self.assertEqual(order["reason"], "后工序急件，线长确认放行")
        self.assertIn("UNDER_TIME", order["flags"])
        self.assertEqual(order["progress_snapshot"]["in_window_minutes"], 5.0)
        flag_codes = {f["code"] for f in it["flags"]}
        self.assertIn("UNDER_TIME", flag_codes)

    # ---------------------------------------------------------- 3. 离炉后锁定
    def test_no_readings_after_unload(self):
        bid = self._trial(["W-1", "W-2"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        body = self._post(bid, [{"workpiece_id": "W-1", "ts": _ts(8, 20),
                                 "metal_temp_c": 170}])
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(len(body["rejected"]), 1)
        self.assertIn("离炉", body["rejected"][0]["reason"])
        # 仍在炉的 W-2 读数正常接收
        body = self._post(bid, [{"workpiece_id": "W-2", "ts": _ts(8, 15),
                                 "metal_temp_c": 170}])
        self.assertEqual(body["accepted"], 1)

    def test_no_disable_probe_after_unload(self):
        bid = self._trial(["W-1", "W-2"])
        self.c.post("/api/workpieces/W-1/probes",
                    json={"probes": [{"probe_id": "T1", "offset_c": 0}]})
        self._issue_load(bid)
        self._post(bid, [{"workpiece_id": "W-1", "probe_id": "T1",
                          "ts": _ts(8, i * 5), "metal_temp_c": 170}
                         for i in range(4)])
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T1/disable",
                        json={"reason": "迟报故障"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("离炉", r.get_json()["error"])

    def test_duplicate_unload_rejected(self):
        bid = self._trial(["W-1", "W-2"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 20))
        self.assertEqual(r.status_code, 409)
        self.assertIn("重复出炉", r.get_json()["error"])
        # 强制也不允许重复
        r = self._unload_piece(bid, "W-1", at=_ts(8, 20), force=True,
                               reason="x")
        self.assertEqual(r.status_code, 409)

    # ---------------------------------------------------------- 4. 最后一件与顺序
    def test_last_piece_finalizes_batch_with_last_unload_time(self):
        bid = self._trial(["W-1", "W-2"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._post(bid, self._in_window("W-2", 8, 5, 3))
        r1 = self._unload_piece(bid, "W-1", at=_ts(8, 15))
        self.assertEqual(r1.get_json()["state"], "IN_OVEN")
        r2 = self._unload_piece(bid, "W-2", at=_ts(8, 25))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(r2.get_json()["state"], "UNLOADED")
        # actual_unload_at 取最后离炉时刻（晚于首件）
        self.assertEqual(r2.get_json()["actual_unload_at"], _ts(8, 25))
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(d["state"], "UNLOADED")
        self.assertEqual(d["actual"]["unload_at"], _ts(8, 25))
        self.assertEqual([u["workpiece_id"] for u in d["unload_order"]],
                         ["W-1", "W-2"])
        self.assertEqual(self._item(d, "W-1")["unload_sequence"], 1)
        self.assertEqual(self._item(d, "W-2")["unload_sequence"], 2)
        # 离炉后进度汇总冻结：两件都在，基准为最后离炉时刻
        prog = d["progress"]
        self.assertEqual(prog["basis"]["as_of"], _ts(8, 25))
        self.assertEqual(prog["basis"]["source"], "actual_unload_at")
        self.assertEqual({i["workpiece_id"] for i in prog["items"]},
                         {"W-1", "W-2"})

    # ---------------------------------------------------------- 5. 整炉出炉复用
    def test_batch_unload_skips_already_unloaded(self):
        bid = self._trial(["W-1", "W-2"], hold=30)
        self._issue_load(bid)
        # W-1 保温 35 分钟达标先逐件出炉；W-2 欠时
        self._post(bid, self._in_window("W-1", 8, 5, 7))
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        r1 = self._unload_piece(bid, "W-1", at=_ts(8, 40))
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        self.assertEqual(r1.get_json()["state"], "IN_OVEN")
        r = self.c.post(f"/api/batches/{bid}/unload", json={"at": _ts(8, 45)})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["state"], "UNLOADED")
        # 已离炉的 W-1 被跳过，结果只含 W-2
        self.assertEqual(body["skipped"], ["W-1"])
        self.assertEqual([x["workpiece_id"] for x in body["results"]], ["W-2"])
        self.assertEqual(body["results"][0]["verdict"], "NOT_OK")
        # W-1 的顺序/时刻/判定不被重写；actual_unload_at 取最后离炉时刻
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(d["actual"]["unload_at"], _ts(8, 45))
        i1 = self._item(d, "W-1")
        self.assertEqual(i1["unload_sequence"], 1)
        self.assertEqual(i1["actual_unload_at"], _ts(8, 40))
        self.assertEqual(i1["final_verdict"], "OK")
        i2 = self._item(d, "W-2")
        self.assertEqual(i2["unload_sequence"], 2)
        self.assertEqual(i2["actual_unload_at"], _ts(8, 45))
        self.assertEqual(i2["final_verdict"], "NOT_OK")
        self.assertEqual([u["workpiece_id"] for u in d["unload_order"]],
                         ["W-1", "W-2"])
        self.assertEqual(self.c.get("/api/workpieces/W-1").get_json()["status"],
                         "DONE")

    def test_batch_unload_when_all_pieces_gone(self):
        # 最后一件逐件离炉时炉次已自动转 UNLOADED：整炉接口越序 409，
        # 已离炉件的离炉时刻保持不变
        bid = self._trial(["W-1"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        r = self.c.post(f"/api/batches/{bid}/unload", json={"at": _ts(8, 20)})
        self.assertEqual(r.status_code, 409)
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(d["actual"]["unload_at"], _ts(8, 15))

    def test_batch_unload_preserves_earlier_ok_and_audits_both(self):
        # W-1 达标先离炉（OK 无标记）；整炉出炉只判 W-2（欠时 NOT_OK），
        # W-1 的标记不被补写，两件审计/顺序齐全
        bid = self._trial(["W-1", "W-2"], hold=30)
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 7))
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 40))
        r = self.c.post(f"/api/batches/{bid}/unload", json={"at": _ts(8, 45)})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = {x["workpiece_id"]: x for x in r.get_json()["results"]}
        self.assertEqual(set(res), {"W-2"})
        self.assertEqual(res["W-2"]["verdict"], "NOT_OK")
        self.assertIn("UNDER_TIME", res["W-2"]["flags"])
        d = self.c.get(f"/api/batches/{bid}").get_json()
        i1 = self._item(d, "W-1")
        self.assertEqual(i1["final_verdict"], "OK")
        self.assertEqual(i1["flags"], [])
        actions = {u["workpiece_id"]: u for u in d["unload_order"]}
        self.assertEqual(actions["W-1"]["verdict"], "OK")
        self.assertFalse(actions["W-1"]["forced"])
        self.assertEqual(actions["W-2"]["verdict"], "NOT_OK")
        self.assertEqual([u["sequence"] for u in d["unload_order"]], [1, 2])

    # ---------------------------------------------------------- 6. 三端同步
    def test_archive_and_card_carry_unload_order_force_snapshot(self):
        bid = self._trial(["W-1", "W-2"], hold=30)
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 7))   # 35 分钟，08:35 达标
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 40))
        self._unload_piece(bid, "W-2", at=_ts(8, 20), force=True,
                           reason="插单抢炉")
        detail = self.c.get(f"/api/batches/{bid}").get_json()
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        # 档案与详情的离炉顺序、强制原因、快照一致
        self.assertEqual(arch["unload_order"], detail["unload_order"])
        self.assertEqual(arch["progress"], detail["progress"])
        u2 = detail["unload_order"][1]
        self.assertEqual(u2["workpiece_id"], "W-2")
        self.assertEqual(u2["reason"], "插单抢炉")
        self.assertTrue(u2["forced"])
        self.assertEqual(u2["progress_snapshot"]["remaining_hold_minutes"], 25.0)
        # 整炉离炉后进度汇总取逐件离炉当时冻结快照（非按最后时刻重算）
        frozen = {i["workpiece_id"]: i for i in detail["progress"]["items"]}
        self.assertEqual(frozen["W-2"]["in_window_minutes"], 5.0)
        self.assertEqual(frozen["W-2"]["remaining_hold_minutes"], 25.0)
        self.assertEqual(frozen["W-1"]["first_met_at"], _ts(8, 35))
        # 随炉卡：离炉记录段、顺序、强制原因、离炉时刻
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("逐件离炉记录", card)
        self.assertIn("插单抢炉", card)
        self.assertIn(_ts(8, 40), card)
        self.assertIn(_ts(8, 20), card)
        self.assertIn("强制出炉", card)

    def test_in_oven_progress_excludes_unloaded_pieces(self):
        bid = self._trial(["W-1", "W-2"])
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        self._post(bid, self._in_window("W-2", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 15))
        # 进度接口（无 as_of）只汇总仍在炉的 W-2
        prog = self.c.get(f"/api/batches/{bid}/progress").get_json()["progress"]
        self.assertEqual([i["workpiece_id"] for i in prog["items"]], ["W-2"])
        # 历史复盘（早于 W-1 离炉时刻）两件都在炉
        old = self.c.get(
            f"/api/batches/{bid}/progress?as_of={_ts(8, 10)}").get_json()
        old_ids = {i["workpiece_id"] for i in old["progress"]["items"]}
        self.assertEqual(old_ids, {"W-1", "W-2"})

    # ---------------------------------------------------------- 7. 状态与参数校验
    def test_unissued_piece_unload_flagged(self):
        bid = self._trial(["W-1"])
        r = self._unload_piece(bid, "W-1", at=_ts(8, 30))
        self.assertEqual(r.status_code, 409)
        flags = self.c.get("/api/workpieces/W-1").get_json()["flags"]
        self.assertTrue(any(f["code"] == "UNISSUED_UNLOAD" for f in flags))

    def test_piece_not_in_batch_404(self):
        bid = self._trial(["W-1"])
        self._issue_load(bid)
        r = self._unload_piece(bid, "W-GHOST", at=_ts(8, 30))
        self.assertEqual(r.status_code, 404)

    def test_unknown_batch_404(self):
        r = self.c.post("/api/batches/9999/workpieces/W-1/unload", json={})
        self.assertEqual(r.status_code, 404)

    def test_bad_at_400(self):
        bid = self._trial(["W-1"])
        self._issue_load(bid)
        r = self._unload_piece(bid, "W-1", at="not-a-time")
        self.assertEqual(r.status_code, 400)

    def test_rework_possible_after_forced_unload(self):
        # 强制出炉 → REWORK_PENDING → 返工 → PENDING(is_rework=1)
        bid = self._trial(["W-1"], hold=30)
        self._issue_load(bid)
        self._post(bid, self._in_window("W-1", 8, 5, 2))
        self._unload_piece(bid, "W-1", at=_ts(8, 10), force=True, reason="x")
        r = self.c.post("/api/workpieces/W-1/rework")
        self.assertEqual(r.status_code, 200)
        w = self.c.get("/api/workpieces/W-1").get_json()
        self.assertEqual(w["status"], "PENDING")
        self.assertTrue(w["is_rework"])

    def test_incompat_pair_force_unload(self):
        # 同炉禁配组：排产引擎不会把禁配组编进同炉，直接构造同炉数据
        bid = self._trial(["W-1"])
        self.c.post(f"/api/batches/{bid}/issue")
        db_path = os.path.join(self.tmp.name, "t.sqlite")
        import sqlite3
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute("UPDATE workpieces SET compat_group='A' WHERE id='W-1'")
        db.execute(
            "INSERT INTO workpieces (id, order_id, length_mm, width_mm,"
            " height_mm, weight_kg, powder_batch, compat_group, due_at, status)"
            " VALUES ('W-2','O',500,400,300,10,'P1','B',NULL,'IN_OVEN')")
        db.execute(
            "INSERT INTO batch_items (batch_id, workpiece_id, hanger_slot,"
            " slots_used, snap_powder_batch, snap_temp_min_c, snap_temp_max_c,"
            " snap_hold_minutes) VALUES (?, 'W-2', 3, 1, 'P1', 160, 180, 10)",
            (bid,))
        import json as _json
        row = db.execute(
            "SELECT id, params_json FROM schedule_versions WHERE id="
            " (SELECT version_id FROM batches WHERE id=?)", (bid,)).fetchone()
        params = _json.loads(row["params_json"])
        params["forbidden_pairs"] = [["A", "B"]]
        db.execute("UPDATE schedule_versions SET params_json=? WHERE id=?",
                   (_json.dumps(params, ensure_ascii=False), row["id"]))
        db.commit()
        db.close()
        self.c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        self._post(bid, self._in_window("W-1", 8, 5, 3))
        r = self._unload_piece(bid, "W-1", at=_ts(8, 15))
        self.assertEqual(r.status_code, 409)
        codes = {x["code"] for x in r.get_json()["blockers"]}
        self.assertIn("INCOMPAT_CONFLICT", codes)
        r = self._unload_piece(bid, "W-1", at=_ts(8, 15), force=True,
                               reason="禁配组同炉，隔离返工")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("INCOMPAT_CONFLICT", r.get_json()["result"]["flags"])
        # 炉次保持 IN_OVEN（W-2 仍在炉）
        self.assertEqual(r.get_json()["state"], "IN_OVEN")


if __name__ == "__main__":
    unittest.main()
