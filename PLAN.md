# 当前实现与真实管道

版本 1.2.0 只保留一条生产管道：

```text
fetch_ohlcv.py
  → ohlcv.csv, meta.json
compute_vpa.py
  → vpa.csv, data_quality.json, structure_hint.json
  → candidates.json, run_manifest.json
build_plan.py
  → plan_skeleton.json
Agent
  → analysis.json（只提出方案，不能自我授权）
validate_analysis.py
  → validation.json, analysis.validated.json
make_chart.py
  → chart.html（可选，只接受已验证终态）
```

开单设置白名单：`spring_retest`、`ut_retest`、`sos_lps`、`sow_lpsy`。

最终非空仓方案必须由校验器从原始 OHLCV 重算并同时满足：完整候选审查、最新有效事件对、结构化触发、周线绑定、0.3–0.6 ATR 止损缓冲、实际 RR≥1.6、合流≥6、可信风险配置和完整 A/B/C 预案。

任何失败均生成 fail-closed 的 `no_trade / wait` 输出；不存在“验证失败但仍保留 LONG/SHORT”的兼容路径。
