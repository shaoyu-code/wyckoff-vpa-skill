# 唯一输出合同：analysis.json 1.2.0

机器合同：`references/analysis.schema.json`。本文件只解释语义，不创建第二套 JSON 结构。

## 1. 顶层字段

```text
schema_version
lineage
instrument
data_quality
reference_levels
structure
decision
confluence
main_plan
alt_plan
invalidation
manage
risk
probability
validation_status
disclaimer
```

输入阶段必须为 `schema_version=1.2.0`、`validation_status=unvalidated`。最终阶段只允许：

- `validated_trade`：方向性方案通过全部硬闸门；
- `validated_no_trade`：合法空仓方案；
- `blocked`：Schema、证据、风险或安全闸门失败后的 fail-closed 结果。

## 2. 证据与品种

`lineage` 绑定日线/周线的 `run_id` 和 `candidates.json` 文件哈希。Agent 不得修改。

`instrument` 的品种类型、合约乘数、报价币和是否需要实际交易载体由 resolved symbol 决定。Agent 输入中的 `actual_tradable_symbol` 必须为 `null`；非空仓校验时由独立风险配置写入。

`data_quality.daily/weekly` 必须与经过原始行情重算后的候选证据完全一致。

## 3. 事件证据

`events_confirmed` 的每一项必须包含：

```text
id
evidence_hash
date
type
bar_index
price
extreme_price
boundary
related_event_id
why
```

`events_rejected` 必须包含候选的 `id/evidence_hash/date/type` 与拒绝原因。

`structure.setup_evidence` 和 `main_plan.setup_evidence` 必须完全相同，并包含：

```text
core_event_id
core_event_hash
retest_id
retest_hash
reviewed_event_ids
```

`reviewed_event_ids` 必须精确覆盖活动审查窗口内全部可确认候选，不能只选择有利证据。

## 4. 结构化触发

`trigger` 字段：

```text
text
mode = retest_close_volume
bar_date
price_rule {field=close, operator, value}
volume_rule {field=vol_rel, operator=<=, value}
confirmed
evidence_hash
```

Agent 输入阶段 `evidence_hash` 必须为 `null`。校验器在与所选回测相同的真实 K 线上执行价格与成交量规则，并写入最终证据哈希。未来日期、恒真表达式和纯文字触发无效。

## 5. 主方案与反向预案

合法方向：`long | short | no_trade`。合法动作：

- 主方案：`enter_long | enter_short | wait | stand_aside`；
- 反向预案：只能保持 `wait | stand_aside`，不能预先成为自动反手指令。

设置白名单：`spring_retest | ut_retest | sos_lps | sow_lpsy`。

非空仓主方案必须提供方向一致的 `entry_zone`、`stop`、`t1`、`t2`。Agent 输入阶段 `rr_t1`、仓位受控字段和概率数值必须为空，由校验器计算。

反向预案必须描述未来对立事件、结构化触发、入场、止损、T1/T2，但 `trigger.confirmed=false`。其概率由校验器独立评估，不能从主方案机械减分。

## 6. 风险、概率和终态

主方案 `position` 的账户权益、FX、实际交易产品、乘数和数量来自 `risk_config.json`，不能由 Agent 提供。

概率对象必须声明：

```text
method = heuristic_uncalibrated
event = reach_t1_before_stop
historical_win_rate = false
cap = 72
conditional_on = [...]
```

输入阶段概率状态为 pending 且数值为空；最终数字只能由校验器写入，单位为整数百分比。

用户报告只能读取 `analysis.validated.json`。方向性方案还必须同时满足 `validation.json.valid=true` 和 `trade_gates_pass=true`。`validation.json.analysis_validated_sha256` 必须等于验证结果文件的规范化 JSON 哈希。
