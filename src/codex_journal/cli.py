from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import JournalEngine, default_state_root, format_discovery_line


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-journal")
    parser.add_argument("--codex-home", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", type=Path, default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="list safe session metadata")
    discover.add_argument("--session", help="list one exact session ID")
    for name in ("sync", "rebuild"):
        command = subparsers.add_parser(name)
        command.add_argument("--session", required=name == "rebuild")
        command.add_argument("--timezone")
    subparsers.add_parser("verify")
    return parser


def _print_sync(result: object) -> None:
    print(
        f"discovered={result.discovered} processed={result.processed} "
        f"unchanged={result.unchanged} appended={result.appended} rebuilt={result.rebuilt}"
    )
    print(
        f"no_heartbeats={result.no_heartbeats} active_or_incomplete={result.active_or_incomplete} "
        f"sessions_with_errors={result.sessions_with_errors}"
    )
    for error in result.errors:
        print(f"warning: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = (args.repo_root or repository_root()).expanduser()
    state_root = (args.codex_home or default_state_root()).expanduser()
    engine = JournalEngine(repo_root, state_root)
    if args.command == "discover":
        sessions, errors = engine.discover()
        if args.session:
            sessions = [session for session in sessions if session.session_id == args.session]
        print(f"sessions={len(sessions)}")
        for session in sessions:
            print(format_discovery_line(session, engine.home))
        for error in errors:
            print(f"warning: {error}", file=sys.stderr)
        return 0 if sessions or not args.session else 1
    if args.command == "sync":
        result = engine.sync(session_id=args.session, timezone_name=args.timezone)
        _print_sync(result)
        return 1 if result.processed == 0 and result.errors else 0
    if args.command == "rebuild":
        result = engine.rebuild(args.session, timezone_name=args.timezone)
        _print_sync(result)
        return 1 if result.processed == 0 else 0
    verification = engine.verify()
    print(f"journals={verification.journals} entries={verification.entries} errors={len(verification.errors)}")
    for warning in verification.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in verification.errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if verification.errors else 0
