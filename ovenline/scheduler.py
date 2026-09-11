"""炉次编排引擎：纯函数，不依赖 Flask / 数据库。

编排规则：
- 混炉时各粉料固化窗口取交集，交集为空则不得同炉；
- 同炉禁配组两两不可同炉；
- 工件按尺寸与重量折算占用挂位数（相邻挂位），不得超过炉内挂位总数；
- 保温时长取同炉各粉料要求的最大值；
- 升温时间 = (目标温度 - 环境温度) / 升温速率 + 装载重量 × 热惯性系数；
- 同炉目标温度取窗口中值；炉次按工件交期先后串行衔接；
- 装载、升温、保温及周转是一个不可拆分的占用区间，与停机窗
  （清炉/校准/检修）相交时整体移到该停机窗结束之后，再比较完工时刻与交期；
- 新建炉次选炉时先分别应用各炉停机窗试算完工时刻，按（逾期, 完工时刻, 炉号）
  选优，结果不随 ovens 传入顺序改变；
- 另保留一套不应用停机窗的基准排程，逐炉次计算停机造成的累计推迟
  （含上游炉次延误的传导），作为等待分钟与逾期变化的差值口径。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

REASON_OVERSIZE = "OVERSIZE"          # 尺寸超过炉膛/挂位
REASON_OVERWEIGHT = "OVERWEIGHT"      # 重量超过吊点承重能力
REASON_UNKNOWN_POWDER = "UNKNOWN_POWDER"  # 粉料批号未登记


def slots_needed(wp, oven):
    """工件占用的相邻挂位数：按水平投影尺寸与单吊点承重折算，取较大者。"""
    by_size = math.ceil(max(wp["length_mm"], wp["width_mm"]) / oven["hanger_spacing_mm"])
    by_weight = math.ceil(wp["weight_kg"] / oven["hanger_max_load_kg"])
    return max(1, by_size, by_weight)


def oven_reject_reason(wp, oven):
    """返回工件无法进入该炉的原因代码；可入炉返回 None。"""
    if not (wp["length_mm"] <= oven["chamber_l_mm"]
            and wp["width_mm"] <= oven["chamber_w_mm"]
            and wp["height_mm"] <= oven["chamber_h_mm"]):
        return REASON_OVERSIZE
    by_size = math.ceil(max(wp["length_mm"], wp["width_mm"]) / oven["hanger_spacing_mm"])
    if by_size > oven["hanger_slots"]:
        return REASON_OVERSIZE
    if slots_needed(wp, oven) > oven["hanger_slots"]:
        return REASON_OVERWEIGHT
    return None


def _conflict(group_a, group_b, forbidden):
    if not group_a or not group_b:
        return False
    return (group_a, group_b) in forbidden or (group_b, group_a) in forbidden


class _Batch:
    """组批中的临时炉次。"""

    def __init__(self, oven):
        self.oven = oven
        self.items = []            # [{wp, hanger_slot, slots_used}]
        self.used_slots = 0
        self.window_min = None
        self.window_max = None
        self.hold = 0.0
        self.weight = 0.0
        self.groups = set()

    def _window_with(self, powder):
        wmin = powder["temp_min_c"] if self.window_min is None \
            else max(self.window_min, powder["temp_min_c"])
        wmax = powder["temp_max_c"] if self.window_max is None \
            else min(self.window_max, powder["temp_max_c"])
        return wmin, wmax

    def can_add(self, wp, powder, forbidden):
        if self.used_slots + slots_needed(wp, self.oven) > self.oven["hanger_slots"]:
            return False
        wmin, wmax = self._window_with(powder)
        if wmin > wmax:  # 固化窗口交集为空
            return False
        return not any(_conflict(g, wp.get("compat_group"), forbidden)
                       for g in self.groups)

    def add(self, wp, powder):
        need = slots_needed(wp, self.oven)
        self.items.append({
            "wp": wp,
            "hanger_slot": self.used_slots + 1,  # 挂位从 1 起连续分配
            "slots_used": need,
        })
        self.used_slots += need
        self.window_min, self.window_max = self._window_with(powder)
        self.hold = max(self.hold, powder["hold_minutes"])
        self.weight += wp["weight_kg"]
        if wp.get("compat_group"):
            self.groups.add(wp["compat_group"])


def _shift_past_blackouts(load_at, block_minutes, windows):
    """不可拆分占用区间 [load_at, load_at+block) 撞上停机窗时整体后移。

    windows: [{"start_at", "end_at", ...}]，须按开始时刻升序且互不重叠；
    区间与某窗相交即把装载时刻推迟到该窗结束，后续窗口继续检查
    （窗口互不重叠，单趟扫描即可）。返回 (避让后装载时刻, 被避让的窗口列表)。
    """
    avoided = []
    for w in windows:
        block_end = load_at + timedelta(minutes=block_minutes)
        if load_at < w["end_at"] and block_end > w["start_at"]:
            avoided.append(w)
            load_at = w["end_at"]
    return load_at, avoided


def _lateness(unload_at, dues):
    """炉次逾期分钟：出炉时刻相对炉内最早交期的超出量（无交期为 0）。"""
    if not dues:
        return 0.0, None
    earliest = min(datetime.fromisoformat(d) for d in dues)
    late = max(0.0, (unload_at - earliest).total_seconds() / 60.0)
    return late, min(dues)


def _batch_due(b):
    dues = [i["wp"]["due_at"] for i in b.items if i["wp"].get("due_at")]
    return (not dues, min(dues) if dues else "")


def _schedule_oven(ov, batches, start_at, busy_until, windows):
    """炉内串行计时：炉次按最早交期排序衔接，停机窗整段避让。

    windows 传空列表即得无停机基准排程。
    返回 {id(batch): (load_at, unload_at, release_at, heatup, avoided)}。
    """
    timed = {}
    available = max(start_at, busy_until)
    for b in sorted(batches, key=_batch_due):
        target = (b.window_min + b.window_max) / 2.0
        heatup = max(0.0, (target - ov["ambient_c"]) / ov["heat_rate_c_per_min"]) \
            + b.weight * ov["mass_factor_min_per_kg"]
        # 装载→升温→保温→周转不可拆分：整段撞上停机窗则整体移到窗后
        block = heatup + b.hold + ov["turnaround_minutes"]
        load_at, avoided = _shift_past_blackouts(available, block, windows)
        cure_start = load_at + timedelta(minutes=heatup)
        unload_at = cure_start + timedelta(minutes=b.hold)
        release = unload_at + timedelta(minutes=ov["turnaround_minutes"])
        timed[id(b)] = (load_at, unload_at, release, heatup, avoided)
        available = release
    return timed


def _choose_oven(wp, powder, ovens, open_batches, busy_until, blackouts, start_at):
    """为新建炉次选炉：逐炉应用各自停机窗试算，按（逾期, 完工时刻, 炉号）选优。

    键中不含请求顺序相关量，同参数下结果不随 ovens 传入顺序改变。
    调用方已保证至少一台炉装得下该工件。
    """
    best = None
    for ov in ovens:
        if oven_reject_reason(wp, ov) is not None:
            continue
        candidate = _Batch(ov)
        candidate.add(wp, powder)
        siblings = [b for b in open_batches if b.oven is ov]
        windows = sorted(blackouts.get(ov["id"], []),
                         key=lambda w: w["start_at"])
        timed = _schedule_oven(ov, siblings + [candidate], start_at,
                               busy_until.get(ov["id"], start_at), windows)
        _, unload_at, _, _, _ = timed[id(candidate)]
        dues = [wp["due_at"]] if wp.get("due_at") else []
        late, _ = _lateness(unload_at, dues)
        key = (late, unload_at, ov["id"])
        if best is None or key < best[0]:
            best = (key, candidate)
    return best[1]


def build_plan(workpieces, powders, ovens, forbidden_pairs, start_at, busy_until=None,
               blackouts=None):
    """编排炉次。

    workpieces:      待排产工件 dict 列表（含 powder_batch / compat_group / due_at）
    powders:         {批号: {temp_min_c, temp_max_c, hold_minutes}}
    ovens:           炉膛 dict 列表
    forbidden_pairs: {(组A, 组B)} 同炉禁配组
    start_at:        排产起点 datetime
    busy_until:      {炉号: datetime} 已签发/在炉炉次占用到的时刻
    blackouts:       {炉号: [{"start_at", "end_at", "kind", "note"}]} 停机窗
                     （清炉/校准/检修），整段占用区间须避让

    返回 (planned_batches, unscheduled)
    """
    busy_until = busy_until or {}
    blackouts = blackouts or {}
    unscheduled = []
    plannable = []

    for wp in workpieces:
        if wp["powder_batch"] not in powders:
            unscheduled.append({
                "workpiece_id": wp["id"],
                "reason": REASON_UNKNOWN_POWDER,
                "detail": f"粉料批号 {wp['powder_batch']} 未登记",
            })
            continue
        reasons = {}
        for ov in ovens:
            r = oven_reject_reason(wp, ov)
            if r is None:
                reasons = None
                break
            reasons.setdefault(r, []).append(ov["id"])
        if reasons is not None:
            if REASON_OVERSIZE in reasons:
                code, detail = REASON_OVERSIZE, "工件尺寸超出所有炉膛尺寸或挂位跨度"
            else:
                code, detail = REASON_OVERWEIGHT, "工件重量超出所有炉膛吊点承重能力"
            unscheduled.append({"workpiece_id": wp["id"], "reason": code, "detail": detail})
            continue
        plannable.append(wp)

    # 交期优先，其次重件优先（提高挂位利用率）
    plannable.sort(key=lambda w: (w.get("due_at") is None, w.get("due_at") or "",
                                  -w["weight_kg"], w["id"]))

    open_batches = []
    for wp in plannable:
        powder = powders[wp["powder_batch"]]
        for b in open_batches:
            if b.can_add(wp, powder, forbidden_pairs):
                b.add(wp, powder)
                break
        else:
            # 新炉次选炉：先分别应用各炉停机窗试算完工时刻再选优
            open_batches.append(_choose_oven(wp, powder, ovens, open_batches,
                                             busy_until, blackouts, start_at))

    # 计时：每台炉的炉次按最早交期排序，串行衔接；停机窗整段避让。
    # 另跑一套不应用停机窗的基准排程，逐炉次差值即停机造成的累计推迟
    # （上游炉次被推迟后，下游炉次的等待/逾期变化同样计入）。
    planned = []
    for ov in sorted(ovens, key=lambda o: o["id"]):
        obs = [b for b in open_batches if b.oven is ov]
        if not obs:
            continue
        windows = sorted(blackouts.get(ov["id"], []),
                         key=lambda w: w["start_at"])
        busy = busy_until.get(ov["id"], start_at)
        actual = _schedule_oven(ov, obs, start_at, busy, windows)
        baseline = _schedule_oven(ov, obs, start_at, busy, [])
        for b in sorted(obs, key=_batch_due):
            load_at, unload_at, release, heatup, avoided = actual[id(b)]
            base_load, base_unload, _, _, _ = baseline[id(b)]
            wait_min = (load_at - base_load).total_seconds() / 60.0
            cure_start = load_at + timedelta(minutes=heatup)
            unload_iso = unload_at.isoformat(timespec="seconds")
            dues = [i["wp"]["due_at"] for i in b.items if i["wp"].get("due_at")]
            lateness, earliest_due = _lateness(unload_at, dues)
            base_lateness, _ = _lateness(base_unload, dues)
            planned.append({
                "oven_id": ov["id"],
                "window_min_c": b.window_min,
                "window_max_c": b.window_max,
                "hold_minutes": b.hold,
                "total_weight_kg": round(b.weight, 3),
                "heatup_minutes": round(heatup, 2),
                "planned_load_at": load_at.isoformat(timespec="seconds"),
                "planned_cure_start_at": cure_start.isoformat(timespec="seconds"),
                "planned_unload_at": unload_iso,
                "turnaround_end_at": release.isoformat(timespec="seconds"),
                # 停机避让计算依据：无停机基准排程时刻、直接避让的窗口、
                # 以及相对基准的累计推迟分钟（含上游延误传导）
                "baseline_load_at": base_load.isoformat(timespec="seconds"),
                "baseline_unload_at": base_unload.isoformat(timespec="seconds"),
                "blackout_wait_minutes": round(wait_min, 2),
                "avoided_windows": [{
                    "kind": w["kind"],
                    "start_at": w["start_at"].isoformat(timespec="seconds"),
                    "end_at": w["end_at"].isoformat(timespec="seconds"),
                    "note": w.get("note"),
                } for w in avoided],
                # 逾期：出炉时刻相对炉内最早交期；delta 为相对无停机基准的变化
                "earliest_due_at": earliest_due,
                "lateness_minutes": round(lateness, 2),
                "baseline_lateness_minutes": round(base_lateness, 2),
                "lateness_delta_minutes": round(lateness - base_lateness, 2),
                "items": [{
                    "workpiece_id": i["wp"]["id"],
                    "order_id": i["wp"].get("order_id"),
                    "hanger_slot": i["hanger_slot"],
                    "slots_used": i["slots_used"],
                    "is_rework": bool(i["wp"].get("is_rework")),
                    "due_at": i["wp"].get("due_at"),
                    "late": bool(i["wp"].get("due_at")) and unload_iso > i["wp"]["due_at"],
                } for i in b.items],
            })
    planned.sort(key=lambda b: (b["planned_load_at"], b["oven_id"]))
    return planned, unscheduled
