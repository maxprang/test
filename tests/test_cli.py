"""CLI argument handling — the flags cron and systemd actually type."""

import pytest

from restore_guard.cli import build_parser, main


def parse(argv):
    args = build_parser().parse_args(argv)
    return args


@pytest.mark.parametrize(
    "argv",
    [
        ["-q", "run"],
        ["run", "-q"],
        ["--quiet", "run"],
        ["run", "--quiet"],
    ],
)
def test_quiet_works_on_either_side_of_the_subcommand(argv):
    assert getattr(parse(argv), "quiet", False) is True


@pytest.mark.parametrize("argv", [["-c", "x.yml", "run"], ["run", "-c", "x.yml"]])
def test_config_works_on_either_side_of_the_subcommand(argv):
    assert parse(argv).config == "x.yml"


def test_flags_default_to_absent_not_false():
    """SUPPRESS keeps a subparser from clobbering a value set before it."""
    args = parse(["run"])
    assert not hasattr(args, "quiet")


def test_verbose_before_subcommand_survives():
    args = parse(["-v", "run"])
    assert getattr(args, "verbose", False) is True


def test_status_output_modes_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        parse(["status", "--json", "--html"])


def test_missing_config_is_exit_code_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--config", str(tmp_path / "nope.yml")]) == 2


def test_plugins_needs_no_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "zfs" in out and "mysql" in out
