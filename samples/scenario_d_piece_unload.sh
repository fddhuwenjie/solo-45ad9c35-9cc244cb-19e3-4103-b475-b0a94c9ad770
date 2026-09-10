#!/usr/bin/env bash
# 场景 D：轻薄件/厚重件不同时达标 —— 逐件出炉
#   试算 → 签发 → 入炉 → 测温回传
#   → 轻薄件先达标单独出炉（炉次保持 IN_OVEN）
#   → 厚重件普通请求被拒（返回累计/剩余/阻塞原因）→ 强制出炉须填原因
#   → 最后一件离炉后炉次自动 UNLOADED → 档案与随炉卡含离炉顺序与进度快照
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_d_piece_unload.sh
# 注意：重复运行前请删除 instance/ovenline.sqlite 以获得干净数据
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$OUT_DIR"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/health" | pp

echo "== 1. 试算（2 件：轻薄件 THIN 与厚重件 THICK，同粉料同炉） =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_d.json <<'JSON'
{
  "reason": "轻薄/厚重混炉，逐件出炉演示",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P-MIX", "temp_min_c": 170, "temp_max_c": 190, "hold_minutes": 20}
  ],
  "forbidden_pairs": [],
  "orders": [
    {"workpiece_id": "THIN",  "order_id": "PO-D", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 8,  "powder_batch": "P-MIX", "compat_group": null,
     "due_at": "2026-09-10T16:00:00"},
    {"workpiece_id": "THICK", "order_id": "PO-D", "length_mm": 1200, "width_mm": 800,
     "height_mm": 600, "weight_kg": 40, "powder_batch": "P-MIX", "compat_group": null,
     "due_at": "2026-09-10T16:00:00"}
  ]
}
JSON
pp < /tmp/trial_d.json

BID=$(python3 -c "import json; print(json.load(open('/tmp/trial_d.json'))['new_batches'][0]['batch_id'])")
echo ">> 新炉次号: $BID"

echo "== 2. 签发 / 入炉 =="
curl -sS -X POST "$BASE/batches/$BID/issue" | pp
curl -sS -X POST "$BASE/batches/$BID/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 3. 测温：THIN 升温快（08:25 即累计 20 分钟），THICK 升温慢 =="
python3 - > /tmp/readings_d.json <<'PY'
import json
def series(wid, in_window_from, count):
    pts = [{"workpiece_id": wid, "ts": "2026-09-10T08:00:00", "metal_temp_c": 25}]
    h0, m0 = in_window_from
    for i in range(count):
        t = h0 * 60 + m0 + i * 5
        pts.append({"workpiece_id": wid,
                    "ts": f"2026-09-10T{t // 60:02d}:{t % 60:02d}:00",
                    "metal_temp_c": 180.0 + (i % 3 - 1)})
    return pts
readings = (series("THIN", (8, 5), 5)      # 08:05-08:25，累计 20 分钟
            + series("THICK", (8, 15), 2)) # 08:15-08:20，累计 5 分钟
print(json.dumps({"readings": readings}))
PY
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  --data @/tmp/readings_d.json | pp > /tmp/readings_d_resp.json
cat /tmp/readings_d_resp.json

echo "== 4. THIN 先达标：逐件出炉（炉次保持 IN_OVEN） =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/THIN/unload" \
  -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:25:00"}' | pp

echo "== 4b. THIN 已离炉：再报读数按件拒收（炉次仍 IN_OVEN，THICK 不受影响） =="
curl -sS -X POST "$BASE/batches/$BID/readings" -H 'Content-Type: application/json' \
  -d '{"readings": [{"workpiece_id": "THIN", "ts": "2026-09-10T08:26:00",
                      "metal_temp_c": 180}]}' | pp || true

echo "== 5. THICK 普通请求：未达标被拒（409，给累计/剩余/阻塞原因） =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/THICK/unload" \
  -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:25:00"}' | pp || true

echo "== 6. 强制出炉不填原因：400；填原因后强制离炉，标记不合格并审计 =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/THICK/unload" \
  -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:25:00", "force": true}' | pp || true
curl -sS -X POST "$BASE/batches/$BID/workpieces/THICK/unload" \
  -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:25:00", "force": true, "reason": "后工序急件，线长确认放行返工"}' | pp

echo "== 7. 重复出炉 409；炉次已 UNLOADED，整炉再出炉同样 409 =="
curl -sS -X POST "$BASE/batches/$BID/workpieces/THIN/unload" \
  -H 'Content-Type: application/json' -d '{}' | pp || true

echo "== 8. 炉次已自动 UNLOADED（最后离炉 08:25）：档案与随炉卡含离炉顺序 =="
curl -sS "$BASE/batches/$BID" | pp
curl -sS "$BASE/batches/$BID/archive" -o "$OUT_DIR/batch_${BID}_archive.json"
echo "saved: $OUT_DIR/batch_${BID}_archive.json"
curl -sS "$BASE/batches/$BID/card" -o "$OUT_DIR/batch_${BID}_card.html"
echo "saved: $OUT_DIR/batch_${BID}_card.html （逐件离炉记录段含顺序/强制原因/快照）"
