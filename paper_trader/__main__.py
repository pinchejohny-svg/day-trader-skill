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
from paper_trader.jsonutil import pretty
from paper_trader.paths import state_path
from paper_trader.persist import StateStore
from paper_trader.report import build_report
from paper_trader.spec import ACK, STRATEGY_ID


def run(argv):
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


def main(argv=None):
    sys.exit(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
