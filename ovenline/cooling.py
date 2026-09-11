"""冷却放行判定（纯函数）。

厚板走出炉膛时涂层已固化，但工件表面温度可能仍高于包装材料耐温上限。
合格件离炉后进入 COOLING 状态（暂不计为完成），持续按带时标的**表面
温度**观测：只有形成**连续低温区间**——表面温度持续不高于包装耐温上限
`pack_temp_limit_c`、连续保持达到 `low_temp_hold_minutes` 分钟——才允许
正常搬运放行（工件转 DONE）。

区间按**读数到达顺序**逐条处理（乱序不允许事后按时间重排洗白），
以下情形截断当前连续低温区间：

- **乱序** `OUT_OF_ORDER`：新读数时标早于已收录最新时标（时间倒流，
  无法证明中间时刻的温度）；
- **同点重复** `TS_CONFLICT` / `TS_DUPLICATE`：同一时标再次上报——
  温度不同为 `TS_CONFLICT`，**温度完全相同**为 `TS_DUPLICATE`；两种重复
  都照常保存本次提交并截断区间（重复点不锚新区间，从该点**之后**的读数
  重新累计连续低温时长）；
- **采样间隔过长** `LONG_GAP`：相邻读数间隔超过 `gap_threshold_minutes`
  （缺报期间温度无法验证，此前连续保持作废，与固化判定同一保守口径）；
- **再次升温** `REHEAT`：读数温度高于包装耐温上限（重新变热，
  低温连续性被破坏；下一条不超温读数另起新区间）。

区间有效保持分钟按保守口径累计：相邻两条读数**都**不超温时，该区间
时长才计入；若要求保持为 H 分钟、区间已累计 M 分钟，则按当前连续区间
计算的最早放行时刻 = 最新读数时刻 + (H − M)。
"""
from __future__ import annotations

from datetime import timedelta

# 读数收录类型
KIND_OK = "OK"
KIND_OUT_OF_ORDER = "OUT_OF_ORDER"
KIND_TS_CONFLICT = "TS_CONFLICT"
KIND_TS_DUPLICATE = "TS_DUPLICATE"   # 同点重复（同时刻同温度）：照常收录并截断

# 区间中断原因代码
BREAK_OUT_OF_ORDER = "OUT_OF_ORDER"   # 乱序读数截断区间
BREAK_TS_CONFLICT = "TS_CONFLICT"     # 同时刻不同温度截断区间
BREAK_TS_DUPLICATE = "TS_DUPLICATE"   # 同时刻同温度重复上报截断区间
BREAK_LONG_GAP = "LONG_GAP"           # 采样间隔过长截断区间
BREAK_REHEAT = "REHEAT"               # 再次升温（高于包装耐温上限）截断区间

# 放行未满足项代码
UNMET_MISSING_LIMIT = "PACK_LIMIT_MISSING"   # 粉料未登记包装冷却门限
UNMET_NO_READING = "NO_READING"              # 尚无冷却测温
UNMET_REHEAT = "REHEAT"                      # 当前读数高于耐温上限
UNMET_NOT_HELD = "HOLD_NOT_MET"              # 当前连续低温区间保持不足
UNMET_STALE = "STALE_READING"                # 最新读数距基准时刻过久（缺报）


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt is not None else None


def admit(readings, new_ts, new_temp):
    """判定一条新读数相对已收录读数的收录类型。

    readings: 已收录 [(ts datetime, temp, kind), ...]，按收录（到达）顺序
    返回 (kind, prev_ts)：
      OK               —— 正常新读数
      OUT_OF_ORDER     —— new_ts 早于已收录最新时标
      TS_CONFLICT      —— 同一时标已收录但温度不同
      TS_DUPLICATE     —— 同一时标已有完全相同温度（同点重复：不幂等忽略，
                          照常收录并由 build_segments 截断区间，从该点后重算）
    prev_ts 为截断时引用的已收录最新时标（正常时为时间上前一条）。
    """
    prev_ts = None
    for ts, temp, _kind in readings:
        if ts == new_ts:
            if temp == new_temp:
                return KIND_TS_DUPLICATE, ts
            return KIND_TS_CONFLICT, ts
        if prev_ts is None or ts > prev_ts:
            prev_ts = ts
    if prev_ts is not None and new_ts < prev_ts:
        return KIND_OUT_OF_ORDER, prev_ts
    return KIND_OK, prev_ts


def build_segments(readings, pack_temp_limit, gap_threshold_minutes=10):
    """按读数到达顺序构建连续低温区间与区间中断清单。

    readings: [(ts datetime, temp float, kind str), ...]，**按收录顺序**
              （id 升序），kind 为收录时标记（OK/OUT_OF_ORDER/TS_CONFLICT）。
              同点重复（TS_DUPLICATE/TS_CONFLICT）照常出现在序列中并截断区间。
    pack_temp_limit: 包装耐温上限 ℃；None 时不构建区间（门限缺失）。
    返回 dict：segments（所有已结束/进行中的低温段）、current（当前连续
              低温区间，无则 None）、interruptions（区间中断明细）。
    """
    segments = []
    interruptions = []
    current = None          # {"start","last","held_minutes"}
    prev = None             # 已收录最新时标（乱序/间隔判定锚点）
    last_temp = None

    def _close(end_ts, code, detail_extra=None):
        """结束当前低温区间并记录一次中断。"""
        nonlocal current
        if current is None:
            return
        seg = {**current, "end": end_ts, "break_code": code}
        segments.append(seg)
        item = {"code": code, "from": _iso(current["last"]),
                "to": _iso(end_ts),
                "held_minutes": round(current["held_minutes"], 2)}
        if detail_extra:
            item.update(detail_extra)
        interruptions.append(item)
        current = None

    if pack_temp_limit is None:
        return {"segments": [], "current": None, "interruptions": []}

    for ts, temp, kind in readings:
        if kind in (KIND_TS_CONFLICT, KIND_TS_DUPLICATE):
            # 同点重复（温度不同或完全相同）：本次提交照常保存，但该时刻
            # 不能与前后拼接——截断当前区间且重复点不锚新区间；下一条
            # 正常读数从该点之后重新累计（prev 已含此时标）
            code = (BREAK_TS_CONFLICT if kind == KIND_TS_CONFLICT
                    else BREAK_TS_DUPLICATE)
            _close(ts, code, {"at": _iso(ts), "temp_c": temp})
            prev = ts if prev is None or ts > prev else prev
            continue
        if kind == KIND_OUT_OF_ORDER or (prev is not None and ts < prev):
            # 乱序（收录时标记或防御性判定）：时间倒流，截断当前区间；
            # 若该乱序读数本身不超温，它锚定一个新的低温区间
            _close(ts, BREAK_OUT_OF_ORDER,
                   {"at": _iso(ts), "expected_after": _iso(prev),
                    "temp_c": temp})
            prev = ts if prev is None or ts > prev else prev
            if temp <= pack_temp_limit:
                current = {"start": ts, "last": ts, "held_minutes": 0.0}
            last_temp = temp
            continue

        if prev is not None:
            dt = (ts - prev).total_seconds() / 60.0
            if dt > gap_threshold_minutes:
                # 采样间隔过长：缺报期间无法验证，此前保持作废
                _close(ts, BREAK_LONG_GAP,
                       {"from": _iso(prev), "to": _iso(ts),
                        "minutes": round(dt, 2)})
            elif current is not None:
                if last_temp is not None and last_temp <= pack_temp_limit \
                        and temp <= pack_temp_limit:
                    current["held_minutes"] += max(0.0, dt)
        if temp > pack_temp_limit:
            # 再次升温：低温连续性被破坏，下一条不超温读数另起新区间
            if current is not None:
                _close(ts, BREAK_REHEAT,
                       {"at": _iso(ts), "temp_c": temp,
                        "limit_c": pack_temp_limit})
        elif current is None:
            # 新区间锚点（首个低温点 / 升温后恢复 / 长间隔后恢复）
            current = {"start": ts, "last": ts, "held_minutes": 0.0}
        else:
            current["last"] = ts
        prev = ts
        last_temp = temp

    return {"segments": segments, "current": current,
            "interruptions": interruptions}


def evaluate(readings, pack_temp_limit, low_temp_hold_minutes,
             as_of, gap_threshold_minutes=10):
    """单件工件冷却放行评估。

    readings: [(ts datetime, temp float, kind str), ...]，按收录顺序；
              只采用 ts <= as_of 的读数（历史复盘口径）。
    pack_temp_limit / low_temp_hold_minutes: 签发时冻结的包装门限，
              任一为 None 表示门限缺失（不得放行）。
    as_of: 计算基准时刻（最新读数距该时刻超过缺报阈值即视为读数陈旧）。
    返回 dict：当前读数、当前连续区间有效保持分钟、按当前连续区间计算的
              最早放行时刻、未满足项与完整区间/中断追溯。
    """
    missing = pack_temp_limit is None or low_temp_hold_minutes is None
    # 区间按读数到达（收录）顺序构建：过滤只按时标截断，不重排
    arrival = [(ts, t, k) for ts, t, k in readings if ts <= as_of]
    used = sorted(arrival, key=lambda r: r[0])  # 仅用于取最新时标读数
    built = build_segments(
        arrival, None if missing else pack_temp_limit, gap_threshold_minutes)
    current = built["current"]

    latest = max((r for r in used), key=lambda r: r[0], default=None)
    latest_ts = latest[0] if latest else None
    latest_temp = latest[1] if latest else None
    latest_kind = latest[2] if latest else None

    held = round(current["held_minutes"], 2) if current else 0.0
    remaining = round(max(0.0, (low_temp_hold_minutes or 0) - held), 2) \
        if not missing else None

    freshness = (round((as_of - latest_ts).total_seconds() / 60.0, 2)
                 if latest_ts is not None and as_of >= latest_ts else None)
    stale = freshness is not None and freshness > gap_threshold_minutes

    unmet = []
    if missing:
        unmet.append({"code": UNMET_MISSING_LIMIT,
                      "message": "粉料未登记包装温度上限/低温保持时长，"
                                 "签发快照缺少冷却放行门限"})
    if latest_ts is None:
        unmet.append({"code": UNMET_NO_READING,
                      "message": f"截至 {_iso(as_of)} 尚无冷却测温读数"})
    else:
        if latest_temp is not None and pack_temp_limit is not None \
                and latest_temp > pack_temp_limit:
            unmet.append({"code": UNMET_REHEAT,
                          "message": f"最新表面温度 {latest_temp:g}℃ 高于"
                                     f"包装耐温上限 {pack_temp_limit:g}℃"
                                     f"（{_iso(latest_ts)}），连续低温区间已截断"})
        if not missing and current is None and latest_temp is not None \
                and latest_temp <= pack_temp_limit:
            # 读数在阈值下但尚不能形成有效区间起点（理论上不会发生，防御）
            unmet.append({"code": UNMET_NOT_HELD,
                          "message": "当前连续低温区间尚未形成"})
        elif not missing and current is not None \
                and held + 1e-9 < low_temp_hold_minutes:
            unmet.append({"code": UNMET_NOT_HELD,
                          "message": f"当前连续低温区间已保持 {held:g} 分钟，"
                                     f"不足要求的 {low_temp_hold_minutes:g} 分钟"
                                     f"（还差 {remaining:g} 分钟）"})
        if stale:
            unmet.append({"code": UNMET_STALE,
                          "message": f"最新冷却读数 {_iso(latest_ts)} 距基准时刻"
                                     f"已 {freshness:g} 分钟，超过缺报阈值 "
                                     f"{gap_threshold_minutes:g} 分钟"})

    # 最早放行时刻：仅当存在进行中的低温区间且读数未陈旧时才可外推；
    # 门限缺失/无读数/最新超温时不可预测
    earliest = None
    if (not missing and current is not None and latest_ts is not None
            and not stale and latest_temp is not None
            and latest_temp <= pack_temp_limit):
        earliest = latest_ts + timedelta(minutes=remaining)
    releasable = not unmet

    # 已结束区间序列化（档案追溯：区间中断完整保留）
    seg_out = [{
        "start": _iso(s["start"]), "end": _iso(s["end"]),
        "held_minutes": round(s["held_minutes"], 2),
        "break_code": s["break_code"],
    } for s in built["segments"]]
    current_out = None
    if current is not None:
        current_out = {
            "start": _iso(current["start"]),
            "last_reading_at": _iso(current["last"]),
            "held_minutes": held,
            "required_minutes": low_temp_hold_minutes,
            "remaining_minutes": remaining,
        }
    return {
        "pack_temp_limit_c": pack_temp_limit,
        "low_temp_hold_minutes": low_temp_hold_minutes,
        "reading_count": len(used),
        "latest_reading_at": _iso(latest_ts),
        "latest_surface_temp_c": latest_temp,
        "latest_reading_kind": latest_kind,
        "reading_freshness_minutes": freshness,
        "stale": stale,
        "held_low_temp_minutes": held,
        "remaining_hold_minutes": remaining,
        "earliest_release_at": _iso(earliest),
        "releasable": releasable,
        "unmet": unmet,
        "current_segment": current_out,
        "closed_segments": seg_out,
        "interruptions": built["interruptions"],
        "as_of": _iso(as_of),
    }
