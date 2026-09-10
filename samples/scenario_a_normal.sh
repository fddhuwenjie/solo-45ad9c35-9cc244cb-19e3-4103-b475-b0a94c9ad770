#!/usr/bin/env bash
# 场景 A：正常闭环
#   试算 → 签发 → 入炉 → 测温回传 → 出炉判定(合格) → 结案 → 下载档案与随炉卡
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_a_normal.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/health" | pp

echo "== 1. 试算（4 件工件，两种粉料，窗口有交集可混炉） =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_a.json <<'JSON'
{
  "reason": "早班初始排产",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P-RED",  "temp_min_c": 170, "temp_max_c": 190, "hold_minutes": 20},
    {"batch_no": "P-BLUE", "temp_min_c": 175, "temp_max_c": 200, "hold_minutes": 25}
  ],
  "forbidden_pairs": [["G1", "G3"]],
  "orders": [
    {"workpiece_id": "W-1001", "order_id": "PO-01", "length_mm": 800, "width_mm": 400,
     "height_mm": 300, "weight_kg": 12, "powder_batch": "P-RED",  "compat_group": "G1",
     "due_at": "2026-09-10T14:00:00"},
    {"workpiece_id": "W-1002", "order_id": "PO-01", "length_mm": 700, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-BLUE", "compat_group": "G1",
     "due_at": "2026-09-10T14:00:00"},
    {"workpiece_id": "W-1003", "order_id": "PO-02", "length_mm": 900, "width_mm": 500,
     "height_mm": 400, "weight_kg": 20, "powder_batch": "P-RED",  "compat_group": "G2",
     "due_at": "2026-09-10T16:00:00"},
    {"workpiece_id": "W-1004", "order_id": "PO-02", "length_mm": 600, "width_mm": 500,
     "height_mm": 400, "weight_kg": 15, "powder_batch": "P-BLUE", "compat_group": "G2",
     "due_at": "2026-09-10T16:00:00"}
  ]
}
JSON
pp < /tmp/trial_a.json

BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_a.json'))['new_batches'][0]['batch_id'])")
echo ">> 新炉次号: $BID"

echo "== 2. 签发 =="
curl -sS -X POST "$BASE/batches/$BID/issue" | pp

echo "== 3. 入炉 =="
curl -sS -X POST "$BASE/batches/$BID/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 4. 测温回传（每 5 分钟一点，金属温度进入窗口后保持） =="
python3 - > /tmp/readings_a.json <<'PY'
import json
temps = [25, 60, 100, 140, 165, 178, 182, 184, 183, 182, 184, 183, 182, 181, 120]
readings = []
for wid in ("W-1001", "W-1002", "W-1003", "W-1004"):
    for i, t in enumerate(temps):
        h, m = 8 + (i * 5) // 60, (i * 5) % 60
        readings.append({"workpiece_id": wid,
                         "ts": f"2026-09-10T{h:02d}:{m:02d}:00",
                         "metal_temp_c": t})
print(json.dumps({"readings": readings}))
PY
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/readings_a.json | pp

echo "== 5. 出炉判定（应全部 OK） =="
curl -sS -X POST "$BASE/batches/$BID/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T09:10:00"}' | pp

echo "== 6. 工件结案 + 炉次结案 =="
for W in W-1001 W-1002 W-1003 W-1004; do
  curl -sS -X POST "$BASE/workpieces/$W/close" -H 'Content-Type: application/json' \
    -d '{"note": "检验合格入库"}' | pp
done
curl -sS -X POST "$BASE/batches/$BID/close" | pp

echo "== 7. 下载 JSON 炉次档案与随炉卡 =="
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "saved: $OUT_DIR/batch_${BID}_card.html （浏览器打开即可打印）"
