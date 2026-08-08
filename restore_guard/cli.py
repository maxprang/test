"""Command line interface.

Exit codes are the contract for cron/systemd/monitoring:
    0  everything verified
    1  at least one job failed or is stale
    2  configuration or usage error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .notify import notify
from .report import (
    collect,
    history_table,
    html_report,
    json_report,
    prometheus_metrics,
    status_table,
    write_atomic,
)
from .runner import Runner
from .sources import known_sources
from .state import State
from .util import Logger
from .verifiers import build_verifiers, known_verifiers

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    # Shared flags, accepted on either side of the subcommand: `-q run` and
    # `run -q` both work. SUPPRESS matters — without it the subparser's default
    # would overwrite a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", "-c", default=argparse.SUPPRESS, help="path to config.yml"
    )
    common.add_argument(
        "--verbose", "-v", action="store_true", default=argparse.SUPPRESS,
        help="log every check",
    )
    common.add_argument(
        "--quiet", "-q", action="store_true", default=argparse.SUPPRESS,
        help="only report problems",
    )

    parser = argparse.ArgumentParser(
        prog="restore-guard",
        description="Prove that your backups can actually be restored.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"restore-guard {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    run_cmd = add("run", help="restore and verify one or all jobs")
    run_cmd.add_argument("jobs", nargs="*", help="job names (default: all enabled jobs)")
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="reach the repository and pick a snapshot, but do not restore",
    )
    run_cmd.add_argument("--no-notify", action="store_true", help="suppress notifications")
    run_cmd.add_argument(
        "--parallel",
        type=int,
        metavar="N",
        help="verify up to N jobs at once (overrides defaults.parallel)",
    )

    status_cmd = add("status", help="show when each backup was last proven restorable")
    output = status_cmd.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable output")
    output.add_argument(
        "--html",
        nargs="?",
        const="-",
        metavar="FILE",
        help="write a self-contained status page (default: stdout)",
    )

    history_cmd = add("history", help="show recent runs")
    history_cmd.add_argument("job", nargs="?", help="limit to one job")
    history_cmd.add_argument("--limit", type=int, default=20)
    history_cmd.add_argument("--json", action="store_true")

    add("validate", help="check the config file and exit")
    add("metrics", help="print (and optionally write) Prometheus metrics")
    add("plugins", help="list available source and verifier types")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.verbose = getattr(args, "verbose", False)
    args.quiet = getattr(args, "quiet", False)
    args.config = getattr(args, "config", None)
    logger = Logger(verbose=args.verbose, quiet=args.quiet)

    if args.command == "plugins":
        print("sources:   " + ", ".join(known_sources()))
        print("verifiers: " + ", ".join(known_verifiers()))
        return EXIT_OK

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    handlers = {
        "run": _cmd_run,
        "status": _cmd_status,
        "history": _cmd_history,
        "validate": _cmd_validate,
        "metrics": _cmd_metrics,
    }
    try:
        return handlers[args.command](args, config, logger)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_PROBLEM


def _cmd_run(args, config: Config, logger: Logger) -> int:
    parallel = args.parallel if args.parallel else config.parallel
    with State(config.state_path) as state:
        runner = Runner(config, state, logger, dry_run=args.dry_run, parallel=parallel)
        outcomes = runner.run_all(only=args.jobs or None)

        if not args.no_notify and not args.dry_run:
            notify(config, outcomes, logger)

        statuses = collect(config, state)
        if config.metrics_file:
            write_atomic(config.metrics_file, prometheus_metrics(statuses))
        if config.report_file:
            write_atomic(config.report_file, html_report(statuses))

        if not args.quiet:
            print()
            print(status_table(statuses))

    failed = [o for o in outcomes if not o.record.ok and o.record.status != "skipped"]
    return EXIT_PROBLEM if failed else EXIT_OK


def _cmd_status(args, config: Config, logger: Logger) -> int:
    with State(config.state_path) as state:
        statuses = collect(config, state)

    if args.json:
        print(json_report(statuses))
    elif args.html:
        content = html_report(statuses)
        if args.html == "-":
            print(content, end="")
        else:
            write_atomic(Path(args.html), content)
            print(f"written to {args.html}")
    else:
        print(status_table(statuses))
    return EXIT_OK if all(status.healthy for status in statuses) else EXIT_PROBLEM


def _cmd_history(args, config: Config, logger: Logger) -> int:
    with State(config.state_path) as state:
        records = state.history(args.job, args.limit)
    if args.json:
        import json as _json

        print(_json.dumps([record.to_dict() for record in records], indent=2, default=str))
    else:
        print(history_table(records))
    return EXIT_OK


def _cmd_validate(args, config: Config, logger: Logger) -> int:
    from .sources import build_source

    problems = 0
    for job in config.jobs:
        try:
            build_source(job, logger)
            build_verifiers(job)
        except ConfigError as exc:
            print(f"  ! {exc}", file=sys.stderr)
            problems += 1
            continue
        checks = ", ".join(str(check.get("type")) for check in job.verify)
        state = "" if job.enabled else " [disabled]"
        print(f"  + {job.name}{state}: {job.source['type']} -> {checks}")

    if problems:
        print(f"{problems} job(s) misconfigured", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{len(config.jobs)} job(s) OK — config {config.source_path} is valid")
    return EXIT_OK


def _cmd_metrics(args, config: Config, logger: Logger) -> int:
    with State(config.state_path) as state:
        content = prometheus_metrics(collect(config, state))
    if config.metrics_file:
        write_atomic(config.metrics_file, content)
        print(f"# written to {config.metrics_file}")
    print(content, end="")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
