import json
import math
import re
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
OHLCV = ("open", "high", "low", "close", "volume")
HISTORY_HOST = "finance.yahoo.co.jp"


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



def parse_published_history(html, dates):
    wanted = {day.replace("-", "/"): day for day in dates}
    bars = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        header = re.search(r"<th\b[^>]*>\s*(\d{4}/\d{2}/\d{2})\s*</th>", row)
        if not header or header[1] not in wanted:
            continue
        day = wanted[header[1]]
        values = re.findall(r'<span\b[^>]*class="[^"]*_StyledNumber__value[^"]*"[^>]*>([0-9,.]+)</span>', row)
        if day in bars or len(values) < 6:
            raise ValueError(f"Ambiguous Japanese daily history row: {day}")
        fields = [float(value.replace(",", "")) for value in values[:5]]
        bars[day] = dict(zip(OHLCV, fields))
    if set(bars) != set(dates):
        raise ValueError("Japanese history is missing required daily rows")
    return bars


def fetch_published_history(symbol, dates):
    url = f"https://{HISTORY_HOST}/quote/{quote(symbol, safe='')}/history"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 StockSignals/2.0",
        "Accept": "text/html",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=15) as response:
        return parse_published_history(response.read().decode("utf-8"), dates)


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
    fields = OHLCV
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

    placeholders = []
    errors = (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError)
    for host in HOSTS:
        diagnostic = {"source": host, "validation": "error", "interval": "1d"}
        attempts.append(diagnostic)
        result = None
        try:
            result, diagnostic["http_age"] = fetch_result(host, symbol)
            bars = validate_result(result, symbol, expected, now, diagnostic)
        except errors as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
            if result is not None and str(exc) == f"Incomplete OHLCV: {expected}":
                placeholders.append(result)
        else:
            diagnostic["validation"] = "ok"
            return bars, attempts

    if not placeholders:
        return None, attempts

    diagnostic = {"source": HISTORY_HOST, "validation": "error",
                  "interval": "1d", "fallback": "published_daily_history"}
    attempts.append(diagnostic)
    previous = session_date(calendar().previous_session(expected))
    try:
        published = fetch_published_history(symbol, (previous, expected))
        for result in placeholders:
            prior = validate_result(result, symbol, previous, now, {})
            if any(prior[-1][field] != published[previous][field] for field in OHLCV):
                continue
            quotes = result["indicators"]["quote"][0]
            index = next(i for i, timestamp in enumerate(result["timestamp"])
                         if yahoo_time(timestamp).date().isoformat() == expected)
            if any(quotes[field][index] is not None
                   and quotes[field][index] != published[expected][field] for field in OHLCV):
                continue
            recovered = {field: values.copy() for field, values in quotes.items()}
            for field in OHLCV:
                recovered[field][index] = published[expected][field]
            candidate = {**result, "indicators": {"quote": [recovered]}}
            bars = validate_result(candidate, symbol, expected, now, diagnostic)
            diagnostic["validation"] = "ok"
            diagnostic["matched_prior_session"] = previous
            return bars, attempts
        raise ValueError("Japanese and global Yahoo daily rows disagree")
    except errors as exc:
        diagnostic["error"] = f"{type(exc).__name__}: {exc}"
    return None, attempts
