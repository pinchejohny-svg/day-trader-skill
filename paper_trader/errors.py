"""Errors for the offline paper trader. None of these place an order."""


class PaperTraderError(Exception):
    """Base error. Callers should not treat this as a broker rejection."""


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


class ConcurrentRunRefused(PaperTraderError):
    """Another writer holds this ledger. This run will not trade."""


class ReplayConflict(PaperTraderError):
    """A resume would rewrite history or change the approved scope."""


class RunBoundExceeded(PaperTraderError):
    """The tape is larger than the bounded offline run allows."""


class EngineInvariant(PaperTraderError):
    """Internal accounting broke. The ledger must not be saved."""


class ForwardDisabled(PaperTraderError):
    """Forward paper mode has no verified data contract and does no I/O."""
