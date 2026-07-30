const COHORTS = {
  official_forward_paper: "Official paper",
  alphaops_signal_research: "Research observations",
  alphaops_research: "Research observations",
  historical_backtest: "Historical backtest",
  shadow_challenger: "Shadow challenger",
};

const PAGE_SIZE = 10;
const state = {
  data: null,
  readiness: null,
  manifest: null,
  performancePage: 0,
  researchPage: 0,
};

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});

document.querySelectorAll("[data-table][data-direction]").forEach((button) => {
  button.addEventListener("click", () => changePage(button.dataset.table, Number(button.dataset.direction)));
});

function showView(name) {
  document.querySelectorAll(".page-view").forEach((view) => {
    view.classList.toggle("is-visible", view.id === `view-${name}`);
  });
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("is-active", link.dataset.view === name);
    link.setAttribute("aria-pressed", String(link.dataset.view === name));
  });
  document.getElementById(`view-${name}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function changePage(table, direction) {
  if (!["performance", "research"].includes(table) || ![-1, 1].includes(direction)) return;
  const key = `${table}Page`;
  state[key] = Math.max(0, Number(state[key] || 0) + direction);
  if (table === "performance") {
    renderPerformance(Array.isArray(state.data?.daily) ? state.data.daily : []);
  } else {
    renderResearch(Array.isArray(state.data?.rows) ? state.data.rows : []);
  }
}

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const payload = await response.json();
  return { payload, status: response.status };
}

async function init() {
  try {
    const [snapshot, readiness, manifest] = await Promise.all([
      loadJson("/data/performance.json"),
      loadJson("/readiness.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/performance.json.manifest.json").catch(() => ({ payload: {}, status: 0 })),
    ]);
    state.data = snapshot.payload;
    state.readiness = readiness;
    state.manifest = manifest.payload;
    render();
  } catch (error) {
    document.getElementById("header-status").textContent = "Snapshot unavailable";
    document.getElementById("app-alert").textContent = "The public snapshot could not be loaded. No return is being shown.";
    document.getElementById("paper-summary").textContent = "No verified snapshot is available right now.";
  }
}

function render() {
  const data = state.data || { daily: [], rows: [] };
  const daily = Array.isArray(data.daily) ? data.daily : [];
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const official = daily.filter((item) => item.cohort === "official_forward_paper");
  const latest = official[0];
  const missing = daily.reduce((total, item) => total + numberOrZero(item.missing_outcome_count), 0);
  document.getElementById("header-status").textContent = latest ? `Updated ${latest.market_date}` : "No paper result yet";
  document.getElementById("footer-date").textContent = latest ? `Latest record ${latest.market_date}` : "Snapshot date pending";
  document.getElementById("kpi-date").textContent = latest?.market_date || "Not reported";
  document.getElementById("kpi-date-note").textContent = latest ? labelForStatus(latest.status) : "Waiting for snapshot";
  document.getElementById("kpi-return").innerHTML = formatPercent(latest?.return_pct);
  document.getElementById("kpi-return-note").textContent = latest?.return_pct == null ? "Cost inputs incomplete" : "Net after sourced costs";
  document.getElementById("kpi-cumulative").innerHTML = formatPercent(latest?.cumulative_return_pct);
  document.getElementById("kpi-benchmark").innerHTML = formatPercent(latest?.benchmark_return_pct);
  document.getElementById("kpi-excess").innerHTML = formatPercent(latest?.excess_return_pct);
  document.getElementById("kpi-pnl").innerHTML = formatMoney(latest?.net_pnl_cents);
  document.getElementById("kpi-drawdown").innerHTML = formatPercent(latest?.drawdown_pct);
  document.getElementById("kpi-open").textContent = latest?.unrealized_trade_count == null ? "Not reported" : String(latest.unrealized_trade_count);
  const coverage = latest?.coverage?.coverage_pct;
  document.getElementById("kpi-coverage").innerHTML = coverage == null ? '<span class="value-muted">Not reported</span>' : `${Number(coverage).toFixed(1)}%`;
  document.getElementById("kpi-coverage-note").textContent = `${missing} unresolved outcome${missing === 1 ? "" : "s"} excluded`;
  const readinessStatus = state.readiness?.payload?.status;
  document.getElementById("kpi-system").textContent = readinessStatus === "ready" ? "Ready" : readinessStatus === "not_ready" ? "Not ready" : "Not reported";
  document.getElementById("kpi-system-note").textContent = state.readiness?.payload?.http_status ? `Readiness HTTP ${state.readiness.payload.http_status}` : "Readiness is separate from liveness";
  document.getElementById("kpi-context").textContent = latest ? returnContext(latest) : "Official paper context pending: cohort, period, denominator, cost treatment, coverage, and as-of time will appear with the latest record.";
  renderOverview(official, latest);
  renderPerformance(daily);
  renderResearch(rows);
  renderSystem(state.readiness, state.manifest, data);
}

function renderOverview(official, latest) {
  const status = latest ? labelForStatus(latest.status) : "No data";
  setStatus("overview-pill", status, latest?.status);
  setStatus("paper-status", status, latest?.status);
  const summary = document.getElementById("paper-summary");
  if (!latest) {
    summary.textContent = "No official paper record has been published yet. This is a data state, not a zero-return claim.";
  } else if (latest.return_pct == null) {
    summary.innerHTML = `The latest paper day was <strong>${escapeHtml(latest.market_date)}</strong>. Its gross observed return was <strong>${formatPercent(latest.gross_return_pct)}</strong>, but the complete after-cost result is not reported because at least one cost input is missing.`;
  } else {
    summary.innerHTML = `The latest official paper day was <strong>${escapeHtml(latest.market_date)}</strong>, with a net after-cost result of <strong>${formatPercent(latest.return_pct)}</strong>.`;
  }
  const bars = document.getElementById("paper-days");
  const recent = official.slice(0, 5).reverse();
  const scale = Math.max(...recent.map((item) => Math.abs(numberOrZero(item.gross_return_pct))), 1);
  bars.innerHTML = recent.length ? recent.map((item) => {
    const value = numberOrZero(item.gross_return_pct);
    const height = Math.max(5, Math.round(Math.abs(value) / scale * 78));
    const label = `${shortDate(item.market_date)}: ${formatPercentText(item.gross_return_pct)}`;
    return `<div class="mini-bar"><span class="mini-bar-fill ${value < 0 ? "negative" : ""}" style="height:${height}px" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></span><span class="mini-bar-label">${escapeHtml(shortDate(item.market_date))}</span></div>`;
  }).join("") : "<span class=\"muted\">No official paper days yet.</span>";
}

function renderPerformance(daily) {
  const body = document.getElementById("performance-table");
  document.getElementById("performance-updated").textContent = state.data?.generated_at ? `Snapshot ${formatTimestamp(state.data.generated_at)}` : "";
  if (!daily.length) {
    body.innerHTML = '<tr><td colspan="7">No published performance rows.</td></tr>';
    updatePager("performance", 0, 0, 0);
    return;
  }
  const pageCount = Math.ceil(daily.length / PAGE_SIZE);
  state.performancePage = Math.min(state.performancePage, pageCount - 1);
  const start = state.performancePage * PAGE_SIZE;
  const visible = daily.slice(start, start + PAGE_SIZE);
  body.innerHTML = visible.map((item) => `<tr>
    <td>${escapeHtml(item.market_date || "Not reported")}</td>
    <td><span class="cohort-label ${item.cohort === "official_forward_paper" ? "official" : ""}">${escapeHtml(COHORTS[item.cohort] || item.cohort || "Not reported")}</span></td>
    <td>${statusChip(item.status)}</td>
    <td class="${numberOrZero(item.gross_return_pct) < 0 ? "value-bad" : ""}">${formatPercent(item.gross_return_pct)}</td>
    <td class="${numberOrZero(item.return_pct) < 0 ? "value-bad" : "value-good"}">${formatPercent(item.return_pct)}</td>
    <td>${formatPercent(item.excess_return_pct)}</td>
    <td>${numberOrZero(item.unrealized_trade_count) + numberOrZero(item.missing_outcome_count) + numberOrZero(item.quarantined_count)}</td>
  </tr>`).join("");
  updatePager("performance", start, visible.length, daily.length);
}

function renderResearch(rows) {
  const body = document.getElementById("research-table");
  const research = rows.filter((row) => ["alphaops_signal_research", "alphaops_research"].includes(row.cohort));
  if (!research.length) {
    body.innerHTML = '<tr><td colspan="6">No research observations are published.</td></tr>';
    updatePager("research", 0, 0, 0);
    return;
  }
  const pageCount = Math.ceil(research.length / PAGE_SIZE);
  state.researchPage = Math.min(state.researchPage, pageCount - 1);
  const start = state.researchPage * PAGE_SIZE;
  const visible = research.slice(start, start + PAGE_SIZE);
  body.innerHTML = visible.map((row) => `<tr>
    <td>${escapeHtml(row.market_date || "Not reported")}</td><td><strong>${escapeHtml(row.ticker || "Not reported")}</strong></td>
    <td>${statusChip(row.record_status)}</td><td>${formatPercent(row.gross_return_pct)}</td>
    <td>${formatPercent(row.return_pct)}</td><td>${row.source_refs?.length ? `${row.source_refs.length} reference${row.source_refs.length === 1 ? "" : "s"}` : "Not reported"}</td>
  </tr>`).join("");
  updatePager("research", start, visible.length, research.length);
}

function updatePager(table, start, visibleCount, total) {
  const page = Number(state[`${table}Page`] || 0);
  const status = document.getElementById(`${table}-page-status`);
  const previous = document.querySelector(`[data-table="${table}"][data-direction="-1"]`);
  const next = document.querySelector(`[data-table="${table}"][data-direction="1"]`);
  status.textContent = total ? `Showing ${start + 1}–${start + visibleCount} of ${total}` : "No rows to show";
  previous.disabled = page <= 0;
  next.disabled = start + visibleCount >= total;
}

function renderSystem(readiness, manifest, data) {
  const readinessPayload = readiness?.payload || {};
  const readinessStatus = readinessPayload.status || "not_reported";
  setStatus("system-pill", readinessStatus === "ready" ? "Ready" : "Needs attention", readinessStatus === "ready" ? "COMPLETE" : "DEGRADED");
  setStatus("readiness-status", readinessStatus === "ready" ? "Ready" : "Not ready", readinessStatus === "ready" ? "COMPLETE" : "DEGRADED");
  document.getElementById("readiness-details").innerHTML = detailRows([
    ["Publication", readinessStatus === "ready" ? "Complete" : "Not ready", readinessStatus === "ready"],
    ["HTTP readiness", readinessPayload.http_status ?? "Not reported", readinessPayload.http_status === 200],
    ["Market date", readinessPayload.market_date || "Not reported", true],
    ["Input hash", shortHash(readinessPayload.input_hash_sha256), true],
    ["Trading", readinessPayload.live_trading_enabled === false ? "Disabled" : "Not reported", readinessPayload.live_trading_enabled === false],
  ]);
  document.getElementById("manifest-details").innerHTML = detailRows([
    ["Snapshot", manifest.status || "Not reported", manifest.status === "complete" || manifest.status === "no_trade"],
    ["Payload size", manifest.byte_count ? `${Number(manifest.byte_count).toLocaleString()} raw / ${Number(manifest.compressed_byte_count || 0).toLocaleString()} gzip bytes` : "Not reported", Number(manifest.compressed_byte_count || 0) <= 250 * 1024],
    ["Rows", manifest.row_count ?? "Not reported", true],
    ["Payload hash", shortHash(manifest.payload_sha256), true],
    ["Generated", formatTimestamp(manifest.generated_at), true],
  ]);
  const safety = data.safety_evidence || {};
  document.getElementById("safety-details").innerHTML = detailRows([
    ["Source quality", safetyLabel(safety.source_quality), false],
    ["Halt status", safetyLabel(safety.halt_status), false],
    ["Corporate actions", safetyLabel(safety.corporate_action_status), false],
    ["Liquidity evidence", safetyLabel(safety.liquidity_evidence), false],
  ]);
}

function detailRows(rows) { return rows.map(([label, value, good]) => `<dt>${escapeHtml(label)}</dt><dd class="${good ? "good" : "bad"}">${escapeHtml(String(value))}</dd>`).join(""); }
function setStatus(id, text, status) { const node = document.getElementById(id); if (!node) return; node.textContent = text; node.classList.toggle("good", ["COMPLETE", "ready", "NO_TRADE"].includes(String(status))); node.classList.toggle("bad", ["DEGRADED", "PARTIAL", "FAILED"].includes(String(status))); }
function statusChip(status) { const label = labelForStatus(status); const cls = ["COMPLETE", "NO_TRADE", "realized"].includes(String(status)) ? "good" : ["DEGRADED", "PARTIAL", "missing_outcome", "quarantined"].includes(String(status)) ? "bad" : ""; return `<span class="status-chip ${cls}">${escapeHtml(label)}</span>`; }
function labelForStatus(status) { return ({ COMPLETE: "Complete", PARTIAL: "Partial", DEGRADED: "Needs attention", NO_TRADE: "No trade", realized: "Realized", missing_outcome: "Outcome needed", quarantined: "Quarantined", unrealized: "Open", no_trade: "No trade" }[status] || "Not reported"); }
function formatPercent(value) { return value == null || value === "" ? '<span class="value-muted">Not reported</span>' : `<span>${formatPercentText(value)}</span>`; }
function formatPercentText(value) { return value == null || value === "" ? "Not reported" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`; }
function formatMoney(value) { return value == null || value === "" ? '<span class="value-muted">Not reported</span>' : `<span>${Number(value) >= 0 ? "+" : "-"}$${Math.abs(Number(value) / 100).toFixed(2)}</span>`; }
function returnContext(item) {
  const coverage = item.coverage?.coverage_pct == null ? "coverage not reported" : `coverage ${Number(item.coverage.coverage_pct).toFixed(1)}%`;
  const denominator = item.opening_equity_cents == null ? "opening-equity denominator not reported" : `opening-equity denominator ${formatCents(item.opening_equity_cents)}`;
  const sample = Number(item.realized_trade_count || 0) + Number(item.unrealized_trade_count || 0) + Number(item.missing_outcome_count || 0);
  return `Official paper · daily period ending ${item.market_date || "not reported"} · ${returnBasisLabel(item.return_basis)} · ${costStatusLabel(item.cost_status)} · ${denominator} · ${sample} observed/outcome row${sample === 1 ? "" : "s"} · ${coverage} · as of ${formatTimestamp(item.generated_at || item.calculated_at)}`;
}
function formatCents(value) { return `$${(Number(value) / 100).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`; }
function returnBasisLabel(value) { return ({ net_after_costs: "net after fees/slippage", gross_observed_or_missing: "gross observed; complete net result pending", gross_observed: "gross observed" }[value] || "return basis not reported"); }
function costStatusLabel(value) { return ({ complete: "fees/slippage complete", missing_cost_component: "fees/slippage incomplete", unknown: "cost treatment unknown" }[value] || "cost treatment not reported"); }
function safetyLabel(value) { return value?.state === "verified" ? "Verified" : value?.state === "blocked" ? "Blocked" : "Unknown — not reported"; }
function numberOrZero(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function shortDate(value) { return value ? value.slice(5) : "—"; }
function shortHash(value) { return value ? `${String(value).slice(0, 10)}…` : "Not reported"; }
function formatTimestamp(value) { if (!value) return "Not reported"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[char])); }

init();
