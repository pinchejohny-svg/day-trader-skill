"""Inert read-only Massive aggregates adapter.

Nothing in this module opens a socket unless ``https_get`` is called, and
``https_get`` is not called by import, by tests, or by the default controller.
A process environment key does not enable the network. A refused call stops.
There is no retry, no sleep, and no scheduler. There is no broker, no order,
and no SDK.

Verified request shape only: ``GET https://api.massive.com/v2/aggs/ticker/
{ticker}/range/5/minute/{from}/{to}`` with ``adjusted=false``, ``sort=asc``,
and ``limit=5000``. ``results[].t`` is the Unix-millisecond start of the
window. Missing results are missing bars. This module does not invent OHLC.

``adjusted=true`` is the provider default and is split-adjusted. This
controller always sends ``adjusted=false`` so a later response cannot be
treated as permission to rewrite bars already used. The response field
``adjusted`` is not in the verified results contract. If it is present and
not false, the page is rejected. If it is absent, that absence is recorded.

Python documents Windows ``os.fsync`` elsewhere. This file does not claim
that CRT ``_commit`` calls ``FlushFileBuffers``.

Full-market ticker and snapshot field lists were not in the verified
aggregates contract. ``FULL_MARKET_UNIVERSE`` stays unimplemented.
"""

import datetime
import hashlib
import json
import re
import ssl

from paper_trader.errors import PaperTraderError

HOST = "api.massive.com"
ADJUSTED = "false"
SORT = "asc"
LIMIT = "5000"
MAX_LIMIT = 50000
MAX_CALLS = 8
MAX_PAGES = 2
MAX_BYTES = 1000000
MAX_SYMBOLS = 20
TIMEOUT_MIN = 1
TIMEOUT_MAX = 10
RUNTIME_APPROVAL = "massive_readonly_fetch_approved"
FULL_MARKET_UNIVERSE = "not_implemented_contract_unverified"
RAW_STORAGE_POLICY = (
    "Raw provider bodies are not written. A cache is not implemented. "
    "Only translated bars and rejection records leave this module."
)
_PRICE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_SECRET_QUERY = {"apikey", "api_key", "token", "access_token", "key"}


class FeedRejected(PaperTraderError):
    """A page, URL, or budget was refused. The message has no secret."""


class Offline(FeedRejected):
    """Network is disabled. A key being present does not change this."""


def windows_note():
    return (
        "Massive HTTPS uses the stdlib and is not enabled by import. "
        "The vendor SDK's Windows support was not in the verified contract. "
        "Native Windows use still needs a later review."
    )


def build_request_url(ticker, start, end):
    """Return a URL with no credential and no userinfo."""
    if not isinstance(ticker, str) or _TICKER.fullmatch(ticker) is None:
        raise FeedRejected("ticker is not an allowed symbol")
    for label, value in (("from", start), ("to", end)):
        if not isinstance(value, str) or _DATE.fullmatch(value) is None:
            raise FeedRejected("%s must be YYYY-MM-DD" % label)
        datetime.date.fromisoformat(value)
    if end < start:
        raise FeedRejected("to is before from")
    return (
        "https://%s/v2/aggs/ticker/%s/range/5/minute/%s/%s"
        "?adjusted=%s&sort=%s&limit=%s"
        % (HOST, ticker, start, end, ADJUSTED, SORT, LIMIT)
    )


def assert_timeout(timeout_seconds):
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise FeedRejected("timeout must be a whole number of seconds")
    if not TIMEOUT_MIN <= timeout_seconds <= TIMEOUT_MAX:
        raise FeedRejected("timeout is outside 1 to 10 seconds")


def redact(text, secret=None):
    """Drop a secret and any URL userinfo or credential query."""
    if not isinstance(text, str):
        return ""
    out = text
    if secret:
        out = out.replace(secret, "[redacted]")
    import urllib.parse
    try:
        parsed = urllib.parse.urlsplit(out)
    except ValueError:
        return "[redacted]"
    if parsed.scheme or parsed.netloc or parsed.query:
        host = parsed.hostname or ""
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        cleaned = []
        for key, _value in query:
            if key.lower() in _SECRET_QUERY:
                cleaned.append((key, "[redacted]"))
            else:
                cleaned.append((key, "[redacted]" if secret and secret in _value else _value))
        out = urllib.parse.urlunsplit((
            parsed.scheme,
            host,
            parsed.path,
            urllib.parse.urlencode(cleaned),
            "",
        ))
    return out


def validate_next_url(url):
    """Allow another page only on the same host, with no embedded secret."""
    if not isinstance(url, str) or not url:
        raise FeedRejected("pagination URL is missing")
    import urllib.parse
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or host != HOST:
        raise FeedRejected("pagination host is not allowed")
    if parsed.username or parsed.password:
        raise FeedRejected("pagination URL has embedded credentials")
    if parsed.port not in (None, 443):
        raise FeedRejected("pagination port is not allowed")
    for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _SECRET_QUERY:
            raise FeedRejected("pagination query contains a credential")
    return urllib.parse.urlunsplit((
        "https", HOST, parsed.path, parsed.query, "",
    ))


def _parse_json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise FeedRejected("duplicate key")
            out[key] = value
        return out

    def const(name):
        raise FeedRejected("invalid JSON constant")

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=str,
            parse_int=str,
            parse_constant=const,
        )
    except FeedRejected:
        raise
    except json.JSONDecodeError:
        raise FeedRejected("Massive response was not valid JSON")


def parse_response(text):
    """Parse one aggregates body. Numbers stay strings. No invented bars."""
    if not isinstance(text, str):
        raise FeedRejected("Massive response was not text")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise FeedRejected("response exceeded the byte budget")
    doc = _parse_json(text)
    if not isinstance(doc, dict):
        raise FeedRejected("Massive response was not a JSON object")
    status = doc.get("status", None)
    if status is not None and status != "OK":
        raise FeedRejected("Massive response status was not OK")
    adjusted = doc.get("adjusted", None)
    if adjusted is True or adjusted == "true":
        raise FeedRejected("split-adjusted page is refused")
    if adjusted not in (None, False, "false"):
        raise FeedRejected("adjustment flag is not false")
    results = doc.get("results", None)
    if results is None:
        results = []
    if not isinstance(results, list):
        raise FeedRejected("results was not a list")
    next_url = doc.get("next_url", None)
    if next_url is not None and not isinstance(next_url, str):
        raise FeedRejected("next_url was not a string")
    return {
        "adjusted_field": "absent" if adjusted is None else "false",
        "next_url": next_url,
        "request_adjusted": ADJUSTED,
        "results": results,
    }


def _nth_sunday(year, month, n):
    first = datetime.date(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def ny_wall_from_unix_ms(unix_ms):
    """America/New_York wall time for a post-2006 US DST rule.

    The rule is second Sunday in March at 07:00 UTC through first Sunday in
    November at 06:00 UTC. Years before 2007 are refused. The repeated hour
    on the November transition is refused. This is not an IANA database and
    it is not imported from the scanner.
    """
    if isinstance(unix_ms, bool) or not isinstance(unix_ms, int):
        if isinstance(unix_ms, str) and unix_ms.isdigit():
            unix_ms = int(unix_ms)
        else:
            raise FeedRejected("bar start is not a unix millisecond")
    if unix_ms < 0 or unix_ms % 1000:
        raise FeedRejected("bar start is not a whole UTC second")
    utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=unix_ms // 1000)
    if utc.year < 2007:
        raise FeedRejected("DST era before 2007 is unsupported")
    start = datetime.datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.time(6, 0))
    # Both UTC hours that print as 01:00-02:00 local on the fall-back morning.
    if end - datetime.timedelta(hours=1) <= utc < end + datetime.timedelta(hours=1):
        raise FeedRejected("ambiguous local time")
    local = utc + datetime.timedelta(hours=-4 if start <= utc < end else -5)
    return local.strftime("%Y-%m-%dT%H:%M:%S")


def bar_hash(symbol, start_ms, open_, high, low, close, volume=""):
    text = "|".join([symbol, str(start_ms), open_, high, low, close, volume])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def adjustment_basis(parsed, *, synthetic):
    """Name the price basis from the response, not from the request flag.

    ``massive_unadjusted`` is used only when the response itself says
    ``adjusted`` is false. A missing field stays ``unverified``. A synthetic
    fixture is never described as a provider adjustment.
    """
    if synthetic:
        return "synthetic"
    if not isinstance(parsed, dict):
        raise FeedRejected("adjustment basis has no page")
    field = parsed.get("adjusted_field")
    if field == "false":
        return "massive_unadjusted"
    if field == "absent":
        return "unverified"
    raise FeedRejected("adjustment basis is not recognized")


def acquisition_stamp():
    """Wall time of this call. Not a bar end and not a replay clock.

    Offline replay does not call this. The static check allows ``now`` only
    in this function. Resolution is one second because bar stamps are whole
    seconds. The returned value is America/New_York wall time.
    """
    utc = datetime.datetime.now(datetime.timezone.utc)
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    whole = int((utc - epoch).total_seconds())
    return ny_wall_from_unix_ms(whole * 1000)


def _volume(row):
    """Keep provider ``v`` when it is present. Absence is not a zero."""
    if "v" not in row or row.get("v") is None:
        return None
    value = row.get("v")
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise FeedRejected("provider volume is not a whole-share string")
    return value


def _price(row, key):
    value = row.get(key)
    if not isinstance(value, str) or _PRICE.fullmatch(value) is None:
        raise FeedRejected("price field is not a decimal string")
    return value


def translate_results(results, *, symbol, mode, source, ingested_at=None,
                      adjustment_basis="synthetic", clock_source="supplied"):
    """Turn one results list into engine bars or rejection records.

    ``recorded_batch_replay`` sets ``observed_at`` to the bar end. That is a
    completed-tape assumption, not a live fill and not an acquisition clock.
    ``as_observed`` uses a supplied stamp and labels it as not an acquisition
    clock. ``fresh_forward`` requires ``clock_source="acquisition"`` and uses
    that stamp for every bar. It never substitutes the bar end. Provider
    ``v`` is kept as ``provider_volume``. The engine has no volume filter, so
    this is not stored under the engine key ``volume``.
    """
    if mode not in ("recorded_batch_replay", "as_observed", "fresh_forward"):
        raise FeedRejected("replay mode is not recognized")
    if source not in ("synthetic", "historical"):
        raise FeedRejected("bar source is not recognized")
    if adjustment_basis not in ("synthetic", "massive_unadjusted", "unverified"):
        raise FeedRejected("adjustment basis is not recognized")
    if adjustment_basis == "massive_unadjusted" and source == "synthetic":
        raise FeedRejected("a synthetic tape cannot claim provider adjustment")
    if mode == "fresh_forward":
        if clock_source != "acquisition" or not isinstance(ingested_at, str):
            raise FeedRejected("fresh forward requires the acquisition clock")
    elif clock_source == "acquisition":
        raise FeedRejected("the acquisition clock is only for fresh forward")
    elif mode == "as_observed" and not isinstance(ingested_at, str):
        raise FeedRejected("ingested_at is required")
    if not isinstance(results, list):
        raise FeedRejected("results was not a list")
    bars = []
    rejections = []
    seen = {}
    last_ms = None
    for row in results:
        if not isinstance(row, dict):
            rejections.append({"symbol": symbol, "reason": "malformed_row"})
            continue
        try:
            start_ms = row.get("t")
            wall = ny_wall_from_unix_ms(start_ms)
            open_ = _price(row, "o")
            high = _price(row, "h")
            low = _price(row, "l")
            close = _price(row, "c")
            volume = _volume(row)
        except FeedRejected as exc:
            rejections.append({"symbol": symbol, "reason": str(exc)})
            continue
        ms = int(start_ms)
        digest = bar_hash(symbol, ms, open_, high, low, close, volume or "")
        if last_ms is not None and ms < last_ms:
            rejections.append({"symbol": symbol, "reason": "time_went_backwards"})
            continue
        last_ms = ms
        if ms in seen:
            if seen[ms] == digest:
                rejections.append({"symbol": symbol, "reason": "duplicate_bar"})
            else:
                rejections.append({
                    "symbol": symbol,
                    "reason": "correction_not_applied",
                    "kept_hash": seen[ms],
                    "replacement_hash": digest,
                })
            continue
        seen[ms] = digest
        end = _add_minutes(wall, 5)
        if mode == "fresh_forward":
            observed = ingested_at
            assumption = "acquisition_clock"
        elif mode == "recorded_batch_replay":
            observed = end
            assumption = "bar_end_for_recorded_replay_not_a_live_fill"
        else:
            observed = ingested_at
            assumption = "supplied_stamp_not_an_acquisition_clock"
        item = {
            "adjustment_basis": adjustment_basis,
            "close": close,
            "high": high,
            "low": low,
            "observed_at": observed,
            "open": open_,
            "source": source,
            "start": wall,
            "symbol": symbol,
            "_assumption": assumption,
        }
        if volume is not None:
            item["provider_volume"] = volume
        bars.append(item)
    public = []
    for bar in bars:
        item = dict(bar)
        item.pop("_assumption", None)
        public.append(item)
    return public, rejections, [bar["_assumption"] for bar in bars]


def _add_minutes(stamp, minutes):
    parsed = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    return (parsed + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


class CallBudget(object):
    def __init__(self, max_calls=MAX_CALLS, max_pages=MAX_PAGES):
        self.max_calls = max_calls
        self.max_pages = max_pages
        self.calls = 0
        self.pages = 0

    def call(self):
        self.calls += 1
        if self.calls > self.max_calls:
            raise FeedRejected("call budget exhausted")

    def page(self):
        self.pages += 1
        if self.pages > self.max_pages:
            raise FeedRejected("page budget exhausted")


def collect_pages(first_url, transport, budget):
    """Follow same-host pages. Validate every URL before the transport.

    A bad or repeated ``next_url``, or a spent budget, keeps the pages
    already accepted and reports the stop. It does not drop those pages and
    it does not mark the walk complete. The first URL is validated before
    any transport, including an injected one. An invalid first URL raises
    because there is no page to keep.
    """
    url = validate_next_url(first_url)
    pages = []
    seen = set()
    stop = None
    while url:
        if url in seen:
            stop = "duplicate_page"
            break
        seen.add(url)
        try:
            budget.page()
            budget.call()
        except FeedRejected:
            stop = "budget"
            break
        body = transport(url)
        if not isinstance(body, str):
            raise FeedRejected("transport did not return text")
        parsed = parse_response(body)
        pages.append(parsed)
        nxt = parsed.get("next_url")
        if not nxt:
            break
        try:
            url = validate_next_url(nxt)
        except FeedRejected:
            stop = "next_url_rejected"
            break
    if not pages and stop:
        raise FeedRejected(stop)
    return {"complete": stop is None, "pages": pages, "stop": stop}


def resolve_api_key(explicit):
    """One-shot key. Never stored and never returned in an error."""
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit or explicit != explicit.strip():
            raise FeedRejected("Massive API key is missing or unusable")
        if any(char in explicit for char in "\r\n\x00"):
            raise FeedRejected("Massive API key is missing or unusable")
        return explicit
    import os
    value = os.environ.get("MASSIVE_API_KEY")
    if not isinstance(value, str) or not value or value != value.strip():
        raise FeedRejected("Massive API key is missing or unusable")
    if any(char in value for char in "\r\n\x00"):
        raise FeedRejected("Massive API key is missing or unusable")
    return value


def https_get(url, api_key, timeout_seconds):
    """One GET. No redirects, no query key, no secret in the error."""
    import urllib.request
    assert_timeout(timeout_seconds)
    validate_next_url(url)
    key = resolve_api_key(api_key)
    opener = urllib.request.build_opener(
        _redirect_handler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": "Bearer " + key},
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            data = response.read(MAX_BYTES + 1)
    except FeedRejected:
        raise
    except Exception:
        raise FeedRejected("Massive request failed") from None
    if len(data) > MAX_BYTES:
        raise FeedRejected("response exceeded the byte budget")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise FeedRejected("Massive response was not text") from None


def _redirect_handler():
    import urllib.request

    class Handler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise FeedRejected("redirect refused")

    return Handler()


def fetch_aggregates(ticker, start, end, timeout_seconds, runtime_approval,
                     api_key=None, network_enabled=False, transport=None):
    """Fetch only with an explicit approval. Default is offline.

    A transport injected by a test is still not a live socket. ``https_get``
    runs only when the approval phrase matches, the network flag is true,
    and no transport was injected.
    """
    if transport is None and not network_enabled:
        raise Offline("Massive network is disabled")
    if runtime_approval != RUNTIME_APPROVAL:
        raise FeedRejected("live Massive fetch is not approved")
    assert_timeout(timeout_seconds)
    url = validate_next_url(build_request_url(ticker, start, end))
    if transport is not None:
        body = transport(url)
        if not isinstance(body, str):
            raise FeedRejected("transport did not return text")
        return body
    return https_get(url, api_key, timeout_seconds)


def read_url(url, api_key, timeout_seconds, transport=None):
    """Validate, then either use an injected transport or ``https_get``.

    ``fetch_aggregates`` also calls ``https_get`` for a single aggregates
    request. This function is the pagination reader. The URL check runs
    before either path, including an injected transport.
    """
    checked = validate_next_url(url)
    if transport is not None:
        body = transport(checked)
        if not isinstance(body, str):
            raise FeedRejected("transport did not return text")
        return body
    return https_get(checked, api_key, timeout_seconds)
