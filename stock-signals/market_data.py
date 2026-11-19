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
    needed = [session_date(s) for s in cal.sessions_window(expected, -(HISTORY - 1))]
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
    for host in HOSTS:
        diagnostic = {"source": host, "validation": "error"}
        attempts.append(diagnostic)
        try:
            result, diagnostic["http_age"] = fetch_result(host, symbol)
            bars = validate_result(result, symbol, expected, now, diagnostic)
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        else:
            diagnostic["validation"] = "ok"
            return bars, attempts
    return None, attempts

