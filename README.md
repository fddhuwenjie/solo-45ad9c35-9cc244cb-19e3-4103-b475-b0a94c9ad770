# 粉末喷涂烘炉固化排产系统（ovenline）

本地 REST API：Python + Flask + SQLite，覆盖粉末固化生产的**排产 → 签发 → 入炉 →
测温回传 → 出炉判定 → 冷却放行 → 返工结案**全流程。

核心原则：**空气温度到达设定值不代表固化开始**——系统按每件工件金属探头温度处于
许可区间的**累计分钟数**判定固化是否充分；混炉须同时满足各粉料固化窗口交集、
同炉禁配组、工件间距（挂位）与吊具承重。

多探头：同一工件可登记**多个金属探头**同步测温（各带校准偏移或绑定校准证书
版本），签发时冻结配置；判定序列取**每个采样时刻有效探头的最低校正温度**，
单个探头松脱、卡值或漂移不会直接决定整件返工——可在出炉前停用故障探头，
系统只重算该工件并记录结果变化。

吊具布置与载荷平衡：大门板全挂炉架一侧时，即使每个吊点都不超载，横梁仍会
偏载。试算可携带**挂杆/吊点坐标模型**（`hanger_rack`：多挂杆、吊点 x 坐标、
单点限载、横梁分区载荷、横梁总载、左右偏载力矩容差），工件可带**重心偏移、
可旋转方向、吊耳坐标、要求净距**；引擎把工件映射到**具体吊点坐标**，检查
净距、共享吊点、单点承重、分区承重、横梁总载与左右力矩，并返回**可复现的
挂位组合与搬入顺序**。偏载按**整组方案**判定（单件偏载不直接拒绝，对侧补挂
对称件后力矩归零仍可同炉）；现场临时封掉挂位用 `hanger_blackouts`（只在与
炉次占用时段重叠时封点），人工调整可用校验端点复核；**签发后布置冻结**，
吊点故障登记（`/schedule/point-fault`）只重排未签发炉次，并给出迁移工件与
交期变化。


校准证书版本化：探头可录入**不可覆盖的多点校准版本**（证书号、校准/到期时刻、
示值—参考值点列），绑定探头时指定版本；签发按计划入炉时刻检查证书有效期与
粉料温区覆盖；测温按签发时冻结的点列**线性插值**，区间外读数不计入保温累计；
新证书只供未签发炉次使用，各炉次采用的证书版本全程可追溯。

## 快速开始

```bash
pip install -r requirements.txt   # Flask>=3.0
python3 run.py                    # 监听 127.0.0.1:5000，自动建库 instance/ovenline.sqlite

# 三组本地请求样例（另开终端执行）
bash samples/scenario_a_normal.sh             # 正常闭环
bash samples/scenario_b_conflicts_rework.sh   # 冲突/越序/返工/版本链
bash samples/scenario_c_probes.sh             # 多探头测温/停用故障探头
bash samples/scenario_d_piece_unload.sh       # 轻薄/厚重混炉逐件出炉、强制出炉
bash samples/scenario_e_blackout.sh           # 停机窗避让/冲突/逐炉时间线
bash samples/scenario_f_calibrations.sh       # 探头校准证书版本化/签发检查/插值
bash samples/scenario_g_racking.sh            # 吊具布置/载荷平衡/人工校验/吊点故障
bash samples/scenario_h_cooling.sh            # 厚板冷却测温/区间截断/正常与紧急搬运放行

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
  cooling.py               冷却放行：连续低温区间/截断/最早放行时刻（纯函数）
  probes.py                多探头判定：校正/判定序列/异常检测（纯函数）
  progress.py              在炉进度与安全出炉预测（纯函数，基于 probes.analyze）
  views.py                 REST 路由与状态机
samples/
  scenario_a_normal.sh             样例一：正常闭环
  scenario_b_conflicts_rework.sh   样例二：冲突/返工/版本
  scenario_c_probes.sh             样例三：多探头测温
  scenario_d_piece_unload.sh       样例四：逐件出炉/强制出炉
  scenario_e_blackout.sh           样例五：停机窗避让/冲突/时间线
  scenario_f_calibrations.sh       样例六：校准证书版本化/签发检查/插值
  scenario_g_racking.sh            样例七：吊具布置/载荷平衡/人工校验/吊点故障
  scenario_h_cooling.sh            样例八：厚板冷却测温/区间截断/正常与紧急搬运放行
  out/                             样例下载产物（档案 JSON、随炉卡 HTML）
```

## 业务规则

### 排产（试算）
- 混炉时各粉料固化窗口取**交集**，交集为空不得同炉；保温时长取同炉最大值；
- **同炉禁配组**两两不可同炉；
- 工件占用挂位数 = max(按水平尺寸/吊点间距， 按重量/单吊点承重)，取相邻挂位；
- 升温分钟 = (窗口中值 − 环境温度)/升温速率 + 装载公斤 × 热惯性系数；
- 同炉次按工件交期先后串行衔接，已签发/在炉炉次占用炉膛的时间段自动避让；
- **新建炉次选炉**：先分别应用各炉停机窗试算该工件的完工时刻，再按
  （逾期分钟, 完工时刻, 炉号）选优——键中不含请求顺序相关量，
  结果不随 `ovens` 传入顺序改变；
- **停机窗（清炉/校准/检修）**：试算请求可带 `blackout_windows`
  （每段含 `oven_id`、`kind`、`start_at`、`end_at`、`note`；
  `kind` 支持 `CLEANING`/`CALIBRATION`/`MAINTENANCE` 或中文 清炉/校准/检修）。
  起止倒序、同炉重叠、未知炉号一律 400 拒绝。
  排产时**装载、升温、保温及周转是一个不可拆分的占用区间**，撞上停机窗
  便整体移到该窗结束之后，再比较各炉完工时刻与交期；
- 排产同时保留一套**不应用停机窗的基准排程**，逐炉次差值即停机造成的
  **累计推迟**（上游炉次被推迟后，下游炉次同样承接）：每个新炉次返回
  `blackout_wait_minutes`（相对基准的等待分钟）与逾期变化
  （`lateness_minutes` / `baseline_lateness_minutes` /
  `lateness_delta_minutes`，按炉内最早交期衡量），以及基准时刻
  （`baseline_load_at` / `baseline_unload_at`）和本炉次直接避让的窗口；
- 试算响应带**逐炉时间线**（`oven_timelines`）：区分生产占用 `PRODUCTION`、
  周转 `TURNAROUND` 与停机 `BLACKOUT`，并给出各炉完工/释放时刻与逾期汇总；
- 停机窗写入**排产版本快照**（`blackout_windows` 表 + 版本 `params_json`），
  后续试算可整体改写；**已签发/在炉炉次不得改时刻**——若新窗口撞上这些炉次
  的占用区间（计划入炉 → 计划出炉+周转），响应 `blackout_conflicts` 列出
  每处重叠区间与冲突分钟（仅告警，试算照常完成）；
- 炉次详情、版本查询（`GET /versions/<id>`）与 JSON 档案均保留关联停机窗
  及计算依据（`schedule_basis`）；
- 无法安排的工件进入 `unscheduled` 并给出具体原因：
  `OVERSIZE`（尺寸超炉膛/挂位跨度）、`OVERWEIGHT`（超吊点承重）、
  `UNKNOWN_POWDER`（粉料批号未登记；该订单不入库，登记粉料后重新提交即可排产。
  复用已有 workpiece_id 提交未登记粉料时，该工件本次不参与编排，
  不会按库内残留的旧粉料进入新炉次）。

### 吊具布置与载荷平衡
- 炉架模型 `ovens[].hanger_rack`（缺省退化为旧连续编号：单挂杆、等距吊点、
  不查分区/总载/力矩）：
  - `rods[]` 挂杆：`id`、`axis`（L 沿炉长 / W 沿炉宽）、`y_mm/z_mm` 位置、
    `point_count`+`point_spacing_mm`（以挂杆中心 x=0 对称生成）或显式
    `points:[{index,x_mm,max_load_kg}]`（也可在顶层 `hanger_rack.points`
    用 `rod_id` 区分）、`zones:[{id,x_min_mm,x_max_mm,max_load_kg}]` 横梁分区；
  - `beam_max_load_kg` 横梁总载；`moment_tolerance_kg_mm` 左右偏载力矩容差
    （相对跨中 Σ载荷×力臂 的绝对值）与/或 `moment_tolerance_ratio`
    （力矩/总载，等效平均偏心 mm）；`default_clearance_mm` 默认净距、
    `lug_tolerance_mm` 吊耳对齐容差；
- 工件（`orders[]`）可选：`cg_offset_x_mm/cg_offset_y_mm` 重心相对几何中心
  偏移、`allowed_rotations_deg` 可旋转方向（默认 [0,90]）、
  `lift_points_mm` 吊耳沿长轴坐标（相对工件中心；给出即按吊耳对吊点，
  非吊耳对齐吊点只占位不承重）、`clearance_mm` 与相邻工件的要求净距；
- 载荷分配：均布承重时工件投影覆盖的相邻吊点按重心力臂静力学分配；吊耳
  承重时按吊耳对齐吊点（两个吊耳用杠杆法）。布置检查顺序（首个冲突约束
  代码，随拒绝明细返回）：`CHAMBER_FIT` 炉膛尺寸 → `ROD_SPAN` 挂杆跨度 →
  `POINT_BLOCKED` 禁用吊点 → `POINT_TAKEN` 净距/共享吊点 → `LUG_MATCH`
  吊耳对不上 → `CG_SUPPORT` 重心在支撑跨外 → `POINT_LOAD` 单点超载 →
  `ZONE_LOAD` 分区超载 → `BEAM_TOTAL` 横梁总载 → `MOMENT` 整组偏载；
- **偏载按整组方案判定**：引擎对炉内工件做光束搜索（beam search），在满足
  单点/分区/总载的候选挂位中选整组 |力矩| 最小的可复现方案；单个工件偏心
  不拒绝，只要整组（如对侧补挂对称件）满足容差即可。未设力矩容差的炉架
  保持 first-fit 旧确定性布置，不改位；
- 净距检查**同时计入相邻双方**的 `clearance_mm`（各取一半之和，相切允许）；
- 临时封位 `hanger_blackouts`：`{oven_id,rod_id,point_index,start_at,end_at}`，
  **只封与炉次占用区间（计划入炉→出炉+周转）重叠的吊点**；已结束的窗不影响
  后续开排炉次，未知炉号/挂杆/吊点或起止倒序一律 400；
- 炉次返回 `rack_layout`：每件 `placement`（挂杆/旋转/中心与重心坐标/
  占用与承重吊点载荷/吊挂方式）、`load_balance`（左右载荷、各分区、
  总载、力矩与是否合格）、`load_in_sequence` 搬入顺序（由内向外、同杆
  从左到右、再按工件号）；
- 放不下的工件进入 `unscheduled`：原因新增 `POINT_BLACKOUT`（禁用吊点）
  与 `LOAD_BALANCE`（整组平衡不满足），并给 `first_conflict`（首个冲突
  约束代码/炉号/挂杆/吊点/说明）、`per_oven` 逐炉明细与 `alternative_ovens`
  硬可行炉（尺寸/承重放得下，只是当前时段/平衡不满足）；拒绝明细随版本快照
  存入 `schedule_rejections`，版本详情 `GET /versions/<id>` 的 `rejections`
  可查；
- **人工调整**：`POST /batches/<id>/arrangement/verify`，body
  `{"assignments":[{workpiece_id,rod_id,point_indices,rotation_deg?}],
  "apply":false}`。逐条复核净距/共享吊点/单点/分区/总载/重心/吊耳/禁用吊点，
  并按整组复核总载与力矩；不通过返回 409 与 `violations`（不落库），
  `apply=true` 时仅 DRAFT 炉次可采用（已签发布置冻结，只能 check_only）；
- **吊点故障**：`POST /schedule/point-fault`
  `{oven_id,rod_id,point_index,reason,started_at?}` 登记跨版本持续故障，
  自动基于最新版本参数重排**未签发（DRAFT）炉次**：已签发/在炉炉次原样
  保留，占用故障吊点时列入 `frozen_conflicts`；草稿重排后返回 `migrations`
  （每件的炉号/挂杆/挂位/出炉时刻变化与逾期）。重复登记同一未修复故障
  返回 409；`POST /schedule/point-fault/<id>/resolve` 修复后吊点恢复可用，
  `GET /schedule/point-faults?active=1` 查询；
- 炉次详情、版本快照、JSON 档案与随炉卡均记录挂杆/吊点坐标、各点载荷、
  分区/总载、力矩与搬入顺序；签发时整套布置随炉次冻结（`arrangement_json`）。

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

### 探头校准证书版本化
固定偏移之外，探头可登记**多点校准证书版本**，全程追溯各炉次采用的校准：
- `POST /workpieces/<wid>/probes/<pid>/calibrations` 录入版本：
  `certificate_no`（证书号）、`calibrated_at`（校准时刻）、
  `valid_until`（到期时刻）、`points`（示值—参考值点列，
  `[{"indicated_c","reference_c"}, ...]`）。**少于两点、校准/到期时刻倒置、
  点列示值不递增一律 400 拒绝**；每次录入追加新版本（`version` 自增），
  已录入版本**不可覆盖**（无修改/删除接口）；
- `GET /workpieces/<wid>/probes/<pid>/calibrations` 查询历史版本
  （按版本升序，含点列与插值区间）；
- **绑定探头时指定版本**：`POST /workpieces/<id>/probes` 的探头条目可带
  `calibration_id`（须属于该探头，否则 400）；不带该键保留既有绑定，
  显式传 `null` 解除绑定；未绑定版本的探头保持固定偏移模式；
- **签发检查**：签发炉次按**计划入炉时刻**核对每个绑定探头的证书——
  版本缺失（`CALIBRATION_MISSING`）、尚未生效（`CALIBRATION_NOT_YET_VALID`）、
  已过期（`CALIBRATION_EXPIRED`）、点列区间未覆盖该工件粉料温区
  （`CALIBRATION_COVERAGE`）时，响应 409 并在 `calibration_problems` 中
  逐条列出相关工件、探头与原因，**阻止签发**（炉次保持 DRAFT，
  换证/改绑后可重新签发）；
- 签发时把证书版本与点列**冻结进炉次快照**（`batch_item_probes`），
  **新证书只供未签发炉次使用**，已签发炉次的判定不受换证影响；
- **测温插值**：绑定版本的探头按冻结点列对原始示值**线性插值**得到校正
  温度；**超出点列区间的读数不计入保温累计**（不参与判定序列与超温判定，
  原始值仍保留在 readings 表），并产生 `CALIBRATION_RANGE` 告警
  （进度响应 alerts 与固化分析 `calibration_range` 明细）；
- 批次查询、进度响应（`calibrations`）、JSON 档案与随炉卡均保留
  **证书版本、插值区间及到期状态**（相对计划入炉时刻判定 `expired`）。

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
工件：PENDING --> SCHEDULED --> IN_OVEN --合格离炉--> COOLING --冷却正常放行--> DONE --> CLOSED
                                   |                     └ 紧急搬运 --> REWORK_PENDING
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

### 冷却放行（厚板离炉后包装耐温门限）
厚板走出炉膛时涂层已固化，但工件**表面温度可能仍高于包装材料耐温上限**；
按计划时刻直接套膜/堆叠会压出印痕。合格件离炉后进入 **COOLING**（冷却放行
观察中，**暂不计为完成 DONE**），按带时标的**表面温度**形成连续低温区间：
- 粉料资料新增两个包装门限（试算 `powders[]`，两者须同时给出、非负）：
  `pack_temp_limit_c` 包装温度上限、`low_temp_hold_minutes` 低温保持时长；
  **签发时必须两字段齐全（默认强制，缺失 409 阻止签发，列 `pack_threshold_problems`）
  并冻结到工件**（`batch_items.snap_*`），签发后只读取冻结快照，**不回退
  后来修改的粉料主数据**——空快照炉次的冷却查询与 NORMAL 放行永远给出
  `PACK_LIMIT_MISSING`（应用配置 `REQUIRE_PACK_LIMIT_AT_ISSUE=false` 可放宽签发，
  但空快照仍不可放行；仅紧急搬运可用）；
- 离炉动作：安全合格件置 **COOLING**（不再直接 DONE），强制/不合格件仍为
  REWORK_PENDING；炉次仍按「最后一件离炉」转 UNLOADED。
- **冷却测温写入** `POST /batches/<id>/workpieces/<wid>/cooling-readings`，
  body `{"readings":[{ts, surface_temp_c}]}`（仅 COOLING 件接收，早于离炉
  时刻拒收）；以下情形**截断当前连续低温区间**（此前保持作废）：
  - **乱序** `OUT_OF_ORDER`：ts 早于已收录最新时标（读数照常入库标记，
    若本身不超温则锚定新区间）；
  - **同点重复**：同一时刻再次上报，无论温度是否相同都**保存本次提交**、
    记录区间中断并从该点**之后**重新累计（不做幂等忽略）——温度不同为
    `TS_CONFLICT`（不锚新区间），温度完全相同为 `TS_DUPLICATE`，响应中
    后者计入 `duplicates`、二者都计入 `conflicts`；进度/放行/批次查询/
    归档**不得拼接中断前后**的时间；
  - **采样间隔过长** `LONG_GAP`：相邻读数间隔超过缺报阈值
    （配置 `COOLING_GAP_MINUTES`，默认 10 分钟）；
  - **再次升温** `REHEAT`：读数高于包装温度上限，下一条低温读数另起新区间。
  区间有效保持按保守口径：相邻两点**都**不超温，该区间时长才计入。
- **进度查询** `GET /batches/<id>/workpieces/<wid>/cooling`（可带 `?as_of=`）
  给出：当前读数（时刻/表面温度/收录标记/新鲜度）、当前连续区间有效保持分钟
  `held_low_temp_minutes`、剩余 `remaining_hold_minutes`、按当前连续区间
  计算的**最早放行时刻** `earliest_release_at`（=最新读数+剩余保持，读数陈旧
  或最新超温时不给）、**未满足项** `unmet`
  （PACK_LIMIT_MISSING/NO_READING/REHEAT/HOLD_NOT_MET/STALE_READING），
  以及完整的已闭合区间与区间中断追溯；炉次级 `GET /batches/<id>/cooling`
  逐件汇总冷却中/已放行/紧急搬运计数。
- **搬运放行** `POST /batches/<id>/workpieces/<wid>/release`：
  - 正常放行（缺省）：COOLING 且门限已满足才成功（工件 → **DONE**）；
    **未达门限返回 409**，响应给有效保持分钟、最早放行时刻与未满足项，
    不留放行记录；
  - **紧急搬运** `{"emergency": true, "reason": "..."}`（理由必填）：
    不看门限，工件送**返工处置**（→ REWORK_PENDING，落 EMERGENCY_RELEASE
    标记与 `release_actions` 审计），再经返工接口回待排产队列。
- **结案须引用一次有效放行**：仍在 COOLING / REWORK_PENDING 的工件阻止炉次
  结案（409 列出）；DONE 件必须存在 NORMAL 放行记录。
- 批次详情/JSON 档案/随炉卡/工件履历均保留：**冻结门限、完整表面读数
  （含乱序标记）、区间中断、人工搬运决定与放行时冷却快照**（`release_order`）。

### 版本机制
每次试算生成一个 `schedule_versions` 记录（`parent_id` 指向上版本，参数快照存档）：
- **已签发/在炉炉次原样保留**（响应 `carried_batches`），其炉膛占用被新计划避让；
- **停机窗随版本快照存档**（`blackout_windows` 表 + 版本参数），后续试算可
  整体改写；新窗口撞上已签发/在炉炉次时在 `blackout_conflicts` 中列出
  重叠区间与冲突分钟，这些炉次时刻不变；
- 签发时对炉内工件的尺寸、重量、粉料固化窗口与**探头配置**（含校准证书
  版本与点列）做快照（`batch_items.snap_*` / `batch_item_probes`），
  后续修改主数据、录入新证书或生成新版本，均不改变已签发炉次的查询结果
  与出炉判定；
- 旧草稿作废（`SUPERSEDED`），待排产工件（含返工件）重新编排（`new_batches`）。

## API 一览（前缀 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/schedule/trial` | 试算：排出炉次/挂位/升温/保温/出炉时刻（避让停机窗），形成关联版本；响应含逐炉时间线、停机冲突清单、吊具布置（坐标/载荷/力矩/搬入顺序）、拒绝明细与整组平衡 |
| POST | `/batches/<id>/arrangement/verify` | **人工吊具布置复核**：assignments（工件→挂杆/吊点）逐条+整组校验；`apply=true` 时采用（仅 DRAFT），不通过 409 列违反约束 |
| POST | `/schedule/point-fault` | **登记吊点故障并重排**：只重排未签发炉次，已签发占用列入 frozen_conflicts；返回迁移工件/挂位/时刻与交期变化 |
| POST | `/schedule/point-fault/<id>/resolve` | 修复吊点故障（之后试算恢复使用该吊点，历史保留） |
| GET  | `/schedule/point-faults` | 吊点故障清单（`?active=0` 含已修复） |
| POST | `/batches/<id>/issue` | 签发（DRAFT→ISSUED，冻结工件/粉料/探头/校准证书快照；证书缺失/未生效/过期/温区覆盖不足时 409 阻止） |
| POST | `/batches/<id>/load` | 入炉（ISSUED→IN_OVEN），body 可带 `at` |
| POST | `/batches/<id>/readings` | 测温回传（仅 IN_OVEN 且工件仍在炉），`readings:[{workpiece_id,probe_id,ts,metal_temp_c}]`，接受后即时重算进度 |
| GET  | `/batches/<id>/progress` | 在炉固化进度与安全出炉预测（可带 `?as_of=` 计算基准，逐件状态/阻塞/告警，炉次级最晚安全出炉与计划过早分钟）；**汇总仅计算仍在炉工件** |
| POST | `/batches/<id>/workpieces/<wid>/unload` | **逐件出炉**：按 `at` 判定单件；不达标普通请求 409（给累计/剩余/阻塞原因），`force=true`+`reason` 强制出炉（不合格+审计） |
| POST | `/batches/<id>/unload` | 整炉出炉判定：逐件复用同一判定，**已离炉工件跳过**；最后一件离炉后炉次 UNLOADED；合格件进入 COOLING（暂不完成） |
| POST | `/batches/<id>/workpieces/<wid>/cooling-readings` | **冷却测温写入**：仅 COOLING 件，`readings:[{ts,surface_temp_c}]`；乱序/同点重复（含同温度 TS_DUPLICATE）/长间隔/再次升温均保存并截断连续低温区间，从该点后重新累计 |
| GET  | `/batches/<id>/workpieces/<wid>/cooling` | **单工件冷却进度**：当前读数、当前区间有效保持分钟、最早放行时刻、未满足项、区间中断追溯（可带 `?as_of=`） |
| GET  | `/batches/<id>/cooling` | **炉次冷却汇总**：逐件冷却状态与冷却中/已放行/紧急搬运计数 |
| POST | `/batches/<id>/workpieces/<wid>/release` | **冷却搬运放行**：门限满足正常放行（→DONE）；未达门限 409；`emergency=true`+`reason` 紧急搬运送返工（→REWORK_PENDING） |
| POST | `/batches/<id>/close` | 炉次结案（COOLING/REWORK_PENDING 工件或 DONE 无有效放行记录时 409 并列出） |
| POST | `/workpieces/<id>/probes` | 登记/更新工件探头及校准偏移，可带 `calibration_id` 绑定校准版本（签发时冻结快照） |
| GET  | `/workpieces/<id>/probes` | 工件已登记探头列表（含绑定的证书版本摘要） |
| POST | `/workpieces/<wid>/probes/<pid>/calibrations` | 录入探头校准证书版本（不可覆盖；少于两点/时间倒置/点列不递增拒绝） |
| GET  | `/workpieces/<wid>/probes/<pid>/calibrations` | 探头校准证书历史版本（含点列与插值区间） |
| POST | `/batches/<bid>/workpieces/<wid>/probes/<pid>/disable` | 出炉前停用故障探头（须 `reason`），只重算该工件并记录结果变化 |
| POST | `/workpieces/<id>/rework` | 返工：回到待排产队列 |
| POST | `/workpieces/<id>/close` | 工件结案（合格入库/报废注明 note） |
| GET  | `/batches` `?state=` | 炉次列表 |
| GET  | `/batches/<id>` | 炉次详情（含每件固化累计、探头状态、判定序列与标记） |
| GET  | `/batches/<id>/archive` | 下载 JSON 炉次档案 |
| GET  | `/batches/<id>/card` | 可打印随炉卡（HTML，含探头与判定序列） |
| GET  | `/workpieces/<id>` | 工件状态、探头、履历、标记 |
| GET  | `/versions` | 排产版本链（含各版本炉次数与停机窗数） |
| GET  | `/versions/<id>` | 版本详情：参数快照（含停机窗）与该版本排出的炉次 |
| GET  | `/health` | 健康检查 |

### 应用配置（`create_app` 可覆盖）

| 配置 | 默认 | 说明 |
|---|---|---|
| `PROBE_GAP_MINUTES` | 10 | 启用探头缺报超过该分钟数记为探头中断，且不再计为有效探头 |
| `PROBE_DIVERGENCE_C` | 5.0 | 同一时刻有效探头校正值极差超过该温度记为温差异常 |
| `STUCK_PROBE_MIN_CONSECUTIVE` | 5 | 同一探头连续相同读数达到该点数记为卡值 |
| `MIN_VALID_PROBES` | 1 | 判定合格所需的最少有效探头数 |
| `COOLING_GAP_MINUTES` | 10 | 冷却测温采样间隔超过该分钟数截断低温区间；最新读数距基准时刻超过该值视为陈旧不得放行 |
| `REQUIRE_PACK_LIMIT_AT_ISSUE` | true | 签发时粉料必须登记包装温度上限/低温保持时长；关闭后允许签发空快照炉次（冷却 NORMAL 放行仍永久以 PACK_LIMIT_MISSING 阻塞） |

### 试算请求体要点

```json
{
  "reason": "排产原因（记入版本）",
  "start_at": "2026-09-10T08:00:00",
  "ovens":   [{"id","chamber_l_mm","chamber_w_mm","chamber_h_mm",
               "heat_rate_c_per_min","mass_factor_min_per_kg","ambient_c",
               "turnaround_minutes","hanger_slots","hanger_spacing_mm","hanger_max_load_kg",
               "hanger_rack?":{"rods":[{"id","axis?","y_mm?","z_mm?",
                 "point_count?","point_spacing_mm?","default_point_load_kg?",
                 "points?":[{"index","x_mm","max_load_kg?"}],
                 "zones?":[{"id","x_min_mm","x_max_mm","max_load_kg"}]}],
                 "points?":[{"rod_id","index","x_mm","max_load_kg?"}],
                 "beam_max_load_kg?","moment_tolerance_kg_mm?",
                 "moment_tolerance_ratio?","default_clearance_mm?",
                 "lug_tolerance_mm?"}}],
  "powders": [{"batch_no","temp_min_c","temp_max_c","hold_minutes",
               "pack_temp_limit_c"?,"low_temp_hold_minutes"?}],
  "forbidden_pairs": [["GRP_A","GRP_B"]],
  "blackout_windows": [{"oven_id","kind","start_at","end_at","note"}],
  "hanger_blackouts": [{"oven_id","rod_id","point_index","start_at","end_at?","note?"}],
  "orders":  [{"workpiece_id","order_id","length_mm","width_mm","height_mm",
               "weight_kg","powder_batch","compat_group","due_at",
               "cg_offset_x_mm?","cg_offset_y_mm?","allowed_rotations_deg?",
               "lift_points_mm?","clearance_mm?"}]
}
```

`blackout_windows` 可省略；`kind` 取 `CLEANING`（清炉）/ `CALIBRATION`（校准）/
`MAINTENANCE`（检修），也接受中文 清炉/校准/检修。`hanger_blackouts` 为吊点
禁用时段（临时封位），`end_at` 可省略表示开放结束；禁用只在与炉次占用时段
重叠时生效。未给 `hanger_rack` 时退化为旧连续编号模型（单挂杆、等距吊点、
不查分区/总载/力矩）。

时间为本地 ISO 格式（可带时区，将转为本地时间存储）。
