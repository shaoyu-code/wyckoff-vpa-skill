#!/usr/bin/env python3
"""Dependency-light structural validation for the packaged Agent Skill."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.2.0"
REQUIRED = {
    "VERSION",
    "SKILL.md",
    "README.md",
    "SPEC.md",
    "PLAN.md",
    "VALIDATION.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "agents/openai.yaml",
    ".github/workflows/validate.yml",
    "scripts/pipeline_common.py",
    "scripts/fetch_ohlcv.py",
    "scripts/compute_vpa.py",
    "scripts/build_plan.py",
    "scripts/validate_analysis.py",
    "scripts/make_chart.py",
    "scripts/test_compute_vpa.py",
    "scripts/test_adversarial.py",
    "scripts/validate_examples.py",
    "scripts/smoke_fetch.py",
    "references/schema.md",
    "references/analysis.schema.json",
    "references/risk-config.schema.json",
    "references/events.md",
    "references/probability.md",
    "references/risk.md",
    "examples/analysis.tradeable.input.example.json",
    "examples/analysis.tradeable.example.json",
    "examples/analysis.no_trade.input.example.json",
    "examples/analysis.no_trade.example.json",
    "examples/risk-config.tradeable.example.json",
}
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".txt", ".html"}
TEXT_NAMES = {"VERSION", ".gitignore", "requirements.txt", "requirements-dev.txt"}


STALE = {
    "references/analysis-schema.md",
    "examples/analysis.example.json",
    "examples/plan.example.json",
    "examples/plan.sample.json",
    "examples/BTC-USD.candidates.json",
    "examples/BTC-USD.plan_skeleton.json",
}


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def validate_text_hygiene(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if b"\r\n" in data:
            errors.append(f"{relative}: CRLF line endings are not allowed")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{relative}: tracked text source is not valid UTF-8")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.endswith((" ", "\t")):
                errors.append(f"{relative}:{line_number}: trailing whitespace")
        if data and not data.endswith(b"\n"):
            errors.append(f"{relative}: missing final newline")
    return errors


def main() -> int:
    errors: list[str] = []
    errors.extend(validate_text_hygiene(ROOT))
    for relative in sorted(REQUIRED):
        if not (ROOT / relative).exists():
            errors.append(f"missing required path: {relative}")
    for relative in sorted(STALE):
        if (ROOT / relative).exists():
            errors.append(f"stale incompatible path remains: {relative}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else ""
    if version != EXPECTED_VERSION:
        errors.append(f"VERSION must be {EXPECTED_VERSION}, got {version!r}")

    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\n") or "\n---\n" not in skill[4:]:
        errors.append("SKILL.md frontmatter is missing or malformed")
    else:
        frontmatter = skill.split("\n---\n", 1)[0][4:]
        fields: dict[str, str] = {}
        for line in frontmatter.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            errors.append("frontmatter name must use lowercase letters, numbers, and hyphens")
        if name != "wyckoff-vpa":
            errors.append("frontmatter name must be wyckoff-vpa")
        if not 1 <= len(description) <= 1024:
            errors.append("frontmatter description must contain 1..1024 characters")

    required_fragments = [
        "scripts/fetch_ohlcv.py",
        "scripts/compute_vpa.py",
        "scripts/build_plan.py",
        "--weekly-candidates",
        "scripts/validate_analysis.py",
        "--run-dir",
        "--weekly-dir",
        "--risk-config",
        "analysis.validated.json",
        "trade_gates_pass",
        "run_manifest.json",
        "risk_config.json",
        "0.3–0.6 ATR",
        "空仓等待",
    ]
    for fragment in required_fragments:
        if fragment not in skill:
            errors.append(f"SKILL.md is missing workflow fragment: {fragment}")

    prohibited_fragments = [
        "price_condition",
        "confirmed_bar",
        "effort_result_alignment",
        "历史 Spring 胜率",
    ]
    for fragment in prohibited_fragments:
        if fragment in skill:
            errors.append(f"SKILL.md contains stale or unsafe contract fragment: {fragment}")

    try:
        analysis_schema = load_json(ROOT / "references" / "analysis.schema.json")
        risk_schema = load_json(ROOT / "references" / "risk-config.schema.json")
        if analysis_schema.get("properties", {}).get("schema_version", {}).get("const") != EXPECTED_VERSION:
            errors.append("analysis schema version does not match VERSION")
        if risk_schema.get("properties", {}).get("schema_version", {}).get("const") != EXPECTED_VERSION:
            errors.append("risk config schema version does not match VERSION")
        if analysis_schema.get("additionalProperties") is not False:
            errors.append("analysis schema root must use additionalProperties=false")
        if risk_schema.get("additionalProperties") is not False:
            errors.append("risk config schema root must use additionalProperties=false")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"schema unreadable: {exc}")

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    if "yfinance" in requirements:
        errors.append("unused yfinance dependency must not be present")
    for required_dependency in ["pandas", "numpy", "jsonschema"]:
        if required_dependency not in requirements:
            errors.append(f"requirements.txt missing {required_dependency}")

    openai_yaml = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    if re.search(r"(?m)^skill\s*:", openai_yaml):
        errors.append("agents/openai.yaml contains unsupported skill: indirection")
    if "allow_implicit_invocation: false" not in openai_yaml:
        errors.append("implicit invocation must remain disabled for the trading-research Skill")

    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for command in [
        "python scripts/test_compute_vpa.py",
        "python scripts/test_adversarial.py",
        "python scripts/validate_examples.py",
        "python scripts/validate_skill.py",
    ]:
        if command not in workflow:
            errors.append(f"CI is missing required command: {command}")
    if '"deploy/**"' not in workflow and "'deploy/**'" not in workflow:
        errors.append("CI must validate isolated deploy/* branches before main promotion")

    for doc in ["README.md", "SPEC.md", "SKILL.md", "VALIDATION.md", "references/schema.md"]:
        text = (ROOT / doc).read_text(encoding="utf-8")
        if "1.1.0" in text:
            errors.append(f"{doc} still claims the 1.1.0 contract")

    if errors:
        print("SKILL_INVALID")
        for error in errors:
            print(f"- {error}")
        return 2
    print(f"SKILL_VALID name=wyckoff-vpa version={EXPECTED_VERSION} required_paths={len(REQUIRED)} stale_paths=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
