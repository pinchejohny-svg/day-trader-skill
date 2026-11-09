"""One foreground replay. There is no scheduler and no default state path.

Exit 0: the ledger was written and the report was printed.
Exit 2: the scope was not approved.
Exit 3: the state, fixture, or lock refused the run.
"""

import argparse
import sys

from paper_trader.errors import (
    ApprovalRequired,
    ConcurrentRunRefused,
    DataError,
    PaperTraderError,
    PathRefused,
    ReplayConflict,
    RunBoundExceeded,
    ScopeError,
    StateCorrupt,
    StateExists,
    StateMissing,
)
from paper_trader.feed import load_fixture
from paper_trader.forward import run_forward, run_session
from paper_trader.jsonutil import pretty
from paper_trader.paths import confine_existing, read_text, state_path
from paper_trader.persist import StateStore
from paper_trader.report import build_report
from paper_trader.spec import ACK, STRATEGY_ID


def run(argv):
    if argv and argv[0] == "session":
        return _session_command(argv[1:])
    if argv and argv[0] == "forward":
        return _forward_command(argv[1:])
    parser = argparse.ArgumentParser(
        prog="paper_trader",
        description="Bounded offline simulation. State files stay inside paper_trader/demo-state.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new", action="store_true")
    mode.add_argument("--resume", action="store_true")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--state", required=True, help="File name inside paper_trader/demo-state")
    parser.add_argument("--label", required=True, choices=("synthetic", "historical"))
    parser.add_argument("--sessions", required=True, help="Comma-separated YYYY-MM-DD")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--approve", required=True)
    parser.add_argument("--acknowledge-simulated", action="store_true")
    args = parser.parse_args(argv)
    if args.approve != STRATEGY_ID or not args.acknowledge_simulated:
        sys.stderr.write(
            "approval refused: pass --approve %s --acknowledge-simulated\n" % STRATEGY_ID
        )
        return 2
    try:
        if "/" in args.state or "\\" in args.state:
            raise PathRefused("--state must be a file name inside paper_trader/demo-state")
        state_file = state_path(args.state)
        fixture = load_fixture(args.fixture, root="fixtures")
        if fixture["label"] != args.label:
            raise ScopeError("fixture label does not match --label")
        approval = {
            "acknowledge": ACK,
            "label": args.label,
            "per_trade_approval": False,
            "session_dates": [item.strip() for item in args.sessions.split(",") if item.strip()],
            "strategy_id": STRATEGY_ID,
            "symbols": [item.strip() for item in args.symbols.split(",") if item.strip()],
        }
        with StateStore(state_file) as store:
            if args.new:
                frozen = store.create(fixture["bars"], approval)
            else:
                frozen = store.resume(fixture["bars"], approval)
        sys.stdout.write(pretty(build_report(frozen)))
        return 0
    except ApprovalRequired as exc:
        sys.stderr.write("approval refused: %s\n" % exc)
        return 2
    except (
        ConcurrentRunRefused,
        DataError,
        PathRefused,
        ReplayConflict,
        RunBoundExceeded,
        ScopeError,
        StateCorrupt,
        StateExists,
        StateMissing,
        PaperTraderError,
    ) as exc:
        sys.stderr.write("%s\n" % exc)
        return 3


def _forward_command(argv):
    """Explicit paper entrypoint. Dry and offline unless ``--acquire`` is set.

    Proposed command, not a claim that this process has run it:

    ``python3 -B -m paper_trader forward --new --state NAME.json --symbols SYN --sessions 2026-11-02 --session-kind regular --early-close none --mode historical_replay --page SYN=paper_trader/fixtures/massive/synthetic_aggregates_round_trip.json --approve orb15-5m-long-next-open-v1 --acknowledge-simulated``

    A later live read would add ``--acquire --network --approve-fetch massive_readonly_fetch_approved --timeout 5 --mode fresh_forward``. The default does not.
    """
    parser = argparse.ArgumentParser(
        prog="paper_trader forward",
        description=(
            "Bounded paper run. Default is dry and offline, including when a key is present. "
            "Help text is the proposed entrypoint until a reviewed run."
        ),
    )
    parser.add_argument("--state", required=True, help="File name inside paper_trader/demo-state")
    parser.add_argument("--symbols", required=True, help="Comma-separated predeclared symbols")
    parser.add_argument("--sessions", required=True, help="Comma-separated YYYY-MM-DD")
    parser.add_argument("--session-kind", required=True, choices=("regular", "unknown"))
    parser.add_argument("--early-close", required=True, choices=("none", "unknown"))
    parser.add_argument("--mode", required=True, choices=("historical_replay", "fresh_forward"))
    parser.add_argument("--page", action="append", default=[], help="SYM=path under fixtures/")
    parser.add_argument("--new", action="store_true", help="Create the ledger only if it is absent")
    parser.add_argument("--acquire", action="store_true", help="Opt in to one bounded read")
    parser.add_argument("--network", action="store_true", help="Required with --acquire")
    parser.add_argument("--approve-fetch", default="", help="Exact fetch approval phrase")
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--approve", required=True)
    parser.add_argument("--acknowledge-simulated", action="store_true")
    args = parser.parse_args(argv)
    if args.approve != STRATEGY_ID or not args.acknowledge_simulated:
        sys.stderr.write(
            "approval refused: pass --approve %s --acknowledge-simulated\n" % STRATEGY_ID
        )
        return 2
    try:
        if "/" in args.state or "\\" in args.state:
            raise PathRefused("--state must be a file name inside paper_trader/demo-state")
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        dates = [item.strip() for item in args.sessions.split(",") if item.strip()]
        bodies = None
        if not args.acquire:
            bodies = {}
            for item in args.page:
                if item.count("=") != 1:
                    raise DataError("page must be SYM=path")
                symbol, path = item.split("=", 1)
                bodies[symbol] = [read_text(confine_existing(path, "fixtures"))]
        view = run_forward(
            symbols=symbols,
            session_dates=dates,
            session_kind=args.session_kind,
            early_close=args.early_close,
            state_name=args.state,
            mode=args.mode,
            bodies_by_symbol=bodies,
            acquire=args.acquire,
            dry_run=not args.acquire,
            network_enabled=args.network,
            runtime_approval=args.approve_fetch,
            timeout_seconds=args.timeout,
            create=args.new,
        )
        sys.stdout.write(pretty(view))
        return 0
    except (ApprovalRequired, PaperTraderError) as exc:
        code = 2 if isinstance(exc, ApprovalRequired) else 3
        sys.stderr.write("%s\n" % exc)
        return code


def _session_command(argv):
    """Foreground wall-clock session. Proposed until a reviewed run.

    It waits between polls and exits. It does not install a service.

    ``python3 -B -m paper_trader session --new --state NAME.json --symbols SYN --sessions 2026-11-02 --session-kind regular --early-close none --max-iterations 8 --max-requests 8 --max-runtime-seconds 1800 --poll-seconds 60 --page SYN=paper_trader/fixtures/massive/synthetic_aggregates_round_trip.json --approve orb15-5m-long-next-open-v1 --acknowledge-simulated``
    """
    parser = argparse.ArgumentParser(
        prog="paper_trader session",
        description=(
            "One foreground session. The process clock and a pause between polls are the "
            "defaults. It exits at a cap, at 16:00, or on interrupt. Not a service and not "
            "a background task. Live network stays off unless --acquire and --network are "
            "both set."
        ),
    )
    parser.add_argument("--state", required=True, help="File name inside paper_trader/demo-state")
    parser.add_argument("--symbols", required=True, help="Comma-separated predeclared symbols")
    parser.add_argument("--sessions", required=True, help="One YYYY-MM-DD session")
    parser.add_argument("--session-kind", required=True, choices=("regular", "unknown"))
    parser.add_argument("--early-close", required=True, choices=("none", "unknown"))
    parser.add_argument("--max-iterations", required=True, type=int)
    parser.add_argument("--max-requests", required=True, type=int)
    parser.add_argument("--max-runtime-seconds", required=True, type=int)
    parser.add_argument("--poll-seconds", type=int, default=60, help="Pause between polls, 1 to 240")
    parser.add_argument("--page", action="append", default=[], help="SYM=path under fixtures/")
    parser.add_argument("--new", action="store_true")
    parser.add_argument("--acquire", action="store_true", help="Opt in to a live read")
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--approve-fetch", default="")
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--approve", required=True)
    parser.add_argument("--acknowledge-simulated", action="store_true")
    args = parser.parse_args(argv)
    if args.approve != STRATEGY_ID or not args.acknowledge_simulated:
        sys.stderr.write(
            "approval refused: pass --approve %s --acknowledge-simulated\n" % STRATEGY_ID
        )
        return 2
    try:
        if "/" in args.state or "\\" in args.state:
            raise PathRefused("--state must be a file name inside paper_trader/demo-state")
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        dates = [item.strip() for item in args.sessions.split(",") if item.strip()]
        injected = None
        if args.page:
            loaded = {}
            for item in args.page:
                if item.count("=") != 1:
                    raise DataError("page must be SYM=path")
                symbol, path = item.split("=", 1)
                loaded[symbol] = read_text(confine_existing(path, "fixtures"))

            def injected(symbol, _clock, loaded=loaded):
                if symbol not in loaded:
                    raise DataError("page is missing for a predeclared symbol")
                return loaded[symbol]

        view = run_session(
            symbols=symbols,
            session_dates=dates,
            session_kind=args.session_kind,
            early_close=args.early_close,
            state_name=args.state,
            max_iterations=args.max_iterations,
            max_requests=args.max_requests,
            max_runtime_seconds=args.max_runtime_seconds,
            poll_seconds=args.poll_seconds,
            injected_pages=injected,
            acquire=args.acquire,
            dry_run=not args.acquire,
            network_enabled=args.network,
            runtime_approval=args.approve_fetch,
            timeout_seconds=args.timeout,
            create=args.new,
        )
        sys.stdout.write(pretty(view))
        if view.get("stop") == "interrupted":
            return 130
        return 0
    except (ApprovalRequired, PaperTraderError) as exc:
        code = 2 if isinstance(exc, ApprovalRequired) else 3
        sys.stderr.write("%s\n" % exc)
        return code


def main(argv=None):
    sys.exit(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
