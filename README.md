# 粉末喷涂烘炉固化排产系统（ovenline）

本地 REST API：Python + Flask + SQLite，覆盖粉末固化生产的**排产 → 签发 → 入炉 →
测温回传 → 出炉判定 → 返工结案**全流程。

核心原则：**空气温度到达设定值不代表固化开始**——系统按每件工件金属探头温度处于
许可区间的**累计分钟数**判定固化是否充分；混炉须同时满足各粉料固化窗口交集、
同炉禁配组、工件间距（挂位）与吊具承重。

多探头：同一工件可登记**多个金属探头**同步测温（各带校准偏移），签发时冻结配置；
判定序列取**每个采样时刻有效探头的最低校正温度**，单个探头松脱、卡值或漂移
不会直接决定整件返工——可在出炉前停用故障探头，系统只重算该工件并记录结果变化。

## 快速开始

```bash
pip install -r requirements.txt   # Flask>=3.0
python3 run.py                    # 监听 127.0.0.1:5000，自动建库 instance/ovenline.sqlite

# 三组本地请求样例（另开终端执行）
bash samples/scenario_a_normal.sh             # 正常闭环
bash samples/scenario_b_conflicts_rework.sh   # 冲突/越序/返工/版本链
bash samples/scenario_c_probes.sh             # 多探头测温/停用故障探头
bash samples/scenario_d_piece_unload.sh       # 轻薄/厚重混炉逐件出炉、强制出炉

# 回归测试（不依赖服务进程）
python3 -m unittest discover -s tests -v
```

重复运行样例前删除 `instance/ovenline.sqlite` 可获得干净数据。

## 目录结构

```
run.py                     入口
ovenline/
  __init__.py              应用工厂（探头阈值配置见下）
  db.py                    SQLite 连接与建表
  scheduler.py             排产引擎（纯函数）
  cure.py                  固化判定（纯函数）
  probes.py                多探头判定：校正/判定序列/异常检测（纯函数）
  progress.py              在炉进度与安全出炉预测（纯函数，基于 probes.analyze）
  views.py                 REST 路由与状态机
samples/
  scenario_a_normal.sh             样例一：正常闭环
  scenario_b_conflicts_rework.sh   样例二：冲突/返工/版本
  scenario_c_probes.sh             样例三：多探头测温
  scenario_d_piece_unload.sh       样例四：逐件出炉/强制出炉
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
  `OVERSIZE`（尺寸超炉膛/挂位跨度）、`OVERWEIGHT`（超吊点承重）、
  `UNKNOWN_POWDER`（粉料批号未登记；该订单不入库，登记粉料后重新提交即可排产。
  复用已有 workpiece_id 提交未登记粉料时，该工件本次不参与编排，
  不会按库内残留的旧粉料进入新炉次）。

### 多探头测温
- 每件工件可登记一个或多个探头及**校准偏移**（`POST /workpieces/<id>/probes`），
  校正温度 = 探头原始值 + 校准偏移；**签发时冻结探头配置**（编号 + 偏移），
  此后修改主数据不影响已签发炉次；
- 测温回传携带 `probe_id`，按 **(炉次, 工件, 探头, 时刻)** 幂等去重
  （重复回传计入 `duplicates`，不重复入库）；**未绑定、已停用或早于实际入炉时刻**
  的读数一律拒收；**已逐件离炉的工件也按件拒收**（炉次仍 IN_OVEN 时其他在炉
  工件照常接收）；已绑定探头的工件必须携带 `probe_id`，
  未登记探头的工件按隐式单通道接收（偏移为 0）；
- **判定序列** = 每个采样时刻各有效（未停用）探头校正温度的**最低值**，
  固化窗口分钟数按该序列累计；各探头原始值完整保留于 readings 表；
- 异常检测（阈值见应用配置）：
  - 同一探头连续相同读数达到 `STUCK_PROBE_MIN_CONSECUTIVE`（默认 5）个点
    → `STUCK_PROBE` 卡值；
  - 同一时刻有效探头校正值极差超过 `PROBE_DIVERGENCE_C`（默认 5℃）
    → `PROBE_DIVERGENCE` 探头温差；
  - **按每个启用探头自身的时间序列**识别缺报：首读数前的起始缺报、
    相邻读数间断、末读数后的尾随缺报（以全体启用探头的最早/最晚读数为
    测量窗口），超过 `PROBE_GAP_MINUTES`（默认 10 分钟）→ `PROBE_GAP`；
    全程无读数的启用探头按整个测量窗口记一段缺报；
- **出炉前可停用故障探头**（`POST /batches/<bid>/workpieces/<wid>/probes/<pid>/disable`，
  必须填写原因）：停用后该探头读数不再参与判定且拒收新读数，
  系统**只重算该工件**并把重算前后的判定摘要记入 `probe_actions` 审计；
  **工件已逐件离炉后不能再停用探头**（409）；
  已停用探头的历史异常区间仍保留备查；
- 有效探头 = 未停用、有读数、且**末读数距测量窗口末端未超过缺报阈值**
  （超过缺报阈值的探头不再计为有效）；有效探头少于 `MIN_VALID_PROBES`
  （默认 1）时 → `INSUFFICIENT_PROBES`，**不得判定合格**；
- 炉次详情、JSON 档案与随炉卡均列出：探头状态/校准偏移/异常区间（含缺报
  区间的起止时间与时长）/探头处置审计，以及最终采用的判定序列。

### 固化判定（出炉判定）
已签发炉次按**签发时快照**的工件尺寸与粉料窗口评估（草稿炉次按当前主数据）：
- 相邻两个判定序列点都在窗口内，该区间时长才计入有效固化分钟（保守口径）；
- **相邻判定点间隔超过缺报阈值（`PROBE_GAP`）即切断连续保温段**：该区间
  不计入，且缺报前已累计分钟作废，读数恢复后只从重新得到验证的连续段
  （最后一次缺报之后的在窗区间）重新累计——两条相隔 30 分钟的合格读数
  不会被插值成连续保温，缺报告警仍保留；
- 测温时刻早于实际入炉时刻的读数**拒收**，不落库、不计入有效保温；
- 累计分钟 < 保温要求 → `UNDER_TIME` 欠时；
- 判定序列任一点超过粉料上限 → `OVER_TEMP` 超温；
- 探头异常：`STUCK_PROBE` / `PROBE_DIVERGENCE` / `PROBE_GAP`（见上节）；
- 有效探头数不足 → `INSUFFICIENT_PROBES`；
- 未签发即请求出炉 → 拒绝并记录 `UNISSUED_UNLOAD` 未签发出炉；
- 炉内出现禁配组同炉 → `INCOMPAT_CONFLICT` 禁配冲突；
- 无任何标记 → 工件 `DONE`；否则 `REWORK_PENDING` 待返工。

### 在炉固化进度与安全出炉预测（只读，不改写签发计划）
炉次运行中可随时查询进度：`GET /api/batches/<id>/progress?as_of=<ISO>`，
`as_of` 为计算基准时刻（缺省：在炉取当前时刻、已出炉取实际出炉时刻）。
炉次详情、JSON 档案、随炉卡均携带**同一进度快照**并注明基准时刻与来源。
逐件返回：
- `latest_reading_at` 最新有效测温时刻、`latest_temp_c` 当前最低校正温度
  （各有效探头校正温度的最低值）、`reading_freshness_minutes` 读数新鲜度
  （距基准时刻分钟数，超过缺报阈值 → `stale`）；
- `in_window_minutes` 已累计保温分钟、`remaining_hold_minutes` 剩余分钟
  （内部缺报会切断连续保温段并清零重算，见固化判定节）；
- `first_met_at` 首次达标时刻、`safe_unload_at` 最早安全出炉时刻；
- `status`：`MET` 已达标 / `TRACKING` 保温中可预测 / `BLOCKED` 不可预测，
  `blockers` 阻塞原因、`alerts` 告警（卡值/温差/缺报/历史超温等，达标后保留）。

预测口径：
- **仅当最新最低校正温度位于许可区间、有效探头数量达标且读数未超时**，
  才按连续保温外推：安全出炉 = 最新测温时刻 + 剩余保温分钟；
- 其他情形标记 `BLOCKED`，阻塞原因可为 `UNDER_TEMP`（最新欠温）、
  `OVER_TEMP`（最新超温）、`STALE_READING`（超时缺报）、
  `INSUFFICIENT_PROBES`（有效探头不足）、`NO_READING`（基准时刻前无读数），
  可同时多个；历史上曾超温但最新已恢复时只给 `OVER_TEMP_HISTORY` 告警；
- **首次达标**只在「探头充足、读数新鲜」的采样时刻确认；一旦确认永久保留，
  其后的欠温/超温/缺报等异常读数**不回退达标状态**，只保留告警；
- 炉次级 `safe_unload_at` 取**所有工件最晚**的可出炉时刻（有不可预测
  工件时不给整体时刻）；与签发的计划出炉时刻比较：`plan_status` 为
  `OK` / `TOO_EARLY`（`planned_unload_early_minutes` 给出早了多少分钟，
  说明计划出炉时刻已失效）/ `CANNOT_VERIFY`（有不可预测工件）。
- 每次**接受新读数**或**停用探头**后即时重算（读数/停用响应中直接带
  `progress`）；预测为只读计算，**不改写已签发计划**，也不落任何标记。

### 状态机（越序一律 409）
```
炉次：DRAFT --签发--> ISSUED --入炉--> IN_OVEN --全部工件离炉--> UNLOADED --结案--> CLOSED
       └ 新试算后旧草稿 --> SUPERSEDED
工件：PENDING --> SCHEDULED --> IN_OVEN --> DONE ----------> CLOSED
                                   └--> REWORK_PENDING --返工--> PENDING（is_rework=1）
```

### 逐件出炉（轻薄件/厚重件不同时达标）
同炉工件固化完成时刻不同，支持逐件离炉：
- `POST /batches/<id>/workpieces/<wid>/unload`，body 可带 `at`（缺省当前时刻），
  按该件**截至 `at` 的进度与全部固化标记**判定安全条件：已达标（`MET`）且无
  欠时/超温/缺报/卡值/温差/探头不足/禁配冲突等任何不合格标记；
- **普通请求未达安全条件 → 409 拒绝**，响应含该件 `in_window_minutes` 累计、
  `remaining_hold_minutes` 剩余时间、`blockers` 阻塞原因与当时 `progress`，
  不留任何离炉记录（工件继续受热，后续可再次请求）；
- **强制出炉**须 `"force": true` 且填写 `reason`：最终判定一律 `NOT_OK`、
  工件转 `REWORK_PENDING`，保留全部不合格标记，并写 `unload_actions` 审计
  （顺序、判定、强制原因、离炉时刻与当时进度快照）；只给 `force` 不给原因 → 400；
- 工件**离炉后**：不再接收读数（按件拒收并注明离炉时刻）、不能停用探头、
  不能重复出炉（409）；
- 还有工件在炉时炉次保持 `IN_OVEN`，炉次进度汇总**只计算在炉工件**；
  已离炉工件保留**首次达标时刻、实际离炉时刻、最终判定与离炉当时进度快照**；
- **最后一件离炉后**炉次自动转 `UNLOADED`，`actual_unload_at` 取最后离炉时刻；
  逐件离炉顺序从 1 编号（先离炉序号小）；
- **整炉出炉**接口复用同一单件判定：对仍在炉的工件逐件落判定/标记/审计，
  已离炉工件出现在响应 `skipped` 中，其判定不被重写；
- 炉次详情、JSON 档案、随炉卡均给出 `unload_order`（离炉顺序、强制原因、
  当时进度快照）；整炉离炉后进度汇总改取逐件离炉时冻结的快照，不再随时间漂移。


### 版本机制
每次试算生成一个 `schedule_versions` 记录（`parent_id` 指向上版本，参数快照存档）：
- **已签发/在炉炉次原样保留**（响应 `carried_batches`），其炉膛占用被新计划避让；
- 签发时对炉内工件的尺寸、重量、粉料固化窗口与**探头配置**做快照
  （`batch_items.snap_*` / `batch_item_probes`），
  后续修改主数据或生成新版本，均不改变已签发炉次的查询结果与出炉判定；
- 旧草稿作废（`SUPERSEDED`），待排产工件（含返工件）重新编排（`new_batches`）。

## API 一览（前缀 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/schedule/trial` | 试算：排出炉次/挂位/升温/保温/出炉时刻，形成关联版本 |
| POST | `/batches/<id>/issue` | 签发（DRAFT→ISSUED，冻结工件/粉料/探头快照） |
| POST | `/batches/<id>/load` | 入炉（ISSUED→IN_OVEN），body 可带 `at` |
| POST | `/batches/<id>/readings` | 测温回传（仅 IN_OVEN 且工件仍在炉），`readings:[{workpiece_id,probe_id,ts,metal_temp_c}]`，接受后即时重算进度 |
| GET  | `/batches/<id>/progress` | 在炉固化进度与安全出炉预测（可带 `?as_of=` 计算基准，逐件状态/阻塞/告警，炉次级最晚安全出炉与计划过早分钟）；**汇总仅计算仍在炉工件** |
| POST | `/batches/<id>/workpieces/<wid>/unload` | **逐件出炉**：按 `at` 判定单件；不达标普通请求 409（给累计/剩余/阻塞原因），`force=true`+`reason` 强制出炉（不合格+审计） |
| POST | `/batches/<id>/unload` | 整炉出炉判定：逐件复用同一判定，**已离炉工件跳过**；最后一件离炉后炉次 UNLOADED |
| POST | `/batches/<id>/close` | 炉次结案（存在未了结工件时 409 并列出） |
| POST | `/workpieces/<id>/probes` | 登记/更新工件探头及校准偏移（签发时冻结快照） |
| GET  | `/workpieces/<id>/probes` | 工件已登记探头列表 |
| POST | `/batches/<bid>/workpieces/<wid>/probes/<pid>/disable` | 出炉前停用故障探头（须 `reason`），只重算该工件并记录结果变化 |
| POST | `/workpieces/<id>/rework` | 返工：回到待排产队列 |
| POST | `/workpieces/<id>/close` | 工件结案（合格入库/报废注明 note） |
| GET  | `/batches` `?state=` | 炉次列表 |
| GET  | `/batches/<id>` | 炉次详情（含每件固化累计、探头状态、判定序列与标记） |
| GET  | `/batches/<id>/archive` | 下载 JSON 炉次档案 |
| GET  | `/batches/<id>/card` | 可打印随炉卡（HTML，含探头与判定序列） |
| GET  | `/workpieces/<id>` | 工件状态、探头、履历、标记 |
| GET  | `/versions` | 排产版本链 |
| GET  | `/health` | 健康检查 |

### 应用配置（`create_app` 可覆盖）

| 配置 | 默认 | 说明 |
|---|---|---|
| `PROBE_GAP_MINUTES` | 10 | 启用探头缺报超过该分钟数记为探头中断，且不再计为有效探头 |
| `PROBE_DIVERGENCE_C` | 5.0 | 同一时刻有效探头校正值极差超过该温度记为温差异常 |
| `STUCK_PROBE_MIN_CONSECUTIVE` | 5 | 同一探头连续相同读数达到该点数记为卡值 |
| `MIN_VALID_PROBES` | 1 | 判定合格所需的最少有效探头数 |

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
