"""在炉固化进度与安全出炉预测（纯函数）。

在多探头判定结果（probes.analyze）之上，按给定计算基准时刻 as_of 逐件推算：
- 最新有效测温时刻、最新最低校正温度与读数新鲜度（距 as_of 的分钟数）；
- 已累计 / 剩余保温分钟（连续在窗口内口径与 cure 一致）；
- 首次达标时刻（条件合格的前提下，连续保温首次累计达到要求的时刻）；
- 仅当最新温度处于许可区间且有效探头数量达标、读数未超时，
  才按连续保温推算最早安全出炉时刻 = 最新测温时刻 + 剩余保温分钟；
- 其余情况标记为不可预测（BLOCKED），给出欠温/超温/缺报/探头不足等阻塞原因；
- 已达标工件保留首次达标时刻作为安全出炉时刻，其后的异常读数只保留告警、
  不再回退其达标状态。

炉次级预测取所有工件最晚的安全出炉时刻，并与签发时冻结的计划出炉时刻比较，
指出计划出炉过早的分钟数；存在不可预测工件时计划是否可执行无法判定。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import probes

# 工件进度状态
STATUS_MET = "MET"                # 已达标：累计保温已满足，保留首次达标时刻
STATUS_TRACKING = "TRACKING"      # 保温中：条件正常，可推算安全出炉时刻
STATUS_BLOCKED = "BLOCKED"        # 不可预测：存在阻塞原因

STATUS_TEXT = {
    STATUS_MET: "已达标",
    STATUS_TRACKING: "保温中，可预测",
    STATUS_BLOCKED: "不可预测",
}

# 炉次级预测状态
BATCH_ALL_MET = "ALL_MET"                # 全部工件已达标
BATCH_PREDICTABLE = "PREDICTABLE"        # 存在保温中工件，但均可预测
BATCH_BLOCKED = "BLOCKED"               # 存在不可预测工件

BATCH_STATUS_TEXT = {
    BATCH_ALL_MET: "全部达标，可按首件达标时刻之后出炉",
    BATCH_PREDICTABLE: "可预测",
    BATCH_BLOCKED: "存在不可预测工件，暂不能给出炉次安全出炉时刻",
}

# 计划出炉时刻校验结果
PLAN_OK = "OK"                    # 计划出炉时刻不早于预测安全出炉时刻
PLAN_TOO_EARLY = "TOO_EARLY"      # 计划出炉早于预测安全出炉时刻，已失效风险
PLAN_CANNOT_VERIFY = "CANNOT_VERIFY"  # 有不可预测工件，无法判定计划是否有效

# 阻塞 / 告警原因代码
REASON_NO_READING = "NO_READING"                # 截至基准时刻无任何有效读数
REASON_UNDER_TEMP = "UNDER_TEMP"                # 最新读数欠温（低于窗口下限）
REASON_OVER_TEMP = "OVER_TEMP"                  # 最新读数超温（高于窗口上限）
REASON_STALE_READING = "STALE_READING"          # 最新读数超时未更新（缺报）
REASON_INSUFFICIENT_PROBES = "INSUFFICIENT_PROBES"  # 有效探头数量不足
ALERT_OVER_TEMP_HISTORY = "OVER_TEMP_HISTORY"   # 历史上曾超温（最新读数已恢复）
ALERT_CALIBRATION_RANGE = "CALIBRATION_RANGE"   # 读数超出校准点列区间，未计入保温累计


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _valid_count_at(per_probe, active, ts, threshold_minutes):
    """截至采样时刻 ts 的有效探头数（与 probes._probe_gaps 同一口径）。

    有效 = 截至 ts 有读数且末读数距 ts 不超过缺报阈值；
    首读数前的起始缺报不取消有效性（与 analyze 一致）。
    """
    thr = timedelta(minutes=threshold_minutes)
    valid = 0
    for pid in active:
        pts = [(t, v) for t, v in per_probe.get(pid, []) if t <= ts]
        if pts and (ts - pts[-1][0]) <= thr:
            valid += 1
    return valid


def _first_met(series, temp_min, temp_max, hold_minutes,
               insufficient_moments=(), gap_threshold_minutes=10):
    """按连续在窗口口径扫描：最后一次缺报之后的连续段首次累计达标时刻。

    与 cure.evaluate 同一保守规则：
    - 相邻两点都在窗口内，该区间才计入；
    - 相邻两点间隔超过缺报阈值 → PROBE_GAP，中间温度无法验证：
      该区间不计入，且**清零此前累计**，读数恢复后从重新验证的连续段重新累计。
    区间末端落在 insufficient_moments 中（该时刻有效探头不足）时，
    该时刻不能确认达标。返回 (首次达标时刻 datetime|None, 精确累计分钟)。
    """
    bad = set(insufficient_moments)
    acc = 0.0
    first_met = None
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        dt = (t1 - t0).total_seconds() / 60.0
        if dt <= 0:
            continue
        if dt > gap_threshold_minutes:
            # 内部缺报切断连续保温：缺报前累计作废，从恢复后的连续段重算
            acc = 0.0
            continue
        if temp_min <= v0 <= temp_max and temp_min <= v1 <= temp_max:
            before = acc
            acc += dt
            if first_met is None and t1 not in bad and acc + 1e-9 >= hold_minutes:
                need = max(0.0, hold_minutes - before)
                first_met = t0 + timedelta(minutes=need)
    # 保温要求为 0 的退化情况：首个在窗口内且探头充足的点即视为达标
    if first_met is None and hold_minutes <= 0:
        for ts, v in series:
            if temp_min <= v <= temp_max and ts not in bad:
                first_met = ts
                break
    return first_met, acc


def project_item(readings, probe_cfg, temp_min, temp_max, hold_minutes,
                 as_of, gap_threshold_minutes, divergence_c=5.0,
                 stuck_min_consecutive=5, min_valid_probes=1):
    """单件工件在炉进度与安全出炉预测。

    readings/probe_cfg 与 probes.analyze 相同（读数已由调用方按 as_of 截断）。
    as_of: 计算基准时刻 datetime（同时作为缺报测量窗口末端）。
    """
    analysis = probes.analyze(
        readings, probe_cfg, temp_min, temp_max, hold_minutes,
        gap_threshold_minutes, divergence_c, stuck_min_consecutive,
        min_valid_probes, window_end=as_of)
    channels, per_probe, active, _ = probes.split_channels(readings, probe_cfg)

    series = [(datetime.fromisoformat(p["ts"]), float(p["temp_c"]))
              for p in analysis["judgment_series"]]
    latest_ts = series[-1][0] if series else None
    latest_temp = series[-1][1] if series else None
    freshness = (round((as_of - latest_ts).total_seconds() / 60.0, 2)
                 if latest_ts is not None and as_of >= latest_ts else None)
    stale = freshness is not None and freshness > gap_threshold_minutes

    # 各判定序列采样时刻的有效探头数：用于确认首次达标时的探头条件重放
    insufficient_moments = set()
    for ts, _ in series:
        if _valid_count_at(per_probe, active, ts, gap_threshold_minutes) \
                < min_valid_probes:
            insufficient_moments.add(ts)

    first_met, acc_exact = _first_met(series, temp_min, temp_max, hold_minutes,
                                      insufficient_moments,
                                      gap_threshold_minutes)
    # 达标确认还要求该时刻读数不超时（其后长时间无读数时，达标时刻本身仍有效；
    # 但若达标判定依赖的最后读数距离 as_of 超时，则当前不能视为在保条件）
    alerts = _alerts(analysis, series, temp_min, temp_max, as_of,
                     gap_threshold_minutes, stale, first_met)

    blockers = []
    if first_met is not None:
        # 已达标：永久保留首次达标时刻；后续异常读数只保留告警
        status = STATUS_MET
        safe_at = first_met
        remaining = 0.0
    else:
        blockers = _blockers(analysis, series, temp_min, temp_max, as_of,
                             gap_threshold_minutes, stale)
        if blockers:
            status = STATUS_BLOCKED
            safe_at = None
            remaining = analysis["remaining_minutes"]
        else:
            # 最新温度在窗口内、探头充足、读数新鲜：按连续保温外推
            status = STATUS_TRACKING
            remaining = round(max(0.0, hold_minutes - acc_exact), 2)
            safe_at = (latest_ts + timedelta(minutes=remaining)
                       if remaining > 0 else latest_ts)

    return {
        "status": status,
        "status_text": STATUS_TEXT[status],
        "window": {"min_c": temp_min, "max_c": temp_max,
                   "hold_minutes": hold_minutes},
        "latest_reading_at": _iso(latest_ts),
        "latest_temp_c": latest_temp,
        "reading_freshness_minutes": freshness,
        "stale": stale,
        "in_window_minutes": analysis["in_window_minutes"],
        "remaining_hold_minutes": remaining,
        "valid_probe_count": analysis["valid_probe_count"],
        "min_valid_probes": analysis["min_valid_probes"],
        "first_met_at": _iso(first_met),
        "safe_unload_at": _iso(safe_at),
        "blockers": blockers,
        "alerts": alerts,
        # 绑定校准证书版本的探头：证书版本/插值区间/到期状态（质量追溯）
        "calibrations": [
            {"probe_id": p["probe_id"], **p["calibration"]}
            for p in analysis["probes"] if p.get("calibration")
        ],
    }


def _blockers(analysis, series, temp_min, temp_max, as_of,
              gap_threshold_minutes, stale):
    """不可预测原因：欠温 / 超温 / 缺报 / 探头不足（可同时多个）。"""
    out = []
    if not series:
        out.append({"code": REASON_NO_READING,
                    "message": f"截至 {_iso(as_of)} 尚无有效测温读数，"
                               "无法推算固化进度"})
        if analysis["insufficient_probes"]:
            out.append(_insufficient(analysis))
        return out
    latest_ts, latest_temp = series[-1]
    if latest_temp < temp_min:
        out.append({"code": REASON_UNDER_TEMP,
                    "message": f"最新最低校正温度 {latest_temp:g}℃ 低于窗口下限 "
                               f"{temp_min:g}℃（{_iso(latest_ts)}），尚未进入保温"})
    elif latest_temp > temp_max:
        out.append({"code": REASON_OVER_TEMP,
                    "message": f"最新最低校正温度 {latest_temp:g}℃ 超过窗口上限 "
                               f"{temp_max:g}℃（{_iso(latest_ts)}），须处置后再预测"})
    if stale:
        out.append({"code": REASON_STALE_READING,
                    "message": f"最新读数时刻 {_iso(latest_ts)} 距计算基准已 "
                               f"{(as_of - latest_ts).total_seconds() / 60.0:.0f} "
                               f"分钟，超过缺报阈值 {gap_threshold_minutes:g} 分钟"})
    if analysis["insufficient_probes"]:
        out.append(_insufficient(analysis))
    return out


def _insufficient(analysis):
    return {"code": REASON_INSUFFICIENT_PROBES,
            "message": f"有效探头 {analysis['valid_probe_count']} 个，少于要求的 "
                       f"{analysis['min_valid_probes']} 个，不得据此判定/预测合格"}


def _alerts(analysis, series, temp_min, temp_max, as_of,
            gap_threshold_minutes, stale, first_met):
    """告警：无论是否达标都保留（达标后的异常读数同样在此体现）。"""
    out = []
    if not series:
        out.append({"code": REASON_NO_READING,
                    "message": f"截至 {_iso(as_of)} 尚无有效测温读数"})
        if analysis["insufficient_probes"]:
            out.append(_insufficient(analysis))
        if analysis["calibration_range"]:
            out.append({"code": ALERT_CALIBRATION_RANGE,
                        "message": f"存在 {len(analysis['calibration_range'])} 个"
                                   "超出校准点列区间的读数，未计入保温累计",
                        "readings": analysis["calibration_range"]})
        return out

    latest_ts, latest_temp = series[-1]
    # 达标后出现的异常读数，告警文案中显式注明（达标状态仍保留）
    suffix = "（工件已达标，异常读数保留告警）" if first_met is not None else ""

    if latest_temp > temp_max:
        out.append({"code": REASON_OVER_TEMP,
                    "message": f"最新最低校正温度 {latest_temp:g}℃ 超过上限 "
                               f"{temp_max:g}℃（{_iso(latest_ts)}）{suffix}"})
    elif analysis["over_temp"]:
        out.append({"code": ALERT_OVER_TEMP_HISTORY,
                    "message": f"历史读数曾超温（最高 {analysis['max_temp_c']:g}℃），"
                               f"最新 {latest_temp:g}℃ 已回到窗口内{suffix}"})
    if latest_temp < temp_min:
        out.append({"code": REASON_UNDER_TEMP,
                    "message": f"最新最低校正温度 {latest_temp:g}℃ 低于下限 "
                               f"{temp_min:g}℃（{_iso(latest_ts)}）{suffix}"})
    if stale:
        out.append({"code": REASON_STALE_READING,
                    "message": f"读数缺报：最新 {_iso(latest_ts)}，"
                               f"已 {((as_of - latest_ts).total_seconds() / 60.0):.0f} "
                               f"分钟未更新（阈值 {gap_threshold_minutes:g} 分钟）"
                               + suffix})
    if analysis["insufficient_probes"]:
        out.append(_insufficient(analysis))
    if analysis["probe_gaps"]:
        out.append({"code": "PROBE_GAP",
                    "message": f"存在 {len(analysis['probe_gaps'])} 段探头缺报区间",
                    "intervals": analysis["probe_gaps"]})
    stuck = [a for p in analysis["probes"] if p["status"] == "ACTIVE"
             for a in p["anomalies"]]
    if stuck:
        out.append({"code": "STUCK_PROBE",
                    "message": f"存在 {len(stuck)} 段启用探头卡值区间",
                    "intervals": stuck})
    if analysis["divergences"]:
        out.append({"code": "PROBE_DIVERGENCE",
                    "message": f"存在 {len(analysis['divergences'])} 个时刻"
                               "有效探头温差超阈值",
                    "points": analysis["divergences"]})
    if analysis["calibration_range"]:
        out.append({"code": ALERT_CALIBRATION_RANGE,
                    "message": f"存在 {len(analysis['calibration_range'])} 个"
                               "超出校准点列区间的读数，未计入保温累计",
                    "readings": analysis["calibration_range"]})
    return out


def summarize(items, planned_unload_at, as_of, computed_at, basis_source):
    """炉次级预测：取所有工件最晚安全出炉时刻，校验计划出炉是否过早。

    items: project_item 返回的列表（已带 workpiece_id）
    planned_unload_at: 签发快照中的计划出炉时刻 datetime 或 None
    """
    met = [i for i in items if i["status"] == STATUS_MET]
    tracking = [i for i in items if i["status"] == STATUS_TRACKING]
    blocked = [i for i in items if i["status"] == STATUS_BLOCKED]

    # 只有所有工件都可预测时才给炉次安全出炉时刻（取最晚）；
    # 存在不可预测工件时整体时刻为 None，仅保留可预测工件的时刻供参考
    predictable_times = [datetime.fromisoformat(i["safe_unload_at"])
                         for i in items if i["safe_unload_at"]]
    safe_at = (max(predictable_times)
               if predictable_times and not blocked else None)

    if blocked:
        status = BATCH_BLOCKED
    elif tracking:
        status = BATCH_PREDICTABLE
    elif met:
        status = BATCH_ALL_MET
    else:
        status = BATCH_BLOCKED

    plan_status = PLAN_CANNOT_VERIFY
    early_minutes = None
    if safe_at is not None and planned_unload_at is not None:
        delta = (safe_at - planned_unload_at).total_seconds() / 60.0
        if delta > 0:
            plan_status = PLAN_TOO_EARLY
            early_minutes = round(delta, 2)
        else:
            plan_status = PLAN_OK
            early_minutes = 0.0

    return {
        "basis": {
            "as_of": _iso(as_of),
            "source": basis_source,
            "computed_at": _iso(computed_at),
        },
        "prediction_status": status,
        "prediction_status_text": BATCH_STATUS_TEXT[status],
        "met_count": len(met),
        "tracking_count": len(tracking),
        "blocked_count": len(blocked),
        "safe_unload_at": _iso(safe_at),
        "planned_unload_at": _iso(planned_unload_at),
        "plan_status": plan_status,
        "planned_unload_early_minutes": early_minutes,
        "items": items,
    }
