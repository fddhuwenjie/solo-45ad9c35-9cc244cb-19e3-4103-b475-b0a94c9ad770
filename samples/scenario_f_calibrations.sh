#!/usr/bin/env bash
# 场景 F：探头校准证书版本化
#   登记探头 → 录入多点校准版本（非法版本被拒）→ 绑定版本
#   → 证书过期签发被阻止（列出工件/探头）→ 换证后签发 → 入炉
#   → 测温按冻结点列线性插值（区间外读数 CALIBRATION_RANGE 告警）
#   → 出炉判定 → 档案/随炉卡保留证书版本/插值区间/到期状态
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_f_calibrations.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 1. 试算（1 件工件，粉料温区 160–180℃） =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_f.json <<'JSON'
{
  "reason": "校准证书版本化演示",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P-RED", "temp_min_c": 160, "temp_max_c": 180, "hold_minutes": 20}
  ],
  "forbidden_pairs": [],
  "orders": [
    {"workpiece_id": "W-6001", "order_id": "PO-61", "length_mm": 800, "width_mm": 400,
     "height_mm": 300, "weight_kg": 12, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T14:00:00"}
  ]
}
JSON
pp < /tmp/trial_f.json
BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_f.json'))['new_batches'][0]['batch_id'])")
echo ">> 新炉次号: $BID"

echo "== 2. 登记探头 T1 =="
curl -sS -X POST "$BASE/workpieces/W-6001/probes" -H 'Content-Type: application/json' \
  -d '{"probe_id": "T1", "offset_c": 0}' | pp

echo "== 3. 非法校准版本被拒（少于两点 / 时间倒置 / 点列不递增） =="
curl -sS -X POST "$BASE/workpieces/W-6001/probes/T1/calibrations" \
  -H 'Content-Type: application/json' \
  -d '{"certificate_no": "BAD-1", "calibrated_at": "2026-09-01T00:00:00",
       "valid_until": "2026-12-31T00:00:00",
       "points": [{"indicated_c": 100, "reference_c": 100}]}' | pp
curl -sS -X POST "$BASE/workpieces/W-6001/probes/T1/calibrations" \
  -H 'Content-Type: application/json' \
  -d '{"certificate_no": "BAD-2", "calibrated_at": "2026-09-10T00:00:00",
       "valid_until": "2026-09-01T00:00:00",
       "points": [{"indicated_c": 100, "reference_c": 100},
                  {"indicated_c": 200, "reference_c": 200}]}' | pp
curl -sS -X POST "$BASE/workpieces/W-6001/probes/T1/calibrations" \
  -H 'Content-Type: application/json' \
  -d '{"certificate_no": "BAD-3", "calibrated_at": "2026-09-01T00:00:00",
       "valid_until": "2026-12-31T00:00:00",
       "points": [{"indicated_c": 200, "reference_c": 200},
                  {"indicated_c": 100, "reference_c": 100}]}' | pp

echo "== 4. 录入已过期证书并绑定 → 签发被阻止（CALIBRATION_EXPIRED） =="
curl -sS -X POST "$BASE/workpieces/W-6001/probes/T1/calibrations" \
  -H 'Content-Type: application/json' --data @- -o /tmp/cal_old.json <<'JSON'
{"certificate_no": "CERT-2025-001", "calibrated_at": "2025-09-01T00:00:00",
 "valid_until": "2026-09-01T00:00:00",
 "points": [{"indicated_c": 100, "reference_c": 100},
            {"indicated_c": 150, "reference_c": 150},
            {"indicated_c": 200, "reference_c": 200}]}
JSON
pp < /tmp/cal_old.json
OLD_ID=$(python3 -c "import json; print(json.load(open('/tmp/cal_old.json'))['calibration']['calibration_id'])")
curl -sS -X POST "$BASE/workpieces/W-6001/probes" -H 'Content-Type: application/json' \
  -d "{\"probe_id\": \"T1\", \"offset_c\": 0, \"calibration_id\": $OLD_ID}" | pp
echo "-- 签发（应 409，列出工件 W-6001 / 探头 T1） --"
curl -sS -X POST "$BASE/batches/$BID/issue" | pp

echo "== 5. 换发新证书（有效、覆盖温区）并改绑 → 签发成功 =="
curl -sS -X POST "$BASE/workpieces/W-6001/probes/T1/calibrations" \
  -H 'Content-Type: application/json' --data @- -o /tmp/cal_new.json <<'JSON'
{"certificate_no": "CERT-2026-118", "calibrated_at": "2026-09-01T00:00:00",
 "valid_until": "2027-03-01T00:00:00",
 "points": [{"indicated_c": 100, "reference_c": 100.0},
            {"indicated_c": 150, "reference_c": 150.5},
            {"indicated_c": 200, "reference_c": 201.0}]}
JSON
pp < /tmp/cal_new.json
NEW_ID=$(python3 -c "import json; print(json.load(open('/tmp/cal_new.json'))['calibration']['calibration_id'])")
curl -sS -X POST "$BASE/workpieces/W-6001/probes" -H 'Content-Type: application/json' \
  -d "{\"probe_id\": \"T1\", \"offset_c\": 0, \"calibration_id\": $NEW_ID}" | pp
curl -sS -X POST "$BASE/batches/$BID/issue" | pp

echo "== 6. 历史版本查询（v1 过期证书 + v2 新证书，均不可覆盖） =="
curl -sS "$BASE/workpieces/W-6001/probes/T1/calibrations" | pp

echo "== 7. 入炉 + 测温回传（按冻结点列插值；08:20 示值 215℃ 超区间） =="
curl -sS -X POST "$BASE/batches/$BID/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp
python3 - > /tmp/readings_f.json <<'PY'
import json
# 示值 165 → 插值 165.83…；215 超出点列上限 200 → CALIBRATION_RANGE，不计入保温
temps = [25, 90, 140, 160, 165, 215, 170, 172, 171, 170, 171, 172, 171]
readings = []
for i, t in enumerate(temps):
    h, m = 8 + (i * 5) // 60, (i * 5) % 60
    readings.append({"workpiece_id": "W-6001", "probe_id": "T1",
                     "ts": f"2026-09-10T{h:02d}:{m:02d}:00", "metal_temp_c": t})
print(json.dumps({"readings": readings}))
PY
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/readings_f.json | pp

echo "== 8. 在炉进度：CALIBRATION_RANGE 告警 + 证书版本/插值区间/到期状态 =="
curl -sS "$BASE/batches/$BID/progress?as_of=2026-09-10T09:05:00" | pp

echo "== 9. 出炉判定（超区间读数未计入保温，判定序列合格） =="
curl -sS -X POST "$BASE/batches/$BID/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T09:10:00"}' | pp

echo "== 10. 结案 + 下载档案与随炉卡（保留证书版本/插值区间/到期状态） =="
curl -sS -X POST "$BASE/workpieces/W-6001/close" -H 'Content-Type: application/json' \
  -d '{"note": "检验合格入库"}' | pp
curl -sS -X POST "$BASE/batches/$BID/close" | pp
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "saved: $OUT_DIR/batch_${BID}_card.html （浏览器打开即可打印）"
