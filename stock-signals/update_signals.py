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
    now_jst = datetime.now(JST)
    today_jst = now_jst.date()
    market_closed = (now_jst.hour, now_jst.minute) >= (15, 40)

    for i, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)
        # Include today's session only after the Tokyo market has fully closed.
        # Weekend runs can safely include the last completed weekday.
        if dt.date() > today_jst or (dt.date() == today_jst and not market_closed):
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



def load_analyst_snapshot():
    if not ANALYSTS.exists():
        return {"stocks": {}, "expires_after_days": 0}
    try:
        with ANALYSTS.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stocks": {}, "expires_after_days": 0}


def shift_strength(value, steps):
    ladder = ["D", "C", "B-", "B", "B+", "A", "A+"]
    if value not in ladder or steps == 0:
        return value
    idx = max(0, min(len(ladder) - 1, ladder.index(value) + steps))
    return ladder[idx]


def analyst_overlay(ticker, close, bullish, bearish, tenkan, kijun, snapshot):
    item = (snapshot.get("stocks") or {}).get(str(ticker))
    if not item:
        return {
            "fresh": False,
            "stance": "UNAVAILABLE",
            "alignment": "TECHNICAL ONLY",
            "target_price": None,
            "target_upside_pct": None,
            "revision_momentum": "UNKNOWN",
            "as_of": None,
            "strength_delta": 0,
        }

    try:
        as_of = datetime.fromisoformat(item["as_of"]).date()
        expiry = int(snapshot.get("expires_after_days", 10))
        age_days = (datetime.now(JST).date() - as_of).days
        fresh = 0 <= age_days <= expiry
    except Exception:
        fresh = False

    target = item.get("target_price")
    upside = None
    if target is not None and close:
        upside = (float(target) / float(close) - 1.0) * 100.0

    if not fresh:
        return {
            "fresh": False,
            "stance": item.get("stance", "STALE"),
            "alignment": "STALE — IGNORED",
            "target_price": target,
            "target_upside_pct": round(upside, 1) if upside is not None else None,
            "revision_momentum": item.get("revision_momentum", "UNKNOWN"),
            "as_of": item.get("as_of"),
            "strength_delta": 0,
        }

    score = int(item.get("stance_score", 0))
    price_bullish = bullish and close >= tenkan and close >= kijun
    price_bearish = bearish or (close < tenkan and close < kijun)

    if score > 0:
        if price_bullish:
            alignment, delta = "CONFIRMS", 1
        elif price_bearish:
            alignment, delta = "DIVERGES", -1
        else:
            alignment, delta = "MIXED", 0
    elif score < 0:
        if price_bearish:
            alignment, delta = "CONFIRMS", 1
        elif price_bullish:
            alignment, delta = "DIVERGES", -1
        else:
            alignment, delta = "MIXED", 0
    else:
        alignment, delta = "NEUTRAL", 0

    return {
        "fresh": True,
        "stance": item.get("stance", "NEUTRAL"),
        "raw_consensus": item.get("raw_consensus"),
        "alignment": alignment,
        "target_price": target,
        "target_upside_pct": round(upside, 1) if upside is not None else None,
        "revision_momentum": item.get("revision_momentum", "UNKNOWN"),
        "as_of": item.get("as_of"),
        "buy_count": int(item.get("strong_buy", 0)) + int(item.get("buy", 0)),
        "neutral_count": int(item.get("neutral", 0)),
        "sell_count": int(item.get("sell", 0)) + int(item.get("strong_sell", 0)),
        "strength_delta": delta,
    }


def analyze(stock, bars, analyst_snapshot):
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


    # Temporary, auditable risk-watch overlay for the week of October 26-30.
    # Only use completed daily bars; never translate an intraday touch into a signal.
    plan = stock.get("weekly_plan") or {}
    if plan.get("valid_from", "9999") <= last["date"] <= plan.get("valid_through", "0000"):
        mode = plan.get("mode")
        if mode == "long_exit_watch" and has_core:
            warning = float(plan["warning_close"])
            risk = float(plan["risk_close"])
            breakout_level = float(plan["bullish_close"])
            close_near_low = (last["high"] > last["low"] and
                              (last["high"] - close) / (last["high"] - last["low"]) >= 0.75)
            rejection = (last["high"] >= breakout_level and close < breakout_level and close_near_low)
            if close < risk:
                signal, strength = "REDUCE", "B"
                trigger = f"Exit risk: close < {fmt_yen(risk)}"
                invalid = f"Recover > {fmt_yen(warning)}"
                rationale = "Long position risk elevated: closing price broke the deeper risk level."
            elif close < warning:
                signal, strength = "REDUCE", "C"
                trigger = f"Exit watch: close < {fmt_yen(warning)}"
                invalid = f"Recover > {fmt_yen(warning)}"
                rationale = "Long position showing weakness; reassess risk before the next session."
            elif close > breakout_level:
                signal, strength = "HOLD", "B+"
                trigger = f"Breakout holds > {fmt_yen(breakout_level)}"
                invalid = f"Exit watch < {fmt_yen(warning)}"
                rationale = "Price confirmed a new closing breakout; monitor follow-through, not an automatic add."
            else:
                signal, strength = "HOLD", "C" if rejection else "B-"
                trigger = f"Exit watch < {fmt_yen(warning)}"
                invalid = f"Bullish > {fmt_yen(breakout_level)}"
                rationale = ("Intraday breakout rejected; closed near session low. Protect long-position risk."
                             if rejection else
                             "Holding above warning level; watch for close below warning or renewed breakout.")
        elif mode == "short_confirmation_watch" and not has_core:
            watch = float(plan["watch_below"])
            confirm = float(plan["confirm_below"])
            invalidate = float(plan["invalid_above"])
            if close > invalidate:
                signal, strength = "WAIT", "C"
                trigger = f"No short above {fmt_yen(invalidate)}"
                invalid = f"Watch again < {fmt_yen(watch)}"
                rationale = "Upside breakout contradicts the short thesis; no bearish confirmation."
            elif (close < confirm and prev["close"] < confirm and
                  close < kijun and volume_ratio >= 1.10):
                signal, strength = "SHORT", "B"
                trigger = f"2 closes < {fmt_yen(confirm)}"
                invalid = f"Breakdown invalid > {fmt_yen(watch)}"
                rationale = "Bearish breakdown confirmed by two closes, Kijun and volume; review short risks."
            elif close < watch:
                signal, strength = "WAIT", "C"
                trigger = f"Confirm < {fmt_yen(confirm)}"
                invalid = f"No short > {fmt_yen(invalidate)}"
                rationale = "Initial bearish warning only; wait for sustained downside confirmation."
            else:
                signal, strength = "WAIT", "C"
                trigger = f"Short watch < {fmt_yen(watch)}"
                invalid = f"No short > {fmt_yen(invalidate)}"
                rationale = "Price has not broken the watch level; shorting the uptrend is premature."

    analyst = analyst_overlay(
        stock["ticker"], close, bullish, bearish, tenkan, kijun, analyst_snapshot
    )
    strength = shift_strength(strength, analyst.get("strength_delta", 0))

    if analyst.get("fresh"):
        stance = analyst.get("stance", "NEUTRAL")
        alignment = analyst.get("alignment", "MIXED")
        rationale += f" Analyst stance: {stance}; price/analyst alignment: {alignment}."

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
        "data_as_of": last["date"],
        "weekly_plan_through": plan.get("valid_through") if plan else None,
        "analyst_stance": analyst.get("stance"),
        "analyst_alignment": analyst.get("alignment"),
        "analyst_fresh": analyst.get("fresh"),
        "analyst_as_of": analyst.get("as_of"),
        "analyst_target_price": analyst.get("target_price"),
        "analyst_target_upside_pct": analyst.get("target_upside_pct"),
        "analyst_revision_momentum": analyst.get("revision_momentum"),
        "analyst_buy_count": analyst.get("buy_count"),
        "analyst_neutral_count": analyst.get("neutral_count"),
        "analyst_sell_count": analyst.get("sell_count"),
    }, last["date"]


def main():
    with WATCHLIST.open("r", encoding="utf-8") as f:
        watchlist = json.load(f)

    analyst_snapshot = load_analyst_snapshot()

    signals = []
    dates = []

    for stock in watchlist["stocks"]:
        try:
            bars = fetch_chart(stock["symbol"])
            signal, data_date = analyze(stock, bars, analyst_snapshot)
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
        "generated_at": now.isoformat(timespec="seconds"),
        "market_data_through": f"{latest_date} close" if latest_date != "unknown" else "unknown",
        "market_status": ("partial" if dates and len(dates) != len(watchlist["stocks"]) else
                          "unavailable" if not dates else "updated"),
        "stale": not bool(dates) or any((now.date() - datetime.fromisoformat(d).date()).days > 4 for d in dates),
        "signals": signals,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    # urllib.parse is imported here to keep the top import list compact.
    import urllib.parse
    main()
