"""炉次编排引擎：纯函数，不依赖 Flask / 数据库。

编排规则：
- 混炉时各粉料固化窗口取交集，交集为空则不得同炉；
- 同炉禁配组两两不可同炉；
- 工件按尺寸与重量折算占用挂位数（相邻挂位），不得超过炉内挂位总数；
- 保温时长取同炉各粉料要求的最大值；
- 升温时间 = (目标温度 - 环境温度) / 升温速率 + 装载重量 × 热惯性系数；
- 同炉目标温度取窗口中值；炉次按工件交期先后串行衔接。
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


def build_plan(workpieces, powders, ovens, forbidden_pairs, start_at, busy_until=None):
    """编排炉次。

    workpieces:      待排产工件 dict 列表（含 powder_batch / compat_group / due_at）
    powders:         {批号: {temp_min_c, temp_max_c, hold_minutes}}
    ovens:           炉膛 dict 列表
    forbidden_pairs: {(组A, 组B)} 同炉禁配组
    start_at:        排产起点 datetime
    busy_until:      {炉号: datetime} 已签发/在炉炉次占用到的时刻

    返回 (planned_batches, unscheduled)
    """
    busy_until = busy_until or {}
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
            # 选占用率最低且装得下的炉
            cand = [ov for ov in ovens if oven_reject_reason(wp, ov) is None]
            cand.sort(key=lambda ov: sum(b.used_slots for b in open_batches
                                         if b.oven is ov) / ov["hanger_slots"])
            b = _Batch(cand[0])
            b.add(wp, powder)
            open_batches.append(b)

    # 计时：每台炉的炉次按最早交期排序，串行衔接
    planned = []
    for ov in ovens:
        obs = [b for b in open_batches if b.oven is ov]

        def batch_due(b):
            dues = [i["wp"]["due_at"] for i in b.items if i["wp"].get("due_at")]
            return (not dues, min(dues) if dues else "")

        obs.sort(key=batch_due)
        available = max(start_at, busy_until.get(ov["id"], start_at))
        for b in obs:
            target = (b.window_min + b.window_max) / 2.0
            heatup = max(0.0, (target - ov["ambient_c"]) / ov["heat_rate_c_per_min"]) \
                + b.weight * ov["mass_factor_min_per_kg"]
            load_at = available
            cure_start = load_at + timedelta(minutes=heatup)
            unload_at = cure_start + timedelta(minutes=b.hold)
            available = unload_at + timedelta(minutes=ov["turnaround_minutes"])
            unload_iso = unload_at.isoformat(timespec="seconds")
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
