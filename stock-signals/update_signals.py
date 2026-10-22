import json
import math
import os
import urllib.request
import urllib.parse
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
OUTPUT = BASE / "signals.json"
JST = timezone(timedelta(hours=9))


def fetch_chart(symbol, expected_date=None):
    error = None
    latest = None
    for attempt in range(3):
        for host in ("query1", "query2"):
            try:
                bars = download_chart(symbol, expected_date, host)
                if expected_date and bars[-1]["date"] != expected_date:
                    if latest is None or bars[-1]["date"] > latest[-1]["date"]:
                        latest = bars
                    raise ValueError("Provider has not published expected close")
                return bars
            except Exception as exc:
                error = exc
        if attempt < 2:
            time.sleep(2 ** attempt)
    if latest:
        return latest  # build_feed labels it stale and disables recommendations.
    raise RuntimeError(f"Market data unavailable for {symbol}") from error


def download_chart(symbol, expected_date, host):
    url = (
        f"https://{host}.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?range=1y&interval=1d&includePrePost=false&events=div%2Csplits"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 StockSignals/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)

    result = payload["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    adj = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")

    bars = []
    expected_date = expected_date or session_context(datetime.now(JST))[0]

    for i, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)
        if dt.date().isoformat() > expected_date:
            continue

        o = q["open"][i]
        h = q["high"][i]
        l = q["low"][i]
        c = q["close"][i]
        v = q["volume"][i]

        if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (h, l, c)):
            continue
        if h < l or not l <= c <= h or v is None or not math.isfinite(v) or v < 0:
            continue

        bars.append(
            {
                "date": dt.date().isoformat(),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v or 0,
                "adjclose": adj[i] if adj and i < len(adj) else c,
            }
        )

    bars.sort(key=lambda bar: bar["date"])
    if len({bar["date"] for bar in bars}) != len(bars):
        raise ValueError("Duplicate daily bars")
    if len(bars) < 78:
        raise RuntimeError(f"Not enough daily data for {symbol}: {len(bars)} bars")

    return bars


def midpoint(bars, window, end_idx):
    if end_idx - window + 1 < 0:
        return None
    chunk = bars[end_idx - window + 1 : end_idx + 1]
    return (max(x["high"] for x in chunk) + min(x["low"] for x in chunk)) / 2


def avg(values):
    vals = [x for x in values if x is not None]
    return sum(vals) / len(vals) if vals else None


def fmt_yen(x):
    if x is None or not math.isfinite(x):
        return "-"
    return f"¥{x:,.0f}"


def analyze(stock, bars):
    i = len(bars) - 1
    last = bars[i]
    prev = bars[i - 1]

    tenkan = midpoint(bars, 9, i)
    kijun = midpoint(bars, 26, i)

    # Standard Ichimoku cloud at the current date uses spans calculated 26 sessions earlier.
    src = i - 26
    span_a = None
    span_b = None
    if src >= 51:
        t_src = midpoint(bars, 9, src)
        k_src = midpoint(bars, 26, src)
        span_a = (t_src + k_src) / 2 if t_src is not None and k_src is not None else None
        span_b = midpoint(bars, 52, src)

    cloud_top = max(x for x in (span_a, span_b) if x is not None) if (span_a is not None or span_b is not None) else None
    cloud_bottom = min(x for x in (span_a, span_b) if x is not None) if (span_a is not None or span_b is not None) else None

    recent = bars[max(0, i - 20) : i]
    recent_high = max(x["high"] for x in recent)
    recent_low = min(x["low"] for x in recent)

    avg_vol20 = avg([x["volume"] for x in bars[max(0, i - 20) : i]])
    volume_ratio = (last["volume"] / avg_vol20) if avg_vol20 else 1.0

    close = last["close"]
    position = str(stock.get("position", "WATCH")).upper()
    has_core = position not in ("WATCH", "0", "NONE", "-") and not position.startswith("-")

    breakout = close > recent_high and close > tenkan and close > kijun
    breakdown = close < recent_low and close < tenkan and close < kijun

    bullish = (
        close > kijun
        and tenkan >= kijun
        and (cloud_top is None or close >= cloud_top)
    )
    bearish = (
        close < kijun
        and tenkan <= kijun
        and (cloud_bottom is None or close <= cloud_bottom)
    )

    # Deterministic signal framework matching the widget rules.
    if breakout:
        signal = "ADD" if has_core else "LONG"
        strength = "A" if volume_ratio >= 1.15 else "B+"
        trigger = f"Breakout confirmed > {fmt_yen(recent_high)}"
        invalid = f"Watch < {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)}, above the prior 20-session high "
            f"and above Tenkan/Kijun."
        )
    elif breakdown:
        signal = "REDUCE" if has_core else "SHORT"
        strength = "A" if volume_ratio >= 1.15 else "B+"
        trigger = f"Breakdown confirmed < {fmt_yen(recent_low)}"
        invalid = f"Watch > {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)}, below the prior 20-session low "
            f"and below Tenkan/Kijun."
        )
    elif bullish:
        signal = "HOLD" if has_core else "WAIT"
        strength = "B+"
        trigger = f"Add/Long > {fmt_yen(recent_high)}"
        invalid = f"Watch < {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} with a constructive trend above "
            f"Kijun; a fresh breakout is still required for a stronger entry."
        )
    elif bearish:
        signal = "HOLD" if has_core else "WAIT"
        strength = "B"
        trigger = f"Short/Reduce < {fmt_yen(recent_low)}"
        invalid = f"Watch > {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} in a weak technical regime; "
            f"wait for a confirmed breakdown before acting."
        )
    else:
        signal = "HOLD" if has_core else "WAIT"
        strength = "C"
        trigger = f"Long > {fmt_yen(recent_high)} / Short < {fmt_yen(recent_low)}"
        invalid = f"Kijun {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} in a mixed/range regime with no "
            f"high-conviction breakout or breakdown."
        )

    return {
        "ticker": stock["ticker"],
        "name": stock["name"],
        "signal": signal,
        "position": stock.get("position", "WATCH"),
        "trigger": trigger,
        "invalid": invalid,
        "rationale": rationale,
        "strength": strength,
        "close": round(close, 2),
        "tenkan": round(tenkan, 2) if tenkan is not None else None,
        "kijun": round(kijun, 2) if kijun is not None else None,
        "cloud_top": round(cloud_top, 2) if cloud_top is not None else None,
        "cloud_bottom": round(cloud_bottom, 2) if cloud_bottom is not None else None,
        "volume_ratio_20d": round(volume_ratio, 2),
    }, last["date"]


def session_context(now):
    """Latest close due after a 45-minute vendor publication buffer, and expiry."""
    import exchange_calendars as xcals
    calendar = xcals.get_calendar("XTKS")
    schedule = calendar.schedule.loc[
        (now - timedelta(days=40)).date().isoformat():
        (now + timedelta(days=40)).date().isoformat()
    ]
    deadlines = [(idx.date().isoformat(), close.to_pydatetime() + timedelta(minutes=45))
                 for idx, close in schedule["close"].items()]
    completed = [(day, due) for day, due in deadlines if due <= now]
    expected = completed[-1][0]
    expires = next(due for day, due in deadlines if due > now)
    return expected, expires


def blocked(stock, previous, reason):
    result = dict(previous or {})
    result.update(ticker=stock["ticker"], name=stock["name"],
                  position=stock.get("position", "WATCH"), signal="WAIT",
                  strength="D", trigger="DATA UNAVAILABLE - DO NOT TRADE",
                  invalid="-", rationale=reason, status="error", stale=True,
                  actionable=False, error=reason)
    return result


def build_feed(watchlist, previous, now, fetcher=fetch_chart):
    expected, expires = session_context(now)
    stocks = watchlist["stocks"]
    if not stocks or len({s["ticker"] for s in stocks}) != len(stocks):
        raise ValueError("Watchlist must contain unique tickers")
    old = {s["ticker"]: s for s in previous.get("signals", [])}
    signals = []
    for stock in stocks:
        try:
            bars = fetcher(stock["symbol"], expected)
            signal, day = analyze(stock, bars)
            signal.update(market_data_date=day,
                          last_success_at=now.isoformat() if day == expected else old.get(stock["ticker"], {}).get("last_success_at"),
                          status="ok", stale=False, actionable=True, error=None)
            if day != expected:
                signal = blocked(stock, signal, f"Expected {expected} close; received {day}")
            signals.append(signal)
        except Exception as exc:
            signals.append(blocked(stock, old.get(stock["ticker"]),
                                   f"Market-data fetch/analysis failed: {type(exc).__name__}"))
    healthy = all(s["status"] == "ok" for s in signals)
    dates = [s.get("market_data_date") for s in signals]
    # Never describe mixed dates as a common fresh close.
    common = min(dates) if all(dates) else None
    return {
        "schema_version": 2,
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
        "generated_at": now.isoformat(),
        "last_success_at": now.isoformat() if healthy else previous.get("last_success_at"),
        "expected_market_date": expected,
        "market_data_through": f"{common} close" if common else "unknown",
        "market_status": "updated" if healthy else "unavailable",
        "status": "ok" if healthy else "error",
        "stale": not healthy,
        "actionable": healthy,
        "expires_at": expires.astimezone(JST).isoformat(),
        "source": "Yahoo Finance chart API (query1/query2, same provider)",
        "run_url": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                    f"{os.environ['GITHUB_RUN_ID']}" if os.getenv("GITHUB_RUN_ID") else None),
        "signals": signals,
    }


def main():
    try:
        previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    except (ValueError, OSError):
        previous = {}
    now = datetime.now(JST)
    try:
        watchlist = json.loads(WATCHLIST.read_text(encoding="utf-8"))
        output = build_feed(watchlist, previous, now)
    except Exception as exc:
        # Configuration/calendar failure also revokes previously published signals.
        output = dict(previous)
        output.update(schema_version=2, generated_at=now.isoformat(),
                      updated=now.strftime("%Y-%m-%d %H:%M JST"),
                      status="error", stale=True, actionable=False,
                      market_status="unavailable", expires_at=now.isoformat(),
                      error=f"Pipeline failed: {type(exc).__name__}")
        output["signals"] = [blocked(s, s, output["error"]) for s in previous.get("signals", [])]
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")
    temp.replace(OUTPUT)
    report = ["# Stock signals", "",
              f"Status: {output['status']} | Generated: {output['generated_at']}",
              f"Market data: {output.get('market_data_through', 'unknown')}",
              f"Expires: {output['expires_at']}", ""]
    if not output["actionable"]:
        report += ["**STALE / UNAVAILABLE — DO NOT TRADE**", ""]
    for signal in output.get("signals", []):
        report += [f"## {signal['ticker']} {signal['name']}",
                   f"{signal['signal']} | Position: {signal['position']} | Strength: {signal['strength']}",
                   signal["trigger"], signal["invalid"], signal["rationale"], ""]
    OUTPUT.with_name("report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({key: output.get(key) for key in
                      ("status", "expected_market_date", "market_data_through", "expires_at")}))
    return 0 if output["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
