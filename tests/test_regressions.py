"""三处已修复缺陷的回归测试：

1. 试算遇到未登记粉料 → 工件进 unscheduled（UNKNOWN_POWDER），不得 500；
2. 已签发炉次保留签发时的工件尺寸与固化窗口，主数据变更/新版本不影响查询与判定；
3. 测温接口拒绝早于实际入炉时刻的读数（不落库、不计入有效保温）。

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


def _order(wid, powder="P1", length=500, weight=10):
    return {"workpiece_id": wid, "order_id": "PO-T", "length_mm": length,
            "width_mm": 400, "height_mm": 300, "weight_kg": weight,
            "powder_batch": powder, "compat_group": None,
            "due_at": "2026-09-10T18:00:00"}


def _trial_payload(orders, powders=None, reason="t"):
    return {"reason": reason, "start_at": "2026-09-10T08:00:00",
            "ovens": [OVEN],
            "powders": powders if powders is not None
            else [{"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180,
                   "hold_minutes": 15}],
            "forbidden_pairs": [], "orders": orders}


class RegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.sqlite"),
                               "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _trial(self, payload):
        r = self.c.post("/api/schedule/trial", json=payload)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(r.content_type, "application/json")
        return r.get_json()

    def _issue_load(self, bid, at="2026-09-10T08:00:00"):
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.c.post(f"/api/batches/{bid}/load", json={"at": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # 缺陷 1：未登记粉料不得 500，应进 unscheduled
    def test_unknown_powder_goes_to_unscheduled(self):
        d = self._trial(_trial_payload([_order("W-OK"), _order("W-BAD", powder="P-GHOST")]))
        bad = [u for u in d["unscheduled"] if u["workpiece_id"] == "W-BAD"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["reason"], "UNKNOWN_POWDER")
        # 正常工件不受影响，仍被编排
        planned = [i["workpiece_id"] for b in d["new_batches"] for i in b["items"]]
        self.assertIn("W-OK", planned)
        self.assertNotIn("W-BAD", planned)

    # 缺陷 1b：复用 workpiece_id 提交未登记粉料，不得按库内残留旧粉料编排
    def test_reused_id_with_unknown_powder_not_planned(self):
        # v1：W-REUSE 用已登记粉料正常排产（草稿）
        d1 = self._trial(_trial_payload([_order("W-REUSE")], reason="v1"))
        planned1 = [i["workpiece_id"] for b in d1["new_batches"] for i in b["items"]]
        self.assertIn("W-REUSE", planned1)

        # v2：同一 workpiece_id 提交未登记粉料
        d2 = self._trial(_trial_payload([_order("W-REUSE", powder="P-GHOST")],
                                        reason="v2 换粉料(未登记)"))
        # 只出现在 unscheduled，且原因 UNKNOWN_POWDER
        bad = [u for u in d2["unscheduled"] if u["workpiece_id"] == "W-REUSE"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["reason"], "UNKNOWN_POWDER")
        # 不得进入 new_batches
        planned2 = [i["workpiece_id"] for b in d2["new_batches"] for i in b["items"]]
        self.assertNotIn("W-REUSE", planned2)
        # 数据库状态不得仍为 SCHEDULED
        w = self.c.get("/api/workpieces/W-REUSE").get_json()
        self.assertNotEqual(w["status"], "SCHEDULED")

    # 缺陷 2：已签发炉次冻结签发时的尺寸与固化窗口
    def test_issued_batch_snapshot_frozen(self):
        d = self._trial(_trial_payload([_order("W-1")], reason="v1"))
        bid = d["new_batches"][0]["batch_id"]
        r = self.c.post(f"/api/batches/{bid}/issue")
        self.assertEqual(r.status_code, 200)

        # 修改主数据：粉料窗口 160-180/15 → 200-220/99，工件尺寸/重量改大
        self._trial(_trial_payload(
            [_order("W-1", length=999, weight=99)],
            powders=[{"batch_no": "P1", "temp_min_c": 200, "temp_max_c": 220,
                      "hold_minutes": 99}],
            reason="v2 改主数据"))

        # 查询结果必须保持签发时的快照
        item = self.c.get(f"/api/batches/{bid}").get_json()["items"][0]
        self.assertEqual(item["length_mm"], 500)
        self.assertEqual(item["weight_kg"], 10)
        self.assertEqual(item["cure"]["required_hold_minutes"], 15)

        # 出炉判定也必须按签发时窗口：170℃ 保温在旧窗口合格、在新窗口欠温
        self.c.post(f"/api/batches/{bid}/load", json={"at": "2026-09-10T08:00:00"})
        readings = [{"workpiece_id": "W-1",
                     "ts": f"2026-09-10T{8 + (i * 5) // 60:02d}:{(i * 5) % 60:02d}:00",
                     "metal_temp_c": t}
                    for i, t in enumerate([25, 165, 170, 172, 171, 170])]
        r = self.c.post(f"/api/batches/{bid}/readings", json={"readings": readings})
        self.assertEqual(r.get_json()["accepted"], 6)
        r = self.c.post(f"/api/batches/{bid}/unload",
                        json={"at": "2026-09-10T08:30:00"})
        res = r.get_json()["results"][0]
        self.assertEqual(res["verdict"], "OK", res)
        self.assertEqual(res["cure"]["in_window_minutes"], 20.0)

    # 缺陷 3：早于实际入炉时刻的测温读数被拒收
    def test_readings_before_load_rejected(self):
        d = self._trial(_trial_payload([_order("W-1")]))
        bid = d["new_batches"][0]["batch_id"]
        self._issue_load(bid, at="2026-09-10T08:00:00")

        r = self.c.post(f"/api/batches/{bid}/readings", json={"readings": [
            {"workpiece_id": "W-1", "ts": "2026-09-10T07:30:00", "metal_temp_c": 170},
            {"workpiece_id": "W-1", "ts": "2026-09-10T08:05:00", "metal_temp_c": 165},
            {"workpiece_id": "W-1", "ts": "2026-09-10T08:10:00", "metal_temp_c": 170},
        ]})
        body = r.get_json()
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(len(body["rejected"]), 1)
        self.assertEqual(body["rejected"][0]["workpiece_id"], "W-1")
        self.assertIn("入炉", body["rejected"][0]["reason"])

        # 被拒读数不落库、不计入有效保温
        item = self.c.get(f"/api/batches/{bid}").get_json()["items"][0]
        self.assertEqual(item["cure"]["reading_count"], 2)
        self.assertEqual(item["cure"]["in_window_minutes"], 5.0)


if __name__ == "__main__":
    unittest.main()
