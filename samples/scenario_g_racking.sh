#!/usr/bin/env bash
# 场景 G：吊具布置与载荷平衡
#   挂杆/吊点坐标模型 → 整组力矩平衡 → 临时封位避让 → 人工布置校验
#   → 吊点故障只重排未签发炉次（已签发冻结）→ 迁移工件/交期变化 → 档案/随炉卡
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_g_racking.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

# 单根挂杆 R1，3 个吊点（坐标 -1000/0/+1000，单点 80kg），左右分区各 120kg，
# 横梁总载 600kg，左右偏载力矩容差 200000 kg·mm
OVEN='{
  "id": "OVEN-1",
  "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
  "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
  "ambient_c": 25, "turnaround_minutes": 15,
  "hanger_slots": 3, "hanger_spacing_mm": 1000, "hanger_max_load_kg": 80,
  "hanger_rack": {
    "rods": [{
      "id": "R1", "axis": "L", "y_mm": 900, "z_mm": 1400,
      "points": [
        {"index": 1, "x_mm": -1000, "max_load_kg": 80},
        {"index": 2, "x_mm": 0,     "max_load_kg": 80},
        {"index": 3, "x_mm": 1000,  "max_load_kg": 80}
      ],
      "zones": [
        {"id": "ZL", "x_min_mm": -1500, "x_max_mm": -1, "max_load_kg": 120},
        {"id": "ZR", "x_min_mm": 0,     "x_max_mm": 1500, "max_load_kg": 120}
      ]
    }],
    "beam_max_load_kg": 600,
    "moment_tolerance_kg_mm": 200000
  }
}'
POWDER='[{"batch_no": "P-RED", "temp_min_c": 160, "temp_max_c": 180, "hold_minutes": 15}]'

echo "== 1. 试算：两扇大门板（吊耳 ±1000，重心略偏）整组平衡布置 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_g1.json <<JSON
{
  "reason": "门板整组平衡排产",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN],
  "powders": $POWDER,
  "forbidden_pairs": [],
  "orders": [
    {"workpiece_id": "W-3001", "order_id": "PO-G1", "length_mm": 2200, "width_mm": 800,
     "height_mm": 60, "weight_kg": 60, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T14:00:00",
     "lift_points_mm": [-1000, 1000], "cg_offset_x_mm": 80, "clearance_mm": 100},
    {"workpiece_id": "W-3002", "order_id": "PO-G1", "length_mm": 2200, "width_mm": 800,
     "height_mm": 60, "weight_kg": 60, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T14:00:00",
     "lift_points_mm": [-1000, 1000], "cg_offset_x_mm": -80, "clearance_mm": 100}
  ]
}
JSON
pp < /tmp/trial_g1.json
echo ">> 关注 rack_layout：两扇门板吊点载荷（杠杆法）、左右力矩接近 0、搬入顺序"

BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_g1.json'))['new_batches'][0]['batch_id'])")

echo "== 2. 人工调整校验：把两件都指到同一吊点（应 409：POINT_TAKEN + MOMENT） =="
set +e
curl -sS -X POST "$BASE/batches/$BID/arrangement/verify" -H 'Content-Type: application/json' \
  --data @- <<'JSON' | pp
{"assignments": [
  {"workpiece_id": "W-3001", "rod_id": "R1", "point_indices": [1]},
  {"workpiece_id": "W-3002", "rod_id": "R1", "point_indices": [1]}
]}
JSON
set -e

echo "== 3. 人工调整校验：分置两侧吊点（apply=true 采用） =="
curl -sS -X POST "$BASE/batches/$BID/arrangement/verify" -H 'Content-Type: application/json' \
  --data @- <<JSON | pp
{"apply": true, "assignments": [
  {"workpiece_id": "W-3001", "rod_id": "R1", "point_indices": [1, 2]},
  {"workpiece_id": "W-3002", "rod_id": "R1", "point_indices": [2, 3]}
]}
JSON

echo "== 4. 临时封位：吊点 2 在 08:00-20:00 禁用，重新试算 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_g2.json <<JSON
{
  "reason": "吊点 2 临时封掉",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [$OVEN],
  "powders": $POWDER,
  "forbidden_pairs": [],
  "hanger_blackouts": [
    {"oven_id": "OVEN-1", "rod_id": "R1", "point_index": 2,
     "start_at": "2026-09-10T08:00:00", "end_at": "2026-09-10T20:00:00",
     "note": "挂点检修"}
  ],
  "orders": [
    {"workpiece_id": "W-3003", "order_id": "PO-G2", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-RED", "due_at": "2026-09-10T12:00:00"},
    {"workpiece_id": "W-3004", "order_id": "PO-G2", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-RED", "due_at": "2026-09-10T12:00:00"}
  ]
}
JSON
pp < /tmp/trial_g2.json
echo ">> 两件不得使用吊点 2（POINT_BLOCKED），按整组平衡重排"

echo "== 5. 吊点故障：登记吊点 3 裂纹，只重排未签发炉次 =="
curl -sS -X POST "$BASE/schedule/point-fault" -H 'Content-Type: application/json' \
  --data @- <<'JSON' | pp
{"oven_id": "OVEN-1", "rod_id": "R1", "point_index": 3,
 "reason": "发现裂纹", "started_at": "2026-09-10T08:00:00"}
JSON
echo ">> migrations 给出迁移工件/挂位/出炉时刻变化；若有已签发炉次占用该点，"
echo ">> frozen_conflicts 列出且炉次不改动"

echo "== 6. 故障清单与修复 =="
curl -sS "$BASE/schedule/point-faults?active=1" | pp
FID=$(curl -sS "$BASE/schedule/point-faults?active=1" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['faults'][0]['id'])")
curl -sS -X POST "$BASE/schedule/point-fault/$FID/resolve" -H 'Content-Type: application/json' \
  -d '{"note": "更换吊具"}' | pp

echo "== 7. 下载档案与随炉卡（含坐标/载荷/力矩/搬入顺序） =="
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "档案已保存: samples/out/batch_${BID}_archive.json"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "随炉卡已保存: samples/out/batch_${BID}_card.html"
