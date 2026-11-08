"""Offline simulation for one approved opening-range scope.

This package does not import the day-trader skills, does not place orders,
and does not read a network. There is no broker pathway and no forward feed.
"""

from paper_trader.engine import run_bars, size_batch, size_one
from paper_trader.forward import BLOCKER, FORWARD_IMPLEMENTED
from paper_trader.spec import ACK, STRATEGY_ID

__version__ = "0.1.0"

__all__ = [
    "ACK",
    "BLOCKER",
    "FORWARD_IMPLEMENTED",
    "STRATEGY_ID",
    "run_bars",
    "size_batch",
    "size_one",
]
