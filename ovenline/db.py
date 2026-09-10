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
    created_at            TEXT NOT NULL
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
    PRIMARY KEY (batch_id, workpiece_id)
);

CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    workpiece_id TEXT NOT NULL,
    ts           TEXT NOT NULL,
    metal_temp_c REAL NOT NULL       -- 工件金属探头温度
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
    # 兼容旧库：为 batch_items 补齐签发快照列
    existing = {r["name"] for r in db.execute("PRAGMA table_info(batch_items)")}
    snapshot_cols = {"snap_length_mm": "REAL", "snap_width_mm": "REAL",
                     "snap_height_mm": "REAL", "snap_weight_kg": "REAL",
                     "snap_powder_batch": "TEXT", "snap_temp_min_c": "REAL",
                     "snap_temp_max_c": "REAL", "snap_hold_minutes": "REAL"}
    for col, typ in snapshot_cols.items():
        if col not in existing:
            db.execute(f"ALTER TABLE batch_items ADD COLUMN {col} {typ}")
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
