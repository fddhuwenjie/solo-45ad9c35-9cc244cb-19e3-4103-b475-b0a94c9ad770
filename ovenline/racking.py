"""吊具布置与载荷平衡引擎：纯函数，不依赖 Flask / 数据库。

大门板若全挂在炉架一侧，即使每个吊点没超载，横梁也会偏载。本模块把
「挂杆 → 吊点坐标 → 单点限载 / 横梁分区载荷 / 左右力矩容差」的实际炉架
模型与工件（尺寸 / 重心 / 可旋转方向 / 吊耳）映射到**具体吊点坐标**，
返回可复现的挂位组合与搬入顺序。

布置检查（按固定顺序，返回首个冲突约束代码）：
1. CHAMBER_FIT   旋转后尺寸超出炉膛；
2. ROD_SPAN      沿杆投影超出挂杆吊点跨度；
3. POINT_BLOCKED 命中禁用吊点（临时封掉的挂位 / 禁用时段）；
4. POINT_TAKEN   共享吊点（净距不足 / 投影重叠）；
5. LUG_MATCH     吊耳坐标对不上任何吊点（lift_mode=LUG 时）；
6. CG_SUPPORT    重心落在承重吊点构成的支撑跨之外（会侧翻）；
7. POINT_LOAD    单点承重超限；
8. ZONE_LOAD     横梁分区局部承重超限；
9. BEAM_TOTAL    横梁总载超限；
10. MOMENT       左右偏载超容差。

载荷模型：
- UNIFORM（默认，兼容旧连续编号方案）：工件投影覆盖的相邻吊点均承重，
  重量按重心到各吊点的力臂静力学分配（重心居中即均摊）；
- LUG：工件显式给出吊耳坐标（lift_points_mm，沿工件长轴），承重吊点为
  吊耳对齐的吊点；两个吊耳按杠杆法分配，多个吊耳按刚性梁均摊。
非承重吊点（投影内但非吊耳对齐）只占位不承重。
"""
from __future__ import annotations

import math

# 首个冲突约束代码（检查顺序见模块文档）
F_CHAMBER = "CHAMBER_FIT"
F_ROD_SPAN = "ROD_SPAN"
F_POINT_BLOCKED = "POINT_BLOCKED"
F_POINT_TAKEN = "POINT_TAKEN"
F_LUG_MATCH = "LUG_MATCH"
F_CG_SUPPORT = "CG_SUPPORT"
F_POINT_LOAD = "POINT_LOAD"
F_ZONE_LOAD = "ZONE_LOAD"
F_BEAM_TOTAL = "BEAM_TOTAL"
F_MOMENT = "MOMENT"

# 放不下工件的汇总原因
REASON_OVERSIZE = "OVERSIZE"          # 尺寸超过炉膛/挂杆跨度
REASON_OVERWEIGHT = "OVERWEIGHT"      # 重量超过吊点/横梁承重
REASON_BLOCKED = "POINT_BLACKOUT"     # 禁用吊点/禁用时段导致无法布置
REASON_BALANCE = "LOAD_BALANCE"       # 力矩/分区平衡无法满足

# 硬不适合（任何时刻、任何炉次都放不进该炉）的冲突代码
HARD_CONFLICTS = {F_CHAMBER, F_ROD_SPAN, F_LUG_MATCH, F_POINT_LOAD,
                  F_ZONE_LOAD, F_BEAM_TOTAL, F_CG_SUPPORT}
# 临时性冲突（禁用时段结束、炉内清空后即可布置）
SOFT_CONFLICTS = {F_POINT_BLOCKED, F_POINT_TAKEN, F_MOMENT}

DEFAULT_CLEARANCE_MM = 0.0
DEFAULT_LUG_TOLERANCE_MM = 50.0


# ---------------------------------------------------------------- 炉架模型

class Rack:
    """一台炉的挂杆 / 吊点 / 横梁分区模型。

    rods: [{id, axis, y_mm, z_mm, point_spacing_mm, point_max_load_kg,
             default_point_load_kg, points:[{index,x_mm,max_load_kg,blocked}],
             zones:[{id,x_min_mm,x_max_mm,max_load_kg}]}]
    其余为炉级限制与容差。
    """

    def __init__(self, oven_id, rods, *, chamber_l, chamber_w, chamber_h,
                 beam_max_load_kg=None, moment_tolerance_kg_mm=None,
                 moment_tolerance_ratio=None, default_clearance_mm=0.0,
                 lug_tolerance_mm=DEFAULT_LUG_TOLERANCE_MM):
        self.oven_id = oven_id
        self.rods = rods
        self.chamber_l = chamber_l
        self.chamber_w = chamber_w
        self.chamber_h = chamber_h
        self.beam_max_load_kg = beam_max_load_kg
        self.moment_tolerance_kg_mm = moment_tolerance_kg_mm
        self.moment_tolerance_ratio = moment_tolerance_ratio
        self.default_clearance_mm = default_clearance_mm
        self.lug_tolerance_mm = lug_tolerance_mm
        self._rod = {r["id"]: r for r in rods}

    # -- 查询 ----------------------------------------------------------
    def rod(self, rod_id):
        return self._rod.get(rod_id)

    def point(self, rod_id, index):
        r = self._rod.get(rod_id)
        if r is None:
            return None
        for p in r["points"]:
            if p["index"] == index:
                return p
        return None

    def rod_x_bounds(self, rod):
        xs = [p["x_mm"] for p in rod["points"]]
        return min(xs), max(xs)

    def zone_of(self, rod, x):
        for z in rod.get("zones", []):
            if z["x_min_mm"] <= x <= z["x_max_mm"]:
                return z
        return None

    def block_point(self, rod_id, index, blocked=True):
        p = self.point(rod_id, index)
        if p is not None:
            p["blocked"] = blocked

    # -- 旋转后的工件外廓 ----------------------------------------------
    def orientations(self, wp):
        """可旋转方向（去重、数值升序，保证可复现）。

        0°：沿杆方向取 length_mm；90°：沿杆方向取 width_mm。
        其余角度按「投影到沿杆方向」折算（length 方向与杆轴夹角）。
        """
        rots = sorted({int(r) for r in wp.get("allowed_rotations_deg") or [0, 90]})
        out = []
        seen = set()
        for deg in rots:
            rad = math.radians(deg)
            along = abs(wp["length_mm"] * math.cos(rad)) \
                + abs(wp["width_mm"] * math.sin(rad))
            across = abs(wp["length_mm"] * math.sin(rad)) \
                + abs(wp["width_mm"] * math.cos(rad))
            key = (round(along, 6), round(across, 6))
            if key in seen:
                continue
            seen.add(key)
            out.append({"rotation_deg": deg, "along_mm": along,
                        "across_mm": across, "height_mm": wp["height_mm"]})
        return out


def build_rack(oven):
    """由试算请求中的炉膛 dict 构建 Rack（无 hanger_rack 时合成默认单杆）。

    默认模型（兼容旧连续编号）：单根挂杆 R1 沿炉长方向，hanger_slots 个
    吊点，间距 hanger_spacing_mm，从 0 起等距分布，吊点承重 hanger_max_load_kg；
    不设分区、不设横梁总载/力矩容差（None = 不检查，保持旧行为）。
    """
    hr = oven.get("hanger_rack")
    chamber_l = float(oven["chamber_l_mm"])
    chamber_w = float(oven["chamber_w_mm"])
    chamber_h = float(oven["chamber_h_mm"])
    if not hr:
        n = int(oven["hanger_slots"])
        spacing = float(oven["hanger_spacing_mm"])
        max_load = float(oven["hanger_max_load_kg"])
        # 吊点以挂杆中心（x=0，横梁回转中心）对称布置：左负右正，
        # 第一个吊点仍编号 1（hanger_slot 兼容旧连续编号）
        x0 = -(n - 1) * spacing / 2.0
        points = [{"index": i + 1, "x_mm": x0 + i * spacing,
                   "max_load_kg": max_load, "blocked": False}
                  for i in range(n)]
        rods = [{"id": "R1", "axis": "L", "y_mm": chamber_w / 2.0,
                 "z_mm": float(chamber_h), "point_spacing_mm": spacing,
                 "default_point_load_kg": max_load, "points": points,
                 "zones": []}]
        return Rack(oven["id"], rods, chamber_l=chamber_l,
                    chamber_w=chamber_w, chamber_h=chamber_h)

    defn_points = hr.get("points") or []
    rods = []
    rod_ids = set()
    for rr in hr.get("rods", []):
        rid = str(rr["id"])
        if rid in rod_ids:
            raise ValueError(f"挂杆 id 重复: {rid}")
        rod_ids.add(rid)
        axis = rr.get("axis", "L")
        if axis not in ("L", "W"):
            raise ValueError(f"挂杆 {rid} 的 axis 只支持 L（沿炉长）/ W（沿炉宽）")
        spacing = float(rr.get("point_spacing_mm")
                        or oven["hanger_spacing_mm"])
        default_load = float(rr.get("default_point_load_kg")
                             or oven["hanger_max_load_kg"])
        pts = []
        seen_index = set()
        for pp in defn_points:
            if str(pp.get("rod_id")) != rid:
                continue
            idx = int(pp["index"])
            if idx in seen_index:
                raise ValueError(f"挂杆 {rid} 吊点编号重复: {idx}")
            seen_index.add(idx)
            pts.append({"index": idx, "x_mm": float(pp["x_mm"]),
                        "max_load_kg": float(pp.get("max_load_kg", default_load)),
                        "blocked": False})
        if not pts:
            # 未显式给坐标：按间距从 0 等距生成
            n = int(rr.get("point_count") or oven["hanger_slots"])
            pts = [{"index": i + 1, "x_mm": i * spacing,
                    "max_load_kg": default_load, "blocked": False}
                   for i in range(n)]
        pts.sort(key=lambda p: p["index"])
        zones = []
        for z in rr.get("zones", []) or []:
            zones.append({"id": str(z["id"]),
                          "x_min_mm": float(z["x_min_mm"]),
                          "x_max_mm": float(z["x_max_mm"]),
                          "max_load_kg": float(z["max_load_kg"])})
        rods.append({"id": rid, "axis": axis,
                     "y_mm": float(rr.get("y_mm", 0.0)),
                     "z_mm": float(rr.get("z_mm", chamber_h)),
                     "point_spacing_mm": spacing,
                     "default_point_load_kg": default_load,
                     "points": pts, "zones": zones})
    if not rods:
        raise ValueError("hanger_rack.rods 至少需要一根挂杆")
    beam = hr.get("beam_max_load_kg")
    mom_abs = hr.get("moment_tolerance_kg_mm")
    mom_ratio = hr.get("moment_tolerance_ratio")
    return Rack(
        oven["id"], rods, chamber_l=chamber_l, chamber_w=chamber_w,
        chamber_h=chamber_h,
        beam_max_load_kg=(float(beam) if beam is not None else None),
        moment_tolerance_kg_mm=(float(mom_abs) if mom_abs is not None else None),
        moment_tolerance_ratio=(float(mom_ratio) if mom_ratio is not None else None),
        default_clearance_mm=float(hr.get("default_clearance_mm",
                                          DEFAULT_CLEARANCE_MM)),
        lug_tolerance_mm=float(hr.get("lug_tolerance_mm",
                                      DEFAULT_LUG_TOLERANCE_MM)))


# ---------------------------------------------------------------- 几何与候选

def piece_extents(center, run, rod):
    """工件沿杆方向的占位区间 [lo, hi]（含覆盖吊点的半间距）。"""
    xs = [p["x_mm"] for p in run]
    half = rod["point_spacing_mm"] / 2.0
    return min(xs) - half, max(xs) + half


def _collides(existing, lo, hi, across_half, rod):
    """与同杆已布置工件的净距检查（含工件要求净距）。

    existing: [{"lo","hi","across_half","clearance"}]（同杆）。
    跨杆方向：按相对挂杆中心的横向半宽重叠才算占位冲突（多杆时）。
    """
    for e in existing:
        if e["rod_id"] != rod["id"]:
            continue
        gap_need = (e["clearance"] or 0.0) / 2.0
        # 沿杆净距
        if lo < e["hi"] + gap_need and hi > e["lo"] - gap_need:
            return True
    return False


def _candidate_runs(rack, rod, wp, ori):
    """该挂杆 + 该旋转方向下所有可能的相邻吊点段（按段编号、长度确定性枚举）。

    段为编号连续的吊点；段长 k 取沿杆投影所需（ceil(along/spacing) 折成点数），
    同时覆盖显式吊耳数量（LUG 模式）。
    """
    spacing = rod["point_spacing_mm"]
    pts = rod["points"]
    k = max(1, math.ceil(ori["along_mm"] / spacing))
    lugs = wp.get("lift_points_mm")
    if lugs:
        k = max(k, len(lugs))
    runs = []
    for i in range(len(pts) - k + 1):
        run = pts[i:i + k]
        # 编号必须连续
        if [p["index"] for p in run] == list(range(run[0]["index"],
                                                   run[0]["index"] + k)):
            runs.append(run)
    return runs


def _lug_alignments(rod, run, wp, ori, tol):
    """LUG 模式：把工件吊耳对齐到段内吊点（平移不变）。

    吊耳坐标 lift_points_mm 沿工件长轴（相对工件中心，负左正右）。
    逐吊点作为第一个吊耳的对齐基准，要求其余吊耳按相对间距都能在段内
    找到吊点；相对偏差超 lug_tolerance_mm 视为对不上。返回对齐方案。
    """
    lugs = sorted(float(v) for v in wp["lift_points_mm"])
    offsets = [v - lugs[0] for v in lugs]
    best = None
    for anchor in run:
        aligns = []
        used = set()
        ok = True
        for lug, off in zip(lugs, offsets):
            want = anchor["x_mm"] + off
            match = None
            for p in run:
                if p["index"] in used:
                    continue
                if abs(p["x_mm"] - want) <= tol + 1e-9 \
                        and (match is None
                             or abs(p["x_mm"] - want)
                             < abs(match["x_mm"] - want)):
                    match = p
            if match is None:
                ok = False
                break
            used.add(match["index"])
            aligns.append({"lug_mm": lug, "point_index": match["index"],
                           "x_mm": match["x_mm"],
                           "error_mm": round(match["x_mm"] - want, 3)})
        if ok:
            # 多个可行锚点时取对齐误差最小、再按吊点编号最小（确定性）
            err = sum(abs(a["error_mm"]) for a in aligns)
            key = (err, anchor["index"])
            if best is None or key < best[0]:
                # 工件中心 = 首吊耳对齐点 − 首吊耳相对中心坐标
                center = anchor["x_mm"] - lugs[0]
                best = (key, center, aligns)
    if best is None:
        return None
    return {"center_x": best[1], "aligns": best[2]}


def _load_split(wp, run, center_x, bearing_indices, ori):
    """按重心力臂把重量静力学分配到承重吊点。

    - 单个承重吊点：承担全部重量（重心横向偏移仍计入横梁力矩）；
    - 两个承重吊点：杠杆法；
    - 多于两个：刚性梁按距重心距离反比分配（重心在跨内）。
    返回 {point_index: load_kg}。
    """
    weight = float(wp["weight_kg"])
    bearing = [p for p in run if p["index"] in bearing_indices]
    bearing.sort(key=lambda p: p["x_mm"])
    loads = {}
    if len(bearing) == 1:
        loads[bearing[0]["index"]] = weight
        return loads
    if len(bearing) == 2:
        a, b = bearing[0]["x_mm"], bearing[1]["x_mm"]
        cg = center_x + float(wp.get("cg_offset_x_mm") or 0.0)
        span = b - a
        if span <= 0:
            share = weight / len(bearing)
            for p in bearing:
                loads[p["index"]] = share
            return loads
        cg = min(max(cg, a), b)
        fa = weight * (b - cg) / span
        fb = weight - fa
        loads[bearing[0]["index"]] = fa
        loads[bearing[1]["index"]] = fb
        return loads
    # 多于两点：距重心距离反比（刚体多支点近似）
    cg = center_x + float(wp.get("cg_offset_x_mm") or 0.0)
    xs = [p["x_mm"] for p in bearing]
    if cg < min(xs) or cg > max(xs):
        return None  # 重心在支撑跨之外（CG_SUPPORT）
    inv = []
    for p in bearing:
        d = abs(p["x_mm"] - cg)
        inv.append(0.0 if d == 0 else 1.0 / d)
    s = sum(inv)
    if s == 0:
        share = weight / len(bearing)
        for p in bearing:
            loads[p["index"]] = share
    else:
        for p, wgt in zip(bearing, inv):
            loads[p["index"]] = weight * wgt / s
    return loads


def placement_view(plc, rack):
    """单个工件布置的对外视图。"""
    return {
        "workpiece_id": plc["wp"]["id"],
        "rod_id": plc["rod_id"],
        "rotation_deg": plc["rotation_deg"],
        "center_x_mm": round(plc["center_x"], 3),
        "cg_x_mm": round(plc["center_x"] + plc["wp"].get("cg_offset_x_mm", 0.0), 3),
        "occupied_points": [{"index": p["index"], "x_mm": p["x_mm"],
                              "load_kg": round(plc["loads"].get(p["index"], 0.0), 3),
                              "bearing": p["index"] in plc["loads"]}
                             for p in plc["run"]],
        "span_mm": [round(plc["lo"], 3), round(plc["hi"], 3)],
        "lift_mode": plc["lift_mode"],
    }


class _Layout:
    """组批中的炉内布置（可变）。"""

    def __init__(self, rack):
        self.rack = rack
        self.placements = []          # placement dict 列表（已通过全部检查）
        self._by_rod = {}             # rod_id -> [placement]
        self.point_load = {}          # (rod_id, index) -> kg
        self.zone_load = {}           # (rod_id, zone_id) -> kg
        self.total = 0.0

    def _moment(self):
        m = 0.0
        for (rid, idx), load in self.point_load.items():
            p = self.rack.point(rid, idx)
            if p:
                m += load * p["x_mm"]
        return m

    def balance(self):
        """横梁总载与左右力矩平衡视图（相对各挂杆吊点跨度中心）。"""
        rods_view = []
        moment_kg_mm = 0.0
        for rod in self.rack.rods:
            lo, hi = self.rack.rod_x_bounds(rod)
            center = (lo + hi) / 2.0
            left = right = 0.0
            for p in rod["points"]:
                load = self.point_load.get((rod["id"], p["index"]), 0.0)
                if p["x_mm"] < center:
                    left += load
                elif p["x_mm"] > center:
                    right += load
                moment_kg_mm += load * (p["x_mm"] - center)
            zloads = [{"zone_id": z["id"],
                       "load_kg": round(self.zone_load.get((rod["id"], z["id"]), 0.0), 3),
                       "limit_kg": z["max_load_kg"],
                       "ok": self.zone_load.get((rod["id"], z["id"]), 0.0)
                             <= z["max_load_kg"] + 1e-9}
                      for z in rod.get("zones", [])]
            rod_moment = sum(self.point_load.get((rod["id"], p["index"]), 0.0)
                             * (p["x_mm"] - center) for p in rod["points"])
            rods_view.append({
                "rod_id": rod["id"], "center_x_mm": round(center, 3),
                "left_load_kg": round(left, 3),
                "right_load_kg": round(right, 3),
                "moment_kg_mm": round(rod_moment, 3),
                "zones": zloads})
        abs_mom = abs(moment_kg_mm)
        total = self.total
        ratio = (abs_mom / total) if total > 0 else 0.0
        ok_abs = (self.rack.moment_tolerance_kg_mm is None
                  or abs_mom <= self.rack.moment_tolerance_kg_mm + 1e-9)
        ok_ratio = (self.rack.moment_tolerance_ratio is None
                    or ratio <= self.rack.moment_tolerance_ratio + 1e-9)
        return {
            "total_load_kg": round(total, 3),
            "beam_limit_kg": self.rack.beam_max_load_kg,
            "total_ok": (self.rack.beam_max_load_kg is None
                         or total <= self.rack.beam_max_load_kg + 1e-9),
            "moment_abs_kg_mm": round(abs_mom, 3),
            "moment_ratio": round(ratio, 6),
            "moment_tolerance_kg_mm": self.rack.moment_tolerance_kg_mm,
            "moment_tolerance_ratio": self.rack.moment_tolerance_ratio,
            "moment_ok": ok_abs and ok_ratio,
            "rods": rods_view,
        }

    def try_place(self, wp, blocked=None):
        """尝试把工件布置进当前炉内布置（自动枚举挂杆/旋转/吊点段）。

        blocked: {(rod_id, index)} 本炉次占用时段内禁用的吊点
                 （禁用时段 / 已被临时封掉的挂位）。
        成功返回 placement dict（尚未提交）；失败返回
        {"conflict": 首个冲突代码, "rod_id", "point_index", "detail"}。
        """
        blocked = blocked or set()
        clearance = float(wp.get("clearance_mm")
                          if wp.get("clearance_mm") is not None
                          else self.rack.default_clearance_mm)
        first_failure = None
        for rod in self.rack.rods:
            existing = self._by_rod.get(rod["id"], [])
            for ori in self.rack.orientations(wp):
                # 1. 炉膛尺寸
                axis_along = self.rack.chamber_l if rod["axis"] == "L" \
                    else self.rack.chamber_w
                axis_across = self.rack.chamber_w if rod["axis"] == "L" \
                    else self.rack.chamber_l
                if (ori["along_mm"] > axis_along
                        or ori["across_mm"] > axis_across
                        or ori["height_mm"] > self.rack.chamber_h):
                    if first_failure is None:
                        first_failure = (F_CHAMBER, rod, None,
                                         f"旋转 {ori['rotation_deg']}° 外廓"
                                         f" {ori['along_mm']:.0f}×"
                                         f"{ori['across_mm']:.0f}×"
                                         f"{ori['height_mm']:.0f} 超出炉膛")
                    continue
                runs = _candidate_runs(self.rack, rod, wp, ori)
                if not runs:
                    if first_failure is None:
                        first_failure = (F_ROD_SPAN, rod, None,
                                         f"沿杆投影 {ori['along_mm']:.0f}mm"
                                         " 超出挂杆吊点跨度")
                    continue
                for run in runs:
                    res = self._check_run(rod, ori, run, wp, existing, blocked,
                                          clearance)
                    if "wp" in res:
                        return res
                    if first_failure is None:
                        first_failure = (res["conflict"], rod,
                                         res.get("point_index"),
                                         res.get("detail", ""))
        code, rod, idx, detail = first_failure or (F_ROD_SPAN, None, None,
                                                    "无可行挂位")
        return {"conflict": code, "rod_id": rod["id"] if rod else None,
                "point_index": idx, "detail": detail}

    def try_place_manual(self, wp, rod_id, point_indices, blocked=None):
        """把工件布置到人工指定的挂杆 + 连续吊点段（人工调整复核用）。

        旋转方向仍从工件允许方向中选取使该段可行的第一个；重心/吊耳/净距/
        承重/分区/总载/力矩检查与自动布置完全相同。
        """
        blocked = blocked or set()
        rod = self.rack.rod(rod_id)
        if rod is None:
            return {"conflict": "UNKNOWN_ROD", "rod_id": rod_id,
                    "point_index": None, "detail": f"挂杆 {rod_id} 不存在"}
        run = []
        for i in sorted(point_indices):
            p = self.rack.point(rod_id, i)
            if p is None:
                return {"conflict": "UNKNOWN_POINT", "rod_id": rod_id,
                        "point_index": i, "detail": f"挂杆 {rod_id} 无吊点 {i}"}
            run.append(p)
        clearance = float(wp.get("clearance_mm")
                          if wp.get("clearance_mm") is not None
                          else self.rack.default_clearance_mm)
        existing = self._by_rod.get(rod_id, [])
        last = None
        for ori in self.rack.orientations(wp):
            res = self._check_run(rod, ori, run, wp, existing, blocked,
                                  clearance, manual=True)
            if "wp" in res:
                return res
            last = res
        return last or {"conflict": F_ROD_SPAN, "rod_id": rod_id,
                        "point_index": None, "detail": "无可行旋转方向"}

    def _check_run(self, rod, ori, run, wp, existing, blocked, clearance,
                   manual=False):
        """对单个（挂杆, 旋转, 吊点段）执行全部布置检查；通过返回 placement。"""
        def fail(code, idx=None, detail=""):
            return {"conflict": code, "rod_id": rod["id"],
                    "point_index": idx, "detail": detail}

        # 炉膛尺寸（人工布置同样检查）
        axis_along = self.rack.chamber_l if rod["axis"] == "L" \
            else self.rack.chamber_w
        axis_across = self.rack.chamber_w if rod["axis"] == "L" \
            else self.rack.chamber_l
        if (ori["along_mm"] > axis_along or ori["across_mm"] > axis_across
                or ori["height_mm"] > self.rack.chamber_h):
            return fail(F_CHAMBER, None,
                        f"旋转 {ori['rotation_deg']}° 外廓超出炉膛")
        # 2. 禁用吊点
        blk = [p for p in run
               if (rod["id"], p["index"]) in blocked or p.get("blocked")]
        if blk:
            return fail(F_POINT_BLOCKED, blk[0]["index"],
                        f"吊点 {blk[0]['index']} 处于禁用时段")
        lo, hi = piece_extents(None, run, rod)
        center_x = (lo + hi) / 2.0
        span_lo = rod["points"][0]["x_mm"] - rod["point_spacing_mm"] / 2.0
        span_hi = rod["points"][-1]["x_mm"] + rod["point_spacing_mm"] / 2.0
        lugs = wp.get("lift_points_mm")
        lift_mode = "LUG" if lugs else "UNIFORM"
        aligns = None
        # 自动枚举时段长度由 _candidate_runs 保证；人工段必须装得下工件沿杆投影
        provided = (run[-1]["x_mm"] - run[0]["x_mm"]) + rod["point_spacing_mm"]
        if provided + 1e-9 < ori["along_mm"]:
            return fail(F_ROD_SPAN, run[0]["index"],
                        f"指定 {len(run)} 个吊点跨度 {provided:.0f}mm"
                        f" 不足以容纳工件沿杆投影 {ori['along_mm']:.0f}mm")
        # 3. 共享吊点 / 净距（先按段外廓粗查；LUG 对齐后按真实外廓复查）
        if _collides(existing, lo, hi, ori["across_mm"] / 2.0, rod):
            return fail(F_POINT_TAKEN, run[0]["index"],
                        "与已布置工件净距不足/共享吊点")
        # 4. 吊耳对齐（决定工件中心的平移位置）
        piece_center = center_x
        if lift_mode == "LUG":
            lug_res = _lug_alignments(rod, run, wp, ori,
                                      self.rack.lug_tolerance_mm)
            if lug_res is None:
                return fail(F_LUG_MATCH, run[0]["index"],
                            "吊耳间距无法对齐到吊点")
            aligns = lug_res["aligns"]
            piece_center = lug_res["center_x"]
            llo = piece_center - ori["along_mm"] / 2.0
            lhi = piece_center + ori["along_mm"] / 2.0
            if llo < span_lo - 1e-9 or lhi > span_hi + 1e-9:
                return fail(F_ROD_SPAN, run[0]["index"],
                            "吊耳对齐后工件外廓超出挂杆跨度")
            if _collides(existing, llo, lhi, ori["across_mm"] / 2.0, rod):
                return fail(F_POINT_TAKEN, run[0]["index"],
                            "与已布置工件净距不足/共享吊点")
            lo, hi, center_x = llo, lhi, piece_center
        cg_x = center_x + float(wp.get("cg_offset_x_mm") or 0.0)
        bearing_indices = ({a["point_index"] for a in aligns}
                           if lift_mode == "LUG"
                           else {p["index"] for p in run})
        # 5. 重心在支撑跨内
        xs_bearing = [p["x_mm"] for p in run
                      if p["index"] in bearing_indices]
        if cg_x < min(xs_bearing) - 1e-9 or cg_x > max(xs_bearing) + 1e-9:
            return fail(F_CG_SUPPORT, run[0]["index"],
                        f"重心 x={cg_x:.0f} 落在支撑跨 "
                        f"{min(xs_bearing):.0f}–{max(xs_bearing):.0f} 之外")
        loads = _load_split(wp, run, center_x, bearing_indices, ori)
        if loads is None:
            return fail(F_CG_SUPPORT, run[0]["index"],
                        "重心落在承重吊点支撑跨之外")
        # 6. 单点承重
        for p in run:
            if p["index"] not in loads:
                continue
            cur = self.point_load.get((rod["id"], p["index"]), 0.0)
            if cur + loads[p["index"]] > p["max_load_kg"] + 1e-9:
                return fail(F_POINT_LOAD, p["index"],
                            f"吊点 {p['index']} 承重 "
                            f"{cur + loads[p['index']]:.1f}/"
                            f"{p['max_load_kg']:g} kg 超限")
        # 7. 横梁分区局部承重
        zone_inc = {}
        for idx, load in loads.items():
            p = next(q for q in run if q["index"] == idx)
            z = self.rack.zone_of(rod, p["x_mm"])
            if z:
                zone_inc[z["id"]] = zone_inc.get(z["id"], 0.0) + load
        for zid, inc in zone_inc.items():
            z = next(z for z in rod["zones"] if z["id"] == zid)
            cur = self.zone_load.get((rod["id"], zid), 0.0)
            if cur + inc > z["max_load_kg"] + 1e-9:
                return fail(F_ZONE_LOAD, None,
                            f"横梁分区 {z['id']} 承重 "
                            f"{cur + inc:.1f}/{z['max_load_kg']:g} kg 超限")
        # 8. 横梁总载
        new_total = self.total + float(wp["weight_kg"])
        if (self.rack.beam_max_load_kg is not None
                and new_total > self.rack.beam_max_load_kg + 1e-9):
            return fail(F_BEAM_TOTAL, None,
                        f"横梁总载 {new_total:.1f}/"
                        f"{self.rack.beam_max_load_kg:g} kg 超限")
        # 9. 左右力矩平衡
        trial = self._trial_moment(rod, run, loads, center_x, wp)
        bad_mom = (self.rack.moment_tolerance_kg_mm is not None
                   and abs(trial) > self.rack.moment_tolerance_kg_mm + 1e-9)
        if self.rack.moment_tolerance_ratio is not None and new_total > 0:
            bad_mom = bad_mom or (
                abs(trial) / new_total > self.rack.moment_tolerance_ratio + 1e-9)
        if bad_mom:
            return fail(F_MOMENT, None,
                        f"布置后偏载力矩 {abs(trial):.0f} kg·mm 超容差")
        return {"wp": wp, "rod_id": rod["id"], "rod": rod,
                "rotation_deg": ori["rotation_deg"],
                "run": run, "lo": lo, "hi": hi, "center_x": center_x,
                "loads": loads, "lift_mode": lift_mode,
                "lug_alignments": aligns,
                "across_half": ori["across_mm"] / 2.0, "clearance": clearance}


    def _trial_moment(self, rod, run, loads, center_x, wp):
        """临时加入工件后的相对跨中力矩（kg·mm，带符号）。"""
        lo, hi = self.rack.rod_x_bounds(rod)
        center = (lo + hi) / 2.0
        m = 0.0
        for (rid, idx), load in self.point_load.items():
            if rid != rod["id"]:
                continue
            p = self.rack.point(rid, idx)
            m += load * (p["x_mm"] - center)
        for idx, load in loads.items():
            p = next(q for q in run if q["index"] == idx)
            m += load * (p["x_mm"] - center)
        return m

    def commit(self, plc):
        """提交一个已通过全部检查的布置。"""
        rid = plc["rod_id"]
        self.placements.append(plc)
        self._by_rod.setdefault(rid, []).append(plc)
        for idx, load in plc["loads"].items():
            self.point_load[(rid, idx)] = \
                self.point_load.get((rid, idx), 0.0) + load
            p = self.rack.point(rid, idx)
            z = self.rack.zone_of(plc["rod"], p["x_mm"])
            if z:
                self.zone_load[(rid, z["id"])] = \
                    self.zone_load.get((rid, z["id"]), 0.0) + load
        self.total += float(plc["wp"]["weight_kg"])

    def load_in_sequence(self):
        """搬入顺序：由内向外（挂杆 y/z 靠炉内优先），同杆从左到右，再按工件号。

        大门板先入内端，避免遮挡后续挂位；顺序可复现。
        """
        ordered = sorted(
            self.placements,
            key=lambda q: (-q["rod"].get("y_mm", 0.0),
                           q["rod"]["id"], q["center_x"], q["wp"]["id"]))
        return [{"sequence": i + 1,
                 "workpiece_id": q["wp"]["id"], "rod_id": q["rod_id"],
                 "center_x_mm": round(q["center_x"], 3)}
                for i, q in enumerate(ordered)]

    def report(self):
        """完整布置报告（坐标 / 载荷 / 力矩 / 搬入顺序）。"""
        return {
            "oven_id": self.rack.oven_id,
            "placements": [placement_view(q, self.rack)
                           for q in sorted(self.placements,
                                           key=lambda q: (q["rod"]["id"],
                                                          q["center_x"],
                                                          q["wp"]["id"]))],
            "load_balance": self.balance(),
            "load_in_sequence": self.load_in_sequence(),
        }
