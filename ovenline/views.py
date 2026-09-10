"""REST API：试算 / 签发 / 入炉 / 测温回传 / 出炉判定 / 返工结案 / 查询下载。

多探头：工件可登记多个金属探头及校准偏移，签发时冻结配置；
测温按 (炉次, 工件, 探头, 时刻) 幂等去重；判定序列取每个采样时刻
有效探头校正温度的最低值；故障探头可在出炉前停用并重算该工件。
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from . import probes, scheduler
from .db import get_db

bp = Blueprint("api", __name__)

# 炉次状态机：动作 -> (允许的前置状态, 目标状态)，越序动作一律 409 拒绝
TRANSITIONS = {
    "issue": (["DRAFT"], "ISSUED"),
    "load": (["ISSUED"], "IN_OVEN"),
    "unload": (["IN_OVEN"], "UNLOADED"),
    "close": (["UNLOADED"], "CLOSED"),
}

# 工件异常标记
FLAG_UNDER_TIME = "UNDER_TIME"              # 欠时：许可区间累计分钟数不足
FLAG_OVER_TEMP = "OVER_TEMP"                # 超温：金属温度超过粉料上限
FLAG_PROBE_GAP = "PROBE_GAP"                # 探头中断：启用探头缺报超阈值
FLAG_STUCK_PROBE = "STUCK_PROBE"            # 探头卡值：连续相同读数超阈值
FLAG_PROBE_DIVERGENCE = "PROBE_DIVERGENCE"  # 探头温差：有效探头间温差超阈值
FLAG_INSUFFICIENT_PROBES = "INSUFFICIENT_PROBES"  # 有效探头数不足，不得判定合格
FLAG_UNISSUED_UNLOAD = "UNISSUED_UNLOAD"    # 未签发出炉
FLAG_INCOMPAT = "INCOMPAT_CONFLICT"         # 禁配冲突


# ---------------------------------------------------------------- 工具

def _now():
    return datetime.now().replace(microsecond=0)


def _parse_dt(value, field):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"字段 {field} 不是合法 ISO 时间: {value!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _err(status, message, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def _fetch_batch(db, bid):
    return db.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()


def _add_flag(db, batch_id, workpiece_id, code, detail):
    db.execute(
        "INSERT OR IGNORE INTO flags (batch_id, workpiece_id, code, detail, created_at)"
        " VALUES (?,?,?,?,?)",
        (batch_id, workpiece_id, code, detail, _now().isoformat()),
    )


def _probe_cfg_for_item(db, batch_id, workpiece_id, frozen):
    """工件在该炉次中的探头配置。

    frozen=True（已签发及以后）取签发时冻结的快照（即使为空也不再回退主数据）；
    frozen=False（草稿/被取代）取当前登记的探头主数据。
    """
    if frozen:
        rows = db.execute(
            "SELECT probe_id, offset_c, status, disabled_reason, disabled_at"
            " FROM batch_item_probes WHERE batch_id=? AND workpiece_id=?"
            " ORDER BY probe_id", (batch_id, workpiece_id)).fetchall()
    else:
        rows = db.execute(
            "SELECT probe_id, offset_c, 'ACTIVE' AS status,"
            " NULL AS disabled_reason, NULL AS disabled_at"
            " FROM probes WHERE workpiece_id=? ORDER BY probe_id",
            (workpiece_id,)).fetchall()
    return {r["probe_id"]: dict(r) for r in rows}


def _cure_for_item(db, b, workpiece_id):
    """按工件固化窗口评估。已签发炉次用签发快照，草稿用当前主数据。

    多探头：校正温度 = 原始值 + 冻结的校准偏移；判定序列取每个采样时刻
    有效探头校正温度的最低值，据此累计固化窗口分钟。
    """
    bid = b["id"]
    row = db.execute(
        "SELECT COALESCE(bi.snap_temp_min_c, p.temp_min_c) AS temp_min_c,"
        " COALESCE(bi.snap_temp_max_c, p.temp_max_c) AS temp_max_c,"
        " COALESCE(bi.snap_hold_minutes, p.hold_minutes) AS hold_minutes"
        " FROM batch_items bi"
        " JOIN workpieces w ON w.id = bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE bi.batch_id=? AND bi.workpiece_id=?",
        (bid, workpiece_id),
    ).fetchone()
    rows = db.execute(
        "SELECT ts, probe_id, metal_temp_c FROM readings"
        " WHERE batch_id=? AND workpiece_id=? ORDER BY ts, id",
        (bid, workpiece_id),
    ).fetchall()
    cfg = _probe_cfg_for_item(db, bid, workpiece_id,
                              frozen=b["state"] not in ("DRAFT", "SUPERSEDED"))
    pts = [(datetime.fromisoformat(r["ts"]), r["probe_id"], r["metal_temp_c"])
           for r in rows]
    result = probes.analyze(
        pts,
        {pid: {"offset_c": c["offset_c"], "status": c["status"]}
         for pid, c in cfg.items()},
        row["temp_min_c"],
        row["temp_max_c"],
        row["hold_minutes"],
        current_app.config["PROBE_GAP_MINUTES"],
        current_app.config["PROBE_DIVERGENCE_C"],
        current_app.config["STUCK_PROBE_MIN_CONSECUTIVE"],
        current_app.config["MIN_VALID_PROBES"],
    )
    # 补充停用原因/时刻（快照中的处置信息）
    for p in result["probes"]:
        c = cfg.get(p["probe_id"]) or {}
        p["disabled_reason"] = c.get("disabled_reason")
        p["disabled_at"] = c.get("disabled_at")
    return result


def _cure_summary(c):
    """判定摘要：探头处置审计记录重算前后的关键结果变化。"""
    stuck = sum(len(p["anomalies"]) for p in c["probes"]
                if p["status"] == "ACTIVE")
    return {
        "in_window_minutes": c["in_window_minutes"],
        "required_hold_minutes": c["required_hold_minutes"],
        "under_time": c["under_time"],
        "over_temp": c["over_temp"],
        "max_temp_c": c["max_temp_c"],
        "valid_probe_count": c["valid_probe_count"],
        "insufficient_probes": c["insufficient_probes"],
        "stuck_intervals": stuck,
        "divergences": len(c["divergences"]),
        "probe_gaps": len(c["probe_gaps"]),
    }


def _batch_payload(db, b):
    items = db.execute(
        "SELECT bi.workpiece_id, bi.hanger_slot, bi.slots_used, w.order_id,"
        " COALESCE(bi.snap_length_mm, w.length_mm) AS length_mm,"
        " COALESCE(bi.snap_width_mm, w.width_mm) AS width_mm,"
        " COALESCE(bi.snap_height_mm, w.height_mm) AS height_mm,"
        " COALESCE(bi.snap_weight_kg, w.weight_kg) AS weight_kg,"
        " COALESCE(bi.snap_powder_batch, w.powder_batch) AS powder_batch,"
        " w.compat_group, w.due_at, w.is_rework, w.status"
        " FROM batch_items bi JOIN workpieces w ON w.id = bi.workpiece_id"
        " WHERE bi.batch_id=? ORDER BY bi.hanger_slot",
        (b["id"],),
    ).fetchall()
    out_items = []
    for it in items:
        wid = it["workpiece_id"]
        flags = [
            {"code": f["code"], "detail": f["detail"], "created_at": f["created_at"]}
            for f in db.execute(
                "SELECT code, detail, created_at FROM flags"
                " WHERE batch_id=? AND workpiece_id=? ORDER BY id",
                (b["id"], wid),
            )
        ]
        probe_actions = [
            {"probe_id": a["probe_id"], "action": a["action"],
             "reason": a["reason"],
             "before": json.loads(a["before_json"]) if a["before_json"] else None,
             "after": json.loads(a["after_json"]) if a["after_json"] else None,
             "created_at": a["created_at"]}
            for a in db.execute(
                "SELECT probe_id, action, reason, before_json, after_json,"
                " created_at FROM probe_actions"
                " WHERE batch_id=? AND workpiece_id=? ORDER BY id",
                (b["id"], wid),
            )
        ]
        out_items.append({
            **dict(it),
            "is_rework": bool(it["is_rework"]),
            "cure": _cure_for_item(db, b, wid),
            "flags": flags,
            "probe_actions": probe_actions,
        })
    return {
        "batch_id": b["id"],
        "version_id": b["version_id"],
        "oven_id": b["oven_id"],
        "state": b["state"],
        "window": {
            "min_c": b["window_min_c"],
            "max_c": b["window_max_c"],
            "hold_minutes": b["hold_minutes"],
        },
        "total_weight_kg": b["total_weight_kg"],
        "heatup_minutes": b["heatup_minutes"],
        "planned": {
            "load_at": b["planned_load_at"],
            "cure_start_at": b["planned_cure_start_at"],
            "unload_at": b["planned_unload_at"],
        },
        "actual": {"load_at": b["actual_load_at"], "unload_at": b["actual_unload_at"]},
        "created_at": b["created_at"],
        "items": out_items,
    }


# ---------------------------------------------------------------- 试算

@bp.post("/schedule/trial")
def trial():
    """试算：编排炉次、挂位、升温/保温/出炉时刻。

    每次试算生成一个关联版本（parent_id 指向上版本）：
    已签发/在炉炉次原样保留，旧草稿作废，待排产工件（含返工件）重新编排。
    """
    data = request.get_json(silent=True) or {}
    for key in ("ovens", "powders", "orders"):
        if key not in data:
            return _err(400, f"缺少字段: {key}")
    try:
        start_at = _parse_dt(data["start_at"], "start_at") if data.get("start_at") else _now()
        forbidden = set()
        for pair in data.get("forbidden_pairs", []):
            if len(pair) != 2:
                return _err(400, "forbidden_pairs 元素须为 [组A, 组B]")
            forbidden.add((pair[0], pair[1]))
    except ValueError as e:
        return _err(400, str(e))

    db = get_db()

    # 炉膛主数据 upsert（本次试算按请求中的炉膛集合编排）
    oven_rows = []
    for ov in data["ovens"]:
        missing = [k for k in ("id", "chamber_l_mm", "chamber_w_mm", "chamber_h_mm",
                               "heat_rate_c_per_min", "hanger_slots",
                               "hanger_spacing_mm", "hanger_max_load_kg") if k not in ov]
        if missing:
            return _err(400, f"炉膛参数缺少字段: {missing}")
        row = {
            "id": ov["id"],
            "chamber_l_mm": float(ov["chamber_l_mm"]),
            "chamber_w_mm": float(ov["chamber_w_mm"]),
            "chamber_h_mm": float(ov["chamber_h_mm"]),
            "heat_rate_c_per_min": float(ov["heat_rate_c_per_min"]),
            "mass_factor_min_per_kg": float(ov.get("mass_factor_min_per_kg", 0)),
            "ambient_c": float(ov.get("ambient_c", 25)),
            "turnaround_minutes": float(ov.get("turnaround_minutes", 15)),
            "hanger_slots": int(ov["hanger_slots"]),
            "hanger_spacing_mm": float(ov["hanger_spacing_mm"]),
            "hanger_max_load_kg": float(ov["hanger_max_load_kg"]),
        }
        db.execute(
            "INSERT INTO ovens (id, chamber_l_mm, chamber_w_mm, chamber_h_mm,"
            " heat_rate_c_per_min, mass_factor_min_per_kg, ambient_c,"
            " turnaround_minutes, hanger_slots, hanger_spacing_mm, hanger_max_load_kg)"
            " VALUES (:id, :chamber_l_mm, :chamber_w_mm, :chamber_h_mm,"
            " :heat_rate_c_per_min, :mass_factor_min_per_kg, :ambient_c,"
            " :turnaround_minutes, :hanger_slots, :hanger_spacing_mm, :hanger_max_load_kg)"
            " ON CONFLICT(id) DO UPDATE SET"
            " chamber_l_mm=excluded.chamber_l_mm, chamber_w_mm=excluded.chamber_w_mm,"
            " chamber_h_mm=excluded.chamber_h_mm,"
            " heat_rate_c_per_min=excluded.heat_rate_c_per_min,"
            " mass_factor_min_per_kg=excluded.mass_factor_min_per_kg,"
            " ambient_c=excluded.ambient_c, turnaround_minutes=excluded.turnaround_minutes,"
            " hanger_slots=excluded.hanger_slots,"
            " hanger_spacing_mm=excluded.hanger_spacing_mm,"
            " hanger_max_load_kg=excluded.hanger_max_load_kg",
            row,
        )
        oven_rows.append(row)

    # 粉料主数据 upsert
    powder_req = []
    for p in data["powders"]:
        missing = [k for k in ("batch_no", "temp_min_c", "temp_max_c", "hold_minutes")
                   if k not in p]
        if missing:
            return _err(400, f"粉料参数缺少字段: {missing}")
        if float(p["temp_min_c"]) > float(p["temp_max_c"]):
            return _err(400, f"粉料 {p['batch_no']} 温度下限高于上限")
        row = {"batch_no": p["batch_no"], "temp_min_c": float(p["temp_min_c"]),
               "temp_max_c": float(p["temp_max_c"]), "hold_minutes": float(p["hold_minutes"])}
        db.execute(
            "INSERT INTO powders (batch_no, temp_min_c, temp_max_c, hold_minutes)"
            " VALUES (:batch_no, :temp_min_c, :temp_max_c, :hold_minutes)"
            " ON CONFLICT(batch_no) DO UPDATE SET"
            " temp_min_c=excluded.temp_min_c, temp_max_c=excluded.temp_max_c,"
            " hold_minutes=excluded.hold_minutes",
            row,
        )
        powder_req.append(row)

    # 订单/工件 upsert（已存在的非待排产工件保持原状态，不会被重排）
    # 未登记粉料的订单不入库，直接进 unscheduled 并给出 UNKNOWN_POWDER 原因
    known_powders = {r["batch_no"] for r in db.execute("SELECT batch_no FROM powders")}
    pre_unscheduled = []
    try:
        for o in data["orders"]:
            missing = [k for k in ("workpiece_id", "length_mm", "width_mm", "height_mm",
                                   "weight_kg", "powder_batch") if k not in o]
            if missing:
                return _err(400, f"订单缺少字段: {missing}"
                                 f" (workpiece_id={o.get('workpiece_id')})")
            if o["powder_batch"] not in known_powders:
                pre_unscheduled.append({
                    "workpiece_id": o["workpiece_id"],
                    "reason": scheduler.REASON_UNKNOWN_POWDER,
                    "detail": f"粉料批号 {o['powder_batch']} 未登记",
                })
                continue
            due = _parse_dt(o["due_at"], "due_at").isoformat() if o.get("due_at") else None
            db.execute(
                "INSERT INTO workpieces (id, order_id, length_mm, width_mm, height_mm,"
                " weight_kg, powder_batch, compat_group, due_at, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,'PENDING')"
                " ON CONFLICT(id) DO UPDATE SET"
                " order_id=excluded.order_id, length_mm=excluded.length_mm,"
                " width_mm=excluded.width_mm, height_mm=excluded.height_mm,"
                " weight_kg=excluded.weight_kg, powder_batch=excluded.powder_batch,"
                " compat_group=excluded.compat_group, due_at=excluded.due_at",
                (o["workpiece_id"], o.get("order_id"), float(o["length_mm"]),
                 float(o["width_mm"]), float(o["height_mm"]), float(o["weight_kg"]),
                 o["powder_batch"], o.get("compat_group"), due),
            )
    except ValueError as e:
        return _err(400, str(e))

    # 旧草稿作废，其工件回到待排产
    for d in db.execute("SELECT id FROM batches WHERE state='DRAFT'").fetchall():
        db.execute("UPDATE batches SET state='SUPERSEDED' WHERE id=?", (d["id"],))
        db.execute(
            "UPDATE workpieces SET status='PENDING' WHERE status='SCHEDULED'"
            " AND id IN (SELECT workpiece_id FROM batch_items WHERE batch_id=?)",
            (d["id"],),
        )

    # 关联版本
    parent = db.execute(
        "SELECT id FROM schedule_versions ORDER BY id DESC LIMIT 1").fetchone()
    snapshot = {
        "reason": data.get("reason", ""),
        "start_at": start_at.isoformat(),
        "forbidden_pairs": sorted(list(p) for p in forbidden),
        "ovens": oven_rows,
        "powders": powder_req,
    }
    cur = db.execute(
        "INSERT INTO schedule_versions (parent_id, reason, params_json, created_at)"
        " VALUES (?,?,?,?)",
        (parent["id"] if parent else None, data.get("reason", ""),
         json.dumps(snapshot, ensure_ascii=False), _now().isoformat()),
    )
    version_id = cur.lastrowid

    # 已签发/在炉炉次占用炉膛到计划出炉+周转，编排时避让
    busy_until = {}
    for c in db.execute(
            "SELECT oven_id, planned_unload_at FROM batches"
            " WHERE state IN ('ISSUED','IN_OVEN')").fetchall():
        until = _parse_dt(c["planned_unload_at"], "planned_unload_at")
        ov = db.execute("SELECT turnaround_minutes FROM ovens WHERE id=?",
                        (c["oven_id"],)).fetchone()
        if ov:
            until = until + timedelta(minutes=ov["turnaround_minutes"])
        if c["oven_id"] not in busy_until or until > busy_until[c["oven_id"]]:
            busy_until[c["oven_id"]] = until

    powder_all = {r["batch_no"]: dict(r)
                  for r in db.execute("SELECT * FROM powders").fetchall()}
    # 本次提交因未登记粉料被拒的工件：不得按库内残留旧粉料参与本次编排
    rejected_ids = {u["workpiece_id"] for u in pre_unscheduled}
    pending = [dict(r) for r in db.execute(
        "SELECT * FROM workpieces WHERE status='PENDING' ORDER BY id").fetchall()
        if r["id"] not in rejected_ids]
    planned, unscheduled = scheduler.build_plan(
        pending, powder_all, oven_rows, forbidden, start_at, busy_until)

    new_batches = []
    for b in planned:
        cur = db.execute(
            "INSERT INTO batches (version_id, oven_id, state, window_min_c, window_max_c,"
            " hold_minutes, total_weight_kg, heatup_minutes, planned_load_at,"
            " planned_cure_start_at, planned_unload_at, created_at)"
            " VALUES (?,?,'DRAFT',?,?,?,?,?,?,?,?,?)",
            (version_id, b["oven_id"], b["window_min_c"], b["window_max_c"],
             b["hold_minutes"], b["total_weight_kg"], b["heatup_minutes"],
             b["planned_load_at"], b["planned_cure_start_at"], b["planned_unload_at"],
             _now().isoformat()),
        )
        bid = cur.lastrowid
        for it in b["items"]:
            db.execute(
                "INSERT INTO batch_items (batch_id, workpiece_id, hanger_slot, slots_used)"
                " VALUES (?,?,?,?)",
                (bid, it["workpiece_id"], it["hanger_slot"], it["slots_used"]),
            )
            db.execute("UPDATE workpieces SET status='SCHEDULED' WHERE id=?",
                       (it["workpiece_id"],))
        new_batches.append({"batch_id": bid, "state": "DRAFT", **b})
    db.commit()

    carried = db.execute(
        "SELECT id, oven_id, state, planned_load_at, planned_unload_at FROM batches"
        " WHERE state IN ('ISSUED','IN_OVEN') ORDER BY id").fetchall()
    return jsonify({
        "version": {"id": version_id, "parent_id": parent["id"] if parent else None,
                    "reason": data.get("reason", "")},
        "carried_batches": [dict(c) for c in carried],
        "new_batches": new_batches,
        "unscheduled": pre_unscheduled + unscheduled,
    }), 201


# ---------------------------------------------------------------- 状态机动作

@bp.post("/batches/<int:bid>/issue")
def issue(bid):
    """签发：DRAFT -> ISSUED，签发后炉次冻结，不再参与重排。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] not in TRANSITIONS["issue"][0]:
        return _err(409, f"炉次状态为 {b['state']}，不能签发（要求 DRAFT）", state=b["state"])
    # 签发时快照工件尺寸/重量与粉料固化窗口，此后主数据变更不影响本炉次
    rows = db.execute(
        "SELECT bi.workpiece_id, w.length_mm, w.width_mm, w.height_mm, w.weight_kg,"
        " w.powder_batch, p.temp_min_c, p.temp_max_c, p.hold_minutes"
        " FROM batch_items bi"
        " JOIN workpieces w ON w.id = bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE bi.batch_id=?", (bid,)).fetchall()
    for r in rows:
        db.execute(
            "UPDATE batch_items SET snap_length_mm=?, snap_width_mm=?, snap_height_mm=?,"
            " snap_weight_kg=?, snap_powder_batch=?, snap_temp_min_c=?, snap_temp_max_c=?,"
            " snap_hold_minutes=? WHERE batch_id=? AND workpiece_id=?",
            (r["length_mm"], r["width_mm"], r["height_mm"], r["weight_kg"],
             r["powder_batch"], r["temp_min_c"], r["temp_max_c"], r["hold_minutes"],
             bid, r["workpiece_id"]))
    # 签发时冻结探头配置（编号 + 校准偏移），此后主数据变更不影响本炉次
    for r in rows:
        for pr in db.execute(
                "SELECT probe_id, offset_c FROM probes WHERE workpiece_id=?"
                " ORDER BY probe_id", (r["workpiece_id"],)).fetchall():
            db.execute(
                "INSERT OR IGNORE INTO batch_item_probes"
                " (batch_id, workpiece_id, probe_id, offset_c) VALUES (?,?,?,?)",
                (bid, r["workpiece_id"], pr["probe_id"], pr["offset_c"]))
    db.execute("UPDATE batches SET state='ISSUED' WHERE id=?", (bid,))
    db.commit()
    return jsonify({"batch_id": bid, "state": "ISSUED"})


@bp.post("/batches/<int:bid>/load")
def load(bid):
    """入炉：ISSUED -> IN_OVEN，记录实际入炉时刻。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] not in TRANSITIONS["load"][0]:
        return _err(409, f"炉次状态为 {b['state']}，不能入炉（要求 ISSUED）", state=b["state"])
    data = request.get_json(silent=True) or {}
    try:
        at = _parse_dt(data["at"], "at") if data.get("at") else _now()
    except ValueError as e:
        return _err(400, str(e))
    db.execute("UPDATE batches SET state='IN_OVEN', actual_load_at=? WHERE id=?",
               (at.isoformat(), bid))
    db.execute(
        "UPDATE workpieces SET status='IN_OVEN' WHERE id IN"
        " (SELECT workpiece_id FROM batch_items WHERE batch_id=?)", (bid,))
    db.commit()
    return jsonify({"batch_id": bid, "state": "IN_OVEN",
                    "actual_load_at": at.isoformat()})


@bp.post("/batches/<int:bid>/readings")
def add_readings(bid):
    """测温回传：仅在炉（IN_OVEN）状态接收金属探头温度。

    已绑定探头的工件必须携带 probe_id；按 (炉次, 工件, 探头, 时刻) 幂等去重；
    未绑定、已停用或早于实际入炉时刻的读数一律拒收。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] != "IN_OVEN":
        return _err(409, f"炉次状态为 {b['state']}，不能回传测温（要求 IN_OVEN）",
                    state=b["state"])
    data = request.get_json(silent=True) or {}
    entries = data.get("readings")
    if entries is None and "workpiece_id" in data:
        entries = [data]
    if not entries:
        return _err(400, "缺少 readings（或单条 workpiece_id/ts/metal_temp_c）")
    valid = {r["workpiece_id"] for r in db.execute(
        "SELECT workpiece_id FROM batch_items WHERE batch_id=?", (bid,)).fetchall()}
    # 签发时冻结的探头配置：{工件: {探头: 状态}}
    probe_cfg = {}
    for r in db.execute(
            "SELECT workpiece_id, probe_id, status FROM batch_item_probes"
            " WHERE batch_id=?", (bid,)).fetchall():
        probe_cfg.setdefault(r["workpiece_id"], {})[r["probe_id"]] = r["status"]
    load_at = (datetime.fromisoformat(b["actual_load_at"])
               if b["actual_load_at"] else None)
    accepted, duplicates, rejected = 0, 0, []
    for e in entries:
        wid = e.get("workpiece_id")
        pid = e.get("probe_id")
        if wid not in valid:
            rejected.append({"workpiece_id": wid, "probe_id": pid,
                             "reason": "工件不在该炉次"})
            continue
        try:
            ts = _parse_dt(e["ts"], "ts")
            temp = float(e["metal_temp_c"])
        except (KeyError, TypeError, ValueError) as ex:
            rejected.append({"workpiece_id": wid, "probe_id": pid,
                             "reason": f"测温记录无效: {ex}"})
            continue
        bound = probe_cfg.get(wid, {})
        if bound:
            if pid is None:
                rejected.append({"workpiece_id": wid, "probe_id": pid,
                                 "reason": "该工件已绑定探头，测温须携带 probe_id"})
                continue
            if pid not in bound:
                rejected.append({"workpiece_id": wid, "probe_id": pid,
                                 "reason": f"探头 {pid} 未绑定工件 {wid}"})
                continue
            if bound[pid] != "ACTIVE":
                rejected.append({"workpiece_id": wid, "probe_id": pid,
                                 "reason": f"探头 {pid} 已停用，读数不予采信"})
                continue
        elif pid is not None:
            rejected.append({"workpiece_id": wid, "probe_id": pid,
                             "reason": f"探头 {pid} 未绑定工件 {wid}"})
            continue
        if load_at is not None and ts < load_at:
            rejected.append({"workpiece_id": wid, "probe_id": pid,
                             "reason": f"测温时刻早于实际入炉时刻 {b['actual_load_at']}，"
                                       "不予采信"})
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO readings"
            " (batch_id, workpiece_id, probe_id, ts, metal_temp_c)"
            " VALUES (?,?,?,?,?)", (bid, wid, pid, ts.isoformat(), temp))
        if cur.rowcount:
            accepted += 1
        else:
            duplicates += 1  # 同 (炉次, 工件, 探头, 时刻) 重复回传，幂等忽略
    db.commit()
    return jsonify({"batch_id": bid, "accepted": accepted,
                    "duplicates": duplicates, "rejected": rejected})


@bp.post("/batches/<int:bid>/unload")
def unload(bid):
    """出炉判定：IN_OVEN -> UNLOADED，逐件评估并打标记。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] == "DRAFT":
        # 未签发出炉：记录违规标记后拒绝
        items = db.execute("SELECT workpiece_id FROM batch_items WHERE batch_id=?",
                           (bid,)).fetchall()
        for it in items:
            _add_flag(db, bid, it["workpiece_id"], FLAG_UNISSUED_UNLOAD,
                      "炉次未签发即请求出炉判定")
        db.commit()
        return _err(409, "炉次未签发，禁止出炉；已为炉内工件记录 UNISSUED_UNLOAD 标记",
                    state=b["state"])
    if b["state"] not in TRANSITIONS["unload"][0]:
        return _err(409, f"炉次状态为 {b['state']}，不能出炉判定（要求 IN_OVEN）",
                    state=b["state"])
    data = request.get_json(silent=True) or {}
    try:
        at = _parse_dt(data["at"], "at") if data.get("at") else _now()
    except ValueError as e:
        return _err(400, str(e))

    # 禁配冲突检查（按该炉次所属版本的禁配组判定）
    version = db.execute("SELECT params_json FROM schedule_versions WHERE id=?",
                         (b["version_id"],)).fetchone()
    forbidden = set()
    if version:
        for pair in json.loads(version["params_json"]).get("forbidden_pairs", []):
            forbidden.add((pair[0], pair[1]))
    items = db.execute(
        "SELECT bi.workpiece_id, w.compat_group FROM batch_items bi"
        " JOIN workpieces w ON w.id = bi.workpiece_id WHERE bi.batch_id=?",
        (bid,)).fetchall()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            ga, gb = items[i]["compat_group"], items[j]["compat_group"]
            if ga and gb and ((ga, gb) in forbidden or (gb, ga) in forbidden):
                _add_flag(db, bid, items[i]["workpiece_id"], FLAG_INCOMPAT,
                          f"与 {items[j]['workpiece_id']} 属同炉禁配组 {ga}/{gb}")
                _add_flag(db, bid, items[j]["workpiece_id"], FLAG_INCOMPAT,
                          f"与 {items[i]['workpiece_id']} 属同炉禁配组 {ga}/{gb}")

    results = []
    for it in items:
        wid = it["workpiece_id"]
        c = _cure_for_item(db, b, wid)
        if c["under_time"]:
            _add_flag(db, bid, wid, FLAG_UNDER_TIME,
                      f"许可区间累计 {c['in_window_minutes']} 分钟，"
                      f"不足要求的 {c['required_hold_minutes']} 分钟")
        if c["over_temp"]:
            _add_flag(db, bid, wid, FLAG_OVER_TEMP,
                      f"金属温度最高 {c['max_temp_c']}℃，超过粉料上限")
        if c["probe_gaps"]:
            _add_flag(db, bid, wid, FLAG_PROBE_GAP,
                      "探头缺报: " + json.dumps(c["probe_gaps"], ensure_ascii=False))
        stuck = [a for p in c["probes"] if p["status"] == "ACTIVE"
                 for a in p["anomalies"]]
        if stuck:
            _add_flag(db, bid, wid, FLAG_STUCK_PROBE,
                      "探头卡值: " + json.dumps(stuck, ensure_ascii=False))
        if c["divergences"]:
            _add_flag(db, bid, wid, FLAG_PROBE_DIVERGENCE,
                      "探头温差: " + json.dumps(c["divergences"], ensure_ascii=False))
        if c["insufficient_probes"]:
            _add_flag(db, bid, wid, FLAG_INSUFFICIENT_PROBES,
                      f"有效探头 {c['valid_probe_count']} 个，"
                      f"少于设定的 {c['min_valid_probes']} 个，不得判定合格")
        codes = [r["code"] for r in db.execute(
            "SELECT code FROM flags WHERE batch_id=? AND workpiece_id=? ORDER BY id",
            (bid, wid)).fetchall()]
        ok = not codes
        db.execute("UPDATE workpieces SET status=? WHERE id=?",
                   ("DONE" if ok else "REWORK_PENDING", wid))
        results.append({"workpiece_id": wid, "verdict": "OK" if ok else "NOT_OK",
                        "flags": codes, "cure": c})
    db.execute("UPDATE batches SET state='UNLOADED', actual_unload_at=? WHERE id=?",
               (at.isoformat(), bid))
    db.commit()
    return jsonify({"batch_id": bid, "state": "UNLOADED",
                    "actual_unload_at": at.isoformat(), "results": results})


@bp.post("/batches/<int:bid>/close")
def close_batch(bid):
    """炉次结案：UNLOADED -> CLOSED，要求炉内工件均已了结。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] not in TRANSITIONS["close"][0]:
        return _err(409, f"炉次状态为 {b['state']}，不能结案（要求 UNLOADED）",
                    state=b["state"])
    blockers = db.execute(
        "SELECT w.id, w.status FROM batch_items bi JOIN workpieces w ON w.id=bi.workpiece_id"
        " WHERE bi.batch_id=? AND w.status NOT IN ('DONE','CLOSED')"
        " AND NOT (w.status='PENDING' AND w.is_rework=1)", (bid,)).fetchall()
    if blockers:
        return _err(409, "存在未了结工件，不能结案",
                    blockers=[{"workpiece_id": r["id"], "status": r["status"]}
                              for r in blockers])
    db.execute("UPDATE batches SET state='CLOSED' WHERE id=?", (bid,))
    db.commit()
    return jsonify({"batch_id": bid, "state": "CLOSED"})


# ---------------------------------------------------------------- 探头登记与处置

@bp.post("/workpieces/<wid>/probes")
def register_probes(wid):
    """登记/更新工件探头及校准偏移；签发时随炉次冻结快照，此后变更只影响新炉次。"""
    db = get_db()
    w = db.execute("SELECT id FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    data = request.get_json(silent=True) or {}
    entries = data.get("probes")
    if entries is None and "probe_id" in data:
        entries = [data]
    if not entries:
        return _err(400, "缺少 probes（或单条 probe_id/offset_c）")
    seen = set()
    for e in entries:
        pid = str(e.get("probe_id") or "").strip()
        if not pid:
            return _err(400, "probe_id 不能为空")
        if pid in seen:
            return _err(400, f"请求中探头 {pid} 重复")
        seen.add(pid)
        try:
            offset = float(e.get("offset_c", 0))
        except (TypeError, ValueError):
            return _err(400, f"探头 {pid} 的 offset_c 不是数字: {e.get('offset_c')!r}")
        db.execute(
            "INSERT INTO probes (workpiece_id, probe_id, offset_c, created_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(workpiece_id, probe_id) DO UPDATE SET"
            " offset_c=excluded.offset_c",
            (wid, pid, offset, _now().isoformat()))
    db.commit()
    return jsonify({"workpiece_id": wid, "probes": _probes_of(db, wid)}), 201


@bp.get("/workpieces/<wid>/probes")
def list_probes(wid):
    """工件已登记的探头（主数据，不含各炉次冻结快照）。"""
    db = get_db()
    w = db.execute("SELECT id FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    return jsonify({"workpiece_id": wid, "probes": _probes_of(db, wid)})


def _probes_of(db, wid):
    return [dict(r) for r in db.execute(
        "SELECT probe_id, offset_c, created_at FROM probes"
        " WHERE workpiece_id=? ORDER BY probe_id", (wid,)).fetchall()]


@bp.post("/batches/<int:bid>/workpieces/<wid>/probes/<pid>/disable")
def disable_probe(bid, wid, pid):
    """出炉前停用故障探头（须填写原因）：只重算该工件并记录结果变化。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] not in ("ISSUED", "IN_OVEN"):
        return _err(409, f"炉次状态为 {b['state']}，不能停用探头（要求出炉前）",
                    state=b["state"])
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return _err(400, "停用探头必须填写原因 reason")
    row = db.execute(
        "SELECT status, disabled_reason FROM batch_item_probes"
        " WHERE batch_id=? AND workpiece_id=? AND probe_id=?",
        (bid, wid, pid)).fetchone()
    if row is None:
        return _err(404, f"探头 {pid} 未绑定炉次 {bid} 中的工件 {wid}")
    if row["status"] == "DISABLED":
        return _err(409, f"探头 {pid} 已停用", probe_status="DISABLED",
                    disabled_reason=row["disabled_reason"])

    before = _cure_for_item(db, b, wid)
    db.execute(
        "UPDATE batch_item_probes SET status='DISABLED', disabled_reason=?,"
        " disabled_at=? WHERE batch_id=? AND workpiece_id=? AND probe_id=?",
        (reason, _now().isoformat(), bid, wid, pid))
    after = _cure_for_item(db, b, wid)  # 只重算该工件
    db.execute(
        "INSERT INTO probe_actions (batch_id, workpiece_id, probe_id, action,"
        " reason, before_json, after_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (bid, wid, pid, "DISABLE", reason,
         json.dumps(_cure_summary(before), ensure_ascii=False),
         json.dumps(_cure_summary(after), ensure_ascii=False),
         _now().isoformat()))
    db.commit()
    return jsonify({
        "batch_id": bid, "workpiece_id": wid, "probe_id": pid,
        "status": "DISABLED", "reason": reason,
        "recalc": {"before": _cure_summary(before),
                   "after": _cure_summary(after)},
        "cure": after,
    })


# ---------------------------------------------------------------- 返工与结案

@bp.post("/workpieces/<wid>/rework")
def rework(wid):
    """返工：判定不合格工件回到待排产队列，下次试算重新编排。"""
    db = get_db()
    w = db.execute("SELECT * FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    if w["status"] != "REWORK_PENDING":
        return _err(409, f"工件状态为 {w['status']}，不能返工（要求 REWORK_PENDING）",
                    status=w["status"])
    db.execute("UPDATE workpieces SET status='PENDING', is_rework=1 WHERE id=?", (wid,))
    db.commit()
    return jsonify({"workpiece_id": wid, "status": "PENDING", "is_rework": True,
                    "note": "已回到待排产队列，下次试算将重新编排"})


@bp.post("/workpieces/<wid>/close")
def close_workpiece(wid):
    """工件结案：合格品入库结案，或不合格品报废结案（note 说明）。"""
    db = get_db()
    w = db.execute("SELECT * FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    if w["status"] not in ("DONE", "REWORK_PENDING"):
        return _err(409, f"工件状态为 {w['status']}，不能结案（要求 DONE 或 REWORK_PENDING）",
                    status=w["status"])
    data = request.get_json(silent=True) or {}
    note = data.get("note", "")
    db.execute("UPDATE workpieces SET status='CLOSED', note=? WHERE id=?", (note, wid))
    db.commit()
    return jsonify({"workpiece_id": wid, "status": "CLOSED", "note": note})


# ---------------------------------------------------------------- 查询与下载

@bp.get("/health")
def health():
    return jsonify({"ok": True, "time": _now().isoformat()})


@bp.get("/batches")
def list_batches():
    db = get_db()
    state = request.args.get("state")
    sql = ("SELECT id, version_id, oven_id, state, planned_load_at, planned_unload_at"
           " FROM batches")
    args = ()
    if state:
        sql += " WHERE state=?"
        args = (state,)
    sql += " ORDER BY id"
    rows = db.execute(sql, args).fetchall()
    return jsonify({"batches": [dict(r) for r in rows]})


@bp.get("/batches/<int:bid>")
def batch_detail(bid):
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    return jsonify(_batch_payload(db, b))


@bp.get("/workpieces/<wid>")
def workpiece_detail(wid):
    db = get_db()
    w = db.execute("SELECT * FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    batches = db.execute(
        "SELECT bi.batch_id, bi.hanger_slot, bi.slots_used, b.state, b.oven_id"
        " FROM batch_items bi JOIN batches b ON b.id = bi.batch_id"
        " WHERE bi.workpiece_id=? ORDER BY bi.batch_id", (wid,)).fetchall()
    flags = db.execute(
        "SELECT batch_id, code, detail, created_at FROM flags WHERE workpiece_id=?"
        " ORDER BY id", (wid,)).fetchall()
    return jsonify({**dict(w), "is_rework": bool(w["is_rework"]),
                    "probes": _probes_of(db, wid),
                    "batches": [dict(r) for r in batches],
                    "flags": [dict(r) for r in flags]})


@bp.get("/versions")
def list_versions():
    db = get_db()
    rows = db.execute(
        "SELECT v.id, v.parent_id, v.reason, v.created_at,"
        " (SELECT COUNT(*) FROM batches b WHERE b.version_id = v.id) AS batch_count"
        " FROM schedule_versions v ORDER BY v.id").fetchall()
    return jsonify({"versions": [dict(r) for r in rows]})


@bp.get("/batches/<int:bid>/archive")
def batch_archive(bid):
    """下载 JSON 炉次档案。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    payload = _batch_payload(db, b)
    payload["exported_at"] = _now().isoformat()
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=batch_{bid}_archive.json"},
    )


@bp.get("/batches/<int:bid>/card")
def batch_card(bid):
    """可打印随炉卡（HTML，浏览器直接打印）。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    p = _batch_payload(db, b)
    rows = []
    for it in p["items"]:
        flag_txt = "、".join(f["code"] for f in it["flags"]) or "-"
        rows.append(
            "<tr>"
            f"<td>{it['workpiece_id']}</td>"
            f"<td>{it['order_id'] or ''}</td>"
            f"<td>{it['powder_batch']}</td>"
            f"<td>{it['length_mm']:.0f}×{it['width_mm']:.0f}×{it['height_mm']:.0f}</td>"
            f"<td>{it['weight_kg']:.1f}</td>"
            f"<td>{it['hanger_slot']}–{it['hanger_slot'] + it['slots_used'] - 1}</td>"
            f"<td>{it['due_at'] or ''}</td>"
            f"<td>{it['cure']['in_window_minutes']:.1f} / "
            f"{it['cure']['required_hold_minutes']:.0f}</td>"
            f"<td>{flag_txt}</td>"
            "</tr>"
        )
    # 探头明细与最终采用的判定序列（随炉卡质量追溯）
    probe_blocks = []
    for it in p["items"]:
        c = it["cure"]
        probe_rows = []
        for pr in c["probes"]:
            anomalies = "；".join(
                f"卡值 {a['count']} 点 @ {a['value_c']}℃（{a['from']}–{a['to']}）"
                for a in pr["anomalies"]) or "-"
            status = pr["status"]
            if pr.get("disabled_reason"):
                status += f"（{html.escape(pr['disabled_reason'])}）"
            probe_rows.append(
                "<tr>"
                f"<td>{html.escape(str(pr['probe_id'] or '（隐式通道）'))}</td>"
                f"<td>{pr['offset_c']:+.2f}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{pr['reading_count']}</td>"
                f"<td>{anomalies}</td>"
                "</tr>"
            )
        divergences = "；".join(
            f"{d['ts']} 极差 {d['spread_c']}℃" for d in c["divergences"]) or "-"
        gaps = "；".join(
            f"{html.escape(str(g['probe_id'] or '（隐式通道）'))} "
            f"{g['from']}–{g['to']}（{g['minutes']} 分钟）"
            for g in c["probe_gaps"]) or "-"
        series = "，".join(f"{pt['ts'][11:16]}={pt['temp_c']:.1f}"
                           for pt in c["judgment_series"]) or "-"
        probe_blocks.append(
            f"<h3>工件 {html.escape(it['workpiece_id'])}"
            f"（有效探头 {c['valid_probe_count']} / 要求 {c['min_valid_probes']}）</h3>"
            "<table><tr><th>探头</th><th>校准偏移 ℃</th><th>状态</th>"
            "<th>读数</th><th>异常区间</th></tr>"
            f"{''.join(probe_rows)}</table>"
            f"<p class='small'>探头温差异常：{divergences}</p>"
            f"<p class='small'>缺报区间：{gaps}</p>"
            f"<p class='small'>判定序列（各时刻有效探头最低校正温度）：{series}</p>"
        )
    html_doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>随炉卡 · 炉次 {p['batch_id']}</title>
<style>
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; margin-bottom: 4px; }}
h2 {{ font-size: 16px; margin: 20px 0 4px; }}
h3 {{ font-size: 14px; margin: 12px 0 4px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
td, th {{ border: 1px solid #333; padding: 4px 8px; font-size: 13px; }}
table.meta td {{ border: none; padding: 2px 16px 2px 0; }}
p.small {{ font-size: 12px; margin: 4px 0; }}
.sign {{ margin-top: 36px; display: flex; gap: 64px; font-size: 14px; }}
@media print {{ button {{ display: none; }} }}
</style></head><body>
<button onclick="window.print()">打印</button>
<h1>粉末固化随炉卡 · 炉次 #{p['batch_id']}</h1>
<table class="meta">
<tr><td>炉号：{p['oven_id']}</td><td>状态：{p['state']}</td>
    <td>排产版本：v{p['version_id']}</td><td>总重：{p['total_weight_kg']} kg</td></tr>
<tr><td>固化窗口：{p['window']['min_c']}–{p['window']['max_c']} ℃</td>
    <td>保温：{p['window']['hold_minutes']} min</td>
    <td>升温：{p['heatup_minutes']} min</td><td></td></tr>
<tr><td>计划入炉：{p['planned']['load_at']}</td>
    <td>固化开始：{p['planned']['cure_start_at']}</td>
    <td>计划出炉：{p['planned']['unload_at']}</td><td></td></tr>
<tr><td>实际入炉：{p['actual']['load_at'] or ''}</td>
    <td>实际出炉：{p['actual']['unload_at'] or ''}</td><td></td><td></td></tr>
</table>
<table>
<tr><th>工件</th><th>订单</th><th>粉料</th><th>尺寸 mm</th><th>重量 kg</th>
    <th>挂位</th><th>交期</th><th>在区/要求 min</th><th>标记</th></tr>
{''.join(rows)}
</table>
<h2>探头与判定序列</h2>
{''.join(probe_blocks)}
<div class="sign"><span>操作工：____________</span><span>检验员：____________</span>
<span>日期：____________</span></div>
</body></html>"""
    return Response(html_doc, mimetype="text/html")
