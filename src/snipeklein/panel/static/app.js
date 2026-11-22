/* SnipeKlein decision panel - vanilla JS, no dependencies.
 * Listing data is third party content: it is always inserted with
 * textContent, never as HTML. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  limit: 50, offset: 0, total: 0, view: "listings", prices: [],
  stats: null, statsLoadedAt: 0,
};

const KEY_COLORS = {
  rejected: "#6e7681", accepted: "#3fb950", alerted: "#d29922",
  near_miss: "#58a6ff", not_target: "#a5b4fc",
  extreme: "#f85149", actionable: "#3fb950", not_a_deal: "#6e7681",
  green: "#3fb950", yellow: "#d29922", red: "#f85149",
  pickup: "#3fb950", shipping: "#d29922", unknown: "#6e7681",
  pro: "#a5b4fc", private: "#6e7681", yes: "#3fb950", no: "#6e7681",
  with_badges: "#3fb950", without_badges: "#6e7681",
};
const SERIES_COLORS = {
  ads: "#6ea8fe", valued: "#3fb950", alerts: "#d29922",
  cycles: "#a5b4fc", errors: "#f85149",
  requests: "#6ea8fe", tokens: "#3fb950", quota_429: "#f85149",
};
const LABELS = {
  rejected: "rejected", accepted: "accepted (no alert)", alerted: "alerted",
  near_miss: "near miss", not_target: "not a DDR5 target",
  extreme: "extreme", actionable: "actionable", not_a_deal: "not a deal",
  green: "green", yellow: "yellow", red: "red",
  pickup: "pickup", shipping: "shipping", unknown: "unknown",
  pro: "commercial", private: "private", yes: "yes", no: "no",
  with_badges: "with badges", without_badges: "without badges",
  ads: "ads", valued: "valued", alerts: "alerts", cycles: "cycles", errors: "errors",
  requests: "requests", tokens: "tokens", quota_429: "429s",
};
const COLOR_ORDER = [
  "#6ea8fe", "#3fb950", "#d29922", "#f85149", "#a5b4fc",
  "#58a6ff", "#f0883e", "#c8a2ff",
];

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== "" && value !== null && value !== undefined) url.searchParams.set(key, value);
  }
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

function filters() {
  return {
    q: $("#q").value.trim(),
    outcome: $("#outcome").value,
    stage: $("#stage").value,
    query_tag: $("#query_tag").value,
    hours: $("#hours").value,
    order: $("#order").value,
  };
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (isNaN(date)) return value;
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 5400) return Math.round(seconds / 60) + " min ago";
  if (seconds < 172800) return Math.round(seconds / 3600) + " h ago";
  return date.toISOString().slice(0, 16).replace("T", " ");
}

function fmtEur(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toLocaleString("de-DE", { maximumFractionDigits: 0 }) + " €";
}

function fmtNum(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toLocaleString("de-DE", { maximumFractionDigits: 0 });
}

function fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(digits) + " %";
}

function fmtRatio(value, digits = 0) {
  if (value === null || value === undefined) return "-";
  return (Number(value) * 100).toFixed(digits) + " %";
}

function fmtSeconds(value) {
  if (value === null || value === undefined) return "-";
  const seconds = Number(value);
  if (seconds < 90) return Math.round(seconds) + " s";
  if (seconds < 5400) return Math.round(seconds / 60) + " min";
  return (seconds / 3600).toFixed(1) + " h";
}

function fmtAxis(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  if (Math.abs(number) >= 10000) return (number / 1000).toFixed(0) + "k";
  if (Math.abs(number) >= 1000) return (number / 1000).toFixed(1) + "k";
  return String(Math.round(number * 100) / 100);
}

function labelFor(key) {
  return LABELS[key] || String(key || "unknown");
}

function colorFor(key, index) {
  return KEY_COLORS[key] || COLOR_ORDER[index % COLOR_ORDER.length];
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

function emptyNode(text) {
  return el("div", "panel-empty", text || "no data yet");
}

function statsSection(grid, title, subtitle) {
  const head = el("div", "stats-section");
  head.appendChild(el("h2", null, title));
  if (subtitle) head.appendChild(el("span", "muted", subtitle));
  grid.appendChild(head);
}

function statsPanel(grid, title, subtitle, body, wide) {
  const box = el("div", "panel-card" + (wide ? " wide" : ""));
  const head = el("div", "panel-head");
  head.appendChild(el("div", "panel-title", title));
  if (subtitle) head.appendChild(el("div", "panel-sub", subtitle));
  box.appendChild(head);
  box.appendChild(body || emptyNode());
  grid.appendChild(box);
}

function statsColumns(...nodes) {
  const wrap = el("div", "stats-cols");
  for (const node of nodes) {
    const col = el("div", "stats-col");
    col.appendChild(node);
    wrap.appendChild(col);
  }
  return wrap;
}

function labeledBlock(title, body) {
  const wrap = el("div");
  wrap.appendChild(el("div", "mini-title", title));
  wrap.appendChild(body);
  return wrap;
}

function metricList(items) {
  const wrap = el("div", "metric-list");
  for (const item of items || []) {
    const box = el("div", "metric");
    box.appendChild(el("div", "k", item.label));
    box.appendChild(el("div", "v", item.value));
    if (item.sub) box.appendChild(el("div", "x", item.sub));
    wrap.appendChild(box);
  }
  return wrap;
}

function makeBars(items, options) {
  const settings = options || {};
  const rows = (items || []).filter((item) => item && Number(item.value ?? item.count ?? 0) > 0);
  if (!rows.length) return emptyNode(settings.empty);
  const max = Math.max(...rows.map((item) => Number(item.value ?? item.count ?? 0)), 1);
  const wrap = el("div", "bars");
  for (const item of rows) {
    const value = Number(item.value ?? item.count ?? 0);
    const row = el("div", "bar-row");
    const label = el("div", "bar-label");
    label.appendChild(el("div", "bar-name", item.label ?? labelFor(item.key)));
    const sub = typeof settings.sub === "function" ? settings.sub(item) : item.sub;
    if (sub) label.appendChild(el("div", "bar-sub", sub));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    fill.style.width = Math.max(2, (value / max) * 100) + "%";
    fill.style.background = item.color || settings.color || colorFor(item.key);
    fill.title = (item.label ?? labelFor(item.key)) + ": " + value;
    track.appendChild(fill);
    const text = settings.format
      ? settings.format(item)
      : fmtNum(value);
    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(el("div", "bar-value", text));
    wrap.appendChild(row);
  }
  return wrap;
}

function makeStacked(items, options) {
  const settings = options || {};
  const rows = (items || []).filter((item) => item && Number(item.count || 0) > 0);
  const total = rows.reduce((sum, item) => sum + Number(item.count || 0), 0);
  if (!rows.length || !total) return emptyNode(settings.empty);
  const wrap = el("div", "stack-wrap");
  const bar = el("div", "stack");
  for (const item of rows) {
    const segment = el("div", "stack-seg");
    segment.style.width = (Number(item.count) / total) * 100 + "%";
    segment.style.background = item.color || colorFor(item.key);
    segment.title = labelFor(item.key) + ": " + fmtNum(item.count) + " (" + fmtPct(item.pct, 0) + ")";
    bar.appendChild(segment);
  }
  wrap.appendChild(bar);
  const legend = el("div", "legend");
  for (const item of rows) {
    const entry = el("span", "legend-item");
    const swatch = el("span", "swatch");
    swatch.style.background = item.color || colorFor(item.key);
    entry.appendChild(swatch);
    entry.appendChild(document.createTextNode(labelFor(item.key) + " " + fmtNum(item.count)));
    legend.appendChild(entry);
  }
  wrap.appendChild(legend);
  return wrap;
}

function makeHistogram(bins, options) {
  const settings = options || {};
  const rows = (bins || []).filter((bin) => bin);
  if (!rows.length) return emptyNode(settings.empty);
  const max = Math.max(...rows.map((bin) => Number(bin.count || 0)), 1);
  const low = Number(rows[0].from);
  const high = Number(rows[rows.length - 1].to);
  const wrap = el("div", "hist-wrap");
  const chart = el("div", "hist");
  for (const bin of rows) {
    const bar = el("div", "bar");
    bar.style.height = Math.max(2, (Number(bin.count || 0) / max) * 92) + "px";
    const axis = settings.axisFormat || fmtAxis;
    bar.title = axis(bin.from) + " – " + axis(bin.to) + ": " + fmtNum(bin.count);
    chart.appendChild(bar);
  }
  for (const marker of settings.markers || []) {
    if (marker.value === null || marker.value === undefined) continue;
    if (!Number.isFinite(Number(marker.value)) || high <= low) continue;
    const position = ((Number(marker.value) - low) / (high - low)) * 100;
    if (position < 0 || position > 100) continue;
    const line = el("div", "hist-marker");
    line.style.left = position + "%";
    line.appendChild(el("span", null, marker.label));
    chart.appendChild(line);
  }
  wrap.appendChild(chart);
  const axis = el("div", "hist-axis");
  const axisFormat = settings.axisFormat || fmtAxis;
  axis.appendChild(el("span", null, axisFormat(low)));
  axis.appendChild(el("span", null, axisFormat(high)));
  wrap.appendChild(axis);
  return wrap;
}

function makeLineChart(series, keys) {
  const rows = (series || []).filter((row) => row);
  const usableKeys = (keys || []).filter(Boolean);
  if (!rows.length || !usableKeys.length) return emptyNode("no daily data yet");
  const width = 640, height = 200;
  const pad = { left: 42, right: 12, top: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const max = Math.max(
    1,
    ...rows.flatMap((row) => usableKeys.map((key) => Number(row[key]) || 0)),
  );
  const xFor = (index) => pad.left + (rows.length === 1 ? plotWidth / 2 : (index / (rows.length - 1)) * plotWidth);
  const yFor = (value) => pad.top + plotHeight - (Number(value) / max) * plotHeight;
  const svg = svgEl("svg", {
    viewBox: "0 0 " + width + " " + height,
    class: "line-chart",
    role: "img",
    "aria-label": usableKeys.join(", ") + " over time",
  });

  for (let step = 0; step <= 4; step += 1) {
    const value = max * (1 - step / 4);
    const y = pad.top + (step / 4) * plotHeight;
    svg.appendChild(svgEl("line", {
      x1: pad.left, x2: width - pad.right, y1: y, y2: y,
      stroke: "#2a3140", "stroke-width": "1",
    }));
    const label = svgEl("text", {
      x: pad.left - 6, y: y + 4, fill: "#98a2b3",
      "font-size": "10", "text-anchor": "end",
    });
    label.textContent = fmtAxis(value);
    svg.appendChild(label);
  }

  const labelIndexes = new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1]);
  for (const index of labelIndexes) {
    const label = svgEl("text", {
      x: xFor(index), y: height - 5, fill: "#98a2b3",
      "font-size": "10", "text-anchor": "middle",
    });
    label.textContent = String(rows[index].day || "").slice(5);
    svg.appendChild(label);
  }

  for (const key of usableKeys) {
    const color = SERIES_COLORS[key] || colorFor(key);
    const points = rows.map((row, index) => xFor(index) + "," + yFor(row[key] || 0));
    svg.appendChild(svgEl("polyline", {
      points: points.join(" "), fill: "none", stroke: color,
      "stroke-width": "2", "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    rows.forEach((row, index) => {
      const point = svgEl("circle", {
        cx: xFor(index), cy: yFor(row[key] || 0), r: "2.5", fill: color,
      });
      const title = svgEl("title");
      title.textContent = String(row.day || "") + " · " + key + ": " + fmtNum(row[key] || 0);
      point.appendChild(title);
      svg.appendChild(point);
    });
  }

  const wrap = el("div", "line-wrap");
  wrap.appendChild(svg);
  const legend = el("div", "line-legend");
  for (const key of usableKeys) {
    const item = el("span", "legend-item");
    const swatch = el("span", "swatch");
    swatch.style.background = SERIES_COLORS[key] || colorFor(key);
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(labelFor(key)));
    legend.appendChild(item);
  }
  wrap.appendChild(legend);
  return wrap;
}

function makeTable(items, columns) {
  if (!items || !items.length) return emptyNode("no rows yet");
  const table = el("table", "mini stats-table");
  const head = el("tr");
  for (const column of columns) {
    head.appendChild(el("th", column.align === "num" ? "num" : null, column.label));
  }
  table.appendChild(head);
  for (const item of items) {
    const row = el("tr");
    for (const column of columns) {
      const value = column.format ? column.format(item) : item[column.key];
      row.appendChild(el("td", column.align === "num" ? "num" : null, value ?? "-"));
    }
    table.appendChild(row);
  }
  return table;
}

function withLabels(items) {
  return (items || []).map((item, index) => ({
    ...item,
    label: labelFor(item.key),
    color: item.color || colorFor(item.key, index),
  }));
}

function valuationOf(ad) {
  return (ad.detail || {}).valuation || null;
}

function outcomeBadge(outcome, band) {
  if (outcome === "near_miss") return el("span", "badge b-nearmiss", "near miss");
  if (outcome === "not_target") return el("span", "badge b-estimate", "not a DDR5 target");
  if (outcome === "alerted") return el("span", "badge b-alerted", "alerted");
  if (outcome === "accepted") return el("span", "badge b-accepted", "accepted");
  if (band === "extreme") return el("span", "badge b-extreme", "rejected · extreme");
  return el("span", "badge b-rejected", "rejected");
}

function renderCalibration(summary) {
  const versionEl = $("#version");
  if (versionEl) versionEl.textContent = summary.version ? "v" + summary.version : "";
  const el = $("#calibration");
  if (!el) return;
  const market = summary.market || {};
  const refresh = summary.refresh || {};
  if (market.factor == null) {
    el.textContent = "calibration: none";
    el.classList.add("warn");
    return;
  }
  const factor = Number(market.factor).toFixed(3);
  el.textContent = "calibration: " + (market.samples || 0) + " systems · " + factor + "×";
  el.classList.toggle("warn", Boolean(refresh.stale));
  el.title = refresh.stale ? "the price/market refresh is stale" : "";
}

function renderCards(summary) {
  const cards = $("#cards");
  cards.textContent = "";
  const decisions = summary.decisions || {};
  const byOutcome = decisions.by_outcome || {};

  card("decisions", decisions.total || 0, "unique ads");
  card("rejected", byOutcome.rejected || 0, "filtered out");
  card("accepted", byOutcome.accepted || 0, "deal, no alert");
  card("alerted", byOutcome.alerted || 0, "alert sent");
  card("near miss", byOutcome.near_miss || 0, "15-30 % unter Markt");
  card("alerts", summary.alerts || 0, "discord messages");
  card("llm today", summary.llm_requests_today || 0,
    (summary.llm_tokens_today || 0) + " tokens");
  const last = summary.last_run;
  card("last run", last ? fmtTime(last.started_at) : "never",
    last ? "seen " + (last.ads_seen || 0) + " · alerts " + (last.alerts_sent || 0) : "");

  const watchdog = summary.watchdog || {};
  const alerting = watchdog.alerting ? "ALARMING" : (watchdog.failures || 0) + " fails";
  card("watchdog", alerting, watchdog.last_alert
    ? "last alarm " + fmtTime(new Date(watchdog.last_alert * 1000).toISOString())
    : "no alarm yet");
}

function renderQueryOptions(summary) {
  for (const selector of ["#query_tag", "#stat-query"]) {
    const select = $(selector);
    const current = select.value;
    select.textContent = "";
    select.appendChild(new Option("any query", ""));
    for (const tag of summary.queries || []) select.appendChild(new Option(tag, tag));
    select.value = current;
  }
}

function renderRows(payload) {
  const rows = $("#rows");
  rows.textContent = "";
  state.total = payload.total;

  for (const ad of payload.ads) {
    const detail = ad.detail || {};
    const valuation = detail.valuation || {};
    const tr = el("tr");

    const titleCell = el("td", "t");
    const link = el("a", null, ad.title || ad.ad_id);
    link.href = ad.url || "#";
    link.target = "_blank";
    link.rel = "noreferrer";
    link.addEventListener("click", (event) => event.stopPropagation());
    titleCell.appendChild(link);
    titleCell.appendChild(el("div", "muted mono", ad.ad_id + (ad.location ? " · " + ad.location : "")));
    tr.appendChild(titleCell);

    const priceCell = el("td", "num");
    priceCell.appendChild(document.createTextNode(fmtEur(ad.price)));
    if (ad.card_price !== null && ad.card_price !== undefined &&
        Math.abs(ad.card_price - (ad.price || 0)) > 0.5) {
      priceCell.appendChild(el("div", "muted", "card: " + fmtEur(ad.card_price)));
    }
    tr.appendChild(priceCell);

    const outcomeCell = el("td");
    outcomeCell.appendChild(outcomeBadge(ad.outcome, valuation.band));
    tr.appendChild(outcomeCell);

    const stageCell = el("td");
    stageCell.appendChild(el("span", "badge b-stage", ad.stage));
    tr.appendChild(stageCell);

    const reasonCell = el("td", "muted");
    reasonCell.appendChild(document.createTextNode(ad.reason || "-"));
    const rank = valuationOf(ad) && valuationOf(ad).price_rank;
    if (rank !== null && rank !== undefined) {
      reasonCell.appendChild(el("div", "muted", "price rank p" + Math.round(rank)));
    }
    tr.appendChild(reasonCell);
    tr.appendChild(el("td", "muted", fmtTime(ad.updated_at)));

    tr.addEventListener("click", () => openAd(ad.ad_id));
    rows.appendChild(tr);
  }

  const from = payload.total ? payload.offset + 1 : 0;
  const to = Math.min(payload.offset + payload.limit, payload.total);
  $("#pageinfo").textContent = from + "–" + to + " of " + payload.total;
  $("#prev").disabled = payload.offset <= 0;
  $("#next").disabled = payload.offset + payload.limit >= payload.total;
}

function kv(list, key, value) {
  if (value === undefined || value === null || value === "") return;
  list.appendChild(el("dt", null, key));
  list.appendChild(el("dd", null, value));
}

function openAd(adId) {
  const drawer = $("#drawer");
  $("#scrim").classList.add("open");
  drawer.classList.add("open");
  drawer.textContent = "";

  api("/api/ads/" + encodeURIComponent(adId)).then((ad) => {
    drawer.textContent = "";
    const detail = ad.detail || {};

    const head = el("div");
    const link = el("a", null, ad.title || ad.ad_id);
    link.href = ad.url || "#";
    link.target = "_blank";
    link.rel = "noreferrer";
    head.appendChild(link);
    drawer.appendChild(head);
    drawer.appendChild(el("div", "muted mono", ad.ad_id + " · " + (ad.url || "")));
    if (ad.snippet) {
      const snippet = el("div", "muted");
      snippet.style.margin = "6px 0";
      snippet.textContent = "what the triage saw: " + ad.snippet;
      drawer.appendChild(snippet);
    }

    const badges = el("div");
    badges.style.margin = "10px 0";
    badges.appendChild(outcomeBadge(ad.outcome, (detail.valuation || {}).band));
    badges.appendChild(document.createTextNode(" "));
    badges.appendChild(el("span", "badge b-stage", ad.stage));
    drawer.appendChild(badges);

    drawer.appendChild(el("h3", null, "decision trail"));
    const trail = el("ul", "trail");
    for (const step of ad.steps || []) {
      const item = el("li", step.outcome || "");
      item.appendChild(el("div", "stage", step.stage + " · " + (step.outcome || "")));
      const bits = [];
      if (step.reason) bits.push(step.reason);
      if (step.coarse_margin_pct !== undefined) bits.push("coarse margin " + step.coarse_margin_pct + " %");
      if (step.margin_pct !== undefined) bits.push("margin " + step.margin_pct + " %");
      if (step.fmv_net !== undefined) bits.push("FMV " + fmtEur(step.fmv_net));
      if (step.coverage !== undefined) bits.push("coverage " + Math.round(step.coverage * 100) + " %");
      if (step.risk_level) bits.push("risk " + step.risk_level + " (" + step.risk_score + ")");
      if (step.band) bits.push("band " + step.band);
      if (bits.length) item.appendChild(el("div", "why", bits.join(" · ")));
      trail.appendChild(item);
    }
    drawer.appendChild(trail);

    const extraction = detail.extraction || {};
    if (Object.keys(extraction).length) {
      drawer.appendChild(el("h3", null, "extracted specification"));
      const list = el("dl", "kv");
      kv(list, "complete system", extraction.is_complete_system);
      kv(list, "cpu", extraction.cpu_model);
      kv(list, "gpu", extraction.gpu_model);
      kv(list, "ram", (extraction.ram_capacity_gb || "?") + " GB " + (extraction.ram_generation || ""));
      kv(list, "storage", (extraction.storage_capacity_gb || "?") + " GB");
      kv(list, "mainboard", extraction.motherboard_chipset);
      kv(list, "psu", extraction.psu_wattage ? extraction.psu_wattage + " W" : "");
      kv(list, "os included", extraction.operating_system_included);
      kv(list, "defects", (extraction.defects_or_missing_parts || []).join(", "));
      kv(list, "pickup only", extraction.pickup_only);
      kv(list, "accessories", (extraction.accessories || []).join(", "));
      kv(list, "source", detail.extraction_source);
      drawer.appendChild(list);
    }

    const valuation = detail.valuation || {};
    if (Object.keys(valuation).length) {
      drawer.appendChild(el("h3", null, "valuation"));
      const list = el("dl", "kv");
      kv(list, "asking price", fmtEur(valuation.asking_price));
      kv(list, "fmv gross", fmtEur(valuation.fmv_gross));
      kv(list, "fmv net", fmtEur(valuation.fmv_net));
      kv(list, "margin", (valuation.margin_pct || 0).toFixed(1) + " % (" + fmtEur(valuation.margin_eur) + ")");
      kv(list, "band", valuation.band);
      kv(list, "coverage", Math.round((valuation.coverage || 0) * 100) + " %");
      kv(list, "adjustments", (valuation.adjustments || []).join("; "));
      kv(list, "missing", (valuation.missing_components || []).join(", "));
      kv(list, "defect costs", (valuation.defect_costs || []).map((d) => d.label + " " + fmtEur(d.cost_eur)).join("; "));
      drawer.appendChild(list);

      if ((valuation.components || []).length) {
        const table = el("table");
        const head = el("tr");
        ["component", "value", "source"].forEach((label, index) => {
          const th = el("th", index ? "num" : null, label);
          head.appendChild(th);
        });
        table.appendChild(head);
        for (const component of valuation.components) {
          const row = el("tr");
          row.appendChild(el("td", null, component.label));
          row.appendChild(el("td", "num", fmtEur(component.fmv_eur)));
          row.appendChild(el("td", "muted", component.source));
          table.appendChild(row);
        }
        drawer.appendChild(table);
      }
    }

    const risk = detail.risk || {};
    if (Object.keys(risk).length) {
      drawer.appendChild(el("h3", null, "risk"));
      const list = el("dl", "kv");
      kv(list, "score", risk.score);
      kv(list, "level", risk.level);
      for (const signal of risk.signals || []) {
        kv(list, signal.label, signal.detail ? signal.label + " (" + signal.detail + ")" : "");
      }
      drawer.appendChild(list);
    }

    const listing = detail.listing || {};
    if (Object.keys(listing).length) {
      drawer.appendChild(el("h3", null, "listing"));
      const list = el("dl", "kv");
      kv(list, "price", fmtEur(listing.price));
      kv(list, "location", [listing.plz, listing.city].filter(Boolean).join(" "));
      kv(list, "shipping", listing.shipping_mode);
      kv(list, "condition", listing.condition);
      kv(list, "seller", listing.seller_name);
      kv(list, "seller since", listing.seller_since);
      kv(list, "seller type", listing.seller_type);
      kv(list, "badges", (listing.seller_badges || []).join(", "));
      kv(list, "active ads", listing.seller_active_ads);
      kv(list, "posted", listing.posted_at);
      drawer.appendChild(list);
      if (listing.description) {
        drawer.appendChild(el("h3", null, "description"));
        drawer.appendChild(el("div", "pre", listing.description));
      }
    }
  }).catch((error) => {
    drawer.textContent = "";
    drawer.appendChild(el("div", "muted", "could not load the listing: " + error.message));
  });
}

function histogram(prices, median) {
  const wrap = el("div", "hist");
  if (!prices.length) return wrap;
  const min = Math.min(...prices), max = Math.max(...prices);
  const bins = 12;
  const counts = new Array(bins).fill(0);
  for (const price of prices) {
    const index = max === min ? 0 : Math.min(bins - 1, Math.floor(((price - min) / (max - min)) * bins));
    counts[index] += 1;
  }
  const peak = Math.max(...counts);
  counts.forEach((count, index) => {
    const bar = el("div", "bar");
    bar.style.height = Math.max(3, Math.round((count / peak) * 70)) + "px";
    if (count) bar.appendChild(el("span", null, count));
    wrap.appendChild(bar);
  });
  return wrap;
}

function sparkline(points) {
  const wrap = el("div", "spark");
  if (!points.length) return wrap;
  const values = points.map((point) => point.median);
  const min = Math.min(...values), max = Math.max(...values);
  points.forEach((point, index) => {
    const bar = el("div", "bar" + (index === points.length - 1 ? " last" : ""));
    const height = max === min ? 40 : 8 + Math.round(((point.median - min) / (max - min)) * 40);
    bar.style.height = height + "px";
    bar.title = point.day + ": " + point.median + " EUR (" + point.n + ")";
    wrap.appendChild(bar);
  });
  return wrap;
}

function openPrice(componentId) {
  const drawer = $("#drawer");
  $("#scrim").classList.add("open");
  drawer.classList.add("open");
  drawer.textContent = "";
  drawer.appendChild(el("div", "muted", "loading…"));

  api("/api/prices/" + encodeURIComponent(componentId)).then((data) => {
    const component = data.component || {};
    drawer.textContent = "";

    const head = el("h2", null, component.label || componentId);
    drawer.appendChild(head);
    drawer.appendChild(el("div", "muted mono", component.component_id));

    const badges = el("div");
    badges.style.margin = "10px 0";
    badges.appendChild(el("span", "badge b-kind", component.kind || ""));
    badges.appendChild(document.createTextNode(" "));
    badges.appendChild(el("span", "badge " + (component.source === "market" ? "b-market" : "b-estimate"),
      component.source === "market" ? "market value" : "estimate"));
    drawer.appendChild(badges);

    const list = el("dl", "kv");
    kv(list, "FMV (effective)", fmtEur(component.fmv_eur));
    kv(list, "committed table", fmtEur(component.curated_eur));
    kv(list, "samples behind it", component.sample_n || 0);
    kv(list, "confidence", component.confidence);
    kv(list, "last measured", component.updated_at || "-");
    kv(list, "TDP", component.tdp_w ? component.tdp_w + " W" : "");
    drawer.appendChild(list);

    if ((component.aliases || []).length) {
      drawer.appendChild(el("h3", null, "matched aliases"));
      const chips = el("div");
      for (const alias of component.aliases) chips.appendChild(el("span", "chip", alias));
      drawer.appendChild(chips);
    }

    const distribution = data.distribution || {};
    drawer.appendChild(el("h3", null, "market samples"));
    const dist = el("dl", "kv");
    kv(dist, "count", distribution.count || 0);
    kv(dist, "min", distribution.min !== null && distribution.min !== undefined ? fmtEur(distribution.min) : "");
    kv(dist, "median", distribution.median !== null && distribution.median !== undefined ? fmtEur(distribution.median) : "");
    kv(dist, "max", distribution.max !== null && distribution.max !== undefined ? fmtEur(distribution.max) : "");
    drawer.appendChild(dist);
    const prices = (data.samples || []).map((sample) => sample.price);
    if (prices.length) {
      drawer.appendChild(histogram(prices, distribution.median));
    } else {
      drawer.appendChild(
        el("div", "muted", "no market samples yet - the harvester has not measured this component")
      );
    }

    if ((data.trend || []).length) {
      drawer.appendChild(el("h3", null, "price trend (daily median)"));
      drawer.appendChild(sparkline(data.trend));
      const table = el("table", "mini");
      const head = el("tr");
      ["day", "median", "n"].forEach((label) => head.appendChild(el("th", null, label)));
      table.appendChild(head);
      for (const point of data.trend.slice().reverse()) {
        const row = el("tr");
        row.appendChild(el("td", null, point.day));
        row.appendChild(el("td", "num", fmtEur(point.median)));
        row.appendChild(el("td", "num", point.n));
        table.appendChild(row);
      }
      drawer.appendChild(table);
    }

    if ((data.samples || []).length) {
      drawer.appendChild(el("h3", null, "the samples"));
      const table = el("table", "mini");
      const head = el("tr");
      ["price", "listing", "date"].forEach((label) => head.appendChild(el("th", null, label)));
      table.appendChild(head);
      for (const sample of data.samples) {
        const row = el("tr");
        row.appendChild(el("td", "num", fmtEur(sample.price)));
        row.appendChild(el("td", null, sample.title || "-"));
        row.appendChild(el("td", null, (sample.at || "").slice(0, 10)));
        table.appendChild(row);
      }
      drawer.appendChild(table);
    }
  }).catch((error) => {
    drawer.textContent = "";
    drawer.appendChild(el("div", "muted", "could not load the component: " + error.message));
  });
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#scrim").classList.remove("open");
}

function renderStatsCards(stats) {
  const cards = stats.cards || {};
  $("#cards").textContent = "";
  card("ads", fmtNum(cards.ads || 0), "unique in range");
  card("valued", fmtNum(cards.valued || 0), "reached valuation");
  card("alerts", fmtNum(cards.alerts || 0), "discord alerts");
  card("alert rate", fmtPct(cards.alert_rate_pct), "of valued ads");
  card("median margin", fmtPct(cards.median_margin_pct), "below market");
  card("potential", fmtEur(cards.potential_margin_eur), "alerted margin");
  card("coverage", fmtPct(cards.median_coverage_pct, 0), "median");
  card("fallback", fmtPct(cards.fallback_share_pct, 0), "regex extraction");
}

function renderStats(stats) {
  renderStatsCards(stats);
  const grid = $("#stats-grid");
  grid.textContent = "";
  const range = stats.range || {};
  const days = Number(range.days || 0);
  const period = days === 0 ? "all retained data" : days === 1 ? "last 24 h" : "last " + days + " days";
  const cohort = fmtNum(range.cohort || 0);
  const margin = stats.margin || {};
  const risk = stats.risk || {};
  const hardware = stats.hardware || {};
  const mix = stats.listing_mix || {};
  const alerts = stats.alerts || {};
  const quality = stats.quality || {};
  const market = stats.market || {};
  const ops = stats.ops || {};

  statsSection(grid, "Core", period + " · " + cohort + " unique ads");
  statsPanel(
    grid,
    "Decision funnel",
    "furthest stage reached by each ad",
    makeBars(stats.funnel, {
      format: (item) => fmtNum(item.count) + " · " + fmtPct(item.pct, 0),
    }),
  );
  statsPanel(
    grid,
    "Outcome & band mix",
    "current decision state and valuation band",
    statsColumns(
      labeledBlock("outcome", makeStacked(withLabels(stats.outcomes))),
      labeledBlock("valuation band", makeStacked(withLabels(stats.bands))),
    ),
  );
  statsPanel(
    grid,
    "Activity timeline",
    "daily events; errors and cycles are global operations",
    makeLineChart(stats.series, ["ads", "valued", "alerts", "errors"]),
    true,
  );
  statsPanel(
    grid,
    "Rejection reasons",
    "share of rejected ads by filter reason",
    makeBars((stats.rejections || []).map((item) => ({ ...item, label: item.code })), {
      sub: (item) => item.stage,
      format: (item) => fmtNum(item.count) + " · " + fmtPct(item.pct, 0),
    }),
  );
  statsPanel(
    grid,
    "Margin distribution",
    "n " + fmtNum(margin.count || 0) +
      " · P25 " + fmtPct(margin.p25) +
      " · median " + fmtPct(margin.median) +
      " · P75 " + fmtPct(margin.p75),
    makeHistogram(margin.bins, {
      markers: [
        { value: 5, label: "5 %" },
        { value: 12, label: "12 %" },
        { value: 30, label: "30 %" },
        { value: margin.median, label: "median" },
      ],
    }),
  );
  statsPanel(
    grid,
    "Risk profile",
    "risk level of valued ads and score distribution",
    statsColumns(
      labeledBlock("risk level", makeStacked(withLabels(risk.levels))),
      labeledBlock(
        "risk score",
        makeHistogram(risk.score_bins, { axisFormat: fmtAxis }),
      ),
    ),
    true,
  );
  statsPanel(
    grid,
    "Top risk signals",
    "what drives the risk score",
    makeBars(risk.signals, { format: (item) => fmtNum(item.count) }),
  );
  const hardwareBars = (rows) => makeBars(
    (rows || []).map((row) => ({
      ...row,
      label: row.label || row.key,
      sub: row.median_margin_pct !== null && row.median_margin_pct !== undefined
        ? "margin " + fmtPct(row.median_margin_pct) +
          (row.median_asking_eur !== null && row.median_asking_eur !== undefined
            ? " · " + fmtEur(row.median_asking_eur)
            : "")
        : null,
    })),
    { format: (item) => fmtNum(item.count) },
  );
  statsPanel(
    grid,
    "Hardware segments",
    "top extracted models and RAM sizes",
    statsColumns(
      labeledBlock("GPUs", hardwareBars(hardware.gpus)),
      labeledBlock("CPUs", hardwareBars(hardware.cpus)),
      labeledBlock("RAM", makeBars(
        (hardware.ram || []).map((row) => ({ ...row, label: row.label || row.key })),
        { format: (item) => fmtNum(item.count) },
      )),
    ),
    true,
  );
  statsPanel(
    grid,
    "Listing & seller mix",
    "detail-page metadata; only ads that reached the detail page",
    statsColumns(
      labeledBlock("shipping", makeStacked(withLabels(mix.shipping))),
      labeledBlock("seller type", makeStacked(withLabels(mix.seller_type))),
      labeledBlock("condition", makeStacked(withLabels(mix.condition))),
      labeledBlock("badges", makeStacked(withLabels(mix.badges))),
      labeledBlock("negotiable", makeStacked(withLabels(mix.negotiable))),
      labeledBlock("account type", makeStacked(withLabels(mix.pro))),
    ),
    true,
  );
  statsPanel(
    grid,
    "Alert quality",
    "alerts by band and risk, latency from first seen",
    statsColumns(
      labeledBlock("alert band", makeStacked(withLabels(alerts.by_band))),
      labeledBlock("alert risk", makeStacked(withLabels(alerts.by_risk))),
      metricList([
        { label: "alerts", value: fmtNum(alerts.count || 0) },
        { label: "median margin", value: fmtPct(alerts.median_margin_pct) },
        { label: "median latency", value: fmtSeconds(alerts.median_latency_s) },
      ]),
    ),
    true,
  );
  statsPanel(
    grid,
    "Data quality",
    "valuation coverage and missing joins",
    statsColumns(
      metricList([
        { label: "valued", value: fmtNum(quality.with_valuation || 0) },
        { label: "missing listing", value: fmtNum(quality.missing_listing || 0) },
        { label: "missing extraction", value: fmtNum(quality.missing_extraction || 0) },
        { label: "fallback share", value: fmtPct(quality.fallback_share_pct), sub: "regex extraction" },
      ]),
      labeledBlock(
        "coverage",
        makeHistogram(quality.coverage_bins, {
          axisFormat: (value) => fmtRatio(value, 0),
        }),
      ),
    ),
    true,
  );

  statsSection(grid, "Deep dive", "v2 analyses from the same cohort");
  statsPanel(
    grid,
    "Query performance",
    "which searches find ads, valuations and alerts",
    makeTable(stats.queries, [
      { key: "tag", label: "query" },
      { key: "ads", label: "ads", align: "num" },
      { key: "valued", label: "valued", align: "num" },
      { key: "alerts", label: "alerts", align: "num" },
      { key: "alert_rate_pct", label: "alert rate", align: "num", format: (item) => fmtPct(item.alert_rate_pct) },
      { key: "median_margin_pct", label: "median margin", align: "num", format: (item) => fmtPct(item.median_margin_pct) },
    ]),
    true,
  );
  const freshness = quality.price_freshness || {};
  statsPanel(
    grid,
    "Price freshness",
    "search-card price vs. the seller's current price",
    metricList([
      { label: "price pairs", value: fmtNum(freshness.with_both_prices || 0) },
      { label: "card ≠ detail", value: fmtNum(freshness.changed || 0), sub: fmtPct(freshness.changed_share_pct) },
      { label: "median delta", value: fmtEur(freshness.median_delta_eur) },
    ]),
  );
  const age = stats.age || {};
  statsPanel(
    grid,
    "Ad age",
    "age at first sighting, from the detail page",
    statsColumns(
      makeBars((age.bins || []).map((bin) => ({ ...bin, label: bin.key })), {
        format: (item) => fmtNum(item.count),
      }),
      metricList([
        { label: "median alerted age", value: age.median_alert_age_days === null || age.median_alert_age_days === undefined ? "-" : fmtNum(age.median_alert_age_days) + " d" },
        { label: "stale share", value: fmtPct(age.stale_share_pct), sub: fmtNum(age.known || 0) + " known ages" },
      ]),
    ),
    true,
  );
  statsPanel(
    grid,
    "Seller concentration",
    "sellers with more than one detail-page listing",
    statsColumns(
      makeTable((stats.sellers || {}).top, [
        { key: "seller", label: "seller" },
        { key: "ads", label: "ads", align: "num" },
      ]),
      metricList([
        { label: "distinct sellers", value: fmtNum((stats.sellers || {}).distinct || 0) },
        { label: "multi-listing", value: fmtNum((stats.sellers || {}).multi || 0) },
      ]),
    ),
    true,
  );
  const geography = stats.geography || {};
  statsPanel(
    grid,
    "Geography",
    "top cities and regions by listing count",
    statsColumns(
      makeTable((geography.cities || []).map((row) => ({ ...row, margin: fmtPct(row.median_margin_pct) })), [
        { key: "city", label: "city" },
        { key: "count", label: "ads", align: "num" },
        { key: "margin", label: "median margin", align: "num" },
      ]),
      makeTable((geography.regions || []).map((row) => ({ ...row, margin: fmtPct(row.median_margin_pct) })), [
        { key: "region", label: "region" },
        { key: "count", label: "ads", align: "num" },
        { key: "margin", label: "median margin", align: "num" },
      ]),
    ),
    true,
  );
  const calibration = market.calibration || {};
  statsPanel(
    grid,
    "Market health",
    "component table and complete-system calibration",
    metricList([
      { label: "components", value: fmtNum(market.components || 0), sub: fmtNum(market.measured || 0) + " measured · " + fmtNum(market.estimated || 0) + " estimates" },
      { label: "stale components", value: fmtNum(market.stale_components || 0), sub: "older than " + fmtNum(market.stale_after_days || 0) + " d" },
      { label: "calibration", value: calibration.factor === null || calibration.factor === undefined ? "uncalibrated" : Number(calibration.factor).toFixed(3) + "×", sub: fmtNum(calibration.samples || 0) + " systems" },
      { label: "market samples", value: fmtNum(market.systems || 0), sub: "price observations " + fmtNum(market.observations || 0) },
    ]),
  );
  statsPanel(
    grid,
    "LLM & operations",
    "requests, tokens and cycle health",
    statsColumns(
      makeLineChart(ops.llm, ["requests", "errors", "quota_429"]),
      metricList([
        { label: "cycles", value: fmtNum(ops.cycles || 0), sub: fmtNum(ops.error_cycles || 0) + " with errors" },
        { label: "cycle median", value: fmtSeconds(ops.cycle_median_s) },
        { label: "LLM requests", value: fmtNum(ops.llm_requests || 0) },
        { label: "LLM tokens", value: fmtNum(ops.llm_tokens || 0) },
        { label: "LLM errors", value: fmtNum(ops.llm_errors || 0), sub: fmtNum(ops.llm_quota_429 || 0) + " × 429" },
        { label: "ads seen", value: fmtNum(ops.ads_seen || 0), sub: fmtNum(ops.ads_new || 0) + " new · " + fmtNum(ops.ads_evaluated || 0) + " evaluated" },
      ]),
    ),
    true,
  );
}

async function refresh(forceStats = false) {
  try {
    const summary = await api("/api/summary");
    renderCalibration(summary);
    renderQueryOptions(summary);
    if (state.view === "listings") {
      renderCards(summary);
      const payload = await api("/api/ads", Object.assign({ limit: state.limit, offset: state.offset }, filters()));
      renderRows(payload);
    } else if (state.view === "statistics") {
      if (!forceStats && Date.now() - state.statsLoadedAt < 60000) {
        $("#status").textContent = "updated " + new Date().toLocaleTimeString();
        return;
      }
      const payload = await api("/api/statistics", {
        days: $("#stat-days").value,
        query_tag: $("#stat-query").value,
      });
      state.stats = payload;
      state.statsLoadedAt = Date.now();
      renderStats(payload);
    } else {
      const payload = await api("/api/prices");
      state.prices = payload.prices || [];
      renderPrices();
    }
    $("#status").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (error) {
    $("#status").textContent = "error: " + error.message;
  }
}

function switchView(view) {
  if (view === "statistics" && state.view !== "statistics") state.statsLoadedAt = 0;
  state.view = view;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === view);
  });
  $("#view-listings").hidden = view !== "listings";
  $("#view-statistics").hidden = view !== "statistics";
  $("#view-prices").hidden = view !== "prices";
  $("#filters-listings").hidden = view !== "listings";
  $("#filters-statistics").hidden = view !== "statistics";
  refresh();
}

// --- the price table ---
function priceFilters() {
  return {
    q: $("#pq").value.trim().toLowerCase(),
    kind: $("#pkind").value,
    source: $("#psource").value,
  };
}

function renderPrices() {
  const filter = priceFilters();
  let rows = state.prices.filter((row) => {
    if (filter.kind && row.kind !== filter.kind) return false;
    if (filter.source && row.source !== filter.source) return false;
    if (filter.q) {
      const hay = [row.label, row.component_id, row.kind].concat(row.aliases || []).join(" ").toLowerCase();
      if (!hay.includes(filter.q)) return false;
    }
    return true;
  });

  const sort = $("#psort").value;
  rows.sort((a, b) => {
    if (sort === "value") return b.fmv_eur - a.fmv_eur;
    if (sort === "name") return a.label.localeCompare(b.label);
    if (sort === "updated") return String(b.updated_at).localeCompare(String(a.updated_at));
    return a.kind.localeCompare(b.kind) || b.fmv_eur - a.fmv_eur;
  });

  const measured = state.prices.filter((row) => row.source === "market").length;
  const estimated = state.prices.length - measured;
  const lastHarvest = state.prices.map((row) => row.updated_at).filter(Boolean).sort().pop() || "-";
  $("#cards").textContent = "";
  card("components", state.prices.length, "in the effective table");
  card("measured", measured, "market value");
  card("estimates", estimated, "no market sample");
  card("last harvest", (lastHarvest || "-").slice(0, 10), "newest measurement");

  const tbody = $("#price-rows");
  tbody.textContent = "";
  for (const row of rows) {
    const tr = el("tr");
    const nameCell = el("td");
    nameCell.appendChild(el("div", null, row.label));
    nameCell.appendChild(el("div", "muted mono", row.component_id));
    tr.appendChild(nameCell);

    const kindCell = el("td");
    kindCell.appendChild(el("span", "badge b-kind", row.kind));
    tr.appendChild(kindCell);

    tr.appendChild(el("td", "num", fmtEur(row.fmv_eur)));

    const sourceCell = el("td");
    sourceCell.appendChild(el("span", "badge " + (row.source === "market" ? "b-market" : "b-estimate"),
      row.source === "market" ? "market" : "estimate"));
    tr.appendChild(sourceCell);

    tr.appendChild(el("td", "num", row.sample_n || "-"));
    tr.appendChild(el("td", "muted", row.updated_at ? row.updated_at.slice(0, 10) : "-"));
    tr.addEventListener("click", () => openPrice(row.component_id));
    tbody.appendChild(tr);
  }
  $("#price-info").textContent = rows.length + " of " + state.prices.length + " components";
}

function card(key, value, extra) {
  const box = el("div", "card");
  box.appendChild(el("div", "k", key));
  box.appendChild(el("div", "v", value));
  if (extra) box.appendChild(el("div", "x", extra));
  $("#cards").appendChild(box);
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
});

["#pq", "#pkind", "#psource", "#psort"].forEach((sel) => {
  const node = $(sel);
  const event = node.tagName === "INPUT" ? "input" : "change";
  node.addEventListener(event, () => renderPrices());
});

["#q", "#outcome", "#stage", "#query_tag", "#hours", "#order"].forEach((sel) => {
  const node = $(sel);
  const event = node.tagName === "INPUT" ? "input" : "change";
  node.addEventListener(event, () => { state.offset = 0; refresh(); });
});

["#stat-days", "#stat-query"].forEach((sel) => {
  $(sel).addEventListener("change", () => {
    state.statsLoadedAt = 0;
    refresh(true);
  });
});

$("#reload").addEventListener("click", () => refresh(true));
$("#stat-reload").addEventListener("click", () => {
  state.statsLoadedAt = 0;
  refresh(true);
});
$("#prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit); refresh();
});
$("#next").addEventListener("click", () => {
  state.offset = state.offset + state.limit; refresh();
});
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });

refresh();
setInterval(refresh, 15000);
