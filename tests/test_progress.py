"""在炉固化进度与安全出炉预测测试：

1. 新接口 GET /batches/<id>/progress 接受 as_of，逐件返回最新有效测温时刻、
   当前最低校正温度、已累计/剩余保温分钟、读数新鲜度与阻塞原因；
2. 仅最新温度在许可区间且有效探头达标、读数未超时才按连续保温推算安全出炉；
3. 欠温 / 超温 / 缺报（超时未报）/ 探头不足 → BLOCKED 并给原因；
4. 炉次级预测取所有工件最晚安全出炉时刻，计划出炉过早给出分钟数，
   存在不可预测工件时计划无法判定；
5. 已达标工件保留首次达标时刻，后续异常读数保留告警；
6. as_of 截断读数（历史复盘）；
7. 新读数 / 停用探头后即时重算；同一进度快照贯穿详情 / 档案 / 随炉卡。

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


def _ts(i, step=5, base_h=8, base_m=0):
    total = base_h * 60 + base_m + i * step
    return f"2026-09-10T{total // 60:02d}:{total % 60:02d}:00"


def _readings(wid, temps, pid=None, step=5, start=0):
    return [{"workpiece_id": wid, "probe_id": pid, "ts": _ts(i + start, step),
             "metal_temp_c": t} for i, t in enumerate(temps)]


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _trial(self, orders, hold=10, tmin=160, tmax=180):
        r = self.c.post("/api/schedule/trial", json={
            "reason": "p", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": tmin, "temp_max_c": tmax,
                         "hold_minutes": hold}],
            "forbidden_pairs": [], "orders": orders})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        return d["new_batches"][0]["batch_id"], d["new_batches"][0]

    def _load(self, bid):
        self.assertEqual(self.c.post(f"/api/batches/{bid}/issue").status_code, 200)
        r = self.c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _progress(self, bid, as_of=None):
        url = f"/api/batches/{bid}/progress"
        if as_of:
            url += f"?as_of={as_of}"
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["progress"]

    def _item(self, prog, wid):
        return next(i for i in prog["items"] if i["workpiece_id"] == wid)

    def _post(self, bid, readings):
        r = self.c.post(f"/api/batches/{bid}/readings", json={"readings": readings})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    # ---------------------------------------------------------- 1. 基础推算
    def test_tracking_predicts_safe_unload(self):
        bid, planned = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        # 08:05 起进入窗口，5 分钟一段
        self._post(bid, _readings("W-1", [25, 165, 170]))
        prog = self._progress(bid, "2026-09-10T08:10:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "TRACKING")
        self.assertEqual(it["latest_reading_at"], "2026-09-10T08:10:00")
        self.assertEqual(it["latest_temp_c"], 170.0)
        self.assertEqual(it["reading_freshness_minutes"], 0.0)
        self.assertFalse(it["stale"])
        self.assertEqual(it["in_window_minutes"], 5.0)
        self.assertEqual(it["remaining_hold_minutes"], 5.0)
        # 安全出炉 = 最新测温 08:10 + 剩余 5 分钟
        self.assertEqual(it["safe_unload_at"], "2026-09-10T08:15:00")
        self.assertIsNone(it["first_met_at"])
        self.assertEqual(it["blockers"], [])
        self.assertEqual(prog["prediction_status"], "PREDICTABLE")
        self.assertEqual(prog["safe_unload_at"], "2026-09-10T08:15:00")
        # 基准时刻注明
        self.assertEqual(prog["basis"]["as_of"], "2026-09-10T08:10:00")
        self.assertEqual(prog["basis"]["source"], "query")
        # 计划出炉时刻不被改写
        self.assertEqual(prog["planned_unload_at"],
                         planned["planned_unload_at"])

    def test_no_reading_blocked(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        prog = self._progress(bid, "2026-09-10T08:30:00")
        self.assertEqual(prog["prediction_status"], "BLOCKED")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "BLOCKED")
        self.assertIsNone(it["safe_unload_at"])
        codes = {x["code"] for x in it["blockers"]}
        self.assertIn("NO_READING", codes)
        self.assertEqual(prog["plan_status"], "CANNOT_VERIFY")

    # ---------------------------------------------------------- 2. 阻塞原因
    def test_under_temp_blocker(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        self._post(bid, _readings("W-1", [165, 170, 150]))
        prog = self._progress(bid, "2026-09-10T08:10:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "BLOCKED")
        self.assertEqual([x["code"] for x in it["blockers"]], ["UNDER_TEMP"])
        self.assertIn("150", it["blockers"][0]["message"])

    def test_over_temp_blocker(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        self._post(bid, _readings("W-1", [165, 170, 195]))
        prog = self._progress(bid, "2026-09-10T08:10:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "BLOCKED")
        self.assertEqual([x["code"] for x in it["blockers"]], ["OVER_TEMP"])

    def test_historical_over_temp_recovered_keeps_alert(self):
        # 未达标期间曾超温、最新已回到窗口：不阻塞，仅保留告警
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        self._post(bid, _readings("W-1", [165, 195, 170, 171]))
        prog = self._progress(bid, "2026-09-10T08:15:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "TRACKING")
        self.assertEqual(it["blockers"], [])
        self.assertIn("OVER_TEMP_HISTORY", {a["code"] for a in it["alerts"]})

    def test_stale_reading_blocker(self):
        # 保温要求 30 分钟：08:10 时仅累计 5 分钟，随后缺报到 08:25（超时）→ BLOCKED
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}],
            hold=30)
        self._load(bid)
        self._post(bid, _readings("W-1", [165, 170, 171]))
        # 最后读数 08:10，基准 08:25 → 15 分钟未报，超过缺报阈值 10
        prog = self._progress(bid, "2026-09-10T08:25:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "BLOCKED")
        self.assertIn("STALE_READING", [x["code"] for x in it["blockers"]])
        self.assertTrue(it["stale"])
        self.assertEqual(it["reading_freshness_minutes"], 15.0)
        # 基准回到 08:15（间隔 5 分钟）→ 仍可预测
        prog2 = self._progress(bid, "2026-09-10T08:15:00")
        self.assertEqual(self._item(prog2, "W-1")["status"], "TRACKING")

    def test_insufficient_probes_blocker_and_disable_recalc(self):
        # MIN_VALID_PROBES=2 需要在 create_app 时配置：单独建一个 app
        app2 = create_app({"DATABASE": os.path.join(self.tmp.name, "t2.sqlite"),
                           "TESTING": True, "MIN_VALID_PROBES": 2})
        c = app2.test_client()
        r = c.post("/api/schedule/trial", json={
            "reason": "p", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                         "hold_minutes": 10}],
            "forbidden_pairs": [], "orders": [
                {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
                 "width_mm": 400, "height_mm": 300, "weight_kg": 10,
                 "powder_batch": "P1", "compat_group": None, "due_at": None}]})
        bid = r.get_json()["new_batches"][0]["batch_id"]
        c.post("/api/workpieces/W-1/probes", json={
            "probes": [{"probe_id": "T1", "offset_c": 0},
                       {"probe_id": "T2", "offset_c": 0}]})
        c.post(f"/api/batches/{bid}/issue")
        c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        c.post(f"/api/batches/{bid}/readings",
               json={"readings": _readings("W-1", [165, 170, 171], pid="T1")})
        prog = c.get(f"/api/batches/{bid}/progress?as_of=2026-09-10T08:10:00"
                     ).get_json()["progress"]
        it = next(i for i in prog["items"] if i["workpiece_id"] == "W-1")
        # T2 全程无读数 → 有效探头 1 < 2
        self.assertEqual(it["status"], "BLOCKED")
        self.assertIn("INSUFFICIENT_PROBES",
                      [x["code"] for x in it["blockers"]])

        # 停用接口即时重算：响应自带最新进度（基准为服务器当前时刻；
        # 读数相对该时刻已超时，但有效探头数口径仍可校验）
        r = c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                   json={"reason": "装机时发现通道损坏"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["progress"]["basis"]["source"], "now")
        self.assertEqual(body["recalc"]["after"]["valid_probe_count"], 1)

        # 停用后按显式基准查询：T2 已停用，仅 T1 有效，仍不足 2 个
        prog3 = c.get(f"/api/batches/{bid}/progress?as_of=2026-09-10T08:10:00"
                      ).get_json()["progress"]
        it3 = next(i for i in prog3["items"] if i["workpiece_id"] == "W-1")
        self.assertEqual(it3["valid_probe_count"], 1)
        self.assertIn("INSUFFICIENT_PROBES",
                      [x["code"] for x in it3["blockers"]])
        self.assertEqual(it3["status"], "BLOCKED")

    # ---------------------------------------------------------- 3. 达标与告警
    def test_met_keeps_first_met_time_and_keeps_later_alerts(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}],
            hold=10)
        self._load(bid)
        # 08:05-08:20 连续在窗口 → 08:15 累计满 10 分钟（首次达标）
        self._post(bid, _readings("W-1", [25, 165, 170, 172, 171]))
        prog = self._progress(bid, "2026-09-10T08:20:00")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "MET")
        self.assertEqual(it["first_met_at"], "2026-09-10T08:15:00")
        self.assertEqual(it["safe_unload_at"], "2026-09-10T08:15:00")
        self.assertEqual(it["remaining_hold_minutes"], 0.0)
        self.assertEqual(prog["prediction_status"], "ALL_MET")
        first_met = it["first_met_at"]

        # 达标后出现欠温读数：状态保持 MET，安全出炉时刻不变，告警保留
        self._post(bid, [{"workpiece_id": "W-1",
                          "ts": "2026-09-10T08:25:00", "metal_temp_c": 140}])
        prog2 = self._progress(bid, "2026-09-10T08:25:00")
        it2 = self._item(prog2, "W-1")
        self.assertEqual(it2["status"], "MET")
        self.assertEqual(it2["first_met_at"], first_met)
        self.assertEqual(it2["safe_unload_at"], first_met)
        self.assertEqual(it2["blockers"], [])
        self.assertIn("UNDER_TEMP", {a["code"] for a in it2["alerts"]})
        self.assertTrue(any("已达标" in a["message"] for a in it2["alerts"]))

        # 达标后再出现超温读数：同样仅告警
        self._post(bid, [{"workpiece_id": "W-1",
                          "ts": "2026-09-10T08:30:00", "metal_temp_c": 200}])
        prog3 = self._progress(bid, "2026-09-10T08:30:00")
        it3 = self._item(prog3, "W-1")
        self.assertEqual(it3["status"], "MET")
        self.assertEqual(it3["safe_unload_at"], first_met)
        self.assertIn("OVER_TEMP", {a["code"] for a in it3["alerts"]})

    # ---------------------------------------------------------- 4. 炉次级预测
    def test_batch_prediction_takes_latest_safe_unload(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None},
            {"workpiece_id": "W-2", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        # W-1 08:05 进入窗口；W-2 到 08:10 才进入窗口
        self._post(bid, _readings("W-1", [25, 165, 170]))
        self._post(bid, _readings("W-2", [25, 140, 170]))
        prog = self._progress(bid, "2026-09-10T08:10:00")
        i1 = self._item(prog, "W-1")
        i2 = self._item(prog, "W-2")
        self.assertEqual(i1["safe_unload_at"], "2026-09-10T08:15:00")
        self.assertEqual(i2["safe_unload_at"], "2026-09-10T08:20:00")
        # 炉次级取最晚（全部工件均可预测时）
        self.assertEqual(prog["safe_unload_at"], "2026-09-10T08:20:00")
        self.assertEqual(prog["prediction_status"], "PREDICTABLE")
        self.assertEqual(prog["met_count"], 0)
        self.assertEqual(prog["tracking_count"], 2)

        # W-2 出现欠温 → 炉次 BLOCKED，不再给整体安全出炉时刻，
        # 计划是否有效无法判定
        self._post(bid, [{"workpiece_id": "W-2",
                          "ts": "2026-09-10T08:15:00", "metal_temp_c": 140}])
        prog2 = self._progress(bid, "2026-09-10T08:15:00")
        self.assertEqual(prog2["prediction_status"], "BLOCKED")
        self.assertIsNone(prog2["safe_unload_at"])
        self.assertEqual(prog2["plan_status"], "CANNOT_VERIFY")
        self.assertIsNone(prog2["planned_unload_early_minutes"])

    def test_planned_unload_too_early_minutes(self):
        # hold=60：计划出炉 = 08:00 + 升温约 38.95 + 60 ≈ 09:38:57；
        # 实际 09:00 才进入窗口，连续保温下安全出炉 10:00，计划过早约 21 分钟
        bid, planned = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}],
            hold=60)
        self._load(bid)
        # 09:00 / 09:05 / 09:10 / 09:15 均在窗口
        readings = [{"workpiece_id": "W-1", "ts": ts, "metal_temp_c": 175}
                    for ts in ("2026-09-10T09:00:00", "2026-09-10T09:05:00",
                               "2026-09-10T09:10:00", "2026-09-10T09:15:00")]
        self._post(bid, readings)
        prog = self._progress(bid, "2026-09-10T09:15:00")
        self.assertEqual(prog["plan_status"], "TOO_EARLY")
        self.assertEqual(prog["safe_unload_at"], "2026-09-10T10:00:00")
        # 计划出炉 09:36:27（升温 36.45 + 保温 60）→ 过早 23.55 分钟
        self.assertAlmostEqual(prog["planned_unload_early_minutes"],
                               23.55, places=1)
        self.assertEqual(prog["planned_unload_at"],
                         planned["planned_unload_at"])

    def test_planned_unload_ok_when_ahead(self):
        # 实际 08:20 即进入窗口，比计划固化开始提前 → 安全出炉早于计划
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}],
            hold=60)
        self._load(bid)
        self._post(bid, [{"workpiece_id": "W-1", "ts": ts, "metal_temp_c": 175}
                         for ts in ("2026-09-10T08:20:00", "2026-09-10T08:25:00",
                                    "2026-09-10T08:30:00")])
        prog = self._progress(bid, "2026-09-10T08:30:00")
        self.assertEqual(prog["plan_status"], "OK")
        self.assertEqual(prog["planned_unload_early_minutes"], 0.0)

    # ---------------------------------------------------------- 5. as_of 截断
    def test_as_of_truncates_readings(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        self._post(bid, _readings("W-1", [25, 165, 170, 172, 171]))
        early = self._progress(bid, "2026-09-10T08:10:00")
        it = self._item(early, "W-1")
        self.assertEqual(it["latest_reading_at"], "2026-09-10T08:10:00")
        self.assertEqual(it["in_window_minutes"], 5.0)
        # 更晚的基准看得到全部读数
        later = self._progress(bid, "2026-09-10T08:20:00")
        self.assertEqual(self._item(later, "W-1")["latest_reading_at"],
                         "2026-09-10T08:20:00")
        # 非法 as_of → 400
        r = self.c.get(f"/api/batches/{bid}/progress?as_of=not-a-time")
        self.assertEqual(r.status_code, 400)

    # ---------------------------------------------------------- 6. 即时重算
    def test_readings_post_returns_recalculated_progress(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        body = self._post(bid, _readings("W-1", [25, 165, 170]))
        self.assertIn("progress", body)
        it = self._item(body["progress"], "W-1")
        self.assertEqual(it["latest_temp_c"], 170.0)
        self.assertEqual(body["progress"]["basis"]["source"], "now")
        # 纯重复回传：无新读数，但仍返回当前进度快照
        dup = self._post(bid, _readings("W-1", [25, 165, 170]))
        self.assertEqual(dup["accepted"], 0)
        self.assertIn("progress", dup)
        self.assertEqual(self._item(dup["progress"], "W-1")["latest_temp_c"],
                         170.0)

    # ---------------------------------------------------------- 7. 三端同一快照
    def test_same_progress_snapshot_in_detail_archive_card(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}],
            hold=30)
        self._load(bid)
        self._post(bid, _readings("W-1", [25, 165, 170, 172]))
        as_of = "2026-09-10T08:15:00"
        detail = self.c.get(f"/api/batches/{bid}?as_of={as_of}").get_json()
        arch = self.c.get(f"/api/batches/{bid}/archive?as_of={as_of}").get_json()
        self.assertEqual(detail["progress"], arch["progress"])
        card = self.c.get(f"/api/batches/{bid}/card?as_of={as_of}")
        self.assertEqual(card.status_code, 200)
        html_text = card.get_data(as_text=True)
        self.assertIn("在炉固化进度与安全出炉预测", html_text)
        self.assertIn("计算基准时刻", html_text)
        self.assertIn(as_of, html_text)
        self.assertIn("保温中，可预测", html_text)

    def test_unloaded_batch_progress_basis_defaults_to_actual_unload(self):
        bid, _ = self._trial([
            {"workpiece_id": "W-1", "order_id": "O", "length_mm": 500,
             "width_mm": 400, "height_mm": 300, "weight_kg": 10,
             "powder_batch": "P1", "compat_group": None, "due_at": None}])
        self._load(bid)
        self._post(bid, _readings("W-1", [25, 165, 170, 172, 171]))
        self.c.post(f"/api/batches/{bid}/unload",
                    json={"at": "2026-09-10T08:30:00"})
        prog = self._progress(bid)  # 不传 as_of
        self.assertEqual(prog["basis"]["as_of"], "2026-09-10T08:30:00")
        self.assertEqual(prog["basis"]["source"], "actual_unload_at")
        it = self._item(prog, "W-1")
        self.assertEqual(it["status"], "MET")

    def test_progress_not_available_endpoint_404(self):
        r = self.c.get("/api/batches/9999/progress")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
