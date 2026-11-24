// Stock Signals — robust Scriptable widget
// Source of truth: GitHub signals.json
// This script never silently substitutes hard-coded trading signals.

const DATA_URL =
  "https://raw.githubusercontent.com/cliu-nature/Chang-Liu/main/stock-signals/signals.json";

const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), "stock-signals-cache.json");

const COLORS = {
  bg: new Color("#0B0B0D"),
  primary: new Color("#F5F5F7"),
  secondary: new Color("#A1A1A6"),
  tertiary: new Color("#636366"),
  divider: new Color("#2C2C2E"),
  green: new Color("#30D158"),
  red: new Color("#FF453A"),
  yellow: new Color("#FFD60A"),
  blue: new Color("#0A84FF"),
  gray: new Color("#8E8E93"),
  orange: new Color("#FF9F0A")
};

function signalColor(signal) {
  const s = String(signal || "WAIT").toUpperCase();
  if (["ADD", "LONG"].includes(s)) return COLORS.green;
  if (["REDUCE", "SHORT"].includes(s)) return COLORS.red;
  if (s === "COVER") return COLORS.blue;
  if (s === "HOLD") return COLORS.yellow;
  return COLORS.gray;
}

function compactDate(v) {
  if (!v) return "-";
  const m = String(v).match(/(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?/);
  if (!m) return String(v);
  return m[4] ? `${m[2]}/${m[3]} ${m[4]}` : `${m[2]}/${m[3]}`;
}

const SIGNALS = ["ADD", "LONG", "HOLD", "WAIT", "REDUCE", "SHORT", "COVER"];
const STRENGTHS = ["A+", "A", "B+", "B", "B-", "C", "D"];
const EXPECTED_TICKERS = ["9843", "6460"];
const MAX_FEED_AGE_MS = 10 * 86400000;

function marketDate(value) {
  return typeof value === "string"
    ? value.match(/^(\d{4}-\d{2}-\d{2})(?: close)?$/)?.[1] || ""
    : "";
}

function feedShape(data) {
  return data && Array.isArray(data.signals)
    && data.signals.every(row => row && typeof row.ticker === "string")
    && (marketDate(data.market_data_through) || data.stale === true);
}

function effectiveDate(data) {
  return [marketDate(data.market_data_through),
    ...data.signals.map(row => marketDate(row.data_as_of))].sort().pop();
}

async function fetchFresh() {
  const url = `${DATA_URL}?cb=${Date.now()}`;
  const req = new Request(url);
  req.timeoutInterval = 12;
  req.headers = {
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
    "Accept": "application/json"
  };

  const data = await req.loadJSON();
  if (!feedShape(data)) {
    throw new Error("Invalid signals.json");
  }

  const cached = loadCache()?.data;
  if (cached) {
    const current = effectiveDate(data);
    const stored = effectiveDate(cached);
    if (current < stored || (current === stored
        && Date.parse(data.generated_at) < Date.parse(cached.generated_at))) {
      throw new Error("Raw GitHub response regressed behind the local feed");
    }
  }
  try {
    fm.writeString(cachePath, JSON.stringify(data));
  } catch (e) {
    console.log("Cache write failed: " + e);
  }
  return { data, source: "LIVE" };
}

function loadCache() {
  if (!fm.fileExists(cachePath)) return null;
  try {
    const data = JSON.parse(fm.readString(cachePath));
    if (!feedShape(data)) return null;
    return { data, source: "CACHE" };
  } catch (_) {
    return null;
  }
}

async function loadData() {
  try {
    return await fetchFresh();
  } catch (e) {
    console.log("Live feed failed: " + e);
    const cached = loadCache();
    if (cached) return cached;
    return {
      data: {
        updated: null,
        generated_at: null,
        market_data_through: "unavailable",
        market_status: "error",
        stale: true,
        error: "Network/feed unavailable",
        signals: []
      },
      source: "ERROR"
    };
  }
}

function isUnsafeFeed(data, source, now = Date.now()) {
  if (source !== "LIVE" || data.stale !== false || data.pipeline_status !== "ok"
      || data.error || data.source_data_lag_business_days !== 0) return true;

  const day = marketDate(data.market_data_through);
  const generation = Date.parse(data.generated_at);
  const due = Date.parse(data.next_update_due_at);
  if (!day || day !== data.expected_market_date
      || !Number.isFinite(generation) || generation > now + 5 * 60000
      || !Number.isFinite(due) || due <= generation || now >= due
      || now - generation > MAX_FEED_AGE_MS) return true;

  const tickers = new Set(data.signals.map(row => row.ticker));
  return data.signals.length !== tickers.size
    || !EXPECTED_TICKERS.every(ticker => tickers.has(ticker))
    || data.signals.some(row => row.data_as_of !== day
      || !SIGNALS.includes(row.signal) || !STRENGTHS.includes(row.strength));
}

function addHeader(widget, data, source, unsafe) {
  const row = widget.addStack();
  row.centerAlignContent();

  const title = row.addText("Stocks");
  title.font = Font.boldSystemFont(15);
  title.textColor = COLORS.primary;

  row.addSpacer();

  const badge = row.addText(source);
  badge.font = Font.boldSystemFont(8);
  badge.textColor = unsafe ? COLORS.orange : COLORS.tertiary;

  widget.addSpacer(2);

  const meta = widget.addStack();
  meta.centerAlignContent();

  const through = meta.addText(`Data ${compactDate(data.market_data_through)}`);
  through.font = Font.systemFont(8);
  through.textColor = unsafe ? COLORS.orange : COLORS.secondary;

  meta.addSpacer();

  const updated = meta.addText(`Updated ${compactDate(data.updated)}`);
  updated.font = Font.systemFont(8);
  updated.textColor = COLORS.tertiary;

  if (unsafe) {
    widget.addSpacer(5);
    const warn = widget.addText(
      source === "CACHE" ? "CACHED DATA — NOT CURRENT"
        : source === "ERROR" ? "FEED UNAVAILABLE"
        : "STALE / FEED ERROR — CHECK BEFORE USING"
    );
    warn.font = Font.boldSystemFont(8);
    warn.textColor = COLORS.orange;
    warn.lineLimit = 1;
    warn.minimumScaleFactor = 0.65;
  }

  widget.addSpacer(7);
}

function addSignalRow(widget, s, unsafe) {
  const row = widget.addStack();
  row.layoutVertically();

  const top = row.addStack();
  top.centerAlignContent();

  const dot = top.addText("●");
  dot.font = Font.systemFont(9);
  dot.textColor = unsafe ? COLORS.gray : signalColor(s.signal);

  top.addSpacer(6);

  const ticker = top.addText(String(s.ticker || "-"));
  ticker.font = Font.boldSystemFont(14);
  ticker.textColor = COLORS.primary;

  top.addSpacer(6);

  const name = top.addText(String(s.name || ""));
  name.font = Font.systemFont(9);
  name.textColor = COLORS.secondary;
  name.lineLimit = 1;
  name.minimumScaleFactor = 0.7;

  top.addSpacer();

  const strength = top.addText(String(s.strength || "-"));
  strength.font = Font.boldSystemFont(9);
  strength.textColor = COLORS.secondary;

  top.addSpacer(6);

  const signalLabel = s.strategy === "short_only" && s.signal === "WAIT"
    ? "WAIT FOR SHORT" : String(s.signal || "—");
  const sig = top.addText(signalLabel);
  sig.font = Font.boldSystemFont(9);
  sig.textColor = unsafe ? COLORS.orange : signalColor(s.signal);

  row.addSpacer(4);

  const line2 = row.addStack();
  line2.centerAlignContent();

  const pos = line2.addText(String(s.position || "-"));
  pos.font = Font.mediumSystemFont(9);
  pos.textColor = COLORS.primary;

  line2.addSpacer(8);

  const trigger = line2.addText(String(s.trigger || "-"));
  trigger.font = Font.systemFont(8);
  trigger.textColor = unsafe ? COLORS.tertiary : COLORS.secondary;
  trigger.lineLimit = 1;
  trigger.minimumScaleFactor = 0.55;

  if (s.analyst_stance) {
    row.addSpacer(3);
    const analyst = row.addText(
      `Analyst: ${s.analyst_stance} · ${s.analyst_alignment || "-"}`
    );
    analyst.font = Font.systemFont(7);
    analyst.textColor = s.analyst_fresh ? COLORS.tertiary : COLORS.orange;
    analyst.lineLimit = 1;
    analyst.minimumScaleFactor = 0.6;
  }
}

function addDivider(widget) {
  widget.addSpacer(5);
  const line = widget.addStack();
  line.backgroundColor = COLORS.divider;
  line.size = new Size(0, 0.5);
  widget.addSpacer(5);
}

async function createWidget(wrapper) {
  const data = wrapper.data;
  const source = wrapper.source;
  const unsafe = isUnsafeFeed(data, source);

  const widget = new ListWidget();
  widget.backgroundColor = COLORS.bg;
  widget.setPadding(9, 14, 8, 14);

  addHeader(widget, data, source, unsafe);

  const family = config.widgetFamily || "medium";
  const maxSignals = family === "small" ? 1 : family === "large" ? 5 : 2;
  const signals = (data.signals || []).slice(0, maxSignals);

  if (!signals.length) {
    const msg = widget.addText("No current signal data");
    msg.font = Font.mediumSystemFont(11);
    msg.textColor = COLORS.orange;
  } else {
    for (let i = 0; i < signals.length; i++) {
      addSignalRow(widget, signals[i], unsafe);
      if (i < signals.length - 1) addDivider(widget);
    }
  }

  // iOS treats this as a request, not a guarantee.
  widget.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);

  // Tap opens Scriptable so the user can force an immediate run if iOS delays refresh.
  widget.url = "scriptable:///run";

  return widget;
}

const wrapper = await loadData();
const widget = await createWidget(wrapper);

Script.setWidget(widget);

if (!config.runsInWidget) {
  if (config.widgetFamily === "small") await widget.presentSmall();
  else if (config.widgetFamily === "large") await widget.presentLarge();
  else await widget.presentMedium();
}

Script.complete();
