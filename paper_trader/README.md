# Offline paper ledger

Simulated long-only accounting for one human-approved scope. A higher cash
balance on a synthetic fixture is arithmetic on invented bars. It is not a
market result, a broker fill, or evidence that the rule makes money.

This package does not download data, open a socket, spawn a process, read
credentials, or import the day-trader skills. There is no broker object and
no paper-trading flag. `forward.py` has no callable feed. State files can be
written only under `paper_trader/demo-state`. A `..` path or a symlink is
refused. `--new` does not overwrite. A missing or stale bar pauses the fill
and leaves an open position unresolved; it does not book an exit.

## What is reused

No function, module, or output of the existing skills is imported or called.
`trader-scanner/scan.py`, `trader-entry/entry.py`, `trader-exit/exit.py`,
`trader-budget/size.py`, and `trader-risk/check.py` each embed HTTP clients
(`YAHOO_HOSTS` at `scan.py:83`, `yahoo_get` around `scan.py:235` and the same
pattern in entry and exit). Loading them would put network code on the
offline path. Their fill and loss rules also disagree with causal replay, so
forcing those scripts into this loop would be the wrong integration.

Rules copied as behavior, and reimplemented here in `Decimal`:

- Fifteen-minute opening range on three closed 5-minute bars, 09:30, 09:35,
  and 09:40. Range high is the max high. Range low is the min low.
- The candidate is the first later bar whose close is strictly above that
  high. A wick does not count. A close equal to the high does not count.
  That is `scan.py` `orb_hit` and `entry.py` `evaluate`, not stage 1.
- Stop is the opening-range midpoint, floored to the cent. Target is 2R,
  half-up to the cent. Risk under 0.05 percent of the fill is refused. Those
  are the `exit.py` `compute` rules for `or-mid` and `2R`.
- Whole shares, long only, no new signal after 15:30, flat-by attempt at the
  15:55 bar. At most three entries a day. A realized loss of $150 on the
  session blocks further entries. Those limits are the example config's
  counts, not its dollar-risk definition.

Intentionally not reused:

- Stage 1 (`orb_candidate`) treats a close at or above the highest range
  close as a candidate. That is not the high/low breakout. This engine
  requires highs and lows and does not promote a close-only chart.
- `scan_orb` keeps at most `MAX_CONFIRM` (200) names by price times volume
  and sorts hits by trigger time, then symbol. This engine's universe is the
  approved symbol list. Same-time names are ranked by how far the signal
  close is above the range high, then by symbol. Volume is ignored.
- `entry.py` books the signal bar's close as the price. That is a same-bar
  decision and fill. This engine fills the next bar's open.
- `size_trade` uses binary floats, a fixed dollar max loss, a 75 percent
  position cap in the example config, and an Alpaca bracket. This engine
  uses 0.5 percent of ledger equity, a 25 percent position cap, and cash.
- `check.py` counts each entry's planned stop loss against the $150 day
  limit before the outcome exists. This engine blocks new entries only after
  realized session pnl is less than or equal to -$150. Exits are never
  blocked by that stop. A gap can lose more than the planned 0.5 percent.
- The NYSE holiday list and the 13:00 early-close shift are not applied.
  `scan.py` only encodes 2026 and 2027, and importing it pulls in the HTTP
  client. The approved session dates are the calendar. A Saturday in that
  list would still be replayed if the fixture contains bars.
- `scan.py` can request a current-day `range=1d` chart and refuses a
  non-offline range longer than six days (`scan.py` around 950-967). That
  is a live-data limit. It is not used here, and it is not evidence that a
  60-day history endpoint works. No history is downloaded.
- The checked-in TSLA morning fixture is not read. A morning tape cannot
  support a full-day figure. Reports say so when 15:55 was not observed.

`trader-broker` and `trader-pelosi` are not imported.

## Replay rules

Bars are `America/New_York` wall times, `YYYY-MM-DDTHH:MM:SS`, with no
offset and no timezone database. Off-grid, premarket, and after-hours bars
are rejected. A batch is sorted by start, then symbol, before any decision,
so file order cannot peek ahead. A resume may only append bars strictly
after the stored watermark.

A bar is closed when `observed_at` is omitted (treated as the bar end) or
falls in `[end, end + 5 minutes]`. Earlier than the end means the bar is not
closed. Later than end + 5 minutes is stale. A stale, unclosed, or missing
bar writes a `pause` event, does not fill, and does not book an exit. The
position, if one is already open, stays open and is marked stale. A bad
price or two different bars with the same start block that symbol the same
way. Later bars for that symbol are ignored.

The signal is recorded when the signal bar is accepted. The fill, if any,
is the next bar's open plus one cent. The fill bar's high, low, and close
are not inputs to that decision. One signal per symbol per session: a missed
or refused fill does not get a later retry.

Inside one completed bar the order is fixed, because the tape delivers the
whole bar at once:

1. Exits already determined by the open (gap through the stop, gap through
   the target, or the 15:55 flat) for positions opened on an earlier bar.
2. New entries at that open, after ranking and cash reservation.
3. Intrabar stop and target checks, including on a position just opened.
   If both levels are touched and the open is between them, the stop is
   first. The fill is the stop or the target, not the bar's extreme. A gap
   fill is the open, then slippage. This can look better than a tick path
   that traded the wick; the report is a bar model, not a tick claim.

The close is never a fill. It is only a mark after the bar is accepted, and
only for a fresh position.

## Accounting

| Item | Value |
| --- | --- |
| Starting cash | $10,000.000 |
| Sizing equity | cash plus open cost bases, not marked prices |
| Planned risk | 0.5 percent of that equity |
| Position cap | 25 percent of that equity |
| Entries | 3 per session date |
| Daily stop | realized session pnl `<= -$150.000` blocks new entries |
| Run stop | realized pnl `<= -$300.000` blocks new entries |
| Entry price | next open + $0.01 |
| Exit price | raw level or gap open, minus $0.01 |
| Commission | $0.005 per share each side, exact thousandths |
| Shares | `floor(budget / per share)` using those prices and both commissions in the risk distance |

Planned risk can be exceeded by a gap. A non-positive slipped exit is not
invented: if open minus one cent would be below one cent, the fill stays at
the observed positive price and `slippage_capped` is true. Fees are not a
broker schedule.

Same-time candidates are ranked, then sized against remaining cash and
remaining entry slots. A zero-share name does not consume a slot. On a fresh
$10,000 account the 25 percent cap binds before cash for the first three
maximum positions (3 x 25 percent is 75 percent). Reservation is still
applied: each later name in the batch sees a smaller `shares_cash`.

## State

`StateStore` writes only under `paper_trader/demo-state`. `..`, a symlink,
and a path outside that directory are refused before open or replace.
`fcntl.flock` is non-blocking, and a second store in the same process is
refused. The lock is not stolen. A crash drops the flock. Writes go to a
temp file in that same directory, then `os.replace`. `--new` does not
replace an existing file. Resume replaces only that same regular file. A
truncated, empty, duplicate-key, or internally inconsistent file raises
`StateCorrupt` and is not replaced with a fresh $10,000 ledger. Resume
requires the same approval and the same prefix; an earlier suffix is a
`ReplayConflict`.

The file stores the inputs and a fingerprint of a full replay. A hand edit
that does not replay to the same cash, events, and positions fails closed.
A fully rewritten but self-consistent file cannot be distinguished from a
real replay; there is no signature.

The engine does not read the system clock. Event time is the bar's
`observed_at`. `decision_time` for an entry is the signal bar's end.

## Windows

The accounting rules are the same on every platform. Durability and path
checks are not.

- Locking: POSIX `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Windows
  `msvcrt.locking(fd, LK_NBLCK, 1)` from the start of the lock file, released
  with `LK_UNLCK`. Neither module is imported until a lock is taken, so
  importing the package does not require `fcntl` on Windows.
- Replace: `os.replace` in the same directory after `os.fsync` of the temp
  file. POSIX then fsyncs the directory. On the Windows run of `5bebda4`,
  opening the replaced file read-only and calling `os.fsync` raised errno 9
  after the name was already replaced, and the populated file stayed on
  disk. The flush now opens that file `O_RDWR` (and `O_BINARY` when that
  flag exists) and writes no bytes. Python documents Windows `os.fsync` as
  CRT `_commit`. Microsoft's `_commit` page says an invalid descriptor sets
  `EBADF` and does not state an access-mode rule. `FlushFileBuffers` requires
  `GENERIC_WRITE`. This code does not assert that `_commit` calls
  `FlushFileBuffers`. There is still no stdlib directory fsync, so a crash
  can lose the name. If the flush raises, the file is left in place and the
  error is `StateUnflushed`: that call does not resume it and does not write
  a new account over it.
- Reparse points: `os.path.islink`, `os.path.isjunction` (POSIX returns
  false), and `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` when that
  Windows stat field exists. Junctions use reparse tag
  `IO_REPARSE_TAG_MOUNT_POINT`, which `os.path.isjunction` reads.
- `os.O_NOFOLLOW` is used only when the interpreter defines it. It is not a
  documented Windows `os` flag. A reparse point created after the `lstat`
  and before `open` can still be followed on Windows.
- Lexical refusal, via `ntpath.splitroot`, covers UNC paths, `\\?\` and
  `\\.\` paths, drive-relative paths (`C:name`), alternate data streams
  (`name:stream`), reserved device names (`NUL`, `CON`, `COM1` through
  `COM9`, `LPT1` through `LPT9`, including an extension), and trailing dots
  or spaces. A file with `st_nlink > 1` is refused.
- Hard-link and in-process lock tests are in the suite. A second Windows
  handle is checked with `LK_NBLCK` only on `win32`, because POSIX `flock`
  is per process and a second descriptor in this process does not fail.
  Another process is not spawned.

Target check, not run from this revision, on native Windows PowerShell with
Python 3.12.10, from the repo root, after review. Do not use WSL.

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONNOUSERSITE = "1"
python -B -m unittest discover -s paper_trader/tests -t . -v
python -B -m paper_trader --new --fixture paper_trader/fixtures/synthetic_round_trip.json --state synthetic-round-trip.json --label synthetic --sessions 2026-11-02 --symbols SYN --approve orb15-5m-long-next-open-v1 --acknowledge-simulated
```

The Linux result for `c9f23cc30bec4cbbb957caaa551d354f38dcde79` does not
cover this Windows code. `forward.py` is still only the unimplemented marker.

## Reports

`label` is `synthetic` or `historical` as supplied by the caller.
`data_verified` is always false. `architecture` is `simulation`.
`profitability_claim` is always false. There is no broker flag to flip.
Realized pnl, unrealized pnl, equity, drawdown, and open positions are
reported separately. Unrealized pnl uses the last accepted close and is
omitted when any open position is stale, carried, or unpriced. A session is
`full_session` only when every approved symbol has a 15:55 bar, nothing was
rejected, and no position is open. Anything else is an incomplete tape, not
a full-day return.

## Run

One foreground command. There is no scheduler. The human passes the scope
once. Individual simulated fills are not approved one by one.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m paper_trader \
  --new \
  --fixture paper_trader/fixtures/synthetic_round_trip.json \
  --state synthetic-round-trip.json \
  --label synthetic \
  --sessions 2026-11-02 \
  --symbols SYN \
  --approve orb15-5m-long-next-open-v1 \
  --acknowledge-simulated
```

`--state` is a file name only. The ledger is created at
`paper_trader/demo-state/<name>`. `--resume` updates that same regular file.
The command prints a report and exits.

After a reviewer has read the diff, the bounded test command is:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s paper_trader/tests -t . -v
```

Run that from the repository root. `-B` and `PYTHONDONTWRITEBYTECODE` keep
bytecode out of this package and the skill folders. Tests write ledgers only
under `paper_trader/demo-state`. Do not point discovery at the repository
`tests/` directory; that runs the skill suite. The suite does not spawn a
process.

These commands were not executed while this package was written. No syntax
check, import, formatter, or unit test was run. `TESTS NOT RUN`.
