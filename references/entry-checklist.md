# 清单：六项决定能不能给「非空仓」主方案

用户要的是建议，不是沉默。但建议分两种：

- 六项全 true → 主方案可以是 long/short，并给入场区
- 任一 false → 主方案必须是 **空仓等待**，并写清缺哪项、什么条件补齐后才升级

| key | true 的标准 |
|-----|-------------|
| index_aligned | 股票有 SPY/QQQ 同向；黄金看自身周线；BTC 不拿纳指当大盘 |
| range_drawn | 上沿、下沿、高潮、AR 能复现 |
| event_complete | 是回测，不是刺破当根 |
| effort_agrees | 最近同向波段量不弱于反向；没有持续努力无结果对着做 |
| risk_defined | 止损在事件极值外，目标 ≥1.6R |
| cause_enough | 日线区间大约 ≥15 根，高度配得上手续费和滑点 |

给用户的建议句式：

> 主方案：若 {trigger}，在 {entry_zone} 执行，止损 {stop}，目标 {targets}，单笔风险 {pct}%。  
> 预案 B：{invalidation} 则停手。  
> 条件概率 {p_low}–{p_high}（结构评分，非回测）。  
> 未完成清单：{false keys}。
