"""炉次编排引擎：纯函数，不依赖 Flask / 数据库。

编排规则：
- 混炉时各粉料固化窗口取交集，交集为空则不得同炉；
- 同炉禁配组两两不可同炉；
- **吊具布置（racking.py）**：工件尺寸 / 重心 / 可旋转方向 / 吊耳映射到挂杆
  上的实际吊点坐标，检查净距、共享吊点、单点承重、横梁分区、横梁总载与
  左右力矩平衡；临时封掉的挂位（point_blackouts）在该炉次占用时段内禁用；
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

from . import racking

REASON_OVERSIZE = "OVERSIZE"             # 尺寸超过炉膛/挂杆
REASON_OVERWEIGHT = "OVERWEIGHT"         # 重量超过吊点/横梁承重
REASON_UNKNOWN_POWDER = "UNKNOWN_POWDER"  # 粉料批号未登记
REASON_BLOCKED = racking.REASON_BLOCKED   # 禁用吊点/禁用时段
REASON_BALANCE = racking.REASON_BALANCE   # 力矩/分区平衡无法满足

_MAX_ROUNDS = 8


def slots_needed(wp, oven):
    """工件占用的相邻挂位数（旧连续编号口径，保留兼容）：
    按水平投影尺寸与单吊点承重折算，取较大者。
    """
    by_size = math.ceil(max(wp["length_mm"], wp["width_mm"])
                        / oven["hanger_spacing_mm"])
    by_weight = math.ceil(wp["weight_kg"] / oven["hanger_max_load_kg"])
    return max(1, by_size, by_weight)


def hard_reject(wp, rack):
    """工件对该炉架空炉的硬不适合结果（任何时刻都放不下）；可放入返回 None。

    逐挂杆 / 旋转方向 / 吊点段尝试布置，返回首个冲突约束 dict。
    """
    first = None
    for rod in rack.rods:
        empty = racking._Layout(rack)
        res = empty.try_place(wp, blocked=set())
        if "wp" in res:
            return None
        if first is None:
            first = res
    return first or {"conflict": racking.F_ROD_SPAN, "rod_id": None,
                     "point_index": None, "detail": "无可行挂位"}


def oven_reject_reason(wp, oven, rack=None):
    """返回工件无法进入该炉的原因代码；可入炉返回 None（旧接口，兼容）。"""
    rack = rack or racking.build_rack(oven)
    r = hard_reject(wp, rack)
    if r is None:
        return None
    if r["conflict"] in (racking.F_CHAMBER, racking.F_ROD_SPAN,
                         racking.F_LUG_MATCH):
        return REASON_OVERSIZE
    return REASON_OVERWEIGHT


def _conflict(group_a, group_b, forbidden):
    if not group_a or not group_b:
        return False
    return (group_a, group_b) in forbidden or (group_b, group_a) in forbidden


class _Batch:
    """组批中的临时炉次：窗口/禁配 + 实际吊点布置。"""

    def __init__(self, oven, rack):
        self.oven = oven
        self.rack = rack
        self.layout = racking._Layout(rack)
        self.items = []            # [{wp, placement}]
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

    def can_add(self, wp, powder, forbidden, blocked=None):
        """窗口交集 / 禁配组通过后尝试实际吊点布置。

        返回 placement（成功）或冲突 dict；窗口/禁配不兼容返回冲突 dict
        （conflict='WINDOW'/'INCOMPAT'）。
        """
        wmin, wmax = self._window_with(powder)
        if wmin > wmax:
            return {"conflict": "WINDOW", "rod_id": None, "point_index": None,
                    "detail": "粉料固化窗口交集为空"}
        if any(_conflict(g, wp.get("compat_group"), forbidden)
               for g in self.groups):
            return {"conflict": "INCOMPAT", "rod_id": None, "point_index": None,
                    "detail": "同炉禁配组冲突"}
        return self.layout.try_place(wp, blocked=blocked)

    def add(self, wp, powder, placement):
        self.layout.commit(placement)
        self.items.append({"wp": wp, "placement": placement})
        self.window_min, self.window_max = self._window_with(powder)
        self.hold = max(self.hold, powder["hold_minutes"])
        self.weight += wp["weight_kg"]
        if wp.get("compat_group"):
            self.groups.add(wp["compat_group"])

    def reset(self):
        """清空布置与工件，保留炉号（重排轮复用炉壳）。"""
        self.layout = racking._Layout(self.rack)
        self.items = []
        self.window_min = self.window_max = None
        self.hold = 0.0
        self.weight = 0.0
        self.groups = set()


def _shift_past_blackouts(load_at, block_minutes, windows):
    """不可拆分占用区间撞上停机窗时整体后移；返回 (装载时刻, 被避让窗口)。"""
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


def _batch_due_key(b):
    dues = [i["wp"]["due_at"] for i in b.items if i["wp"].get("due_at")]
    return (not dues, min(dues) if dues else "")


def _schedule_oven(ov, batches, start_at, busy_until, windows):
    """炉内串行计时：炉次按最早交期排序衔接，停机窗整段避让。"""
    timed = {}
    available = max(start_at, busy_until)
    for b in sorted(batches, key=_batch_due_key):
        target = (b.window_min + b.window_max) / 2.0
        heatup = max(0.0, (target - ov["ambient_c"]) / ov["heat_rate_c_per_min"]) \
            + b.weight * ov["mass_factor_min_per_kg"]
        block = heatup + b.hold + ov["turnaround_minutes"]
        load_at, avoided = _shift_past_blackouts(available, block, windows)
        cure_start = load_at + timedelta(minutes=heatup)
        unload_at = cure_start + timedelta(minutes=b.hold)
        release = unload_at + timedelta(minutes=ov["turnaround_minutes"])
        timed[id(b)] = (load_at, unload_at, release, heatup, avoided)
        available = release
    return timed


def _blocked_for_interval(rack, interval, point_blackouts):
    """炉次占用区间 [load, release) 内禁用的吊点集合 {(rod_id, index)}。

    end_at 为 None 表示开放结束（吊点故障持续中），按 +inf 处理。
    """
    load, release = interval
    return {(pb["rod_id"], pb["point_index"])
            for pb in point_blackouts
            if pb["start_at"] < release
            and (pb["end_at"] is None or pb["end_at"] > load)}


def _time_all(ovens, batches, start_at, busy_until, blackouts):
    """逐炉计时（实际含停机窗 + 无停机基准），返回 {id(batch): actual+baseline}。"""
    timing = {}
    for ov in sorted(ovens, key=lambda o: o["id"]):
        obs = [b for b in batches if b.oven is ov]
        if not obs:
            continue
        windows = sorted(blackouts.get(ov["id"], []),
                         key=lambda w: w["start_at"])
        busy = busy_until.get(ov["id"], start_at)
        actual = _schedule_oven(ov, obs, start_at, busy, windows)
        baseline = _schedule_oven(ov, obs, start_at, busy, [])
        for b in obs:
            timing[id(b)] = actual[id(b)] + (baseline[id(b)],)
    return timing


def _choose_new_oven(wp, powder, ov_candidates, racks, open_batches, busy_until,
                     blackouts, point_blackouts, start_at, forbidden):
    """新建炉次选炉：逐炉试布并应用停机窗计时，按（逾期, 完工, 炉号）选优。

    布置候选按该炉全部禁用吊点做悲观试布，保证选入后不会因封点被赶出。
    返回 (best_batch, attempts: {oven_id: 布置结果})；全部放不下时 batch 为 None。
    """
    best = None
    attempts = {}
    for ov in ov_candidates:
        rack = racks[ov["id"]]
        candidate = _Batch(ov, rack)
        # 选炉时只做空炉可行性试布：吊点禁用只在与该炉次实际占用时段重叠时
        # 才封点（修复轮按计时区间处理），07:00 已结束的禁用窗不得封掉 08:00
        # 开排炉次的挂位
        res = candidate.can_add(wp, powder, forbidden, blocked=set())
        attempts[ov["id"]] = res
        if "wp" not in res:
            continue
        candidate.add(wp, powder, res)
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
    return (best[1] if best else None), attempts


def _classify_hard(hard):
    """旧口径汇总原因：任一炉尺寸类不过 → OVERSIZE；否则 OVERWEIGHT。"""
    if any(r["conflict"] in (racking.F_CHAMBER, racking.F_ROD_SPAN,
                             racking.F_LUG_MATCH) for r in hard.values()):
        return REASON_OVERSIZE, "工件尺寸超出所有炉膛尺寸或挂杆吊点跨度"
    return REASON_OVERWEIGHT, "工件重量超出所有炉膛吊点/横梁承重能力"


def _pick_first(hard):
    """逐炉硬冲突中选代表性首个冲突：尺寸类优先，再按炉号。"""
    def rank(item):
        oid, r = item
        size_first = 0 if r["conflict"] in (racking.F_CHAMBER,
                                            racking.F_ROD_SPAN,
                                            racking.F_LUG_MATCH) else 1
        return (size_first, oid)
    oid, r = min(hard.items(), key=rank)
    return oid, r


def _per_oven_view(hard):
    return [{"oven_id": oid, "code": r["conflict"], "rod_id": r.get("rod_id"),
             "point_index": r.get("point_index"), "detail": r.get("detail")}
            for oid, r in sorted(hard.items())]


def _place_into(wp, powder, ovens, racks, batches, forbidden, blocked_by_batch,
                busy_until, blackouts, point_blackouts, start_at, allow_new):
    """把一个工件放进现有炉次或新建炉次。

    blocked_by_batch: {id(batch): blocked set}（None 键表示不应用禁用吊点）。
    返回 (batch_or_None, attempts: {oven_id: result})。
    """
    attempts = {}
    for ov in sorted(ovens, key=lambda o: o["id"]):
        for b in batches:
            if b.oven is not ov:
                continue
            blk = blocked_by_batch.get(id(b), set()) \
                if blocked_by_batch is not None else set()
            res = b.can_add(wp, powder, forbidden, blocked=blk)
            attempts[ov["id"]] = res
            if "wp" in res:
                b.add(wp, powder, res)
                return b, attempts
    if allow_new:
        cand, new_attempts = _choose_new_oven(
            wp, powder, sorted(ovens, key=lambda o: o["id"]), racks, batches,
            busy_until, blackouts, point_blackouts, start_at, forbidden)
        attempts.update(new_attempts)
        if cand is not None:
            # 炉内只有该工件；布置/窗口已在 candidate 上完成，调用方负责入列
            return cand, attempts
    return None, attempts


def _rebuild_batch_from_placements(batch, wps, powders, placements):
    """用光束搜索得到的整组布置重建炉次（窗口/保温/总重/禁配组）。"""
    batch.reset()
    for wp, plc in zip(wps, placements):
        batch.add(wp, powders[wp["powder_batch"]], plc)


def _enforce_balance(batch, powders, blocked):
    """对一个炉次按整组方案重排吊点，使左右力矩满足容差。

    保持炉次成员顺序（交期优先的放入顺序）做光束搜索；若整组仍无法平衡，
    从尾部确定性撤出工件直到剩余前缀平衡，返回撤出工件 wp 列表。
    未设容差的炉架不做任何处理。
    """
    rack = batch.rack
    if (rack.moment_tolerance_kg_mm is None
            and rack.moment_tolerance_ratio is None) or not batch.items:
        return []
    ordered_wps = [i["wp"] for i in batch.items]
    for cut in range(len(ordered_wps), 0, -1):
        prefix = ordered_wps[:cut]
        empty = racking._Layout(rack)
        placements, fail_id = empty.balanced_layout(prefix, blocked=blocked)
        if placements is not None:
            evicted = ordered_wps[cut:]
            _rebuild_batch_from_placements(batch, prefix, powders, placements)
            return evicted
    # 连首件（单件整组）都无法满足容差：整批撤出并清空炉壳，
    # 由调用方按 MOMENT 首冲突报 LOAD_BALANCE
    batch.reset()
    return list(ordered_wps)


def _balance_only_failure(wp, powders, ovens, racks):
    """队列工件在无禁用吊点的空炉仍无法整组平衡 → (per_oven, first_conflict)。"""
    per_oven = []
    first = None
    for ov in sorted(ovens, key=lambda o: o["id"]):
        rack = racks[ov["id"]]
        if (rack.moment_tolerance_kg_mm is None
                and rack.moment_tolerance_ratio is None):
            continue
        empty = racking._Layout(rack)
        placements, _ = empty.balanced_layout([wp], blocked=set())
        view = {"oven_id": ov["id"],
                "code": None if placements is not None else racking.F_MOMENT,
                "rod_id": None, "point_index": None,
                "detail": None if placements is not None
                else "单件整组布置仍超出左右偏载容差"}
        per_oven.append(view)
        if placements is None and first is None:
            first = {"code": racking.F_MOMENT, "oven_id": ov["id"],
                     "rod_id": None, "point_index": None,
                     "detail": view["detail"]}
    if first is None:
        return None
    return per_oven, first


def build_plan(workpieces, powders, ovens, forbidden_pairs, start_at,
               busy_until=None, blackouts=None, point_blackouts=None,
               racks=None):
    """编排炉次并完成吊具布置。

    返回 (planned_batches, unscheduled)；unscheduled 元素含首个冲突约束
    first_conflict、逐炉拒绝明细 per_oven 与可选炉 alternative_ovens。
    """
    busy_until = busy_until or {}
    blackouts = blackouts or {}
    point_blackouts = point_blackouts or {}
    racks = racks or {ov["id"]: racking.build_rack(ov) for ov in ovens}
    unscheduled = []
    plannable = []

    # 硬可行性预检：空炉、忽略禁用时段也放不下 → 直接 unscheduled
    for wp0 in workpieces:
        wp = dict(wp0)
        if wp["powder_batch"] not in powders:
            unscheduled.append({
                "workpiece_id": wp["id"],
                "reason": REASON_UNKNOWN_POWDER,
                "detail": f"粉料批号 {wp['powder_batch']} 未登记",
            })
            continue
        hard = {}
        feasible = []
        for ov in ovens:
            r = hard_reject(wp, racks[ov["id"]])
            if r is None:
                feasible.append(ov["id"])
            else:
                hard[ov["id"]] = r
        if not feasible:
            oid, chosen = _pick_first(hard)
            code, summary = _classify_hard(hard)
            unscheduled.append({
                "workpiece_id": wp["id"], "reason": code, "detail": summary,
                "first_conflict": {"code": chosen["conflict"], "oven_id": oid,
                                   "rod_id": chosen.get("rod_id"),
                                   "point_index": chosen.get("point_index"),
                                   "detail": chosen.get("detail")},
                "per_oven": _per_oven_view(hard),
                "alternative_ovens": [],
            })
            continue
        wp["_feasible_ovens"] = feasible
        plannable.append(wp)

    # 交期优先，其次重件优先（提高挂位利用率）
    plannable.sort(key=lambda w: (w.get("due_at") is None, w.get("due_at") or "",
                                  -w["weight_kg"], w["id"]))

    # 第一轮：不考虑禁用吊点（乐观分组），保留旧的「先现有炉次、再选新炉」
    batches = []
    repair_attempts = {}
    for wp in plannable:
        powder = powders[wp["powder_batch"]]
        ov_cands = [o for o in ovens if o["id"] in wp["_feasible_ovens"]]
        b, attempts = _place_into(
            wp, powder, ov_cands, racks, batches, forbidden_pairs, None,
            busy_until, blackouts, point_blackouts, start_at, allow_new=True)
        if b is not None and b not in batches:
            batches.append(b)
        if b is None:
            repair_attempts[wp["id"]] = attempts

    timing = _time_all(ovens, batches, start_at, busy_until, blackouts)

    # 修复轮：两类问题统一处理——
    # (a) 炉次占用区间与吊点禁用时段相交 → 撤出占用封点的工件；
    # (b) 设有力矩容差的炉架按**整组方案**重排光束搜索，放不下平衡的工件
    #     从炉次撤出（单件偏载不在放入时拒绝），反复无法平衡的件在修复轮
    #     结束后判 LOAD_BALANCE。
    # 撤出工件按各炉次真实 blocked 集合重新分组；最后一轮对全部禁用吊点
    # 悲观布置保证收敛。
    queue = [wp for wp in plannable if wp["id"] in repair_attempts]
    balance_rejects = {}   # workpiece_id -> 末次整组平衡失败信息
    balance_failed = set()  # 已判定不可平衡（不再开新批反复搬移）
    for round_no in range(_MAX_ROUNDS):
        pessimistic = round_no == _MAX_ROUNDS - 1
        timing = _time_all(ovens, batches, start_at, busy_until, blackouts)
        blocked_by_batch = {}
        evict_ids = set()
        affected = set()
        for b in batches:
            if id(b) not in timing:
                continue
            load, _, release, _, _, _ = timing[id(b)]
            blk = _blocked_for_interval(
                racks[b.oven["id"]], (load, release),
                point_blackouts.get(b.oven["id"], []))
            blocked_by_batch[id(b)] = blk
            for item in b.items:
                used = {(item["placement"]["rod_id"], p["index"])
                        for p in item["placement"]["run"]}
                if used & blk:
                    evict_ids.add(item["wp"]["id"])
                    affected.add(id(b))
            # 整组力矩平衡（未设容差的炉架保持 first-fit 原布置不动）
            rack = racks[b.oven["id"]]
            if (rack.moment_tolerance_kg_mm is not None
                    or rack.moment_tolerance_ratio is not None):
                for wp in _enforce_balance(b, powders, blk):
                    evict_ids.add(wp["id"])
                    balance_rejects[wp["id"]] = racking.F_MOMENT
                    affected.add(id(b))
        # 只剩已判不可平衡的件（不再重放）且无待处理队列时收敛
        if not (evict_ids - balance_failed) and not queue:
            break
        # 受影响炉次整批清空（炉壳保留、重置），其工件与待处理队列合并重排。
        # 注意：平衡撤出时 _enforce_balance 可能已重置炉次，被撤出件以
        # evict_ids 为准从本轮待排工件中找回，不能只依赖此刻 b.items。
        requeue = list(queue)
        queue = []
        for b in batches:
            if id(b) in affected:
                requeue.extend(item["wp"] for item in b.items)
                b.reset()
        known = {w["id"]: w for w in plannable}
        for w_id in evict_ids:
            if all(w["id"] != w_id for w in requeue) and w_id in known:
                requeue.append(known[w_id])
        # 平衡类撤出件：先判空炉单件整组是否可行；不可平衡者直接标记，
        # 不再开新批反复搬移（最后统一报 LOAD_BALANCE）
        for w in requeue:
            if w["id"] in balance_rejects:
                bal_fail = _balance_only_failure(w, powders, ovens, racks)
                if bal_fail is not None:
                    balance_failed.add(w["id"])
        # 去重（保持交期优先顺序）；已判定不可平衡的件不再重放
        seen = set()
        deduped = []
        for w in sorted(requeue,
                        key=lambda w: (w.get("due_at") is None,
                                       w.get("due_at") or "",
                                       -w["weight_kg"], w["id"])):
            if w["id"] in seen or w["id"] in balance_failed:
                continue
            seen.add(w["id"])
            deduped.append(w)
        for wp in deduped:
            powder = powders[wp["powder_batch"]]
            ov_cands = [o for o in ovens if o["id"] in wp["_feasible_ovens"]]
            blk_map = dict(blocked_by_batch)
            if pessimistic:
                for b in batches:
                    blk_map[id(b)] = blk_map.get(id(b), set()) | {
                        (pb["rod_id"], pb["point_index"])
                        for pb in point_blackouts.get(b.oven["id"], [])}
            b, attempts = _place_into(
                wp, powder, ov_cands, racks, batches, forbidden_pairs,
                blk_map, busy_until, blackouts, point_blackouts, start_at,
                allow_new=True)
            if b is not None and b not in batches:
                batches.append(b)
            if b is None:
                queue.append(wp)
                repair_attempts[wp["id"]] = attempts
        # 删除被清空且重排后仍为空的炉壳
        batches = [b for b in batches if b.items]
        # 收敛：本轮无撤出/无新入队，或所有撤出件都已判为不可平衡/入队
        still_evictable = bool(evict_ids - balance_failed
                               - {w["id"] for w in queue})
        if not still_evictable and not queue:
            break
    timing = _time_all(ovens, batches, start_at, busy_until, blackouts)

    # 修复轮结束：对最终批次再做一次整组平衡，仍无法平衡的件直接判
    # LOAD_BALANCE（不再开新批）。修复轮中已反复撤出的不可平衡件
    # （balance_failed）一并汇总，保证既不出现在炉次，也进入 unscheduled。
    final_balance_evict = []
    for b in list(batches):
        rack = racks[b.oven["id"]]
        if (rack.moment_tolerance_kg_mm is None
                and rack.moment_tolerance_ratio is None) or not b.items:
            continue
        if id(b) not in timing:
            continue
        load, _, release, _, _, _ = timing[id(b)]
        blk = _blocked_for_interval(
            rack, (load, release), point_blackouts.get(b.oven["id"], []))
        for wp in _enforce_balance(b, powders, blk):
            balance_failed.add(wp["id"])
            final_balance_evict.append((b.oven["id"], wp))
    batches = [b for b in batches if b.items]
    wp_by_id = {w["id"]: w for w in plannable}
    for wid in sorted(balance_failed):
        wp = wp_by_id.get(wid)
        if wp is None or wid in {u["workpiece_id"] for u in unscheduled}:
            continue
        per_oven = []
        first = None
        for ov in sorted(ovens, key=lambda o: o["id"]):
            rack = racks[ov["id"]]
            if (rack.moment_tolerance_kg_mm is None
                    and rack.moment_tolerance_ratio is None):
                continue
            empty = racking._Layout(rack)
            placements, _ = empty.balanced_layout([wp], blocked=set())
            fail = placements is None
            view = {"oven_id": ov["id"],
                    "code": racking.F_MOMENT if fail else None,
                    "rod_id": None, "point_index": None,
                    "detail": "单件整组布置仍超出左右偏载容差" if fail else None}
            per_oven.append(view)
            if fail and first is None:
                first = {"code": racking.F_MOMENT, "oven_id": ov["id"],
                         "rod_id": None, "point_index": None,
                         "detail": view["detail"]}
        unscheduled.append({
            "workpiece_id": wp["id"], "reason": REASON_BALANCE,
            "detail": "所有可行炉的整组吊具方案均无法满足左右偏载容差",
            "first_conflict": first, "per_oven": per_oven,
            "alternative_ovens": wp.get("_feasible_ovens", []),
        })
    for wp in queue:
        if wp["id"] in balance_failed:
            continue
        bal_fail = _balance_only_failure(wp, powders, ovens, racks)
        if bal_fail is not None:
            per_oven, first = bal_fail
            unscheduled.append({
                "workpiece_id": wp["id"], "reason": REASON_BALANCE,
                "detail": "所有可行炉的整组吊具方案均无法满足左右偏载容差",
                "first_conflict": first, "per_oven": per_oven,
                "alternative_ovens": wp.get("_feasible_ovens", []),
            })
            continue
        attempts = repair_attempts.get(wp["id"], {})
        per_oven = []
        first = None
        for ov in sorted(ovens, key=lambda o: o["id"]):
            r = attempts.get(ov["id"])
            if r is None:
                continue
            view = {"oven_id": ov["id"], "code": r.get("conflict"),
                    "rod_id": r.get("rod_id"), "point_index": r.get("point_index"),
                    "detail": r.get("detail")}
            per_oven.append(view)
            if first is None and r.get("conflict") not in ("WINDOW", "INCOMPAT"):
                first = {"code": r["conflict"], "oven_id": ov["id"],
                         "rod_id": r.get("rod_id"),
                         "point_index": r.get("point_index"),
                         "detail": r.get("detail")}
        unscheduled.append({
            "workpiece_id": wp["id"], "reason": REASON_BLOCKED,
            "detail": "禁用时段/当前炉次占用下所有可行炉均无法布置",
            "first_conflict": first, "per_oven": per_oven,
            "alternative_ovens": wp.get("_feasible_ovens", []),
        })

    planned = _emit(ovens, batches, timing)
    planned.sort(key=lambda b: (b["planned_load_at"], b["oven_id"]))
    return planned, unscheduled


def _emit(ovens, batches, timing):
    """临时炉次转对外 dict（含吊具布置、载荷、力矩、搬入顺序）。"""
    planned = []
    for b in batches:
        ov = b.oven
        if id(b) not in timing:
            timing = {**timing, **_time_all(ovens, [b], datetime.min, {}, {})}
        (load_at, unload_at, release, heatup, avoided,
         base_tuple) = timing[id(b)]
        base_load, base_unload = base_tuple[0], base_tuple[1]
        wait_min = (load_at - base_load).total_seconds() / 60.0
        cure_start = load_at + timedelta(minutes=heatup)
        unload_iso = unload_at.isoformat(timespec="seconds")
        dues = [i["wp"]["due_at"] for i in b.items if i["wp"].get("due_at")]
        lateness, earliest_due = _lateness(unload_at, dues)
        base_lateness, _ = _lateness(base_unload, dues)
        report = b.layout.report()
        seq = {s["workpiece_id"]: s["sequence"]
               for s in report["load_in_sequence"]}
        items = []
        for i in b.items:
            plc = i["placement"]
            items.append({
                "workpiece_id": i["wp"]["id"],
                "order_id": i["wp"].get("order_id"),
                # 兼容旧字段：起始吊点取占位段在该挂杆的局部编号
                "hanger_slot": plc["run"][0]["index"],
                "slots_used": len(plc["run"]),
                "is_rework": bool(i["wp"].get("is_rework")),
                "due_at": i["wp"].get("due_at"),
                "late": bool(i["wp"].get("due_at"))
                        and unload_iso > i["wp"]["due_at"],
                "load_in_sequence": seq[i["wp"]["id"]],
                "placement": racking.placement_view(plc, b.rack),
            })
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
            "baseline_load_at": base_load.isoformat(timespec="seconds"),
            "baseline_unload_at": base_unload.isoformat(timespec="seconds"),
            "blackout_wait_minutes": round(wait_min, 2),
            "avoided_windows": [{
                "kind": w["kind"],
                "start_at": w["start_at"].isoformat(timespec="seconds"),
                "end_at": w["end_at"].isoformat(timespec="seconds"),
                "note": w.get("note"),
            } for w in avoided],
            "earliest_due_at": earliest_due,
            "lateness_minutes": round(lateness, 2),
            "baseline_lateness_minutes": round(base_lateness, 2),
            "lateness_delta_minutes": round(lateness - base_lateness, 2),
            # 吊具布置：挂杆/吊点坐标、旋转、各点载荷、分区、横梁总载、力矩、
            # 搬入顺序
            "rack_layout": report,
            "items": items,
        })
    return planned
