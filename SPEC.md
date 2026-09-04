# 给 GPT Pro 的改造说明书（若要继续改）

本仓库已经按这个 SPEC 落地。若要扩展，遵守下面的边界。

## 产品目标

用户**不自己拍板开单**。Skill 必须给出：

1. 开单建议（做多 / 做空 / 空仓等待）
2. 主方案（触发、入场区、止损、T1/T2、仓位）
3. 预案 A 反向、预案 B 作废、预案 C 持仓
4. 风险把控（单笔 %、相关仓、新闻）
5. 概率带 P(先到 T1 再到止损)

禁止：无触发的市价指令；把规则旗标直接当 Spring；宣称回测胜率。

## 架构

```
fetch_ohlcv.py   → ohlcv.csv, meta.json     数字，零 LLM
compute_vpa.py   → vpa.csv, candidates.json 数字，零 LLM
build_plan.py    → plan_skeleton.json       把分数填进预案骨架
SKILL.md + LLM   → analysis.json + 中文报告 只解释数字，不编造 K 线
```

## 不要做的扩展

- 不要接 YoungCan 的 CLI 账号体系。
- 不要做全市场 Spring 扫描当自动交易。
- 不要把 P 改成点估计（52.7%）。保持带。
- 不要为了图表引入必须登录的前端。

## 可以做的扩展

- 用 Polygon/Binance 替换 Yahoo（保持 CSV schema）。
- 把用户成交日志积累成自己的校准表，替换 34+3s 映射。
- 增加 4h BTC 周期，但日线结构仍是主图。
- 把 `analysis.json` 画成静态 HTML（只读 runs/ 文件）。
