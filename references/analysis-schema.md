# analysis.json

图表和复盘都读这个文件。`setup.action` 只能是 WAIT / WATCH / INVALID。

```json
{
  "symbol": { "code": "BTC-USD", "name": "Bitcoin", "timeframe": "1d", "benchmark": "BTC-USD" },
  "asof": "2026-09-04",
  "quote": "8 万还是阻力，不是已经换手完成的地板。",
  "market_cycle": "range",
  "index_context": "BTC 自身为基准。日线在 7.6–8.1 万箱体，尚未确认 SOS。",
  "phases": [
    { "name": "Phase B", "start": "2026-06-15", "end": null, "description": "箱体换手，未完成测验" }
  ],
  "zones": [
    {
      "type": "range",
      "label": "7.6–8.0 万争议区",
      "top": 80000,
      "bottom": 76000,
      "start": "2026-06-22",
      "end": "2026-09-04"
    }
  ],
  "events": [
    {
      "date": "2026-08-21",
      "price": 79500,
      "type": "UT",
      "label": "UT 候选：刺到 7.95 万后收回，量未转化为站稳"
    }
  ],
  "vpa_notes": {
    "wave_bias": "balanced",
    "last_flag": "climax",
    "effort": "刺穿 8 万那根需要对照 rvol；没有收盘站稳不算 SOS"
  },
  "setup": {
    "bias": "none",
    "action": "WAIT",
    "trigger": "若收盘站稳 81500 后缩量回踩 80000 不破，才像 LPS；或跌回 77000 缩量守住才像测试",
    "invalidation": "日线收在 76000 下方",
    "targets": [85000, 87000],
    "checklist": {
      "index_aligned": true,
      "range_drawn": true,
      "event_complete": false,
      "effort_agrees": false,
      "risk_defined": true,
      "cause_enough": true
    }
  },
  "summary": "结构有了，触发没有。8 万仍是供给区。用户等回测，不追针。"
}
```

`zones.type`：`accumulation` | `distribution` | `range`（分不清就用 range，不要猜主力意图）。

`market_cycle`：`accumulation` | `markup` | `distribution` | `markdown` | `range` | `unclear`。
