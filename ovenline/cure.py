"""固化判定：按工件金属温度处于许可区间的累计分钟数评估。

累计规则：相邻两个测温点都落在 [temp_min, temp_max] 内时，
该区间时长计入有效固化分钟数（保守口径，避免把升温沿计入）。
"""
from __future__ import annotations


def evaluate(readings, temp_min, temp_max, hold_minutes, gap_threshold_minutes=10):
    """评估单件工件在某炉次内的固化情况。

    readings: [(datetime, 金属温度℃)]，须按时间升序
    返回: dict（累计分钟数、欠时/超温/探头中断判定）
    """
    in_window = 0.0
    gaps = []
    max_temp = None
    over = False

    for _, t in readings:
        if max_temp is None or t > max_temp:
            max_temp = t
        if t > temp_max:
            over = True

    for (t0, v0), (t1, v1) in zip(readings, readings[1:]):
        dt = (t1 - t0).total_seconds() / 60.0
        if dt <= 0:
            continue
        if dt > gap_threshold_minutes:
            gaps.append({
                "from": t0.isoformat(timespec="seconds"),
                "to": t1.isoformat(timespec="seconds"),
                "minutes": round(dt, 2),
            })
        if temp_min <= v0 <= temp_max and temp_min <= v1 <= temp_max:
            in_window += dt

    return {
        "reading_count": len(readings),
        "in_window_minutes": round(in_window, 2),
        "required_hold_minutes": hold_minutes,
        "remaining_minutes": round(max(0.0, hold_minutes - in_window), 2),
        "under_time": in_window < hold_minutes,
        "over_temp": over,
        "max_temp_c": max_temp,
        "probe_gaps": gaps,
    }
