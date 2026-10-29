"use strict";
const $ = (id) => document.getElementById(id);
const SATS = 1e8;
const fmt = (n, digits = 0) =>
  n == null
    ? "N/A"
    : Number(n).toLocaleString("en-US", { maximumFractionDigits: digits });
const amount = (n) => (n == null ? "N/A" : fmt(n, Math.abs(n) >= 100 ? 0 : 2));
const btc = (n) => (n == null ? "N/A" : amount(n / SATS));
// Relative fees in percent, as makers configure them (0.2%, 0.002%).
const rateValue = (n) =>
  n == null
    ? "N/A"
    : Number(n).toLocaleString("en-US", { maximumSignificantDigits: 2 });
const rate = (n) => (n == null ? "N/A" : `${rateValue(n)}%`);
const percent = (n) =>
  n == null
    ? "N/A"
    : `${Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2, ...(Math.abs(n) < 0.1 && n !== 0 ? { maximumSignificantDigits: 2, minimumSignificantDigits: 1 } : {}) })}%`;
let dataset;

function theme() {
  try {
    $("theme").value = localStorage.getItem("jmstats-theme") || "system";
  } catch {}
  if (!["system", "light", "dark"].includes($("theme").value))
    $("theme").value = "system";
  const apply = () => {
    const value = $("theme").value;
    if (value === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = value;
    try {
      localStorage.setItem("jmstats-theme", value);
    } catch {}
  };
  $("theme").addEventListener("change", apply);
  apply();
}
function cards(id, items, extra = "") {
  $(id).className = "cards " + extra;
  $(id).innerHTML = items
    .map(
      ([label, value, unit, note]) =>
        `<div class="card"><div class="label">${label}</div><div class="value">${value} <span class="unit">${unit}</span></div><div class="note">${note}</div></div>`,
    )
    .join("");
}
function activityChart(months) {
  if (!months.length) return '<p class="empty">No scanned activity yet.</p>';
  const w = Math.max(280, $("activity-chart").clientWidth),
    h = $("activity-chart").clientHeight,
    small = w < 600,
    left = small ? 48 : 62,
    right = small ? 55 : 70,
    top = 20,
    bottom = 30,
    pw = w - left - right,
    ph = h - top - bottom;
  const maxCount = Math.max(1, ...months.map((m) => m.count || 0)),
    maxVolume = Math.max(1, ...months.map((m) => (m.volume_sats || 0) / SATS));
  const step = pw / Math.max(1, months.length);
  let svg = "",
    line = "",
    connected = false;
  for (let i = 0; i <= 4; i++) {
    const y = top + (ph * i) / 4;
    svg += `<line class="axis" x1="${left}" x2="${w - right}" y1="${y}" y2="${y}"/><text x="${left - 10}" y="${y + 4}" text-anchor="end">${fmt(maxCount * (1 - i / 4))}</text><text x="${w - right + 10}" y="${y + 4}">${amount(maxVolume * (1 - i / 4))}</text>`;
  }
  months.forEach((m, i) => {
    const x = left + step * (i + 0.5);
    if (m.count == null) {
      connected = false;
      svg += `<text x="${x}" y="${h - bottom - 5}" text-anchor="middle">·<title>${m.month}: unscanned</title></text>`;
    } else {
      const bh = Math.max(1, (m.count / maxCount) * ph),
        y = top + ph - (m.volume_sats / SATS / maxVolume) * ph;
      svg += `<rect class="bar ${m.coverage === "complete" ? "" : "partial"}" x="${x - step * 0.35}" y="${top + ph - bh}" width="${Math.max(1, step * 0.7)}" height="${bh}" rx="1"><title>${m.month}: ${fmt(m.count)} transactions, ${btc(m.volume_sats)} BTC, ${m.coverage}</title></rect>`;
      line += `${connected ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
      connected = true;
    }
    if (i % Math.max(1, Math.ceil(months.length / (small ? 4 : 10))) === 0)
      svg += `<text x="${x}" y="${h - 5}" text-anchor="middle">${small && $("year").value !== "all" ? m.month.slice(5) : m.month}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Monthly transaction bars and equal-output volume line, with separate left and right axes"><text x="${left}" y="10">Transactions</text><text x="${w - right}" y="10" text-anchor="end">BTC</text>${svg}<path class="volume-line" d="${line}"/></svg>`;
}
function feeSummary(m) {
  return `${fmt(m.maker_fee_identified)} identified, ${fmt(m.maker_fee_ambiguous)} bounded, ${fmt(m.maker_fee_unresolved)} unresolved, ${fmt(m.maker_fee_unanalyzed)} unanalyzed; ${fmt(m.inconsistent)} inconsistent excluded`;
}
function makerFeesChart(months) {
  if (!months.length) return '<p class="empty">No scanned activity yet.</p>';
  if (!months.some((m) => m.count != null && m.maker_fee_estimate_sats != null))
    return '<p class="empty">No maker fee estimates for this period.</p>';
  const w = Math.max(280, $("maker-fees-chart").clientWidth),
    h = $("maker-fees-chart").clientHeight,
    small = w < 600,
    max = Math.max(
      1,
      ...months.map((m) =>
        Math.max(m.maker_fee_high_sats || 0, m.maker_fee_estimate_sats || 0),
      ),
    ),
    left = Math.max(55, fmt(max).length * 6 + 12),
    right = 20,
    top = 20,
    bottom = 30,
    pw = w - left - right,
    ph = h - top - bottom,
    step = pw / months.length,
    y = (v) => top + ph - (v / max) * ph;
  let svg = "";
  for (let i = 0; i <= 4; i++) {
    const gy = top + (ph * i) / 4;
    svg += `<line class="axis" x1="${left}" x2="${w - right}" y1="${gy}" y2="${gy}"/><text x="${left - 10}" y="${gy + 4}" text-anchor="end">${fmt(max * (1 - i / 4))}</text>`;
  }
  months.forEach((m, i) => {
    const x = left + step * (i + 0.5),
      bw = Math.max(1, step * 0.7),
      total = m.maker_fee_estimate_sats;
    if (m.count == null || total == null) {
      svg += `<text class="fee-gap" x="${x}" y="${h - bottom - 5}" text-anchor="middle">·<title>${m.month}: ${m.count == null ? "unscanned" : "no maker fee estimate; " + feeSummary(m)}</title></text>`;
    } else {
      const partial = m.coverage !== "complete" || m.maker_fee_partial,
        title = `<title>${m.month}: ${fmt(total)} sats estimated, ${fmt(m.maker_fee_exact_sats)} exact, ${fmt(m.maker_fee_unresolved_sats)} for unresolved; range ${fmt(m.maker_fee_low_sats)} to ${fmt(m.maker_fee_high_sats)}; ${feeSummary(m)}; ${m.coverage} block coverage</title>`,
        exactTop = y(m.maker_fee_exact_sats),
        totalTop = Math.min(y(total), top + ph - 1);
      svg += `<g class="fee-month">${title}`;
      svg += `<rect class="bar fee-imputed" x="${x - bw / 2}" y="${totalTop}" width="${bw}" height="${exactTop - totalTop}"/>`;
      svg += `<rect class="bar fee-exact" x="${x - bw / 2}" y="${exactTop}" width="${bw}" height="${top + ph - exactTop}"/>`;
      if (partial)
        svg += `<rect class="fee-partial" x="${x - bw / 2}" y="${totalTop}" width="${bw}" height="${top + ph - totalTop}"/>`;
      if (m.maker_fee_high_sats > m.maker_fee_low_sats) {
        const lo = y(m.maker_fee_low_sats),
          hi = y(m.maker_fee_high_sats),
          cap = Math.min(4, bw / 3);
        svg += `<path class="fee-range" d="M${x},${lo}V${hi}M${x - cap},${lo}H${x + cap}M${x - cap},${hi}H${x + cap}"/>`;
      }
      svg += "</g>";
    }
    if (i % Math.max(1, Math.ceil(months.length / (small ? 4 : 10))) === 0)
      svg += `<text x="${x}" y="${h - 5}" text-anchor="middle">${small && $("year").value !== "all" ? m.month.slice(5) : m.month}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Monthly estimated maker fee totals in sats: exact part, estimated part, and the range implied by proven per-transaction bounds">${svg}<text x="${left}" y="10">sats</text></svg>`;
}
function activity() {
  const months = dataset.months.filter(
    (m) => $("year").value === "all" || m.month.startsWith($("year").value),
  );
  const seen = months.filter((m) => m.count !== null),
    latest =
      [...seen].reverse().find((m) => m.coverage === "complete") || seen.at(-1);
  const label = (field) =>
    latest
      ? `${latest.month}, ${fmt(latest.maker_fee_fixed_slots)} exactly known makers; p90 ${field}`
      : "No fee observations";
  $("coverage").textContent =
    `${months.filter((m) => m.coverage === "complete").length} of ${months.length} months fully scanned. ${months.filter((m) => m.coverage === "unscanned").length} unscanned. Latest block: ${dataset.last_scanned_at ? new Date(dataset.last_scanned_at).toLocaleString("en-US", { timeZone: "UTC" }) + " UTC" : "none"}.`;
  cards("activity-cards", [
    [
      "Observed transactions",
      fmt(seen.reduce((s, m) => s + m.count, 0)),
      "",
      "Selected scanned blocks",
    ],
    [
      "Equal-output volume",
      btc(seen.reduce((s, m) => s + m.volume_sats, 0)),
      "BTC",
      "Includes repeated mixing",
    ],
    [
      "Median CJ size",
      fmt(latest?.median_size_sats),
      "sat",
      latest ? latest.month : "No observations",
    ],
    [
      "Median participants",
      amount(latest?.median_participants),
      "",
      latest ? latest.month : "No observations",
    ],
    [
      "Median maker fee",
      fmt(latest?.maker_fee_median_sats),
      "sat / maker",
      label(`${fmt(latest?.maker_fee_p90_sats)} sat`),
    ],
    [
      "Median relative maker fee",
      rateValue(latest?.maker_fee_median_percent),
      "%",
      label(rate(latest?.maker_fee_p90_percent)),
    ],
  ]);
  $("activity-chart").innerHTML = activityChart(months);
  $("maker-fees-chart").innerHTML = makerFeesChart(months);
  $("monthly-table").innerHTML = [...months]
    .reverse()
    .map(
      (m) =>
        `<tr><td>${m.month}</td><td>${m.coverage}</td><td>${fmt(m.count)}</td><td>${btc(m.volume_sats)}</td><td>${fmt(m.median_size_sats)}</td><td>${amount(m.median_participants)}</td><td>${fmt(m.maker_fee_estimate_sats)}${m.maker_fee_partial ? " (partial)" : ""}</td><td>${m.maker_fee_low_sats == null ? "N/A" : `${fmt(m.maker_fee_low_sats)} to ${fmt(m.maker_fee_high_sats)}`}</td><td>${fmt(m.maker_fee_exact_sats)}</td><td>${fmt(m.maker_fee_mean_sats)}</td><td>${rate(m.maker_fee_mean_percent)}</td><td>${fmt(m.maker_fee_median_sats)}</td><td>${rate(m.maker_fee_median_percent)}</td><td>${fmt(m.maker_fee_p90_sats)}</td><td>${rate(m.maker_fee_p90_percent)}</td><td>${fmt(m.maker_fee_fixed_slots)} / ${fmt(m.maker_fee_slots)}</td><td>${[m.maker_fee_identified, m.maker_fee_ambiguous, m.maker_fee_unresolved, m.maker_fee_unanalyzed].map((v) => fmt(v)).join(" / ")}</td><td>${fmt(m.inconsistent)}</td></tr>`,
    )
    .join("");
}
function historyChart(history) {
  // Rendering owns daily aggregation and spike preservation.
  const points = history.map((p) => ({
    day: p.as_of.slice(0, 10),
    makers: p.makers,
    bonded: p.advertised_bonded_makers ?? p.bonded_makers ?? null,
    locked: p.reported_locked_sats ?? p.locked_sats ?? null,
  }));
  if (!points.length) return '<p class="empty">No historical observations.</p>';
  const w = Math.max(280, $("market-chart").clientWidth),
    h = $("market-chart").clientHeight,
    small = w < 600,
    left = small ? 95 : 120,
    right = small ? 45 : 65,
    rowHeight = (h - 28) / 3,
    ph = rowHeight - 25;
  let svg = "";
  [
    ["makers", "Total makers", "maker-line", 1],
    ["bonded", "Bonded makers", "bonded-line", 1],
    ["locked", "Locked BTC", "capital-line", SATS],
  ].forEach(([field, label, cls, scale], row) => {
    const top = 10 + row * rowHeight,
      max = Math.max(1, ...points.map((p) => (p[field] || 0) / scale));
    let line = "",
      connected = false;
    svg += `<text x="0" y="${top + 10}">${label}</text><text x="0" y="${top + 29}">Peak ${amount(Math.max(0, ...points.map((p) => (p[field] || 0) / scale)))}</text><line class="axis" x1="${left}" x2="${w - right}" y1="${top + ph}" y2="${top + ph}"/>`;
    points.forEach((p, i) => {
      if (p[field] == null) {
        connected = false;
        return;
      }
      const x =
          left +
          (points.length === 1 ? 0.5 : i / (points.length - 1)) *
            (w - left - right),
        y = top + ph - (p[field] / scale / max) * ph;
      line += `${connected ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
      connected = true;
    });
    svg += `<path class="${cls}" d="${line}"/><text x="${w - right + 10}" y="${top + 12}">${amount(max)}</text><text x="${w - right + 10}" y="${top + ph + 3}">0</text>`;
  });
  for (
    let i = 0;
    i < points.length;
    i += Math.max(1, Math.ceil(points.length / (small ? 3 : 7)))
  ) {
    svg += `<text x="${left + (i / Math.max(1, points.length - 1)) * (w - left - right)}" y="${h - 4}" text-anchor="middle">${points[i].day.slice(5)}</text>`;
  }
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Orderbook history: total makers including spam, bonded makers, and total reported locked BTC">${svg}</svg>`;
}
function market() {
  const m = dataset.market;
  if (!m) {
    $("market-cards").innerHTML =
      '<p class="empty">No current orderbook snapshot.</p>';
    $("market-chart").innerHTML = historyChart(dataset.market_history);
    return;
  }
  $("market-time").textContent =
    new Date(m.as_of).toLocaleString("en-US", { timeZone: "UTC" }) + " UTC";
  cards(
    "market-cards",
    [
      ["Total makers", fmt(m.makers), "", "Unique advertised nicknames"],
      [
        "Bonded makers",
        fmt(m.advertised_bonded_makers ?? m.bonded_makers),
        "",
        "Positive advertised bond weight",
      ],
      [
        "Bond locked",
        btc(m.reported_locked_sats ?? m.locked_sats),
        "BTC",
        "Unique reported unexpired bonds",
      ],
      ["Total offers", fmt(m.offer_count), "", "Includes spam advertisements"],
    ],
    "market-cards",
  );
  $("market-chart").innerHTML = historyChart(dataset.market_history);
  $("capacity-table").innerHTML = m.capacity
    .map(
      (c) =>
        `<tr><td>${fmt(c.amount)}</td><td>${fmt(c.makers)}</td><td>${fmt(c.fee.median)}</td></tr>`,
    )
    .join("");
  $("advertised-fees").innerHTML =
    `<div class="fee-row"><span>Absolute, median / mean</span><strong>${fmt(m.absolute_fees.median)} / ${fmt(m.absolute_fees.mean)} sat</strong></div><div class="fee-row"><span>Relative, median / mean</span><strong>${rate(m.relative_fees.median == null ? null : m.relative_fees.median * 100)} / ${rate(m.relative_fees.mean == null ? null : m.relative_fees.mean * 100)}</strong></div>`;
}
function maker() {
  const m = dataset.maker_model;
  const comparison = m?.windows?.length
    ? `<article class="panel" id="maker-comparison"><h3>Activity-window comparison</h3><div class="table-wrap"><table><thead><tr><th>Window</th><th>Median CJ, sat</th><th>Median makers</th><th>Bond, BTC</th><th>Fee</th><th>Per fill, sat</th><th>CJs / month</th><th>Net sat / month</th><th>Annual ROI</th></tr></thead><tbody>${m.windows.map((w) => `<tr><td>${w.window_days} days</td><td>${fmt(w.median_cj_sats)}</td><td>${fmt(w.median_makers, 1)}</td><td>${fmt(w.bond_sats == null ? null : w.bond_sats / SATS, 2)}</td><td>${rate(w.relative_fee_percent)}</td><td>${fmt(w.per_fill_sats)}</td><td>${fmt(w.monthly_cjs, 2)}</td><td>${fmt(w.monthly_net_sats)}</td><td>${fmt(w.annual_roi_percent, 2)}${w.annual_roi_percent == null ? "" : "%"}</td></tr>`).join("")}</tbody></table></div><p id="maker-window-coverage" class="caption"></p></article>`
    : "";
  if (!m || m.status !== "ok") {
    $("maker-result").innerHTML =
      '<p class="empty" id="maker-unavailable"></p>' + comparison;
    $("maker-unavailable").textContent =
      m?.reason || "Historical CJ fee and bond-weight evidence is needed.";
    if ($("maker-window-coverage"))
      $("maker-window-coverage").textContent = m.windows
        .map((w) => `${w.window_days} days: ${w.reason || "available"}`)
        .join(" ");
    return;
  }
  $("maker-period").textContent = `${m.window_days}-day typical-round estimate`;
  const curve = `<article class="panel" id="maker-curve"><h3>Annual ROI by bond size</h3><p id="maker-bond-note" class="caption"></p><div class="table-wrap"><table><thead><tr><th>Bond, BTC</th><th>Annual ROI</th><th>Selection probability</th><th>CJs / month</th><th>Net sat / month</th></tr></thead><tbody>${(m.bond_curve || []).map((c) => `<tr${c.optimal ? ' class="optimal"' : ""}><td>${fmt(c.bond_sats / SATS, 2)}${c.optimal ? " (optimal)" : ""}</td><td>${percent(c.annual_roi_percent)}</td><td>${percent(100 * c.selection_probability)}</td><td>${fmt(c.monthly_cjs, 1)}</td><td>${fmt(c.monthly_net_sats)}</td></tr>`).join("")}</tbody></table></div></article>`;
  $("maker-result").innerHTML =
    '<div id="maker-cards"></div><article class="panel"><p id="maker-fee" class="maker-fee"></p><p id="maker-drivers" class="caption"></p></article>' +
    curve +
    comparison;
  cards(
    "maker-cards",
    [
      [
        "Optimal relative fee",
        rateValue(m.relative_fee_percent),
        "%",
        `${fmt(m.per_fill_sats)} sat per typical CJ`,
      ],
      [
        "Optimal modeled bond",
        fmt(m.bond_sats / SATS, 2),
        "BTC",
        `${m.lock_years}-year lock${m.boundary ? "; " + m.boundary + " search bound" : ""}`,
      ],
      [
        "Expected monthly CJs",
        amount(m.monthly_cjs),
        "",
        "Unlimited offer liquidity",
      ],
      [
        "Expected monthly net fees",
        fmt(m.monthly_net_sats),
        "sat",
        `${fmt(m.per_fill_sats)} sat per fill`,
      ],
      [
        "Annual ROI on bond",
        fmt(m.annual_roi_percent, 2),
        "%",
        "12 × monthly net fees / bond",
      ],
    ],
    "maker-grid",
  );
  $("maker-fee").textContent = m.fee_description;
  $("maker-drivers").textContent = m.driver_note;
  $("maker-bond-note").textContent = m.bond_note;
  $("maker-assumptions").textContent = m.assumptions;
  $("maker-window-coverage").textContent = m.windows
    .map(
      (w) =>
        `${w.window_days} days: ${fmt(w.fee_samples)} fee observations (${fmt(w.fee_outliers)} outliers excluded); ${fmt(w.bond_matches)} of ${fmt(w.activity_cjs)} CJs matched to historical bond weights${w.bond_sample_start ? " (" + w.bond_sample_start + " to " + w.bond_sample_end + ")" : ""}.`,
    )
    .join(" ");
}
async function start() {
  theme();
  try {
    const response = await fetch("data.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dataset = await response.json();
    if (dataset.schema_version !== 2)
      throw new Error("Unsupported data version");
    // Hide leading unscanned months, but retain later gaps and scanned zeroes.
    const firstScanned = dataset.months.findIndex((m) => m.count !== null);
    dataset.months = firstScanned < 0 ? [] : dataset.months.slice(firstScanned);
    $("updated").textContent =
      `Rendered ${new Date(dataset.generated_at).toLocaleString("en-US", { timeZone: "UTC" })} UTC`;
    const years = [
      ...new Set(
        dataset.months
          .filter((m) => m.count !== null)
          .map((m) => m.month.slice(0, 4)),
      ),
    ].reverse();
    for (const year of years) $("year").add(new Option(year, year));
    $("year").value = years[0] || "all";
    $("year").addEventListener("change", activity);
    window.addEventListener("resize", () => {
      activity();
      $("market-chart").innerHTML = historyChart(dataset.market_history);
    });
    activity();
    market();
    maker();
  } catch (error) {
    $("error").hidden = false;
    $("error").textContent =
      `Unable to load data: ${error.message}. Serve this directory over HTTP.`;
  }
}
start();
