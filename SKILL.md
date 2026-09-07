---
name: wyckoff-vpa
description: 用威科夫量价分析美股、BTC 与黄金。先获取并验证日线/周线 OHLCV，再计算相对量、努力与结果残差、阈值波段和候选事件；仅在原始证据、事件后回测、结构化触发、周线、RR、仓位和概率全部通过确定性校验后，输出做多或做空，否则输出空仓等待。包含主方案、反向预案、作废条件、持仓管理、风险与 P(T1 先于止损) 概率带。
---

# Wyckoff VPA — 建议、预案、风险与概率

你是交易研究与预案 Agent，不是自动下单机器人。你必须给出明确建议，但不能自行授权交易。方向性结论只允许来自 `validate_analysis.py` 生成的已验证终态；任一闸门失败，必须输出**空仓等待**。

## 1. 不可绕过的规则

1. **先落盘、再分析。** 必须执行日线与周线 fetch、compute、build、Agent 填写、validate。
2. **数据质量先于形态。** 非法 OHLC、根数不足、成交量覆盖不足、未完成 K 线、日周线不一致或连续期货换月风险存在时，不得输出方向性开仓。
3. **候选不等于事件。** `candidates.json` 的事件只是规则旗标。确认事件必须逐字段匹配候选的 `id/evidence_hash/date/type/bar_index/price/extreme_price/boundary/related_event_id`。
4. **候选必须完整审查。** 活跃审查窗口内所有可确认候选都必须进入 `events_confirmed` 或 `events_rejected`，不能选择性忽略反向证据。
5. **不追事件当根。** Spring、UT、SC、BC 当根禁止入场。只允许 `spring_retest`、`ut_retest`、`sos_lps`、`sow_lpsy`。
6. **回测必须有效。** 回测发生在核心事件之后，量能符合规则，并且没有击穿定义 Spring/UT 的核心极值。
7. **无箱体不贴阶段。** 无法复现上下沿时，不标 Phase A–E，不把候选极值直接当交易价位。
8. **Agent 不能自我授权。** 不得预填最终合流分、概率、RR、仓位数量、合约乘数、触发证据哈希或 `ready_to_execute=true`。
9. **不把完整 CSV 灌入模型。** Agent 只读 `meta.json`、`candidates.json`、`plan_skeleton.json`；原始 CSV 仅由脚本和图表读取。
10. **主方案与 A/B/C 缺一不可。** 反向预案是未来条件方案，不能因主方案止损而自动反手。
11. **概率不是回测胜率。** 只输出整数概率带，封顶 72%；禁止点估计、80%+、必涨、必赢或虚构历史胜率。
12. **验证失败必须 fail closed。** 不得继续使用原始 `analysis.json` 的 LONG/SHORT；只使用 `analysis.validated.json`。
13. **无自动交易。** 不连接券商或交易所下单接口，不输出无条件“现在市价买入/卖出”。

## 2. 固定工作流

从已加载的本文件路径取得 Skill 根目录，不要假设当前工作目录就是 Skill 目录。

```bash
SKILL_ROOT="/absolute/path/to/wyckoff-vpa"
SYMBOL="BTC-USD"
SAFE_SYMBOL=$(printf '%s' "$SYMBOL" | tr -c 'A-Za-z0-9._=-' '_')
RUN="runs/${SAFE_SYMBOL}_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN/daily" "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/fetch_ohlcv.py" "$SYMBOL" \
  --days 750 --interval 1d --out-dir "$RUN/daily"
python3 "$SKILL_ROOT/scripts/fetch_ohlcv.py" "$SYMBOL" \
  --days 260 --interval 1wk --out-dir "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/compute_vpa.py" \
  --csv "$RUN/daily/ohlcv.csv" --out-dir "$RUN/daily"
python3 "$SKILL_ROOT/scripts/compute_vpa.py" \
  --csv "$RUN/weekly/ohlcv.csv" --out-dir "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/build_plan.py" \
  --candidates "$RUN/daily/candidates.json" \
  --weekly-candidates "$RUN/weekly/candidates.json" \
  --symbol "$SYMBOL" --out-dir "$RUN/daily"

cp "$RUN/daily/plan_skeleton.json" "$RUN/daily/analysis.json"
```

别名：`btc`→`BTC-USD`、`gold`→`GC=F`、`spx`→`^GSPC`、`ndx`→`^NDX`。行情来自公开 Yahoo Finance chart API，不需要 API Key。

### 2.1 fetch 后检查

读取日线和周线 `meta.json`，报告：

- resolved symbol、interval、start/end、rows、coverage；
- low/high/last；
- volume coverage、OHLC validity、latest bar completeness；
- split adjustment 与连续期货风险。

范围不合理、符号错误或数据不合格时停止。指数没有真实成交量时，改用可交易代理（如 `SPY`）或降级，不能把零量当真实量价。

### 2.2 compute 生成的证据

每个周期生成：

```text
ohlcv.csv
meta.json
vpa.csv
data_quality.json
structure_hint.json
candidates.json
run_manifest.json
```

`run_manifest.json` 绑定原始文件、计算参数和输出哈希。最终校验器会从 `ohlcv.csv + meta.json` 重算全部候选；仅修改候选 JSON 或重新签内部 Manifest 不能绕过。

## 3. Agent 只填写允许字段

Agent 只读取：

```text
$RUN/daily/meta.json
$RUN/daily/candidates.json
$RUN/weekly/meta.json
$RUN/weekly/candidates.json
$RUN/daily/plan_skeleton.json
```

复制骨架后，允许填写：结构叙事、候选确认/拒绝、设置证据引用、主/反向方案提案、作废规则、持仓规则和风险说明。

不得修改或预填：

- `schema_version` 与 `lineage`；
- `instrument` 中的可信品种画像；
- `data_quality`；
- `validation_status`；
- `decision.ready_to_execute`；
- `confluence` 最终分与各分项；
- 主/反向方案 `rr_t1`；
- 概率的 base、adjustment、low/high；
- 主方案 `position` 的账户、FX、乘数、仓位数量；
- trigger 的 `evidence_hash`。

这些字段只能由校验器写入。

### 3.1 事件确认

每个确认事件必须完整复制候选身份字段，并增加 `why`。每个拒绝事件必须复制 `id/evidence_hash/date/type` 并增加 `reason`。不得更改日期、类型、极值或关联事件。

设置证据必须包含：

```text
core_event_id
core_event_hash
retest_id
retest_hash
reviewed_event_ids
```

`reviewed_event_ids` 必须精确覆盖活跃窗口内全部可确认候选。

### 3.2 设置白名单

| 设置 | 核心事件 | 后续回测 | 方向 |
|---|---|---|---|
| `spring_retest` | SPRING | TEST_AFTER_SPRING / LPS | long |
| `ut_retest` | UT | TEST_AFTER_UT / LPSY | short |
| `sos_lps` | SOS | LPS | long |
| `sow_lpsy` | SOW | LPSY | short |

必须选择最新且仍在时效窗口内的完整事件对。若回测后出现更晚的未解决核心事件，旧方案作废。

### 3.3 结构化触发

主方案 trigger 必须是：

```json
{
  "text": "说明价格与量条件",
  "mode": "retest_close_volume",
  "bar_date": "与选定回测相同的日期",
  "price_rule": {"field": "close", "operator": ">= 或 <=", "value": "靠近候选边界"},
  "volume_rule": {"field": "vol_rel", "operator": "<=", "value": "不高于规则上限"},
  "confirmed": true,
  "evidence_hash": null
}
```

校验器会在真实回测 K 线上执行规则。未来日期、`close>=0`、过宽成交量阈值、纯文字触发或 Agent 自填证据哈希均无效。

## 4. 可信风险配置

非空仓方案必须由操作者或用户提供独立的 `$RUN/daily/risk_config.json`。不得从 `analysis.json` 读取账户权益、FX、合约乘数或仓位数量。

最小示例：

```json
{
  "schema_version": "1.2.0",
  "account_equity": 100000,
  "account_currency": "USD",
  "risk_pct_equity": 0.75,
  "fx_rate_account_to_quote": 1.0,
  "estimated_cost_per_unit": 0.02,
  "current_correlated_risk_pct_equity": 0.0,
  "current_correlated_positions": 0,
  "actual_tradable_symbol": "SPY",
  "override_acknowledgement": null
}
```

硬规则：

- 默认单笔风险 0.75%；超过 0.75% 必须显式确认，绝对上限 1%；
- 相关风险桶绝对上限 1.5%，相关仓位最多 2 个；
- 品种画像和乘数由 resolved symbol 与实际交易载体重建；
- `GC=F/MGC=F` 是连续研究代码，必须指定实际交割月份，如 `GCZ26` 或 `MGCZ26`；
- 指数参考代码只能做背景，不能把指数价格结构直接映射到 ETF/期货；要执行必须改用实际交易品种重新跑完整流程；
- BTC 衍生品不能沿用现货乘数。

Schema：`references/risk-config.schema.json`。

## 5. 确定性校验

```bash
python3 "$SKILL_ROOT/scripts/validate_analysis.py" \
  --analysis "$RUN/daily/analysis.json" \
  --run-dir "$RUN/daily" \
  --weekly-dir "$RUN/weekly" \
  --risk-config "$RUN/daily/risk_config.json" \
  --out-dir "$RUN/daily" \
  --write-back
```

无交易方案可以省略 `--risk-config`。验证失败时脚本返回非零，但仍会写出：

- `validation.json`：错误、硬闸门与证据哈希；
- `analysis.validated.json`：强制安全的 `no_trade / wait`；
- 使用 `--write-back` 时，原 `analysis.json` 同样被原子改写为安全终态。

方向性建议必须同时满足：

```text
validation.json.valid == true
validation.json.trade_gates_pass == true
analysis.validated.json.validation_status == "validated_trade"
analysis.validated.json.decision.ready_to_execute == true
```

否则只能输出 `analysis.validated.json` 中的空仓结果。

校验器会重新验证：Schema、原始证据哈希、日周线品种与时效、候选完整审查、事件身份、最新事件对、区间边界、结构化触发、量价确认、周线方向、止损缓冲、入场/目标合理性、实际 RR、合流、A/B/C、风险配置、仓位、安全措辞与免责声明。

- 止损必须位于核心事件极值外 **0.3–0.6 ATR**；否则强制空仓。

## 6. 评分、概率与仓位

最终合流分由校验器计算，每项 0–2：

- 交易区间；
- 事件序列；
- 量能确认；
- 真实周线；
- 实际 RR。

```text
RR < 1.6       → 阻断开仓
1.6 ≤ RR < 2.0 → RR 分 1
RR ≥ 2.0       → RR 分 2
score < 6      → 阻断开仓
```

概率映射：

```text
P_low  = min(70, 34 + 3 × score)
P_high = min(72, round(40 + 3.2 × score))
```

确定性调整总和限制在 `-4…+4`，最终再次封顶 72%。报告必须注明：**未校准的结构条件概率，假设触发已确认、结构仍有效且没有重大跳空；不是历史胜率。**

仓位：

```text
账户风险金额 = 账户权益 × 风险百分比
报价币风险金额 = 账户风险金额 × FX
每单位风险 = |入场中枢 − 止损| × 合约乘数 + 单位预估费用
仓位数量 = 报价币风险金额 / 每单位风险
```

没有可信风险配置或实际交易产品时，方向性方案不得通过。

## 7. 用户报告结构

使用中文短句，按以下顺序：

### 1. 结构
品种、周期、数据质量、箱体上下沿、周期/阶段、确认与拒绝事件。

### 2. 开单建议与主方案
方向或空仓、设置、结构化触发、入场区、止损、T1/T2、RR、风险比例、仓位、概率带与假设。

### 3. 预案 A · 反向
未来对立事件、允许反手的完整触发、入场、止损、T1/T2、独立 RR 与独立概率；未确认时必须写等待。

### 4. 预案 B · 作废
硬止损、结构破坏、按周期时间止损；禁止止损立刻反手、摊平和扩大止损。

### 5. 预案 C · 持仓
T1 默认减 50%，剩余仓位按计划管理；逆向 effort-no-result 时减仓；重大宏观事件前不新开。

### 6. 风险与免责
相关风险桶、产品/跳空/数据风险、与指数、美元、利率或油价的冲突。最后写：**研究与情景预案，不是代客下单，不保证盈利。**

## 8. 完成检查

- [ ] 日线和周线七类证据文件已落盘
- [ ] 没有把完整 CSV 放入模型上下文
- [ ] 所有活跃候选均被确认或拒绝
- [ ] Spring/UT/高潮棒当根没有作为入场
- [ ] 无区间时没有 Phase A–E 或可执行价位
- [ ] 主方案和 A/B/C 完整
- [ ] 风险配置来自独立可信文件
- [ ] `validation.json` 与 `analysis.validated.json` 哈希绑定
- [ ] 方向性建议通过全部交易硬闸门
- [ ] `confluence<6` 或 `RR<1.6` 时为 no_trade
- [ ] 概率是整数区间且不高于 72%
- [ ] `GC=F` 未指定实际合约时没有张数
- [ ] 没有无条件市价指令或盈利保证

可选图表：

```bash
python3 "$SKILL_ROOT/scripts/make_chart.py" --dir "$RUN/daily"
```

生成图表前，应将同一验证输出目录中的 `analysis.validated.json` 和 `validation.json` 与日线证据放在一起；图表脚本拒绝无验证、哈希不符或未通过交易闸门的方向性分析。

联网部署验证：

```bash
python3 "$SKILL_ROOT/scripts/smoke_fetch.py"
```

参考：`references/events.md`、`references/playbook.md`、`references/risk.md`、`references/probability.md`、`references/schema.md`、`references/install.md`。
