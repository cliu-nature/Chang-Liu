import json
import math
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
OUTPUT = BASE / "signals.json"
JST = timezone(timedelta(hours=9))


def fetch_chart(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?range=6mo&interval=1d&includePrePost=false&events=div%2Csplits"
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
    today_jst = datetime.now(JST).date()

    for i, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)
        # At the 09:05 run we deliberately use only completed sessions.
        if dt.date() >= today_jst:
            continue

        o = q["open"][i]
        h = q["high"][i]
        l = q["low"][i]
        c = q["close"][i]
        v = q["volume"][i]

        if None in (h, l, c):
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

    if len(bars) < 60:
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


def main():
    with WATCHLIST.open("r", encoding="utf-8") as f:
        watchlist = json.load(f)

    signals = []
    dates = []

    for stock in watchlist["stocks"]:
        try:
            bars = fetch_chart(stock["symbol"])
            signal, data_date = analyze(stock, bars)
            signals.append(signal)
            dates.append(data_date)
        except Exception as e:
            signals.append(
                {
                    "ticker": stock["ticker"],
                    "name": stock["name"],
                    "signal": "WAIT",
                    "position": stock.get("position", "WATCH"),
                    "trigger": "Data unavailable",
                    "invalid": "-",
                    "rationale": f"Automatic market-data update failed: {type(e).__name__}",
                    "strength": "D",
                }
            )

    now = datetime.now(JST)
    latest_date = max(dates) if dates else "unknown"

    output = {
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
        "market_data_through": f"{latest_date} close" if latest_date != "unknown" else "unknown",
        "market_status": "unchanged" if not dates else "updated",
        "signals": signals,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    # urllib.parse is imported here to keep the top import list compact.
    import urllib.parse
    main()
