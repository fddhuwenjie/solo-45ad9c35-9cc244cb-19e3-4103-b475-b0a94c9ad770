#!/usr/bin/env bash
# 场景 H：厚板离炉后冷却放行 —— 包装耐温门限
#   试算（粉料带包装温度上限/低温保持时长）→ 签发 → 入炉 → 固化测温
#   → 厚板合格离炉（COOLING，暂不计完成）→ 冷却表面测温
#   → 提前搬运被拒（409，给保持分钟/最早放行/未满足项）
#   → 再次升温截断区间 → 紧急搬运须理由（送返工处置）
#   → 第二块板正常冷却放行 → 炉次结案须引用有效放行 → 档案/随炉卡
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_h_cooling.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/health" | pp

echo "== 1. 试算（厚板 H-PLATE 与薄板 S-PLATE 同炉；粉料带包装门限 50℃×20min） =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_h.json <<'JSON'
{
  "reason": "厚板冷却放行演示",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P-PACK", "temp_min_c": 170, "temp_max_c": 190, "hold_minutes": 10,
     "pack_temp_limit_c": 50, "low_temp_hold_minutes": 20}
  ],
  "forbidden_pairs": [],
  "orders": [
    {"workpiece_id": "H-PLATE", "order_id": "PO-H", "length_mm": 1200, "width_mm": 800,
     "height_mm": 60, "weight_kg": 60, "powder_batch": "P-PACK", "compat_group": null,
     "due_at": "2026-09-10T18:00:00"},
    {"workpiece_id": "S-PLATE", "order_id": "PO-H", "length_mm": 500, "width_mm": 400,
     "height_mm": 20, "weight_kg": 8, "powder_batch": "P-PACK", "compat_group": null,
     "due_at": "2026-09-10T18:00:00"}
  ]
}
JSON
pp < /tmp/trial_h.json >/dev/null

BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_h.json'))['new_batches'][0]['batch_id'])")
echo ">> 新炉次号: $BID"

echo "== 2. 签发（门限冻结到工件）/ 入炉 =="
curl -sS -X POST "$BASE/batches/$BID/issue" | pp
curl -sS -X POST "$BASE/batches/$BID/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 3. 固化测温（08:05–08:55 在窗，避免尾部缺报与卡值） =="
python3 - > /tmp/cure_h.json <<'PY'
import json
def series(wid):
    pts = [{"workpiece_id": wid, "ts": "2026-09-10T08:00:00", "metal_temp_c": 25}]
    for i, t in enumerate(range(485, 540, 5)):
        pts.append({"workpiece_id": wid,
                    "ts": f"2026-09-10T{t//60:02d}:{t%60:02d}:00",
                    "metal_temp_c": 180.0 + (i % 3 - 1) * 0.5})
    return pts
print(json.dumps({"readings": series("H-PLATE") + series("S-PLATE")}))
PY
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/cure_h.json | pp >/dev/null
echo "（固化读数已回传）"

echo "== 4. 两件合格离炉：进入 COOLING（不是 DONE），炉次转 UNLOADED =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/unload" \
  -H 'Content-Type: application/json' -d '{"at": "2026-09-10T09:00:00"}' | pp
curl -sS -X POST "$BASE/batches/$BID/workpieces/S-PLATE/unload" \
  -H 'Content-Type: application/json' -d '{"at": "2026-09-10T09:00:00"}' \
  | pp | grep -E '"state"|"verdict"' || true
curl -sS "$BASE/workpieces/H-PLATE" | pp | grep '"status"'

echo "== 5. 厚板冷却测温：09:00–09:10 表面 45℃（累计 10 分钟，门限 20） =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/cooling-readings" \
  -H 'Content-Type: application/json' --data @- <<'JSON' | pp
{"at": "2026-09-10T09:10:00",
 "readings": [
   {"ts": "2026-09-10T09:00:00", "surface_temp_c": 48},
   {"ts": "2026-09-10T09:05:00", "surface_temp_c": 46},
   {"ts": "2026-09-10T09:10:00", "surface_temp_c": 45}
 ]}
JSON

echo "== 5b. 09:10 提前搬运：409（HOLD_NOT_MET，最早放行 09:20） =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/release" \
  -H 'Content-Type: application/json' -d '{"at": "2026-09-10T09:10:00"}' | pp || true

echo "== 6. 09:15 再次升温 58℃：截断连续低温区间（REHEAT），保持清零 =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/cooling-readings" \
  -H 'Content-Type: application/json' --data @- <<'JSON' | pp
{"at": "2026-09-10T09:15:00",
 "readings": [{"ts": "2026-09-10T09:15:00", "surface_temp_c": 58}]}
JSON

echo "== 7. 紧急搬运不给理由：400；给理由后送返工处置（REWORK_PENDING） =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/release" \
  -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T09:15:00", "emergency": true}' | pp || true
curl -sS -X POST "$BASE/batches/$BID/workpieces/H-PLATE/release" \
  -H 'Content-Type: application/json' --data @- <<'JSON' | pp
{"at": "2026-09-10T09:15:00", "emergency": true,
 "reason": "后工序急件插单，线长与质量签字，先转返工区隔离处置"}
JSON
curl -sS -X POST "$BASE/workpieces/H-PLATE/rework" | pp

echo "== 8. 薄板正常冷却：09:00–09:20 连续 45℃，09:20 正常放行 -> DONE =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/S-PLATE/cooling-readings" \
  -H 'Content-Type: application/json' --data @- <<'JSON' | pp
{"at": "2026-09-10T09:20:00",
 "readings": [
   {"ts": "2026-09-10T09:00:00", "surface_temp_c": 45},
   {"ts": "2026-09-10T09:05:00", "surface_temp_c": 44},
   {"ts": "2026-09-10T09:10:00", "surface_temp_c": 45},
   {"ts": "2026-09-10T09:15:00", "surface_temp_c": 44},
   {"ts": "2026-09-10T09:20:00", "surface_temp_c": 45}
 ]}
JSON
curl -sS -X POST "$BASE/batches/$BID/workpieces/S-PLATE/release" \
  -H 'Content-Type: application/json' -d '{"at": "2026-09-10T09:20:00"}' | pp

echo "== 9. 炉次冷却汇总 + 结案（引用有效放行；厚板已回返工队列） =="
curl -sS "$BASE/batches/$BID/cooling" | pp
curl -sS -X POST "$BASE/batches/$BID/close" | pp

echo "== 10. 档案与随炉卡：冻结门限/完整读数/区间中断/人工决定 =="
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json（release_order、items[].cooling）"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "saved: $OUT_DIR/batch_${BID}_card.html （冷却放行段含门限/中断/人工决定）"
