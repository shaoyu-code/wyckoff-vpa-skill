# wyckoff-vpa-skill

威科夫量价 Skill：先用脚本算相对成交量、VSA 残差、Weis 波，再让 Agent 输出 **开单建议 + 主方案 + 反向预案 + 作废条件 + 仓位风险 + 到达 T1 的概率带**。

适用：美股、BTC、黄金（Yahoo 代码或别名 `btc` / `gold` / `spx`）。  
不是自动下单、不是回测过的圣杯。概率是合流分数映射，封顶 72%。

## 和现成仓库的关系（改造来源）

| 来源 | 拿什么 | 不拿什么 |
|------|--------|----------|
| [RoacherM/StockMastar](https://github.com/RoacherM/StockMastar) `wyckoff-analyst` | 先落盘再分析、禁止凑齐 Phase A–E、CSV 不进上下文 | A 股 MCP、把 LLM 贴的 Spring 当信号 |
| [YoungCan-Wang/wyckoff_skill](https://github.com/YoungCan-Wang/wyckoff_skill) | 固定输出合同、能力降级（数据不够就说不够） | Tushare/TickFlow 账号、持仓 CLI 全家桶 |
| [neurotrader888/VSAIndicator](https://github.com/neurotrader888/VSAIndicator) | 努力 vs 结果用「振幅对成交量回归残差」这个想法 | 原文件；这里是独立实现 |
| 本仓库新增 | 开单建议 / 三套预案 / 仓位公式 / 概率带 / 美股·BTC·黄金数据 | 自动喊「市价买」 |

## 快速跑

```bash
pip install pandas numpy
python3 scripts/fetch_ohlcv.py btc --days 750 --out-dir runs/btc
python3 scripts/compute_vpa.py --csv runs/btc/ohlcv.csv --out-dir runs/btc
python3 scripts/build_plan.py --candidates runs/btc/candidates.json --symbol BTC-USD --out-dir runs/btc
```

然后把 `SKILL.md` 交给 Claude / Codex / OpenClaw，让它读 `candidates.json` 和 `plan_skeleton.json` 写出 `analysis.json`。

安装见 [references/install.md](references/install.md)。输出字段见 [references/schema.md](references/schema.md)。

## 概率

`confluence` 0–10 分（区间、事件序列、量能、高周期、盈亏比各 0–2）。  
`P(先到 T1) ≈ 34+3s ~ 40+3.2s`，封顶 72%。低于 6 分建议空仓。这不是历史胜率。
