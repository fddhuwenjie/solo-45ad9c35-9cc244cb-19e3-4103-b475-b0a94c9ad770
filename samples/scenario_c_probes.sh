#!/usr/bin/env bash
# 场景 C：多探头测温
#   登记探头(含校准偏移) → 签发(冻结配置) → 入炉 → 双探头回传(幂等去重)
#   → 探头松脱卡值 → 出炉前停用故障探头并重算 → 出炉判定 → 档案/随炉卡
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_c_probes.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 1. 试算（2 件工件） =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_c.json <<'JSON'
{
  "reason": "多探头测温演示",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P-RED", "temp_min_c": 170, "temp_max_c": 190, "hold_minutes": 20}
  ],
  "forbidden_pairs": [],
  "orders": [
    {"workpiece_id": "W-3001", "order_id": "PO-11", "length_mm": 800, "width_mm": 400,
     "height_mm": 300, "weight_kg": 12, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T14:00:00"},
    {"workpiece_id": "W-3002", "order_id": "PO-11", "length_mm": 700, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P-RED", "compat_group": null,
     "due_at": "2026-09-10T14:00:00"}
  ]
}
JSON
pp < /tmp/trial_c.json
BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_c.json'))['new_batches'][0]['batch_id'])")
echo ">> 新炉次号: $BID"

echo "== 2. 登记探头（W-3001 双探头带校准偏移；W-3002 单探头） =="
curl -sS -X POST "$BASE/workpieces/W-3001/probes" -H 'Content-Type: application/json' \
  -d '{"probes": [{"probe_id": "T1", "offset_c": 0.5},
                  {"probe_id": "T2", "offset_c": -0.5}]}' | pp
curl -sS -X POST "$BASE/workpieces/W-3002/probes" -H 'Content-Type: application/json' \
  -d '{"probe_id": "T1", "offset_c": 0}' | pp

echo "== 3. 签发（冻结探头配置） + 入炉 =="
curl -sS -X POST "$BASE/batches/$BID/issue" | pp
curl -sS -X POST "$BASE/batches/$BID/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 4. 测温回传（W-3001 的 T2 中途松脱：卡值并拉低判定序列） =="
python3 - > /tmp/readings_c.json <<'PY'
import json
temps = [25, 60, 100, 140, 165, 178, 182, 184, 183, 182, 184, 183, 182, 181, 120]
readings = []
for i, t in enumerate(temps):
    h, m = 8 + (i * 5) // 60, (i * 5) % 60
    ts = f"2026-09-10T{h:02d}:{m:02d}:00"
    readings.append({"workpiece_id": "W-3001", "probe_id": "T1",
                     "ts": ts, "metal_temp_c": t})
    # T2 从第 5 个点起松脱，读数卡在环境温度
    t2 = t if i < 5 else 28.0
    readings.append({"workpiece_id": "W-3001", "probe_id": "T2",
                     "ts": ts, "metal_temp_c": t2})
    readings.append({"workpiece_id": "W-3002", "probe_id": "T1",
                     "ts": ts, "metal_temp_c": t})
print(json.dumps({"readings": readings}))
PY
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/readings_c.json | pp

echo "== 4b. 原样重传（幂等去重，duplicates 应等于条数） =="
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/readings_c.json | pp

echo "== 5. 炉次详情：W-3001 被 T2 低温拖累欠时，T2 卡值区间可见 =="
curl -sS "$BASE/batches/$BID" | pp

echo "== 6. 出炉前停用故障探头 T2（填写原因），只重算 W-3001 =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/W-3001/probes/T2/disable" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "探头松脱，读数卡在环境温度"}' | pp

echo "== 7. 出炉判定（W-3001 重算后应合格） =="
curl -sS -X POST "$BASE/batches/$BID/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T09:10:00"}' | pp

echo "== 8. 结案 + 下载档案与随炉卡（含探头状态/校准/异常区间/判定序列） =="
for W in W-3001 W-3002; do
  curl -sS -X POST "$BASE/workpieces/$W/close" -H 'Content-Type: application/json' \
    -d '{"note": "检验合格入库"}' | pp
done
curl -sS -X POST "$BASE/batches/$BID/close" | pp
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "saved: $OUT_DIR/batch_${BID}_card.html （浏览器打开即可打印）"
