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
    hold_minutes REAL NOT NULL    -- 窗口内需保持的分钟数
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
    -- DONE 判定合格 / REWORK_PENDING 待返工 / CLOSED 已结案
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
    hanger_slot  INTEGER NOT NULL,   -- 起始挂位（1 起）
    slots_used   INTEGER NOT NULL,   -- 占用挂位数
    -- 签发时快照：工件尺寸/重量与粉料固化窗口；签发后不再随主数据变化
    snap_length_mm    REAL,
    snap_width_mm     REAL,
    snap_height_mm    REAL,
    snap_weight_kg    REAL,
    snap_powder_batch TEXT,
    snap_temp_min_c   REAL,
    snap_temp_max_c   REAL,
    snap_hold_minutes REAL,
    -- 逐件出炉：NULL 表示仍在炉；最后一件离炉后炉次转 UNLOADED
    actual_unload_at      TEXT,      -- 该件实际离炉时刻
    unload_sequence       INTEGER,   -- 炉内离炉顺序（1 起，按实际离炉先后）
    first_met_at          TEXT,      -- 离炉时保留的首次达标时刻（永久保留）
    final_verdict         TEXT,      -- 最终判定：OK / NOT_OK
    forced                INTEGER NOT NULL DEFAULT 0,  -- 是否强制出炉
    force_reason          TEXT,      -- 强制出炉原因（强制时必填）
    progress_snapshot_json TEXT,     -- 离炉当时该件进度快照（project_item 结果）
    PRIMARY KEY (batch_id, workpiece_id)
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workpiece_id TEXT NOT NULL REFERENCES workpieces(id),
    probe_id     TEXT NOT NULL,            -- 探头编号（同一工件内唯一）
    offset_c     REAL NOT NULL DEFAULT 0,  -- 校准偏移：校正温度 = 原始值 + 偏移
    created_at   TEXT NOT NULL,
    UNIQUE (workpiece_id, probe_id)
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
                     "actual_unload_at": "TEXT", "unload_sequence": "INTEGER",
                     "first_met_at": "TEXT", "final_verdict": "TEXT",
                     "forced": "INTEGER NOT NULL DEFAULT 0",
                     "force_reason": "TEXT", "progress_snapshot_json": "TEXT"}
    for col, typ in snapshot_cols.items():
        if col not in existing:
            db.execute(f"ALTER TABLE batch_items ADD COLUMN {col} {typ}")
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
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
