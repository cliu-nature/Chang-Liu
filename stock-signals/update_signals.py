import argparse
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from market_data import HOSTS, JST, fetch_chart, market_context, session_lag

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
ANALYSTS = BASE / "analyst_snapshot.json"
OUTPUT = BASE / "signals.json"


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
    return f"¥{x:,.2f}".rstrip("0").rstrip(".")



def shift_strength(value, steps):
    ladder = ["D", "C", "B-", "B", "B+", "A", "A+"]
    if value not in ladder or steps == 0:
        return value
    idx = max(0, min(len(ladder) - 1, ladder.index(value) + steps))
    return ladder[idx]


def analyst_overlay(ticker, close, bullish, bearish, tenkan, kijun, snapshot, now):
    item = (snapshot.get("stocks") or {}).get(str(ticker))
    if not item:
        return {
            "fresh": False,
            "stance": "UNAVAILABLE",
            "alignment": "NEUTRAL",
            "target_price": None,
            "target_upside_pct": None,
            "revision_momentum": "UNKNOWN",
            "as_of": None,
            "strength_delta": 0,
        }

    try:
        as_of = datetime.fromisoformat(item["as_of"]).date()
        expiry = int(snapshot.get("expires_after_days", 10))
        age_days = (now.astimezone(JST).date() - as_of).days
        fresh = 0 <= age_days <= expiry
    except Exception:
        fresh = False

    target = item.get("target_price")
    upside = None
    if target is not None and close:
        upside = (float(target) / float(close) - 1.0) * 100.0

    score = int(item.get("stance_score", 0))
    price_bullish = bullish and close >= tenkan and close >= kijun
    price_bearish = bearish or (close < tenkan and close < kijun)

    if not fresh:
        alignment, delta = "STALE — IGNORED", 0
    elif score > 0:
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
        "fresh": fresh,
        "stance": item.get("stance", "NEUTRAL") if fresh else "EXPIRED",
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


def analyze(stock, bars, analyst_snapshot, now):
    i = len(bars) - 1
    last = bars[i]

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
    is_short = position.startswith("-") and position != "-"
    has_core = position not in ("WATCH", "0", "NONE", "-") and not is_short
    neutral_signal = "HOLD" if has_core or is_short else "WAIT"

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
        signal = "COVER" if is_short else "ADD" if has_core else "LONG"
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
        signal = neutral_signal
        strength = "B+"
        trigger = f"Add/Long > {fmt_yen(recent_high)}"
        invalid = f"Watch < {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} with a constructive trend above "
            f"Kijun; a fresh breakout is still required for a stronger entry."
        )
    elif bearish:
        signal = neutral_signal
        strength = "B"
        trigger = f"Short/Reduce < {fmt_yen(recent_low)}"
        invalid = f"Watch > {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} in a weak technical regime; "
            f"wait for a confirmed breakdown before acting."
        )
    else:
        signal = neutral_signal
        strength = "C"
        trigger = f"Long > {fmt_yen(recent_high)} / Short < {fmt_yen(recent_low)}"
        invalid = f"Kijun {fmt_yen(kijun)}"
        rationale = (
            f"{last['date']} closed at {fmt_yen(close)} in a mixed/range regime with no "
            f"high-conviction breakout or breakdown."
        )


    add_above = None
    reduce_below = None
    if has_core:
        add_above = max(recent_high, tenkan, kijun)
        reduce_below = min(recent_low, tenkan, kijun)
        trigger = f"收盘 加仓 >{fmt_yen(add_above)} · 减仓 <{fmt_yen(reduce_below)}"

    short_only = stock.get("strategy") == "short_only" and not has_core
    if short_only:
        short_level = min(recent_low, tenkan, kijun)
        if signal != "COVER":
            trigger = f"Short: close < {fmt_yen(short_level)}"
            invalid = f"Kijun watch {fmt_yen(kijun)}"
        if not breakdown and signal != "COVER":
            signal = neutral_signal
            strength = "B" if bearish else "C"
            rationale = (
                f"{last['date']} closed at {fmt_yen(close)}. "
                f"No confirmed short entry; watch for a daily close below "
                f"{fmt_yen(short_level)} (prior 20-session low, Tenkan and Kijun)."
            )

    analyst = analyst_overlay(
        stock["ticker"], close, bullish or breakout, bearish or breakdown,
        tenkan, kijun, analyst_snapshot, now
    )
    if not short_only or signal in ("SHORT", "COVER"):
        strength = shift_strength(strength, analyst.get("strength_delta", 0))

    if analyst.get("fresh"):
        stance = analyst.get("stance", "NEUTRAL")
        alignment = analyst.get("alignment", "MIXED")
        rationale += f" Analyst stance: {stance}; price/analyst alignment: {alignment}."

    return {
        "ticker": stock["ticker"],
        "name": stock["name"],
        "signal": signal,
        "strategy": stock.get("strategy"),
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
        "weekly_plan_through": None,
        "prior_20_session_high": recent_high,
        "prior_20_session_low": recent_low,
        "add_above": add_above,
        "reduce_below": reduce_below,
        "breakout": breakout,
        "breakdown": breakdown,
        "trend_regime": "bullish" if bullish else "bearish" if bearish else "mixed",
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
    }


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_market_date(value):
    if value in (None, "unknown"):
        return None
    return date.fromisoformat(value.removesuffix(" close")).isoformat()


def published_date(feed):
    return parse_market_date(feed.get("market_data_through"))


def previous_watermark(feed):
    dates = [published_date(feed)]
    dates.extend(parse_market_date(row.get("data_as_of")) for row in feed.get("signals", []))
    return max((day for day in dates if day), default=None)


def update_feed(now, previous, watchlist_path=WATCHLIST, analyst_path=ANALYSTS):
    context = {}
    expected = None
    results = []
    diagnostics = {}
    failed = []
    blocked = []
    status = "error"
    error = None
    previous_date = published_date(previous)
    watermark = previous_watermark(previous)
    source = previous.get("source", "yahoo_finance" if previous_date else None)

    try:
        context = market_context(now)
        expected = context["expected_market_date"]
        print(f"Now JST={now.isoformat()} expected={expected} previous={previous_date} watermark={watermark}")
        stocks = read_json(watchlist_path)["stocks"]
        if not stocks or len({stock["ticker"] for stock in stocks}) != len(stocks):
            raise ValueError("Watchlist must contain distinct stocks")
        try:
            snapshot = read_json(analyst_path)
            if not isinstance(snapshot.get("stocks"), dict):
                raise ValueError("Invalid analyst snapshot")
        except (OSError, ValueError, AttributeError) as exc:
            snapshot = {}
            diagnostics["analyst_error"] = str(exc)

        diagnostics["symbols"] = {}
        for stock in stocks:
            symbol = stock["symbol"]
            bars, attempts = fetch_chart(symbol, expected, now)
            diagnostics["symbols"][symbol] = attempts
            print(f"{symbol}: {json.dumps(attempts, ensure_ascii=False)}")
            if bars is None:
                failed.append(symbol)
                observed = [item["latest_bar"] for item in attempts if item.get("latest_bar")]
                if watermark and observed and max(observed) < watermark:
                    blocked.append(symbol)
                continue
            try:
                signal = analyze(stock, bars, snapshot, now)
            except (TypeError, ValueError, AttributeError) as exc:
                diagnostics.setdefault("analyst_errors", {})[symbol] = str(exc)
                signal = analyze(stock, bars, {}, now)
            results.append(signal)
            print(f"{symbol}: analyst={signal['analyst_alignment']} fresh={signal['analyst_fresh']}")

        if (watermark and expected < watermark) or blocked:
            status = "source_regression_blocked"
            error = f"Source regressed below {watermark}: {', '.join(blocked) or expected}"
        elif failed:
            status = "partial" if results else "error"
            if all(any(a["validation"] == "behind" for a in diagnostics["symbols"][s])
                   for s in failed) and not results:
                status = "lagging"
            error = f"Expected completed session {expected} unavailable for {', '.join(failed)}"
        elif diagnostics.get("analyst_error") or diagnostics.get("analyst_errors"):
            status = "partial"
            error = "Analyst snapshot could not be validated"
        else:
            status = "ok"
            previous_date = expected
            source = "yahoo_finance"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    return {
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
        "generated_at": now.isoformat(timespec="seconds"),
        "market_data_through": f"{previous_date} close" if previous_date else "unknown",
        "expected_market_date": expected,
        "market_status": context.get("market_status", "unknown"),
        "next_update_due_at": context.get("next_update_due_at"),
        "stale": status != "ok",
        "source": source,
        "source_data_lag_business_days": session_lag(previous_date, expected) if expected else None,
        "pipeline_status": status,
        "error": error,
        "diagnostics": {"previous_market_date": watermark, **diagnostics},
        "signals": results if status == "ok" else previous.get("signals", []),
    }


def write_atomic(path, feed):
    contents = json.dumps(feed, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                            dir=path.parent, delete=False)
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(contents)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def check_health(feed, now):
    expected = market_context(now)["expected_market_date"]
    rows = feed.get("signals", [])
    stocks = read_json(WATCHLIST)["stocks"]
    published = {row["ticker"]: row for row in rows}
    coherent = (len(published) == len(rows) == len(stocks)
                and all(published.get(stock["ticker"], {}).get("data_as_of") == expected
                        and published[stock["ticker"]]["position"] == stock["position"]
                        for stock in stocks))
    generated = datetime.fromisoformat(feed["generated_at"])
    print(f"Health: expected={expected} published={published_date(feed)} "
          f"generated={generated.isoformat()} status={feed.get('pipeline_status')} "
          f"coherent={coherent} error={feed.get('error')}")
    return (
        published_date(feed) == feed.get("expected_market_date") == expected
        and generated <= now and coherent and feed.get("stale") is False
        and feed.get("pipeline_status") == "ok"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="Check existing feed without writing")
    args = parser.parse_args()
    now = datetime.now(JST)
    if args.check:
        return 0 if check_health(read_json(args.output), now) else 1

    # Refuse to publish across an unreadable baseline. Its watermark is unknown.
    previous = read_json(args.output) if args.output.exists() else {}
    feed = update_feed(now, previous)
    write_atomic(args.output, feed)
    print(f"Pipeline={feed['pipeline_status']} published={feed['market_data_through']} "
          f"lag={feed['source_data_lag_business_days']} error={feed['error']}")
    # The writer publishes diagnostics before reporting an unhealthy run.
    return 0 if feed["pipeline_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
