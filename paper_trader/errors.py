"""Errors for the offline simulation. This package has no order pathway."""


class PaperTraderError(Exception):
    """Base error for a refused simulation run."""


class ApprovalRequired(PaperTraderError):
    """The human-approved run scope is missing or does not match this strategy."""


class ScopeError(PaperTraderError):
    """A bar is outside the approved symbols, dates, or label."""


class DataError(PaperTraderError):
    """The fixture or bar list is not a usable tape."""


class StateCorrupt(PaperTraderError):
    """Persisted state is malformed, truncated, or disagrees with its own tape."""


class StateExists(PaperTraderError):
    """Refusing to create a new ledger over an existing file."""


class StateMissing(PaperTraderError):
    """Resume was asked for a state file that is not there."""


class StateUnflushed(PaperTraderError):
    """The ledger name was replaced and the flush then failed.

    The file is left where it is. This call does not resume it and does not
    write a new account over it. Whether the directory entry reached disk
    is unknown.
    """


class ConcurrentRunRefused(PaperTraderError):
    """Another writer holds this ledger. This run will not trade."""


class ReplayConflict(PaperTraderError):
    """A resume would rewrite history or change the approved scope."""


class RunBoundExceeded(PaperTraderError):
    """The tape is larger than the bounded offline run allows."""


class EngineInvariant(PaperTraderError):
    """Internal accounting broke. The ledger must not be saved."""


class PathRefused(PaperTraderError):
    """The path leaves the demo directory, traverses, or uses a symlink."""
