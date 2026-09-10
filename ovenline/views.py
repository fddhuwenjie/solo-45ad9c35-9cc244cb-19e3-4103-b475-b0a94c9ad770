"""REST API：试算 / 签发 / 入炉 / 测温回传 / 出炉判定 / 返工结案 / 查询下载。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from . import cure, scheduler
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
FLAG_PROBE_GAP = "PROBE_GAP"                # 探头中断：相邻测温点间隔超阈值
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


def _cure_for_item(db, batch_id, workpiece_id):
    """按工件自身粉料窗口评估固化（窗口交集只用于排产，判定按各件自身窗口）。"""
    powder = db.execute(
        "SELECT p.* FROM workpieces w JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE w.id=?",
        (workpiece_id,),
    ).fetchone()
    rows = db.execute(
        "SELECT ts, metal_temp_c FROM readings WHERE batch_id=? AND workpiece_id=?"
        " ORDER BY ts, id",
        (batch_id, workpiece_id),
    ).fetchall()
    pts = [(datetime.fromisoformat(r["ts"]), r["metal_temp_c"]) for r in rows]
    return cure.evaluate(
        pts,
        powder["temp_min_c"],
        powder["temp_max_c"],
        powder["hold_minutes"],
        current_app.config["PROBE_GAP_MINUTES"],
    )


def _batch_payload(db, b):
    items = db.execute(
        "SELECT bi.workpiece_id, bi.hanger_slot, bi.slots_used, w.order_id,"
        " w.length_mm, w.width_mm, w.height_mm, w.weight_kg, w.powder_batch,"
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
        out_items.append({
            **dict(it),
            "is_rework": bool(it["is_rework"]),
            "cure": _cure_for_item(db, b["id"], wid),
            "flags": flags,
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
    try:
        for o in data["orders"]:
            missing = [k for k in ("workpiece_id", "length_mm", "width_mm", "height_mm",
                                   "weight_kg", "powder_batch") if k not in o]
            if missing:
                return _err(400, f"订单缺少字段: {missing}"
                                 f" (workpiece_id={o.get('workpiece_id')})")
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
    pending = [dict(r) for r in db.execute(
        "SELECT * FROM workpieces WHERE status='PENDING' ORDER BY id").fetchall()]
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
        "unscheduled": unscheduled,
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
    """测温回传：仅在炉（IN_OVEN）状态接收金属探头温度。"""
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
    accepted, rejected = 0, []
    for e in entries:
        wid = e.get("workpiece_id")
        if wid not in valid:
            rejected.append({"workpiece_id": wid, "reason": "工件不在该炉次"})
            continue
        try:
            ts = _parse_dt(e["ts"], "ts")
            temp = float(e["metal_temp_c"])
        except (KeyError, TypeError, ValueError) as ex:
            rejected.append({"workpiece_id": wid, "reason": f"测温记录无效: {ex}"})
            continue
        db.execute(
            "INSERT INTO readings (batch_id, workpiece_id, ts, metal_temp_c)"
            " VALUES (?,?,?,?)", (bid, wid, ts.isoformat(), temp))
        accepted += 1
    db.commit()
    return jsonify({"batch_id": bid, "accepted": accepted, "rejected": rejected})


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
        c = _cure_for_item(db, bid, wid)
        if c["under_time"]:
            _add_flag(db, bid, wid, FLAG_UNDER_TIME,
                      f"许可区间累计 {c['in_window_minutes']} 分钟，"
                      f"不足要求的 {c['required_hold_minutes']} 分钟")
        if c["over_temp"]:
            _add_flag(db, bid, wid, FLAG_OVER_TEMP,
                      f"金属温度最高 {c['max_temp_c']}℃，超过粉料上限")
        if c["probe_gaps"]:
            _add_flag(db, bid, wid, FLAG_PROBE_GAP,
                      "探头中断: " + json.dumps(c["probe_gaps"], ensure_ascii=False))
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
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>随炉卡 · 炉次 {p['batch_id']}</title>
<style>
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; margin-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
td, th {{ border: 1px solid #333; padding: 4px 8px; font-size: 13px; }}
table.meta td {{ border: none; padding: 2px 16px 2px 0; }}
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
<div class="sign"><span>操作工：____________</span><span>检验员：____________</span>
<span>日期：____________</span></div>
</body></html>"""
    return Response(html, mimetype="text/html")
