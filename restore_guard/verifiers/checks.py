"""The expectation mini-language shared by every query-based verifier.

`sqlite`, `postgres` and `mysql` all express the same idea — run a query, take
the first cell, compare it against a bound — so the comparison and its
validation live here once. When this logic was copied per verifier, sqlite's
copy quietly lost `expect_contains`; one implementation makes that impossible.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import ConfigError

#: Recognised expectation keys. Anything else in a check dict is a typo, and a
#: silently ignored typo here means a check that always passes.
EXPECTATIONS = ("expect_min", "expect_max", "expect_equals", "expect_contains")


def validate_check_specs(specs: Any, where: str) -> list[dict[str, Any]]:
    """Validate a list of ``{sql: ..., expect_*: ...}`` mappings.

    Returns the list so callers can assign it; raises ConfigError otherwise.
    """
    if specs in (None, "", []):
        return []
    if not isinstance(specs, list):
        raise ConfigError(f"{where} must be a list of checks")

    for position, spec in enumerate(specs):
        at = f"{where}[{position}]"
        if not isinstance(spec, dict):
            raise ConfigError(f"{at} must be a mapping")
        if not spec.get("sql"):
            raise ConfigError(f"{at} needs 'sql'")
        # A check with no expectation is deliberately allowed: it still proves
        # the query runs at all, which is a real smoke test against a restored
        # schema. A *misspelled* expectation is not — `expect_minimum: 5` would
        # silently never fail, so unknown keys are rejected.
        unknown = [
            key
            for key in spec
            if key not in EXPECTATIONS and key not in ("sql", "name", "timeout")
        ]
        if unknown:
            raise ConfigError(
                f"{at}: unknown key(s) {sorted(unknown)}; "
                f"valid: sql, name, timeout, {', '.join(EXPECTATIONS)}"
            )
    return list(specs)


def compare(label: str, value: Any, spec: dict[str, Any]) -> str | None:
    """Compare one query result against its expectations.

    Returns a human-readable problem description, or None when the value is
    acceptable.
    """
    if "expect_min" in spec or "expect_max" in spec:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return f"{label}: expected a number, got {value!r}"
        minimum = spec.get("expect_min")
        maximum = spec.get("expect_max")
        if minimum is not None and numeric < float(minimum):
            return f"{label}: {_fmt(numeric)} < expected minimum {minimum}"
        if maximum is not None and numeric > float(maximum):
            return f"{label}: {_fmt(numeric)} > expected maximum {maximum}"
    if "expect_equals" in spec and str(value) != str(spec["expect_equals"]):
        return f"{label}: got {value!r}, expected {spec['expect_equals']!r}"
    if "expect_contains" in spec and str(spec["expect_contains"]) not in str(value):
        return f"{label}: {value!r} does not contain {spec['expect_contains']!r}"
    return None


def label_for(spec: dict[str, Any]) -> str:
    return str(spec.get("name") or str(spec["sql"])[:60])


def run_checks(
    specs: list[dict[str, Any]],
    query: Callable[[dict[str, Any]], Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run every check through ``query`` and collect results and problems.

    ``query`` takes the check spec and returns the scalar to compare, or raises
    ``QueryFailed`` when the query itself could not run.
    """
    results: list[dict[str, Any]] = []
    problems: list[str] = []

    for spec in specs:
        label = label_for(spec)
        try:
            value = query(spec)
        except QueryFailed as exc:
            outcome = {"name": label, "value": None, "problem": f"{label}: {exc}"}
        else:
            outcome = {"name": label, "value": value, "problem": compare(label, value, spec)}
        results.append(outcome)
        if outcome["problem"]:
            problems.append(outcome["problem"])

    return results, problems


class QueryFailed(RuntimeError):
    """The query could not be executed (bad SQL, missing table, dead server)."""


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"
