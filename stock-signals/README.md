# Stock signals: operation and recovery

Market data → deterministic Python rules → signals.json + report.md → Scriptable.
No ChatGPT task, model, API key, or chat response is needed to publish the feed.
The existing watchlist.json remains the sole source of tickers, Yahoo symbols,
names and positions. Existing signal rules and legacy JSON field names remain.

## What was wrong

- The four recorded scheduled runs on October 16, 19, 20 and 21 started at
  13:18–13:36 JST, versus the configured 09:05. A successful push-triggered run
  on October 22 completed in seconds. This supports scheduler delay rather than
  slow analysis. The newer early-morning schedule has no completed scheduled run
  in the inspected history, so its effectiveness was not established.
- The Python updater caught every stock's download error, emitted WAIT/D, then
  exited successfully. It wrote a new update timestamp even when nothing was
  fresh, and used the newest stock date to describe the whole watchlist.
- Every run excluded today's candle, even after close. There was one request
  per stock, no retry, no expiry, no overlap control, and no push-conflict retry.
- The saved Apple-style Scriptable script returned hard-coded October 9 signals
  on network error and requested only one refresh each morning. Its earlier
  loader lacked a cache-busting URL; a later conversation patch added one.
  The actual iPhone installation cannot be inspected from this repository.

## Publication

UTC cron expressions request runs at 16:17, 18:17 and 22:17 JST on weekdays,
plus 06:43 JST on weekday mornings. UTC avoids timezone interpretation ambiguity.
The afternoon feed is prepared for the next morning; later runs repair delayed
vendor publication. Watchlist/code changes on main also trigger publication.
Manual recovery: Actions → Update Stock Signals → Run workflow on main.

GitHub scheduling is best effort: runs can be delayed or dropped. This design
adds recovery opportunities and fails visibly; it cannot guarantee a deadline.
If a guaranteed 09:00 delivery becomes essential, use an independent scheduler
to invoke workflow_dispatch with a narrowly scoped credential.

The XTKS calendar determines holidays, year-end closures and session closes.
A close becomes required 45 minutes after the exchange closes (16:15 JST under
current hours). Before that deadline, the previous session remains valid.
expires_at is the *next session's* publication deadline, so Friday's healthy
feed remains valid over the weekend and Monday morning. It is intentionally
stale once the next required close is due.

Data downloads use bounded retries across Yahoo's query1 and query2 hosts.
These hosts are the same provider, not independent data sources. If either stock
is missing or behind the expected session, the global status is error. Its row
is WAIT/D with actionable=false; available historical prices are labelled stale.
The consumer blocks the whole watchlist until every row passes freshness checks.
Generated time describes the attempt, not data freshness. last_success_at,
per-stock market_data_date, expected_market_date, status, stale, actionable,
expires_at and run_url distinguish the two.

Feed writes use a temporary file and atomic replacement. Actions serializes runs,
recomputes from the latest main/watchlist when publishing, and retries push races.
It publishes failure status and a matching human-readable report before failing
the run, so failure notifications and Actions history remain meaningful.
If installation, tests or the runner itself fails before generation, the prior
feed still expires in the client. No in-repository check can run when Actions
itself never starts.

## iPhone installation (one time)

1. Copy the entire contents of Stock Signals.js into the existing Scriptable
   script. Keep its name so the existing Home Screen widget still selects it.
2. Run it once in Scriptable, then return to the Home Screen. Use a medium
   widget for two stocks, small for one or large for up to five.
3. Tap the widget to fetch again manually. Continue editing watchlist.json as
   before; do not edit generated signals.json or report.md.

The feed URL and legacy fields are unchanged, so older widget versions can
still parse it. **The stale/offline safeguard requires installing the new
script**; the older script does not know how to enforce expiry.

The new loader requests a unique URL, validates schema/expiry and every row, and
stores the last downloaded feed locally. On any network failure it displays
OFFLINE — DO NOT TRADE and suppresses recommendations; cache is context only.
Expired/error/legacy feeds show STALE DATA — DO NOT TRADE. It requests another
refresh after 30 minutes instead of deferring until the next morning.

iOS controls actual widget execution, including while offline. A frozen widget
cannot run new validation or repaint its warning. Check the visible “Valid until”
time, and tap to refresh before relying on any displayed signal. Refresh timing
is not guaranteed by refreshAfterDate.

No workflow uses ChatGPT automations. Old ChatGPT tasks should not write this
feed; any such external task still present in the account should be disabled.
Those legacy cloud tasks are not stored in this repository and were not modified.

## Checks and local use

Python 3.12; install requirements.txt, then:

    python -m unittest discover -s stock-signals -p 'test_*.py'
    node stock-signals/test_widget.cjs
    python stock-signals/update_signals.py

Tests use synthetic bars for session boundaries, holidays, year-end, after-close
inclusion, stale/partial data, provider retry and failure publication. JavaScript
tests exercise expiry, invalid schema, future timestamps and offline suppression.
PR checks never write the production feed. After merging, inspect the first
main-branch run and verify generated_at plus status in the raw feed.

Sources: [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[Scriptable refreshAfterDate](https://docs.scriptable.app/listwidget/#refreshafterdate),
[exchange_calendars](https://github.com/gerrymanoim/exchange_calendars).
