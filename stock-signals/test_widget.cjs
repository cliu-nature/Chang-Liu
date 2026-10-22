const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(__dirname + "/Stock Signals.js", "utf8");
const functions = source.slice(0, source.indexOf("const data = await loadData();"));
const now = Date.parse("2026-10-23T09:00:00+09:00");
const healthy = {schema_version: 2, status: "ok", stale: false, actionable: true,
  generated_at: "2026-10-22T18:00:00+09:00", expires_at: "2026-10-23T16:15:00+09:00",
  expected_market_date: "2026-10-22",
  signals: [{ticker: "9843", signal: "ADD", status: "ok", stale: false,
    actionable: true, market_data_date: "2026-10-22"}]};
const context = vm.createContext({console, Date: class extends Date {static now() {return now;}}});
vm.runInContext(functions, context);
assert.equal(context.feedSafe(healthy, now), true);
for (const change of [{expires_at: "2026-10-23T08:00:00+09:00"},
  {status: "error"}, {stale: true}, {schema_version: 1}, {expires_at: "invalid"},
  {signals: []}, {signals: [null]}, {generated_at: "2026-10-24T00:00:00+09:00"}]) {
  assert.equal(context.feedSafe({...healthy, ...change}, now), false);
}
let cached = JSON.stringify(healthy);
let fail = false;
context.FileManager = {local: () => ({
  documentsDirectory: () => "/", joinPath: (a,b) => a+b,
  writeString: (p,v) => {cached=v;}, readString: () => cached
})};
context.Request = class {async loadJSON() {if(fail) throw Error("offline"); return healthy;}};
(async () => {
  assert.equal((await context.loadData()).displaySafe, true);
  fail = true;
  const offline = await context.loadData();
  assert.equal(offline.displaySafe, false);
  assert.equal(offline.signals[0].signal, "WAIT");
  assert.match(offline.displayStatus, /OFFLINE/);
  console.log("Widget freshness, invalid-feed and offline safeguards passed");
})().catch(e => {console.error(e); process.exitCode=1;});
