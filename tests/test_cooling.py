"""冷却放行功能测试：

1. 粉料资料登记包装温度上限/低温保持时长，签发时冻结到工件；合格件离炉
   进入 COOLING（暂不计为完成 DONE）；
2. 冷却测温写入：仅 COOLING 件接收，按 (炉次, 工件, 时刻) 幂等去重；
   早于离炉时刻拒收；
3. 连续低温区间：乱序、同时刻不同温度、采样间隔过长、再次升温均截断；
   查询给当前读数、有效保持分钟、最早放行时刻与未满足项；
4. 未达门限正常放行 409；紧急搬运须理由并送返工处置；
5. 结案须引用一次有效放行（COOLING 件阻塞，正常放行后可结案）；
6. 批次查询/档案保留冻结门限、完整读数、区间中断与人工决定。

运行：python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

from ovenline import create_app
from ovenline import cooling as cooling_mod

OVEN = {
    "id": "OVEN-1", "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02, "ambient_c": 25,
    "turnaround_minutes": 15, "hanger_slots": 12, "hanger_spacing_mm": 500,
    "hanger_max_load_kg": 80,
}
LOAD_AT = "2026-09-10T08:00:00"
UNLOAD_AT = "2026-09-10T09:00:00"


def _ts(mins_after_nine, base_h=9, base_m=0):
    total = base_h * 60 + base_m + mins_after_nine
    return f"2026-09-10T{total // 60:02d}:{total % 60:02d}:00"


class CoolingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True, "COOLING_GAP_MINUTES": 10})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 辅助
    def _setup(self, wids=("W-1",), pack_limit=50.0, low_hold=20.0,
               cure_hold=10, powder="P1"):
        pw = {"batch_no": powder, "temp_min_c": 160, "temp_max_c": 180,
              "hold_minutes": cure_hold}
        if pack_limit is not None:
            pw.update(pack_temp_limit_c=pack_limit,
                      low_temp_hold_minutes=low_hold)
        r = self.c.post("/api/schedule/trial", json={
            "reason": "cooling", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [pw], "forbidden_pairs": [],
            "orders": [{"workpiece_id": w, "order_id": "O", "length_mm": 500,
                        "width_mm": 400, "height_mm": 300, "weight_kg": 10,
                        "powder_batch": powder, "compat_group": None,
                        "due_at": None} for w in wids]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        bid = r.get_json()["new_batches"][0]["batch_id"]
        self.assertEqual(self.c.post(f"/api/batches/{bid}/issue").status_code, 200)
        self.assertEqual(
            self.c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
                .status_code, 200)
        return bid

    def _cure_and_unload(self, bid, wid, unload_at=UNLOAD_AT):
        """回传充分固化读数并安全离炉，返回离炉响应。"""
        readings = [{"workpiece_id": wid, "ts": LOAD_AT, "metal_temp_c": 25}]
        # 08:05 起每 5 分钟一个在窗读数，一直到离炉时刻前，避免尾部缺报
        t0 = 8 * 60 + 5
        t1 = 9 * 60
        for i, t in enumerate(range(t0, t1, 5)):
            readings.append({"workpiece_id": wid,
                             "ts": f"2026-09-10T{t // 60:02d}:{t % 60:02d}:00",
                             "metal_temp_c": 170.0 + (i % 3 - 1) * 0.5})
        r = self.c.post(f"/api/batches/{bid}/readings",
                        json={"readings": readings})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.c.post(f"/api/batches/{bid}/workpieces/{wid}/unload",
                        json={"at": unload_at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _cool_readings(self, bid, wid, seq, start_min=0, step=5):
        """seq: 温度序列；时刻从离炉后 start_min 分钟起，每 step 分钟一点。"""
        out = []
        for i, temp in enumerate(seq):
            m = start_min + i * step
            total = 9 * 60 + m
            out.append({"ts": f"2026-09-10T{total // 60:02d}:{total % 60:02d}:00",
                        "surface_temp_c": temp})
        r = self.c.post(f"/api/batches/{bid}/workpieces/{wid}/cooling-readings",
                        json={"readings": out, "at": _ts(start_min + len(seq) * step)})
        return r, out

    def _cool(self, bid, wid, **kw):
        return self.c.get(f"/api/batches/{bid}/workpieces/{wid}/cooling",
                          query_string=kw).get_json()["cooling"]

    # ----------------------------------------------------- 1. 冻结门限/状态
    def test_powder_thresholds_frozen_at_issue_and_safe_unload_is_cooling(self):
        bid = self._setup()
        # 粉料主数据保留门限
        r = self._cure_and_unload(bid, "W-1")
        self.assertEqual(r["result"]["verdict"], "OK")
        w = self.c.get("/api/workpieces/W-1").get_json()
        self.assertEqual(w["status"], "COOLING")
        d = self.c.get(f"/api/batches/{bid}").get_json()
        it = next(i for i in d["items"] if i["workpiece_id"] == "W-1")
        self.assertEqual(it["snap_pack_temp_limit_c"], 50.0)
        self.assertEqual(it["snap_low_temp_hold_minutes"], 20.0)
        view = it["cooling"]
        self.assertEqual(view["pack_temp_limit_c"], 50.0)
        self.assertEqual(view["low_temp_hold_minutes"], 20.0)
        self.assertIsNone(view["release"])
        # 炉次仍 IN_OVEN（单工件离炉即转 UNLOADED，但工件是 COOLING 不是 DONE）
        self.assertEqual(d["state"], "UNLOADED")

    def test_powder_thresholds_must_be_pair_and_nonnegative(self):
        payload = {"reason": "x", "start_at": LOAD_AT, "ovens": [OVEN],
                   "powders": [{"batch_no": "P1", "temp_min_c": 160,
                                "temp_max_c": 180, "hold_minutes": 10,
                                "pack_temp_limit_c": 50}],
                   "orders": []}
        r = self.c.post("/api/schedule/trial", json=payload)
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时给出", r.get_json()["error"])
        payload["powders"][0]["low_temp_hold_minutes"] = -1
        del payload["powders"][0]["pack_temp_limit_c"]
        r = self.c.post("/api/schedule/trial", json=payload)
        self.assertEqual(r.status_code, 400)

    # ----------------------------------------------------- 2. 写入校验
    def test_cooling_readings_rejected_before_unload_and_after_release(self):
        bid = self._setup()
        # 尚未离炉：拒收
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
                        json={"readings": [{"ts": UNLOAD_AT,
                                            "surface_temp_c": 45}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["accepted"], 0)
        self.assertIn("尚未离炉", r.get_json()["rejected"][0]["reason"])
        self._cure_and_unload(bid, "W-1")
        # 早于离炉时刻：拒收
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
                        json={"readings": [{"ts": "2026-09-10T08:59:00",
                                            "surface_temp_c": 45}]})
        body = r.get_json()
        self.assertEqual(body["accepted"], 0)
        self.assertIn("早于实际离炉时刻", body["rejected"][0]["reason"])

    def test_same_point_duplicate_saved_and_truncates(self):
        """时刻+温度完全相同的重复提交：保存本次提交、记录中断、从该点后重算。"""
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        r1, out = self._cool_readings(bid, "W-1", [45.0, 44.0, 45.0, 44.0])
        # 09:00-09:15 连续 4 点，累计 15 分钟
        self.assertEqual(r1.get_json()["accepted"], 4)
        ev = r1.get_json()["cooling"]
        self.assertEqual(ev["held_low_temp_minutes"], 15.0)
        # 重复提交 09:00、09:05（时刻+温度完全相同）：不幂等忽略，
        # 保存两条 TS_DUPLICATE 并截断区间
        r2 = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": out[:2], "at": _ts(30)})
        b2 = r2.get_json()
        self.assertEqual(b2["accepted"], 0)
        self.assertEqual(b2["duplicates"], 2)
        self.assertEqual(b2["conflicts"], 2)
        ev = b2["cooling"]
        self.assertIn("TS_DUPLICATE",
                      [i["code"] for i in ev["interruptions"]])
        # 不能拼接中断前后：09:10/09:15 是重复提交之前已采信的读数，不能拿来
        # 与重复点之后拼接——当前区间作废，须从重复点之后新到的读数重新累计
        self.assertEqual(ev["held_low_temp_minutes"], 0.0)
        self.assertIsNone(ev["current_segment"])
        # 完整读数保留：09:00 与 09:05 各两条
        d = self.c.get(f"/api/batches/{bid}").get_json()
        rows = d["items"][0]["cooling"]["readings"]
        dup = [x for x in rows if x["kind"] == "TS_DUPLICATE"]
        self.assertEqual(len(dup), 2)
        # 即使此前累计已 15 分钟，重复截断后未重新保持满 20 分钟，NORMAL 409
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(30)})
        self.assertEqual(r.status_code, 409)
        codes = {u["code"] for u in r.get_json()["unmet"]}
        self.assertIn("HOLD_NOT_MET", codes)
        # 重复点之后新到的读数重新累计（09:20 锚定新段，09:25 累计 5 分钟）
        r3, _ = self._cool_readings(bid, "W-1", [45.0, 45.0],
                                    start_min=20, step=5)
        ev3 = r3.get_json()["cooling"]
        self.assertEqual(ev3["current_segment"]["start"],
                         "2026-09-10T09:20:00")
        self.assertEqual(ev3["held_low_temp_minutes"], 5.0)

    # ----------------------------------------------------- 3. 区间截断
    def test_normal_low_streak_hold_and_earliest_release(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # 09:00/09:05/09:10/09:15 四点都 <=50：累计 15 分钟（保守口径）
        r, _ = self._cool_readings(bid, "W-1", [49.0, 48.0, 47.0, 46.0])
        ev = r.get_json()["cooling"]
        self.assertEqual(ev["held_low_temp_minutes"], 15.0)
        self.assertEqual(ev["remaining_hold_minutes"], 5.0)
        # 最早放行 = 最新读数 09:15 + 5 分钟 = 09:20
        self.assertEqual(ev["earliest_release_at"], "2026-09-10T09:20:00")
        self.assertFalse(ev["releasable"])
        codes = {u["code"] for u in ev["unmet"]}
        self.assertIn("HOLD_NOT_MET", codes)

    def test_reheat_truncates_segment(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0, 45.0, 60.0])
        # 09:10 再次升温：前段（09:00-09:05，5 分钟）截断清零
        ev = self._cool(bid, "W-1", at=_ts(10))
        self.assertEqual(ev["held_low_temp_minutes"], 0.0)
        self.assertIsNone(ev["current_segment"])
        breaks = [i["code"] for i in ev["interruptions"]]
        self.assertIn("REHEAT", breaks)
        self.assertEqual(ev["latest_surface_temp_c"], 60.0)
        codes = {u["code"] for u in ev["unmet"]}
        self.assertIn("REHEAT", codes)
        # 下一条低温读数另起新区间
        r, _ = self._cool_readings(bid, "W-1", [45.0], start_min=15)
        ev = r.get_json()["cooling"]
        self.assertEqual(ev["current_segment"]["start"], "2026-09-10T09:15:00")
        self.assertEqual(ev["held_low_temp_minutes"], 0.0)

    def test_long_gap_truncates_and_resets_hold(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # 09:00、09:05 低温（5 分钟），下一条 09:30（间隔 25 分钟 > 阈值 10）
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [
                {"ts": "2026-09-10T09:00:00", "surface_temp_c": 45},
                {"ts": "2026-09-10T09:05:00", "surface_temp_c": 45},
                {"ts": "2026-09-10T09:30:00", "surface_temp_c": 45}],
                "at": _ts(30)})
        ev = r.get_json()["cooling"]
        # 09:05->09:30 缺报截断：新段从 09:30 锚定，累计清零
        self.assertEqual(ev["held_low_temp_minutes"], 0.0)
        self.assertIn("LONG_GAP", [i["code"] for i in ev["interruptions"]])
        self.assertEqual(ev["current_segment"]["start"],
                         "2026-09-10T09:30:00")

    def test_out_of_order_truncates_segment(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0, 45.0, 45.0])  # 至 09:10，10 分钟
        # 乱序补报 09:06 的读数：截断当前区间
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [
                {"ts": "2026-09-10T09:06:00", "surface_temp_c": 44}],
                "at": _ts(15)})
        body = r.get_json()
        self.assertEqual(body["conflicts"], 1)
        self.assertEqual(body["accepted"], 0)
        ev = body["cooling"]
        self.assertIn("OUT_OF_ORDER",
                      [i["code"] for i in ev["interruptions"]])
        # 乱序低温读数锚定新区间，旧累计作废
        self.assertEqual(ev["held_low_temp_minutes"], 0.0)
        self.assertEqual(ev["current_segment"]["start"],
                         "2026-09-10T09:06:00")
        # 完整读数保留（含乱序标记）
        kinds = {x["ts"][11:16]: x["kind"] for x in
                 self.c.get(f"/api/batches/{bid}").get_json()
                 ["items"][0]["cooling"]["readings"]}
        self.assertEqual(kinds["09:06"], "OUT_OF_ORDER")

    def test_same_ts_conflicting_temp_truncates_segment(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0, 45.0])  # 09:00、09:05
        # 09:05 同时刻不同温度（同点重复）：收录为 TS_CONFLICT，区间截断
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [
                {"ts": "2026-09-10T09:05:00", "surface_temp_c": 70}],
                "at": _ts(10)})
        body = r.get_json()
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(body["conflicts"], 1)
        # 同点重复截断：当前连续低温区间作废，完整读数保留冲突标记
        self.assertEqual(body["cooling"]["held_low_temp_minutes"], 0.0)
        self.assertIn("TS_CONFLICT",
                      [i["code"] for i in body["cooling"]["interruptions"]])
        d = self.c.get(f"/api/batches/{bid}").get_json()
        rows = d["items"][0]["cooling"]["readings"]
        kinds = {f"{x['ts'][11:16]}|{x['surface_temp_c']:g}": x["kind"]
                 for x in rows}
        self.assertEqual(kinds["09:05|45"], "OK")
        self.assertEqual(kinds["09:05|70"], "TS_CONFLICT")
        # 同点同温度同样保存并截断（TS_DUPLICATE，不再幂等忽略）：
        # 先在冲突之后补两条低温形成新区间，再重复其中一点
        self._cool_readings(bid, "W-1", [45.0, 45.0], start_min=10)
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [
                {"ts": "2026-09-10T09:10:00", "surface_temp_c": 45}],
                "at": _ts(20)})
        self.assertEqual(r.get_json()["duplicates"], 1)
        self.assertEqual(r.get_json()["conflicts"], 1)
        self.assertIn("TS_DUPLICATE",
                      [i["code"] for i in r.get_json()["cooling"]["interruptions"]])
        # 重复点截断：09:10->09:15 的 5 分钟不能跨重复点拼接，保持清零
        self.assertEqual(r.get_json()["cooling"]["held_low_temp_minutes"], 0.0)

    def test_stale_reading_blocks_release(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # 09:00-09:25 连续 5 点低温，累计 20 分钟达标，但基准时刻 09:45 已陈旧
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [
                {"ts": f"2026-09-10T09:{m:02d}:00", "surface_temp_c": 45}
                for m in (0, 5, 10, 15, 20, 25)],
                "at": "2026-09-10T09:25:00"})
        ev = r.get_json()["cooling"]
        self.assertTrue(ev["releasable"])  # 基准=09:25，读数新鲜
        ev2 = self._cool(bid, "W-1", as_of="2026-09-10T09:45:00")
        self.assertFalse(ev2["releasable"])
        self.assertIn("STALE_READING", {u["code"] for u in ev2["unmet"]})

    # ----------------------------------------------------- 4. 放行
    def test_normal_release_requires_gate_then_done(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # 保持不足：正常放行 409
        self._cool_readings(bid, "W-1", [45.0, 45.0, 45.0])  # 累计 10 分钟
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(10)})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertIn("HOLD_NOT_MET", {u["code"] for u in body["unmet"]})
        self.assertEqual(body["earliest_release_at"], "2026-09-10T09:20:00")
        # 未放行：无审计记录
        d = self.c.get(f"/api/batches/{bid}").get_json()
        self.assertEqual(d["release_order"], [])
        # 补足低温：09:15、09:20 后累计 20 分钟，09:20 放行成功
        self._cool_readings(bid, "W-1", [45.0, 45.0], start_min=15)
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(20)})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["kind"], "NORMAL")
        self.assertEqual(body["workpiece_status"], "DONE")
        self.assertEqual(self.c.get("/api/workpieces/W-1").get_json()["status"],
                         "DONE")
        # 重复放行 409；放行后不再收冷却读数
        self.assertEqual(self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/release",
            json={"at": _ts(25)}).status_code, 409)
        r = self.c.post(
            f"/api/batches/{bid}/workpieces/W-1/cooling-readings",
            json={"readings": [{"ts": _ts(25), "surface_temp_c": 45}]})
        self.assertTrue(r.get_json()["rejected"])

    def test_emergency_release_requires_reason_and_goes_rework(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # 不给理由：400
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(5), "emergency": True})
        self.assertEqual(r.status_code, 400)
        # 给理由：紧急搬运，不看门限，转返工
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(5), "emergency": True,
                              "reason": "下工序插单，线长签字先转返工区"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["kind"], "EMERGENCY")
        self.assertEqual(body["workpiece_status"], "REWORK_PENDING")
        w = self.c.get("/api/workpieces/W-1").get_json()
        self.assertEqual(w["status"], "REWORK_PENDING")
        self.assertIn("EMERGENCY_RELEASE", {f["code"] for f in w["flags"]})
        # 可走返工回队列
        self.assertEqual(self.c.post("/api/workpieces/W-1/rework").status_code,
                         200)

    def test_issue_rejected_when_pack_threshold_missing(self):
        """粉料缺包装门限：签发明确拒绝（409），炉次保持 DRAFT。"""
        payload = {
            "reason": "x", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "PN", "temp_min_c": 160,
                         "temp_max_c": 180, "hold_minutes": 10}],
            "forbidden_pairs": [],
            "orders": [{"workpiece_id": "WN", "order_id": "O",
                        "length_mm": 500, "width_mm": 400, "height_mm": 300,
                        "weight_kg": 10, "powder_batch": "PN"}]}
        r = self.c.post("/api/schedule/trial", json=payload)
        nbid = r.get_json()["new_batches"][0]["batch_id"]
        r = self.c.post(f"/api/batches/{nbid}/issue")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["pack_threshold_problems"][0]["code"],
                         "PACK_LIMIT_MISSING")
        self.assertEqual(
            body["pack_threshold_problems"][0]["missing_fields"],
            ["pack_temp_limit_c", "low_temp_hold_minutes"])
        self.assertEqual(
            self.c.get(f"/api/batches/{nbid}").get_json()["state"], "DRAFT")

    def test_empty_snapshot_never_falls_back_to_master_data(self):
        """签发快照为空（遗留炉次）：即使后来给粉料补门限，也不得放行。"""
        bid = self._setup(powder="PLEGACY")
        # 模拟门限缺失的遗留签发：清空冻结快照
        import sqlite3
        db_path = self.app.config["DATABASE"]
        db = sqlite3.connect(db_path)
        db.execute("UPDATE batch_items SET snap_pack_temp_limit_c=NULL,"
                   " snap_low_temp_hold_minutes=NULL WHERE batch_id=?", (bid,))
        db.commit()
        db.close()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [30.0, 30.0, 30.0, 30.0, 30.0])
        # 后来补改粉料主数据：已签发炉次不得回退采用
        r = self.c.post("/api/schedule/trial", json={
            "reason": "patch powder", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "PLEGACY", "temp_min_c": 160,
                         "temp_max_c": 180, "hold_minutes": 10,
                         "pack_temp_limit_c": 50,
                         "low_temp_hold_minutes": 20}],
            "orders": []})
        self.assertEqual(r.status_code, 201)
        ev = self._cool(bid, "W-1", at=_ts(25))
        self.assertIn("PACK_LIMIT_MISSING",
                      {u["code"] for u in ev["unmet"]})
        self.assertIsNone(ev["earliest_release_at"])
        # 即使表面温度早已够低、保持够久，NORMAL 仍 409
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(25)})
        self.assertEqual(r.status_code, 409)
        codes = {u["code"] for u in r.get_json()["unmet"]}
        self.assertIn("PACK_LIMIT_MISSING", codes)
        # 紧急搬运（人工决定）仍可用
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(25), "emergency": True,
                              "reason": "门限快照缺失，质量主管现场确认返工"})
        self.assertEqual(r.status_code, 200)

    # ----------------------------------------------------- 5. 结案门限
    def test_batch_close_requires_release(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        # COOLING 未放行：结案 409
        r = self.c.post(f"/api/batches/{bid}/close")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["blockers"][0]["status"], "COOLING")
        # 紧急搬运 -> REWORK_PENDING：仍阻塞（未回队列）
        self._cool_readings(bid, "W-1", [45.0])
        self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                    json={"at": _ts(2), "emergency": True, "reason": "急"})
        r = self.c.post(f"/api/batches/{bid}/close")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["blockers"][0]["status"],
                         "REWORK_PENDING")
        # 返工回队列（is_rework=1）后可结案
        self.c.post("/api/workpieces/W-1/rework")
        r = self.c.post(f"/api/batches/{bid}/close")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_batch_close_ok_after_normal_release(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0] * 5)  # 至 09:20，累计 20
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": _ts(20)})
        self.assertEqual(r.status_code, 200)
        r = self.c.post(f"/api/batches/{bid}/close")
        self.assertEqual(r.status_code, 200)
        kinds = {x["kind"] for x in r.get_json()["releases"]}
        self.assertEqual(kinds, {"NORMAL"})

    # ----------------------------------------------------- 6. 档案保留
    def test_detail_and_archive_retain_frozen_thresholds_readings_decisions(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0, 45.0, 60.0])  # 含再次升温
        self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                    json={"at": _ts(12), "emergency": True,
                          "reason": "现场抢线"})
        d = self.c.get(f"/api/batches/{bid}").get_json()
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        for p in (d, arch):
            view = p["items"][0]["cooling"]
            self.assertEqual(view["pack_temp_limit_c"], 50.0)
            self.assertEqual(view["low_temp_hold_minutes"], 20.0)
            # 完整读数
            self.assertEqual(len(view["readings"]), 3)
            # 区间中断（再次升温）
            self.assertIn("REHEAT",
                          [i["code"] for i in view["interruptions"]])
            # 人工决定
            self.assertEqual(view["release"]["kind"], "EMERGENCY")
            self.assertEqual(view["release"]["reason"], "现场抢线")
            self.assertFalse(view["snapshot"]["releasable"])
        # release_order 保留审计
        self.assertEqual(arch["release_order"][0]["kind"], "EMERGENCY")
        self.assertEqual(arch["release_order"][0]["reason"], "现场抢线")
        # 工件履历含冷却记录
        w = self.c.get("/api/workpieces/W-1").get_json()
        rec = w["cooling_records"][0]
        self.assertEqual(len(rec["readings"]), 3)
        self.assertEqual(rec["release"]["kind"], "EMERGENCY")

    def test_batch_cooling_summary(self):
        bid = self._setup(("W-1", "W-2"))
        self._cure_and_unload(bid, "W-1")
        self._cure_and_unload(bid, "W-2")
        self._cool_readings(bid, "W-1", [45.0] * 5)  # 达标
        self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                    json={"at": _ts(20)})
        r = self.c.get(f"/api/batches/{bid}/cooling",
                       query_string={"as_of": _ts(20)})
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["cooling"]
        self.assertEqual(s["released_count"], 1)
        self.assertEqual(s["cooling_count"], 1)
        self.assertEqual(s["waiting_count"], 1)
        self.assertFalse(s["all_released"])

    def test_card_renders_cooling_section(self):
        bid = self._setup()
        self._cure_and_unload(bid, "W-1")
        self._cool_readings(bid, "W-1", [45.0, 60.0])  # 含再次升温
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("冷却放行", card)
        self.assertIn("包装耐温门限", card)
        self.assertIn("冷却中", card)
        self.assertIn("REHEAT", card)

    def test_release_requires_unloaded_piece(self):
        bid = self._setup()
        # 仍在炉（未离炉）：正常与紧急放行均 409
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": UNLOAD_AT})
        self.assertEqual(r.status_code, 409)
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/release",
                        json={"at": UNLOAD_AT, "emergency": True,
                              "reason": "尚在炉内"})
        self.assertEqual(r.status_code, 409)


class CoolingPureTest(unittest.TestCase):
    """冷却区间引擎纯函数测试（乱序/长间隔/再升温按到达顺序截断）。"""

    def _pts(self, *spec):
        from datetime import datetime
        out = []
        for m, t, k in spec:
            out.append((datetime.fromisoformat(f"2026-09-10T09:{m:02d}:00"),
                        t, k))
        return out

    def test_build_segments_breaks(self):
        pts = self._pts((0, 45, "OK"), (5, 45, "OK"), (20, 45, "OK"),
                        (25, 60, "OK"), (30, 45, "OK"))
        out = cooling_mod.build_segments(pts, 50.0, gap_threshold_minutes=10)
        codes = [i["code"] for i in out["interruptions"]]
        self.assertIn("LONG_GAP", codes)
        self.assertIn("REHEAT", codes)
        # 新段锚在 09:30
        self.assertEqual(out["current"]["start"].isoformat(),
                         "2026-09-10T09:30:00")
        self.assertEqual(out["current"]["held_minutes"], 0.0)

    def test_admit_classification(self):
        from datetime import datetime
        ts = lambda m: datetime.fromisoformat(f"2026-09-10T09:{m:02d}:00")
        base = [(ts(0), 45.0, "OK"), (ts(5), 44.0, "OK")]
        kind, _ = cooling_mod.admit(base, ts(10), 45.0)
        self.assertEqual(kind, cooling_mod.KIND_OK)
        kind, _ = cooling_mod.admit(base, ts(3), 45.0)
        self.assertEqual(kind, cooling_mod.KIND_OUT_OF_ORDER)
        kind, _ = cooling_mod.admit(base, ts(5), 44.0)
        self.assertEqual(kind, cooling_mod.KIND_TS_DUPLICATE)  # 同点重复（同温度）
        kind, _ = cooling_mod.admit(base, ts(5), 80.0)
        self.assertEqual(kind, cooling_mod.KIND_TS_CONFLICT)

    def test_duplicate_breaks_segment_in_engine(self):
        from datetime import datetime
        ts = lambda m: datetime.fromisoformat(f"2026-09-10T09:{m:02d}:00")
        # 已累计 15 分钟后，重复提交 09:00 同温读数：区间截断、不拼前后
        pts = self._pts((0, 45, "OK"), (5, 45, "OK"), (10, 45, "OK"),
                        (15, 45, "OK"), (0, 45, "TS_DUPLICATE"))
        out = cooling_mod.build_segments(pts, 50.0, gap_threshold_minutes=10)
        self.assertIsNone(out["current"])
        self.assertEqual(out["segments"][-1]["held_minutes"], 15.0)
        self.assertEqual(out["interruptions"][-1]["code"], "TS_DUPLICATE")
        # 重复点之后新到读数（09:20）重新锚段，不与中断前拼接
        pts2 = pts + self._pts((20, 45, "OK"), (25, 45, "OK"))
        out2 = cooling_mod.build_segments(pts2, 50.0, gap_threshold_minutes=10)
        self.assertEqual(out2["current"]["start"], ts(20))
        self.assertEqual(out2["current"]["held_minutes"], 5.0)


if __name__ == "__main__":
    unittest.main()
