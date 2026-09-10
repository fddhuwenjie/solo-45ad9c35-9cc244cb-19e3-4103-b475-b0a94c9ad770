#!/usr/bin/env bash
# 场景 B：冲突与异常闭环
#   禁配组拆炉 / 窗口无交集拆炉 / 超限工件给出原因 / 越序动作被状态机拒绝并打标记 /
#   欠时+超温+探头中断 → 返工 → 参数变更后重排形成关联版本（已签发炉次不变）→ 复测合格结案
# 用法：先启动服务（python3 run.py），再执行 bash samples/scenario_b_conflicts_rework.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:5000}/api"
pp() { python3 -m json.tool --no-ensure-ascii; }

echo "== 1. 试算 v1：禁配组 + 窗口无交集 + 超限工件 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_b1.json <<'JSON'
{
  "reason": "中班排产 v1",
  "start_at": "2026-09-10T08:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 4.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180, "hold_minutes": 15},
    {"batch_no": "P2", "temp_min_c": 185, "temp_max_c": 205, "hold_minutes": 30}
  ],
  "forbidden_pairs": [["GRP_A", "GRP_B"]],
  "orders": [
    {"workpiece_id": "W-2001", "order_id": "PO-11", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P1", "compat_group": "GRP_A",
     "due_at": "2026-09-10T12:00:00"},
    {"workpiece_id": "W-2002", "order_id": "PO-11", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P1", "compat_group": "GRP_B",
     "due_at": "2026-09-10T12:00:00"},
    {"workpiece_id": "W-2003", "order_id": "PO-12", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P2", "compat_group": "GRP_A",
     "due_at": "2026-09-10T13:00:00"},
    {"workpiece_id": "W-2004", "order_id": "PO-13", "length_mm": 4000, "width_mm": 400,
     "height_mm": 300, "weight_kg": 10, "powder_batch": "P1", "compat_group": null,
     "due_at": "2026-09-10T13:00:00"},
    {"workpiece_id": "W-2005", "order_id": "PO-13", "length_mm": 500, "width_mm": 400,
     "height_mm": 300, "weight_kg": 2000, "powder_batch": "P1", "compat_group": null,
     "due_at": "2026-09-10T13:00:00"}
  ]
}
JSON
pp < /tmp/trial_b1.json
echo ">> 预期：W-2001/W-2002 禁配拆炉，W-2003 窗口无交集单独一炉，"
echo ">>       W-2004 OVERSIZE、W-2005 OVERWEIGHT 进入 unscheduled 并给出原因"

B1=$(python3 -c "import json; print(json.load(open('/tmp/trial_b1.json'))['new_batches'][0]['batch_id'])")
B2=$(python3 -c "import json; print(json.load(open('/tmp/trial_b1.json'))['new_batches'][1]['batch_id'])")
B3=$(python3 -c "import json; print(json.load(open('/tmp/trial_b1.json'))['new_batches'][2]['batch_id'])")
echo ">> 草稿炉次: $B1 $B2 $B3"

echo "== 2. 越序：未签发直接出炉判定 → 409 且记录 UNISSUED_UNLOAD =="
curl -sS -X POST "$BASE/batches/$B2/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T09:00:00"}' | pp
curl -sS "$BASE/batches/$B2" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('W-2002 flags:', d['items'][0]['flags'])"

echo "== 3. 越序：未签发直接入炉 → 409 =="
curl -sS -X POST "$BASE/batches/$B3/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 4. 正常签发并入炉炉次 $B1（W-2001） =="
curl -sS -X POST "$BASE/batches/$B1/issue" | pp
curl -sS -X POST "$BASE/batches/$B1/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:00:00"}' | pp

echo "== 5. 回传异常测温：探头中断 30 分钟 + 超温 210℃ + 欠时 =="
curl -sS -X POST "$BASE/batches/$B1/readings" -H 'Content-Type: application/json' \
  --data @- <<'JSON' | pp
{"readings": [
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:00:00", "metal_temp_c": 25},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:05:00", "metal_temp_c": 150},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:35:00", "metal_temp_c": 170},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:40:00", "metal_temp_c": 210},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:45:00", "metal_temp_c": 172},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T08:50:00", "metal_temp_c": 60}
]}
JSON

echo "== 6. 出炉判定 → UNDER_TIME / OVER_TEMP / PROBE_GAP，工件转待返工 =="
curl -sS -X POST "$BASE/batches/$B1/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T08:55:00"}' | pp

echo "== 7. 炉次结案被拦（存在待返工工件）→ 先返工，再结案 =="
curl -sS -X POST "$BASE/batches/$B1/close" | pp
curl -sS -X POST "$BASE/workpieces/W-2001/rework" | pp
curl -sS -X POST "$BASE/batches/$B1/close" | pp

echo "== 8. 签发炉次 $B2（W-2002）——用于演示重排时已签发炉次保持不变 =="
curl -sS -X POST "$BASE/batches/$B2/issue" | pp

echo "== 9. 实测升温速率修正 4.0→3.0，重新试算形成关联版本 v2 =="
curl -sS -X POST "$BASE/schedule/trial" -H 'Content-Type: application/json' --data @- \
  -o /tmp/trial_b2.json <<'JSON'
{
  "reason": "实测升温速率修正 + 返工件重排",
  "start_at": "2026-09-10T10:00:00",
  "ovens": [{
    "id": "OVEN-1",
    "chamber_l_mm": 3000, "chamber_w_mm": 1200, "chamber_h_mm": 1500,
    "heat_rate_c_per_min": 3.0, "mass_factor_min_per_kg": 0.02,
    "ambient_c": 25, "turnaround_minutes": 15,
    "hanger_slots": 12, "hanger_spacing_mm": 500, "hanger_max_load_kg": 80
  }],
  "powders": [
    {"batch_no": "P1", "temp_min_c": 160, "temp_max_c": 180, "hold_minutes": 15},
    {"batch_no": "P2", "temp_min_c": 185, "temp_max_c": 205, "hold_minutes": 30}
  ],
  "forbidden_pairs": [["GRP_A", "GRP_B"]],
  "orders": []
}
JSON
pp < /tmp/trial_b2.json
echo ">> 预期：v2.parent_id = v1；carried_batches 含已签发的 $B2（原样保留）；"
echo ">>       旧草稿 $B3 作废，W-2001(返工)/W-2003 重新编排，且避让 $B2 的炉膛占用"

RB=$(python3 -c "
import json
d = json.load(open('/tmp/trial_b2.json'))
for b in d['new_batches']:
    if any(i['workpiece_id'] == 'W-2001' for i in b['items']):
        print(b['batch_id']); break
")
echo ">> 返工件 W-2001 所在新炉次: $RB"

echo "== 10. 返工炉次全流程：签发 → 入炉 → 合格测温 → 出炉判定 OK =="
curl -sS -X POST "$BASE/batches/$RB/issue" | pp
curl -sS -X POST "$BASE/batches/$RB/load" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T10:15:00"}' | pp
curl -sS -X POST "$BASE/batches/$RB/readings" -H 'Content-Type: application/json' \
  --data @- <<'JSON' | pp
{"readings": [
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:15:00", "metal_temp_c": 25},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:20:00", "metal_temp_c": 140},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:25:00", "metal_temp_c": 162},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:30:00", "metal_temp_c": 168},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:35:00", "metal_temp_c": 170},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:40:00", "metal_temp_c": 169},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:45:00", "metal_temp_c": 171},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:50:00", "metal_temp_c": 168},
  {"workpiece_id": "W-2001", "ts": "2026-09-10T10:55:00", "metal_temp_c": 80}
]}
JSON
curl -sS -X POST "$BASE/batches/$RB/unload" -H 'Content-Type: application/json' \
  -d '{"at": "2026-09-10T10:55:00"}' | pp

echo "== 11. 结案 + 版本链 =="
curl -sS -X POST "$BASE/workpieces/W-2001/close" -H 'Content-Type: application/json' \
  -d '{"note": "返工后复测合格"}' | pp
curl -sS -X POST "$BASE/batches/$RB/close" | pp
curl -sS "$BASE/versions" | pp
