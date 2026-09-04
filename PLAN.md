# 改造方案（已按「给建议」落地）

用户纠正：不是自己闷头开单，而是 Agent 必须给出**开单建议、预案、方案、风险把控、概率**。人只负责下单按钮。

## 用哪些项目、怎么改

| 来源 | 拿走 | 改掉 |
|------|------|------|
| StockMastar wyckoff-analyst | CSV 落盘、analysis.json、禁止灌全量 K 线、相位不凑齐 | 去掉「禁止输出方案」；数据源换 yfinance |
| YoungCan-Wang wyckoff_skill | 固定输出顺序、数据审计、降级 | 去掉账号/Tushare/自动持仓 CLI |
| VSA 思路 | 振幅~成交量残差 | 不当买卖点 |
| Weis 思路 | 波段累计成交 | 不当相位器 |
| ChartNagari | 无 | 自动 Spring 推送当入场 |

本仓库新增（现成仓库都没有的）：`plan.json` = 主方案 + A/B/C 预案 + 风控 + 条件概率区间。

## 管道

```
fetch_ohlcv.py  → ohlcv.csv
compute_vpa.py  → vpa.csv, candidates.json, tail.csv, vpa_summary.json
模型             → analysis.json + plan.json
make_chart.py   → chart.html（可选）
```

开单白名单：`spring_retest` | `lps` | `ut_retest`。  
清单六项不全 true → `bias=flat`，主方案就是空仓等待，仍然要写概率（空仓正确的概率）。
