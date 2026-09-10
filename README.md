# 粉末喷涂烘炉固化排产系统（ovenline）

本地 REST API：Python + Flask + SQLite，覆盖粉末固化生产的**排产 → 签发 → 入炉 →
测温回传 → 出炉判定 → 返工结案**全流程。

核心原则：**空气温度到达设定值不代表固化开始**——系统按每件工件金属探头温度处于
许可区间的**累计分钟数**判定固化是否充分；混炉须同时满足各粉料固化窗口交集、
同炉禁配组、工件间距（挂位）与吊具承重。

## 快速开始

```bash
pip install -r requirements.txt   # Flask>=3.0
python3 run.py                    # 监听 127.0.0.1:5000，自动建库 instance/ovenline.sqlite

# 两组本地请求样例（另开终端执行）
bash samples/scenario_a_normal.sh             # 正常闭环
bash samples/scenario_b_conflicts_rework.sh   # 冲突/越序/返工/版本链
```

重复运行样例前删除 `instance/ovenline.sqlite` 可获得干净数据。

## 目录结构

```
run.py                     入口
ovenline/
  __init__.py              应用工厂（PROBE_GAP_MINUTES 探头中断阈值，默认 10 分钟）
  db.py                    SQLite 连接与建表
  scheduler.py             排产引擎（纯函数）
  cure.py                  固化判定（纯函数）
  views.py                 REST 路由与状态机
samples/
  scenario_a_normal.sh             样例一：正常闭环
  scenario_b_conflicts_rework.sh   样例二：冲突/返工/版本
  out/                             样例下载产物（档案 JSON、随炉卡 HTML）
```

## 业务规则

### 排产（试算）
- 混炉时各粉料固化窗口取**交集**，交集为空不得同炉；保温时长取同炉最大值；
- **同炉禁配组**两两不可同炉；
- 工件占用挂位数 = max(按水平尺寸/吊点间距， 按重量/单吊点承重)，取相邻挂位；
- 升温分钟 = (窗口中值 − 环境温度)/升温速率 + 装载公斤 × 热惯性系数；
- 同炉次按工件交期先后串行衔接，已签发/在炉炉次占用炉膛的时间段自动避让；
- 无法安排的工件进入 `unscheduled` 并给出具体原因：
  `OVERSIZE`（尺寸超炉膛/挂位跨度）、`OVERWEIGHT`（超吊点承重）、`UNKNOWN_POWDER`。

### 固化判定（出炉判定）
按工件**自身粉料窗口**评估（窗口交集仅用于排产）：
- 相邻两个测温点都在窗口内，该区间时长才计入有效固化分钟（保守口径）；
- 累计分钟 < 保温要求 → `UNDER_TIME` 欠时；
- 任一测温点超过粉料上限 → `OVER_TEMP` 超温；
- 相邻测温点间隔超过阈值（默认 10 分钟）→ `PROBE_GAP` 探头中断；
- 未签发即请求出炉 → 拒绝并记录 `UNISSUED_UNLOAD` 未签发出炉；
- 炉内出现禁配组同炉 → `INCOMPAT_CONFLICT` 禁配冲突；
- 无任何标记 → 工件 `DONE`；否则 `REWORK_PENDING` 待返工。

### 状态机（越序一律 409）
```
炉次：DRAFT --签发--> ISSUED --入炉--> IN_OVEN --出炉判定--> UNLOADED --结案--> CLOSED
       └ 新试算后旧草稿 --> SUPERSEDED
工件：PENDING --> SCHEDULED --> IN_OVEN --> DONE ----------> CLOSED
                                   └--> REWORK_PENDING --返工--> PENDING（is_rework=1）
```

### 版本机制
每次试算生成一个 `schedule_versions` 记录（`parent_id` 指向上版本，参数快照存档）：
- **已签发/在炉炉次原样保留**（响应 `carried_batches`），其炉膛占用被新计划避让；
- 旧草稿作废（`SUPERSEDED`），待排产工件（含返工件）重新编排（`new_batches`）。

## API 一览（前缀 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/schedule/trial` | 试算：排出炉次/挂位/升温/保温/出炉时刻，形成关联版本 |
| POST | `/batches/<id>/issue` | 签发（DRAFT→ISSUED，此后冻结） |
| POST | `/batches/<id>/load` | 入炉（ISSUED→IN_OVEN），body 可带 `at` |
| POST | `/batches/<id>/readings` | 测温回传（仅 IN_OVEN），`readings:[{workpiece_id,ts,metal_temp_c}]` |
| POST | `/batches/<id>/unload` | 出炉判定（IN_OVEN→UNLOADED），逐件给 verdict 与标记 |
| POST | `/batches/<id>/close` | 炉次结案（存在未了结工件时 409 并列出） |
| POST | `/workpieces/<id>/rework` | 返工：回到待排产队列 |
| POST | `/workpieces/<id>/close` | 工件结案（合格入库/报废注明 note） |
| GET  | `/batches` `?state=` | 炉次列表 |
| GET  | `/batches/<id>` | 炉次详情（含每件固化累计与标记） |
| GET  | `/batches/<id>/archive` | 下载 JSON 炉次档案 |
| GET  | `/batches/<id>/card` | 可打印随炉卡（HTML） |
| GET  | `/workpieces/<id>` | 工件状态、履历、标记 |
| GET  | `/versions` | 排产版本链 |
| GET  | `/health` | 健康检查 |

### 试算请求体要点

```json
{
  "reason": "排产原因（记入版本）",
  "start_at": "2026-09-10T08:00:00",
  "ovens":   [{"id","chamber_l_mm","chamber_w_mm","chamber_h_mm",
               "heat_rate_c_per_min","mass_factor_min_per_kg","ambient_c",
               "turnaround_minutes","hanger_slots","hanger_spacing_mm","hanger_max_load_kg"}],
  "powders": [{"batch_no","temp_min_c","temp_max_c","hold_minutes"}],
  "forbidden_pairs": [["GRP_A","GRP_B"]],
  "orders":  [{"workpiece_id","order_id","length_mm","width_mm","height_mm",
               "weight_kg","powder_batch","compat_group","due_at"}]
}
```

时间为本地 ISO 格式（可带时区，将转为本地时间存储）。
