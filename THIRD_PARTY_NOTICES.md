# Third-party notices

本仓库为独立实现，不是以下项目的 fork。

## neurotrader888/VSAIndicator

- Source: `https://github.com/neurotrader888/VSAIndicator`
- Reviewed source blob: `vsa.py@f6670a3d66194188fe87289f3495fd9518c3c884` (2026-09-04)
- Concept referenced: normalized price range regressed on normalized volume; residual used as effort-versus-result evidence.
- License: MIT License, Copyright (c) 2023 neurotrader888.
- This repository reimplements the calculation with NumPy/Pandas, different interfaces, quality gates, threshold waves, candidate-event logic, plan validation and no copied source file.

## RoacherM/StockMastar — wyckoff-analyst

- Source: `https://github.com/RoacherM/StockMastar`
- Reviewed Skill blob: `.claude/skills/wyckoff-analyst/SKILL.md@a6c7ea73fcc809c1c64ed13251b9dd7c90bcf671` (2026-09-04)
- Ideas referenced: persist artifacts before analysis, keep full CSV out of model context, do not force Phase A–E.
- No source file from that project is included here.

## YoungCan-Wang/wyckoff_skill / WyckoffTradingAgent

- Sources: `https://github.com/YoungCan-Wang/wyckoff_skill` and related AGPL project.
- Reviewed Skill blob: `SKILL.md@9bccf4bda8c578920590ef127b49cdac6a79aa3c` (2026-09-04).
- Ideas referenced: fixed output contract and explicit degradation when capabilities or data are insufficient.
- No AGPL Python/CLI/account/data-source implementation is included. This repository does not use Tushare, TickFlow, authentication, portfolio CLI or the A-share funnel.

Project license remains MIT. This notice records provenance and does not replace the licenses of the referenced projects.
