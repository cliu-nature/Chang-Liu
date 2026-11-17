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

function ageHours(iso) {
  if (!iso) return Infinity;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return Infinity;
  return (Date.now() - t) / 3600000;
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
  if (!data || !Array.isArray(data.signals)) {
    throw new Error("Invalid signals.json");
  }

  fm.writeString(cachePath, JSON.stringify(data));
  return { data, source: "LIVE" };
}

function loadCache() {
  if (!fm.fileExists(cachePath)) return null;
  try {
    const data = JSON.parse(fm.readString(cachePath));
    if (!data || !Array.isArray(data.signals)) return null;
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
        updated: new Date().toISOString(),
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

function isUnsafeFeed(data, source) {
  if (source !== "LIVE") return true;
  if (data.stale === true) return true;
  if (["error", "unavailable"].includes(String(data.market_status || "").toLowerCase())) return true;

  // If the backend itself has not generated anything for 36h, warn even if stale was mis-set.
  if (ageHours(data.generated_at) > 36) return true;
  return false;
}

function addHeader(widget, data, source, unsafe) {
  const row = widget.addStack();
  row.centerAlignContent();

  const title = row.addText("Stocks");
  title.font = Font.boldSystemFont(15);
  title.textColor = COLORS.primary;

  row.addSpacer();

  const badge = row.addText(source === "LIVE" ? "JP" : source);
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
      source === "LIVE"
        ? "⚠︎ STALE / FEED ERROR — CHECK BEFORE USING"
        : "⚠︎ CACHED DATA — NOT CURRENT"
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

  const sig = top.addText(unsafe ? "CHECK" : String(s.signal || "WAIT"));
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
  widget.setPadding(12, 14, 10, 14);

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
