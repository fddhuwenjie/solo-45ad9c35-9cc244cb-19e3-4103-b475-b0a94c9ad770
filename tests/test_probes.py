"""多探头测温功能测试：

1. 探头登记与签发冻结（签发后改主数据不影响在炉炉次）；
2. 测温回传携带 probe_id：未绑定/已停用/入炉前读数拒收，按
   (炉次, 工件, 探头, 时刻) 幂等去重；
3. 判定序列取每个采样时刻有效探头的最低校正温度，据此累计固化窗口；
4. 连续卡值 / 探头温差 / 缺报分别产生 STUCK_PROBE / PROBE_DIVERGENCE / PROBE_GAP；
5. 出炉前停用故障探头：只重算该工件并记录结果变化；
6. 有效探头少于设定数量不得判定合格（INSUFFICIENT_PROBES）；
7. 炉次详情 / JSON 档案 / 随炉卡列出探头状态、校准值、异常区间与判定序列。

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


def _bind_cal(client, wid, pid, offset):
    """录入并绑定复现固定偏移的校准版本（点列斜率 1：校正值 = 原始值+offset）。

    签发门禁要求每个登记探头绑定有效证书版本；点列 [(0,off),(500,500+off)]
    覆盖测试温区与读数范围，插值结果与旧固定偏移一致，既有数值断言不变。
    """
    r = client.post(f"/api/workpieces/{wid}/probes/{pid}/calibrations", json={
        "certificate_no": f"CERT-{wid}-{pid}",
        "calibrated_at": "2026-09-01T00:00:00",
        "valid_until": "2026-12-31T00:00:00",
        "points": [{"indicated_c": 0, "reference_c": offset},
                   {"indicated_c": 500, "reference_c": 500 + offset}]})
    assert r.status_code == 201, r.get_data(as_text=True)
    cal_id = r.get_json()["calibration"]["calibration_id"]
    r = client.post(f"/api/workpieces/{wid}/probes",
                    json={"probes": [{"probe_id": pid, "offset_c": offset,
                                      "calibration_id": cal_id}]})
    assert r.status_code == 201, r.get_data(as_text=True)


class MultiProbeTest(unittest.TestCase):
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
        # 签发门禁要求登记探头绑定证书版本：为每个探头补录并绑定
        # 复现其固定偏移的校准版本（不改变校正数值）
        for p in probes:
            _bind_cal(self.c, wid, p["probe_id"], float(p.get("offset_c", 0)))
        return r.get_json()["probes"]

    def _post_readings(self, bid, readings):
        r = self.c.post(f"/api/batches/{bid}/readings",
                        json={"readings": readings})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _item(self, bid, wid):
        items = self.c.get(f"/api/batches/{bid}").get_json()["items"]
        return next(i for i in items if i["workpiece_id"] == wid)

    # ---------------------------------------------------------- 1. 登记与冻结
    def test_register_probes_and_issue_freezes_config(self):
        bid = self._trial([_order("W-1")])
        probes = self._register("W-1", [{"probe_id": "T1", "offset_c": 1.0},
                                        {"probe_id": "T2", "offset_c": -0.5}])
        self.assertEqual([p["probe_id"] for p in probes], ["T1", "T2"])

        self._issue_load(bid)
        # 签发后修改主数据偏移（T1: 1.0 -> 5.0），在炉炉次不得受影响
        self._register("W-1", [{"probe_id": "T1", "offset_c": 5.0}])

        self._post_readings(bid, _readings("W-1", "T1", [25, 169, 170, 171]))
        item = self._item(bid, "W-1")
        # 冻结偏移 1.0：169 + 1.0 = 170；若误用新偏移 5.0 则为 174
        series = {p["ts"]: p["temp_c"] for p in item["cure"]["judgment_series"]}
        self.assertEqual(series[_ts(1)], 170.0)
        offsets = {p["probe_id"]: p["offset_c"] for p in item["cure"]["probes"]}
        self.assertEqual(offsets, {"T1": 1.0, "T2": -0.5})

    def test_register_probes_validation(self):
        bid = self._trial([_order("W-1")])
        del bid
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probes": [{"probe_id": " ", "offset_c": 0}]})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probes": [{"probe_id": "T1"},
                                         {"probe_id": "T1"}]})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probes": [{"probe_id": "T1",
                                          "offset_c": "hot"}]})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/workpieces/W-GHOST/probes",
                        json={"probes": [{"probe_id": "T1"}]})
        self.assertEqual(r.status_code, 404)
        # 单条形式（非列表）也可登记
        r = self.c.post("/api/workpieces/W-1/probes",
                        json={"probe_id": "T9", "offset_c": 0.5})
        self.assertEqual(r.status_code, 201)
        got = self.c.get("/api/workpieces/W-1/probes").get_json()["probes"]
        self.assertEqual([p["probe_id"] for p in got], ["T9"])

    # ---------------------------------------------------------- 2. 回传校验与幂等
    def test_readings_probe_binding_and_dedup(self):
        bid = self._trial([_order("W-1"), _order("W-2")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0}])
        self._issue_load(bid)

        body = self._post_readings(bid, [
            # 正常：绑定探头
            {"workpiece_id": "W-1", "probe_id": "T1", "ts": _ts(1),
             "metal_temp_c": 165},
            # 未绑定探头
            {"workpiece_id": "W-1", "probe_id": "T-X", "ts": _ts(1),
             "metal_temp_c": 165},
            # 已绑定探头但缺 probe_id
            {"workpiece_id": "W-1", "ts": _ts(1), "metal_temp_c": 165},
            # 未登记探头的工件携带 probe_id
            {"workpiece_id": "W-2", "probe_id": "T1", "ts": _ts(1),
             "metal_temp_c": 165},
            # 入炉前
            {"workpiece_id": "W-1", "probe_id": "T1",
             "ts": "2026-09-10T07:30:00", "metal_temp_c": 165},
            # 未登记探头的工件：隐式通道正常接收
            {"workpiece_id": "W-2", "ts": _ts(1), "metal_temp_c": 165},
        ])
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(len(body["rejected"]), 4)
        reasons = [r["reason"] for r in body["rejected"]]
        self.assertTrue(any("未绑定" in x for x in reasons))
        self.assertTrue(any("probe_id" in x for x in reasons))
        self.assertTrue(any("入炉" in x for x in reasons))

        # 幂等：原样重传 -> 全部计为重复，不再入库
        again = self._post_readings(bid, [
            {"workpiece_id": "W-1", "probe_id": "T1", "ts": _ts(1),
             "metal_temp_c": 165},
            {"workpiece_id": "W-2", "ts": _ts(1), "metal_temp_c": 165},
        ])
        self.assertEqual(again["accepted"], 0)
        self.assertEqual(again["duplicates"], 2)
        # 同时刻不同温度仍是重复（保留先到的值）
        dup = self._post_readings(bid, [
            {"workpiece_id": "W-1", "probe_id": "T1", "ts": _ts(1),
             "metal_temp_c": 999}])
        self.assertEqual(dup["duplicates"], 1)
        item = self._item(bid, "W-1")
        self.assertEqual(item["cure"]["raw_reading_count"], 1)
        self.assertEqual(item["cure"]["judgment_series"][0]["temp_c"], 165.0)

    def test_disabled_probe_readings_rejected(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                        json={"reason": "现场发现松脱"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = self._post_readings(bid, _readings("W-1", "T2", [160]))
        self.assertEqual(body["accepted"], 0)
        self.assertIn("已停用", body["rejected"][0]["reason"])

    # ---------------------------------------------------------- 3. 判定序列
    def test_judgment_series_uses_min_corrected_temp(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 2.0}])
        self._issue_load(bid)
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [20, 160, 165, 166, 165, 164]))
        item = self._item(bid, "W-1")
        c = item["cure"]
        # T2 校正后 = 原始值 + 2.0，与 T1 比较后逐时刻取低
        self.assertEqual([p["temp_c"] for p in c["judgment_series"]],
                         [22.0, 162.0, 167.0, 168.0, 167.0, 166.0])
        self.assertEqual(c["raw_reading_count"], 12)
        self.assertEqual(c["reading_count"], 6)  # 判定序列点数
        self.assertEqual(c["in_window_minutes"], 20.0)  # 08:05-08:25 四段
        self.assertEqual(c["valid_probe_count"], 2)
        # 各探头原始统计保留
        stats = {p["probe_id"]: p for p in c["probes"]}
        self.assertEqual(stats["T2"]["reading_count"], 6)
        self.assertEqual(stats["T2"]["min_c"], 22.0)  # 校正后最低

    # ---------------------------------------------------------- 4. 异常标记
    def test_stuck_probe_flag(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [170, 170, 170, 170, 170, 170]))
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "NOT_OK")
        self.assertIn("STUCK_PROBE", res["flags"])
        self.assertNotIn("UNDER_TIME", res["flags"])  # 判定序列本身合格
        # 详情中列出卡值区间
        item = self._item(bid, "W-1")
        t2 = next(p for p in item["cure"]["probes"] if p["probe_id"] == "T2")
        self.assertEqual(len(t2["anomalies"]), 1)
        self.assertEqual(t2["anomalies"][0]["count"], 6)
        self.assertEqual(t2["anomalies"][0]["value_c"], 170.0)

    def test_probe_divergence_flag(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        # 同一时刻两探头相差 > 5℃（默认阈值）
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [31, 171, 176.5, 178, 177, 176]))
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "NOT_OK")
        self.assertIn("PROBE_DIVERGENCE", res["flags"])
        item = self._item(bid, "W-1")
        dv = item["cure"]["divergences"]
        self.assertTrue(any(d["spread_c"] > 5.0 for d in dv))
        self.assertEqual(dv[0]["ts"], _ts(0))

    def test_probe_gap_flag_multiprobe(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        # 两探头同时在 08:05-08:40 缺报（间隔 35 分钟 > 阈值 10）
        readings = [
            {"workpiece_id": "W-1", "probe_id": p, "ts": ts, "metal_temp_c": t}
            for p in ("T1", "T2")
            for ts, t in [(_ts(0), 25), (_ts(1), 165),
                          ("2026-09-10T08:40:00", 170),
                          ("2026-09-10T08:45:00", 171),
                          ("2026-09-10T08:50:00", 170),
                          ("2026-09-10T08:55:00", 172)]]
        self._post_readings(bid, readings)
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T09:00:00"})
        res = r.get_json()["results"][0]
        self.assertIn("PROBE_GAP", res["flags"])
        gaps = res["cure"]["probe_gaps"]
        # 逐探头检测：T1/T2 各自产生一段 08:05-08:40 缺报区间
        self.assertEqual({g["probe_id"] for g in gaps}, {"T1", "T2"})
        self.assertEqual(gaps[0]["minutes"], 35.0)
        self.assertEqual(gaps[0]["from"], _ts(1))
        self.assertEqual(gaps[0]["to"], "2026-09-10T08:40:00")

    def test_single_probe_dropout_gap_and_validity(self):
        """T2 仅 08:00 上报一次后掉线：生成 PROBE_GAP，且不再计为有效探头。"""
        app = create_app({"DATABASE": os.path.join(self.tmp.name, "t4.sqlite"),
                          "TESTING": True, "MIN_VALID_PROBES": 2})
        c = app.test_client()
        r = c.post("/api/schedule/trial", json={
            "reason": "t", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                         "hold_minutes": 15}],
            "forbidden_pairs": [], "orders": [_order("W-1")]})
        bid = r.get_json()["new_batches"][0]["batch_id"]
        c.post("/api/workpieces/W-1/probes",
               json={"probes": [{"probe_id": "T1", "offset_c": 0},
                                {"probe_id": "T2", "offset_c": 0}]})
        _bind_cal(c, "W-1", "T1", 0.0)
        _bind_cal(c, "W-1", "T2", 0.0)
        c.post(f"/api/batches/{bid}/issue")
        c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        # T1 持续上报至 08:25；T2 仅在 08:00 上报一次
        c.post(f"/api/batches/{bid}/readings", json={"readings":
               _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
               + _readings("W-1", "T2", [25])})

        # 出炉前详情：T2 尾随缺报 25 分钟，有效探头只剩 T1
        item = c.get(f"/api/batches/{bid}").get_json()["items"][0]
        cure = item["cure"]
        self.assertEqual(cure["valid_probe_count"], 1)
        self.assertTrue(cure["insufficient_probes"])
        self.assertEqual(len(cure["probe_gaps"]), 1)
        gap = cure["probe_gaps"][0]
        self.assertEqual(gap["probe_id"], "T2")
        self.assertEqual(gap["from"], _ts(0))
        self.assertEqual(gap["to"], _ts(5))
        self.assertEqual(gap["minutes"], 25.0)

        # 出炉判定：判定序列本身合格（无 UNDER_TIME），
        # 但缺报 + 有效探头不足，不得判定合格
        r = c.post(f"/api/batches/{bid}/unload",
                   json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "NOT_OK")
        self.assertIn("PROBE_GAP", res["flags"])
        self.assertIn("INSUFFICIENT_PROBES", res["flags"])
        self.assertNotIn("UNDER_TIME", res["flags"])
        self.assertEqual(res["cure"]["valid_probe_count"], 1)

    def test_card_shows_probe_gap_intervals(self):
        """随炉卡显示缺报区间起止与时长，与炉次详情及 JSON 档案一致。"""
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [25]))
        self.c.post(f"/api/batches/{bid}/unload",
                    json={"at": "2026-09-10T08:35:00"})

        detail_gaps = self._item(bid, "W-1")["cure"]["probe_gaps"]
        self.assertEqual(len(detail_gaps), 1)
        arch = self.c.get(f"/api/batches/{bid}/archive").get_json()
        arch_gaps = arch["items"][0]["cure"]["probe_gaps"]
        self.assertEqual(arch_gaps, detail_gaps)  # 档案与详情一致

        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        g = detail_gaps[0]
        self.assertIn("缺报区间", card)
        self.assertIn("T2", card)                    # 缺报探头
        self.assertIn(g["from"], card)               # 起始时间
        self.assertIn(g["to"], card)                 # 结束时间
        self.assertIn(f"{g['minutes']} 分钟", card)  # 时长

    # ---------------------------------------------------------- 5. 停用与重算
    def test_disable_detached_probe_recalc_and_audit(self):
        bid = self._trial([_order("W-1"), _order("W-2")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        # T2 松脱后持续报环境温度（卡值且拉低判定序列）
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [25, 30, 30, 30, 30, 30])
            + _readings("W-2", None, [25, 165, 170, 172, 171, 170]))
        before = self._item(bid, "W-1")["cure"]
        self.assertTrue(before["under_time"])  # 被 T2 低温拖累

        # 停用须填原因
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                        json={})
        self.assertEqual(r.status_code, 400)
        # 未绑定探头 404
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T-X/disable",
                        json={"reason": "x"})
        self.assertEqual(r.status_code, 404)

        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                        json={"reason": "探头松脱，报环境温度"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["status"], "DISABLED")
        # 只重算该工件：欠时消除，结果变化被记录
        self.assertTrue(body["recalc"]["before"]["under_time"])
        self.assertFalse(body["recalc"]["after"]["under_time"])
        self.assertEqual(body["cure"]["in_window_minutes"], 20.0)
        # 重复停用 409
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                        json={"reason": "again"})
        self.assertEqual(r.status_code, 409)

        # 出炉判定：W-1 合格（T2 已停用不产生卡值标记），W-2 不受影响
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T08:35:00"})
        results = {x["workpiece_id"]: x for x in r.get_json()["results"]}
        self.assertEqual(results["W-1"]["verdict"], "OK")
        self.assertEqual(results["W-2"]["verdict"], "OK")
        self.assertNotIn("STUCK_PROBE", results["W-1"]["flags"])

        # 审计与探头状态入档
        item = self._item(bid, "W-1")
        self.assertEqual(len(item["probe_actions"]), 1)
        act = item["probe_actions"][0]
        self.assertEqual(act["action"], "DISABLE")
        self.assertEqual(act["reason"], "探头松脱，报环境温度")
        self.assertTrue(act["before"]["under_time"])
        self.assertFalse(act["after"]["under_time"])
        t2 = next(p for p in item["cure"]["probes"] if p["probe_id"] == "T2")
        self.assertEqual(t2["status"], "DISABLED")
        self.assertEqual(t2["disabled_reason"], "探头松脱，报环境温度")
        # 已停用探头的历史卡值区间仍保留备查
        self.assertEqual(len(t2["anomalies"]), 1)

        # 出炉后不得再停用探头
        r = self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T1/disable",
                        json={"reason": "too late"})
        self.assertEqual(r.status_code, 409)

    # ---------------------------------------------------------- 6. 有效探头数
    def test_insufficient_probes_not_ok(self):
        app = create_app({"DATABASE": os.path.join(self.tmp.name, "t2.sqlite"),
                          "TESTING": True, "MIN_VALID_PROBES": 2})
        c = app.test_client()
        r = c.post("/api/schedule/trial", json={
            "reason": "t", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                         "hold_minutes": 15}],
            "forbidden_pairs": [], "orders": [_order("W-1")]})
        bid = r.get_json()["new_batches"][0]["batch_id"]
        c.post("/api/workpieces/W-1/probes",
               json={"probes": [{"probe_id": "T1", "offset_c": 0},
                                {"probe_id": "T2", "offset_c": 0}]})
        _bind_cal(c, "W-1", "T1", 0.0)
        _bind_cal(c, "W-1", "T2", 0.0)
        c.post(f"/api/batches/{bid}/issue")
        c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        c.post(f"/api/batches/{bid}/readings", json={"readings":
               _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
               + _readings("W-1", "T2", [24, 164, 169, 171, 170, 169])})
        # 停用一支后有效探头 1 < 2，不得判定合格
        c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
               json={"reason": "漂移"})
        r = c.post(f"/api/batches/{bid}/unload",
                   json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "NOT_OK")
        self.assertIn("INSUFFICIENT_PROBES", res["flags"])
        self.assertEqual(res["cure"]["valid_probe_count"], 1)

    def test_min_probes_satisfied_ok(self):
        app = create_app({"DATABASE": os.path.join(self.tmp.name, "t3.sqlite"),
                          "TESTING": True, "MIN_VALID_PROBES": 2})
        c = app.test_client()
        r = c.post("/api/schedule/trial", json={
            "reason": "t", "start_at": LOAD_AT, "ovens": [OVEN],
            "powders": [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                         "hold_minutes": 15}],
            "forbidden_pairs": [], "orders": [_order("W-1")]})
        bid = r.get_json()["new_batches"][0]["batch_id"]
        c.post("/api/workpieces/W-1/probes",
               json={"probes": [{"probe_id": "T1", "offset_c": 0},
                                {"probe_id": "T2", "offset_c": 0}]})
        _bind_cal(c, "W-1", "T1", 0.0)
        _bind_cal(c, "W-1", "T2", 0.0)
        c.post(f"/api/batches/{bid}/issue")
        c.post(f"/api/batches/{bid}/load", json={"at": LOAD_AT})
        c.post(f"/api/batches/{bid}/readings", json={"readings":
               _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
               + _readings("W-1", "T2", [24, 164, 169, 171, 170, 169])})
        r = c.post(f"/api/batches/{bid}/unload",
                   json={"at": "2026-09-10T08:35:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "OK", res)
        self.assertEqual(res["cure"]["valid_probe_count"], 2)

    # ---------------------------------------------------------- 7. 详情/档案/随炉卡
    def test_detail_archive_card_expose_probe_info(self):
        bid = self._trial([_order("W-1")])
        self._register("W-1", [{"probe_id": "T1", "offset_c": 0.5},
                               {"probe_id": "T2", "offset_c": 0}])
        self._issue_load(bid)
        self._post_readings(
            bid,
            _readings("W-1", "T1", [25, 165, 170, 172, 171, 170])
            + _readings("W-1", "T2", [170, 170, 170, 170, 170, 170]))
        self.c.post(f"/api/batches/{bid}/workpieces/W-1/probes/T2/disable",
                    json={"reason": "卡值"})
        self.c.post(f"/api/batches/{bid}/unload",
                    json={"at": "2026-09-10T08:35:00"})

        # 炉次详情
        item = self._item(bid, "W-1")
        self.assertEqual(len(item["cure"]["judgment_series"]), 6)
        self.assertEqual(len(item["cure"]["probes"]), 2)

        # JSON 档案
        r = self.c.get(f"/api/batches/{bid}/archive")
        self.assertEqual(r.status_code, 200)
        arch = r.get_json()
        a_item = next(i for i in arch["items"] if i["workpiece_id"] == "W-1")
        probes = {p["probe_id"]: p for p in a_item["cure"]["probes"]}
        self.assertEqual(probes["T1"]["offset_c"], 0.5)
        self.assertEqual(probes["T2"]["status"], "DISABLED")
        self.assertEqual(probes["T2"]["disabled_reason"], "卡值")
        self.assertTrue(a_item["cure"]["judgment_series"])
        self.assertEqual(len(a_item["probe_actions"]), 1)

        # 随炉卡
        card = self.c.get(f"/api/batches/{bid}/card").get_data(as_text=True)
        self.assertIn("探头与判定序列", card)
        self.assertIn("T1", card)
        self.assertIn("+0.50", card)          # 校准偏移
        self.assertIn("DISABLED", card)       # 探头状态
        self.assertIn("卡值", card)           # 停用原因/异常
        self.assertIn("判定序列", card)


if __name__ == "__main__":
    unittest.main()
