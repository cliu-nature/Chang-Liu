import json
import math
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote
from urllib.request import Request, urlopen

import exchange_calendars as xcals

JST = timezone(timedelta(hours=9))
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
HISTORY = 78  # 52 sessions for Span B, displaced 26 sessions
PUBLICATION_WINDOW = timedelta(minutes=75)
FINALIZATION_BUFFER = timedelta(minutes=10)
INTRADAY_INTERVAL = "5m"
INTRADAY_RANGE = "5d"
INTRADAY_STEP = timedelta(minutes=5)


@lru_cache(maxsize=1)
def calendar():
    return xcals.get_calendar("XTKS")


def session_date(session):
    return session.date().isoformat()


def completed_session(now):
    now = now.astimezone(JST)
    cal = calendar()
    session = cal.date_to_session(now.date().isoformat(), direction="previous")
    if now < cal.session_close(session) + FINALIZATION_BUFFER:
        session = cal.previous_session(session)
    return session


def market_context(now):
    cal = calendar()
    completed = completed_session(now)
    next_session = cal.next_session(completed)
    next_due = cal.session_close(next_session) + PUBLICATION_WINDOW
    today = now.astimezone(JST).date().isoformat()
    if not cal.is_session(today):
        status = "closed"
    elif now < cal.session_open(today):
        status = "pre_open"
    elif now < cal.session_close(today):
        status = "trading_day"
    elif session_date(completed) != today:
        status = "finalizing"
    else:
        status = "closed"
    return {
        "expected_market_date": session_date(completed),
        "market_status": status,
        "next_update_due_at": next_due.to_pydatetime().astimezone(JST).isoformat(),
    }


def session_lag(actual, expected):
    if actual is None:
        return None
    if actual >= expected:
        return 0
    return len(calendar().sessions_in_range(actual, expected)) - int(calendar().is_session(actual))


def fetch_result(host, symbol):
    url = (
        f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
        f"?range=6mo&interval=1d&includePrePost=false&_={time.time_ns()}"
    )
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 StockSignals/2.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    })
    with urlopen(req, timeout=15) as response:
        chart = json.load(response)["chart"]
        if chart.get("error"):
            raise ValueError(f"Yahoo chart error: {chart['error']}")
        return chart["result"][0], response.headers.get("Age")



def fetch_intraday_result(host, symbol):
    url = (
        f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
        f"?range={INTRADAY_RANGE}&interval={INTRADAY_INTERVAL}"
        f"&includePrePost=false&_={time.time_ns()}"
    )
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 StockSignals/2.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    })
    with urlopen(req, timeout=15) as response:
        chart = json.load(response)["chart"]
        if chart.get("error"):
            raise ValueError(f"Yahoo intraday chart error: {chart['error']}")
        return chart["result"][0], response.headers.get("Age")


def aggregate_intraday(result, symbol, expected, now, diagnostic):
    meta = result["meta"]
    if meta.get("symbol") != symbol or meta.get("exchangeTimezoneName") != "Asia/Tokyo":
        raise ValueError("Unexpected Yahoo intraday symbol or exchange timezone")
    if meta.get("currency") != "JPY":
        raise ValueError("Expected intraday JPY data")

    timestamps = result.get("timestamp") or []
    quote_series = result["indicators"]["quote"][0]
    fields = ("open", "high", "low", "close", "volume")
    if any(len(quote_series[field]) != len(timestamps) for field in fields):
        raise ValueError("Misaligned Yahoo intraday timestamp and quote arrays")

    rows = []
    for index, timestamp in enumerate(timestamps):
        dt = yahoo_time(timestamp)
        if dt.date().isoformat() != expected:
            continue
        values = {field: quote_series[field][index] for field in fields}
        # Yahoo can include empty placeholder intraday rows. Ignore only fully empty rows.
        if all(value is None for value in values.values()):
            continue
        if any(type(values[field]) not in (int, float) or not math.isfinite(values[field])
               for field in ("open", "high", "low", "close")):
            raise ValueError(f"Incomplete intraday OHLC: {expected}")
        volume = values["volume"]
        if volume is None:
            volume = 0
        if type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0:
            raise ValueError(f"Invalid intraday volume: {expected}")
        if not (values["low"] > 0
                and values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            raise ValueError(f"Invalid intraday OHLC: {expected}")
        rows.append((dt, values["open"], values["high"], values["low"], values["close"], volume))

    if not rows:
        raise ValueError(f"No intraday bars for expected session: {expected}")

    rows.sort(key=lambda row: row[0])
    close_time = calendar().session_close(expected).to_pydatetime().astimezone(JST)
    diagnostic["intraday_first_timestamp"] = rows[0][0].isoformat()
    diagnostic["intraday_last_timestamp"] = rows[-1][0].isoformat()
    diagnostic["intraday_bar_count"] = len(rows)

    # A 5-minute bar is stamped at its start. The last complete regular-session
    # bar should therefore begin no earlier than 5 minutes before the close.
    if rows[-1][0] < close_time - INTRADAY_STEP:
        raise ValueError(f"Intraday session not complete: {expected}")

    quote_time = yahoo_time(meta["regularMarketTime"])
    diagnostic["intraday_regular_market_time"] = quote_time.isoformat()
    if quote_time < close_time or quote_time > now.astimezone(JST):
        raise ValueError("Yahoo intraday quote has no completed-session timestamp")

    bar = {
        "date": expected,
        "open": rows[0][1],
        "high": max(row[2] for row in rows),
        "low": min(row[3] for row in rows),
        "close": rows[-1][4],
        "volume": sum(row[5] for row in rows),
    }
    if bar["volume"] <= 0:
        raise ValueError(f"Intraday volume unavailable: {expected}")
    return bar


def yahoo_time(timestamp):
    return datetime.fromtimestamp(timestamp, tz=JST)


def validate_result(result, symbol, expected, now, diagnostic):
    meta = result["meta"]
    if meta.get("symbol") != symbol or meta.get("exchangeTimezoneName") != "Asia/Tokyo":
        raise ValueError("Unexpected Yahoo symbol or exchange timezone")
    if meta.get("dataGranularity") != "1d" or meta.get("currency") != "JPY":
        raise ValueError("Expected daily JPY data")

    timestamps = result["timestamp"]
    quote_series = result["indicators"]["quote"][0]
    fields = ("open", "high", "low", "close", "volume")
    if any(len(quote_series[field]) != len(timestamps) for field in fields):
        raise ValueError("Misaligned Yahoo timestamp and quote arrays")

    cal = calendar()
    first = cal.session_offset(expected, -(HISTORY - 1))
    needed = [session_date(s) for s in cal.sessions_in_range(first, expected)]
    bars = {}
    previous_date = ""
    diagnostic["last_timestamp"] = yahoo_time(timestamps[-1]).isoformat()
    for index, timestamp in enumerate(timestamps):
        day = yahoo_time(timestamp).date().isoformat()
        if day <= previous_date or not cal.is_session(day):
            raise ValueError(f"Duplicate, out-of-order or non-session bar: {day}")
        previous_date = day
        if day > now.astimezone(JST).date().isoformat():
            raise ValueError(f"Future bar: {day}")
        if day > expected:
            diagnostic["excluded_unfinished_bar"] = day
            continue

        diagnostic["latest_bar"] = day
        if day < needed[0]:
            continue
        bar = {field: quote_series[field][index] for field in fields}
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in bar.values()):
            raise ValueError(f"Incomplete OHLCV: {day}")
        if (bar["low"] <= 0 or bar["volume"] <= 0
                or not bar["low"] <= min(bar["open"], bar["close"])
                <= max(bar["open"], bar["close"]) <= bar["high"]):
            raise ValueError(f"Invalid OHLCV: {day}")
        bars[day] = {"date": day, **bar}

    # A chart can contain an opening-stamped provisional daily bar after close.
    # When Yahoo's latest quote belongs to the expected session, require evidence
    # of the closing auction and agreement with the daily close.
    if expected in bars:
        quote_time = yahoo_time(meta["regularMarketTime"])
        diagnostic["regular_market_time"] = quote_time.isoformat()
        if not cal.session_close(expected) <= quote_time <= now:
            raise ValueError("Yahoo quote has no completed-session timestamp")
        if (quote_time.date().isoformat() == expected
                and not math.isclose(meta["regularMarketPrice"], bars[expected]["close"],
                                     rel_tol=1e-7, abs_tol=0.001)):
            raise ValueError("Daily close disagrees with Yahoo's closing quote")
    if missing := [day for day in needed if day not in bars]:
        if all(day > diagnostic.get("latest_bar", "") for day in missing):
            diagnostic["validation"] = "behind"
        raise ValueError(f"Missing expected sessions: {', '.join(missing)}")
    return list(bars.values())


def fetch_chart(symbol, expected, now):
    attempts = []

    # Primary path: validated Yahoo daily OHLCV.
    for host in HOSTS:
        diagnostic = {"source": host, "validation": "error", "interval": "1d"}
        attempts.append(diagnostic)
        try:
            result, diagnostic["http_age"] = fetch_result(host, symbol)
            bars = validate_result(result, symbol, expected, now, diagnostic)
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        else:
            diagnostic["validation"] = "ok"
            return bars, attempts

    # Recovery path: Yahoo occasionally publishes a placeholder/null daily bar
    # while its intraday history is already complete. Only recover that specific
    # failure mode; a genuinely lagging daily feed must remain stale.
    recoverable = any(
        "Incomplete OHLCV" in attempt.get("error", "")
        for attempt in attempts
        if attempt.get("interval") == "1d"
    )
    if not recoverable:
        return None, attempts

    # Rebuild only the expected session from 5-minute bars and join it to
    # validated prior daily history.
    previous = session_date(calendar().previous_session(expected))
    for host in HOSTS:
        diagnostic = {
            "source": host,
            "validation": "error",
            "interval": INTRADAY_INTERVAL,
            "fallback": "intraday_aggregate",
        }
        attempts.append(diagnostic)
        try:
            daily_result, diagnostic["daily_http_age"] = fetch_result(host, symbol)
            prior_bars = validate_result(
                daily_result, symbol, previous, now, diagnostic.setdefault("daily", {})
            )
            intraday_result, diagnostic["http_age"] = fetch_intraday_result(host, symbol)
            latest_bar = aggregate_intraday(
                intraday_result, symbol, expected, now, diagnostic
            )
            bars = prior_bars[-(HISTORY - 1):] + [latest_bar]
            if len(bars) < HISTORY:
                raise ValueError(f"Not enough combined history for {symbol}")
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        else:
            diagnostic["validation"] = "ok"
            return bars, attempts

    return None, attempts

