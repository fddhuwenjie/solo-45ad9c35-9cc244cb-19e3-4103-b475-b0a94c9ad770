"""多探头测温判定：校准偏移、判定序列合成与探头异常检测（纯函数）。

判定口径：
- 校正温度 = 探头原始值 + 校准偏移（偏移取签发时冻结的探头配置）；
- 判定序列 = 每个采样时刻各有效（未停用）探头校正温度的最低值，
  固化窗口分钟数按该序列累计，各探头原始值仍完整保留在 readings 表；
- 异常检测：同一探头连续相同读数（STUCK_PROBE 卡值）、
  同一时刻有效探头间温差超阈值（PROBE_DIVERGENCE）、
  判定序列相邻点间隔超阈值（PROBE_GAP，由 cure.evaluate 给出）。
"""
from __future__ import annotations

from . import cure


def analyze(readings, probe_cfg, temp_min, temp_max, hold_minutes,
            gap_threshold_minutes=10, divergence_c=5.0,
            stuck_min_consecutive=5, min_valid_probes=1):
    """综合评估单件工件在某炉次内的多探头测温。

    readings:  [(ts datetime, probe_id|None, 原始温度℃)]，顺序不限；
               probe_id 为 None 表示未登记探头工件的隐式单通道读数
    probe_cfg: {probe_id: {"offset_c": float, "status": "ACTIVE"/"DISABLED"}}
               签发快照或当前主数据；空 dict 表示该工件未登记探头
    返回: 固化判定 dict（cure.evaluate 字段 + 判定序列/探头状态/异常区间）
    """
    # 校正温度按探头通道归集
    channels = {pid: {"offset_c": float(c["offset_c"]), "status": c["status"]}
                for pid, c in probe_cfg.items()}
    per_probe = {pid: [] for pid in channels}
    for ts, pid, raw in readings:
        if pid is None:
            # 隐式单通道：未登记探头的工件，偏移按 0 计
            channels.setdefault(None, {"offset_c": 0.0, "status": "ACTIVE"})
            per_probe.setdefault(None, []).append((ts, float(raw)))
        elif pid in channels:
            per_probe[pid].append((ts, float(raw) + channels[pid]["offset_c"]))
        # 未绑定探头的读数在入库前已被拒收；此处防御性忽略
    for pts in per_probe.values():
        pts.sort(key=lambda p: p[0])

    active = [pid for pid, c in channels.items() if c["status"] == "ACTIVE"]

    # 判定序列：每个采样时刻取有效探头校正温度的最低值
    lowest = {}
    for pid in active:
        for ts, t in per_probe.get(pid, []):
            if ts not in lowest or t < lowest[ts]:
                lowest[ts] = t
    series = sorted(lowest.items())

    base = cure.evaluate(series, temp_min, temp_max, hold_minutes,
                         gap_threshold_minutes)

    # 卡值检测：逐探头（含已停用，保留其异常区间备查）
    probes_out = []
    for pid, ch in channels.items():
        pts = per_probe.get(pid, [])
        probes_out.append({
            "probe_id": pid,
            "offset_c": ch["offset_c"],
            "status": ch["status"],
            "reading_count": len(pts),
            "min_c": min((t for _, t in pts), default=None),
            "max_c": max((t for _, t in pts), default=None),
            "anomalies": _stuck_runs(pid, pts, stuck_min_consecutive,
                                     ch["status"]),
        })

    valid_count = sum(1 for pid in active if per_probe.get(pid))

    return {
        **base,
        "raw_reading_count": len(readings),
        "judgment_series": [{"ts": ts.isoformat(timespec="seconds"),
                             "temp_c": t} for ts, t in series],
        "probes": probes_out,
        "divergences": _divergences(per_probe, active, divergence_c),
        "valid_probe_count": valid_count,
        "min_valid_probes": min_valid_probes,
        "insufficient_probes": valid_count < min_valid_probes,
    }


def _stuck_runs(probe_id, points, min_consecutive, status):
    """连续相同校正值达到 min_consecutive 个点记为一段卡值区间。"""
    out = []
    run_start = 0
    for i in range(1, len(points) + 1):
        if i < len(points) and points[i][1] == points[run_start][1]:
            continue
        if i - run_start >= min_consecutive:
            out.append({
                "type": "STUCK_PROBE",
                "probe_id": probe_id,
                "probe_status": status,
                "value_c": points[run_start][1],
                "from": points[run_start][0].isoformat(timespec="seconds"),
                "to": points[i - 1][0].isoformat(timespec="seconds"),
                "count": i - run_start,
            })
        run_start = i
    return out


def _divergences(per_probe, active, threshold_c):
    """同一采样时刻有效探头校正值极差超过阈值记为探头温差异常。"""
    by_ts = {}
    for pid in active:
        for ts, t in per_probe.get(pid, []):
            by_ts.setdefault(ts, {})[pid] = t
    out = []
    for ts in sorted(by_ts):
        temps = by_ts[ts]
        if len(temps) < 2:
            continue
        spread = max(temps.values()) - min(temps.values())
        if spread > threshold_c:
            out.append({
                "type": "PROBE_DIVERGENCE",
                "ts": ts.isoformat(timespec="seconds"),
                "spread_c": round(spread, 2),
                "temps": {str(k): v for k, v in temps.items()},
            })
    return out
