"""探头校准证书版本化测试：

1. 校准版本录入：少于两点 / 时间倒置 / 点列不递增拒绝；版本只增不改，
   历史版本可查询；
2. 绑定探头时指定校准版本（不存在或属于其他探头的版本拒绝）；
3. 签发检查：按计划入炉时刻核对有效期与粉料温区覆盖，
   缺失 / 未生效 / 过期 / 覆盖不足时列出工件和探头并阻止签发；
4. 测温按冻结点列线性插值；区间外读数不计入保温累计并产生
   CALIBRATION_RANGE 告警；
5. 新证书只供未签发炉次使用（已签发炉次用冻结快照）；
6. 批次查询 / 进度响应 / JSON 档案 / 随炉卡保留证书版本、插值区间
   及到期状态。

运行：python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

from ovenline import create_app
from ovenline.db import get_db

OVEN = {
    "id": "OVEN-1", "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02, "ambient_c": 25,
    "turnaround_minutes": 15, "hanger_slots": 12, "hanger_spacing_mm": 500,
    "hanger_max_load_kg": 80,
}
LOAD_AT = "2026-09-10T08:00:00"
# 覆盖计划入炉时刻的有效期
CAL_AT = "2026-09-01T00:00:00"
VALID_UNTIL = "2026-12-31T00:00:00"


def _order(wid, powder="P1"):
    return {"workpiece_id": wid, "order_id": "PO-T", "length_mm": 500,
            "width_mm": 400, "height_mm": 300, "weight_kg": 10,
            "powder_batch": powder, "compat_group": None,
            "due_at": "2026-09-10T18:00:00"}


def _ts(i):
    """08:00 起每 5 分钟一个采样时刻。"""
    return f"2026-09-10T{8 + (i * 5) // 60:02d}:{(i * 5) % 60:02d}:00"


def _readings(wid, pid, temps):
    return [{"workpiece_id": wid, "probe_id": pid, "ts": _ts(i),
             "metal_temp_c": t} for i, t in enumerate(temps)]


def _points(pairs):
    return [{"indicated_c": x, "reference_c": y} for x, y in pairs]


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------- 流程工具
    def _trial(self, orders, powders=None):
        r = self.c.post("/api/schedule/trial", json={
            "reason": "t", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": powders if powders is not None
            else [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                   "hold_minutes": 15}],
            "forbidden_pairs": [], "orders": orders})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["new_batches"][0]["batch_id"]

    def _issue_load(self, bid):
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _register(self, wid, probes):
        r = self.c.post(f"/api/workpieces/{wid}/probes", json={"probes": probes})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["probes"]

    def _add_cal(self, wid, pid, cert="CERT-001", calibrated_at=CAL_AT,
                 valid_until=VALID_UNTIL, points=None, expect=201):
        r = self.c.post(f"/api/workpieces/{wid}/probes/{pid}/calibrations", json={
            "certificate_no": cert, "calibrated_at": calibrated_at,
            "valid_until": valid_until,
            "points": _points(points if points is not None
                              else [(100, 100), (200, 200)])})
        self.assertEqual(r.status_code, expect, r.get_data(as_text=True))
        return r.get_json().get("calibration") if expect == 201 else r.get_json()

    def _bind(self, wid, pid, cal_id, expect=201):
        r = self.c.post(f"/api/workpieces/{wid}/probes",
                        json={"probes": [{"probe_id": pid, "offset_c": 0,
                                          "calibration_id": cal_id}]})
        self.assertEqual(r.status_code, expect, r.get_data(as_text=True))
        return r.get_json()

    def _post_readings(self, bid, readings):
        r = self.c.post(f"/api/batches/{bid}/readings",
                        json={"readings": readings})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _item(self, bid, wid):
        items = self.c.get(f"/api/batches/{bid}").get_json()["items"]
        return next(i for i in items if i["workpiece_id"] == wid)

    # ---------------------------------------------------------- 1. 录入与历史版本
    def test_add_calibration_validation(self):
        self._trial([_order("W-1")])
        # 工件不存在 404 / 探头未登记 404
        r = self.c.post("/api/workpieces/W-GHOST/probes/T1/calibrations", json={})
        self.assertEqual(r.status_code, 404)
        r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={})
        self.assertEqual(r.status_code, 404)

        self._register("W-1", [{"probe_id": "T1", "offset_c": 0}])
        # 少于两点
        r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={
            "certificate_no": "C1", "calibrated_at": CAL_AT,
            "valid_until": VALID_UNTIL, "points": _points([(100, 100)])})
        self.assertEqual(r.status_code, 400)
        self.assertIn("两个点", r.get_json()["error"])
        # 时间倒置（到期不晚于校准时刻）
        r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={
            "certificate_no": "C1", "calibrated_at": "2026-09-10T00:00:00",
            "valid_until": "2026-09-01T00:00:00",
            "points": _points([(100, 100), (200, 200)])})
        self.assertEqual(r.status_code, 400)
        self.assertIn("倒置", r.get_json()["error"])
        # 点列不递增（回退与相等都拒绝）
        for pairs in ([(100, 100), (90, 90)], [(100, 100), (100, 101)]):
            r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={
                "certificate_no": "C1", "calibrated_at": CAL_AT,
                "valid_until": VALID_UNTIL, "points": _points(pairs)})
            self.assertEqual(r.status_code, 400, pairs)
            self.assertIn("不递增", r.get_json()["error"])
        # 缺证书号 / 点列非数值
        r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={
            "certificate_no": " ", "calibrated_at": CAL_AT,
            "valid_until": VALID_UNTIL, "points": _points([(1, 1), (2, 2)])})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/workpieces/W-1/probes/T1/calibrations", json={
            "certificate_no": "C1", "calibrated_at": CAL_AT,
            "valid_until": VALID_UNTIL,
            "points": [{"indicated_c": "hot", "reference_c": 1},
                       {"indicated_c": 2, "reference_c": 2}]})
        self.assertEqual(r.status_code, 400)

    def test_calibration_versions_append_only_and_history(self):
        self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0}])
        v1 = self._add_cal("W-1", "T1", cert="CERT-V1",
                           points=[(100, 100), (200, 200)])
        v2 = self._add_cal("W-1", "T1", cert="CERT-V2",
                           points=[(100, 101), (200, 203), (250, 254)])
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)
        self.assertNotEqual(v1["calibration_id"], v2["calibration_id"])

        # 历史版本查询：按版本升序，含点列与插值区间
        r = self.c.get("/api/workpieces/W-1/probes/T1/calibrations")
        self.assertEqual(r.status_code, 200)
        cals = r.get_json()["calibrations"]
        self.assertEqual([c["version"] for c in cals], [1, 2])
        self.assertEqual([c["certificate_no"] for c in cals],
                         ["CERT-V1", "CERT-V2"])
        self.assertEqual(cals[0]["range_min_c"], 100.0)
        self.assertEqual(cals[0]["range_max_c"], 200.0)
        self.assertEqual(cals[1]["range_max_c"], 250.0)
        self.assertEqual(len(cals[1]["points"]), 3)
        # 不可覆盖：不支持修改/删除（405）
        r = self.c.put("/api/workpieces/W-1/probes/T1/calibrations", json={})
        self.assertEqual(r.status_code, 405)
        r = self.c.delete("/api/workpieces/W-1/probes/T1/calibrations")
        self.assertEqual(r.status_code, 405)
        # 再次查询：版本未被改写
        cals2 = self.c.get(
            "/api/workpieces/W-1/probes/T1/calibrations").get_json()["calibrations"]
        self.assertEqual(cals2, cals)

    # ---------------------------------------------------------- 2. 绑定版本
    def test_bind_calibration_on_register(self):
        self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        cal = self._add_cal("W-1", "T1")
        # 绑定不存在的版本 / 属于其他探头的版本 / 非整数 id
        self._bind("W-1", "T1", 999, expect=400)
        self._bind("W-1", "T2", cal["calibration_id"], expect=400)
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probes": [{"probe_id": "T1",
                                          "calibration_id": "abc"}]})
        self.assertEqual(r.status_code, 400)
        # 正常绑定后探头列表带证书摘要
        got = self._bind("W-1", "T1", cal["calibration_id"])["probes"]
        t1 = next(p for p in got if p["probe_id"] == "T1")
        self.assertEqual(t1["calibration_id"], cal["calibration_id"])
        self.assertEqual(t1["calibration"]["certificate_no"], "CERT-001")
        # 不带 calibration_id 的重复登记保留既有绑定
        got = self._register("W-1", [{"probe_id": "T1", "offset_c": 0.5}])
        t1 = next(p for p in got if p["probe_id"] == "T1")
        self.assertEqual(t1["calibration_id"], cal["calibration_id"])
        # 显式 null 解除绑定
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probes": [{"probe_id": "T1",
                                          "calibration_id": None}]})
        self.assertEqual(r.status_code, 201)
        t1 = next(p for p in r.get_json()["probes"] if p["probe_id"] == "T1")
        self.assertIsNone(t1["calibration_id"])
        self.assertNotIn("calibration", t1)

    # ---------------------------------------------------------- 3. 签发检查
    def _prepared_batch(self, points=None, calibrated_at=CAL_AT,
                        valid_until=VALID_UNTIL, powder=None):
        """造一个 DRAFT 炉次：W-1 登记 T1 并绑定指定参数的证书版本。"""
        powders = [powder] if powder else None
        bid = self._trial([_order("W-1")], powders=powders)
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0}])
        cal = self._add_cal("W-1", "T1", calibrated_at=calibrated_at,
                            valid_until=valid_until, points=points)
        self._bind("W-1", "T1", cal["calibration_id"])
        return bid

    def test_issue_blocked_when_expired(self):
        bid = self._prepared_batch(valid_until="2026-09-09T00:00:00")
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["state"], "DRAFT")
        problems = body["calibration_problems"]
        self.assertEqual(len(problems), 1)
        p = problems[0]
        self.assertEqual(p["code"], "CALIBRATION_EXPIRED")
        self.assertEqual(p["workpiece_id"], "W-1")
        self.assertEqual(p["probe_id"], "T1")
        self.assertEqual(p["certificate_no"], "CERT-001")
        self.assertEqual(p["planned_load_at"], LOAD_AT)
        # 炉次保持 DRAFT，可换证后重新签发
        cal = self._add_cal("W-1", "T1", cert="CERT-002")
        self._bind("W-1", "T1", cal["calibration_id"])
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_issue_blocked_when_not_yet_valid(self):
        bid = self._prepared_batch(calibrated_at="2026-09-11T00:00:00",
                                   valid_until="2026-12-31T00:00:00")
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 409)
        p = r.get_json()["calibration_problems"][0]
        self.assertEqual(p["code"], "CALIBRATION_NOT_YET_VALID")
        self.assertEqual(p["calibrated_at"], "2026-09-11T00:00:00")

    def test_issue_blocked_when_coverage_insufficient(self):
        # 粉料温区 160–180℃，点列示值 100–170℃ 未覆盖上限
        bid = self._prepared_batch(points=[(100, 100), (170, 170)])
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 409)
        p = r.get_json()["calibration_problems"][0]
        self.assertEqual(p["code"], "CALIBRATION_COVERAGE")
        self.assertEqual(p["range_max_c"], 170.0)
        self.assertEqual(p["window_max_c"], 180.0)

    def test_issue_blocked_when_calibration_missing(self):
        bid = self._prepared_batch()
        # 绑定关系指向不存在的版本（脏数据防御）
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE probes SET calibration_id=999"
                       " WHERE workpiece_id='W-1' AND probe_id='T1'")
            db.commit()
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 409)
        p = r.get_json()["calibration_problems"][0]
        self.assertEqual(p["code"], "CALIBRATION_MISSING")
        self.assertEqual(p["calibration_id"], 999)

    def test_issue_check_lists_each_problem_probe(self):
        # 同炉两工件：W-1 证书过期、W-2 覆盖不足，一次响应全部列出
        bid = self._trial([_order("W-1"), _order("W-2")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0}])
        self._register("W-2", [{"probe_id": "T9", "offset_c": 0}])
        c1 = self._add_cal("W-1", "T1", valid_until="2026-09-09T00:00:00")
        self._bind("W-1", "T1", c1["calibration_id"])
        c2 = self._add_cal("W-2", "T9", points=[(100, 100), (170, 170)])
        self._bind("W-2", "T9", c2["calibration_id"])
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 409)
        got = {(p["workpiece_id"], p["probe_id"], p["code"])
               for p in r.get_json()["calibration_problems"]}
        self.assertEqual(got, {("W-1", "T1", "CALIBRATION_EXPIRED"),
                               ("W-2", "T9", "CALIBRATION_COVERAGE")})

    def test_unbound_probe_still_issues_with_fixed_offset(self):
        # 未绑定证书版本的探头保持固定偏移模式，不受签发检查影响
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 1.0}])
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---------------------------------------------------------- 4. 插值与区间外
    def test_interpolation_by_frozen_points(self):
        # 点列 (100→100, 200→204)：raw 150→152.0，raw 170→172.8
        bid = self._prepared_batch(points=[(100, 100), (200, 204)])
        self._issue_load(bid)
        self._post_readings(bid, _readings("W-1", "T1", [150, 170, 175, 170]))
        item = self._item(bid, "W-1")
        series = [p["temp_c"] for p in item["cure"]["judgment_series"]]
        self.assertEqual(len(series), 4)
        self.assertAlmostEqual(series[0], 152.0)
        self.assertAlmostEqual(series[1], 172.8)
        self.assertAlmostEqual(series[2], 178.0)
        self.assertAlmostEqual(series[3], 172.8)
        # 探头条目带证书视图（版本/区间/到期状态）
        cal = item["cure"]["probes"][0]["calibration"]
        self.assertEqual(cal["certificate_no"], "CERT-001")
        self.assertEqual(cal["version"], 1)
        self.assertEqual(cal["range_min_c"], 100.0)
        self.assertEqual(cal["range_max_c"], 200.0)
        self.assertFalse(cal["expired"])

    def test_out_of_range_excluded_and_alerted(self):
        # 恒等点列 [100,200]；raw 210 超区间：剔除 + CALIBRATION_RANGE，
        # 不参与保温累计与超温判定；原始读数仍入库
        bid = self._prepared_batch(points=[(100, 100), (200, 200)])
        self._issue_load(bid)
        body = self._post_readings(
            bid, _readings("W-1", "T1", [160, 170, 210, 175, 170, 172]))
        self.assertEqual(body["accepted"], 6)  # 超区间读数仍接收（保留原始值）
        item = self._item(bid, "W-1")
        c = item["cure"]
        self.assertEqual(c["raw_reading_count"], 6)
        # 判定序列不含 08:10 的超区间点
        self.assertEqual([p["ts"] for p in c["judgment_series"]],
                         [_ts(0), _ts(1), _ts(3), _ts(4), _ts(5)])
        self.assertFalse(c["over_temp"])  # 210 未进入判定
        self.assertEqual(c["in_window_minutes"], 25.0)  # 08:00–08:25 五段
        # CALIBRATION_RANGE 告警明细
        self.assertEqual(len(c["calibration_range"]), 1)
        oor = c["calibration_range"][0]
        self.assertEqual(oor["probe_id"], "T1")
        self.assertEqual(oor["ts"], _ts(2))
        self.assertEqual(oor["raw_c"], 210.0)
        self.assertEqual(oor["range_min_c"], 100.0)
        self.assertEqual(oor["range_max_c"], 200.0)
        probe = c["probes"][0]
        self.assertEqual(probe["out_of_range_count"], 1)
        self.assertEqual(probe["reading_count"], 5)  # 区间内读数
        # 进度响应告警
        prog = self.c.get(f"/api/batches/{bid}/progress").get_json()["progress"]
        alerts = prog["items"][0]["alerts"]
        cal_alert = next(a for a in alerts if a["code"] == "CALIBRATION_RANGE")
        self.assertEqual(len(cal_alert["readings"]), 1)
        # 出炉判定合格（超区间读数不产生不合格标记）
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "OK", res["flags"])
        self.assertNotIn("OVER_TEMP", res["flags"])

    def test_all_readings_out_of_range_no_hold_credit(self):
        # 全部读数超区间（点列覆盖粉料温区，读数高于点列上限）：
        # 判定序列为空，无保温累计，告警仍在
        bid = self._prepared_batch(points=[(100, 100), (200, 200)])
        self._issue_load(bid)
        self._post_readings(bid, _readings("W-1", "T1", [210, 220, 230, 240]))
        item = self._item(bid, "W-1")
        c = item["cure"]
        self.assertEqual(c["judgment_series"], [])
        self.assertEqual(c["in_window_minutes"], 0.0)
        self.assertEqual(len(c["calibration_range"]), 4)
        prog = self.c.get(f"/api/batches/{bid}/progress").get_json()["progress"]
        codes = [a["code"] for a in prog["items"][0]["alerts"]]
        self.assertIn("CALIBRATION_RANGE", codes)
        self.assertIn("NO_READING", codes)

    # ---------------------------------------------------------- 5. 新证书只供未签发炉次
    def test_new_certificate_only_affects_unissued_batches(self):
        bid = self._prepared_batch(points=[(100, 100), (200, 200)])
        self._issue_load(bid)
        # 签发后录入新证书 v2（+10 偏移）并改绑定
        v2 = self._add_cal("W-1", "T1", cert="CERT-V2",
                           points=[(100, 110), (200, 210)])
        self._bind("W-1", "T1", v2["calibration_id"])
        # 在炉炉次仍按 v1 恒等插值（冻结快照）
        self._post_readings(bid, _readings("W-1", "T1", [160, 170, 175, 170]))
        item = self._item(bid, "W-1")
        self.assertEqual(item["cure"]["judgment_series"][1]["temp_c"], 170.0)
        cal = item["cure"]["probes"][0]["calibration"]
        self.assertEqual(cal["certificate_no"], "CERT-001")
        self.assertEqual(cal["version"], 1)
        # 强制出炉 → 返工 → 新炉次冻结 v2（170 → 180）
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/unload",
                        json={"force": True, "reason": "换证重测",
                              "at": "2026-09-10T08:30:00"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.c.post("/api/workpieces/W-1/rework")
        self.assertEqual(r.status_code, 200)
        bid2 = self._trial([_order("W-1")])
        self._issue_load(bid2)
        self._post_readings(bid2, _readings("W-1", "T1", [160, 170, 175, 170]))
        item2 = self._item(bid2, "W-1")
        self.assertEqual(item2["cure"]["judgment_series"][1]["temp_c"], 180.0)
        cal2 = item2["cure"]["probes"][0]["calibration"]
        self.assertEqual(cal2["certificate_no"], "CERT-V2")
        self.assertEqual(cal2["version"], 2)

    # ---------------------------------------------------------- 6. 查询/档案/随炉卡
    def test_detail_progress_archive_card_expose_calibration(self):
        bid = self._prepared_batch(points=[(100, 100), (200, 200)])
        self._issue_load(bid)
        self._post_readings(bid, _readings("W-1", "T1", [160, 170, 175, 170]))

        # 批次查询：探头证书版本 / 插值区间 / 到期状态
        cal = self._item(bid, "W-1")["cure"]["probes"][0]["calibration"]
        self.assertEqual(cal["certificate_no"], "CERT-001")
        self.assertEqual((cal["range_min_c"], cal["range_max_c"]), (100.0, 200.0))
        self.assertEqual(cal["valid_until"], VALID_UNTIL)
        self.assertFalse(cal["expired"])

        # 进度响应
        prog = self.c.get(f"/api/batches/{bid}/progress").get_json()["progress"]
        pcals = prog["items"][0]["calibrations"]
        self.assertEqual(len(pcals), 1)
        self.assertEqual(pcals[0]["certificate_no"], "CERT-001")
        self.assertEqual(pcals[0]["probe_id"], "T1")
        self.assertEqual(pcals[0]["range_max_c"], 200.0)
        self.assertFalse(pcals[0]["expired"])

        # JSON 档案与详情一致
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        a_cal = arch["items"][0]["cure"]["probes"][0]["calibration"]
        self.assertEqual(a_cal, cal)
        a_prog_cal = arch["progress"]["items"][0]["calibrations"]
        self.assertEqual(a_prog_cal[0]["certificate_no"], "CERT-001")

        # 随炉卡：证书号、插值区间、到期状态
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("CERT-001", card)
        self.assertIn("插值区间", card)
        self.assertIn("100–200", card)
        self.assertIn("到期状态", card)
        self.assertIn(VALID_UNTIL, card)
        self.assertIn("计划入炉时有效", card)

    def test_card_marks_expired_certificate(self):
        # 签发后计划入炉时刻晚于证书到期（追溯口径）：随炉卡标注已过期
        bid = self._prepared_batch(points=[(100, 100), (200, 200)])
        self._issue_load(bid)
        # 直接把冻结快照的到期时刻改到计划入炉之前（模拟历史数据追溯）
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE batch_item_probes SET valid_until='2026-09-01T00:00:00'"
                       " WHERE batch_id=? AND workpiece_id='W-1'", (bid,))
            db.commit()
        cal = self._item(bid, "W-1")["cure"]["probes"][0]["calibration"]
        self.assertTrue(cal["expired"])
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("计划入炉时已过期", card)


if __name__ == "__main__":
    unittest.main()
