# Changelog

## 1.2.0

### Packaging revision r1 — 2026-09-05

- Removed two trailing Markdown hard-break spaces from `VALIDATION.md` that were rejected by `git diff --check`.
- Added repository-wide UTF-8 source-hygiene validation for trailing whitespace, CRLF line endings and missing final newlines.
- Added the same hygiene gate to deployment `--verify-only` and assigned a unique `deploy/wyckoff-vpa-1.2.0-r1` branch.
- Kept the frozen architecture, schemas, trading logic and fail-stop/no-source-edit deployment boundary unchanged.

- Added raw-evidence recomputation and `run_manifest.json` lineage binding for daily and weekly runs.
- Bound symbol, interval, as-of date, parameters and artifact hashes across the full pipeline.
- Added strict schema-first validation and fail-closed write-back on every validation failure.
- Replaced free-form trigger assertions with canonical bar-evaluated price and volume rules.
- Added full event identity/evidence-hash checks, complete review coverage and newest-pair freshness gates.
- Prevented retests that breach Spring/UT extremes from forming valid setup pairs.
- Bound range, stop, entry and targets to canonical structure; enforced 0.3–0.6 ATR stop buffers.
- Moved account equity, FX, actual tradable product and sizing inputs to a separate trusted risk config.
- Rebuilt product profiles and futures multipliers from resolved symbols instead of trusting Agent fields.
- Enforced 1% per-trade and 1.5% correlated-risk hard caps and immutable skip conditions.
- Required non-null executable sizing for directional recommendations.
- Bound chart rendering to `validation.json` and the hash of `analysis.validated.json`.
- Added 52 adversarial regression cases covering prior evidence, schema, trigger, sizing and rendering bypasses.
- Added split-adjustment idempotence and conservative continuous-gold roll-risk blocking.
- Blocked index-to-ETF/futures sizing from index price geometry; executable plans must rerun on the actual tradable symbol.
- Updated examples, documentation, CI and deployment workflow to the 1.2.0 contract.

## 1.1.0

- Split pre-LLM candidate evidence from final trade authorization.
- Added OHLC, bar-depth, volume-coverage and incomplete-bar quality gates.
- Added weekly input consumption, event-local context, threshold waves and an initial deterministic validator.
- Added contract-multiplier-aware sizing, one analysis schema, repaired chart mapping and baseline tests.
