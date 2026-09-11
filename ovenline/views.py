"""REST API：试算 / 签发 / 入炉 / 测温回传 / 出炉判定 / 返工结案 / 查询下载。

多探头：工件可登记多个金属探头及校准偏移，签发时冻结配置；
测温按 (炉次, 工件, 探头, 时刻) 幂等去重；判定序列取每个采样时刻
有效探头校正温度的最低值；故障探头可在出炉前停用并重算该工件。

校准证书版本化：探头可录入不可覆盖的多点校准版本（证书号、校准/到期
时刻、示值—参考值点列），绑定探头时指定版本；登记探头必须绑定版本
才能签发——签发按计划入炉时刻检查绑定、有效期与粉料温区覆盖并冻结
点列快照；测温按冻结点列线性插值，区间外读数不计入保温累计并产生
CALIBRATION_RANGE 告警；新证书只供未签发炉次使用。
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from . import cooling, probes, progress, racking, scheduler
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
FLAG_EMERGENCY_RELEASE = "EMERGENCY_RELEASE"  # 冷却未达门限紧急搬运，转返工处置

# 校准证书签发检查代码（命中即阻止签发）
CAL_MISSING = "CALIBRATION_MISSING"              # 绑定的校准版本缺失
CAL_NOT_YET_VALID = "CALIBRATION_NOT_YET_VALID"  # 计划入炉时刻证书尚未生效
CAL_EXPIRED = "CALIBRATION_EXPIRED"              # 计划入炉时刻证书已过期
CAL_COVERAGE = "CALIBRATION_COVERAGE"            # 点列区间未覆盖粉料温区

# 停机窗类型（清炉/校准/检修整段占用烘炉）
BLACKOUT_KINDS = {"CLEANING": "清炉", "CALIBRATION": "校准", "MAINTENANCE": "检修"}
_KIND_ALIASES = {v: k for k, v in BLACKOUT_KINDS.items()}

# 吊点禁用类型：临时封位（清炉/检修时挂位不可用）/ 吊点故障
HANGER_BLACKOUT = "BLACKOUT"
HANGER_FAULT = "FAULT"

# 冷却搬运放行类型：正常放行（合格件 -> DONE）/ 紧急搬运（-> 返工处置）
RELEASE_NORMAL = "NORMAL"
RELEASE_EMERGENCY = "EMERGENCY"


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


def _calibration_view(calibration_id, version, certificate_no, calibrated_at,
                      valid_until, points_json, planned_load_at=None):
    """证书版本视图：证书号/版本、插值区间与到期状态（相对计划入炉时刻）。

    绑定关系存在但版本记录缺失（certificate_no 为 NULL）时返回 None，
    由签发检查以 CALIBRATION_MISSING 拦截。
    """
    if calibration_id is None or certificate_no is None:
        return None
    points = json.loads(points_json) if points_json else []
    pairs = [[p["indicated_c"], p["reference_c"]] for p in points]
    expired = False
    if planned_load_at and valid_until:
        expired = (datetime.fromisoformat(planned_load_at)
                   > datetime.fromisoformat(valid_until))
    return {
        "calibration_id": calibration_id,
        "version": version,
        "certificate_no": certificate_no,
        "calibrated_at": calibrated_at,
        "valid_until": valid_until,
        "range_min_c": pairs[0][0] if pairs else None,
        "range_max_c": pairs[-1][0] if pairs else None,
        "points": pairs,
        "expired": expired,
    }


def _probe_cfg_for_item(db, batch_id, workpiece_id, frozen):
    """工件在该炉次中的探头配置。

    frozen=True（已签发及以后）取签发时冻结的快照（即使为空也不再回退主数据）；
    frozen=False（草稿/被取代）取当前登记的探头主数据。
    绑定校准证书版本的探头附带证书视图（证书号/版本/插值区间/到期状态），
    到期状态相对该炉次计划入炉时刻判定（签发检查口径）。
    """
    planned = db.execute("SELECT planned_load_at FROM batches WHERE id=?",
                         (batch_id,)).fetchone()
    planned_load_at = planned["planned_load_at"] if planned else None
    if frozen:
        rows = db.execute(
            "SELECT probe_id, offset_c, status, disabled_reason, disabled_at,"
            " calibration_id, version, certificate_no, calibrated_at, valid_until,"
            " points_json"
            " FROM batch_item_probes WHERE batch_id=? AND workpiece_id=?"
            " ORDER BY probe_id", (batch_id, workpiece_id)).fetchall()
    else:
        rows = db.execute(
            "SELECT p.probe_id, p.offset_c, 'ACTIVE' AS status,"
            " NULL AS disabled_reason, NULL AS disabled_at,"
            " p.calibration_id, c.version, c.certificate_no, c.calibrated_at,"
            " c.valid_until, c.points_json"
            " FROM probes p"
            " LEFT JOIN probe_calibrations c ON c.id = p.calibration_id"
            " WHERE p.workpiece_id=? ORDER BY p.probe_id",
            (workpiece_id,)).fetchall()
    cfg = {}
    for r in rows:
        d = dict(r)
        points_json = d.pop("points_json")
        d["calibration"] = _calibration_view(
            d.pop("calibration_id"), d.pop("version"), d.pop("certificate_no"),
            d.pop("calibrated_at"), d.pop("valid_until"), points_json,
            planned_load_at)
        cfg[d["probe_id"]] = d
    return cfg


def _analyze_cfg(cfg):
    """probes.analyze / progress.project_item 的探头配置视图（含插值点列）。"""
    return {pid: {"offset_c": c["offset_c"], "status": c["status"],
                  "points": (c.get("calibration") or {}).get("points"),
                  "calibration": c.get("calibration")}
            for pid, c in cfg.items()}


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
        _analyze_cfg(cfg),
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
    不合格标记；普通安全出炉：无标记，最终判定 OK，工件进入 **COOLING**
    冷却放行观察（离炉只是走出炉膛，表面仍可能高于包装耐温上限，
    暂不计为完成；冷却正常放行后才 DONE）。
    """
    at_iso = at.isoformat()
    codes = []
    for code, detail in judgment["defects"]:
        _add_flag(db, bid, wid, code, detail)
        codes.append(code)
    # 强制出炉一律标记不合格；合格离炉进入冷却放行（COOLING），不直接完成
    if forced:
        verdict, wstatus = "NOT_OK", "REWORK_PENDING"
    else:
        verdict = "OK" if judgment["safe"] else "NOT_OK"
        wstatus = "COOLING" if judgment["safe"] else "REWORK_PENDING"
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
        _analyze_cfg(cfg),
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


# ---------------------------------------------------------------- 冷却放行

def _cooling_item(db, bid, wid):
    """取炉次工件行（含离炉/放行列）；不存在返回 None。"""
    return db.execute(
        "SELECT * FROM batch_items WHERE batch_id=? AND workpiece_id=?",
        (bid, wid)).fetchone()


def _workpiece_state(db, wid):
    row = db.execute("SELECT status FROM workpieces WHERE id=?",
                     (wid,)).fetchone()
    return row["status"] if row else None


def _cooling_thresholds(db, bid, item):
    """该件冷却放行门限：**签发后只读取签发时冻结的快照**，不回退粉料主数据。

    快照为空（签发时粉料未登记包装门限）即视为门限缺失：冷却进度与 NORMAL
    放行据此给出 PACK_LIMIT_MISSING，永不放行——即使签发后再补改粉料主数据。
    仅未签发草稿（DRAFT/SUPERSEDED）允许回退当前粉料主数据做预览。
    """
    state = db.execute("SELECT state FROM batches WHERE id=?",
                       (bid,)).fetchone()["state"]
    if state not in ("DRAFT", "SUPERSEDED"):
        return item["snap_pack_temp_limit_c"], item["snap_low_temp_hold_minutes"]
    if item["snap_pack_temp_limit_c"] is not None:
        return item["snap_pack_temp_limit_c"], item["snap_low_temp_hold_minutes"]
    r = db.execute(
        "SELECT p.pack_temp_limit_c, p.low_temp_hold_minutes"
        " FROM workpieces w LEFT JOIN powders p ON p.batch_no=w.powder_batch"
        " WHERE w.id=?", (item["workpiece_id"],)).fetchone()
    if r is None:
        return None, None
    return r["pack_temp_limit_c"], r["low_temp_hold_minutes"]


def _cooling_readings(db, bid, wid):
    """冷却表面温度读数（按收录/id 升序，乱序不重排），供区间引擎处理。"""
    rows = db.execute(
        "SELECT ts, surface_temp_c, kind FROM cooling_readings"
        " WHERE batch_id=? AND workpiece_id=? ORDER BY id",
        (bid, wid)).fetchall()
    return [(datetime.fromisoformat(r["ts"]), float(r["surface_temp_c"]),
             r["kind"]) for r in rows]


def _cooling_evaluate(db, bid, item, as_of=None):
    """单件冷却放行评估（cooling.evaluate 结果，附工件/离炉/放行上下文）。"""
    wid = item["workpiece_id"]
    if as_of is None:
        as_of = _now()
    limit, hold = _cooling_thresholds(db, bid, item)
    ev = cooling.evaluate(
        _cooling_readings(db, bid, wid), limit, hold, as_of,
        current_app.config["COOLING_GAP_MINUTES"])
    ev["workpiece_id"] = wid
    ev["batch_id"] = bid
    ev["unloaded_at"] = item["actual_unload_at"]
    ev["release"] = None
    if item["release_kind"] is not None:
        ev["release"] = {"kind": item["release_kind"],
                         "release_at": item["release_at"],
                         "reason": item["release_reason"]}
    return ev


def _cooling_reading_view(db, bid, wid):
    """该件完整冷却表面温度读数（按收录顺序，含乱序/冲突标记）与中断记录。"""
    readings = [
        {"ts": r["ts"], "surface_temp_c": r["surface_temp_c"], "kind": r["kind"],
         "created_at": r["created_at"]}
        for r in db.execute(
            "SELECT ts, surface_temp_c, kind, created_at FROM cooling_readings"
            " WHERE batch_id=? AND workpiece_id=? ORDER BY id",
            (bid, wid)).fetchall()]
    interruptions = [
        {"code": r["code"], "at_ts": r["at_ts"], "detail": r["detail"],
         "created_at": r["created_at"]}
        for r in db.execute(
            "SELECT code, at_ts, detail, created_at FROM cooling_interruptions"
            " WHERE batch_id=? AND workpiece_id=? ORDER BY id",
            (bid, wid)).fetchall()]
    return readings, interruptions


def _item_cooling_view(db, b, it, as_of):
    """炉次详情/档案中的逐件冷却放行视图（冻结门限+完整读数+区间中断+人工决定）。

    未离炉工件返回 None；已放行件取放行时冻结的冷却快照（不再随时间漂移），
    其余（COOLING 中）按 as_of 重算。无论哪种情况都附完整读数与中断记录。
    """
    bid = b["id"]
    wid = it["workpiece_id"]
    if not it["actual_unload_at"]:
        return None
    readings, interruptions = _cooling_reading_view(db, bid, wid)
    release = None
    if it["release_kind"] is not None:
        release = {"kind": it["release_kind"], "release_at": it["release_at"],
                   "reason": it["release_reason"]}
        snapshot = (json.loads(it["release_snapshot_json"])
                    if it["release_snapshot_json"] else None)
    else:
        snapshot = _cooling_evaluate(db, b["id"], it, as_of)
    # 区间中断 = 数据计算（乱序/长间隔/再次升温，随读数可复现）
    # + 落库的同时刻冲突人工中断；按 (code,to) 去重合并
    if snapshot and snapshot.get("interruptions"):
        seen = {(x["code"], x.get("to") or x.get("at_ts"))
                for x in interruptions}
        for x in snapshot["interruptions"]:
            key = (x["code"], x.get("to") or x.get("at_ts"))
            if key not in seen:
                interruptions.append(x)
                seen.add(key)
        interruptions.sort(key=lambda x: x.get("to") or x.get("at_ts") or "")
    return {
        "pack_temp_limit_c": it["snap_pack_temp_limit_c"],
        "low_temp_hold_minutes": it["snap_low_temp_hold_minutes"],
        "unloaded_at": it["actual_unload_at"],
        "release": release,
        "snapshot": snapshot,
        # 完整读数与区间中断：乱序/长间隔/再次升温/同时刻冲突全程可追溯
        "readings": readings,
        "interruptions": interruptions,
    }


def _batch_cooling(db, b, as_of=None):
    """炉次级冷却放行视图：逐件冷却状态 + 放行统计（含已放行/紧急搬运件）。"""
    rows = db.execute(
        "SELECT workpiece_id FROM batch_items WHERE batch_id=?"
        " ORDER BY unload_sequence, hanger_slot", (b["id"],)).fetchall()
    items = []
    for r in rows:
        it = _cooling_item(db, b["id"], r["workpiece_id"])
        items.append(_cooling_evaluate(db, b["id"], it, as_of))
    normal = [i for i in items if (i["release"] or {}).get("kind")
              == RELEASE_NORMAL]
    emergency = [i for i in items if (i["release"] or {}).get("kind")
                 == RELEASE_EMERGENCY]
    cooling_now = [i for i in items
                   if not i["release"] and i["unloaded_at"]]
    waiting = [i for i in cooling_now if not i["releasable"]]
    return {
        "basis": {"as_of": (as_of or _now()).isoformat(timespec="seconds")},
        "cooling_count": len(cooling_now),
        "released_count": len(normal),
        "emergency_count": len(emergency),
        "waiting_count": len(waiting),
        "all_released": not waiting and len(cooling_now) == 0
                        and (len(normal) + len(emergency)) > 0,
        "items": items,
    }


def _batch_payload(db, b, as_of=None, as_of_source=None):
    resolved_as_of, default_source = _resolve_as_of(b, as_of)
    basis_source = as_of_source if as_of is not None else default_source
    # 冷却评估基准时刻：随炉次详情的 as_of 口径；未指定时取当前时刻
    resolved_cooling_as_of = resolved_as_of if as_of is not None else _now()
    items = db.execute(
        "SELECT bi.workpiece_id, bi.hanger_slot, bi.slots_used, w.order_id,"
        " COALESCE(bi.snap_length_mm, w.length_mm) AS length_mm,"
        " COALESCE(bi.snap_width_mm, w.width_mm) AS width_mm,"
        " COALESCE(bi.snap_height_mm, w.height_mm) AS height_mm,"
        " COALESCE(bi.snap_weight_kg, w.weight_kg) AS weight_kg,"
        " COALESCE(bi.snap_powder_batch, w.powder_batch) AS powder_batch,"
        " w.compat_group, w.due_at, w.is_rework, w.status,"
        " bi.actual_unload_at, bi.unload_sequence, bi.first_met_at,"
        " bi.final_verdict, bi.forced, bi.force_reason, bi.progress_snapshot_json,"
        " bi.rod_id, bi.load_in_sequence, bi.placement_json,"
        " bi.snap_pack_temp_limit_c, bi.snap_low_temp_hold_minutes,"
        " bi.release_kind, bi.release_at, bi.release_reason,"
        " bi.release_snapshot_json"
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
               if k not in ("forced", "progress_snapshot_json",
                            "placement_json", "release_snapshot_json")},
            "is_rework": bool(it["is_rework"]),
            "forced": bool(it["forced"]),
            # 吊具布置（挂杆/吊点坐标/旋转/各点载荷/重心；签发后为冻结快照）
            "placement": (json.loads(it["placement_json"])
                          if it["placement_json"] else None),
            "cure": _cure_for_item(db, b, wid),
            "flags": flags,
            "probe_actions": probe_actions,
            # 离炉当时冻结的进度快照（在炉时为 None）
            "progress_snapshot": (json.loads(it["progress_snapshot_json"])
                                  if it["progress_snapshot_json"] else None),
            # 冷却放行：冻结的包装门限、完整表面温度读数与区间中断、
            # 放行时冻结的冷却快照（未离炉/草稿工件为 None）
            "cooling": _item_cooling_view(db, b, it, resolved_cooling_as_of),
        })
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
    # 冷却搬运放行记录（正常放行 / 紧急搬运，含理由与放行时冷却快照）
    release_order = [
        {"release_id": a["id"], "workpiece_id": a["workpiece_id"],
         "kind": a["kind"], "release_at": a["release_at"],
         "reason": a["reason"], "held_low_temp_minutes": a["held_minutes"],
         "snapshot": json.loads(a["snapshot_json"])}
        for a in db.execute(
            "SELECT id, workpiece_id, kind, release_at, reason, held_minutes,"
            " snapshot_json FROM release_actions"
            " WHERE batch_id=? ORDER BY id", (b["id"],)).fetchall()]
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
        # 吊具布置：挂杆/吊点坐标、旋转、各点载荷、分区、横梁总载、力矩、搬入顺序
        # （草稿取当前 arrangement_json；签发后为冻结快照，主数据/新版本不改变它）
        "rack_layout": (json.loads(b["arrangement_json"])
                        if b["arrangement_json"] else None),
        # 停机避让：关联停机窗与排产计算依据（未避让基准时刻/等待分钟/逾期变化）
        "blackout_windows": blackouts,
        "blackout_wait_minutes": b["blackout_wait_minutes"],
        "schedule_basis": (json.loads(b["schedule_basis_json"])
                           if b["schedule_basis_json"] else None),
        "items": out_items,
        "unload_order": unload_order,
        # 冷却放行：冻结门限/完整读数/区间中断/人工决定随档案保留
        "release_order": release_order,
        "cooling": _batch_cooling(db, b, resolved_cooling_as_of),
        # 同一进度快照贯穿炉次详情 / JSON 档案 / 随炉卡
        "progress": progress_snapshot,
    }


# ---------------------------------------------------------------- 吊具布置

def _racks_for(oven_rows):
    """为本次试算的各炉构建挂杆/吊点模型（纯函数 racking.build_rack）。"""
    return {ov["id"]: racking.build_rack(ov) for ov in oven_rows}


def _parse_hanger_blackouts(raw_list, oven_rows):
    """校验试算请求中的吊点禁用时段（临时封掉的挂位）。

    元素：{oven_id, rod_id, point_index, start_at, end_at(可省=开放结束), note}。
    未知炉号 / 挂杆 / 吊点编号、起止倒序一律 400（ValueError）。
    返回 [{oven_id, rod_id, point_index, start_at(datetime), end_at(datetime|None)}]。
    """
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise ValueError("hanger_blackouts 须为数组，元素含"
                         " oven_id/rod_id/point_index/start_at/end_at")
    racks = _racks_for(oven_rows)
    out = []
    for hb in raw_list:
        if not isinstance(hb, dict):
            raise ValueError(f"吊点禁用须为对象: {hb!r}")
        missing = [k for k in ("oven_id", "rod_id", "point_index", "start_at")
                   if k not in hb]
        if missing:
            raise ValueError(f"吊点禁用缺少字段: {missing}")
        oid, rid = hb["oven_id"], str(hb["rod_id"])
        try:
            idx = int(hb["point_index"])
        except (TypeError, ValueError):
            raise ValueError(f"吊点编号须为整数: {hb['point_index']!r}")
        rack = racks.get(oid)
        if rack is None:
            raise ValueError(f"吊点禁用引用未知炉号: {oid!r}")
        if rack.point(rid, idx) is None:
            raise ValueError(f"吊点不存在: 炉 {oid} 挂杆 {rid} 编号 {idx}")
        start = _parse_dt(hb["start_at"], "hanger_blackouts.start_at")
        end = None
        if hb.get("end_at") is not None and str(hb.get("end_at")).strip():
            end = _parse_dt(hb["end_at"], "hanger_blackouts.end_at")
            if end <= start:
                raise ValueError(
                    f"吊点禁用起止倒序: {start.isoformat()} 不早于 {end.isoformat()}")
        out.append({"oven_id": oid, "rod_id": rid, "point_index": idx,
                    "start_at": start, "end_at": end,
                    "note": hb.get("note"), "kind": HANGER_BLACKOUT})
    return out


def _active_faults(db):
    """当前未修复的吊点故障（跨版本持续生效），返回 point_blackouts 结构。

    开放结束（resolved_at 为空）的故障视为从 started_at 起持续禁用；
    end_at 用 None 表示，计时检查时按 +inf 处理。
    """
    rows = db.execute(
        "SELECT oven_id, rod_id, point_index, started_at, reason"
        " FROM hanger_faults WHERE resolved_at IS NULL ORDER BY id").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["oven_id"], []).append({
            "oven_id": r["oven_id"], "rod_id": r["rod_id"],
            "point_index": r["point_index"],
            "start_at": datetime.fromisoformat(r["started_at"]),
            "end_at": None, "note": r["reason"], "kind": HANGER_FAULT})
    return out


def _hanger_conflicts(db, carried, point_windows):
    """吊点禁用/故障与已签发/在炉炉次冻结布置的冲突（这些炉次不得改动）。"""
    conflicts = []
    for c in carried:
        used = {}
        for bi in db.execute(
                "SELECT workpiece_id, rod_id, placement_json FROM batch_items"
                " WHERE batch_id=?", (c["id"],)).fetchall():
            if not bi["placement_json"]:
                continue
            plc = json.loads(bi["placement_json"])
            for p in plc.get("occupied_points", []):
                used[(plc.get("rod_id"), p["index"])] = bi["workpiece_id"]
        for pb in point_windows.get(c["oven_id"], []):
            hit = used.get((pb["rod_id"], pb["point_index"]))
            if hit:
                conflicts.append({
                    "batch_id": c["id"], "batch_state": c["state"],
                    "oven_id": c["oven_id"], "rod_id": pb["rod_id"],
                    "point_index": pb["point_index"], "kind": pb["kind"],
                    "workpiece_id": hit,
                    "note": pb.get("note"),
                    "started_at": pb["start_at"].isoformat(timespec="seconds"),
                    "end_at": pb["end_at"].isoformat(timespec="seconds")
                              if pb["end_at"] else None,
                    "detail": "禁用吊点被已签发炉次占用，该炉次布置冻结不可重排",
                })
    return conflicts


def _point_blackout_json(pb):
    return {"oven_id": pb["oven_id"], "rod_id": pb["rod_id"],
            "point_index": pb["point_index"], "kind": pb.get("kind",
                                                             HANGER_BLACKOUT),
            "start_at": pb["start_at"].isoformat(timespec="seconds"),
            "end_at": pb["end_at"].isoformat(timespec="seconds")
                      if pb["end_at"] else None, "note": pb.get("note")}


def _merge_point_blackouts(version_windows, active_faults):
    """合并试算级禁用时段与持续吊点故障（开放结束按 +inf 参与区间相交）。"""
    merged = {}
    for oid, lst in version_windows.items():
        merged[oid] = list(lst)
    for oid, lst in active_faults.items():
        merged.setdefault(oid, [])
        existing = {(w["rod_id"], w["point_index"], w["start_at"]) for w in merged[oid]}
        for w in lst:
            key = (w["rod_id"], w["point_index"], w["start_at"])
            if key not in existing:
                merged[oid].append(w)
    return merged


def _wp_kwargs(o):
    """订单中的吊具布置字段（重心/旋转/吊耳/净距），缺省给默认值。"""
    def _num(key, default=0.0):
        v = o.get(key, default)
        return float(v) if v is not None else default
    rots = o.get("allowed_rotations_deg")
    if rots is None:
        rots_json = None
    else:
        if not isinstance(rots, list) or not rots:
            raise ValueError("allowed_rotations_deg 须为非空角度数组")
        rots_json = json.dumps(sorted({int(x) for x in rots}))
    lugs = o.get("lift_points_mm")
    if lugs is not None:
        if not isinstance(lugs, list) or not lugs:
            raise ValueError("lift_points_mm 须为非空坐标数组")
        lugs = [float(x) for x in lugs]
    return {
        "cg_offset_x_mm": _num("cg_offset_x_mm"),
        "cg_offset_y_mm": _num("cg_offset_y_mm"),
        "allowed_rotations": rots_json,
        "lift_points_json": json.dumps(lugs) if lugs is not None else None,
        "clearance_mm": _num("clearance_mm"),
    }


def _wp_rack_fields(row):
    """数据库工件行 → racking 引擎使用的吊具字段 dict。"""
    return {
        "cg_offset_x_mm": row["cg_offset_x_mm"] or 0.0,
        "cg_offset_y_mm": row["cg_offset_y_mm"] or 0.0,
        "allowed_rotations_deg": (json.loads(row["allowed_rotations"])
                                  if row["allowed_rotations"] else None),
        "lift_points_mm": (json.loads(row["lift_points_json"])
                           if row["lift_points_json"] else None),
        "clearance_mm": row["clearance_mm"] or 0.0,
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
        # 挂杆/吊点/横梁模型先构建一次：结构非法（重复挂杆、未知轴等）立即拒绝
        try:
            racking.build_rack(ov)
        except ValueError as e:
            return _err(400, f"炉 {ov.get('id')} 吊具模型无效: {e}")
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
            # 挂杆/吊点坐标/单点限载/分区载荷/偏载容差（随版本参数快照存档）
            "hanger_rack": ov.get("hanger_rack"),
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
            {k: v for k, v in row.items() if k != "hanger_rack"},
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

    # 吊点禁用时段（临时封掉的挂位）：未知炉号/挂杆/吊点、倒序一律拒绝
    try:
        hanger_blackouts = _parse_hanger_blackouts(
            data.get("hanger_blackouts", []), oven_rows)
    except ValueError as e:
        return _err(400, str(e))
    hb_by_oven = {}
    for hb in hanger_blackouts:
        hb_by_oven.setdefault(hb["oven_id"], []).append(hb)

    # 粉料主数据 upsert
    powder_req = []
    for p in data["powders"]:
        missing = [k for k in ("batch_no", "temp_min_c", "temp_max_c", "hold_minutes")
                   if k not in p]
        if missing:
            return _err(400, f"粉料参数缺少字段: {missing}")
        if float(p["temp_min_c"]) > float(p["temp_max_c"]):
            return _err(400, f"粉料 {p['batch_no']} 温度下限高于上限")
        # 包装冷却门限（可选）：两者须同时给出且为非负有限数
        pack_limit = p.get("pack_temp_limit_c")
        hold_low = p.get("low_temp_hold_minutes")
        if pack_limit is not None:
            try:
                pack_limit = float(pack_limit)
            except (TypeError, ValueError):
                return _err(400, f"粉料 {p['batch_no']} 的 pack_temp_limit_c 不是数字")
        if hold_low is not None:
            try:
                hold_low = float(hold_low)
            except (TypeError, ValueError):
                return _err(400, f"粉料 {p['batch_no']} 的 low_temp_hold_minutes 不是数字")
        if (pack_limit is None) != (hold_low is None):
            return _err(400, f"粉料 {p['batch_no']} 的包装温度上限 pack_temp_limit_c"
                             " 与低温保持时长 low_temp_hold_minutes 须同时给出")
        if pack_limit is not None and hold_low is not None and \
                (pack_limit < 0 or hold_low < 0):
            return _err(400, f"粉料 {p['batch_no']} 的包装冷却门限不能为负数")
        row = {"batch_no": p["batch_no"], "temp_min_c": float(p["temp_min_c"]),
               "temp_max_c": float(p["temp_max_c"]), "hold_minutes": float(p["hold_minutes"]),
               "pack_temp_limit_c": pack_limit, "low_temp_hold_minutes": hold_low}
        db.execute(
            "INSERT INTO powders (batch_no, temp_min_c, temp_max_c, hold_minutes,"
            " pack_temp_limit_c, low_temp_hold_minutes)"
            " VALUES (:batch_no, :temp_min_c, :temp_max_c, :hold_minutes,"
            " :pack_temp_limit_c, :low_temp_hold_minutes)"
            " ON CONFLICT(batch_no) DO UPDATE SET"
            " temp_min_c=excluded.temp_min_c, temp_max_c=excluded.temp_max_c,"
            " hold_minutes=excluded.hold_minutes,"
            " pack_temp_limit_c=excluded.pack_temp_limit_c,"
            " low_temp_hold_minutes=excluded.low_temp_hold_minutes",
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
            try:
                rk = _wp_kwargs(o)
            except ValueError as e:
                return _err(400, f"工件 {o['workpiece_id']} 吊具参数无效: {e}")
            db.execute(
                "INSERT INTO workpieces (id, order_id, length_mm, width_mm, height_mm,"
                " weight_kg, powder_batch, compat_group, due_at, status,"
                " cg_offset_x_mm, cg_offset_y_mm, allowed_rotations,"
                " lift_points_json, clearance_mm)"
                " VALUES (?,?,?,?,?,?,?,?,?,'PENDING',?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " order_id=excluded.order_id, length_mm=excluded.length_mm,"
                " width_mm=excluded.width_mm, height_mm=excluded.height_mm,"
                " weight_kg=excluded.weight_kg, powder_batch=excluded.powder_batch,"
                " compat_group=excluded.compat_group, due_at=excluded.due_at,"
                " cg_offset_x_mm=excluded.cg_offset_x_mm,"
                " cg_offset_y_mm=excluded.cg_offset_y_mm,"
                " allowed_rotations=excluded.allowed_rotations,"
                " lift_points_json=excluded.lift_points_json,"
                " clearance_mm=excluded.clearance_mm",
                (o["workpiece_id"], o.get("order_id"), float(o["length_mm"]),
                 float(o["width_mm"]), float(o["height_mm"]), float(o["weight_kg"]),
                 o["powder_batch"], o.get("compat_group"), due,
                 rk["cg_offset_x_mm"], rk["cg_offset_y_mm"],
                 rk["allowed_rotations"], rk["lift_points_json"],
                 rk["clearance_mm"]),
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
        # 吊点禁用时段（临时封位）随版本快照存档
        "hanger_blackouts": [_point_blackout_json(h) for h in hanger_blackouts],
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
    for hb in hanger_blackouts:
        db.execute(
            "INSERT INTO hanger_blackouts (version_id, oven_id, rod_id,"
            " point_index, start_at, end_at, kind, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (version_id, hb["oven_id"], hb["rod_id"], hb["point_index"],
             hb["start_at"].isoformat(timespec="seconds"),
             hb["end_at"].isoformat(timespec="seconds") if hb["end_at"] else None,
             HANGER_BLACKOUT, hb.get("note"), _now().isoformat()),
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
    pending = []
    for r in db.execute(
            "SELECT * FROM workpieces WHERE status='PENDING' ORDER BY id").fetchall():
        if r["id"] in rejected_ids:
            continue
        wd = dict(r)
        wd.update(_wp_rack_fields(r))
        pending.append(wd)
    # 吊具布置：挂杆/吊点模型 + 试算级禁用时段 + 未修复吊点故障（持续生效）
    racks = _racks_for(oven_rows)
    active_faults = _active_faults(db)
    point_blackouts = _merge_point_blackouts(hb_by_oven, active_faults)
    planned, unscheduled = scheduler.build_plan(
        pending, powder_all, oven_rows, forbidden, start_at, busy_until,
        blackouts=windows_by_oven, point_blackouts=point_blackouts,
        racks=racks)

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
            " schedule_basis_json, arrangement_json, created_at)"
            " VALUES (?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, b["oven_id"], b["window_min_c"], b["window_max_c"],
             b["hold_minutes"], b["total_weight_kg"], b["heatup_minutes"],
             b["planned_load_at"], b["planned_cure_start_at"], b["planned_unload_at"],
             b["blackout_wait_minutes"], json.dumps(basis, ensure_ascii=False),
             json.dumps(b["rack_layout"], ensure_ascii=False),
             _now().isoformat()),
        )
        bid = cur.lastrowid
        for it in b["items"]:
            db.execute(
                "INSERT INTO batch_items (batch_id, workpiece_id, hanger_slot,"
                " slots_used, rod_id, load_in_sequence, placement_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (bid, it["workpiece_id"], it["hanger_slot"], it["slots_used"],
                 it["placement"]["rod_id"], it["load_in_sequence"],
                 json.dumps(it["placement"], ensure_ascii=False)),
            )
            db.execute("UPDATE workpieces SET status='SCHEDULED' WHERE id=?",
                       (it["workpiece_id"],))
        new_batches.append({"batch_id": bid, "state": "DRAFT", **b})
    # 放不下工件的首个冲突约束 / 逐炉拒绝明细 / 可选炉随版本存档
    for u in unscheduled:
        db.execute(
            "INSERT INTO schedule_rejections (version_id, workpiece_id, reason,"
            " detail, first_conflict_json, per_oven_json, alternative_ovens_json,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (version_id, u["workpiece_id"], u["reason"], u.get("detail"),
             json.dumps(u.get("first_conflict"), ensure_ascii=False),
             json.dumps(u.get("per_oven", []), ensure_ascii=False),
             json.dumps(u.get("alternative_ovens", []), ensure_ascii=False),
             _now().isoformat()))
    db.commit()

    carried = db.execute(
        "SELECT id, oven_id, state, planned_load_at, planned_unload_at FROM batches"
        " WHERE state IN ('ISSUED','IN_OVEN') ORDER BY id").fetchall()
    # 新停机窗撞上已签发/在炉炉次：这些炉次不得改时刻，列出重叠区间与冲突分钟
    turnaround_by_oven = {ov["id"]: float(ov["turnaround_minutes"])
                          for ov in oven_rows}
    conflicts = _blackout_conflicts(carried, windows_by_oven, turnaround_by_oven)
    # 吊点禁用/故障撞上已签发/在炉炉次的冻结布置：只告警，炉次与吊点都不改
    hanger_conflicts = _hanger_conflicts(db, carried, point_blackouts)
    return jsonify({
        "version": {"id": version_id, "parent_id": parent["id"] if parent else None,
                    "reason": data.get("reason", "")},
        "carried_batches": [dict(c) for c in carried],
        "new_batches": new_batches,
        "unscheduled": pre_unscheduled + unscheduled,
        "blackout_windows": [_window_json(w) for w in blackouts],
        "blackout_conflicts": conflicts,
        "hanger_blackouts": [_point_blackout_json(h) for h in hanger_blackouts],
        "active_point_faults": [_point_blackout_json(w)
                                for lst in active_faults.values() for w in lst],
        "hanger_conflicts": hanger_conflicts,
        "oven_timelines": _oven_timelines(oven_rows, carried, new_batches,
                                          windows_by_oven),
    }), 201


# ---------------------------------------------------------------- 吊点故障

def _draft_snapshot(db):
    """重排前各草稿炉次的工件布置（迁移工件/交期变化对照用）。"""
    snap = {}
    for r in db.execute(
            "SELECT b.id AS batch_id, b.oven_id, b.planned_load_at,"
            " b.planned_unload_at, bi.workpiece_id, bi.rod_id,"
            " bi.hanger_slot, bi.slots_used"
            " FROM batches b JOIN batch_items bi ON bi.batch_id=b.id"
            " WHERE b.state='DRAFT'").fetchall():
        snap[r["workpiece_id"]] = dict(r)
    return snap


def _migration_report(db, old, new_batches, unscheduled):
    """吊点故障重排前后对照：迁移工件、挂位/炉号/时刻与交期变化。"""
    new_pos = {}
    for b in new_batches:
        for it in b["items"]:
            new_pos[it["workpiece_id"]] = b
    migrations = []
    for wid, prev in sorted(old.items()):
        nb = new_pos.get(wid)
        if nb is None:
            migrations.append({
                "workpiece_id": wid, "moved": True,
                "from_oven_id": prev["oven_id"], "to_oven_id": None,
                "from_batch_id": prev["batch_id"], "to_batch_id": None,
                "from_rod_id": prev["rod_id"], "to_rod_id": None,
                "from_slot": prev["hanger_slot"], "to_slot": None,
                "planned_unload_at": None,
                "unload_delta_minutes": None,
                "detail": "重排后无法布置（见 unscheduled）"})
            continue
        moved = (nb["oven_id"] != prev["oven_id"]
                 or nb["planned_unload_at"] != prev["planned_unload_at"])
        # 挂位是否变化（同炉次内由 arrangement 比较）
        to_item = next(i for i in nb["items"] if i["workpiece_id"] == wid)
        to_rod = to_item["placement"]["rod_id"]
        to_slot = to_item["hanger_slot"]
        if not moved:
            moved = (to_rod != prev["rod_id"] or to_slot != prev["hanger_slot"])
        old_dt = _parse_dt(prev["planned_unload_at"], "planned_unload_at")
        new_dt = _parse_dt(nb["planned_unload_at"], "planned_unload_at")
        delta = round((new_dt - old_dt).total_seconds() / 60.0, 2)
        migrations.append({
            "workpiece_id": wid, "moved": moved,
            "from_oven_id": prev["oven_id"], "to_oven_id": nb["oven_id"],
            "from_batch_id": prev["batch_id"],
            "to_batch_id": nb["batch_id"],
            "from_rod_id": prev["rod_id"], "to_rod_id": to_rod,
            "from_slot": prev["hanger_slot"], "to_slot": to_slot,
            "from_planned_unload_at": prev["planned_unload_at"],
            "planned_unload_at": nb["planned_unload_at"],
            "unload_delta_minutes": delta,
            "late": to_item.get("late", False),
            "detail": "布置/时刻不变" if not moved
                      else ("炉号或出炉时刻变化" if delta else "同炉时刻不变，挂位变化")})
    # 此前不在草稿（PENDING 等）但本次新排入的工件不视为迁移对象
    return migrations


@bp.post("/schedule/point-fault")
def point_fault():
    """登记吊点故障并重排：只重排未签发（DRAFT）炉次。

    body: {oven_id, rod_id, point_index, reason, started_at?, end_at?}
    - 故障跨版本持续生效（hanger_faults，直到 /schedule/point-fault/resolve）；
    - 已签发/在炉炉次布置冻结：若其占用该吊点，列入 frozen_conflicts 且不动；
    - 旧草稿作废，按「最新版本炉架参数 + 待排产工件 + 该故障」重新试算生成
      新版本；响应给出迁移工件（炉号/挂杆/挂位/出炉时刻/交期变化）。
    """
    data = request.get_json(silent=True) or {}
    required = ("oven_id", "rod_id", "point_index", "reason")
    missing = [k for k in required if k not in data]
    if missing:
        return _err(400, f"缺少字段: {missing}")
    if not str(data.get("reason") or "").strip():
        return _err(400, "吊点故障必须填写原因 reason")
    db = get_db()
    oid = data["oven_id"]
    rid = str(data["rod_id"])
    try:
        idx = int(data["point_index"])
    except (TypeError, ValueError):
        return _err(400, f"吊点编号须为整数: {data['point_index']!r}")
    try:
        started = (_parse_dt(data["started_at"], "started_at")
                   if data.get("started_at") else _now())
        end = (_parse_dt(data["end_at"], "end_at")
               if data.get("end_at") else None)
        if end is not None and end <= started:
            return _err(400, "故障结束时刻不早于开始时刻")
    except ValueError as e:
        return _err(400, str(e))
    # 以最新版本快照中的炉架校验挂杆/吊点存在
    latest_v = db.execute(
        "SELECT id, params_json FROM schedule_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    params = json.loads(latest_v["params_json"]) if latest_v else {}
    oven = next((o for o in params.get("ovens", []) if o["id"] == oid), None)
    if oven is None:
        return _err(404, f"最新排产版本中没有炉 {oid}，请先试算该炉")
    rack = racking.build_rack(oven)
    if rack.point(rid, idx) is None:
        return _err(400, f"吊点不存在: 炉 {oid} 挂杆 {rid} 编号 {idx}")
    # 重复登记同一未修复故障 → 幂等返回
    dup = db.execute(
        "SELECT * FROM hanger_faults WHERE oven_id=? AND rod_id=?"
        " AND point_index=? AND resolved_at IS NULL",
        (oid, rid, idx)).fetchone()
    if dup is not None:
        return _err(409, "该吊点已有未修复故障登记",
                    fault_id=dup["id"], reason=dup["reason"],
                    started_at=dup["started_at"])
    cur = db.execute(
        "INSERT INTO hanger_faults (oven_id, rod_id, point_index, reason,"
        " started_at, created_at) VALUES (?,?,?,?,?,?)",
        (oid, rid, idx, str(data["reason"]), started.isoformat(),
         _now().isoformat()))
    fault_id = cur.lastrowid

    # ---- 基于最新版本参数重建试算（orders 取当前待排产/草稿工件主数据）----
    old_draft = _draft_snapshot(db)
    oven_rows = params.get("ovens", [])
    powder_req = params.get("powders", [])
    forbidden = set(tuple(p) for p in params.get("forbidden_pairs", []))
    start_at = _parse_dt(params["start_at"], "start_at") \
        if params.get("start_at") else _now()
    try:
        blackouts = _parse_blackouts(params.get("blackout_windows", []),
                                     {o["id"] for o in oven_rows})
        version_hb = _parse_hanger_blackouts(
            params.get("hanger_blackouts", []), oven_rows)
    except ValueError as e:
        return _err(400, f"上一版本快照参数无法重算: {e}")
    # 旧草稿作废，工件回到待排产（与试算一致）
    for d in db.execute("SELECT id FROM batches WHERE state='DRAFT'").fetchall():
        db.execute("UPDATE batches SET state='SUPERSEDED' WHERE id=?", (d["id"],))
        db.execute(
            "UPDATE workpieces SET status='PENDING' WHERE status='SCHEDULED'"
            " AND id IN (SELECT workpiece_id FROM batch_items WHERE batch_id=?)",
            (d["id"],))
    snapshot = {
        "reason": f"吊点故障重排: 炉 {oid} 挂杆 {rid} 吊点 {idx}（{data['reason']}）",
        "start_at": start_at.isoformat(),
        "forbidden_pairs": sorted(list(p) for p in forbidden),
        "ovens": oven_rows, "powders": powder_req,
        "blackout_windows": params.get("blackout_windows", []),
        "hanger_blackouts": params.get("hanger_blackouts", []),
        "trigger": {"type": "POINT_FAULT", "fault_id": fault_id,
                    "oven_id": oid, "rod_id": rid, "point_index": idx},
    }
    vcur = db.execute(
        "INSERT INTO schedule_versions (parent_id, reason, params_json, created_at)"
        " VALUES (?,?,?,?)",
        (latest_v["id"] if latest_v else None, snapshot["reason"],
         json.dumps(snapshot, ensure_ascii=False), _now().isoformat()))
    version_id = vcur.lastrowid
    for w in blackouts:
        db.execute(
            "INSERT INTO blackout_windows (version_id, oven_id, kind, start_at,"
            " end_at, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (version_id, w["oven_id"], w["kind"],
             w["start_at"].isoformat(timespec="seconds"),
             w["end_at"].isoformat(timespec="seconds"),
             w.get("note"), _now().isoformat()))
    # 试算级吊点禁用与全部未修复故障（含本次）都写入版本快照
    all_faults = _active_faults(db)
    for hb in version_hb:
        db.execute(
            "INSERT INTO hanger_blackouts (version_id, oven_id, rod_id,"
            " point_index, start_at, end_at, kind, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (version_id, hb["oven_id"], hb["rod_id"], hb["point_index"],
             hb["start_at"].isoformat(timespec="seconds"),
             hb["end_at"].isoformat(timespec="seconds") if hb["end_at"] else None,
             HANGER_BLACKOUT, hb.get("note"), _now().isoformat()))
    for flst in all_faults.values():
        for pb in flst:
            link = (fault_id if pb["oven_id"] == oid and pb["rod_id"] == rid
                    and pb["point_index"] == idx
                    and pb["start_at"] == started else None)
            db.execute(
                "INSERT INTO hanger_blackouts (version_id, fault_id, oven_id,"
                " rod_id, point_index, start_at, end_at, kind, note, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (version_id, link,
                 pb["oven_id"], pb["rod_id"], pb["point_index"],
                 pb["start_at"].isoformat(timespec="seconds"), None,
                 HANGER_FAULT, pb.get("note"), _now().isoformat()))

    busy_until = {}
    for c in db.execute(
            "SELECT oven_id, planned_unload_at FROM batches"
            " WHERE state IN ('ISSUED','IN_OVEN')").fetchall():
        until = _parse_dt(c["planned_unload_at"], "planned_unload_at")
        ov = db.execute("SELECT turnaround_minutes FROM ovens WHERE id=?",
                        (c["oven_id"],)).fetchone()
        if ov:
            until += timedelta(minutes=ov["turnaround_minutes"])
        if c["oven_id"] not in busy_until or until > busy_until[c["oven_id"]]:
            busy_until[c["oven_id"]] = until
    powder_all = {r["batch_no"]: dict(r)
                  for r in db.execute("SELECT * FROM powders").fetchall()}
    pending = []
    for r in db.execute(
            "SELECT * FROM workpieces WHERE status='PENDING' ORDER BY id").fetchall():
        wd = dict(r)
        wd.update(_wp_rack_fields(r))
        pending.append(wd)
    racks = _racks_for(oven_rows)
    windows_by_oven = {}
    for w in blackouts:
        windows_by_oven.setdefault(w["oven_id"], []).append(w)
    planned, unscheduled = scheduler.build_plan(
        pending, powder_all, oven_rows, forbidden, started, busy_until,
        blackouts=windows_by_oven, point_blackouts=all_faults, racks=racks)

    new_batches = []
    for b in planned:
        basis = {k: b[k] for k in (
            "baseline_load_at", "baseline_unload_at", "blackout_wait_minutes",
            "avoided_windows", "earliest_due_at", "lateness_minutes",
            "baseline_lateness_minutes", "lateness_delta_minutes",
            "turnaround_end_at")}
        cc = db.execute(
            "INSERT INTO batches (version_id, oven_id, state, window_min_c,"
            " window_max_c, hold_minutes, total_weight_kg, heatup_minutes,"
            " planned_load_at, planned_cure_start_at, planned_unload_at,"
            " blackout_wait_minutes, schedule_basis_json, arrangement_json,"
            " created_at) VALUES (?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, b["oven_id"], b["window_min_c"], b["window_max_c"],
             b["hold_minutes"], b["total_weight_kg"], b["heatup_minutes"],
             b["planned_load_at"], b["planned_cure_start_at"],
             b["planned_unload_at"], b["blackout_wait_minutes"],
             json.dumps(basis, ensure_ascii=False),
             json.dumps(b["rack_layout"], ensure_ascii=False),
             _now().isoformat()))
        nbid = cc.lastrowid
        for it in b["items"]:
            db.execute(
                "INSERT INTO batch_items (batch_id, workpiece_id, hanger_slot,"
                " slots_used, rod_id, load_in_sequence, placement_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (nbid, it["workpiece_id"], it["hanger_slot"], it["slots_used"],
                 it["placement"]["rod_id"], it["load_in_sequence"],
                 json.dumps(it["placement"], ensure_ascii=False)))
            db.execute("UPDATE workpieces SET status='SCHEDULED' WHERE id=?",
                       (it["workpiece_id"],))
        new_batches.append({"batch_id": nbid, "state": "DRAFT", **b})
    for u in unscheduled:
        db.execute(
            "INSERT INTO schedule_rejections (version_id, workpiece_id, reason,"
            " detail, first_conflict_json, per_oven_json, alternative_ovens_json,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (version_id, u["workpiece_id"], u["reason"], u.get("detail"),
             json.dumps(u.get("first_conflict"), ensure_ascii=False),
             json.dumps(u.get("per_oven", []), ensure_ascii=False),
             json.dumps(u.get("alternative_ovens", []), ensure_ascii=False),
             _now().isoformat()))
    db.commit()

    carried = db.execute(
        "SELECT id, oven_id, state, planned_load_at, planned_unload_at FROM batches"
        " WHERE state IN ('ISSUED','IN_OVEN') ORDER BY id").fetchall()
    frozen = _hanger_conflicts(db, carried, all_faults)
    migrations = _migration_report(db, old_draft, new_batches, unscheduled)
    return jsonify({
        "fault": {"fault_id": fault_id, "oven_id": oid, "rod_id": rid,
                  "point_index": idx, "reason": data["reason"],
                  "started_at": started.isoformat(timespec="seconds")},
        "version": {"id": version_id,
                    "parent_id": latest_v["id"] if latest_v else None,
                    "reason": snapshot["reason"]},
        "carried_batches": [dict(c) for c in carried],
        "new_batches": new_batches,
        "unscheduled": unscheduled,
        "frozen_conflicts": frozen,
        "migrations": migrations,
    }), 201


@bp.post("/schedule/point-fault/<int:fault_id>/resolve")
def resolve_point_fault(fault_id):
    """修复吊点故障：标记 resolved，之后试算不再禁用该吊点（历史记录保留）。"""
    db = get_db()
    f = db.execute("SELECT * FROM hanger_faults WHERE id=?",
                   (fault_id,)).fetchone()
    if f is None:
        return _err(404, f"吊点故障 {fault_id} 不存在")
    if f["resolved_at"] is not None:
        return _err(409, "该吊点故障已修复", resolved_at=f["resolved_at"])
    data = request.get_json(silent=True) or {}
    at = _parse_dt(data["at"], "at") if data.get("at") else _now()
    db.execute("UPDATE hanger_faults SET resolved_at=?, resolve_note=? WHERE id=?",
               (at.isoformat(), data.get("note"), fault_id))
    db.commit()
    return jsonify({"fault_id": fault_id, "resolved_at": at.isoformat(),
                    "note": data.get("note")})


@bp.get("/schedule/point-faults")
def list_point_faults():
    """吊点故障清单（active=仅未修复，默认全部）。"""
    db = get_db()
    active = request.args.get("active", "1") != "0"
    sql = ("SELECT id, oven_id, rod_id, point_index, reason, started_at,"
           " resolved_at, resolve_note FROM hanger_faults")
    if active:
        sql += " WHERE resolved_at IS NULL"
    sql += " ORDER BY id"
    return jsonify({"faults": [dict(r) for r in db.execute(sql).fetchall()]})


# ---------------------------------------------------------------- 状态机动作

def _pack_threshold_problems(db, b):
    """签发前包装冷却门限检查：粉料须同时有包装温度上限与低温保持时长。

    缺一门限即逐条列出（PACK_LIMIT_MISSING）阻止签发；门限在签发时冻结，
    签发后即使补改粉料主数据，本炉次快照仍为空（不得回退主数据放行）。
    """
    rows = db.execute(
        "SELECT bi.workpiece_id, w.powder_batch, p.pack_temp_limit_c,"
        " p.low_temp_hold_minutes"
        " FROM batch_items bi JOIN workpieces w ON w.id=bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no=w.powder_batch"
        " WHERE bi.batch_id=? ORDER BY bi.hanger_slot", (b["id"],)).fetchall()
    problems = []
    for r in rows:
        missing = []
        if r["pack_temp_limit_c"] is None:
            missing.append("pack_temp_limit_c")
        if r["low_temp_hold_minutes"] is None:
            missing.append("low_temp_hold_minutes")
        if missing:
            problems.append({
                "workpiece_id": r["workpiece_id"],
                "powder_batch": r["powder_batch"],
                "code": "PACK_LIMIT_MISSING",
                "missing_fields": missing,
                "detail": f"粉料 {r['powder_batch']} 缺少包装冷却门限 "
                          f"{', '.join(missing)}（包装温度上限/低温保持时长须"
                          "同时登记），签发后将无冻结门限可用"})
    return problems


def _calibration_problems(db, b):
    """签发前校准检查：逐工件逐探头核对证书绑定、有效期与粉料温区覆盖。

    炉内工件登记的每个探头都必须绑定校准证书版本（不再回退固定偏移）：
    未绑定 / 版本缺失 / 尚未生效 / 已过期 / 点列区间未覆盖该工件粉料
    固化窗口，均按计划入炉时刻判定，列出并阻止签发。
    未登记探头的工件（隐式通道）不参与本检查。
    """
    planned_load = datetime.fromisoformat(b["planned_load_at"])
    items = db.execute(
        "SELECT bi.workpiece_id, p.temp_min_c, p.temp_max_c"
        " FROM batch_items bi JOIN workpieces w ON w.id = bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE bi.batch_id=? ORDER BY bi.hanger_slot", (b["id"],)).fetchall()
    problems = []
    for it in items:
        wid = it["workpiece_id"]
        bound = db.execute(
            "SELECT p.probe_id, p.calibration_id, c.version, c.certificate_no,"
            " c.calibrated_at, c.valid_until, c.points_json"
            " FROM probes p"
            " LEFT JOIN probe_calibrations c ON c.id = p.calibration_id"
            " WHERE p.workpiece_id=?"
            " ORDER BY p.probe_id", (wid,)).fetchall()
        for pr in bound:
            base = {"workpiece_id": wid, "probe_id": pr["probe_id"],
                    "calibration_id": pr["calibration_id"],
                    "planned_load_at": b["planned_load_at"]}
            if pr["calibration_id"] is None:
                problems.append({
                    **base, "code": CAL_MISSING,
                    "detail": f"探头 {pr['probe_id']} 未绑定校准版本"
                              "（登记探头时须指定 calibration_id）"})
                continue
            if pr["certificate_no"] is None:
                problems.append({
                    **base, "code": CAL_MISSING,
                    "detail": f"探头 {pr['probe_id']} 绑定的校准版本"
                              f" {pr['calibration_id']} 不存在"})
                continue
            base.update({"certificate_no": pr["certificate_no"],
                         "version": pr["version"],
                         "calibrated_at": pr["calibrated_at"],
                         "valid_until": pr["valid_until"]})
            if planned_load < datetime.fromisoformat(pr["calibrated_at"]):
                problems.append({
                    **base, "code": CAL_NOT_YET_VALID,
                    "detail": f"证书 {pr['certificate_no']} 于"
                              f" {pr['calibrated_at']} 才生效，"
                              f"计划入炉 {b['planned_load_at']} 尚未生效"})
                continue
            if planned_load > datetime.fromisoformat(pr["valid_until"]):
                problems.append({
                    **base, "code": CAL_EXPIRED,
                    "detail": f"证书 {pr['certificate_no']} 已于"
                              f" {pr['valid_until']} 到期，"
                              f"计划入炉 {b['planned_load_at']} 已过期"})
                continue
            # 粉料温区覆盖：点列示值区间须覆盖该工件粉料固化窗口
            if it["temp_min_c"] is None or it["temp_max_c"] is None:
                continue  # 粉料未登记时无窗口可查（该工件本不应入炉次）
            pts = json.loads(pr["points_json"])
            lo, hi = pts[0]["indicated_c"], pts[-1]["indicated_c"]
            if lo > it["temp_min_c"] or hi < it["temp_max_c"]:
                problems.append({
                    **base, "code": CAL_COVERAGE,
                    "range_min_c": lo, "range_max_c": hi,
                    "window_min_c": it["temp_min_c"],
                    "window_max_c": it["temp_max_c"],
                    "detail": f"证书 {pr['certificate_no']} 点列区间"
                              f" {lo:g}–{hi:g}℃ 未覆盖粉料温区"
                              f" {it['temp_min_c']:g}–{it['temp_max_c']:g}℃"})
    return problems


@bp.post("/batches/<int:bid>/issue")
def issue(bid):
    """签发：DRAFT -> ISSUED，签发后炉次冻结，不再参与重排。

    签发前校验炉内工件登记的每个探头都已绑定校准证书版本，并按计划
    入炉时刻检查有效期与粉料温区覆盖；未绑定/缺失/未生效/过期/覆盖
    不足时列出相关工件和探头并阻止签发（不再回退固定偏移放行）。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    if b["state"] not in TRANSITIONS["issue"][0]:
        return _err(409, f"炉次状态为 {b['state']}，不能签发（要求 DRAFT）", state=b["state"])
    # 包装冷却门限检查：粉料必须同时登记包装温度上限与低温保持时长，
    # 否则冻结快照为空，合格离炉件将永远无法正常放行——签发即明确拒绝。
    # REQUIRE_PACK_LIMIT_AT_ISSUE=False 时允许签发（快照为空：冷却查询/
    # NORMAL 放行以 PACK_LIMIT_MISSING 阻塞，且永不回退后来修改的主数据）。
    if current_app.config.get("REQUIRE_PACK_LIMIT_AT_ISSUE", True):
        pack_problems = _pack_threshold_problems(db, b)
        if pack_problems:
            return _err(409, "粉料缺少包装温度上限/低温保持时长，"
                             "无法冻结冷却放行门限，阻止签发（补齐粉料资料后重试）",
                        state=b["state"], pack_threshold_problems=pack_problems)
    # 校准证书检查：登记探头须绑定证书版本，且在计划入炉时刻有效、覆盖粉料温区
    problems = _calibration_problems(db, b)
    if problems:
        return _err(409, "探头校准证书未通过签发检查（未绑定/缺失/未生效/过期/"
                         "温区覆盖不足），阻止签发",
                    state=b["state"], calibration_problems=problems)
    # 签发时快照工件尺寸/重量与粉料固化窗口，此后主数据变更不影响本炉次
    rows = db.execute(
        "SELECT bi.workpiece_id, w.length_mm, w.width_mm, w.height_mm, w.weight_kg,"
        " w.powder_batch, p.temp_min_c, p.temp_max_c, p.hold_minutes,"
        " p.pack_temp_limit_c, p.low_temp_hold_minutes"
        " FROM batch_items bi"
        " JOIN workpieces w ON w.id = bi.workpiece_id"
        " LEFT JOIN powders p ON p.batch_no = w.powder_batch"
        " WHERE bi.batch_id=?", (bid,)).fetchall()
    for r in rows:
        db.execute(
            "UPDATE batch_items SET snap_length_mm=?, snap_width_mm=?, snap_height_mm=?,"
            " snap_weight_kg=?, snap_powder_batch=?, snap_temp_min_c=?, snap_temp_max_c=?,"
            " snap_hold_minutes=?, snap_pack_temp_limit_c=?,"
            " snap_low_temp_hold_minutes=? WHERE batch_id=? AND workpiece_id=?",
            (r["length_mm"], r["width_mm"], r["height_mm"], r["weight_kg"],
             r["powder_batch"], r["temp_min_c"], r["temp_max_c"], r["hold_minutes"],
             r["pack_temp_limit_c"], r["low_temp_hold_minutes"],
             bid, r["workpiece_id"]))
    # 签发时冻结探头配置（编号 + 校准偏移 + 校准证书版本快照），
    # 此后主数据变更与新证书版本均不影响本炉次
    for r in rows:
        for pr in db.execute(
                "SELECT p.probe_id, p.offset_c, p.calibration_id,"
                " c.version, c.certificate_no, c.calibrated_at, c.valid_until,"
                " c.points_json"
                " FROM probes p"
                " LEFT JOIN probe_calibrations c ON c.id = p.calibration_id"
                " WHERE p.workpiece_id=? ORDER BY p.probe_id",
                (r["workpiece_id"],)).fetchall():
            db.execute(
                "INSERT OR IGNORE INTO batch_item_probes"
                " (batch_id, workpiece_id, probe_id, offset_c, calibration_id,"
                " version, certificate_no, calibrated_at, valid_until, points_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bid, r["workpiece_id"], pr["probe_id"], pr["offset_c"],
                 pr["calibration_id"], pr["version"], pr["certificate_no"],
                 pr["calibrated_at"], pr["valid_until"], pr["points_json"]))
    db.execute("UPDATE batches SET state='ISSUED' WHERE id=?", (bid,))
    db.commit()
    return jsonify({"batch_id": bid, "state": "ISSUED"})


def _batch_rack(db, b):
    """按炉次所属版本快照的炉架参数构建 Rack（人工布置复核/吊点故障复用）。"""
    v = db.execute("SELECT params_json FROM schedule_versions WHERE id=?",
                   (b["version_id"],)).fetchone()
    params = json.loads(v["params_json"]) if v and v["params_json"] else {}
    oven = next((o for o in params.get("ovens", [])
                 if o["id"] == b["oven_id"]), None)
    if oven is None:
        oven = db.execute("SELECT * FROM ovens WHERE id=?",
                          (b["oven_id"],)).fetchone()
        oven = dict(oven) if oven else None
    if oven is None:
        return None
    return racking.build_rack(oven), oven


def _arrangement_violations(db, b, rack, assignments, point_blackouts=None):
    """对人工布置逐条复核：净距/共享吊点/单点/分区/总载/力矩/重心/吊耳。

    assignments: [{workpiece_id, rod_id, point_indices, rotation_deg?}]。
    返回 (violations, layout, placement_map)；全部通过时 violations 为空。
    工件主数据取签发快照（已签发炉次）或当前主数据（草稿）。
    """
    frozen = b["state"] not in ("DRAFT", "SUPERSEDED")
    rows = db.execute(
        "SELECT bi.workpiece_id,"
        " COALESCE(bi.snap_length_mm, w.length_mm) AS length_mm,"
        " COALESCE(bi.snap_width_mm, w.width_mm) AS width_mm,"
        " COALESCE(bi.snap_height_mm, w.height_mm) AS height_mm,"
        " COALESCE(bi.snap_weight_kg, w.weight_kg) AS weight_kg,"
        " w.cg_offset_x_mm, w.cg_offset_y_mm, w.allowed_rotations,"
        " w.lift_points_json, w.clearance_mm"
        " FROM batch_items bi JOIN workpieces w ON w.id=bi.workpiece_id"
        " WHERE bi.batch_id=?", (b["id"],)).fetchall()
    wp_by_id = {r["workpiece_id"]: r for r in rows}
    by_wp = {a.get("workpiece_id"): a for a in assignments}
    violations = []
    # 工件集合必须与炉内一致（不多不少）
    missing = sorted(set(wp_by_id) - set(by_wp))
    extra = sorted(set(by_wp) - set(wp_by_id))
    if missing:
        violations.append({"code": "ITEMS_MISSING", "workpiece_id": None,
                           "detail": f"缺少工件的人工布置: {missing}"})
    if extra:
        violations.append({"code": "ITEMS_EXTRA", "workpiece_id": None,
                           "detail": f"炉次中不存在的工件: {extra}"})
    point_blackouts = point_blackouts or {}
    layout = racking._Layout(rack)
    placement_map = {}
    # 按搬入顺序（rod y 降序、x 升序）应用，保证净距检查确定
    ordered = sorted(
        [a for a in assignments if a.get("workpiece_id") in wp_by_id],
        key=lambda a: (str(a.get("rod_id")),
                       min(int(i) for i in a.get("point_indices", [0])),
                       a.get("workpiece_id")))
    blocked = {(pb["rod_id"], pb["point_index"])
               for pb in point_blackouts.get(b["oven_id"], [])}
    for a in ordered:
        wid = a["workpiece_id"]
        r = wp_by_id[wid]
        rid = str(a.get("rod_id"))
        rod = rack.rod(rid)
        if rod is None:
            violations.append({"code": "UNKNOWN_ROD", "workpiece_id": wid,
                               "rod_id": rid, "detail": f"挂杆 {rid} 不存在"})
            continue
        try:
            indices = [int(i) for i in a["point_indices"]]
        except (KeyError, TypeError, ValueError):
            violations.append({"code": "BAD_POINTS", "workpiece_id": wid,
                               "rod_id": rid, "detail": "point_indices 须为整数数组"})
            continue
        if len(set(indices)) != len(indices) or not indices:
            violations.append({"code": "BAD_POINTS", "workpiece_id": wid,
                               "rod_id": rid, "detail": "吊点编号重复或为空"})
            continue
        pts = [rack.point(rid, i) for i in indices]
        if any(p is None for p in pts):
            bad = indices[[j for j, p in enumerate(pts) if p is None][0]]
            violations.append({"code": "UNKNOWN_POINT", "workpiece_id": wid,
                               "rod_id": rid, "point_index": bad,
                               "detail": f"挂杆 {rid} 无吊点 {bad}"})
            continue
        if sorted(indices) != list(range(min(indices), max(indices) + 1)):
            violations.append({"code": "NON_CONTIGUOUS", "workpiece_id": wid,
                               "rod_id": rid, "point_index": min(indices),
                               "detail": "占用吊点必须编号连续"})
            continue
        wp = {"id": wid, "length_mm": r["length_mm"], "width_mm": r["width_mm"],
              "height_mm": r["height_mm"], "weight_kg": r["weight_kg"],
              "powder_batch": r["workpiece_id"],
              "cg_offset_x_mm": r["cg_offset_x_mm"] or 0.0,
              "cg_offset_y_mm": r["cg_offset_y_mm"] or 0.0,
              "allowed_rotations_deg": (json.loads(r["allowed_rotations"])
                                        if r["allowed_rotations"] else None),
              "lift_points_mm": (json.loads(r["lift_points_json"])
                                 if r["lift_points_json"] else None),
              "clearance_mm": r["clearance_mm"] or 0.0}
        if "rotation_deg" in a and a["rotation_deg"] is not None:
            wp["allowed_rotations_deg"] = [int(a["rotation_deg"])]
        # 手工指定具体吊点：在复制的挂杆布局上只允许这一段
        res = layout.try_place_manual(wp, rid, indices, blocked=blocked)
        if "wp" not in res:
            violations.append({"workpiece_id": wid, "rod_id": rid,
                               "point_index": res.get("point_index"),
                               "code": res["conflict"],
                               "detail": res.get("detail", "")})
            continue
        layout.commit(res)
        placement_map[wid] = res
    # 整组方案检查（不是逐件看）：单点/分区在放入时已逐件累计检查，这里
    # 复核横梁总载与左右力矩平衡，给出整组载荷视图
    bal = layout.balance()
    if not bal.get("total_ok"):
        violations.append({
            "code": "BEAM_TOTAL", "workpiece_id": None,
            "detail": f"横梁总载 {bal['total_load_kg']:g}/"
                      f"{bal['beam_limit_kg']:g} kg 超限"})
    for rod in bal.get("rods", []):
        for z in rod.get("zones", []):
            if not z["ok"]:
                violations.append({
                    "code": "ZONE_LOAD", "workpiece_id": None,
                    "rod_id": rod["rod_id"], "point_index": None,
                    "detail": f"挂杆 {rod['rod_id']} 分区 {z['zone_id']} 承重 "
                              f"{z['load_kg']:g}/{z['limit_kg']:g} kg 超限"})
    if not bal.get("moment_ok"):
        violations.append({
            "code": "MOMENT", "workpiece_id": None,
            "detail": f"整组偏载力矩 |{bal['moment_abs_kg_mm']:g}| kg·mm "
                      "超过左右偏载容差"})
    return violations, layout, placement_map


@bp.post("/batches/<int:bid>/arrangement/verify")
def verify_arrangement(bid):
    """人工调整吊具布置后复核：只校验，不落库（check_only）或校验通过后采用。

    body: {"assignments":[{"workpiece_id","rod_id","point_indices",
                           "rotation_deg"?}], "apply": true/false}
    - 炉次须为 DRAFT 才能采用（apply=true）；签发后布置冻结，只可 check_only；
    - 逐条返回违反的约束（净距/共享吊点/单点承重/分区/总载/力矩/重心/吊耳/
      禁用吊点），任一不通过即 409 且不改动任何数据。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    data = request.get_json(silent=True) or {}
    assignments = data.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        return _err(400, "缺少 assignments 数组（工件→挂杆/吊点）")
    apply_it = bool(data.get("apply"))
    built = _batch_rack(db, b)
    if built is None:
        return _err(404, f"炉次 {bid} 的炉架参数缺失（版本快照无该炉）")
    rack, _oven = built
    if apply_it and b["state"] != "DRAFT":
        return _err(409, f"炉次状态为 {b['state']}，布置已冻结；"
                         "仅草稿炉次可采用人工布置（可去掉 apply 仅复核）",
                    state=b["state"])
    # 复核时计入当前未修复吊点故障（冻结炉次也会显示其影响）
    point_blackouts = _active_faults(db)
    violations, layout, placement_map = _arrangement_violations(
        db, b, rack, assignments, point_blackouts=point_blackouts)
    report = layout.report()
    if violations:
        return _err(409, "人工布置未通过吊具校验",
                    violations=violations,
                    load_balance=report["load_balance"])
    if apply_it:
        report["oven_id"] = b["oven_id"]
        db.execute("UPDATE batches SET arrangement_json=? WHERE id=?",
                   (json.dumps(report, ensure_ascii=False), bid))
        for wid, plc in placement_map.items():
            pview = racking.placement_view(plc, rack)
            seq = {s["workpiece_id"]: s["sequence"]
                   for s in report["load_in_sequence"]}
            db.execute(
                "UPDATE batch_items SET rod_id=?, hanger_slot=?, slots_used=?,"
                " load_in_sequence=?, placement_json=?"
                " WHERE batch_id=? AND workpiece_id=?",
                (plc["rod_id"], plc["run"][0]["index"], len(plc["run"]),
                 seq[wid], json.dumps(pview, ensure_ascii=False), bid, wid))
        db.commit()
    return jsonify({"batch_id": bid, "state": b["state"],
                    "applied": apply_it,
                    "valid": True, "rack_layout": report,
                    "load_in_sequence": report["load_in_sequence"]})


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


@bp.post("/batches/<int:bid>/workpieces/<wid>/cooling-readings")
def add_cooling_readings(bid, wid):
    """冷却测温写入：仅接收 COOLING（已合格离炉、冷却放行观察中）工件。

    body: {"readings":[{ts, surface_temp_c}], "at"?: 基准时刻}
    或单条 {ts, surface_temp_c}。
    - 按 (炉次, 工件, 时刻) 幂等去重：同点同温度重复回传计入 duplicates；
    - **乱序**（ts 早于已收录最新时标）、**同时刻不同温度**（TS_CONFLICT）
      与**时刻和温度完全相同的重复提交**（TS_DUPLICATE，"同点重复"）均照常
      保存本次提交、标记 kind 并记录区间中断，由冷却区间引擎截断当前连续
      低温区间、从该点之后重新累计；
    - 测温时刻早于实际离炉时刻一律拒收（离炉前的表面温度不属冷却观测）。
    接受后即时返回该件冷却进度。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    item = _cooling_item(db, bid, wid)
    if item is None:
        return _err(404, f"工件 {wid} 不在炉次 {bid} 中")
    data = request.get_json(silent=True) or {}
    entries = data.get("readings")
    if entries is None and "ts" in data:
        entries = [data]
    if not entries:
        return _err(400, "缺少 readings（或单条 ts/surface_temp_c）")

    accepted = duplicate_points = conflicts = 0
    rejected = []
    stored = []   # 本次新收录（含乱序/同点重复），用于按到达顺序即时反馈
    unloaded_at = (datetime.fromisoformat(item["actual_unload_at"])
                   if item["actual_unload_at"] else None)
    for e in entries:
        try:
            ts = _parse_dt(e["ts"], "ts")
            temp = float(e["surface_temp_c"])
        except (KeyError, TypeError, ValueError) as ex:
            rejected.append({"ts": e.get("ts"), "reason": f"测温记录无效: {ex}"})
            continue
        if item["release_kind"] is not None:
            rejected.append({"ts": e.get("ts"),
                             "reason": f"工件已于 {item['release_at']} "
                                       f"{item['release_kind']} 放行，"
                                       "放行后不再接收冷却测温"})
            continue
        if unloaded_at is None:
            rejected.append({"ts": e.get("ts"),
                             "reason": "工件尚未离炉，冷却测温自离炉时刻起接收"})
            continue
        if ts < unloaded_at:
            rejected.append({"ts": e.get("ts"),
                             "reason": f"测温时刻早于实际离炉时刻 "
                                       f"{item['actual_unload_at']}，不予收录"})
            continue
        # 时刻相同：温度不同 TS_CONFLICT、温度相同 TS_DUPLICATE。
        # 二者都是"同点重复"：本次提交照常保存并截断区间（不做幂等忽略）。
        prior = _cooling_readings(db, bid, wid) + stored
        kind, prev_ts = cooling.admit(prior, ts, temp)
        created = _now().isoformat()
        db.execute(
            "INSERT INTO cooling_readings (batch_id, workpiece_id, ts,"
            " surface_temp_c, kind, created_at) VALUES (?,?,?,?,?,?)",
            (bid, wid, ts.isoformat(), temp, kind, created))
        stored.append((ts, temp, kind))
        if kind == cooling.KIND_OK:
            accepted += 1
        else:
            conflicts += 1
            if kind == cooling.KIND_TS_CONFLICT:
                prev_temp = db.execute(
                    "SELECT surface_temp_c FROM cooling_readings"
                    " WHERE batch_id=? AND workpiece_id=? AND ts=?"
                    " ORDER BY id LIMIT 1",
                    (bid, wid, ts.isoformat())).fetchone()
                detail = (f"同时刻 {ts.isoformat(timespec='seconds')} 重复上报"
                          f"不同表面温度：既有 "
                          f"{prev_temp['surface_temp_c']:g}℃、新读数 {temp:g}℃，"
                          "该时刻温度不可信，当前连续低温区间截断，"
                          "从该点之后重新累计")
            elif kind == cooling.KIND_TS_DUPLICATE:
                duplicate_points += 1
                detail = (f"同时刻 {ts.isoformat(timespec='seconds')} 重复提交"
                          f"相同表面温度 {temp:g}℃（同点重复），当前连续低温"
                          "区间截断，从该点之后重新累计")
            else:
                detail = (f"读数乱序：{ts.isoformat(timespec='seconds')} 早于已收录最新"
                          f"时刻 {prev_ts.isoformat(timespec='seconds') if prev_ts else '-'}，"
                          "当前连续低温区间截断")
            db.execute(
                "INSERT INTO cooling_interruptions (batch_id, workpiece_id, code,"
                " at_ts, detail, created_at) VALUES (?,?,?,?,?,?)",
                (bid, wid, kind, ts.isoformat(), detail, created))
    db.commit()
    try:
        as_of = _parse_dt(data["at"], "at") if data.get("at") else _now()
    except ValueError as ex:
        return _err(400, str(ex))
    item = _cooling_item(db, bid, wid)
    ev = _cooling_evaluate(db, bid, item, as_of)
    return jsonify({"batch_id": bid, "workpiece_id": wid,
                    "accepted": accepted,
                    # duplicates = 同点重复（时刻+温度完全相同）：已保存并截断区间
                    "duplicates": duplicate_points,
                    "conflicts": conflicts, "rejected": rejected,
                    "cooling": ev})


@bp.get("/batches/<int:bid>/workpieces/<wid>/cooling")
def cooling_progress(bid, wid):
    """单工件冷却放行进度查询。

    返回当前表面读数、当前连续低温区间有效保持分钟、按当前连续区间计算的
    最早放行时刻、未满足项，以及完整区间中断追溯。可带 ?as_of= 历史复盘。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    item = _cooling_item(db, bid, wid)
    if item is None:
        return _err(404, f"工件 {wid} 不在炉次 {bid} 中")
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else _now()
    except ValueError as e:
        return _err(400, str(e))
    return jsonify({"batch_id": bid, "workpiece_id": wid,
                    "state": _workpiece_state(db, wid),
                    "cooling": _cooling_evaluate(db, bid, item, as_of)})


@bp.get("/batches/<int:bid>/cooling")
def batch_cooling(bid):
    """炉次级冷却放行进度：逐件冷却状态、放行/紧急搬运统计。"""
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    try:
        as_of = _parse_dt(request.args["as_of"], "as_of") \
            if request.args.get("as_of") else _now()
    except ValueError as e:
        return _err(400, str(e))
    return jsonify({"batch_id": bid, "state": b["state"],
                    "cooling": _batch_cooling(db, b, as_of)})


@bp.post("/batches/<int:bid>/workpieces/<wid>/release")
def release_workpiece(bid, wid):
    """冷却搬运放行。

    body: {"at"?: 放行时刻, "emergency"?: true, "reason"?: 紧急理由}
    - **正常放行**：工件须 COOLING 且截至 at 冷却门限已满足（连续低温区间
      保持达到要求、读数未陈旧等）；未达门限一律 409，返回未满足项与最早
      放行时刻，不留放行记录（后续测温后可再次请求）；
    - **紧急搬运**：`emergency=true` 且必须填写 reason；不看门限，工件转
      **REWORK_PENDING**（返工处置），落 EMERGENCY_RELEASE 标记与审计。
    结案（炉次/工件）须引用一次有效放行记录（release_actions）。
    """
    db = get_db()
    b = _fetch_batch(db, bid)
    if b is None:
        return _err(404, f"炉次 {bid} 不存在")
    item = _cooling_item(db, bid, wid)
    if item is None:
        return _err(404, f"工件 {wid} 不在炉次 {bid} 中")
    w = db.execute("SELECT status FROM workpieces WHERE id=?", (wid,)).fetchone()
    data = request.get_json(silent=True) or {}
    try:
        at = _parse_dt(data["at"], "at") if data.get("at") else _now()
    except ValueError as e:
        return _err(400, str(e))
    emergency = bool(data.get("emergency"))
    reason = str(data.get("reason") or "").strip() or None

    if item["release_kind"] is not None:
        return _err(409, f"工件 {wid} 已于 {item['release_at']} "
                         f"{item['release_kind']} 放行，不能重复放行",
                    release_kind=item["release_kind"],
                    release_at=item["release_at"])
    if emergency and not reason:
        return _err(400, "紧急搬运必须填写理由 reason")
    # 冷却搬运放行自合格离炉后开始：仍在炉的工件不可搬运；
    # 正常放行与紧急搬运都只适用于冷却放行观察中的 COOLING 件
    if not item["actual_unload_at"]:
        return _err(409, f"工件尚未离炉（状态 {w['status']}），"
                         "冷却搬运放行自合格离炉后开始", state=w["status"])
    if w["status"] != "COOLING":
        action = "正常冷却放行" if not emergency else "紧急搬运"
        return _err(409, f"工件状态为 {w['status']}，不能{action}"
                         "（要求合格离炉后的 COOLING）", state=w["status"])

    ev = _cooling_evaluate(db, bid, item, at)
    if emergency:
        # 紧急搬运：不看门限，转返工处置，保留人工决定与当时冷却快照
        kind = RELEASE_EMERGENCY
        wstatus = "REWORK_PENDING"
        _add_flag(db, bid, wid, FLAG_EMERGENCY_RELEASE,
                  f"冷却未达门限紧急搬运：{reason}"
                  + (f"（未满足: {'、'.join(u['code'] for u in ev['unmet'])}）"
                     if ev["unmet"] else ""))
        held = ev["held_low_temp_minutes"]
    else:
        if not ev["releasable"]:
            return _err(409, f"工件 {wid} 冷却放行门限未满足，拒绝搬运；"
                             "紧急搬运请带 emergency=true 与 reason",
                        state=w["status"], workpiece_id=wid,
                        held_low_temp_minutes=ev["held_low_temp_minutes"],
                        required_minutes=ev["low_temp_hold_minutes"],
                        remaining_hold_minutes=ev["remaining_hold_minutes"],
                        earliest_release_at=ev["earliest_release_at"],
                        unmet=ev["unmet"], cooling=ev)
        kind = RELEASE_NORMAL
        wstatus = "DONE"
        held = ev["held_low_temp_minutes"]

    snap = json.dumps(ev, ensure_ascii=False)
    cur = db.execute(
        "INSERT INTO release_actions (batch_id, workpiece_id, kind, release_at,"
        " reason, held_minutes, snapshot_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (bid, wid, kind, at.isoformat(), reason, held, snap,
         _now().isoformat()))
    db.execute(
        "UPDATE batch_items SET release_kind=?, release_at=?, release_reason=?,"
        " release_snapshot_json=? WHERE batch_id=? AND workpiece_id=?",
        (kind, at.isoformat(), reason, snap, bid, wid))
    db.execute("UPDATE workpieces SET status=? WHERE id=?", (wstatus, wid))
    db.commit()
    return jsonify({"batch_id": bid, "workpiece_id": wid,
                    "release_id": cur.lastrowid, "kind": kind,
                    "release_at": at.isoformat(),
                    "reason": reason if emergency else None,
                    "workpiece_status": wstatus,
                    "held_low_temp_minutes": held,
                    "cooling_snapshot": ev})


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
    # 结案须引用一次有效放行：合格离炉件须冷却正常放行（release_actions
    # NORMAL）后才 DONE；仍在 COOLING 的工件阻止结案；紧急搬运件已转返工，
    # 按返工口径（PENDING+is_rework）不阻塞；每件 DONE 均须有放行记录
    blockers = []
    rows = db.execute(
        "SELECT bi.workpiece_id, w.status, bi.release_kind, ra.id AS release_id"
        " FROM batch_items bi JOIN workpieces w ON w.id=bi.workpiece_id"
        " LEFT JOIN release_actions ra ON ra.batch_id=bi.batch_id"
        "  AND ra.workpiece_id=bi.workpiece_id"
        " WHERE bi.batch_id=?", (bid,)).fetchall()
    for r in rows:
        if r["status"] == "COOLING":
            blockers.append({"workpiece_id": r["workpiece_id"],
                             "status": "COOLING",
                             "detail": "合格离炉件仍在冷却放行观察中，"
                                       "须冷却测温满足门限并正常放行后才能结案"})
        elif r["status"] == "REWORK_PENDING":
            blockers.append({"workpiece_id": r["workpiece_id"],
                             "status": "REWORK_PENDING",
                             "detail": "待返工工件尚未回到待排产队列"})
        elif r["status"] == "DONE" and r["release_id"] is None:
            blockers.append({"workpiece_id": r["workpiece_id"],
                             "status": "DONE",
                             "detail": "工件已完成但缺少冷却放行记录，"
                                       "结案须引用一次有效放行"})
    if blockers:
        return _err(409, "存在未放行/未了结工件，不能结案", blockers=blockers)
    db.execute("UPDATE batches SET state='CLOSED' WHERE id=?", (bid,))
    db.commit()
    releases = db.execute(
        "SELECT id, workpiece_id, kind, release_at FROM release_actions"
        " WHERE batch_id=? ORDER BY id", (bid,)).fetchall()
    return jsonify({"batch_id": bid, "state": "CLOSED",
                    "releases": [dict(r) for r in releases]})


# ---------------------------------------------------------------- 探头登记与处置

@bp.post("/workpieces/<wid>/probes")
def register_probes(wid):
    """登记/更新工件探头及校准偏移，可指定校准证书版本（calibration_id）。

    签发时随炉次冻结快照（含证书版本与点列），此后变更只影响新炉次。
    请求不带 calibration_id 键时保留既有绑定；显式传 null 解除绑定。
    注意：登记探头未绑定证书版本（calibration_id 为 NULL）时炉次不能签发。
    """
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
        if "calibration_id" in e:
            # 绑定指定校准版本（须属于该探头）；显式 null 解除绑定
            cal_id = e.get("calibration_id")
            if cal_id is not None:
                try:
                    cal_id = int(cal_id)
                except (TypeError, ValueError):
                    return _err(400, f"探头 {pid} 的 calibration_id 不是整数:"
                                     f" {e.get('calibration_id')!r}")
                cal = db.execute(
                    "SELECT id FROM probe_calibrations"
                    " WHERE id=? AND workpiece_id=? AND probe_id=?",
                    (cal_id, wid, pid)).fetchone()
                if cal is None:
                    return _err(400, f"校准版本 {cal_id} 不存在或不属于探头 {pid}")
            db.execute(
                "INSERT INTO probes (workpiece_id, probe_id, offset_c,"
                " calibration_id, created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(workpiece_id, probe_id) DO UPDATE SET"
                " offset_c=excluded.offset_c,"
                " calibration_id=excluded.calibration_id",
                (wid, pid, offset, cal_id, _now().isoformat()))
        else:
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
    """工件探头主数据：校准偏移与绑定的证书版本摘要。"""
    rows = db.execute(
        "SELECT p.probe_id, p.offset_c, p.calibration_id, p.created_at,"
        " c.version, c.certificate_no, c.calibrated_at, c.valid_until"
        " FROM probes p"
        " LEFT JOIN probe_calibrations c ON c.id = p.calibration_id"
        " WHERE p.workpiece_id=? ORDER BY p.probe_id", (wid,)).fetchall()
    out = []
    for r in rows:
        d = {"probe_id": r["probe_id"], "offset_c": r["offset_c"],
             "calibration_id": r["calibration_id"], "created_at": r["created_at"]}
        if r["calibration_id"] is not None and r["certificate_no"] is not None:
            d["calibration"] = {
                "version": r["version"],
                "certificate_no": r["certificate_no"],
                "calibrated_at": r["calibrated_at"],
                "valid_until": r["valid_until"],
            }
        out.append(d)
    return out


# ---------------------------------------------------------------- 探头校准证书

def _parse_calibration_points(raw):
    """校验示值—参考值点列：至少两点、数值合法、示值严格递增（ValueError）。"""
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("校准点列至少需要两个点（示值 indicated_c — 参考值"
                         " reference_c）")
    points = []
    for p in raw:
        if not isinstance(p, dict):
            raise ValueError(f"校准点须为对象: {p!r}")
        try:
            indicated = float(p["indicated_c"])
            reference = float(p["reference_c"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"校准点须含数值 indicated_c/reference_c: {p!r}")
        points.append({"indicated_c": indicated, "reference_c": reference})
    for a, b in zip(points, points[1:]):
        if b["indicated_c"] <= a["indicated_c"]:
            raise ValueError(f"校准点列示值不递增: {a['indicated_c']:g} 之后出现"
                             f" {b['indicated_c']:g}")
    return points


def _calibration_json(r):
    """校准版本行的 API 视图（含点列与插值区间）。"""
    points = json.loads(r["points_json"])
    return {
        "calibration_id": r["id"],
        "version": r["version"],
        "certificate_no": r["certificate_no"],
        "calibrated_at": r["calibrated_at"],
        "valid_until": r["valid_until"],
        "range_min_c": points[0]["indicated_c"],
        "range_max_c": points[-1]["indicated_c"],
        "points": points,
        "created_at": r["created_at"],
    }


@bp.post("/workpieces/<wid>/probes/<pid>/calibrations")
def add_calibration(wid, pid):
    """录入探头校准证书版本（不可覆盖的多点版本）。

    每次录入追加一个新版本（version 自增），已录入版本不可修改/删除；
    新证书只供未签发炉次使用（已签发炉次用签发时冻结的快照）。
    少于两点、校准/到期时刻倒置或点列示值不递增时拒绝。
    """
    db = get_db()
    w = db.execute("SELECT id FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    pr = db.execute(
        "SELECT id FROM probes WHERE workpiece_id=? AND probe_id=?",
        (wid, pid)).fetchone()
    if pr is None:
        return _err(404, f"探头 {pid} 未登记在工件 {wid} 上")
    data = request.get_json(silent=True) or {}
    try:
        cert = str(data.get("certificate_no") or "").strip()
        if not cert:
            raise ValueError("certificate_no 不能为空")
        calibrated_at = _parse_dt(data["calibrated_at"], "calibrated_at")
        valid_until = _parse_dt(data["valid_until"], "valid_until")
        if valid_until <= calibrated_at:
            raise ValueError(
                f"校准有效期倒置: 校准时刻 {calibrated_at.isoformat()} 不早于"
                f"到期时刻 {valid_until.isoformat()}")
        points = _parse_calibration_points(data.get("points"))
    except KeyError as e:
        return _err(400, f"缺少字段: {e.args[0]}")
    except ValueError as e:
        return _err(400, str(e))
    version = db.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM probe_calibrations"
        " WHERE workpiece_id=? AND probe_id=?", (wid, pid)).fetchone()["v"]
    cur = db.execute(
        "INSERT INTO probe_calibrations (workpiece_id, probe_id, version,"
        " certificate_no, calibrated_at, valid_until, points_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (wid, pid, version, cert, calibrated_at.isoformat(),
         valid_until.isoformat(), json.dumps(points, ensure_ascii=False),
         _now().isoformat()))
    db.commit()
    row = db.execute("SELECT * FROM probe_calibrations WHERE id=?",
                     (cur.lastrowid,)).fetchone()
    return jsonify({"workpiece_id": wid, "probe_id": pid,
                    "calibration": _calibration_json(row)}), 201


@bp.get("/workpieces/<wid>/probes/<pid>/calibrations")
def list_calibrations(wid, pid):
    """探头校准证书历史版本（按版本序号升序，含点列与插值区间）。"""
    db = get_db()
    w = db.execute("SELECT id FROM workpieces WHERE id=?", (wid,)).fetchone()
    if w is None:
        return _err(404, f"工件 {wid} 不存在")
    pr = db.execute(
        "SELECT id FROM probes WHERE workpiece_id=? AND probe_id=?",
        (wid, pid)).fetchone()
    if pr is None:
        return _err(404, f"探头 {pid} 未登记在工件 {wid} 上")
    rows = db.execute(
        "SELECT * FROM probe_calibrations WHERE workpiece_id=? AND probe_id=?"
        " ORDER BY version", (wid, pid)).fetchall()
    return jsonify({"workpiece_id": wid, "probe_id": pid,
                    "calibrations": [_calibration_json(r) for r in rows]})


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
    # 冷却放行履历：逐炉次的冻结门限、表面读数、区间中断与人工搬运决定
    cooling_records = []
    for r in db.execute(
            "SELECT bi.batch_id, bi.actual_unload_at, bi.snap_pack_temp_limit_c,"
            " bi.snap_low_temp_hold_minutes, bi.release_kind, bi.release_at,"
            " bi.release_reason, bi.release_snapshot_json, b.state AS batch_state"
            " FROM batch_items bi JOIN batches b ON b.id=bi.batch_id"
            " WHERE bi.workpiece_id=? AND bi.actual_unload_at IS NOT NULL"
            " ORDER BY bi.batch_id", (wid,)).fetchall():
        readings, interruptions = _cooling_reading_view(
            db, r["batch_id"], wid)
        cooling_records.append({
            "batch_id": r["batch_id"], "batch_state": r["batch_state"],
            "unloaded_at": r["actual_unload_at"],
            "pack_temp_limit_c": r["snap_pack_temp_limit_c"],
            "low_temp_hold_minutes": r["snap_low_temp_hold_minutes"],
            "release": (None if r["release_kind"] is None else
                        {"kind": r["release_kind"], "release_at": r["release_at"],
                         "reason": r["release_reason"]}),
            "release_snapshot": (json.loads(r["release_snapshot_json"])
                                 if r["release_snapshot_json"] else None),
            "readings": readings, "interruptions": interruptions})
    return jsonify({**{k: w[k] for k in w.keys()}, "is_rework": bool(w["is_rework"]),
                    "probes": _probes_of(db, wid),
                    "batches": [{**dict(r), "forced": bool(r["forced"])}
                                for r in batches],
                    "cooling_records": cooling_records,
                    "flags": [dict(r) for r in flags]})


@bp.get("/versions")
def list_versions():
    db = get_db()
    rows = db.execute(
        "SELECT v.id, v.parent_id, v.reason, v.created_at,"
        " (SELECT COUNT(*) FROM batches b WHERE b.version_id = v.id) AS batch_count,"
        " (SELECT COUNT(*) FROM blackout_windows w WHERE w.version_id = v.id)"
        "   AS blackout_count,"
        " (SELECT COUNT(*) FROM hanger_blackouts h WHERE h.version_id = v.id)"
        "   AS hanger_blackout_count,"
        " (SELECT COUNT(*) FROM schedule_rejections r WHERE r.version_id = v.id)"
        "   AS rejection_count"
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
    # 版本快照中的吊点禁用（临时封位 + 吊点故障）
    hb_rows = db.execute(
        "SELECT oven_id, rod_id, point_index, start_at, end_at, kind, note,"
        " fault_id FROM hanger_blackouts WHERE version_id=?"
        " ORDER BY oven_id, rod_id, point_index", (vid,)).fetchall()
    hanger_blackouts = [dict(r) for r in hb_rows]
    # 放不下工件的拒绝原因（首个冲突约束 / 逐炉明细 / 可选炉）
    rejections = [
        {"workpiece_id": r["workpiece_id"], "reason": r["reason"],
         "detail": r["detail"],
         "first_conflict": json.loads(r["first_conflict_json"])
                           if r["first_conflict_json"] else None,
         "per_oven": json.loads(r["per_oven_json"]) if r["per_oven_json"] else [],
         "alternative_ovens": json.loads(r["alternative_ovens_json"])
                              if r["alternative_ovens_json"] else []}
        for r in db.execute(
            "SELECT workpiece_id, reason, detail, first_conflict_json,"
            " per_oven_json, alternative_ovens_json FROM schedule_rejections"
            " WHERE version_id=? ORDER BY id", (vid,)).fetchall()
    ]
    return jsonify({
        "id": v["id"], "parent_id": v["parent_id"], "reason": v["reason"],
        "created_at": v["created_at"],
        "params": params,                # 试算输入快照（计算依据）
        "blackout_windows": windows,     # 该版本登记的停机窗
        "hanger_blackouts": hanger_blackouts,
        "rejections": rejections,        # 放不下工件的首个冲突与可选炉
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
                cv = it.get("cooling")
                rel = cv.get("release") if cv else None
                if rel:
                    unload_txt += ("\n已放行 " if rel["kind"] == "NORMAL"
                                   else "\n紧急搬运 ") + str(rel["release_at"])[5:16]
                else:
                    unload_txt += "\n冷却中"
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
            cal = pr.get("calibration")
            if cal:
                cert_txt = (f"{html.escape(str(cal['certificate_no']))}"
                            f"（v{cal['version']}）")
                range_txt = f"{cal['range_min_c']:g}–{cal['range_max_c']:g}"
                expiry_txt = f"{cal['valid_until']}"
                expiry_txt += "（计划入炉时已过期）" if cal.get("expired") \
                    else "（计划入炉时有效）"
            else:
                cert_txt = range_txt = expiry_txt = "-"
            readings_txt = str(pr["reading_count"])
            if pr.get("out_of_range_count"):
                readings_txt += f"（超区间 {pr['out_of_range_count']} 点未计入）"
            probe_rows.append(
                "<tr>"
                f"<td>{html.escape(str(pr['probe_id'] or '（隐式通道）'))}</td>"
                f"<td>{pr['offset_c']:+.2f}</td>"
                f"<td>{cert_txt}</td>"
                f"<td>{range_txt}</td>"
                f"<td>{html.escape(expiry_txt)}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{readings_txt}</td>"
                f"<td>{anomalies}</td>"
                "</tr>"
            )
        divergences = "；".join(
            f"{d['ts']} 极差 {d['spread_c']}℃" for d in c["divergences"]) or "-"
        gaps = "；".join(
            f"{html.escape(str(g['probe_id'] or '（隐式通道）'))} "
            f"{g['from']}–{g['to']}（{g['minutes']} 分钟）"
            for g in c["probe_gaps"]) or "-"
        oor = "；".join(
            f"{html.escape(str(o['probe_id']))} {o['ts']} 示值 {o['raw_c']:g}℃"
            f"（区间 {o['range_min_c']:g}–{o['range_max_c']:g}℃）"
            for o in c.get("calibration_range", [])) or "-"
        series = "，".join(f"{pt['ts'][11:16]}={pt['temp_c']:.1f}"
                           for pt in c["judgment_series"]) or "-"
        probe_blocks.append(
            f"<h3>工件 {html.escape(it['workpiece_id'])}"
            f"（有效探头 {c['valid_probe_count']} / 要求 {c['min_valid_probes']}）</h3>"
            "<table><tr><th>探头</th><th>校准偏移 ℃</th><th>证书版本</th>"
            "<th>插值区间 ℃</th><th>到期状态</th><th>状态</th>"
            "<th>读数</th><th>异常区间</th></tr>"
            f"{''.join(probe_rows)}</table>"
            f"<p class='small'>探头温差异常：{divergences}</p>"
            f"<p class='small'>缺报区间：{gaps}</p>"
            f"<p class='small'>超校准区间读数（未计入保温累计）：{oor}</p>"
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
    # 冷却放行：冻结门限、完整表面读数、区间中断与人工搬运决定
    cool_rows = []
    for it in p.get("items", []):
        cv = it.get("cooling")
        if not cv:
            continue
        snap = cv.get("snapshot") or {}
        rel = cv.get("release")
        if rel:
            rel_txt = ("正常放行" if rel["kind"] == "NORMAL"
                       else "紧急搬运（返工）")
            rel_txt += f" {rel['release_at']}"
            if rel.get("reason"):
                rel_txt += f" 理由：{html.escape(rel['reason'])}"
        else:
            rel_txt = "冷却中"
        breaks = "；".join(
            f"{b.get('code')}@{b.get('to') or b.get('at_ts') or ''}"
            for b in cv.get("interruptions", [])) or "-"
        cool_rows.append(
            "<tr>"
            f"<td>{html.escape(it['workpiece_id'])}</td>"
            f"<td>{cv['pack_temp_limit_c'] if cv['pack_temp_limit_c'] is not None else '-'}"
            f" / {cv['low_temp_hold_minutes'] if cv['low_temp_hold_minutes'] is not None else '-'}</td>"
            f"<td>{html.escape(str(snap.get('latest_reading_at') or '-'))}</td>"
            f"<td>{snap.get('latest_surface_temp_c', '-')}</td>"
            f"<td>{snap.get('held_low_temp_minutes', 0):g}</td>"
            f"<td>{html.escape(str(snap.get('earliest_release_at') or '-'))}</td>"
            f"<td>{html.escape(breaks)}</td>"
            f"<td>{html.escape(rel_txt)}</td>"
            "</tr>"
        )
    cool_block = (
        "<h2>冷却放行（包装耐温门限 / 连续低温保持）</h2>"
        "<table><tr><th>工件</th><th>耐温上限℃ / 保持 min</th>"
        "<th>最新测温</th><th>表面℃</th><th>当前区间保持 min</th>"
        "<th>最早放行</th><th>区间中断</th><th>人工决定</th></tr>"
        f"{''.join(cool_rows)}</table>"
    ) if cool_rows else ""
    # 吊具布置与载荷平衡：挂杆/吊点坐标、旋转、各点载荷、分区、横梁总载、
    # 左右力矩与搬入顺序（草稿为当前布置，签发后为冻结快照）
    rl = p.get("rack_layout")
    if rl:
        bal = rl.get("load_balance", {})
        rod_rows = []
        for rod in bal.get("rods", []):
            ztxt = "；".join(
                f"{z['zone_id']}: {z['load_kg']:g}/{z['limit_kg']:g} kg"
                + ("" if z["ok"] else " 超限")
                for z in rod.get("zones", [])) or "-"
            rod_rows.append(
                "<tr>"
                f"<td>{html.escape(str(rod['rod_id']))}</td>"
                f"<td>{rod['left_load_kg']:g}</td>"
                f"<td>{rod['right_load_kg']:g}</td>"
                f"<td>{rod['moment_kg_mm']:+g}</td>"
                f"<td>{html.escape(ztxt)}</td>"
                "</tr>")
        plc_rows = []
        seq_map = {s["workpiece_id"]: s["sequence"]
                   for s in rl.get("load_in_sequence", [])}
        for plc in rl.get("placements", []):
            pts = "、".join(
                f"#{pt['index']}({pt['x_mm']:g}, {pt['load_kg']:g}kg"
                + ("" if pt["bearing"] else ",不承重") + ")"
                for pt in plc["occupied_points"])
            plc_rows.append(
                "<tr>"
                f"<td>{html.escape(str(plc['workpiece_id']))}</td>"
                f"<td>{html.escape(str(plc['rod_id']))}</td>"
                f"<td>{plc['rotation_deg']:g}°</td>"
                f"<td>{plc['center_x_mm']:g}</td>"
                f"<td>{plc['cg_x_mm']:g}</td>"
                f"<td>{html.escape(pts)}</td>"
                f"<td>{seq_map.get(plc['workpiece_id'], '-')}</td>"
                f"<td>{html.escape(plc['lift_mode'])}</td>"
                "</tr>")
        mom_ok = "通过" if bal.get("moment_ok") else "超容差"
        total_ok = "通过" if bal.get("total_ok") else "超限"
        rack_block = (
            "<h2>吊具布置与载荷平衡</h2>"
            "<table class='meta'>"
            f"<tr><td>横梁总载：{bal.get('total_load_kg', 0):g} / "
            f"{bal.get('beam_limit_kg') if bal.get('beam_limit_kg') is not None else '—'} kg"
            f"（{total_ok}）</td>"
            f"<td>偏载力矩：|{bal.get('moment_abs_kg_mm', 0):g}| kg·mm"
            f"（容差 "
            f"{bal.get('moment_tolerance_kg_mm') if bal.get('moment_tolerance_kg_mm') is not None else '—'}"
            f" kg·mm，{mom_ok}）</td></tr>"
            "</table>"
            "<table><tr><th>挂杆</th><th>左侧载荷 kg</th><th>右侧载荷 kg</th>"
            "<th>相对跨中力矩 kg·mm</th><th>分区载荷</th></tr>"
            f"{''.join(rod_rows)}</table>"
            "<table><tr><th>工件</th><th>挂杆</th><th>旋转</th><th>中心 x mm</th>"
            "<th>重心 x mm</th><th>占用/承重吊点（编号:坐标,载荷）</th>"
            "<th>搬入顺序</th><th>吊挂方式</th></tr>"
            f"{''.join(plc_rows)}</table>"
        )
    else:
        rack_block = ""
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
{rack_block}
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
{cool_block}
<div class="sign"><span>操作工：____________</span><span>检验员：____________</span>
<span>日期：____________</span></div>
</body></html>"""
    return Response(html_doc, mimetype="text/html")
