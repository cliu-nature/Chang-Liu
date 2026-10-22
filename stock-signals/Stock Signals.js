// Install in Scriptable as "Stock Signals". Same feed and legacy signal fields.
const DATA_URL = "https://raw.githubusercontent.com/cliu-nature/Chang-Liu/main/stock-signals/signals.json";

function feedSafe(data, now = Date.now()) {
  const generated = Date.parse(data && data.generated_at);
  const expiry = Date.parse(data && data.expires_at);
  return !!(data && data.schema_version === 2 && data.status === "ok" &&
    data.stale === false && data.actionable === true &&
    Number.isFinite(generated) && generated <= now + 300000 &&
    Number.isFinite(expiry) && now < expiry &&
    Array.isArray(data.signals) && data.signals.length &&
    data.signals.every(s => s && s.status === "ok" && s.stale === false &&
      s.actionable === true && s.market_data_date === data.expected_market_date));
}

async function loadData() {
  const fm = FileManager.local();
  const cache = fm.joinPath(fm.documentsDirectory(), "stock-signals-v2-cache.json");
  let data;
  try {
    const req = new Request(DATA_URL + "?t=" + Date.now());
    req.timeoutInterval = 12;
    req.headers = {"Cache-Control": "no-cache"};
    data = await req.loadJSON();
    if (!data || !Array.isArray(data.signals)) throw new Error("Invalid feed");
    // Cache errors too, so a later network failure cannot resurrect older recommendations.
    fm.writeString(cache, JSON.stringify(data));
  } catch (e) {
    try { data = JSON.parse(fm.readString(cache)); } catch (_) { data = {signals: []}; }
    data = {...data, offline: true};
  }
  const safe = feedSafe(data) && !data.offline;
  return {...data, displaySafe: safe,
    displayStatus: data.offline ? "OFFLINE — DO NOT TRADE" :
      safe ? "Stocks" : "STALE DATA — DO NOT TRADE",
    signals: safe ? data.signals : (data.signals || []).map(s => ({
      ...s, signal: "WAIT", strength: "-", trigger: "DO NOT TRADE", invalid: "-"
    }))};
}

function text(stack, value, size, color = "#F5F5F7", bold = false) {
  const t = stack.addText(String(value || "-"));
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = new Color(color);
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.65;
  return t;
}

async function createWidget(data) {
  const w = new ListWidget();
  w.backgroundColor = new Color("#0B0B0D");
  w.setPadding(11, 14, 10, 14);
  text(w, data.displayStatus, data.displaySafe ? 15 : 11,
       data.displaySafe ? "#F5F5F7" : "#FF453A", true);
  text(w, "Data " + (data.market_data_through || "unavailable"), 8, "#A1A1A6");
  text(w, "Updated " + (data.updated || "never"), 8, "#A1A1A6");
  if (data.displaySafe) {
    text(w, "Valid until " + data.expires_at.slice(5,16).replace("T", " ") + " JST", 8, "#A1A1A6");
  }
  w.addSpacer(5);
  const rank = {"A+": 6, "A": 5, "B+": 4, "B": 3, "C": 2, "D": 1};
  const stocks = [...data.signals].sort((a,b) => (rank[b.strength] || 0) - (rank[a.strength] || 0));
  const max = config.widgetFamily === "small" ? 1 : config.widgetFamily === "large" ? 5 : 2;
  for (const stock of stocks.slice(0, max)) {
    const row = w.addStack();
    row.centerAlignContent();
    text(row, stock.ticker, 14, "#F5F5F7", true);
    row.addSpacer(6);
    text(row, stock.name, 9, "#A1A1A6");
    row.addSpacer();
    const color = ["ADD", "LONG"].includes(stock.signal) ? "#30D158" :
      ["SHORT", "REDUCE"].includes(stock.signal) ? "#FF453A" : "#FFD60A";
    text(row, stock.strength + "  " + stock.signal, 10, color, true);
    text(w, stock.position + "  " + stock.trigger, 9, "#A1A1A6");
    text(w, stock.invalid, 8, "#8E8E93");
    w.addSpacer(5);
  }
  if (stocks.length > max) text(w, "+" + (stocks.length - max) + " more in feed", 8, "#A1A1A6");
  // iOS may refresh later than requested; this is a retry request, not an appointment.
  w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);
  w.url = "scriptable:///run?scriptName=" + encodeURIComponent(Script.name());
  return w;
}

const data = await loadData();
const widget = await createWidget(data);
Script.setWidget(widget);
if (!config.runsInWidget) {
  if (config.widgetFamily === "small") await widget.presentSmall();
  else if (config.widgetFamily === "large") await widget.presentLarge();
  else await widget.presentMedium();
}
Script.complete();
