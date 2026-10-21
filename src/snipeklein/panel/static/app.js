/* SnipeKlein decision panel - vanilla JS, no dependencies.
 * Listing data is third party content: it is always inserted with
 * textContent, never as HTML. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { limit: 50, offset: 0, total: 0, view: "listings", prices: [] };

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

function outcomeBadge(outcome, band) {
  if (outcome === "near_miss") return el("span", "badge b-nearmiss", "near miss");
  if (outcome === "alerted") return el("span", "badge b-alerted", "alerted");
  if (outcome === "accepted") return el("span", "badge b-accepted", "accepted");
  if (band === "extreme") return el("span", "badge b-extreme", "rejected · extreme");
  return el("span", "badge b-rejected", "rejected");
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

  const select = $("#query_tag");
  const current = select.value;
  select.textContent = "";
  select.appendChild(new Option("any query", ""));
  for (const tag of summary.queries || []) select.appendChild(new Option(tag, tag));
  select.value = current;
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

    tr.appendChild(el("td", "num", fmtEur(ad.price)));

    const outcomeCell = el("td");
    outcomeCell.appendChild(outcomeBadge(ad.outcome, valuation.band));
    tr.appendChild(outcomeCell);

    const stageCell = el("td");
    stageCell.appendChild(el("span", "badge b-stage", ad.stage));
    tr.appendChild(stageCell);

    tr.appendChild(el("td", "muted", ad.reason || "-"));
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

async function refresh() {
  try {
    const summary = await api("/api/summary");
    renderCards(summary);
    if (state.view === "listings") {
      const payload = await api("/api/ads", Object.assign({ limit: state.limit, offset: state.offset }, filters()));
      renderRows(payload);
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
  state.view = view;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === view);
  });
  $("#view-listings").hidden = view !== "listings";
  $("#view-prices").hidden = view !== "prices";
  $("#filters-listings").hidden = view !== "listings";
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

$("#reload").addEventListener("click", refresh);
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
