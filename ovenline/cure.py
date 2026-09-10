"""固化判定：按工件金属温度处于许可区间的累计分钟数评估。

累计规则：
- 相邻两个测温点都落在 [temp_min, temp_max] 内时，该区间时长计入有效
  固化分钟（保守口径，避免把升温沿计入）；
- 相邻测温点间隔超过缺报阈值（gap_threshold_minutes）时，中间温度无法
  验证，该区间既不计入保温，也**切断连续保温段**：缺报前已累计的分钟全部
  作废，读数恢复后只从重新得到验证的连续段（最后一次缺报之后的在窗区间）
  重新累计。缺报本身仍保留在 probe_gaps 中作为告警/标记。
"""
from __future__ import annotations


def evaluate(readings, temp_min, temp_max, hold_minutes, gap_threshold_minutes=10):
    """评估单件工件在某炉次内的固化情况。

    readings: [(datetime, 金属温度℃)]，须按时间升序
    返回: dict（累计分钟数、欠时/超温/探头中断判定）
    """
    # 只保留最近一个「无缺报、连续在窗口」段的累计分钟：
    # 内部缺报清零，读数恢复后从 0 重新累计
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
            # 缺报切断连续保温：该区间无法验证，此前累计作废
            in_window = 0.0
            continue
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
