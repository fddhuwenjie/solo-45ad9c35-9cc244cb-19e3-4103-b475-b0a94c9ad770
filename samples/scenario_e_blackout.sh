#!/usr/bin/env bash
# 场景 E：停机窗（清炉/校准/检修）
#   试算带停机窗 → 整段占用区间避让 → 已签发炉次冲突检查 → 逐炉时间线 → 版本快照
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_e_blackout.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

OVEN='{
  "id": "OVEN-1",
  "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
  "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
  "ambient_c": 25, "turnaround_minutes": 15,
  "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
}'
POWDER='[{"batch_no": "P-RED", "temp_min_c": 160, "temp_max_c": 180, "hold_minutes": 15}]'

echo "== 1. 试算：08:30-09:00 检修停机，整段占用区间（装载+升温+保温+周转）避让 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_e1.json <<JSON
{
  "reason": "早班排产（含检修停机）",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN],
  "powders": $POWDER,
  "forbidden_pairs": [],
  "blackout_windows": [
    {"oven_id": "OVEN-1", "kind": "MAINTENANCE",
     "start_at": "2026-09-10T08:30:00", "end_at": "2026-09-10T09:00:00",
     "note": "月度检修"}
  ],
  "orders": [
    {"workpiece_id": "W-2001", "order_id": "PO-11", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T09:30:00"}
  ]
}
JSON
pp < /tmp/trial_e1.json
echo ">> 炉次整体移到 09:00 装载；blackout_wait_minutes=60，逾期变化见 lateness_*"

BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_e1.json'))['new_batches'][0]['batch_id'])")

echo "== 2. 签发该炉次（此后其时刻冻结，不得改） =="
curl -sS -X POST "$BASE/batches/$BID/issue" | pp

echo "== 3. 再次试算：新停机窗 09:30-10:00 撞上已签发炉次占用区间 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_e2.json <<JSON
{
  "reason": "临时校准停机",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN],
  "powders": $POWDER,
  "forbidden_pairs": [],
  "blackout_windows": [
    {"oven_id": "OVEN-1", "kind": "CALIBRATION",
     "start_at": "2026-09-10T09:30:00", "end_at": "2026-09-10T10:00:00",
     "note": "热电偶校准"}
  ],
  "orders": [
    {"workpiece_id": "W-2002", "order_id": "PO-12", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T12:00:00"}
  ]
}
JSON
pp < /tmp/trial_e2.json
echo ">> blackout_conflicts 列出与已签发炉次的重叠区间和冲突分钟；"
echo ">> oven_timelines 区分 PRODUCTION / TURNAROUND / BLACKOUT"

echo "== 4. 非法停机窗（倒序 / 同炉重叠 / 未知炉号）一律 400 =="
echo "-- 起止倒序 --"
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- <<JSON | pp
{
  "reason": "非法停机窗", "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN], "powders": $POWDER, "forbidden_pairs": [],
  "blackout_windows": [
    {"oven_id": "OVEN-1", "kind": "CLEANING",
     "start_at": "2026-09-10T09:00:00", "end_at": "2026-09-10T08:00:00"}
  ],
  "orders": []
}
JSON
echo "-- 同炉重叠 --"
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- <<JSON | pp
{
  "reason": "非法停机窗", "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN], "powders": $POWDER, "forbidden_pairs": [],
  "blackout_windows": [
    {"oven_id": "OVEN-1", "kind": "CLEANING",
     "start_at": "2026-09-10T09:00:00", "end_at": "2026-09-10T09:40:00"},
    {"oven_id": "OVEN-1", "kind": "CALIBRATION",
     "start_at": "2026-09-10T09:30:00", "end_at": "2026-09-10T10:00:00"}
  ],
  "orders": []
}
JSON
echo "-- 未知炉号 --"
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- <<JSON | pp
{
  "reason": "非法停机窗", "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN], "powders": $POWDER, "forbidden_pairs": [],
  "blackout_windows": [
    {"oven_id": "OVEN-X", "kind": "CLEANING",
     "start_at": "2026-09-10T07:00:00", "end_at": "2026-09-10T07:30:00"}
  ],
  "orders": []
}
JSON

echo "== 5. 版本快照与炉次详情：保留停机窗与计算依据 =="
VID=$(python3 -c "import json; print(json.load(open('/tmp/trial_e2.json'))['version']['id'])")
curl -sS "$BASE/versions/$VID" | pp
curl -sS "$BASE/batches/$BID" -o "$OUT_DIR/batch_${BID}_detail.json"
echo "saved: $OUT_DIR/batch_${BID}_detail.json（含 blackout_windows 与 schedule_basis）"
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json"
