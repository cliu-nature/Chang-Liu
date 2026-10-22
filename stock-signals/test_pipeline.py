import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import update_signals as u

STOCK = {"ticker": "9843", "symbol": "9843.T", "name": "NITORI", "position": "+100"}

def bars(day):
    return [{"date": (datetime.fromisoformat(day) - timedelta(days=99-i)).date().isoformat(),
             "open": 100+i, "high": 102+i, "low": 99+i, "close": 101+i, "volume": 1000}
            for i in range(100)]

class PipelineTests(unittest.TestCase):
    def test_session_deadlines(self):
        cases = [
            ("2026-10-23T09:00:00+09:00", "2026-10-22", "2026-10-23"),
            ("2026-10-23T16:14:00+09:00", "2026-10-22", "2026-10-23"),
            ("2026-10-23T16:15:00+09:00", "2026-10-23", "2026-10-26"),
            ("2026-10-25T09:00:00+09:00", "2026-10-23", "2026-10-26"),
            ("2026-10-12T09:00:00+09:00", "2026-10-09", "2026-10-13"),
            ("2027-01-03T09:00:00+09:00", "2026-12-30", "2027-01-04"),
        ]
        for instant, expected, expiry in cases:
            with self.subTest(instant=instant):
                day, due = u.session_context(datetime.fromisoformat(instant))
                self.assertEqual(day, expected)
                self.assertEqual(due.astimezone(u.JST).date().isoformat(), expiry)

    def test_after_close_uses_today(self):
        feed = u.build_feed({"stocks": [STOCK]}, {}, datetime.fromisoformat("2026-10-23T18:00:00+09:00"),
                            lambda symbol, expected: bars(expected))
        self.assertEqual(feed["status"], "ok")
        self.assertEqual(feed["market_data_through"], "2026-10-23 close")
        self.assertEqual(feed["signals"][0]["position"], "+100")

    def test_partial_and_old_data_fail_closed(self):
        stocks = [STOCK, dict(STOCK, ticker="6460", symbol="6460.T")]
        def fetch(symbol, expected):
            if symbol == "6460.T":
                raise TimeoutError()
            return bars("2026-10-22")
        feed = u.build_feed({"stocks": stocks}, {}, datetime.fromisoformat("2026-10-23T18:00:00+09:00"), fetch)
        self.assertTrue(feed["stale"])
        self.assertEqual(feed["market_data_through"], "unknown")
        self.assertTrue(all(s["signal"] == "WAIT" and not s["actionable"] for s in feed["signals"]))

    def test_retries_both_hosts(self):
        with patch.object(u, "download_chart", side_effect=[TimeoutError(), bars("2026-10-22")]) as fetch:
            self.assertEqual(len(u.fetch_chart("9843.T", "2026-10-22")), 100)
            self.assertEqual([c.args[2] for c in fetch.call_args_list], ["query1", "query2"])

    def test_today_intraday_excluded_and_minimum_history(self):
        day = datetime(2026, 10, 23, tzinfo=u.JST)
        stamps = [int((day - timedelta(days=100-i)).timestamp()) for i in range(101)]
        quote = {"open": [100]*101, "high": [102]*101, "low": [99]*101,
                 "close": [101]*101, "volume": [1000]*101}
        payload = {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [quote]}}]}}
        import io
        with patch.object(u.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))):
            result = u.download_chart("9843.T", "2026-10-22", "query1")
        self.assertEqual(result[-1]["date"], "2026-10-22")

    def test_failure_published_and_exit_nonzero(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            output = Path(folder) / "signals.json"
            output.write_text(json.dumps({"signals": [dict(STOCK, signal="ADD")]}))
            with patch.object(u, "OUTPUT", output), patch.object(u, "WATCHLIST", Path(folder)/"missing"):
                self.assertEqual(u.main(), 1)
            feed = json.loads(output.read_text())
            self.assertFalse(feed["actionable"])
            self.assertEqual(feed["signals"][0]["signal"], "WAIT")
            self.assertFalse(output.with_suffix(".tmp").exists())

if __name__ == "__main__":
    unittest.main()
