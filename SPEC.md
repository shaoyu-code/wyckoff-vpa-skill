# SPEC — wyckoff-vpa 1.2.0

## 产品目标

Skill 在不自动下单的前提下，必须输出：

1. 做多、做空或空仓等待；
2. 主方案：结构化触发、入场区、止损、T1/T2、RR、仓位；
3. 预案 A 反向、预案 B 作废、预案 C 持仓；
4. 单笔风险、相关风险桶、产品与事件风险；
5. P(T1 先于止损) 的未校准整数概率带。

## 权威顺序

1. `SKILL.md`：Agent 业务合同；
2. `references/analysis.schema.json`：`analysis.json` 机器合同；
3. `references/risk-config.schema.json`：可信风险输入合同；
4. `references/schema.md`：字段语义说明。

其他文档和 examples 不得创建第二套输出结构。

## 固定架构

```text
fetch daily/weekly
→ compute immutable candidate evidence + run manifest
→ build non-executable skeleton
→ Agent proposes analysis
→ validator recomputes evidence and applies trusted risk config
→ validated output / fail-closed no-trade
```

`compute_vpa.py` 不确认 Phase，也不授权交易。Agent 不读取整份 CSV。`validate_analysis.py` 必须从原始行情重算候选，核对完整事件身份、日周线时效、真实触发、实际 RR、概率与仓位。

## 信任边界

可信：

- `ohlcv.csv` 与 `meta.json` 的实际内容；
- 由校验器重新计算得到的候选与特征；
- `run_manifest.json` 的文件哈希和运行参数绑定；
- 独立 `risk_config.json`，但仍需通过 Schema、品种画像和硬上限校验。

不可信：

- Agent 自填的事件日期、事件类型、量价方向、触发结果、最终分数、概率、乘数、仓位数量或 `ready_to_execute`；
- 未通过 `validation.json` 绑定的图表或报告；
- 仅凭包内可被一起篡改的 Manifest 所作的来源声明。

## 不可修改的业务边界

- 不接 YoungCan CLI、A 股漏斗、Tushare、账户系统、券商或交易所自动下单。
- 不引入必须登录的数据源。
- 不把候选事件当确认事件。
- 不在 Spring/UT/SC/BC 当根入场。
- 不强行补齐 Phase A–E。
- 不输出点估计或声称历史胜率。
- 不删除空仓等待分支。
- 不对 `GC=F` 连续代码直接输出合约张数。
- 不允许验证失败后保留做多/做空几何、仓位或概率。
- 不允许图表、examples、部署脚本绕过最终验证器。

## 发布闸门

发布候选必须同时通过：Python 编译、基础回归、对抗回归、examples、Skill 合同、包解压复验。联网 Yahoo smoke 和 GitHub Actions 未通过前，只能标记为 `OFFLINE_VALIDATED / READY_FOR_DEPLOYMENT_VALIDATION`，不能标记为完整发布。
