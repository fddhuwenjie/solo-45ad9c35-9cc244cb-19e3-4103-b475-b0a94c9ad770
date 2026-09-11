"""SQLite 数据访问层：连接管理与建表。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES schedule_versions(id),
    reason      TEXT,
    params_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ovens (
    id                    TEXT PRIMARY KEY,
    chamber_l_mm          REAL NOT NULL,
    chamber_w_mm          REAL NOT NULL,
    chamber_h_mm          REAL NOT NULL,
    heat_rate_c_per_min   REAL NOT NULL,   -- 炉膛空气升温速率
    mass_factor_min_per_kg REAL NOT NULL DEFAULT 0,  -- 装载热惯性附加分钟/公斤
    ambient_c             REAL NOT NULL DEFAULT 25,
    turnaround_minutes    REAL NOT NULL DEFAULT 15,  -- 炉次间周转（卸料/清场）
    hanger_slots          INTEGER NOT NULL,          -- 吊点（挂位）总数
    hanger_spacing_mm     REAL NOT NULL,             -- 吊点间距
    hanger_max_load_kg    REAL NOT NULL              -- 单吊点承重
);

CREATE TABLE IF NOT EXISTS powders (
    batch_no     TEXT PRIMARY KEY,
    temp_min_c   REAL NOT NULL,   -- 固化窗口下限（金属温度）
    temp_max_c   REAL NOT NULL,   -- 固化窗口上限
    hold_minutes REAL NOT NULL,   -- 窗口内需保持的分钟数
    -- 冷却放行门限（包装材料耐温）：离炉后表面温度须连续不高于
    -- pack_temp_limit_c 并保持 low_temp_hold_minutes 分钟；
    -- NULL = 粉料未登记该门限（冷却查询/放行以 MISSING 门限阻塞）
    pack_temp_limit_c    REAL,
    low_temp_hold_minutes REAL
);

CREATE TABLE IF NOT EXISTS workpieces (
    id           TEXT PRIMARY KEY,
    order_id     TEXT,
    length_mm    REAL NOT NULL,
    width_mm     REAL NOT NULL,
    height_mm    REAL NOT NULL,
    weight_kg    REAL NOT NULL,
    powder_batch TEXT NOT NULL,   -- 不建外键：未登记粉料的工件也要能入库并进入 unscheduled
    compat_group TEXT,            -- 禁配组标签；同炉禁配组两两不可同炉
    due_at       TEXT,            -- 交期 ISO 时间
    is_rework    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING 待排产 / SCHEDULED 已排入炉次 / IN_OVEN 在炉
    -- COOLING 已离炉合格件，冷却放行观察中（暂不计为完成）
    -- DONE 冷却放行完成 / REWORK_PENDING 待返工 / CLOSED 已结案
    -- 吊具布置：重心相对工件几何中心的沿杆/横向偏移、可旋转方向、
    -- 吊耳沿长轴坐标（相对工件中心）、与相邻工件的要求净距
    cg_offset_x_mm REAL NOT NULL DEFAULT 0,
    cg_offset_y_mm REAL NOT NULL DEFAULT 0,
    allowed_rotations TEXT,           -- JSON [0,90]，NULL 按默认可旋转方向
    lift_points_json  TEXT,           -- JSON [-1000,1000]，NULL 为均布承重
    clearance_mm      REAL NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id            INTEGER NOT NULL REFERENCES schedule_versions(id),
    oven_id               TEXT NOT NULL REFERENCES ovens(id),
    state                 TEXT NOT NULL DEFAULT 'DRAFT',
    -- DRAFT 草稿 / ISSUED 已签发 / IN_OVEN 在炉 / UNLOADED 已出炉
    -- CLOSED 已结案 / SUPERSEDED 被新版本取代
    window_min_c          REAL,
    window_max_c          REAL,
    hold_minutes          REAL,
    total_weight_kg       REAL,
    heatup_minutes        REAL,
    planned_load_at       TEXT,
    planned_cure_start_at TEXT,
    planned_unload_at     TEXT,
    actual_load_at        TEXT,
    actual_unload_at      TEXT,
    -- 停机避让：因避让停机窗增加的等待分钟；schedule_basis_json 为完整计算依据
    blackout_wait_minutes REAL NOT NULL DEFAULT 0,
    schedule_basis_json   TEXT,
    arrangement_json      TEXT,   -- 吊具布置完整报告（坐标/载荷/力矩/搬入顺序），签发时冻结
    created_at            TEXT NOT NULL
);

-- 停机窗（清炉/校准/检修）：随试算写入排产版本快照，后续试算可整体改写
CREATE TABLE IF NOT EXISTS blackout_windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES schedule_versions(id),
    oven_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- CLEANING 清炉 / CALIBRATION 校准 / MAINTENANCE 检修
    start_at   TEXT NOT NULL,
    end_at     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    workpiece_id TEXT NOT NULL REFERENCES workpieces(id),
    hanger_slot  INTEGER NOT NULL,   -- 起始挂位（1 起，挂杆局部编号）
    slots_used   INTEGER NOT NULL,   -- 占用挂位数
    rod_id       TEXT,               -- 所在挂杆
    load_in_sequence INTEGER,        -- 搬入顺序（1 起）
    placement_json TEXT,             -- 吊点坐标/旋转/各点载荷/重心（签发时冻结）
    -- 签发时快照：工件尺寸/重量与粉料固化窗口；签发后不再随主数据变化
    snap_length_mm    REAL,
    snap_width_mm     REAL,
    snap_height_mm    REAL,
    snap_weight_kg    REAL,
    snap_powder_batch TEXT,
    snap_temp_min_c   REAL,
    snap_temp_max_c   REAL,
    snap_hold_minutes REAL,
    -- 签发时冻结的包装冷却门限（来自粉料主数据，签发后不再随主数据变化）
    snap_pack_temp_limit_c     REAL,  -- 包装耐温上限（表面温度须 <= 该值）
    snap_low_temp_hold_minutes REAL,  -- 连续低温保持要求分钟数
    -- 逐件出炉：NULL 表示仍在炉；最后一件离炉后炉次转 UNLOADED
    actual_unload_at      TEXT,      -- 该件实际离炉时刻
    unload_sequence       INTEGER,   -- 炉内离炉顺序（1 起，按实际离炉先后）
    first_met_at          TEXT,      -- 离炉时保留的首次达标时刻（永久保留）
    final_verdict         TEXT,      -- 最终判定：OK / NOT_OK
    forced                INTEGER NOT NULL DEFAULT 0,  -- 是否强制出炉
    force_reason          TEXT,      -- 强制出炉原因（强制时必填）
    progress_snapshot_json TEXT,     -- 离炉当时该件进度快照（project_item 结果）
    -- 冷却搬运放行：合格件离炉即 COOLING，正常放行后才 DONE；紧急搬运转返工
    release_kind   TEXT,             -- NORMAL 正常放行 / EMERGENCY 紧急搬运
    release_at     TEXT,             -- 放行时刻
    release_reason TEXT,             -- 紧急搬运理由（紧急时必填）
    release_snapshot_json TEXT,      -- 放行当时冷却进度快照（cooling.evaluate 结果）
    PRIMARY KEY (batch_id, workpiece_id)
);

-- 冷却测温（表面温度，带时标）：按收录顺序保留，乱序不按时间重排。
-- 同点（同时刻）同温度为幂等重复（应用层去重，不计入也不截断区间）；
-- 同时刻不同温度（TS_CONFLICT，"同点重复"）照常收录并截断当前连续低温区间。
-- 唯一索引按 (炉次, 工件, 时刻, 温度) 保证同点同温度不重复落库。
CREATE TABLE IF NOT EXISTS cooling_readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    workpiece_id TEXT NOT NULL,
    ts           TEXT NOT NULL,          -- 测温时刻（ISO，按到达顺序收录）
    surface_temp_c REAL NOT NULL,        -- 工件表面温度
    kind         TEXT NOT NULL DEFAULT 'OK',  -- OK / OUT_OF_ORDER / TS_CONFLICT
    created_at   TEXT NOT NULL           -- 收录（到达）时刻：乱序判定依据
);

-- 冷却区间人工中断记录：区间只能由数据本身（乱序/缺报/再次升温）截断，
-- 同时刻不同温度的冲突读数记一条人工可追溯的区间中断
CREATE TABLE IF NOT EXISTS cooling_interruptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    workpiece_id TEXT NOT NULL,
    code         TEXT NOT NULL,          -- TS_CONFLICT
    at_ts        TEXT NOT NULL,          -- 冲突读数时标
    detail       TEXT,
    created_at   TEXT NOT NULL
);

-- 冷却搬运放行审计：正常放行（合格件 -> DONE）/ 紧急搬运（-> 返工处置）
CREATE TABLE IF NOT EXISTS release_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    workpiece_id TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- NORMAL / EMERGENCY
    release_at   TEXT NOT NULL,
    reason       TEXT,                   -- 紧急搬运理由（紧急时必填）
    held_minutes REAL,                   -- 放行当时当前连续区间有效保持分钟
    snapshot_json TEXT NOT NULL,         -- 放行当时冷却进度快照
    created_at   TEXT NOT NULL,
    UNIQUE (batch_id, workpiece_id)      -- 每件每炉次只能放行一次
);

CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    workpiece_id TEXT NOT NULL,
    probe_id     TEXT,                     -- 探头编号；NULL 表示未登记探头工件的隐式通道
    ts           TEXT NOT NULL,
    metal_temp_c REAL NOT NULL             -- 探头原始值（校正 = 原始值 + 校准偏移）
);

-- 工件探头登记（主数据）：校准偏移；签发时快照进 batch_item_probes 冻结
CREATE TABLE IF NOT EXISTS probes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workpiece_id   TEXT NOT NULL REFERENCES workpieces(id),
    probe_id       TEXT NOT NULL,            -- 探头编号（同一工件内唯一）
    offset_c       REAL NOT NULL DEFAULT 0,  -- 校准偏移：校正温度 = 原始值 + 偏移
    calibration_id INTEGER,                  -- 绑定的校准证书版本；NULL 表示固定偏移模式
    created_at     TEXT NOT NULL,
    UNIQUE (workpiece_id, probe_id)
);

-- 探头校准证书版本（不可覆盖）：多点示值—参考值点列；新证书追加新版本，
-- 已录入版本不可修改/删除，已签发炉次仍用签发时冻结的快照
CREATE TABLE IF NOT EXISTS probe_calibrations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workpiece_id   TEXT NOT NULL,
    probe_id       TEXT NOT NULL,
    version        INTEGER NOT NULL,         -- 该探头的证书版本序号（1 起，只增不改）
    certificate_no TEXT NOT NULL,            -- 证书号
    calibrated_at  TEXT NOT NULL,            -- 校准时刻
    valid_until    TEXT NOT NULL,            -- 到期时刻
    points_json    TEXT NOT NULL,            -- 示值—参考值点列（示值严格递增，≥2 点）
    created_at     TEXT NOT NULL,
    UNIQUE (workpiece_id, probe_id, version)
);

-- 签发时冻结的探头配置快照；出炉前可停用故障探头（DISABLED）
CREATE TABLE IF NOT EXISTS batch_item_probes (
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    workpiece_id    TEXT NOT NULL,
    probe_id        TEXT NOT NULL,
    offset_c        REAL NOT NULL,         -- 签发时冻结的校准偏移
    status          TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE / DISABLED
    disabled_reason TEXT,
    disabled_at     TEXT,
    -- 签发时冻结的校准证书版本快照（未绑定校准版本的探头为 NULL）
    calibration_id  INTEGER,
    version         INTEGER,               -- 证书版本序号
    certificate_no  TEXT,
    calibrated_at   TEXT,
    valid_until     TEXT,
    points_json     TEXT,                  -- 冻结的示值—参考值点列（插值依据）
    PRIMARY KEY (batch_id, workpiece_id, probe_id)
);

-- 探头处置审计：停用故障探头后只重算该工件，记录重算前后判定摘要
CREATE TABLE IF NOT EXISTS probe_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    workpiece_id TEXT NOT NULL,
    probe_id     TEXT NOT NULL,
    action       TEXT NOT NULL,            -- DISABLE 停用故障探头
    reason       TEXT,
    before_json  TEXT,                     -- 重算前判定摘要
    after_json   TEXT,                     -- 重算后判定摘要
    created_at   TEXT NOT NULL
);

-- 逐件出炉审计：每次单件/整炉出炉一件记录一条，含强制原因与当时进度快照
CREATE TABLE IF NOT EXISTS unload_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    workpiece_id TEXT NOT NULL,
    sequence     INTEGER NOT NULL,        -- 炉内离炉顺序（1 起）
    verdict      TEXT NOT NULL,           -- OK / NOT_OK
    forced       INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,                    -- 强制出炉原因（强制时必填）
    flags_json   TEXT NOT NULL,           -- 离炉判定标记代码列表
    snapshot_json TEXT NOT NULL,          -- 离炉当时该件进度快照（project_item 结果）
    unload_at    TEXT NOT NULL,           -- 判定基准/实际离炉时刻（请求 at）
    created_at   TEXT NOT NULL,
    UNIQUE (batch_id, workpiece_id)       -- 每件在同炉次只能离炉一次
);

CREATE TABLE IF NOT EXISTS flags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL,
    workpiece_id TEXT NOT NULL,
    code         TEXT NOT NULL,
    detail       TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (batch_id, workpiece_id, code)
);

-- 吊点故障登记（持续到修复）；签发后冻结的炉次不得改动，只重排未签发炉次
CREATE TABLE IF NOT EXISTS hanger_faults (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    oven_id     TEXT NOT NULL,
    rod_id      TEXT NOT NULL,
    point_index INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolve_note TEXT,
    created_at  TEXT NOT NULL
);

-- 吊点禁用时段（清炉/检修时临时封掉的挂位 + 吊点故障登记）：
-- version_id 非空 = 随试算版本快照的试算级禁用；fault_id 非空 = 持续生效的
-- 吊点故障（跨版本，直到修复）。resolved_at 非空表示故障已修复。
CREATE TABLE IF NOT EXISTS hanger_blackouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER REFERENCES schedule_versions(id),
    fault_id    INTEGER REFERENCES hanger_faults(id),
    oven_id     TEXT NOT NULL,
    rod_id      TEXT NOT NULL,
    point_index INTEGER NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT,                  -- NULL = 开放结束（故障持续中）
    kind        TEXT NOT NULL,         -- BLACKOUT 临时封位 / FAULT 吊点故障
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- 试算时放不下的工件：首个冲突约束 / 逐炉拒绝明细 / 可选炉（随版本存档）
CREATE TABLE IF NOT EXISTS schedule_rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL REFERENCES schedule_versions(id),
    workpiece_id TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT,
    first_conflict_json TEXT,
    per_oven_json       TEXT,
    alternative_ovens_json TEXT,
    created_at  TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # 兼容旧库：为 batches 补齐停机避让列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batches)")}
    for col, typ in {"blackout_wait_minutes": "REAL NOT NULL DEFAULT 0",
                     "schedule_basis_json": "TEXT"}.items():
        if col not in existing:
            db.execute(f"ALTER TABLE batches ADD COLUMN {col} {typ}")
    # 兼容旧库：为 batch_items 补齐签发快照列与逐件出炉列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batch_items)")}
    snapshot_cols = {"snap_length_mm": "REAL", "snap_width_mm": "REAL",
                     "snap_height_mm": "REAL", "snap_weight_kg": "REAL",
                     "snap_powder_batch": "TEXT", "snap_temp_min_c": "REAL",
                     "snap_temp_max_c": "REAL", "snap_hold_minutes": "REAL",
                     "snap_pack_temp_limit_c": "REAL",
                     "snap_low_temp_hold_minutes": "REAL",
                     "actual_unload_at": "TEXT", "unload_sequence": "INTEGER",
                     "first_met_at": "TEXT", "final_verdict": "TEXT",
                     "forced": "INTEGER NOT NULL DEFAULT 0",
                     "force_reason": "TEXT", "progress_snapshot_json": "TEXT",
                     "release_kind": "TEXT", "release_at": "TEXT",
                     "release_reason": "TEXT", "release_snapshot_json": "TEXT",
                     "rod_id": "TEXT", "load_in_sequence": "INTEGER",
                     "placement_json": "TEXT"}
    for col, typ in snapshot_cols.items():
        if col not in existing:
            db.execute(f"ALTER TABLE batch_items ADD COLUMN {col} {typ}")
    # 兼容旧库：powders 补包装冷却门限列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(powders)")}
    for col, typ in {"pack_temp_limit_c": "REAL",
                     "low_temp_hold_minutes": "REAL"}.items():
        if col not in existing:
            db.execute(f"ALTER TABLE powders ADD COLUMN {col} {typ}")
    # 兼容旧库：已整炉出炉的历史炉次按实际出炉时刻回填逐件离炉列
    db.execute(
        "UPDATE batch_items SET actual_unload_at=("
        "  SELECT b.actual_unload_at FROM batches b WHERE b.id=batch_items.batch_id),"
        " final_verdict=CASE WHEN EXISTS("
        "  SELECT 1 FROM flags f WHERE f.batch_id=batch_items.batch_id"
        "   AND f.workpiece_id=batch_items.workpiece_id) THEN 'NOT_OK' ELSE 'OK' END"
        " WHERE actual_unload_at IS NULL AND EXISTS("
        "  SELECT 1 FROM batches b WHERE b.id=batch_items.batch_id"
        "   AND b.state IN ('UNLOADED','CLOSED') AND b.actual_unload_at IS NOT NULL)")
    db.execute(
        "UPDATE batch_items SET unload_sequence=("
        "  SELECT COUNT(*) FROM batch_items b2 WHERE b2.batch_id=batch_items.batch_id"
        "   AND b2.actual_unload_at IS NOT NULL"
        "   AND (b2.actual_unload_at < batch_items.actual_unload_at"
        "        OR (b2.actual_unload_at = batch_items.actual_unload_at"
        "            AND b2.workpiece_id <= batch_items.workpiece_id)))"
        " WHERE actual_unload_at IS NOT NULL AND unload_sequence IS NULL")
    # 兼容旧库：readings 补探头列，并按 (炉次, 工件, 探头, 时刻) 建幂等去重索引
    existing = {r["name"] for r in db.execute("PRAGMA table_info(readings)")}
    if "probe_id" not in existing:
        db.execute("ALTER TABLE readings ADD COLUMN probe_id TEXT")
    # 建唯一索引前清理历史重复读数（保留最早一条）
    db.execute(
        "DELETE FROM readings WHERE id NOT IN"
        " (SELECT MIN(id) FROM readings"
        "  GROUP BY batch_id, workpiece_id, COALESCE(probe_id, ''), ts)")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_dedup ON readings"
        " (batch_id, workpiece_id, COALESCE(probe_id, ''), ts)")
    # 兼容旧库：probes 补校准证书版本绑定列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(probes)")}
    if "calibration_id" not in existing:
        db.execute("ALTER TABLE probes ADD COLUMN calibration_id INTEGER")
    # 兼容旧库：batch_item_probes 补签发冻结的校准证书快照列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batch_item_probes)")}
    for col, typ in {"calibration_id": "INTEGER", "version": "INTEGER",
                     "certificate_no": "TEXT", "calibrated_at": "TEXT",
                     "valid_until": "TEXT", "points_json": "TEXT"}.items():
        if col not in existing:
            db.execute(f"ALTER TABLE batch_item_probes ADD COLUMN {col} {typ}")
    # 兼容旧库：workpieces 补重心 / 可旋转方向 / 吊耳 / 净距列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(workpieces)")}
    wp_new_cols = {"cg_offset_x_mm": "REAL NOT NULL DEFAULT 0",
                   "cg_offset_y_mm": "REAL NOT NULL DEFAULT 0",
                   "allowed_rotations": "TEXT",    # JSON [deg,...]
                   "lift_points_json": "TEXT",     # JSON [mm,...] 吊耳坐标
                   "clearance_mm": "REAL NOT NULL DEFAULT 0"}
    for col, typ in wp_new_cols.items():
        if col not in existing:
            db.execute(f"ALTER TABLE workpieces ADD COLUMN {col} {typ}")
    # 兼容旧库：batches 补吊具布置完整报告（签发时冻结）
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batches)")}
    if "arrangement_json" not in existing:
        db.execute("ALTER TABLE batches ADD COLUMN arrangement_json TEXT")
    # 兼容旧库：冷却放行功能上线前已离炉合格的工件视为已正常放行，
    # 补回填 release_actions 与 batch_items 放行列，保证「结案须引用一次
    # 有效放行」与历史档案完整；强制出炉（NOT_OK）件不补，仍走返工处置
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batch_items)")}
    if {"release_kind", "release_at"} <= existing:
        db.execute(
            "UPDATE batch_items SET release_kind='NORMAL',"
            " release_at=actual_unload_at,"
            " release_reason='历史合格件迁移补回填'"
            " WHERE final_verdict='OK' AND actual_unload_at IS NOT NULL"
            " AND release_kind IS NULL")
        db.execute(
            "INSERT OR IGNORE INTO release_actions"
            " (batch_id, workpiece_id, kind, release_at, reason, held_minutes,"
            " snapshot_json, created_at)"
            " SELECT batch_id, workpiece_id, 'NORMAL', actual_unload_at,"
            " '历史合格件迁移补回填', NULL, '{}', actual_unload_at"
            " FROM batch_items WHERE final_verdict='OK'"
            " AND actual_unload_at IS NOT NULL")
    # 冷却测温幂等索引：同 (炉次, 工件, 时刻, 温度) 重复回传不重复入库；
    # 同时刻不同温度（TS_CONFLICT）是不同行，照常收录并截断区间
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cooling_dedup ON cooling_readings"
        " (batch_id, workpiece_id, ts, surface_temp_c)")
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
