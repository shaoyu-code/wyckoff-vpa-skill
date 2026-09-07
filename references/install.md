# 安装与部署

仓库根目录就是完整 Skill。目录名必须为 `wyckoff-vpa`，不能只复制 `SKILL.md`。

运行要求：Python 3.10+、可执行 `python3`、可写 `runs/`、安装 `requirements.txt`。公开行情抓取不需要 API Key。

## Claude Code / Claude Desktop

```bash
git clone https://github.com/shaoyu-code/wyckoff-vpa-skill.git ~/.claude/skills/wyckoff-vpa
python3 -m pip install -r ~/.claude/skills/wyckoff-vpa/requirements.txt
```

项目级目录也可使用：

```text
<project>/.claude/skills/wyckoff-vpa/
```

## Codex / OpenAI Agent

用户级：

```bash
git clone https://github.com/shaoyu-code/wyckoff-vpa-skill.git ~/.agents/skills/wyckoff-vpa
```

项目级：

```text
<repo>/.agents/skills/wyckoff-vpa/
```

`agents/openai.yaml` 只提供界面元数据，不替代 `SKILL.md`。

## OpenClaw

按部署方式放入以下任一位置，目录名保持 `wyckoff-vpa`：

```text
<workspace>/skills/wyckoff-vpa
<workspace>/.agents/skills/wyckoff-vpa
~/.agents/skills/wyckoff-vpa
~/.openclaw/skills/wyckoff-vpa
```

## 离线验收

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m compileall -q scripts
python3 scripts/test_compute_vpa.py
python3 scripts/test_adversarial.py
python3 scripts/validate_examples.py
python3 scripts/validate_skill.py
```

## 联网验收

```bash
python3 scripts/smoke_fetch.py
```

必须核验 BTC-USD、GC=F、SPY 的日线与周线。完整发布还需 GitHub Actions 的 Python 3.10/3.12 矩阵通过。
