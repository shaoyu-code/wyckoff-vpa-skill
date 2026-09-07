# Validation record — 1.2.0

Reviewed baseline: `3715a23047299bf4a4b248038bf6cb39f6ed46fc`.
Validation date: 2026-09-05.
Package revision: `r1`.
Status: `OFFLINE_VALIDATED / LIVE_SMOKE_PENDING / NOT_DEPLOYED`.

## Completed checks

- `python3 -m compileall -q scripts`: PASS on Python 3.13.5.
- `python3 scripts/test_compute_vpa.py`: PASS, **24/24**.
- `python3 scripts/test_adversarial.py`: PASS, **52/52**; executed in bounded chunks in this environment because a single long process is terminated by the host timeout.
- `python3 scripts/validate_examples.py`: PASS; stored schemas plus deterministic tradeable/no-trade end-to-end fixtures.
- `python3 scripts/validate_skill.py`: PASS after documentation and package-contract convergence.
- Source hygiene scan: PASS; no trailing spaces, trailing tabs, CRLF line endings or missing final newlines in tracked text sources.
- Staged-diff whitespace check (`git diff --cached --check`): PASS.
- Strict Draft 2020-12 analysis and risk-config schemas: PASS.
- Fail-closed write-back and chart hash binding: PASS.

## Covered adversarial failures

The suite blocks cross-symbol or stale weekly evidence, stale/future daily evidence, index-price sizing through ETF proxies, non-canonical compute parameters, raw/derived artifact tampering even after local manifest re-signing, incomplete event review, forged event identity, stale or contradictory setup selection, arbitrary ranges/targets, invalid trigger rules, stop buffers outside **0.3–0.6 ATR**, RR below 1.6, missing A/B/C fields, Agent-authored validation/probability/position fields, risk-limit relaxation, unsafe market-order language and invalid chart rendering.

## Trust boundary

The validator protects against accidental or adversarial edits to analysis and derived run artifacts by recomputing them from the local OHLCV source. SHA-256 lineage proves internal consistency, not market-data authenticity against a process that can replace every raw file and manifest. Deployment therefore treats the fetch step, filesystem permissions and operator-controlled bundle checksum as external trust anchors. This Skill remains a research and scenario tool, not an execution or custody system.

## Pending deployment checks

- Live Yahoo Finance smoke test for `BTC-USD`, `GC=F` and `SPY`, daily and weekly.
- GitHub Actions matrix on Python 3.10 and 3.12.
- Remote branch and `main` readback after CI.

The deployment workflow must push an isolated `deploy/*` branch first, wait for CI, re-check that `origin/main` still equals the reviewed baseline, and only then fast-forward `main`. Failure, skipped live smoke, missing authenticated `gh`, or baseline drift must prevent main promotion.

## Packaging correction r1

The original 1.2.0 delivery contained two Markdown hard-break spaces at the ends of `VALIDATION.md` lines 3 and 4. They were valid Markdown but correctly rejected by the deployment repository's `git diff --check` policy. Revision `r1` removes those spaces, adds a source-hygiene regression gate, and retains the fail-stop/no-source-edit deployment boundary.
