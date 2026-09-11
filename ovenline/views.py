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

from . import probes, progress, scheduler
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

# 停机窗类型（清炉/校准/检修整段占用烘炉）
BLACKOUT_KINDS = {"CLEANING": "清炉", "CALIBRATION": "校准", "MAINTENANCE": "检修"}
_KIND_ALIASES = {v: k for k, v in BLACKOUT_KINDS.items()}


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


def _cure_for_item(db, b, workpiece_id, as_of=None):
    """按工件固化窗口评估。已签发炉次用签发快照，草稿用当前主数据。

    多探头：校正温度 = 原始值 + 冻结的校准偏移；判定序列取每个采样时刻
    有效探头校正温度的最低值，据此累计固化窗口分钟。
    as_of 不为 None 时：只采用 ts <= as_of 的读数，且缺报测量窗口末端
    取 as_of（用于在炉进度预测）；为 None 时按全体读数与最晚读数时刻
    评估（出炉判定口径）。
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
    if as_of is None:
        rows = db.execute(
            "SELECT ts, probe_id, metal_temp_c FROM readings"
            " WHERE batch_id=? AND workpiece_id=? ORDER BY ts, id",
            (bid, workpiece_id),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT ts, probe_id, metal_temp_c FROM readings"
            " WHERE batch_id=? AND workpiece_id=? AND ts <= ? ORDER BY ts, id",
            (bid, workpiece_id, as_of.isoformat()),
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
        window_end=as_of,
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


def _cure_defects(c):
    """按出炉判定口径从固化分析提取该件不合格标记（代码 + 明细文案）。

    与整炉出炉原逐件打标逻辑一致：欠时/超温/缺报/卡值（启用探头）/
    温差/有效探头不足均为不合格；禁配冲突单独由 _incompat_defects 给出。
    """
    defects = []
    if c["under_time"]:
        defects.append((FLAG_UNDER_TIME,
                        f"许可区间累计 {c['in_window_minutes']} 分钟，"
                        f"不足要求的 {c['required_hold_minutes']} 分钟"))
    if c["over_temp"]:
        defects.append((FLAG_OVER_TEMP,
                        f"金属温度最高 {c['max_temp_c']}℃，超过粉料上限"))
    if c["probe_gaps"]:
        defects.append((FLAG_PROBE_GAP,
                        "探头缺报: " + json.dumps(c["probe_gaps"], ensure_ascii=False)))
    stuck = [a for p in c["probes"] if p["status"] == "ACTIVE"
             for a in p["anomalies"]]
    if stuck:
        defects.append((FLAG_STUCK_PROBE,
                        "探头卡值: " + json.dumps(stuck, ensure_ascii=False)))
    if c["divergences"]:
        defects.append((FLAG_PROBE_DIVERGENCE,
                        "探头温差: " + json.dumps(c["divergences"], ensure_ascii=False)))
    if c["insufficient_probes"]:
        defects.append((FLAG_INSUFFICIENT_PROBES,
                        f"有效探头 {c['valid_probe_count']} 个，"
                        f"少于设定的 {c['min_valid_probes']} 个，不得判定合格"))
    return defects


def _incompat_defects(db, bid, wid, forbidden):
    """该件与炉内任一同炉禁配组成员的冲突标记（逐件出炉复用同一判定）。"""
    me = db.execute(
        "SELECT compat_group FROM workpieces WHERE id=?", (wid,)).fetchone()
    if not me or not me["compat_group"] or not forbidden:
        return []
    others = db.execute(
        "SELECT w.id, w.compat_group FROM batch_items bi"
        " JOIN workpieces w ON w.id = bi.workpiece_id"
        " WHERE bi.batch_id=? AND bi.workpiece_id<>?", (bid, wid)).fetchall()
    defects = []
    seen = set()
    for o in others:
        g2 = o["compat_group"]
        if g2 and ((me["compat_group"], g2) in forbidden
                   or (g2, me["compat_group"]) in forbidden) and o["id"] not in seen:
            seen.add(o["id"])
            defects.append((FLAG_INCOMPAT,
                            f"与 {o['id']} 属同炉禁配组 {me['compat_group']}/{g2}"))
    return defects


def _judge_piece(db, b, wid, at):
    """单件出炉判定：安全条件 + 累计/剩余/阻塞原因，供单件与整炉出炉复用。

    安全条件 = 截至 at 已达标（progress 状态 MET）且无任何不合格标记
    （历史超温/缺报/卡值/温差/有效探头不足/欠时/禁配冲突）。
    返回 {safe, verdict, cure, project, defects:[(code,detail)], blockers}；
    blockers 合并进度阻塞（欠温/超温/缺报/读数陈旧/探头不足/无读数）与
    其余不合格标记明细，普通请求据此拒绝。
    """
    c = _cure_for_item(db, b, wid, as_of=at)
    version = db.execute("SELECT params_json FROM schedule_versions WHERE id=?",
                         (b["version_id"],)).fetchone()
    forbidden = set()
    if version:
        for pair in json.loads(version["params_json"]).get("forbidden_pairs", []):
            forbidden.add((pair[0], pair[1]))
    defects = _incompat_defects(db, b["id"], wid, forbidden) + _cure_defects(c)
    proj = _item_project(db, b, wid, at)

    blockers = [dict(x) for x in proj["blockers"]]
    have = {x["code"] for x in blockers}
    # 已达标但历史上存在不合格标记（历史超温/缺报/卡值等）同样阻止安全出炉
    for code, detail in defects:
        if code not in have:
            blockers.append({"code": code, "message": detail})
            have.add(code)
    safe = not defects and proj["status"] == progress.STATUS_MET
    return {"safe": safe, "verdict": "OK" if safe else "NOT_OK", "cure": c,
            "project": proj, "defects": defects, "blockers": blockers}


def _apply_piece_unload(db, bid, wid, judgment, at, forced, reason):
    """执行单件离炉：写标记/工件状态/逐件离炉列/审计；返回离炉结果 dict。

    强制出炉：最终判定一律 NOT_OK（REWORK_PENDING），并保留所有已观测
    不合格标记；普通安全出炉：无标记，最终判定 OK（DONE）。
    """
    at_iso = at.isoformat()
    codes = []
    for code, detail in judgment["defects"]:
        _add_flag(db, bid, wid, code, detail)
        codes.append(code)
    # 强制出炉一律标记不合格；普通离炉以安全条件为准
    if forced:
        verdict, wstatus = "NOT_OK", "REWORK_PENDING"
    else:
        verdict = "OK" if judgment["safe"] else "NOT_OK"
        wstatus = "DONE" if judgment["safe"] else "REWORK_PENDING"
    seq_row = db.execute(
        "SELECT COALESCE(MAX(unload_sequence), 0) AS n FROM batch_items"
        " WHERE batch_id=?", (bid,)).fetchone()
    seq = seq_row["n"] + 1
    proj = judgment["project"]
    snap = json.dumps(proj, ensure_ascii=False)
    db.execute(
        "UPDATE batch_items SET actual_unload_at=?, unload_sequence=?,"
        " first_met_at=?, final_verdict=?, forced=?, force_reason=?,"
        " progress_snapshot_json=? WHERE batch_id=? AND workpiece_id=?",
        (at_iso, seq, proj["first_met_at"], verdict,
         1 if forced else 0, reason if forced else None, snap, bid, wid))
    db.execute(
        "INSERT INTO unload_actions (batch_id, workpiece_id, sequence, verdict,"
        " forced, reason, flags_json, snapshot_json, unload_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (bid, wid, seq, verdict, 1 if forced else 0,
         reason if forced else None, json.dumps(codes, ensure_ascii=False),
         snap, at_iso, _now().isoformat()))
    db.execute("UPDATE workpieces SET status=? WHERE id=?", (wstatus, wid))
    return {"workpiece_id": wid, "verdict": verdict, "forced": forced,
            "reason": reason if forced else None, "flags": codes,
            "unload_at": at_iso, "sequence": seq,
            "first_met_at": proj["first_met_at"],
            "in_window_minutes": proj["in_window_minutes"],
            "remaining_hold_minutes": proj["remaining_hold_minutes"],
            "progress_snapshot": proj}


def _finalize_batch_if_empty(db, bid, at):
    """炉内已无工件时炉次转 UNLOADED，actual_unload_at 取最后离炉时刻。"""
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id=?"
        " AND actual_unload_at IS NULL", (bid,)).fetchone()["n"]
    if remaining:
        return None
    row = db.execute(
        "SELECT MAX(actual_unload_at) AS last_at FROM batch_items WHERE batch_id=?",
        (bid,)).fetchone()
    last_at = max(row["last_at"], at.isoformat()) if row["last_at"] else at.isoformat()
    db.execute("UPDATE batches SET state='UNLOADED', actual_unload_at=? WHERE id=?",
               (last_at, bid))
    return last_at


def _item_window(db, batch_id, workpiece_id):
    """工件在该炉次的固化窗口与保温要求（已签发取快照，草稿取当前主数据）。"""
    r = db.execute(
        "SELECT COALESCE(bi.snap_temp_min_c, p.temp_min_c) AS temp_min_c,"
        " COALESCE(bi.snap_temp_max_c, p.temp_max_c) AS temp_max_c,"
        " COALESCE(bi.snap_hold_minutes, p.hold_minutes) AS hold_minutes"
        " FROM batch_items bi JOIN workpieces w ON w.id = bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE bi.batch_id=? AND bi.workpiece_id=?",
        (batch_id, workpiece_id)).fetchone()
    return r["temp_min_c"], r["temp_max_c"], r["hold_minutes"]


def _resolve_as_of(b, requested, now=None):
    """解析进度计算基准时刻。

    显式传入的 as_of 原样采用（允许历史复盘）；未传入时：
    在炉炉次取当前时刻；已出炉炉次取实际出炉时刻（出炉后的快照不再随时间漂移）；
    其他状态取当前时刻。
    返回 (as_of datetime, source)。
    """
    now = now or _now()
    if requested is not None:
        return requested, "query"
    if b["state"] in ("UNLOADED", "CLOSED") and b["actual_unload_at"]:
        return datetime.fromisoformat(b["actual_unload_at"]), "actual_unload_at"
    return now, "now"


def _item_project(db, b, wid, as_of):
    """单件在炉进度与安全出炉预测（读数按 as_of 截断，窗口/探头取签发快照）。"""
    wmin, wmax, hold = _item_window(db, b["id"], wid)
    rows = db.execute(
        "SELECT ts, probe_id, metal_temp_c FROM readings"
        " WHERE batch_id=? AND workpiece_id=? AND ts <= ? ORDER BY ts, id",
        (b["id"], wid, as_of.isoformat())).fetchall()
    pts = [(datetime.fromisoformat(r["ts"]), r["probe_id"], r["metal_temp_c"])
           for r in rows]
    cfg = _probe_cfg_for_item(
        db, b["id"], wid,
        frozen=b["state"] not in ("DRAFT", "SUPERSEDED"))
    return progress.project_item(
        pts,
        {pid: {"offset_c": c["offset_c"], "status": c["status"]}
         for pid, c in cfg.items()},
        wmin, wmax, hold, as_of, current_app.config["PROBE_GAP_MINUTES"],
        current_app.config["PROBE_DIVERGENCE_C"],
        current_app.config["STUCK_PROBE_MIN_CONSECUTIVE"],
        current_app.config["MIN_VALID_PROBES"])


def _batch_progress(db, b, as_of, basis_source, computed_at=None):
    """构建炉次级在炉固化进度与安全出炉预测（逐件 project_item + 汇总）。

    读数按 as_of 截断，签发快照窗口/探头配置，缺报窗口末端取 as_of。
    计划出炉时刻取签发时冻结的 planned_unload_at，本计算不改写它。
    汇总仅计算在炉工件：已离炉（actual_unload_at <= as_of）的工件跳过，
    其离炉当时的进度快照保留在 batch_items.progress_snapshot_json 中。
    """
    item_rows = db.execute(
        "SELECT workpiece_id, actual_unload_at FROM batch_items"
        " WHERE batch_id=? ORDER BY hanger_slot",
        (b["id"],)).fetchall()
    items = []
    for r in item_rows:
        if r["actual_unload_at"]:
            # 历史复盘（as_of 早于该件离炉时刻）时该件当时仍在炉，照常计入
            if datetime.fromisoformat(r["actual_unload_at"]) <= as_of:
                continue
        items.append({"workpiece_id": r["workpiece_id"],
                      **_item_project(db, b, r["workpiece_id"], as_of)})
    planned = (datetime.fromisoformat(b["planned_unload_at"])
               if b["planned_unload_at"] else None)
    return progress.summarize(items, planned, as_of,
                              computed_at or _now(), basis_source)


def _frozen_progress(db, b, computed_at=None):
    """整炉离炉后的只读进度汇总。

    逐件取离炉当时冻结的 project_item 快照（旧库无快照的退化为按最后离炉
    时刻重算），基准时刻取炉次实际出炉时刻（= 最后一件离炉时刻），
    汇总不再随后续时间或读数漂移。
    """
    as_of = datetime.fromisoformat(b["actual_unload_at"])
    rows = db.execute(
        "SELECT workpiece_id, progress_snapshot_json FROM batch_items"
        " WHERE batch_id=? AND actual_unload_at IS NOT NULL"
        " ORDER BY unload_sequence", (b["id"],)).fetchall()
    items = []
    for r in rows:
        if r["progress_snapshot_json"]:
            snap = json.loads(r["progress_snapshot_json"])
        else:
            snap = _item_project(db, b, r["workpiece_id"], as_of)
        snap["workpiece_id"] = r["workpiece_id"]
        items.append(snap)
    planned = (datetime.fromisoformat(b["planned_unload_at"])
               if b["planned_unload_at"] else None)
    return progress.summarize(items, planned, as_of,
                              computed_at or _now(), "actual_unload_at")


def _batch_payload(db, b, as_of=None, as_of_source=None):
    items = db.execute(
        "SELECT bi.workpiece_id, bi.hanger_slot, bi.slots_used, w.order_id,"
        " COALESCE(bi.snap_length_mm, w.length_mm) AS length_mm,"
        " COALESCE(bi.snap_width_mm, w.width_mm) AS width_mm,"
        " COALESCE(bi.snap_height_mm, w.height_mm) AS height_mm,"
        " COALESCE(bi.snap_weight_kg, w.weight_kg) AS weight_kg,"
        " COALESCE(bi.snap_powder_batch, w.powder_batch) AS powder_batch,"
        " w.compat_group, w.due_at, w.is_rework, w.status,"
        " bi.actual_unload_at, bi.unload_sequence, bi.first_met_at,"
        " bi.final_verdict, bi.forced, bi.force_reason, bi.progress_snapshot_json"
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
            **{k: it[k] for k in it.keys()
               if k not in ("forced", "progress_snapshot_json")},
            "is_rework": bool(it["is_rework"]),
            "forced": bool(it["forced"]),
            "cure": _cure_for_item(db, b, wid),
            "flags": flags,
            "probe_actions": probe_actions,
            # 离炉当时冻结的进度快照（在炉时为 None）
            "progress_snapshot": (json.loads(it["progress_snapshot_json"])
                                  if it["progress_snapshot_json"] else None),
        })
    resolved_as_of, default_source = _resolve_as_of(b, as_of)
    basis_source = as_of_source if as_of is not None else default_source
    # 整炉已离炉且未指定历史基准：逐件取离炉当时冻结快照，不再随时间漂移；
    # 其余情况（在炉/历史复盘）按基准时刻重算，且只汇总仍在炉的工件
    if as_of is None and b["state"] in ("UNLOADED", "CLOSED") \
            and b["actual_unload_at"]:
        progress_snapshot = _frozen_progress(db, b)
    else:
        progress_snapshot = _batch_progress(db, b, resolved_as_of, basis_source)
    # 本炉次所属版本、本炉的停机窗（清炉/校准/检修）与排产计算依据
    blackouts = [
        {"oven_id": r["oven_id"], "kind": r["kind"],
         "kind_text": BLACKOUT_KINDS.get(r["kind"], r["kind"]),
         "start_at": r["start_at"], "end_at": r["end_at"], "note": r["note"]}
        for r in db.execute(
            "SELECT oven_id, kind, start_at, end_at, note FROM blackout_windows"
            " WHERE version_id=? AND oven_id=? ORDER BY start_at",
            (b["version_id"], b["oven_id"])).fetchall()
    ]
    # 逐件离炉顺序（序号、时刻、判定、强制原因、当时进度快照）
    unload_order = [
        {"sequence": a["sequence"], "workpiece_id": a["workpiece_id"],
         "unload_at": a["unload_at"], "verdict": a["verdict"],
         "forced": bool(a["forced"]), "reason": a["reason"],
         "flags": json.loads(a["flags_json"]),
         "progress_snapshot": json.loads(a["snapshot_json"])}
        for a in db.execute(
            "SELECT sequence, workpiece_id, unload_at, verdict, forced, reason,"
            " flags_json, snapshot_json FROM unload_actions"
            " WHERE batch_id=? ORDER BY sequence", (b["id"],)).fetchall()
    ]
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
        # 停机避让：关联停机窗与排产计算依据（未避让基准时刻/等待分钟/逾期变化）
        "blackout_windows": blackouts,
        "blackout_wait_minutes": b["blackout_wait_minutes"],
        "schedule_basis": (json.loads(b["schedule_basis_json"])
                           if b["schedule_basis_json"] else None),
        "items": out_items,
        "unload_order": unload_order,
        # 同一进度快照贯穿炉次详情 / JSON 档案 / 随炉卡
        "progress": progress_snapshot,
    }


# ---------------------------------------------------------------- 停机窗

def _parse_blackouts(raw_list, oven_ids):
    """校验并规范化停机窗：未知炉号 / 起止倒序 / 同炉重叠一律拒绝（ValueError）。

    返回 [{oven_id, kind, start_at, end_at, note}]，start_at/end_at 为 datetime。
    """
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise ValueError("blackout_windows 须为数组，元素含"
                         " oven_id/kind/start_at/end_at/note")
    windows = []
    for w in raw_list:
        if not isinstance(w, dict):
            raise ValueError(f"停机窗须为对象: {w!r}")
        missing = [k for k in ("oven_id", "kind", "start_at", "end_at") if k not in w]
        if missing:
            raise ValueError(f"停机窗缺少字段: {missing}")
        oid = w["oven_id"]
        if oid not in oven_ids:
            raise ValueError(f"停机窗引用未知炉号: {oid!r}（本次试算未包含该炉）")
        kind = _KIND_ALIASES.get(str(w["kind"]).strip(),
                                 str(w["kind"]).strip().upper())
        if kind not in BLACKOUT_KINDS:
            raise ValueError(
                f"未知停机类型: {w['kind']!r}（支持 清炉/校准/检修，"
                "即 CLEANING/CALIBRATION/MAINTENANCE）")
        start = _parse_dt(w["start_at"], "blackout_windows.start_at")
        end = _parse_dt(w["end_at"], "blackout_windows.end_at")
        if end <= start:
            raise ValueError(
                f"炉 {oid} 停机窗起止倒序: {start.isoformat()} 不早于 "
                f"{end.isoformat()}")
        windows.append({"oven_id": oid, "kind": kind, "start_at": start,
                        "end_at": end, "note": w.get("note")})
    by_oven = {}
    for w in windows:
        by_oven.setdefault(w["oven_id"], []).append(w)
    for oid, ws in by_oven.items():
        ws.sort(key=lambda x: x["start_at"])
        for a, b in zip(ws, ws[1:]):
            if b["start_at"] < a["end_at"]:
                raise ValueError(
                    f"炉 {oid} 停机窗重叠: "
                    f"{a['start_at'].isoformat()}–{a['end_at'].isoformat()} 与 "
                    f"{b['start_at'].isoformat()}–{b['end_at'].isoformat()}")
    return windows


def _window_json(w):
    return {"oven_id": w["oven_id"], "kind": w["kind"],
            "kind_text": BLACKOUT_KINDS[w["kind"]],
            "start_at": w["start_at"].isoformat(timespec="seconds"),
            "end_at": w["end_at"].isoformat(timespec="seconds"),
            "note": w.get("note")}


def _blackout_conflicts(carried, windows_by_oven, turnaround_by_oven):
    """新停机窗与已签发/在炉炉次占用区间的重叠清单（这些炉次不得改时刻）。

    占用区间 = [计划入炉, 计划出炉+周转]；每处重叠给出区间与冲突分钟。
    """
    conflicts = []
    for c in carried:
        load = _parse_dt(c["planned_load_at"], "planned_load_at")
        unload = _parse_dt(c["planned_unload_at"], "planned_unload_at")
        occ_end = unload + timedelta(
            minutes=turnaround_by_oven.get(c["oven_id"], 0))
        for w in windows_by_oven.get(c["oven_id"], []):
            s = max(load, w["start_at"])
            e = min(occ_end, w["end_at"])
            if s < e:
                conflicts.append({
                    "oven_id": c["oven_id"],
                    "batch_id": c["id"],
                    "batch_state": c["state"],
                    "occupied": {
                        "start_at": load.isoformat(timespec="seconds"),
                        "end_at": occ_end.isoformat(timespec="seconds"),
                    },
                    "blackout": _window_json(w),
                    "overlap": {
                        "start_at": s.isoformat(timespec="seconds"),
                        "end_at": e.isoformat(timespec="seconds"),
                        "minutes": round((e - s).total_seconds() / 60.0, 2),
                    },
                })
    return conflicts


def _oven_timelines(oven_rows, carried, new_batches, windows_by_oven):
    """逐炉时间线：区分生产占用 / 周转 / 停机，并比较各炉完工时刻与交期。

    carried:     已签发/在炉炉次行（id, oven_id, state, 计划起止）
    new_batches: 本次试算新排炉次 dict（含 turnaround_end_at 与逾期字段）
    """
    timelines = []
    for ov in sorted(oven_rows, key=lambda o: o["id"]):
        oid = ov["id"]
        turnaround = float(ov["turnaround_minutes"])
        segments = []
        for c in carried:
            if c["oven_id"] != oid:
                continue
            unload = _parse_dt(c["planned_unload_at"], "planned_unload_at")
            segments.append({
                "kind": "PRODUCTION", "batch_id": c["id"], "state": c["state"],
                "start_at": c["planned_load_at"], "end_at": c["planned_unload_at"]})
            segments.append({
                "kind": "TURNAROUND", "batch_id": c["id"], "state": c["state"],
                "start_at": c["planned_unload_at"],
                "end_at": (unload + timedelta(minutes=turnaround))
                .isoformat(timespec="seconds")})
        for b in new_batches:
            if b["oven_id"] != oid:
                continue
            segments.append({
                "kind": "PRODUCTION", "batch_id": b["batch_id"], "state": "DRAFT",
                "start_at": b["planned_load_at"], "end_at": b["planned_unload_at"]})
            segments.append({
                "kind": "TURNAROUND", "batch_id": b["batch_id"], "state": "DRAFT",
                "start_at": b["planned_unload_at"],
                "end_at": b["turnaround_end_at"]})
        for w in windows_by_oven.get(oid, []):
            wj = _window_json(w)
            segments.append({
                "kind": "BLACKOUT", "blackout_kind": wj["kind"],
                "kind_text": wj["kind_text"], "start_at": wj["start_at"],
                "end_at": wj["end_at"], "note": wj["note"]})
        segments.sort(key=lambda s: (s["start_at"], s["kind"]))
        ends = [s["end_at"] for s in segments if s["kind"] == "PRODUCTION"]
        releases = [s["end_at"] for s in segments if s["kind"] == "TURNAROUND"]
        late = [b["lateness_minutes"] for b in new_batches
                if b["oven_id"] == oid and b["lateness_minutes"] > 0]
        timelines.append({
            "oven_id": oid,
            "segments": segments,
            # 完工时刻 = 最后一炉出炉；释放时刻 = 完工+周转后炉膛可再次排产
            "completed_at": max(ends) if ends else None,
            "released_at": max(releases) if releases else None,
            "late_batches": len(late),
            "max_lateness_minutes": round(max(late), 2) if late else 0.0,
        })
    return timelines


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

    # 停机窗（清炉/校准/检修）：整段占用烘炉；未知炉号/倒序/同炉重叠一律拒绝
    try:
        blackouts = _parse_blackouts(data.get("blackout_windows", []),
                                     {ov["id"] for ov in oven_rows})
    except ValueError as e:
        return _err(400, str(e))
    windows_by_oven = {}
    for w in blackouts:
        windows_by_oven.setdefault(w["oven_id"], []).append(w)

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
        # 停机窗随版本快照存档，后续试算可整体改写
        "blackout_windows": [_window_json(w) for w in blackouts],
    }
    cur = db.execute(
        "INSERT INTO schedule_versions (parent_id, reason, params_json, created_at)"
        " VALUES (?,?,?,?)",
        (parent["id"] if parent else None, data.get("reason", ""),
         json.dumps(snapshot, ensure_ascii=False), _now().isoformat()),
    )
    version_id = cur.lastrowid
    for w in blackouts:
        db.execute(
            "INSERT INTO blackout_windows (version_id, oven_id, kind, start_at,"
            " end_at, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (version_id, w["oven_id"], w["kind"],
             w["start_at"].isoformat(timespec="seconds"),
             w["end_at"].isoformat(timespec="seconds"),
             w.get("note"), _now().isoformat()),
    )

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
        pending, powder_all, oven_rows, forbidden, start_at, busy_until,
        blackouts=windows_by_oven)

    new_batches = []
    for b in planned:
        # 停机避让计算依据随炉次冻结，炉次详情/档案据此复算
        basis = {k: b[k] for k in (
            "baseline_load_at", "baseline_unload_at", "blackout_wait_minutes",
            "avoided_windows", "earliest_due_at", "lateness_minutes",
            "baseline_lateness_minutes", "lateness_delta_minutes",
            "turnaround_end_at")}
        cur = db.execute(
            "INSERT INTO batches (version_id, oven_id, state, window_min_c, window_max_c,"
            " hold_minutes, total_weight_kg, heatup_minutes, planned_load_at,"
            " planned_cure_start_at, planned_unload_at, blackout_wait_minutes,"
            " schedule_basis_json, created_at)"
            " VALUES (?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, b["oven_id"], b["window_min_c"], b["window_max_c"],
             b["hold_minutes"], b["total_weight_kg"], b["heatup_minutes"],
             b["planned_load_at"], b["planned_cure_start_at"], b["planned_unload_at"],
             b["blackout_wait_minutes"], json.dumps(basis, ensure_ascii=False),
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
    # 新停机窗撞上已签发/在炉炉次：这些炉次不得改时刻，列出重叠区间与冲突分钟
    turnaround_by_oven = {ov["id"]: float(ov["turnaround_minutes"])
                          for ov in oven_rows}
    conflicts = _blackout_conflicts(carried, windows_by_oven, turnaround_by_oven)
    return jsonify({
        "version": {"id": version_id, "parent_id": parent["id"] if parent else None,
                    "reason": data.get("reason", "")},
        "carried_batches": [dict(c) for c in carried],
        "new_batches": new_batches,
        "unscheduled": pre_unscheduled + unscheduled,
        "blackout_windows": [_window_json(w) for w in blackouts],
        "blackout_conflicts": conflicts,
        "oven_timelines": _oven_timelines(oven_rows, carried, new_batches,
                                          windows_by_oven),
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
    # 已逐件离炉的工件不再接收读数
    left_at = dict(db.execute(
        "SELECT workpiece_id, actual_unload_at FROM batch_items"
        " WHERE batch_id=? AND actual_unload_at IS NOT NULL", (bid,)).fetchall())
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
        if wid in left_at:
            rejected.append({"workpiece_id": wid, "probe_id": pid,
                             "reason": f"工件已于 {left_at[wid]} 离炉，"
                                       "离炉后不再接收读数"})
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
    # 接受新读数后即时重算在炉进度（重复回传也返回当前进度；不改写签发计划）
    now = _now()
    return jsonify({"batch_id": bid, "accepted": accepted,
                    "duplicates": duplicates, "rejected": rejected,
                    "progress": _batch_progress(db, b, now, "now",
                                                computed_at=now)})


@bp.post("/batches/<int:bid>/workpieces/<wid>/unload")
def unload_workpiece(bid, wid):
    """逐件出炉：按 at 判定单件是否达到安全出炉条件。

    普通请求（force 非真）：未达到安全条件时拒绝（409）并返回该件累计、
    剩余时间与阻塞原因，不留离炉记录；安全时该件离炉（DONE）。
    强制出炉（force=true）：必须填写 reason；保留不合格标记，最终判定
    NOT_OK（REWORK_PENDING），并写 unload_actions 审计。
    还有工件在炉时炉次保持 IN_OVEN；最后一件离炉后炉次自动转 UNLOADED，
    actual_unload_at 取最后离炉时刻。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    item = db.execute(
        "SELECT actual_unload_at FROM batch_items WHERE batch_id=? AND workpiece_id=?",
        (bid, wid)).fetchone()
    if item is None:
        return _err(404, f"工件 {wid} 不在炉次 {bid} 中")
    if item["actual_unload_at"] is not None:
        return _err(409,
                    f"工件 {wid} 已于 {item['actual_unload_at']} 离炉，不能重复出炉",
                    state=b["state"], unload_at=item["actual_unload_at"])
    if b["state"] == "DRAFT":
        _add_flag(db, bid, wid, FLAG_UNISSUED_UNLOAD,
                  "炉次未签发即请求逐件出炉")
        db.commit()
        return _err(409, "炉次未签发，禁止出炉；已记录 UNISSUED_UNLOAD 标记",
                    state=b["state"])
    if b["state"] != "IN_OVEN":
        return _err(409, f"炉次状态为 {b['state']}，不能逐件出炉（要求 IN_OVEN）",
                    state=b["state"])
    data = request.get_json(silent=True) or {}
    try:
        at = _parse_dt(data["at"], "at") if data.get("at") else _now()
    except ValueError as e:
        return _err(400, str(e))
    force = bool(data.get("force"))
    reason = str(data.get("reason") or "").strip() or None

    judgment = _judge_piece(db, b, wid, at)
    if force and not reason:
        return _err(400, "强制出炉必须填写原因 reason")
    if not judgment["safe"] and not force:
        return _err(409, f"工件 {wid} 未达到安全出炉条件，拒绝出炉；"
                         "如确认强制出炉请带 force=true 与 reason",
                    state=b["state"],
                    workpiece_id=wid,
                    in_window_minutes=judgment["project"]["in_window_minutes"],
                    remaining_hold_minutes=judgment["project"]["remaining_hold_minutes"],
                    first_met_at=judgment["project"]["first_met_at"],
                    blockers=judgment["blockers"],
                    progress=judgment["project"])

    result = _apply_piece_unload(db, bid, wid, judgment, at, force, reason)
    last_at = _finalize_batch_if_empty(db, bid, at)
    db.commit()
    new_state = "UNLOADED" if last_at is not None else "IN_OVEN"
    # 离炉后即时返回炉次进度（最后一件离炉后为逐件冻结快照汇总）
    if last_at is not None:
        b2 = _fetch_batch(db, bid)
        prog = _frozen_progress(db, b2)
    else:
        prog = _batch_progress(db, b, at, "query", computed_at=_now())
    return jsonify({"batch_id": bid, "state": new_state,
                    "actual_unload_at": last_at,
                    "result": result, "progress": prog})


@bp.post("/batches/<int:bid>/unload")
def unload(bid):
    """整炉出炉判定：逐件复用同一判定，已离炉工件跳过。

    IN_OVEN -> UNLOADED（全部工件此前已离炉时立即转换）；仍在炉的工件按
    同一套安全/不合格口径逐件落判定与标记。actual_unload_at 取最后离炉时刻。
    """
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

    # 仍在炉的工件逐件复用同一判定；已离炉工件跳过（保留其既有判定）
    rows = db.execute(
        "SELECT bi.workpiece_id FROM batch_items bi"
        " WHERE bi.batch_id=? AND bi.actual_unload_at IS NULL"
        " ORDER BY bi.hanger_slot", (bid,)).fetchall()
    skipped = [r["workpiece_id"] for r in db.execute(
        "SELECT workpiece_id FROM batch_items WHERE batch_id=?"
        " AND actual_unload_at IS NOT NULL ORDER BY unload_sequence", (bid,)).fetchall()]
    # 禁配冲突按炉内成对判定：与原整炉出炉一致，互为冲突的双方都落标记
    version = db.execute("SELECT params_json FROM schedule_versions WHERE id=?",
                         (b["version_id"],)).fetchone()
    forbidden = set()
    if version:
        for pair in json.loads(version["params_json"]).get("forbidden_pairs", []):
            forbidden.add((pair[0], pair[1]))
    remaining = {r["workpiece_id"] for r in rows}
    if remaining and forbidden:
        members = {r["workpiece_id"]: r["compat_group"] for r in db.execute(
            "SELECT bi.workpiece_id, w.compat_group FROM batch_items bi"
            " JOIN workpieces w ON w.id = bi.workpiece_id"
            " WHERE bi.batch_id=?", (bid,)).fetchall()}
        pending = list(remaining)
        for i in range(len(pending)):
            for j in range(i + 1, len(pending)):
                a, other = pending[i], pending[j]
                ga, gb = members.get(a), members.get(other)
                if ga and gb and ((ga, gb) in forbidden
                                  or (gb, ga) in forbidden):
                    _add_flag(db, bid, a, FLAG_INCOMPAT,
                              f"与 {other} 属同炉禁配组 {ga}/{gb}")
                    _add_flag(db, bid, other, FLAG_INCOMPAT,
                              f"与 {a} 属同炉禁配组 {ga}/{gb}")
    results = []
    for r in rows:
        wid = r["workpiece_id"]
        judgment = _judge_piece(db, b, wid, at)
        result = _apply_piece_unload(db, bid, wid, judgment, at, False, None)
        # 兼容旧响应：附带完整 cure 分析
        result["cure"] = judgment["cure"]
        results.append(result)
    last_at = _finalize_batch_if_empty(db, bid, at)
    db.commit()
    return jsonify({"batch_id": bid, "state": "UNLOADED",
                    "actual_unload_at": last_at, "results": results,
                    "skipped": skipped})


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
    item = db.execute(
        "SELECT actual_unload_at FROM batch_items WHERE batch_id=? AND workpiece_id=?",
        (bid, wid)).fetchone()
    if item is None:
        return _err(404, f"工件 {wid} 不在炉次 {bid} 中")
    if item["actual_unload_at"] is not None:
        return _err(409,
                    f"工件 {wid} 已于 {item['actual_unload_at']} 离炉，"
                    "离炉后不能停用探头",
                    state=b["state"], unload_at=item["actual_unload_at"])
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
    now = _now()
    return jsonify({
        "batch_id": bid, "workpiece_id": wid, "probe_id": pid,
        "status": "DISABLED", "reason": reason,
        "recalc": {"before": _cure_summary(before),
                   "after": _cure_summary(after)},
        "cure": after,
        # 停用探头后即时重算在炉进度（只重算该工件，不改写已签发计划）
        "progress": _batch_progress(db, b, now, "now", computed_at=now),
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
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else None
    except ValueError as e:
        return _err(400, str(e))
    return jsonify(_batch_payload(db, b, as_of=as_of, as_of_source="query"))


@bp.get("/batches/<int:bid>/progress")
def batch_progress(bid):
    """在炉固化进度与安全出炉预测。

    查询参数 as_of：计算基准时刻（ISO），缺省取当前时刻（已出炉炉次取
    实际出炉时刻）。逐件返回最新有效测温时刻、最低校正温度、已累计/剩余
    保温分钟、读数新鲜度与阻塞原因；炉次级取最晚安全出炉时刻，
    并指出计划出炉过早的分钟数。本接口为只读预测，不改写已签发计划。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else None
    except ValueError as e:
        return _err(400, str(e))
    resolved, source = _resolve_as_of(b, as_of)
    # 整炉已离炉且未指定历史基准：返回逐件离炉当时冻结快照的汇总
    if as_of is None and b["state"] in ("UNLOADED", "CLOSED") \
            and b["actual_unload_at"]:
        prog = _frozen_progress(db, b)
    else:
        prog = _batch_progress(db, b, resolved, source)
    return jsonify({
        "batch_id": bid,
        "state": b["state"],
        "progress": prog,
    })


@bp.get("/workpieces/<wid>")
def workpiece_detail(wid):
    db = get_db()
    w = db.execute("SELECT * FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    batches = db.execute(
        "SELECT bi.batch_id, bi.hanger_slot, bi.slots_used, b.state, b.oven_id,"
        " bi.actual_unload_at, bi.unload_sequence, bi.first_met_at,"
        " bi.final_verdict, bi.forced, bi.force_reason"
        " FROM batch_items bi JOIN batches b ON b.id = bi.batch_id"
        " WHERE bi.workpiece_id=? ORDER BY bi.batch_id", (wid,)).fetchall()
    flags = db.execute(
        "SELECT batch_id, code, detail, created_at FROM flags WHERE workpiece_id=?"
        " ORDER BY id", (wid,)).fetchall()
    return jsonify({**{k: w[k] for k in w.keys()}, "is_rework": bool(w["is_rework"]),
                    "probes": _probes_of(db, wid),
                    "batches": [{**dict(r), "forced": bool(r["forced"])}
                                for r in batches],
                    "flags": [dict(r) for r in flags]})


@bp.get("/versions")
def list_versions():
    db = get_db()
    rows = db.execute(
        "SELECT v.id, v.parent_id, v.reason, v.created_at,"
        " (SELECT COUNT(*) FROM batches b WHERE b.version_id = v.id) AS batch_count,"
        " (SELECT COUNT(*) FROM blackout_windows w WHERE w.version_id = v.id)"
        "   AS blackout_count"
        " FROM schedule_versions v ORDER BY v.id").fetchall()
    return jsonify({"versions": [dict(r) for r in rows]})


@bp.get("/versions/<int:vid>")
def version_detail(vid):
    """排产版本详情：参数快照（含停机窗）与该版本排出的炉次。"""
    db = get_db()
    v = db.execute("SELECT * FROM schedule_versions WHERE id=?", (vid,)).fetchone()
    if v is None:
        return _err(404, f"排产版本 {vid} 不存在")
    params = json.loads(v["params_json"]) if v["params_json"] else {}
    windows = [
        {"oven_id": r["oven_id"], "kind": r["kind"],
         "kind_text": BLACKOUT_KINDS.get(r["kind"], r["kind"]),
         "start_at": r["start_at"], "end_at": r["end_at"], "note": r["note"]}
        for r in db.execute(
            "SELECT oven_id, kind, start_at, end_at, note FROM blackout_windows"
            " WHERE version_id=? ORDER BY oven_id, start_at", (vid,)).fetchall()
    ]
    batches = [
        {"batch_id": r["id"], "oven_id": r["oven_id"], "state": r["state"],
         "planned_load_at": r["planned_load_at"],
         "planned_unload_at": r["planned_unload_at"],
         "blackout_wait_minutes": r["blackout_wait_minutes"]}
        for r in db.execute(
            "SELECT id, oven_id, state, planned_load_at, planned_unload_at,"
            " blackout_wait_minutes FROM batches WHERE version_id=? ORDER BY id",
            (vid,)).fetchall()
    ]
    return jsonify({
        "id": v["id"], "parent_id": v["parent_id"], "reason": v["reason"],
        "created_at": v["created_at"],
        "params": params,                # 试算输入快照（计算依据）
        "blackout_windows": windows,     # 该版本登记的停机窗
        "batches": batches,
    })


@bp.get("/batches/<int:bid>/archive")
def batch_archive(bid):
    """下载 JSON 炉次档案。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else None
    except ValueError as e:
        return _err(400, str(e))
    payload = _batch_payload(db, b, as_of=as_of, as_of_source="query")
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
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else None
    except ValueError as e:
        return _err(400, str(e))
    p = _batch_payload(db, b, as_of=as_of, as_of_source="query")
    rows = []
    for it in p["items"]:
        flag_txt = "、".join(f["code"] for f in it["flags"]) or "-"
        unload_txt = "-"
        if it["actual_unload_at"]:
            unload_txt = f"#{it['unload_sequence']} {it['actual_unload_at'][5:16]}"
            if it["final_verdict"] == "OK":
                unload_txt += " 合格"
            else:
                unload_txt += " 不合格"
                if it["forced"]:
                    unload_txt += "（强制）"
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
            f"<td>{html.escape(unload_txt)}</td>"
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
    # 在炉固化进度与安全出炉预测（与炉次详情/JSON 档案同一进度快照）
    prog = p.get("progress") or {}
    basis = prog.get("basis", {})
    source_text = {"query": "查询指定", "now": "查询时刻",
                   "actual_unload_at": "实际出炉时刻"}.get(basis.get("source"),
                                                           basis.get("source", ""))
    prog_rows = []
    for it in prog.get("items", []):
        blockers = "；".join(
            f"[{x['code']}] {html.escape(x['message'])}"
            for x in it["blockers"]) or "-"
        alerts = "；".join(
            f"[{x['code']}] {html.escape(x['message'])}"
            for x in it["alerts"]) or "-"
        freshness = ("-" if it["reading_freshness_minutes"] is None
                     else f"{it['reading_freshness_minutes']:g} 分钟"
                          + ("（缺报）" if it["stale"] else ""))
        latest_temp = "-" if it["latest_temp_c"] is None \
            else f"{it['latest_temp_c']:g}"
        safe = it["safe_unload_at"] or "不可预测"
        met = it["first_met_at"] or "-"
        prog_rows.append(
            "<tr>"
            f"<td>{html.escape(it['workpiece_id'])}</td>"
            f"<td>{html.escape(it['status_text'])}（{it['status']}）</td>"
            f"<td>{it['latest_reading_at'] or '-'}</td>"
            f"<td>{latest_temp}</td>"
            f"<td>{freshness}</td>"
            f"<td>{it['in_window_minutes']:g} / {it['window']['hold_minutes']:g}</td>"
            f"<td>{it['remaining_hold_minutes']:g}</td>"
            f"<td>{met}</td>"
            f"<td>{html.escape(safe)}</td>"
            f"<td>{blockers}</td>"
            f"<td>{alerts}</td>"
            "</tr>"
        )
    plan_text = {
        "OK": "计划出炉时刻不早于预测安全出炉时刻，计划有效",
        "TOO_EARLY": f"计划出炉过早 {prog.get('planned_unload_early_minutes') or 0:g} 分钟，"
                     "计划出炉时刻已失效，应按预测安全出炉时刻延后",
        "CANNOT_VERIFY": "存在不可预测工件，无法判定计划出炉时刻是否有效",
    }.get(prog.get("plan_status"), "-")
    # 逐件离炉记录：顺序、时刻、判定、强制原因与离炉当时进度快照
    unload_rows = []
    for u in p.get("unload_order", []):
        snap = u.get("progress_snapshot") or {}
        verdict_txt = "合格" if u["verdict"] == "OK" else "不合格"
        if u["forced"]:
            verdict_txt += "（强制出炉）"
        unload_rows.append(
            "<tr>"
            f"<td>{u['sequence']}</td>"
            f"<td>{html.escape(u['workpiece_id'])}</td>"
            f"<td>{html.escape(u['unload_at'])}</td>"
            f"<td>{verdict_txt}</td>"
            f"<td>{html.escape(u['reason'] or '-')}</td>"
            f"<td>{html.escape(str(snap.get('first_met_at') or '-'))}</td>"
            f"<td>{snap.get('in_window_minutes', 0):g} / "
            f"{snap.get('window', {}).get('hold_minutes', 0):g}</td>"
            f"<td>{snap.get('remaining_hold_minutes', 0):g}</td>"
            f"<td>{html.escape('、'.join(u['flags'])) or '-'}</td>"
            "</tr>"
        )
    unload_block = (
        "<h2>逐件离炉记录</h2>"
        "<table>"
        "<tr><th>顺序</th><th>工件</th><th>离炉时刻</th><th>最终判定</th>"
        "<th>强制原因</th><th>首次达标</th><th>离炉时累计/要求 min</th>"
        "<th>剩余 min</th><th>标记</th></tr>"
        f"{''.join(unload_rows)}"
        "</table>"
    ) if p.get("unload_order") else (
        "<h2>逐件离炉记录</h2><p class='small'>尚无工件离炉。</p>")
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
    <th>挂位</th><th>交期</th><th>在区/要求 min</th><th>离炉（顺序/时刻/判定）</th><th>标记</th></tr>
{''.join(rows)}
</table>
<h2>在炉固化进度与安全出炉预测</h2>
<table class="meta">
<tr><td>计算基准时刻：{html.escape(str(basis.get('as_of') or '-'))}（{source_text}）</td>
    <td>预测状态：{html.escape(prog.get('prediction_status_text', '-'))}
        （{html.escape(str(prog.get('prediction_status', '-')))}）</td></tr>
<tr><td>预测安全出炉：{html.escape(str(prog.get('safe_unload_at') or '不可预测'))}</td>
    <td>计划出炉：{html.escape(str(prog.get('planned_unload_at') or '-'))}</td></tr>
<tr><td colspan="2">计划校验：{html.escape(plan_text)}</td></tr>
</table>
<table>
<tr><th>工件</th><th>状态</th><th>最新测温</th><th>最新最低校正 ℃</th>
    <th>读数新鲜度</th><th>累计/要求 min</th><th>剩余 min</th>
    <th>首次达标</th><th>预测安全出炉</th><th>阻塞原因</th><th>告警</th></tr>
{''.join(prog_rows)}
</table>
<h2>探头与判定序列</h2>
{''.join(probe_blocks)}
{unload_block}
<div class="sign"><span>操作工：____________</span><span>检验员：____________</span>
<span>日期：____________</span></div>
</body></html>"""
    return Response(html_doc, mimetype="text/html")
