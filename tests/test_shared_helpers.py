"""The helpers that replaced per-module copies.

These exist because the duplicated versions had already drifted apart: the
sqlite verifier's private copy of the comparison logic silently lacked
`expect_contains`. One implementation, one test suite, no drift.
"""

from datetime import datetime, timezone

import pytest

from restore_guard.config import ConfigError
from restore_guard.util import (
    PathEscape,
    as_list,
    ensure_within,
    parse_iso_time,
    shell_quote,
)
from restore_guard.verifiers.checks import compare, run_checks, validate_check_specs
from restore_guard.verifiers.checks import QueryFailed


# -- as_list ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, []),
        ("", []),
        ([], []),
        ("one", ["one"]),
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
        (0, [0]),
        (False, [False]),
    ],
)
def test_as_list_normalises_yaml_scalars(value, expected):
    assert as_list(value) == expected


def test_as_list_copies_rather_than_aliasing():
    original = ["a"]
    result = as_list(original)
    result.append("b")
    assert original == ["a"]


# -- shell_quote ------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("plain", "'plain'"),
        ("with space", "'with space'"),
        ("it's", "'it'\\''s'"),
        ("; rm -rf /", "'; rm -rf /'"),
    ],
)
def test_shell_quote(value, expected):
    assert shell_quote(value) == expected


def test_shell_quote_neutralises_injection():
    """The payload must come back as data, not be executed by the shell."""
    import subprocess

    payload = "'; echo executed; '"
    out = subprocess.run(
        ["sh", "-c", f"printf %s {shell_quote(payload)}"], capture_output=True, text=True
    )
    # Exact round-trip proves the embedded command was never run: had it been,
    # stdout would carry "executed\n" on its own line instead.
    assert out.stdout == payload
    assert out.stdout.splitlines() == [payload]


# -- parse_iso_time ---------------------------------------------------


EPOCH_2026_08_08_10Z = datetime(2026, 8, 8, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def test_parse_iso_time_handles_zulu():
    assert parse_iso_time("2026-08-08T10:00:00Z") == pytest.approx(EPOCH_2026_08_08_10Z)


def test_parse_iso_time_truncates_restic_nanoseconds():
    """restic emits 9 fractional digits, which fromisoformat rejects."""
    value = parse_iso_time("2026-08-08T10:00:00.123456789Z")
    assert value == pytest.approx(EPOCH_2026_08_08_10Z + 0.123456, abs=0.001)


def test_parse_iso_time_handles_offsets():
    utc = parse_iso_time("2026-08-08T10:00:00+00:00")
    plus_two = parse_iso_time("2026-08-08T12:00:00+02:00")
    assert utc == plus_two


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45T99:99:99Z"])
def test_parse_iso_time_returns_none_rather_than_raising(value):
    # A snapshot with an unreadable timestamp must still be listable.
    assert parse_iso_time(value) is None


# -- ensure_within ----------------------------------------------------


def test_ensure_within_allows_paths_inside(tmp_path):
    (tmp_path / "sub").mkdir()
    assert ensure_within(tmp_path, "sub") == (tmp_path / "sub").resolve()


def test_ensure_within_allows_the_root_itself(tmp_path):
    assert ensure_within(tmp_path, ".") == tmp_path.resolve()


@pytest.mark.parametrize("escape", ["../outside", "../../etc/passwd", "sub/../../.."])
def test_ensure_within_rejects_traversal(tmp_path, escape):
    with pytest.raises(PathEscape):
        ensure_within(tmp_path, escape)


def test_ensure_within_rejects_symlink_pointing_out(tmp_path):
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathEscape):
        ensure_within(root, "link")


def test_ensure_within_rejects_prefix_lookalike(tmp_path):
    """/restore-evil must not pass because it starts with /restore."""
    root = tmp_path / "restore"
    root.mkdir()
    (tmp_path / "restore-evil").mkdir()
    with pytest.raises(PathEscape):
        ensure_within(root, "../restore-evil")


# -- check specs ------------------------------------------------------


def test_validate_accepts_a_well_formed_check():
    specs = validate_check_specs(
        [{"sql": "SELECT 1", "expect_min": 1}], "verify.postgres.checks"
    )
    assert len(specs) == 1


def test_validate_allows_a_check_with_no_expectation():
    """Running at all is a real check against a restored schema."""
    assert validate_check_specs([{"sql": "SELECT 1"}], "where") == [{"sql": "SELECT 1"}]


def test_validate_rejects_missing_sql():
    with pytest.raises(ConfigError, match="needs 'sql'"):
        validate_check_specs([{"expect_min": 1}], "verify.mysql.checks")


def test_validate_rejects_misspelled_expectation():
    """`expect_minimum` would silently never fail — that is the trap."""
    with pytest.raises(ConfigError, match="unknown key"):
        validate_check_specs([{"sql": "SELECT 1", "expect_minimum": 5}], "where")


def test_validate_rejects_non_list():
    with pytest.raises(ConfigError, match="must be a list"):
        validate_check_specs({"sql": "SELECT 1"}, "where")


def test_validate_treats_absent_as_empty():
    assert validate_check_specs(None, "where") == []


# -- compare ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,spec",
    [
        (5, {"expect_min": 1}),
        (5, {"expect_max": 10}),
        (5, {"expect_min": 5, "expect_max": 5}),
        ("ok", {"expect_equals": "ok"}),
        (1, {"expect_equals": "1"}),
        ("PostgreSQL 16.2", {"expect_contains": "16."}),
        (0, {}),
    ],
)
def test_compare_accepts(value, spec):
    assert compare("check", value, spec) is None


@pytest.mark.parametrize(
    "value,spec,fragment",
    [
        (0, {"expect_min": 1}, "< expected minimum"),
        (99, {"expect_max": 10}, "> expected maximum"),
        ("nope", {"expect_equals": "ok"}, "expected 'ok'"),
        ("MariaDB", {"expect_contains": "Postgres"}, "does not contain"),
        ("abc", {"expect_min": 1}, "expected a number"),
        (None, {"expect_min": 1}, "expected a number"),
    ],
)
def test_compare_rejects(value, spec, fragment):
    problem = compare("check", value, spec)
    assert problem is not None
    assert fragment in problem


def test_compare_formats_integers_without_decimal_point():
    assert "2 < expected minimum 500" in compare("users", 2, {"expect_min": 500})


def test_expect_contains_is_available_to_every_engine():
    """The regression this refactor fixes: sqlite's copy had lost this key."""
    assert compare("v", "PostgreSQL 16", {"expect_contains": "16"}) is None
    assert compare("v", "PostgreSQL 16", {"expect_contains": "17"}) is not None


# -- run_checks -------------------------------------------------------


def test_run_checks_collects_results_and_problems():
    specs = [
        {"name": "rows", "sql": "SELECT count(*)", "expect_min": 10},
        {"name": "version", "sql": "SELECT version()", "expect_contains": "16"},
    ]
    values = {"SELECT count(*)": 3, "SELECT version()": "PostgreSQL 16.2"}

    results, problems = run_checks(specs, lambda spec: values[spec["sql"]])

    assert [r["name"] for r in results] == ["rows", "version"]
    assert len(problems) == 1
    assert "rows: 3 < expected minimum 10" in problems[0]


def test_run_checks_reports_a_failed_query_as_a_problem():
    def explode(spec):
        raise QueryFailed('relation "users" does not exist')

    results, problems = run_checks([{"name": "users", "sql": "SELECT 1"}], explode)

    assert results[0]["value"] is None
    assert problems == ['users: relation "users" does not exist']


def test_run_checks_labels_from_sql_when_unnamed():
    results, _ = run_checks([{"sql": "SELECT count(*) FROM assets"}], lambda spec: 1)
    assert results[0]["name"] == "SELECT count(*) FROM assets"


def test_run_checks_on_empty_list():
    assert run_checks([], lambda spec: 1) == ([], [])
