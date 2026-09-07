# wyckoff-vpa-skill

`wyckoff-vpa` 是面向 Claude Code、Codex 与 OpenClaw 的威科夫量价分析 Skill。它分析美股、BTC 与黄金，输出做多、做空或空仓等待的研究预案，但不连接券商、不自动下单。

当前版本：**1.2.0（证据链硬化版）**。

## 输出合同

每次分析必须给出：

1. 明确建议：做多、做空或空仓等待；
2. 主方案：结构化价格/成交量触发、入场区、止损、T1/T2、RR、仓位；
3. 预案 A 反向、预案 B 作废、预案 C 持仓；
4. 单笔风险、相关风险桶、产品风险与跳空风险；
5. P(T1 先于止损) 的整数概率带；该概率是未校准结构区间，不是历史胜率。

## 核心安全边界

- 日线、周线、品种、参数和原始 OHLCV 通过 `run_manifest.json` 与 SHA-256 绑定。
- `candidates.json` 只是候选证据，不能直接授权交易。
- Spring/UT、SC/BC 当根不入场；只允许事件后的规则化回测。
- Agent 只能填写解释和方案提案，不能自行授权分数、概率、仓位、合约乘数或 `ready_to_execute`。
- 最终校验器会从原始行情重算候选、事件、触发、RR、周线、概率和仓位。
- 任一 Schema、证据、时效、风险或安全闸门失败，输出强制改写为 `no_trade / wait`。
- 图表只读取哈希绑定的 `analysis.validated.json + validation.json`，不能旁路校验器。
- `confluence < 6` 或 `RR(T1) < 1.6` 强制空仓。
- 概率为整数区间，最终封顶 72%。

## 固定管道

```text
fetch_ohlcv.py（日线/周线）
  → ohlcv.csv + meta.json
compute_vpa.py
  → vpa.csv + data_quality.json + structure_hint.json
  → candidates.json + run_manifest.json
build_plan.py
  → plan_skeleton.json（不可执行、无预填交易价位）
Agent
  → analysis.json（validation_status=unvalidated）
validate_analysis.py
  → validation.json + analysis.validated.json
make_chart.py（可选）
  → chart.html
```

## 快速运行

```bash
python3 -m pip install -r requirements.txt

SKILL_ROOT="$(pwd)"
SYMBOL="BTC-USD"
RUN="runs/btc-demo"
mkdir -p "$RUN/daily" "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/fetch_ohlcv.py" "$SYMBOL" \
  --days 750 --interval 1d --out-dir "$RUN/daily"
python3 "$SKILL_ROOT/scripts/fetch_ohlcv.py" "$SYMBOL" \
  --days 260 --interval 1wk --out-dir "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/compute_vpa.py" \
  --csv "$RUN/daily/ohlcv.csv" --out-dir "$RUN/daily"
python3 "$SKILL_ROOT/scripts/compute_vpa.py" \
  --csv "$RUN/weekly/ohlcv.csv" --out-dir "$RUN/weekly"

python3 "$SKILL_ROOT/scripts/build_plan.py" \
  --candidates "$RUN/daily/candidates.json" \
  --weekly-candidates "$RUN/weekly/candidates.json" \
  --symbol "$SYMBOL" --out-dir "$RUN/daily"
```

复制 `$RUN/daily/plan_skeleton.json` 为 `$RUN/daily/analysis.json`，按 `SKILL.md` 仅填写允许的字段。非空仓方案还必须提供独立的 `risk_config.json`，格式见 `references/risk-config.schema.json` 与 `examples/risk-config.tradeable.example.json`。

```bash
python3 "$SKILL_ROOT/scripts/validate_analysis.py" \
  --analysis "$RUN/daily/analysis.json" \
  --run-dir "$RUN/daily" \
  --weekly-dir "$RUN/weekly" \
  --risk-config "$RUN/daily/risk_config.json" \
  --out-dir "$RUN/daily" \
  --write-back
```

只有下列条件同时成立时，才允许向用户输出方向性开仓建议：

```text
validation.json.valid == true
validation.json.trade_gates_pass == true
analysis.validated.json.validation_status == "validated_trade"
```

无交易方案可以不提供风险配置，但仍必须通过 Schema 与证据校验并生成 `validated_no_trade`。任何失败都会生成安全的 `analysis.validated.json`；使用 `--write-back` 时，原 `analysis.json` 也会被原子替换为安全终态。

## 本地验证

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m compileall -q scripts
python3 scripts/test_compute_vpa.py
python3 scripts/test_adversarial.py
python3 scripts/validate_examples.py
python3 scripts/validate_skill.py
```

联网环境另运行：

```bash
python3 scripts/smoke_fetch.py
```

该 smoke test 检查 BTC-USD、GC=F、SPY 的日线与周线公开行情链路。安装路径见 `references/install.md`；唯一输出合同见 `references/schema.md` 和 `references/analysis.schema.json`。
