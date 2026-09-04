# analysis.json

在 `plan_skeleton.json` 上填完后保存为 `analysis.json`。

```json
{
  "instrument": { "symbol": "BTC-USD", "last": 80120, "asof": "2026-09-03", "interval": "1d" },
  "structure": {
    "cycle": "range",
    "phase": "C",
    "has_trading_range": true,
    "range_high": 81500,
    "range_low": 76000,
    "events_confirmed": [
      { "date": "2026-08-21", "type": "UT", "price": 79500, "why": "冲到供给区收回，量未持续" }
    ],
    "events_rejected": [
      { "date": "2026-09-03", "type": "SOS", "why": "只有盘中刺穿 8 万，没有收盘站稳" }
    ],
    "narrative": "8 万是阻力带不是已确认支撑。"
  },
  "bias": "short_wait_or_stand_aside",
  "confluence": { "confluence_0_10": 6, "parts": {}, "p_reach_t1_before_stop": { "low": 52, "high": 59 } },
  "main_plan": {
    "action": "propose",
    "direction": "short",
    "setup": "UT_fail_test",
    "trigger": "日线收在 80000 下方且回抽 80000 时 vol_rel < 1.0",
    "entry_zone": [79200, 80000],
    "stop": 81600,
    "t1": 77000,
    "t2": 75600,
    "position_risk_pct": 0.75,
    "rr_t1": 1.9,
    "p_t1_before_stop": { "low": 52, "high": 59 }
  },
  "alt_plan": { "direction": "long", "trigger": "收盘站稳 81500 且回踩 80000 缩量守住", "stop": 75600, "t1": 85000 },
  "invalidation": { "hard_stop_beyond": 81600, "time_stop_bars": 8 },
  "manage": { "at_t1": "平 50%，剩余移到成本" },
  "risk": { "max_risk_pct_equity": 0.75, "skip_if": [] }
}
```

`bias` 取值：`long` | `short` | `no_trade` | `long_wait` | `short_wait`。
`action` 取值：`propose` | `wait` | `stand_aside`。
