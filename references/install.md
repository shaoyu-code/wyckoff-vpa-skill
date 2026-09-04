# 安装

仓库根目录就是 Skill。不要只拷 SKILL.md，脚本必须在相对路径 `scripts/`。

## Claude Code / Claude Desktop

```bash
git clone https://github.com/shaoyu-code/wyckoff-vpa-skill.git
cp -R wyckoff-vpa-skill ~/.claude/skills/wyckoff-vpa
```

或在项目里：

```
your-project/.claude/skills/wyckoff-vpa/   ← 本仓库内容
```

依赖：Python 3.10+，`pandas`、`numpy`（fetch 用标准库 urllib，不必装 yfinance）。

```bash
pip install pandas numpy
```

## Codex / OpenAI agent

`agents/openai.yaml` 指向本 Skill。把仓库放到 Codex skills 目录，或在项目中引用 `SKILL.md`。

## OpenClaw

把本仓库放到 OpenClaw 的 skills 目录，名称用 `wyckoff-vpa`。Agent 只要能跑 `python3 scripts/*.py` 即可。

## 调用例子

```
用威科夫量价分析 BTC 日线，给开单建议、预案和概率。
用威科夫看黄金 GC=F，如果不能开仓就给等待条件。
分析 SPY，并对照 NDX 周线方向。
```
