"""One bounded paper run. Import does not fetch and does not place orders.

The orchestration is wired. The runtime is held until a license, a plan, a
key handoff, and an explicit acquire flag. The default is dry and offline:
a key in the environment does not fetch. The live reader is reached only
from ``read_url`` and ``fetch_aggregates`` after those gates, and this
module does not call it on import.

``historical_replay`` is one bounded replay, not the unattended session.
``run_session`` is one foreground process: it reads the wall clock, waits,
and stops at a cap or the session cutoff. It does not start itself or
become a service. Tests may inject the clock and the wait. ``fresh_forward``
uses the acquisition clock and never the bar end. Selection is the existing
engine rank inside the predeclared symbol list. The full-market universe
is false. There is no broker pathway.
"""

from paper_trader.controller import (
    bind_universe,
    resolve_session,
    rows_for_clock,
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
    parse_response,
    read_url,
)
from paper_trader.paths import kind, state_path

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
                api_key=None, timeout_seconds=5, injected_body=None, acquired_at=None,
                create=False):
    """Persist one bounded replay, or return a waiting report with no write.

    ``injected_body`` is a fixture callback, not a provider transport. It
    cannot produce a historical or verified ledger. ``acquire`` reaches the
    live reader only when no injection is passed, and only with the dry-run
    off, the network flag, and the approval phrase.
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
        injected = injected_body is not None
        provenance = "injected" if injected else "unverified_read"
        label = "synthetic" if injected else "historical"
        pages, complete = _acquire(
            symbols, session_dates, dry_run=dry_run, network_enabled=network_enabled,
            runtime_approval=runtime_approval, api_key=api_key,
            timeout_seconds=timeout_seconds, injected_body=injected_body,
            provenance=provenance,
        )
        if acquired_at is None:
            if injected:
                raise FeedRejected("an injected page needs an injected clock")
            acquired_at = acquisition_stamp()
        kept, rejected = _closed_pages(pages, acquired_at, provenance)
        if not any(kept.values()):
            report = waiting_report(session)
            report["rejections"] = rejected
            report["invocation"] = "one_replay"
            report["network"] = "injected_not_a_provider_read" if injected else "not_called"
            report["provenance"] = [provenance]
            report["label"] = label
            report["bar_source"] = []
            report["adjustment_basis"] = ["synthetic" if provenance != "unverified_read" else "unverified"]
            report["data_verified"] = False
            report["status"] = "no_closed_anchor"
            return report
        view = run_recorded(
            kept,
            symbols=symbols,
            session_dates=session_dates,
            session_kind=session_kind,
            early_close=early_close,
            mode="fresh_forward",
            ingested_at=acquired_at,
            clock_source="acquisition",
            state_name=state_name,
            label=label,
            create=create,
            provenance=provenance,
        )
        view["rejections"] = list(view["rejections"]) + rejected
        if not complete:
            view["dataset_complete"] = False
            view["scan"]["dataset_complete"] = False
        view["network"] = (
            "injected_not_a_provider_read" if injected else "explicit_acquire_path_unverified"
        )
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
        provenance="synthetic",
    )
    view["network"] = "not_called"
    view["invocation"] = "one_replay"
    return view


def _acquire(symbols, session_dates, *, dry_run, network_enabled, runtime_approval,
             api_key, timeout_seconds, injected_body, provenance):
    if injected_body is None and (dry_run or not network_enabled):
        raise Offline("Massive network is disabled")
    if injected_body is None and runtime_approval != RUNTIME_APPROVAL:
        raise FeedRejected("live Massive fetch is not approved")
    if len(session_dates) != 1:
        raise FeedRejected("one session date per acquisition")
    day = session_dates[0]
    budget = CallBudget(max_calls=MAX_CALLS, max_pages=MAX_PAGES)
    pages = {}
    complete = True

    def _load(url):
        if injected_body is not None:
            body = injected_body(url)
            if not isinstance(body, str):
                raise FeedRejected("injected page was not text")
            return body
        return read_url(url, api_key, timeout_seconds, transport=None)

    for symbol in symbols:
        walked = collect_pages(build_request_url(symbol, day, day), _load, budget)
        if not walked["complete"]:
            complete = False
        bodies = []
        for page in walked["pages"]:
            bodies.append(_page_text(page, provenance))
        pages[symbol] = bodies
    return pages, complete


def _page_text(page, provenance):
    """Rebuild one page without dropping the invented note or the provenance."""
    import json
    payload = {
        "provenance": provenance,
        "results": page.get("results") or [],
    }
    if isinstance(page.get("note"), str):
        payload["note"] = page["note"]
    if page.get("adjusted_field") == "false":
        payload["adjusted"] = False
    return json.dumps(payload)


def _closed_pages(pages, clock, provenance):
    kept = {}
    rejected = []
    for symbol, bodies in pages.items():
        texts = []
        for body in bodies:
            parsed = parse_response(body)
            eligible, extra = rows_for_clock(parsed["results"], clock, [])
            rejected.extend(extra)
            if not eligible:
                continue
            texts.append(_page_text({
                "adjusted_field": parsed["adjusted_field"],
                "note": parsed.get("note"),
                "results": eligible,
            }, provenance))
        if texts:
            kept[symbol] = texts
    return kept, rejected


def _wall_clock():
    """New York stamp from the process clock. Not a supplied schedule."""
    return acquisition_stamp()


def _wall_monotonic():
    import time
    return time.monotonic()


def _wall_wait(seconds):
    """Ordinary pause between polls. This is the runtime default."""
    import time
    if seconds > 0:
        time.sleep(seconds)


def run_session(*, symbols, session_dates, session_kind, early_close, state_name,
                max_iterations, max_requests, max_runtime_seconds, poll_seconds=60,
                injected_pages=None, clock=None, waiter=None, monotonic=None,
                acquire=False, dry_run=True, network_enabled=False,
                runtime_approval=None, api_key=None, timeout_seconds=5, create=False):
    """One foreground session. It reads the clock, polls, waits, and exits.

    The default clock is the wall clock and the default wait is the stdlib
    pause. A test may pass ``clock``, ``waiter``, and ``monotonic`` instead.
    Those stand-ins are not a schedule of future trades. The process does
    not start itself and does not keep running after a cap, 16:00, or an
    interrupt. Each poll keeps only bars closed at that reading.
    """
    bind_universe(symbols)
    session = resolve_session(
        session_dates, session_kind=session_kind, early_close=early_close,
    )
    if session["status"] != "proceed":
        report = waiting_report(session)
        report["iterations"] = 0
        report["requests"] = 0
        report["stop"] = session["reason"]
        report["invocation"] = "finite_wall_clock_session"
        return report
    _check_session_bounds(
        session_dates, max_iterations, max_requests, max_runtime_seconds, poll_seconds,
    )
    clock_fn = _wall_clock if clock is None else clock
    wait_fn = _wall_wait if waiter is None else waiter
    mono = _wall_monotonic if monotonic is None else monotonic
    injected = injected_pages is not None
    if not injected and (not acquire or dry_run or not network_enabled):
        raise Offline("Massive network is disabled")
    if not injected and runtime_approval != RUNTIME_APPROVAL:
        raise FeedRejected("live Massive fetch is not approved")
    provenance = "injected" if injected else "unverified_read"
    label = "synthetic" if injected else "historical"
    path = state_path(state_name)
    existed = kind(path) == "file"
    if existed and create:
        from paper_trader.errors import StateExists
        raise StateExists("state file already exists; resume it or choose a new name")
    starts = _stored_starts(path) if existed else []
    made = existed
    last = None
    requests = 0
    iterations = 0
    stop = "max_iterations"
    step_rejected = []
    updates = []
    started = mono()
    day = session_dates[0] + "T"
    cutoff = session_dates[0] + "T16:00:00"
    try:
        while True:
            if iterations >= max_iterations:
                stop = "max_iterations"
                break
            if requests >= max_requests:
                stop = "max_requests"
                break
            if mono() - started >= max_runtime_seconds:
                stop = "max_runtime"
                break
            stamp = clock_fn()
            if not isinstance(stamp, str) or not stamp.startswith(day) or stamp > cutoff:
                stop = "session_cutoff"
                break
            pages = {}
            step_rejected = []
            for symbol in symbols:
                if requests >= max_requests:
                    stop = "max_requests"
                    break
                if injected:
                    body = injected_pages(symbol, stamp)
                else:
                    body = read_url(
                        build_request_url(symbol, session_dates[0], session_dates[0]),
                        api_key, timeout_seconds, transport=None,
                    )
                if not isinstance(body, str):
                    raise FeedRejected("injected page was not text")
                requests += 1
                parsed = parse_response(body)
                eligible, extra = rows_for_clock(parsed["results"], stamp, starts)
                step_rejected.extend(extra)
                if not eligible:
                    continue
                pages[symbol] = [_page_text({
                    "adjusted_field": parsed["adjusted_field"],
                    "note": parsed.get("note"),
                    "results": eligible,
                }, provenance)]
            if pages:
                if not made and not create:
                    from paper_trader.errors import StateMissing
                    raise StateMissing("state file is absent")
                last = run_recorded(
                    pages,
                    symbols=symbols,
                    session_dates=session_dates,
                    session_kind=session_kind,
                    early_close=early_close,
                    mode="fresh_forward",
                    ingested_at=stamp,
                    clock_source="acquisition",
                    state_name=state_name,
                    label=label,
                    create=not made,
                    provenance=provenance,
                )
                last["rejections"] = list(last["rejections"]) + step_rejected
                made = True
                starts = _stored_starts(path)
            elif last is not None:
                last["rejections"] = list(last.get("rejections") or []) + step_rejected
            if last is not None:
                updates.append({
                    "cash": last.get("cash"),
                    "cursor": last.get("cursor"),
                    "equity": last.get("equity"),
                    "iteration": iterations + 1,
                })
            iterations += 1
            if stop == "max_requests" or iterations >= max_iterations:
                if iterations >= max_iterations:
                    stop = "max_iterations"
                break
            remaining = max_runtime_seconds - (mono() - started)
            if remaining <= 0:
                stop = "max_runtime"
                break
            delay = poll_seconds if poll_seconds < remaining else remaining
            wait_fn(delay)
    except KeyboardInterrupt:
        stop = "interrupted"
    if last is None:
        last = waiting_report(session)
        last["persistence"] = "not_written"
        last["provenance"] = [provenance]
        last["label"] = label
        last["data_verified"] = False
        last["rejections"] = step_rejected
    last["clock"] = "wall_clock" if clock is None else "injected"
    last["invocation"] = "finite_wall_clock_session"
    last["iterations"] = iterations
    last["poll_seconds"] = poll_seconds
    last["requests"] = requests
    last["running"] = False
    last["stop"] = stop
    last["streaming"] = "foreground_finite_session_not_a_service"
    last["updates"] = updates
    last["wait"] = "stdlib_pause" if waiter is None else "injected"
    last["network"] = "injected_not_a_provider_read" if injected else "explicit_acquire_path_unverified"
    return last


def _check_session_bounds(session_dates, max_iterations, max_requests, max_runtime_seconds, poll_seconds):
    if len(session_dates) != 1:
        raise FeedRejected("one session date per acquisition")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise FeedRejected("iteration budget is not a whole number")
    if not 1 <= max_iterations <= 16:
        raise FeedRejected("iteration budget is outside 1 to 16")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int):
        raise FeedRejected("request budget is not a whole number")
    if not 1 <= max_requests <= MAX_CALLS:
        raise FeedRejected("request budget is outside 1 to %d" % MAX_CALLS)
    if isinstance(max_runtime_seconds, bool) or not isinstance(max_runtime_seconds, int):
        raise FeedRejected("runtime budget is not a whole number")
    if not 1 <= max_runtime_seconds <= 7200:
        raise FeedRejected("runtime budget is outside 1 to 7200 seconds")
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int):
        raise FeedRejected("poll interval is not a whole number")
    if not 1 <= poll_seconds <= 240:
        raise FeedRejected("poll interval is outside 1 to 240 seconds")


def _stored_starts(path):
    from paper_trader.persist import StateStore
    with StateStore(path) as store:
        doc = store.load()
    return [
        bar.get("start") for bar in doc["inputs"]
        if isinstance(bar, dict)
    ]
