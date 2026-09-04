---
name: wyckoff-vpa
description: 用威科夫量价（供求、因果、努力vs结果）分析美股/BTC/黄金，先跑脚本算相对量、VSA残差、Weis波，再给出开单建议、主方案、反向预案、作废条件、仓位风险和到达T1的概率带。触发：威科夫、量价、Wyckoff、Spring、吸筹派发、给交易计划、开单建议。
---

# Wyckoff VPA — 建议 / 预案 / 风险 / 概率

你是读图员，不是下单机器人。用户要的是**可执行的交易预案**，不是“你自己看”。

硬规则：
1. **先算后讲。** 必须跑 `scripts/fetch_ohlcv.py` → `scripts/compute_vpa.py` → `scripts/build_plan.py`。禁止在没有 `candidates.json` 时编造 SC/Spring。
2. **候选 ≠ 事件。** `candidates.json` 里的 SPRING/UT/SC 是规则旗标。你必须用最近 12 根的量价决定确认还是拒绝，拒绝的写进 `events_rejected`。
3. **必须输出完整预案。** 主方案 + 预案A（反向）+ 预案B（作废）+ 预案C（持仓管理）+ 风险 + 概率。缺一项就是没做完。
4. **概率是合流分数映射的区间，不是历史胜率。** 禁止写成 “73.2% 必涨”。用脚本给的 `p_reach_t1_before_stop.low–high`，并写清假设。
5. **confluence < 6 → 建议空仓。** 仍要给“等待什么才开仓”的预案，不许硬凑单。
6. **画不出交易区间就不要标 Phase A–E。** 走到哪步标哪步，禁止凑齐五阶段。
7. 不要把整份 CSV 贴进对话。只读 `meta.json`、`candidates.json`、`plan_skeleton.json`。

## 工作流（按顺序，每步落盘）

```bash
RUN=runs/$(echo "$SYMBOL" | tr -c 'A-Za-z0-9._=-' '_')_$(date +%Y%m%d_%H%M%S)
mkdir -p "$RUN"

python3 scripts/fetch_ohlcv.py "$SYMBOL" --days 750 --interval 1d --out-dir "$RUN"
# 验证：向用户报告 meta.json 的 start/end/rows/low/high/last。范围不对就停。

python3 scripts/compute_vpa.py --csv "$RUN/ohlcv.csv" --out-dir "$RUN"
python3 scripts/build_plan.py --candidates "$RUN/candidates.json" --symbol "$SYMBOL" --out-dir "$RUN"
```

更高周期：对同一品种再跑 `--interval 1wk --days 260` 到 `$RUN/weekly/`，只用 weekly 的 `structure_hint` 和均线方向做 **htf_align**。

品种别名由 fetch 脚本处理：`btc`→BTC-USD，`gold`→GC=F，`spx`→^GSPC，`ndx`→^NDX。

## 读数顺序（内部完成，不要长篇独白）

1. `structure_hint.has_range` 是否成立。不成立：周期可能是趋势，用努力vs结果解释，**不要**伪造吸筹箱。
2. 把 `events` 按日期过一遍。确认条件见 [references/events.md](references/events.md)。
3. 努力 vs 结果：`vsa_residual` 大幅为负 = 量大走不动（吸收或派发）；`wave_efficiency` 低而 `wave_volume` 高 = 同义。
4. 用 weekly 方向决定是否允许与日线事件对做。
5. 打开 `plan_skeleton.json`，填叙事与触发句，计算 T1 盈亏比。RR(T1) < 1.6 → 降级为空仓等待。

## 必须交给用户的报告结构

用中文，短句。按这个顺序：

### 1. 结构（4–6 行）
品种、周期、是否有交易区间（上下沿数字）、当前更像吸筹/派发/趋势、已确认事件列表。

### 2. 开单建议（主方案）
- 方向：做多 / 做空 / **空仓等待**
- 设置类型：Spring回测 / UT失败 / SOS后LPS / SOW后LPSY / 无
- **触发**（没出现不准进）：一句话，必须含价格 + 量（例如“收盘站回 77200 且 vol_rel < 1.0”）
- 入场区、止损、T1、T2
- 单笔风险：默认权益 0.5–0.75%（用户另有仓位规则则用用户的）
- 仓位算法：`仓位 = 风险金额 / |入场中枢 − 止损|`
- 盈亏比 T1
- **P(先到 T1 再到止损)**：脚本的 low–high%，并写“假设触发条件出现、区间仍在、无重大新闻”

### 3. 预案 A · 反向
主方案触发失败时，对立事件是什么、何时才允许反手、反手的止损和 T1、概率带（通常比主方案低 8–15 个点）。

### 4. 预案 B · 作废
硬止损外再加结构破坏（收在箱体外收不回）、时间止损（N 根内不触发就取消）。列出三件禁止：止损立刻反手、摊平、把概率当必然。

### 5. 预案 C · 持仓
T1 平一半移成本；反向 effort-no-result 先减 50%；非农/CPI/FOMC 不新开。

### 6. 风险
最大相关仓位数、跳过条件、与指数/油价/美债的冲突（用户在看美股或 BTC 时主动提 30 年债和油）。

最后写一行免责：研究预案，不是代客下单。

把填好的结果写入 `$RUN/analysis.json`，字段见 [references/schema.md](references/schema.md)。

## 概率怎么说（禁止装成回测）

`compute_vpa.py` 的分数 0–10：区间质量、事件序列、量能确认、高周期、盈亏比，各 0–2。

映射：`P ≈ 34 + 3×score` 到 `40 + 3.2×score`，封顶 72%。  
score < 6 → 不建议开仓，P 只作为“如果硬开”的参考。  
你只能在脚本带上 **±4 个点**（有 weekly 共振可加，事件被你否决须减），并说明加减原因。禁止无依据改到 80%+。

## 完成检查

- [ ] `ohlcv.csv` / `candidates.json` / `analysis.json` 都在 runs 目录
- [ ] 主方案有触发句、止损、T1、仓位公式、概率带
- [ ] A/B/C 三个预案都有
- [ ] 没有“现在市价买入”这种无触发的指令
- [ ] 没有伪造未发生的 Phase E

参考：[references/playbook.md](references/playbook.md) · [references/events.md](references/events.md) · [references/schema.md](references/schema.md) · [references/install.md](references/install.md)
