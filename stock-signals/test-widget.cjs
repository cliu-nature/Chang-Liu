const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const script = fs.readFileSync(path.join(__dirname, "scriptable-widget.js"), "utf8");
const startup = script.indexOf("const wrapper = await loadData();");
assert.ok(startup > 0);
const context = vm.createContext({
  Color: function () {},
  FileManager: { local: () => ({
    documentsDirectory: () => "/tmp",
    joinPath: () => "feed.json"
  }) }
});
const { isUnsafeFeed, feedShape } = vm.runInContext(
  script.slice(0, startup) + "\n({ isUnsafeFeed, feedShape })", context
);

const now = Date.parse("2026-11-23T17:00:00+09:00");
const healthy = {
  generated_at: "2026-11-20T16:55:00+09:00",
  next_update_due_at: "2026-11-24T16:45:00+09:00",
  market_data_through: "2026-11-20 close",
  expected_market_date: "2026-11-20",
  stale: false,
  pipeline_status: "ok",
  source_data_lag_business_days: 0,
  error: null,
  signals: ["9843", "6460"].map(ticker => ({
    ticker, data_as_of: "2026-11-20", signal: "WAIT", strength: "B"
  }))
};
assert.ok(feedShape(healthy));
assert.equal(isUnsafeFeed(healthy, "LIVE", now), false);
assert.equal(isUnsafeFeed(healthy, "CACHE", now), true);
assert.equal(isUnsafeFeed(healthy, "ERROR", now), true);
assert.equal(isUnsafeFeed(healthy, "LIVE", Date.parse(healthy.next_update_due_at)), true);
assert.equal(isUnsafeFeed({ ...healthy, stale: true }, "LIVE", now), true);
for (const status of ["lagging", "partial", "source_regression_blocked", "error"]) {
  assert.equal(isUnsafeFeed({ ...healthy, pipeline_status: status }, "LIVE", now), true);
}
assert.equal(isUnsafeFeed({ ...healthy, generated_at: "invalid" }, "LIVE", now), true);
assert.equal(isUnsafeFeed({ ...healthy, generated_at: "2026-11-24T12:00:00+09:00" }, "LIVE", now), true);
assert.equal(isUnsafeFeed({ ...healthy, signals: healthy.signals.slice(0, 1) }, "LIVE", now), true);
const { pipeline_status, expected_market_date, next_update_due_at, ...legacy } = healthy;
assert.ok(feedShape(legacy));
assert.equal(isUnsafeFeed(legacy, "LIVE", now), true);
console.log("Widget freshness and legacy schema checks passed.");
