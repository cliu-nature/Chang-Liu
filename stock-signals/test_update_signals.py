import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import market_data as market
import update_signals as updater


def jst(value):
    return datetime.fromisoformat(value).replace(tzinfo=market.JST)


AFTER_CLOSE = jst("2026-11-19T17:00")
STOCKS = updater.read_json(updater.WATCHLIST)["stocks"]
PREVIOUS = {
    "market_data_through": "2026-11-19 close",
    "signals": [
        {"ticker": stock["ticker"], "data_as_of": "2026-11-19",
         "signal": "HOLD", "position": stock["position"]}
        for stock in STOCKS
    ],
}


def chart(symbol, latest="2026-11-19"):
    cal = market.calendar()
    days = cal.sessions_window(latest, -90)
    count = len(days)
    return {
        "meta": {
            "symbol": symbol, "exchangeTimezoneName": "Asia/Tokyo",
            "dataGranularity": "1d", "currency": "JPY",
            "regularMarketTime": jst(latest + "T15:30").timestamp(),
            "regularMarketPrice": 100,
        },
        "timestamp": [cal.session_open(day).timestamp() for day in days],
        "indicators": {
            "quote": [{
                field: [value] * count for field, value in
                {"open": 100, "high": 102, "low": 98, "close": 100, "volume": 1000}.items()
            }],
            "adjclose": [{"adjclose": [70] * count}],
        },
    }



def snapshot(score, as_of="2026-11-16"):
    return {"expires_after_days": 10, "stocks": {
        "9843": {"as_of": as_of, "stance": "TEST", "stance_score": score,
                 "target_price": 100, "buy": 3, "neutral": 1},
    }}


class PipelineTests(unittest.TestCase):
    def test_current_data_is_coherent_and_uses_quote_prices(self):
        def result(host, symbol):
            return chart(symbol), "0"

        with patch.object(market, "fetch_result", side_effect=result):
            feed = updater.update_feed(AFTER_CLOSE, {})
        self.assertEqual(feed["pipeline_status"], "ok")
        self.assertEqual(feed["market_data_through"], "2026-11-19 close")
        self.assertEqual(feed["source_data_lag_business_days"], 0)
        self.assertFalse(feed["stale"])
        self.assertTrue(updater.check_health(feed, AFTER_CLOSE))
        self.assertEqual({row["close"] for row in feed["signals"]}, {100})
        self.assertEqual({row["cloud_top"] for row in feed["signals"]}, {100})
        self.assertEqual({row["cloud_bottom"] for row in feed["signals"]}, {100})
        self.assertEqual([row["position"] for row in feed["signals"]], ["+300", "WATCH"])

    def test_held_nitori_always_displays_both_lines(self):
        stock = STOCKS[0]
        bars = market.validate_result(
            chart(stock["symbol"]), stock["symbol"], "2026-11-19", AFTER_CLOSE, {}
        )
        for close, expected, add, reduce in ((110, "ADD", 104, 98), (100, "HOLD", 102, 98),
                                             (90, "REDUCE", 102, 96)):
            with self.subTest(close=close):
                bars[-1].update(close=close, high=max(102, close), low=min(98, close))
                result = updater.analyze(stock, bars, {}, AFTER_CLOSE)
                self.assertEqual(result["signal"], expected)
                self.assertEqual(result["add_above"], add)
                self.assertEqual(result["reduce_below"], reduce)
                self.assertEqual(result["trigger"], f"收盘 加仓 >¥{add} · 减仓 <¥{reduce}")

    def test_sega_short_only_waits_on_breakout_and_shorts_on_breakdown(self):
        stock = STOCKS[1]
        self.assertEqual(stock["strategy"], "short_only")
        bars = market.validate_result(
            chart(stock["symbol"]), stock["symbol"], "2026-11-19", AFTER_CLOSE, {}
        )
        for close, expected in ((110, "WAIT"), (100, "WAIT"), (90, "SHORT")):
            with self.subTest(close=close):
                bars[-1].update(close=close, high=max(102, close), low=min(98, close))
                result = updater.analyze(stock, bars, {}, AFTER_CLOSE)
                self.assertEqual(result["signal"], expected)
                self.assertEqual(result["position"], "WATCH")
                self.assertTrue(result["trigger"].startswith("Short: close < "))

    def test_older_source_retains_coherent_feed(self):
        def result(host, symbol):
            # A single regressing ticker must block the entire publication.
            latest = "2026-11-18" if symbol == "9843.T" else "2026-11-19"
            return chart(symbol, latest), "0"

        with patch.object(market, "fetch_result", side_effect=result):
            feed = updater.update_feed(AFTER_CLOSE, PREVIOUS)
        self.assertEqual(feed["pipeline_status"], "source_regression_blocked")
        self.assertTrue(feed["stale"])
        self.assertEqual(feed["signals"], PREVIOUS["signals"])
        self.assertEqual(feed["market_data_through"], PREVIOUS["market_data_through"])
        self.assertEqual(len(feed["diagnostics"]["symbols"]["9843.T"]), 2)

    def test_network_error_and_behind_both_fall_back(self):
        for failure in (OSError("timeout"), (chart("9843.T", "2026-11-18"), "240")):
            with self.subTest(failure=type(failure).__name__):
                def result(host, symbol):
                    if host == market.HOSTS[1]:
                        return chart(symbol), "0"
                    if isinstance(failure, OSError):
                        raise failure
                    return failure

                with patch.object(market, "fetch_result", side_effect=result):
                    bars, attempts = market.fetch_chart("9843.T", "2026-11-19", AFTER_CLOSE)
                self.assertEqual(bars[-1]["date"], "2026-11-19")
                self.assertEqual(attempts[-1]["source"], market.HOSTS[1])


    def test_null_daily_bar_recovers_from_published_history(self):
        def daily_result(host, symbol):
            result = chart(symbol)
            for field in market.OHLCV:
                result["indicators"]["quote"][0][field][-1] = None
            return result, "0"

        html = "".join(
            '<tr><th class="date">' + day + '</th>'
            + ''.join(f'<td><span class="_StyledNumber__value_abc">{value}</span></td>'
                      for value in (100, 102, 98, 100, "1,000", 100))
            + '</tr>' for day in ("2026/11/18", "2026/11/19")
        )
        history = market.parse_published_history(html, ("2026-11-18", "2026-11-19"))
        with patch.object(market, "fetch_result", side_effect=daily_result), \
             patch.object(market, "fetch_published_history", return_value=history):
            bars, attempts = market.fetch_chart("9843.T", "2026-11-19", AFTER_CLOSE)
            self.assertEqual(len(bars), market.HISTORY)
            self.assertEqual(bars[-1], {"date": "2026-11-19", **history["2026-11-19"]})
            self.assertEqual(attempts[-1]["fallback"], "published_daily_history")

            history["2026-11-18"]["volume"] = 999
            bars, attempts = market.fetch_chart("9843.T", "2026-11-19", AFTER_CLOSE)
            self.assertIsNone(bars)
            self.assertIn("disagree", attempts[-1]["error"])

    def test_both_hosts_fail_and_failure_publishes_diagnostics(self):
        with patch.object(market, "fetch_result", side_effect=OSError("timeout")):
            feed = updater.update_feed(AFTER_CLOSE, PREVIOUS)
        self.assertEqual(feed["pipeline_status"], "error")
        self.assertTrue(feed["stale"])
        self.assertEqual(feed["signals"], PREVIOUS["signals"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "signals.json"
            updater.write_atomic(path, PREVIOUS)
            updater.write_atomic(path, feed)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), feed)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_partial_and_common_lag_preserve_previous_rows(self):
        older = {
            **PREVIOUS, "market_data_through": "2026-11-18 close",
            "signals": [{**row, "data_as_of": "2026-11-18"} for row in PREVIOUS["signals"]],
        }
        for partial, status in ((True, "partial"), (False, "lagging")):
            def result(host, symbol):
                latest = "2026-11-19" if partial and symbol == "9843.T" else "2026-11-18"
                return chart(symbol, latest), "0"

            with patch.object(market, "fetch_result", side_effect=result):
                feed = updater.update_feed(AFTER_CLOSE, older)
            self.assertEqual(feed["pipeline_status"], status)
            self.assertEqual(feed["signals"], older["signals"])
            self.assertTrue(feed["stale"])
            self.assertEqual(feed["source_data_lag_business_days"], 1)

    def test_analyst_expiry_and_one_step_alignment(self):
        for score, as_of, expected_alignment, delta in (
            (1, "2026-11-08", "STALE — IGNORED", 0),
            (1, "2026-11-09", "CONFIRMS", 1),
            (-1, "2026-11-16", "DIVERGES", -1),
        ):
            with self.subTest(expected_alignment=expected_alignment):
                overlay = updater.analyst_overlay(
                    "9843", 105, True, False, 100, 100, snapshot(score, as_of), AFTER_CLOSE
                )
                self.assertEqual(overlay["alignment"], expected_alignment)
                if not overlay["fresh"]:
                    self.assertEqual(overlay["stance"], "EXPIRED")
                self.assertEqual(overlay["strength_delta"], delta)
                self.assertEqual(overlay["buy_count"], 3)
                self.assertEqual(updater.shift_strength("B", delta),
                                 {-1: "B-", 0: "B", 1: "B+"}[delta])

    def test_analyst_cannot_reverse_breakout_or_breakdown(self):
        for stock, close, score, expected in (
            (STOCKS[0], 90, 1, "REDUCE"), (STOCKS[0], 110, -1, "ADD"),
            (STOCKS[1], 90, 1, "SHORT"),
        ):
            symbol = stock["symbol"]
            bars = market.validate_result(chart(symbol), symbol, "2026-11-19", AFTER_CLOSE, {})
            bars[-1].update(close=close, high=max(102, close), low=min(98, close))
            overlay = snapshot(score)
            overlay["stocks"][stock["ticker"]] = overlay["stocks"]["9843"]
            base = updater.analyze(stock, bars, {}, AFTER_CLOSE)
            result = updater.analyze(stock, bars, overlay, AFTER_CLOSE)
            self.assertEqual(result["signal"], expected)
            self.assertEqual(result["analyst_alignment"], "DIVERGES")
            self.assertEqual(result["strength"], updater.shift_strength(base["strength"], -1))

    def test_exchange_close_weekends_and_holidays(self):
        for clock, expected in (
            ("2026-11-19T12:00", "2026-11-18"),
            ("2026-11-19T15:29", "2026-11-18"),
            ("2026-11-19T15:39", "2026-11-18"),
            ("2026-11-19T15:40", "2026-11-19"),
            ("2026-11-21T17:00", "2026-11-20"),
            ("2026-11-23T17:00", "2026-11-20"),
            ("2027-01-02T17:00", "2026-12-30"),
        ):
            with self.subTest(clock=clock):
                self.assertEqual(market.market_context(jst(clock))["expected_market_date"], expected)
        self.assertEqual(market.market_context(jst("2026-11-23T17:00"))["next_update_due_at"],
                         "2026-11-24T16:45:00+09:00")
        self.assertEqual(market.session_lag("2026-11-20", "2026-11-24"), 1)

    def test_intraday_and_cached_opening_bar(self):
        result = chart("9843.T")
        result["meta"]["regularMarketTime"] = jst("2026-11-19T10:10").timestamp()
        midday = jst("2026-11-19T12:00")
        diagnostic = {}
        bars = market.validate_result(result, "9843.T", "2026-11-18", midday, diagnostic)
        self.assertEqual(bars[-1]["date"], "2026-11-18")
        self.assertEqual(diagnostic["excluded_unfinished_bar"], "2026-11-19")
        with self.assertRaisesRegex(ValueError, "no completed-session timestamp"):
            market.validate_result(result, "9843.T", "2026-11-19", AFTER_CLOSE, {})

    def test_duplicate_missing_and_mismatched_close(self):
        original = chart("9843.T")
        for issue in ("duplicate", "missing", "closing quote"):
            result = copy.deepcopy(original)
            if issue == "duplicate":
                result["timestamp"][-2] = result["timestamp"][-3]
            elif issue == "missing":
                del result["timestamp"][-2]
                for field in result["indicators"]["quote"][0].values():
                    del field[-2]
            else:
                result["meta"]["regularMarketPrice"] = 101
            with self.subTest(issue=issue), self.assertRaisesRegex(ValueError, "(?i)" + issue):
                market.validate_result(result, "9843.T", "2026-11-19", AFTER_CLOSE, {})

    def test_nitori_position_and_obsolete_config(self):
        nitori, sega = STOCKS
        self.assertEqual(nitori["position"], "+300")
        self.assertEqual(nitori["last_position_update"], {
            "date": "2026-11-17", "change_shares": 100, "price_jpy": 3682, "resulting_shares": 300,
        })
        self.assertEqual(sega["position"], "WATCH")
        self.assertTrue(all("weekly_plan" not in stock for stock in STOCKS))


if __name__ == "__main__":
    unittest.main()
