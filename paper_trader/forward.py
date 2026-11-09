"""One bounded paper run. Import does not fetch and does not place orders.

The orchestration is wired. The runtime is held until a license, a plan, a
key handoff, and an explicit acquire flag. The default is dry and offline:
a key in the environment does not fetch. The live reader is reached only
from ``read_url`` and ``fetch_aggregates`` after those gates, and this
module does not call it on import.

``historical_replay`` uses the bar-end clock and says so. ``fresh_forward``
uses the acquisition clock and never the bar end. Selection is the existing
engine rank inside the predeclared symbol list. The full-market universe is
false. There is no broker pathway.
"""

from paper_trader.controller import (
    bind_universe,
    resolve_session,
    run_recorded,
    waiting_report,
)
from paper_trader.massive import (
    MAX_CALLS,
    MAX_PAGES,
    RUNTIME_APPROVAL,
    CallBudget,
    FeedRejected,
    Offline,
    acquisition_stamp,
    build_request_url,
    collect_pages,
    read_url,
)

ORCHESTRATION = "wired_inert"
RUNTIME = "held_pending_license_plan_key_and_explicit_approval"
FORWARD_IMPLEMENTED = False
FORWARD_RUNNING = False
BLOCKER = (
    "runtime is held and there is no broker pathway. "
    "Orchestration is wired. A license, plan, key handoff, and explicit "
    "approval are still required before a live read. Import does not fetch."
)


def run_forward(*, symbols, session_dates, session_kind, early_close, state_name,
                mode="historical_replay", bodies_by_symbol=None, acquire=False,
                dry_run=True, network_enabled=False, runtime_approval=None,
                api_key=None, timeout_seconds=5, transport=None, acquired_at=None,
                create=False):
    """Persist one bounded run, or return a waiting report with no write.

    ``acquire`` is the only path that can read Massive. It still requires
    ``dry_run`` false, ``network_enabled`` true, and the approval phrase.
    Tests pass ``transport`` so the live reader is not used.
    """
    bind_universe(symbols)
    session = resolve_session(
        session_dates, session_kind=session_kind, early_close=early_close,
    )
    if session["status"] != "proceed":
        return waiting_report(session)
    if mode not in ("historical_replay", "fresh_forward"):
        raise FeedRejected("mode is not recognized")
    if acquire:
        if mode != "fresh_forward":
            raise FeedRejected("acquisition is fresh forward only")
        pages, complete = _acquire(
            symbols, session_dates, dry_run=dry_run, network_enabled=network_enabled,
            runtime_approval=runtime_approval, api_key=api_key,
            timeout_seconds=timeout_seconds, transport=transport,
        )
        if acquired_at is None:
            acquired_at = acquisition_stamp()
        view = run_recorded(
            pages,
            symbols=symbols,
            session_dates=session_dates,
            session_kind=session_kind,
            early_close=early_close,
            mode="fresh_forward",
            ingested_at=acquired_at,
            clock_source="acquisition",
            state_name=state_name,
            label="historical",
            create=create,
        )
        if not complete:
            view["dataset_complete"] = False
            view["scan"]["dataset_complete"] = False
        view["network"] = "explicit_acquire_path"
        return view
    if mode != "historical_replay":
        raise FeedRejected("fresh forward requires acquire")
    if not isinstance(bodies_by_symbol, dict):
        raise FeedRejected("an offline run needs local pages")
    view = run_recorded(
        bodies_by_symbol,
        symbols=symbols,
        session_dates=session_dates,
        session_kind=session_kind,
        early_close=early_close,
        mode="recorded_batch_replay",
        state_name=state_name,
        label="synthetic",
        create=create,
    )
    view["network"] = "not_called"
    return view


def _acquire(symbols, session_dates, *, dry_run, network_enabled, runtime_approval,
             api_key, timeout_seconds, transport):
    if dry_run or not network_enabled:
        raise Offline("Massive network is disabled")
    if runtime_approval != RUNTIME_APPROVAL:
        raise FeedRejected("live Massive fetch is not approved")
    if len(session_dates) != 1:
        raise FeedRejected("one session date per acquisition")
    day = session_dates[0]
    budget = CallBudget(max_calls=MAX_CALLS, max_pages=MAX_PAGES)
    pages = {}
    complete = True

    def _load(url):
        if transport is not None:
            body = transport(url)
            if not isinstance(body, str):
                raise FeedRejected("transport did not return text")
            return body
        return read_url(url, api_key, timeout_seconds, transport=None)

    for symbol in symbols:
        walked = collect_pages(build_request_url(symbol, day, day), _load, budget)
        if not walked["complete"]:
            complete = False
        bodies = []
        for page in walked["pages"]:
            # Pages were parsed once to follow next_url. The controller parses
            # text, so hand it the original results only through a rebuilt
            # body that carries the adjustment field and no next_url. The
            # completeness flag already records that the walk stopped.
            bodies.append(_page_text(page))
        pages[symbol] = bodies
    return pages, complete


def _page_text(page):
    import json
    payload = {"results": page.get("results") or []}
    field = page.get("adjusted_field")
    if field == "false":
        payload["adjusted"] = False
    return json.dumps(payload)
