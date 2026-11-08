"""Test helpers. Import this before the package so bytecode stays off.

Run the suite only after review, from the repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s paper_trader/tests -t . -v

That command was not run while these tests were written. -B and
PYTHONDONTWRITEBYTECODE keep .pyc files out of this package and the skill
folders. Temp files stay under paper_trader/.tmp-tests.
"""

import os
import shutil
import sys

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(PACKAGE)
FIXTURES = os.path.join(PACKAGE, "fixtures")
TMP = os.path.join(PACKAGE, ".tmp-tests")

if REPO not in sys.path:
    sys.path.insert(0, REPO)


def workdir(name):
    path = os.path.join(TMP, name)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    absolute = os.path.abspath(path)
    package = os.path.abspath(PACKAGE)
    if os.path.commonpath([absolute, package]) != package:
        raise RuntimeError("test directory escaped paper_trader: %s" % absolute)
    return absolute


def fixture(name):
    return os.path.join(FIXTURES, name)


def approval(symbols, dates, label="synthetic"):
    from paper_trader.spec import ACK, STRATEGY_ID

    return {
        "acknowledge": ACK,
        "label": label,
        "per_trade_approval": False,
        "session_dates": list(dates),
        "strategy_id": STRATEGY_ID,
        "symbols": list(symbols),
    }


def bar(symbol, start, open_, high, low, close, source="synthetic", **extra):
    item = {
        "close": close,
        "high": high,
        "low": low,
        "open": open_,
        "source": source,
        "start": start,
        "symbol": symbol,
    }
    item.update(extra)
    return item


def of_type(doc, kind):
    return [event for event in doc["events"] if event["type"] == kind]
