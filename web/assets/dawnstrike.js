const COHORTS = {
  official_forward_paper: "Official paper",
  alphaops_signal_research: "Research observations",
  alphaops_research: "Research observations",
  historical_backtest: "Historical backtest",
  shadow_challenger: "Shadow challenger",
};

const V6_GATE_LABELS = {
  all_sourced_and_point_in_time: "All inputs sourced and point-in-time",
  bootstrap_95_lower_bound_above_zero: "95% bootstrap lower bound above zero",
  challenger_beats_frozen_v5_objective: "Beats the frozen V5 objective",
  eligible_outcome_coverage_at_least_98_pct: "Eligible outcome coverage at least 98%",
  gain_loss_concentration_no_more_than_25_pct: "Gain/loss concentration no more than 25%",
  included_benchmark_coverage_100_pct: "Included benchmark coverage is 100%",
  manual_operator_approval_recorded: "Manual operator approval recorded",
  maximum_drawdown_no_worse_than_minus_8_pct: "Maximum drawdown no worse than -8%",
  minimum_closed_paper_trades: "At least 100 closed paper trades",
  minimum_forward_sessions: "At least 60 forward sessions",
  no_lookahead_and_reconciliation_pass: "No-lookahead and reconciliation pass",
  positive_excess_vs_primary_and_cash: "Positive excess versus benchmark and cash",
  positive_mean_net_excess_return: "Positive mean net excess return",
  positive_purged_walk_forward: "Positive purged walk-forward result",
  positive_under_1_5x_slippage: "Positive under 1.5× slippage",
  positive_untouched_holdout: "Positive untouched holdout",
  primary_benchmark_coverage_complete: "SPY benchmark coverage complete",
  profit_factor_at_least_1_20: "Profit factor at least 1.20",
  secondary_benchmark_coverage_complete: "IWM benchmark coverage complete",
};

const PAGE_SIZE = 10;
const state = {
  data: null,
  readiness: null,
  manifest: null,
  calendar: null,
  calendarManifest: null,
  publicationSet: null,
  scenarios: null,
  v6: null,
  stage: null,
  calendarMonth: null,
  calendarSelectedDate: null,
  calendarFilters: {
    cohort: "",
    strategy_id: "",
    strategy_version: "",
    execution_policy_version: "",
    account_id: "",
  },
  performancePage: 0,
  researchPage: 0,
};

document.querySelectorAll(".table-wrap").forEach((region) => {
  const caption = region.querySelector("caption")?.textContent?.trim();
  region.setAttribute("role", "region");
  region.setAttribute("aria-label", caption || "Scrollable data table");
  region.setAttribute("tabindex", "0");
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});

document.querySelectorAll("[data-table][data-direction]").forEach((button) => {
  button.addEventListener("click", () => changePage(button.dataset.table, Number(button.dataset.direction)));
});

document.getElementById("calendar-previous-month")?.addEventListener("click", () => changeCalendarMonth(-1));
document.getElementById("calendar-next-month")?.addEventListener("click", () => changeCalendarMonth(1));
[
  ["calendar-cohort-filter", "cohort"],
  ["calendar-strategy-filter", "strategy_id"],
  ["calendar-version-filter", "strategy_version"],
  ["calendar-policy-filter", "execution_policy_version"],
  ["calendar-account-filter", "account_id"],
].forEach(([id, key]) => {
  document.getElementById(id)?.addEventListener("change", (event) => {
    state.calendarFilters[key] = event.target.value;
    state.calendarSelectedDate = null;
    renderCalendar();
  });
});

function showView(name) {
  document.querySelectorAll(".page-view").forEach((view) => {
    view.classList.toggle("is-visible", view.id === `view-${name}`);
  });
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("is-active", link.dataset.view === name);
    link.setAttribute("aria-pressed", String(link.dataset.view === name));
  });
  if (name && window.location.hash !== `#${name}`) {
    window.history.replaceState(null, "", `#${name}`);
  }
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
    const [snapshot, readiness, manifest, stage, calendar, calendarManifest, publicationSet, v6, scenarios] = await Promise.all([
      loadJson("/data/performance.json"),
      loadJson("/readiness.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/performance.json.manifest.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/stage-manifest.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/calendar.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/calendar.json.manifest.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/publication-set.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/v6-learning.json").catch(() => ({ payload: {}, status: 0 })),
      loadJson("/data/scenarios.json").catch(() => ({ payload: {}, status: 0 })),
    ]);
    state.data = snapshot.payload;
    state.readiness = readiness;
    state.manifest = manifest.payload;
    state.calendar = calendar.payload;
    state.calendarManifest = calendarManifest.payload;
    state.publicationSet = publicationSet.payload;
    state.v6 = v6.payload;
    state.scenarios = scenarios.payload;
    state.stage = stage.payload;
    state.calendarMonth = String(calendar.payload?.as_of_market_date || snapshot.payload?.as_of_market_date || "").slice(0, 7) || null;
    initializeCalendarFilters();
    render();
    const requestedView = window.location.hash.slice(1);
    if (["overview", "calendar", "performance", "research", "scenarios", "system"].includes(requestedView)) showView(requestedView);
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
  const official = daily
    .filter((item) => item.cohort === "official_forward_paper")
    .sort((a, b) => String(b.market_date).localeCompare(String(a.market_date)) || Number(Boolean(b.account_id)) - Number(Boolean(a.account_id)));
  const latest = official[0];
  const missing = numberOrNull(latest?.missing_outcome_count);
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
  document.getElementById("kpi-coverage-note").textContent = missing == null ? "Unresolved denominator not reported" : `${missing} unresolved outcome${missing === 1 ? "" : "s"} excluded`;
  const readinessStatus = state.readiness?.payload?.status;
  document.getElementById("kpi-system").textContent = readinessStatus === "ready" ? "Ready" : readinessStatus === "not_ready" ? "Not ready" : "Not reported";
  document.getElementById("kpi-system-note").textContent = state.readiness?.payload?.http_status ? `Readiness HTTP ${state.readiness.payload.http_status}` : "Readiness is separate from liveness";
  document.getElementById("kpi-context").textContent = latest ? returnContext(latest) : "Official paper context pending: cohort, period, denominator, cost treatment, coverage, and as-of time will appear with the latest record.";
  renderOverview(official, latest);
  renderPerformance(daily);
  renderResearch(rows);
  renderToday(rows, latest);
  renderLedger(rows, latest);
  renderCurve(official);
  renderResearchCohorts(daily);
  renderV6Research();
  renderScenarios();
  renderCalendar();
  renderSystem(state.readiness, state.manifest, state.stage, data);
}

function renderScenarios() {
  const payload = state.scenarios || {};
  const performance = Array.isArray(payload.performance) ? payload.performance : [];
  const replay = payload.historical_replay || {};
  const replayPerformance = Array.isArray(replay.performance) ? replay.performance : [];
  const records = Array.isArray(payload.records) ? payload.records : [];
  const latest = performance.slice().sort((a, b) => String(b.market_date).localeCompare(String(a.market_date)))[0];
  const calibration = payload.calibration_status || "UNCALIBRATED";
  setStatus("scenario-calibration", calibration === "UNCALIBRATED" ? "Uncalibrated" : calibration, calibration === "UNCALIBRATED" ? "PARTIAL" : "COMPLETE");
  document.getElementById("scenario-updated").textContent = payload.generated_at ? `Published ${formatTimestamp(payload.generated_at)}` : "Not published";
  document.getElementById("scenario-net-return").innerHTML = formatPercent(latest?.modeled_after_cost_return_pct);
  document.getElementById("scenario-gross-return").innerHTML = formatPercent(latest?.gross_return_pct);
  document.getElementById("scenario-position-count").textContent = latest ? `${latest.closed_eligible_count ?? 0} / ${latest.open_count ?? 0}` : "Not reported";
  document.getElementById("scenario-hit-rate").innerHTML = formatPercent(latest?.hit_rate_pct);
  document.getElementById("scenario-return-note").textContent = latest?.return_status === "complete" ? `After recorded paper-fill costs · ${latest.market_date}` : "No completed paper positions";
  document.getElementById("scenario-coverage-note").textContent = latest ? `${latest.triggered_count ?? 0} triggered · ${latest.missing_count ?? 0} missing` : "Missing stays excluded";
  const replayTotals = replayPerformance.reduce((total, row) => ({
    closed: total.closed + Number(row.closed_eligible_count || 0),
    quarantined: total.quarantined + Number(row.quarantined_count || 0),
    returns: row.modeled_after_cost_return_pct == null ? total.returns : [...total.returns, Number(row.modeled_after_cost_return_pct)],
  }), { closed: 0, quarantined: 0, returns: [] });
  const replayMean = replayTotals.returns.length ? replayTotals.returns.reduce((sum, value) => sum + value, 0) / replayTotals.returns.length : null;
  document.getElementById("scenario-replay-disclosure").textContent = replay.disclosure || "Historical replay not run.";
  setStatus("scenario-replay-status", replayPerformance.length ? "Separate audit cohort" : "Not run", replayPerformance.length ? "PARTIAL" : "");
  document.getElementById("scenario-replay-metrics").innerHTML = detailRows([
    ["Days", replayPerformance.length || "Not reported", replayPerformance.length > 0],
    ["Mean after-cost result", formatPercentText(replayMean), replayMean != null],
    ["Closed eligible", replayTotals.closed || "Not reported", replayTotals.closed > 0],
    ["Quarantined", replayTotals.quarantined, replayTotals.quarantined === 0],
  ]);
  const disclosures = Array.isArray(payload.disclosures) ? payload.disclosures : [];
  document.getElementById("scenario-disclosures").innerHTML = disclosures.length
    ? `<strong>Research-only, no broker execution.</strong><span>${disclosures.map(escapeHtml).join("<br>")}</span>`
    : "<strong>Scenario disclosure unavailable.</strong> No scenario claim is being inferred.";
  const body = document.getElementById("scenario-table");
  if (!records.length) {
    body.innerHTML = '<tr><td colspan="7">No scenario records have been published yet. This is not a zero-return claim.</td></tr>';
    return;
  }
  body.innerHTML = records.slice(0, 100).map((record) => {
    const levels = record.entry_trigger == null
      ? "Not eligible"
      : `Entry ${formatPrice(record.entry_trigger)} · Stop ${formatPrice(record.invalidation_level)} · Target ${formatPrice(record.target_1)}`;
    const source = record.source_url
      ? `<a class="source-link" href="${escapeHtml(record.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(record.source_tier || "Source")}</a>`
      : escapeHtml(record.source_tier || "Not reported");
    const evidence = [record.headline, ...(Array.isArray(record.reason_codes) ? record.reason_codes : [])]
      .filter(Boolean)
      .join(" · ");
    return `<tr>
      <td>${escapeHtml(formatTimestamp(record.decision_at))}</td>
      <td><strong>${escapeHtml(record.ticker || "Not reported")}</strong></td>
      <td>${source}</td>
      <td>${escapeHtml(humanizeIdentifier(record.event_type || "not_reported"))}<br><span class="value-muted">${escapeHtml(record.direction || "unknown")}</span></td>
      <td><span class="status-chip ${record.action === "ENTER_LONG" ? "good" : ["ABSTAIN", "AVOID"].includes(record.action) ? "bad" : ""}">${escapeHtml(humanizeIdentifier(record.action || "not_reported"))}</span></td>
      <td>${escapeHtml(levels)}</td>
      <td>${escapeHtml(evidence || "Not reported")}</td>
    </tr>`;
  }).join("");
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
  const recent = official.filter((item) => numberOrNull(item.gross_return_pct) != null).slice(0, 5).reverse();
  const scale = Math.max(...recent.map((item) => Math.abs(numberOrNull(item.gross_return_pct))), 1);
  bars.innerHTML = recent.length ? recent.map((item) => {
    const value = numberOrNull(item.gross_return_pct);
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
    <td class="${valueClass(item.gross_return_pct)}">${formatPercent(item.gross_return_pct)}</td>
    <td class="${valueClass(item.return_pct)}">${formatPercent(item.return_pct)}</td>
    <td>${formatPercent(item.excess_return_pct)}</td>
    <td>${sumReportedCounts(item.unrealized_trade_count, item.missing_outcome_count, item.quarantined_count)}</td>
  </tr>`).join("");
  updatePager("performance", start, visible.length, daily.length);
}

function renderToday(rows, latest) {
  document.getElementById("today-date").textContent = latest?.market_date || "Not reported";
  const dated = rows.filter((row) => row.cohort === "official_forward_paper" && row.market_date === latest?.market_date);
  const counts = {
    selected: dated.length,
    entered: dated.filter((row) => ["realized", "unrealized"].includes(row.record_status)).length,
    closed: dated.filter((row) => row.record_status === "realized").length,
    open: dated.filter((row) => row.record_status === "unrealized").length,
    unresolved: dated.filter((row) => ["missing_outcome", "quarantined"].includes(row.record_status)).length,
  };
  document.getElementById("today-activity").innerHTML = Object.entries(counts).map(([label, value]) => `<div class="activity-card"><span>${escapeHtml(label)}</span><strong>${value}</strong><small>${value ? "Source rows" : "None reported"}</small></div>`).join("");
}

function renderLedger(rows, latest) {
  const body = document.getElementById("ledger-table");
  const dated = rows.filter((row) => row.cohort === "official_forward_paper" && row.market_date === latest?.market_date).slice(0, 12);
  body.innerHTML = dated.length ? dated.map((row) => `<tr><td>${escapeHtml(row.market_date || "Not reported")}</td><td><strong>${escapeHtml(row.ticker || "Not reported")}</strong></td><td>${statusChip(row.record_status)}</td><td>${formatMoney(row.net_pnl_cents)}</td><td>${row.source_refs?.length ? `${row.source_refs.length} ref${row.source_refs.length === 1 ? "" : "s"}` : "Not reported"}</td></tr>`).join("") : '<tr><td colspan="5">No official ledger rows for the latest dated record.</td></tr>';
}

function renderCurve(official) {
  const node = document.getElementById("return-curve");
  const points = official.slice().sort((a, b) => String(a.market_date).localeCompare(String(b.market_date))).slice(-30);
  if (!points.length) {
    node.innerHTML = '<span class="muted">No sourced official curve is available.</span>';
    return;
  }
  const values = points.map((item) => Number(item.cumulative_return_pct)).filter(Number.isFinite);
  if (!values.length) {
    node.innerHTML = '<span class="muted">Cumulative return is not reported for the available days.</span>';
    return;
  }
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const range = Math.max(max - min, 0.01);
  const polyline = values.map((value, index) => `${(index / Math.max(values.length - 1, 1)) * 100},${92 - ((value - min) / range) * 82}`).join(" ");
  node.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="92" x2="100" y2="92" class="curve-axis"></line><polyline points="${polyline}" class="curve-line"></polyline></svg><div class="curve-labels"><span>${escapeHtml(points[0].market_date)}</span><strong>${formatPercent(points.at(-1).cumulative_return_pct)}</strong><span>${escapeHtml(points.at(-1).market_date)}</span></div>`;
}

function renderResearchCohorts(daily) {
  const groups = new Map();
  daily.filter((row) => row.cohort !== "official_forward_paper").forEach((row) => {
    const current = groups.get(row.cohort) || { days: 0, complete: 0 };
    current.days += 1;
    if (["COMPLETE", "NO_TRADE"].includes(row.status)) current.complete += 1;
    groups.set(row.cohort, current);
  });
  document.getElementById("research-cohorts").innerHTML = groups.size ? [...groups.entries()].map(([cohort, value]) => `<div class="cohort-row"><strong>${escapeHtml(COHORTS[cohort] || cohort)}</strong><span>${value.days} day${value.days === 1 ? "" : "s"} · ${value.complete} complete</span></div>`).join("") : '<span class="muted">No backtest or challenger rows are published.</span>';
}

function renderV6Research() {
  const v6 = state.v6 || {};
  const promotion = v6.promotion_readiness || {};
  const stateLabel = promotion.performance_status || "WAITING_FOR_FORWARD_EVIDENCE";
  const forwardState = document.getElementById("forward-state");
  if (forwardState) forwardState.textContent = stateLabel;
  const cohorts = document.getElementById("research-cohorts");
  if (!cohorts) return;
  const count = Number(v6.learning_eligible_outcome_count || 0);
  const sessions = Number(promotion.forward_session_count || 0);
  const trades = Number(promotion.closed_paper_trade_count || 0);
  const v6Row = `<div class="cohort-row"><strong>V6 shadow challenger</strong><span>${sessions}/60 sessions · ${trades}/100 after-cost labels · ${count} eligible</span></div>`;
  cohorts.innerHTML += v6Row;

  const chip = document.getElementById("v6-promotion-chip");
  if (chip) {
    chip.textContent = formatGateLabel(promotion.status || "NOT_ELIGIBLE_FOR_PROMOTION");
    chip.className = `status-chip ${promotion.status === "ELIGIBLE_FOR_MANUAL_REVIEW" ? "warn" : promotion.status === "MANUALLY_APPROVED_FOR_CONTROLLED_PROMOTION" ? "good" : "bad"}`;
  }
  const criteria = promotion.criteria || {};
  const gateNode = document.getElementById("v6-promotion-gates");
  if (gateNode) {
    const gates = Object.entries(criteria);
    gateNode.innerHTML = gates.length ? gates.map(([key, passed]) => `<div class="gate-row"><span class="gate-dot ${passed ? "good" : "bad"}" aria-hidden="true"></span><span>${escapeHtml(V6_GATE_LABELS[key] || formatGateLabel(key))}</span><strong class="${passed ? "good" : "bad"}">${passed ? "PASS" : "BLOCKED"}</strong></div>`).join("") : '<span class="muted">No promotion gate evidence is published.</span>';
  }

  const model = v6.latest_model_run || {};
  const evaluation = v6.latest_evaluation || {};
  const calibration = evaluation.calibration || {};
  const intervals = evaluation.interval_coverage || {};
  const drift = v6.latest_drift || {};
  const operational = v6.operational_freshness || {};
  const dailyMonitor = operational.latest_daily_monitor || {};
  const weeklyTraining = operational.latest_weekly_training || {};
  const evidenceGate = v6.prediction_evidence_gate || {};
  const accountComparison = v6.account_comparison || {};
  const modelNode = document.getElementById("v6-model-evidence");
  if (modelNode) {
    const details = [
      ["Model", model.model_version || "Not trained"],
      ["Training cutoff", model.training_cutoff || "Not available"],
      ["Training status", model.status || "Not trained"],
      ["Purged folds", evaluation.fold_count == null ? "Not evaluated" : String(evaluation.fold_count)],
      ["No-lookahead", evaluation.no_lookahead === true ? "Passed" : "Not proven"],
      ["Calibration", calibration.status || "Not evaluated"],
      ["Interval coverage", intervals.coverage_pct == null ? (intervals.status || "Not evaluated") : `${Number(intervals.coverage_pct).toFixed(1)}%`],
      ["Drift", drift.status || "Not evaluated"],
      ["Daily monitor", dailyMonitor.created_at ? `${formatTimestamp(dailyMonitor.created_at)} · ${dailyMonitor.status || "Unknown"}` : "Not recorded"],
      ["Weekly training", weeklyTraining.created_at ? `${formatTimestamp(weeklyTraining.created_at)} · ${weeklyTraining.status || "Unknown"}` : "Not recorded"],
      ["Prediction display", evidenceGate.passed ? "Evidence gate passed" : "Hidden—evidence incomplete"],
      ["Artifact", shortHash(model.model_artifact_hash_sha256) || "Not available"],
    ];
    modelNode.innerHTML = details.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  }

  const accountComparisonNode = document.getElementById("v6-account-comparison");
  if (accountComparisonNode) {
    const alignment = accountComparison.alignment || {};
    const metrics = accountComparison.series_metrics || {};
    const blockers = Array.isArray(accountComparison.promotion_blockers) ? accountComparison.promotion_blockers : [];
    const details = [
      ["Comparison state", accountComparison.status || "Not recorded"],
      ["Aligned sessions", alignment.aligned_session_count == null ? "Not reported" : `${alignment.aligned_session_count}/${alignment.eligible_session_count ?? "?"}`],
      ["Coverage", alignment.coverage_pct == null ? "Not reported" : `${Number(alignment.coverage_pct).toFixed(1)}%`],
      ["V5 compounded", formatPercent(metrics.v5?.compounded_net_return_pct)],
      ["V6 compounded", formatPercent(metrics.v6?.compounded_net_return_pct)],
      ["Cash / SPY / IWM", [metrics.cash?.compounded_net_return_pct, metrics.SPY?.compounded_net_return_pct, metrics.IWM?.compounded_net_return_pct].map(formatPercent).join(" / ")],
      ["Why withheld", blockers.length ? blockers.map(formatGateLabel).join(" · ") : "No blockers recorded"],
    ];
    accountComparisonNode.innerHTML = details.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  }

  const attribution = v6.failure_attribution || {};
  const categories = attribution.categories || {};
  const failureModes = attribution.failure_modes || {};
  const executionCost = failureModes.execution_cost || {};
  const dataQuality = failureModes.data_quality || {};
  const failureNode = document.getElementById("v6-failure-attribution");
  if (failureNode) {
    const details = [
      ["Attribution status", attribution.status || "Waiting for sourced outcomes"],
      ["Setup × regime", summarizeFailureCohorts(categories.by_setup_regime)],
      ["Source quality", summarizeFailureCohorts(categories.by_source_quality)],
      ["Liquidity", summarizeFailureCohorts(categories.by_liquidity)],
      ["Catalyst", summarizeFailureCohorts(categories.by_catalyst)],
      ["Volatility", summarizeFailureCohorts(categories.by_volatility)],
      ["Observed slippage", executionCost.observed_slippage_status || "Not reported"],
      ["Outcome completeness", `${dataQuality.sourced_complete_count ?? 0} sourced · ${dataQuality.terminal_missing_count ?? 0} terminal missing`],
    ];
    failureNode.innerHTML = details.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  }

  const replayNode = document.getElementById("v6-replay-list");
  const decisions = Array.isArray(v6.decision_replay) ? v6.decision_replay : [];
  if (replayNode) {
    replayNode.innerHTML = decisions.length ? decisions.map((decision) => {
      const reasons = Array.isArray(decision.reasons) && decision.reasons.length ? decision.reasons.join(" · ") : "No veto or rejection reason recorded";
      const prediction = decision.prediction_visible
        ? `Activation ${formatProbability(decision.activation_probability)} · expected excess ${formatPercentText(decision.conditional_net_excess_return_pct)} · utility LCB ${formatPercentText(decision.utility_lcb_pct)}`
        : "Prediction hidden until the evidence gate passes";
      return `<details class="decision-replay-card"><summary><span><strong>${escapeHtml(decision.ticker || "NO_TRADE")}</strong><small>${escapeHtml(decision.market_date || "Date unavailable")} · ${escapeHtml(formatGateLabel(decision.decision_state || decision.action || "UNKNOWN"))}</small></span><span class="status-chip">${escapeHtml(decision.setup_key || "no setup")}</span></summary><div class="decision-replay-body"><dl><dt>Why</dt><dd>${escapeHtml(reasons)}</dd><dt>Regime</dt><dd>${escapeHtml(decision.regime_key || "Unknown")}</dd><dt>Model</dt><dd>${escapeHtml(decision.model_version || "Not available")}</dd><dt>Evidence</dt><dd>${escapeHtml(prediction)}</dd><dt>Feature snapshot</dt><dd>${escapeHtml(shortHash(decision.feature_hash_sha256) || "Missing")}</dd><dt>Source lineage</dt><dd>${escapeHtml(shortHash(decision.source_lineage_hash_sha256) || "Missing")}</dd><dt>Decision ID</dt><dd>${escapeHtml(decision.decision_id || "Missing")}</dd></dl></div></details>`;
    }).join("") : '<span class="muted">No V6 decisions are available for replay yet.</span>';
  }
}

function summarizeFailureCohorts(rows) {
  if (!Array.isArray(rows) || !rows.length) return "No sourced outcomes yet";
  return rows.slice(0, 3).map((row) => {
    const eligible = Number(row.eligible_return_count || 0);
    const mean = formatPercentText(row.mean_net_excess_return_pct);
    return `${row.group || "unknown"}: ${eligible} eligible · mean ${mean}`;
  }).join(" | ");
}

function formatGateLabel(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatProbability(value) {
  return Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : "Not reported";
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

function initializeCalendarFilters() {
  const records = calendarRecords();
  if (!records.length) return;
  const official = records.filter((record) => record.cohort === "official_forward_paper");
  const cohortPool = official.length ? official : records;
  const v5 = cohortPool.filter((record) => record.strategy_id === "alphaops_v5");
  const preferred = v5.at(-1) || cohortPool.at(-1);
  state.calendarFilters = {
    cohort: preferred?.cohort || "",
    strategy_id: preferred?.strategy_id || "",
    strategy_version: preferred?.strategy_version || "",
    execution_policy_version: preferred?.execution_policy_version || "",
    account_id: preferred?.account_id || "",
  };
}

function calendarRecords() {
  return Array.isArray(state.calendar?.days)
    ? state.calendar.days.flatMap((day) => Array.isArray(day.records) ? day.records : [])
    : [];
}

function populateCalendarFilters() {
  const records = calendarRecords();
  const definitions = [
    ["calendar-cohort-filter", "cohort", records, "All cohorts"],
    ["calendar-strategy-filter", "strategy_id", records.filter((row) => !state.calendarFilters.cohort || row.cohort === state.calendarFilters.cohort), "All strategies"],
    ["calendar-version-filter", "strategy_version", records.filter((row) => (!state.calendarFilters.cohort || row.cohort === state.calendarFilters.cohort) && (!state.calendarFilters.strategy_id || row.strategy_id === state.calendarFilters.strategy_id)), "All versions"],
    ["calendar-policy-filter", "execution_policy_version", records.filter((row) => (!state.calendarFilters.cohort || row.cohort === state.calendarFilters.cohort) && (!state.calendarFilters.strategy_id || row.strategy_id === state.calendarFilters.strategy_id) && (!state.calendarFilters.strategy_version || row.strategy_version === state.calendarFilters.strategy_version)), "All policies"],
    ["calendar-account-filter", "account_id", records.filter((row) => (!state.calendarFilters.cohort || row.cohort === state.calendarFilters.cohort) && (!state.calendarFilters.strategy_id || row.strategy_id === state.calendarFilters.strategy_id)), "All accounts"],
  ];
  definitions.forEach(([id, key, pool, allLabel]) => {
    const node = document.getElementById(id);
    if (!node) return;
    const values = [...new Set(pool.map((row) => String(row[key] || "")).filter(Boolean))].sort();
    const selected = values.includes(state.calendarFilters[key]) ? state.calendarFilters[key] : "";
    state.calendarFilters[key] = selected;
    node.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>${values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(calendarFilterLabel(key, value))}</option>`).join("")}`;
    node.value = selected;
  });
}

function calendarFilterLabel(key, value) {
  if (key === "cohort") return COHORTS[value] || humanizeIdentifier(value);
  if (key === "strategy_id") return value === "alphaops_v5" ? "AlphaOps V5" : humanizeIdentifier(value);
  if (key === "account_id") return value === "alphaops_v5_simulated" ? "V5 · $100k simulated" : humanizeIdentifier(value);
  return value;
}

function renderCalendar() {
  const calendar = state.calendar || {};
  const allDays = Array.isArray(calendar.days) ? calendar.days : [];
  populateCalendarFilters();
  const label = document.getElementById("calendar-month-label");
  const grid = document.getElementById("calendar-grid");
  if (!allDays.length || !state.calendarMonth) {
    label.textContent = "No calendar published";
    grid.innerHTML = '<div class="calendar-empty">No canonical calendar is available. No return is being inferred.</div>';
    renderCalendarSummary([]);
    renderCalendarDetail(null, []);
    setStatus("calendar-integrity", "Calendar unavailable", "DEGRADED");
    return;
  }
  const integrity = calendarIntegrity();
  setStatus("calendar-integrity", integrity.label, integrity.ok ? "COMPLETE" : "DEGRADED");
  label.textContent = monthLabel(state.calendarMonth);
  const availableMonths = [...new Set(allDays.map((day) => String(day.month || "")))].filter(Boolean).sort();
  document.getElementById("calendar-previous-month").disabled = state.calendarMonth <= availableMonths[0];
  document.getElementById("calendar-next-month").disabled = state.calendarMonth >= availableMonths.at(-1);
  const lookup = new Map(allDays.map((day) => [day.date, day]));
  const monthDateKeys = datesForMonth(state.calendarMonth);
  const leading = monthDateKeys.length ? mondayOffset(monthDateKeys[0]) : 0;
  const cells = Array.from({ length: leading }, () => '<span class="calendar-spacer" aria-hidden="true"></span>');
  monthDateKeys.forEach((dateKey) => {
    const day = lookup.get(dateKey);
    const matches = filteredCalendarRecords(day);
    const record = matches.length === 1 ? matches[0] : null;
    const status = calendarCellStatus(day, matches);
    const value = record?.eligible_for_return ? numberOrNull(record.net_return_pct) : null;
    const selected = dateKey === state.calendarSelectedDate;
    const intensity = value == null ? 0 : Math.min(Math.abs(value) / 3, 1);
    const returnText = value == null ? "—" : formatPercentText(value);
    const statusText = matches.length > 1 ? "Refine filters" : calendarStatusLabel(status);
    const aria = `${dateKey}. ${statusText}. ${value == null ? "Return not reported" : `Net return ${returnText}`}`;
    cells.push(`<button type="button" class="calendar-cell status-${String(status).toLowerCase().replaceAll("_", "-")} ${value > 0 ? "positive" : value < 0 ? "negative" : ""} ${selected ? "selected" : ""}" data-calendar-date="${dateKey}" style="--heat:${intensity.toFixed(3)}" aria-label="${escapeHtml(aria)}" aria-pressed="${selected}">
      <span class="calendar-date-number">${Number(dateKey.slice(-2))}</span>
      <strong>${escapeHtml(returnText)}</strong>
      <small>${escapeHtml(statusText)}</small>
      <i aria-hidden="true"></i>
    </button>`);
  });
  grid.innerHTML = cells.join("");
  grid.querySelectorAll("[data-calendar-date]").forEach((button) => {
    button.addEventListener("click", () => {
      state.calendarSelectedDate = button.dataset.calendarDate;
      renderCalendar();
    });
  });
  const currentDays = allDays.filter((day) => day.month === state.calendarMonth);
  const currentRecords = currentDays.flatMap((day) => filteredCalendarRecords(day));
  renderCalendarSummary(currentRecords);
  if (!state.calendarSelectedDate || !state.calendarSelectedDate.startsWith(state.calendarMonth)) {
    const preferred = currentDays.slice().reverse().find((day) => filteredCalendarRecords(day).length === 1)
      || currentDays.at(-1);
    state.calendarSelectedDate = preferred?.date || null;
    if (state.calendarSelectedDate) {
      const selectedButton = grid.querySelector(`[data-calendar-date="${state.calendarSelectedDate}"]`);
      selectedButton?.classList.add("selected");
      selectedButton?.setAttribute("aria-pressed", "true");
    }
  }
  const selectedDay = lookup.get(state.calendarSelectedDate);
  renderCalendarDetail(selectedDay, filteredCalendarRecords(selectedDay));
}

function filteredCalendarRecords(day) {
  if (!day || !Array.isArray(day.records)) return [];
  return day.records.filter((record) => Object.entries(state.calendarFilters).every(([key, value]) => !value || String(record[key] || "") === value));
}

function calendarCellStatus(day, records) {
  if (records.length > 1) return "PARTIAL";
  if (records.length === 1) return records[0]?.status || "MISSING";
  return day?.market_session_status === "closed" ? "UNAVAILABLE" : "MISSING";
}

function renderCalendarSummary(records) {
  const months = Array.isArray(state.calendar?.months) ? state.calendar.months : [];
  const summaries = months.filter((row) => row.month === state.calendarMonth && Object.entries(state.calendarFilters).every(([key, value]) => !value || String(row[key] || "") === value));
  const summary = summaries.length === 1 ? summaries[0] : null;
  document.getElementById("calendar-summary-return").innerHTML = summary ? formatPercent(summary.net_return_pct) : '<span class="value-muted">Not reported</span>';
  document.getElementById("calendar-summary-excess").innerHTML = summary ? formatPercent(summary.excess_return_pct) : '<span class="value-muted">Not reported</span>';
  document.getElementById("calendar-summary-benchmark").textContent = summary?.benchmark_return_pct == null ? "Benchmark not reported" : `Benchmark ${formatPercentText(summary.benchmark_return_pct)}`;
  document.getElementById("calendar-summary-coverage").textContent = summary?.coverage_pct == null ? "Not reported" : `${Number(summary.coverage_pct).toFixed(1)}%`;
  document.getElementById("calendar-summary-denominator").textContent = summary ? `${summary.eligible_day_count} eligible / ${summary.expected_market_day_count} expected market days` : summaries.length > 1 ? "Refine filters to one exact contract" : "No exact monthly denominator";
  document.getElementById("calendar-summary-days").textContent = summary ? `${summary.eligible_day_count} / ${summary.expected_market_day_count}` : records.length ? `${records.length} observed rows` : "Not reported";
  document.getElementById("calendar-summary-no-trades").textContent = summary ? `${summary.no_trade_day_count} explicit no-trade day${summary.no_trade_day_count === 1 ? "" : "s"}` : "No-trade days not reported";
}

function renderCalendarDetail(day, records) {
  const title = document.getElementById("calendar-detail-title");
  const note = document.getElementById("calendar-detail-note");
  const metrics = document.getElementById("calendar-detail-metrics");
  const reasons = document.getElementById("calendar-detail-reasons");
  const trades = document.getElementById("calendar-detail-trades");
  if (!day) {
    title.textContent = "Choose a date";
    note.textContent = "Select a calendar day to inspect its return basis, account equation, selections, and source lineage.";
    metrics.innerHTML = "";
    reasons.innerHTML = "";
    trades.innerHTML = "";
    setStatus("calendar-detail-status", "Not selected", "");
    return;
  }
  title.textContent = formatCalendarDate(day.date);
  if (records.length > 1) {
    setStatus("calendar-detail-status", "Refine filters", "PARTIAL");
    note.textContent = `${records.length} canonical contracts match. Choose one cohort, strategy, version, policy, and account; Dawnstrike will not blend them.`;
    metrics.innerHTML = "";
    reasons.innerHTML = "";
    trades.innerHTML = "";
    return;
  }
  const record = records[0];
  if (!record) {
    const closed = day.market_session_status === "closed";
    setStatus("calendar-detail-status", closed ? "Market closed" : "Missing", closed ? "" : "PARTIAL");
    note.textContent = closed ? day.market_session_reason || "The market was closed." : "No canonical observation exists for this market day. It is not a zero-return day.";
    metrics.innerHTML = detailRows([
      ["Market session", closed ? "Closed" : day.market_session_status || "Not reported", closed],
      ["Return", "Not reported", false],
      ["Observed zero", "No", true],
    ]);
    reasons.innerHTML = "";
    trades.innerHTML = "";
    return;
  }
  setStatus("calendar-detail-status", calendarStatusLabel(record.status), record.status);
  note.textContent = record.eligible_for_return
    ? `${record.return_basis === "account_equity_identity_after_external_flows" ? "Account return" : "Canonical return"} is eligible under ${record.execution_policy_version}.`
    : "The return is withheld until the required outcome, account, and source evidence is complete.";
  metrics.innerHTML = detailRows([
    ["Net return", formatPercentText(record.net_return_pct), record.net_return_pct != null],
    ["Gross observed", formatPercentText(record.gross_return_pct), record.gross_return_pct != null],
    ["Benchmark", formatPercentText(record.benchmark_return_pct), record.benchmark_return_pct != null],
    ["Excess", formatPercentText(record.excess_return_pct), record.excess_return_pct != null],
    ["Beginning equity", formatMoneyText(record.opening_equity_cents), record.opening_equity_cents != null],
    ["External flow", formatMoneyText(record.external_flow_cents), record.external_flow_cents != null],
    ["Ending equity", formatMoneyText(record.ending_equity_cents), record.ending_equity_cents != null],
    ["Accounting delta", formatMoneyText(record.accounting_delta_cents), record.accounting_delta_cents === 0],
    ["Activity", `${record.entries} entered · ${record.exits} exited · ${record.open_positions} open`, true],
    ["Coverage", coverageText(record.coverage), record.coverage?.missing_count === 0],
  ]);
  reasons.innerHTML = Array.isArray(record.missing_reasons) && record.missing_reasons.length
    ? `<h4>Why it is not complete</h4><ul>${record.missing_reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
    : "";
  const details = Array.isArray(record.details) ? record.details : [];
  trades.innerHTML = details.length ? `<h4>Selections and outcomes</h4>${details.map((detail) => `<article class="calendar-trade-card">
    <div><strong>${escapeHtml(detail.ticker || "Account")}</strong><span>${escapeHtml(detail.telegram_selection_tier || detail.record_status || "Not reported")}</span></div>
    <dl>
      <dt>Outcome</dt><dd>${escapeHtml(labelForStatus(detail.record_status))}</dd>
      <dt>Net result</dt><dd>${formatPercentText(detail.net_return_pct)}</dd>
      <dt>Catalyst</dt><dd>${escapeHtml(detail.catalyst || "Not reported")}</dd>
      <dt>Source lineage</dt><dd>${Array.isArray(detail.source_lineage) && detail.source_lineage.length ? `${detail.source_lineage.length} reference${detail.source_lineage.length === 1 ? "" : "s"}` : "Not reported"}</dd>
    </dl>
    ${detail.block_or_veto_reasons?.length ? `<p>${detail.block_or_veto_reasons.map(escapeHtml).join(" · ")}</p>` : ""}
  </article>`).join("")}` : '<p class="muted">No ticker-level row is attached to this account day.</p>';
}

function changeCalendarMonth(direction) {
  if (!state.calendarMonth || ![-1, 1].includes(direction)) return;
  const available = [...new Set((state.calendar?.days || []).map((day) => day.month))].filter(Boolean).sort();
  const index = available.indexOf(state.calendarMonth);
  const next = available[index + direction];
  if (!next) return;
  state.calendarMonth = next;
  state.calendarSelectedDate = null;
  renderCalendar();
}

function calendarIntegrity() {
  const calendar = state.calendar || {};
  const manifest = state.calendarManifest || {};
  const publication = state.publicationSet || {};
  const ok = Boolean(
    calendar.canonical_input_hash_sha256
    && calendar.canonical_input_hash_sha256 === manifest.canonical_input_hash_sha256
    && calendar.canonical_input_hash_sha256 === publication.canonical_input_hash_sha256
    && manifest.payload_sha256 === publication.calendar_payload_sha256
    && calendar.performance_payload_sha256 === publication.performance_payload_sha256
  );
  return { ok, label: ok ? "Canonical hashes match" : "Integrity not verified" };
}

function datesForMonth(month) {
  const [year, monthNumber] = month.split("-").map(Number);
  if (!year || !monthNumber) return [];
  const count = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  return Array.from({ length: count }, (_, index) => `${year}-${String(monthNumber).padStart(2, "0")}-${String(index + 1).padStart(2, "0")}`);
}

function mondayOffset(dateKey) {
  const day = new Date(`${dateKey}T12:00:00Z`).getUTCDay();
  return (day + 6) % 7;
}

function monthLabel(month) {
  const date = new Date(`${month}-01T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? month : date.toLocaleDateString([], { month: "long", year: "numeric", timeZone: "UTC" });
}

function formatCalendarDate(value) {
  const date = new Date(`${value}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
}

function calendarStatusLabel(status) {
  return ({
    COMPLETE: "Complete",
    NO_TRADE: "No trade",
    PARTIAL: "Partial",
    PENDING: "Pending",
    MISSING: "Missing",
    UNAVAILABLE: "Unavailable",
    UNREALIZED: "Unrealized",
  }[status] || "Not reported");
}

function coverageText(coverage) {
  if (!coverage || coverage.eligible_count == null || coverage.observed_count == null) return "Not reported";
  return `${coverage.observed_count} observed / ${coverage.eligible_count} eligible`;
}

function humanizeIdentifier(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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

function renderSystem(readiness, manifest, stage, data) {
  const readinessPayload = readiness?.payload || {};
  const readinessStatus = readinessPayload.status || "not_reported";
  const dailyRun = readinessPayload.daily_run || {};
  const run = dailyRun.run || readinessPayload.last_attempted_run || {};
  const lastSuccess = dailyRun.last_fully_successful_run || readinessPayload.last_fully_successful_run || {};
  const scheduler = readinessPayload.scheduler || {};
  const coverage = readinessPayload.outcome_coverage || {};
  setStatus("system-pill", readinessStatus === "ready" ? "Ready" : "Needs attention", readinessStatus === "ready" ? "COMPLETE" : "DEGRADED");
  setStatus("readiness-status", readinessStatus === "ready" ? "Ready" : "Not ready", readinessStatus === "ready" ? "COMPLETE" : "DEGRADED");
  document.getElementById("readiness-details").innerHTML = detailRows([
    ["Publication", readinessStatus === "ready" ? "Complete" : "Not ready", readinessStatus === "ready"],
    ["HTTP readiness", readinessPayload.http_status ?? "Not reported", readinessPayload.http_status === 200],
    ["Calendar", readinessPayload.calendar_status || "Not reported", ["complete", "no_trade"].includes(readinessPayload.calendar_status)],
    ["Market date", readinessPayload.market_date || "Not reported", true],
    ["Last attempted run", run.run_id ? `${run.market_date || "date missing"} · ${run.status || "status missing"}` : "Not reported", Boolean(run.run_id)],
    ["Last full success", lastSuccess.run_id ? `${lastSuccess.market_date || "date missing"} · ${formatTimestamp(lastSuccess.completed_at)}` : "None recorded", Boolean(lastSuccess.run_id)],
    ["Current stage", run.current_stage || "Not reported", Boolean(run.current_stage)],
    ["Failed stage", run.failed_stage ? `${run.failed_stage} · ${run.failure_reason || "reason not reported"}` : "None", !run.failed_stage],
    ["Source watermark", readinessPayload.source_data_watermark || run.source_data_watermark || "Not reported", Boolean(readinessPayload.source_data_watermark || run.source_data_watermark)],
    ["Outcome coverage", coverage.coverage_pct == null ? `${coverage.observed_count ?? 0}/${coverage.eligible_count ?? 0}; denominator unavailable` : `${Number(coverage.coverage_pct).toFixed(1)}% · ${coverage.observed_count}/${coverage.eligible_count}`, coverage.missing_count === 0],
    ["Published", formatTimestamp(readinessPayload.publication_timestamp || run.publication_timestamp), Boolean(readinessPayload.publication_timestamp || run.publication_timestamp)],
    ["Source SHA", shortHash(readinessPayload.deployed_source_sha || run.deployed_source_sha || manifest.source_sha), Boolean(readinessPayload.deployed_source_sha || run.deployed_source_sha || manifest.source_sha)],
    ["Build SHA", shortHash(readinessPayload.deployed_build_sha || run.deployed_build_sha || manifest.build_sha || manifest.build_id), Boolean(readinessPayload.deployed_build_sha || run.deployed_build_sha || manifest.build_sha || manifest.build_id)],
    ["Scheduler", scheduler.status || "Not reported", scheduler.status === "LOCAL_VERIFIED"],
    ["Runtime boundary", scheduler.runtime_boundary || "Configured", scheduler.runtime_boundary === "configured"],
    ["State boundary", scheduler.state_boundary || "Configured", scheduler.state_boundary === "configured"],
    ["Next scheduled", formatTimestamp(readinessPayload.next_scheduled_run || scheduler.next_scheduled_run), Boolean(readinessPayload.next_scheduled_run || scheduler.next_scheduled_run)],
    ["Input hash", shortHash(readinessPayload.input_hash_sha256), true],
    ["Bound set", shortHash(readinessPayload.publication_set_sha256), Boolean(readinessPayload.publication_set_sha256)],
    ["Trading", readinessPayload.live_trading_enabled === false ? "Disabled" : "Not reported", readinessPayload.live_trading_enabled === false],
  ]);
  document.getElementById("manifest-details").innerHTML = detailRows([
    ["As of", data.as_of_market_date || manifest.market_date || "Not reported", true],
    ["Snapshot", manifest.status || "Not reported", manifest.status === "complete" || manifest.status === "no_trade"],
    ["Payload size", manifest.byte_count ? `${Number(manifest.byte_count).toLocaleString()} raw / ${Number(manifest.compressed_byte_count || 0).toLocaleString()} gzip bytes` : "Not reported", Number(manifest.compressed_byte_count || 0) <= 250 * 1024],
    ["Rows", manifest.row_count ?? "Not reported", true],
    ["Payload hash", shortHash(manifest.payload_sha256), true],
    ["Calendar hash", shortHash(state.calendarManifest?.payload_sha256), Boolean(state.calendarManifest?.payload_sha256)],
    ["Hash binding", calendarIntegrity().label, calendarIntegrity().ok],
    ["Generated", formatTimestamp(manifest.generated_at), true],
  ]);
  const sharedStages = Object.entries(dailyRun.latest_stage_statuses || {}).map(([name, item]) => ({
    stage: name,
    status: item?.status || "NOT_STARTED",
    next_action: item?.error_detail || item?.error_code || `Attempt ${item?.attempt_no ?? "not reported"}`,
  }));
  const stages = sharedStages.length ? sharedStages : (Array.isArray(stage?.stages) ? stage.stages : []);
  document.getElementById("stage-details").innerHTML = stages.length ? stages.map((item) => `<div class="stage-row"><span>${escapeHtml(item.stage || "Not reported")}</span><strong class="${stageClass(item.status)}">${escapeHtml(item.status || "NOT_STARTED")}</strong><small>${escapeHtml(item.next_action || "No next action reported")}</small></div>`).join("") : '<span class="muted">Stage manifest not published.</span>';
  const safety = data.safety_evidence || {};
  document.getElementById("safety-details").innerHTML = detailRows([
    ["Source quality", safetyLabel(safety.source_quality), false],
    ["Halt status", safetyLabel(safety.halt_status), false],
    ["Corporate actions", safetyLabel(safety.corporate_action_status), false],
    ["Liquidity evidence", safetyLabel(safety.liquidity_evidence), false],
  ]);
}

function stageClass(status) { return ["LOCAL_VERIFIED", "COMPLETE"].includes(String(status)) ? "good" : ["FAILED", "DEGRADED"].includes(String(status)) ? "bad" : "warn"; }

function detailRows(rows) { return rows.map(([label, value, good]) => `<dt>${escapeHtml(label)}</dt><dd class="${good ? "good" : "bad"}">${escapeHtml(String(value))}</dd>`).join(""); }
function setStatus(id, text, status) { const node = document.getElementById(id); if (!node) return; node.textContent = text; node.classList.toggle("good", ["COMPLETE", "ready", "NO_TRADE", "realized"].includes(String(status))); node.classList.toggle("bad", ["DEGRADED", "PARTIAL", "FAILED", "PENDING", "MISSING", "UNAVAILABLE", "UNREALIZED"].includes(String(status))); }
function statusChip(status) { const label = labelForStatus(status); const cls = ["COMPLETE", "NO_TRADE", "realized"].includes(String(status)) ? "good" : ["DEGRADED", "PARTIAL", "PENDING", "MISSING", "UNAVAILABLE", "UNREALIZED", "missing_outcome", "quarantined"].includes(String(status)) ? "bad" : ""; return `<span class="status-chip ${cls}">${escapeHtml(label)}</span>`; }
function labelForStatus(status) { return ({ COMPLETE: "Complete", PARTIAL: "Partial", PENDING: "Pending", MISSING: "Missing", UNAVAILABLE: "Unavailable", UNREALIZED: "Unrealized", DEGRADED: "Needs attention", NO_TRADE: "No trade", realized: "Realized", missing_outcome: "Outcome needed", quarantined: "Quarantined", unrealized: "Open", no_trade: "No trade" }[status] || "Not reported"); }
function formatPercent(value) { return numberOrNull(value) == null ? '<span class="value-muted">Not reported</span>' : `<span>${formatPercentText(value)}</span>`; }
function formatPercentText(value) { const numeric = numberOrNull(value); return numeric == null ? "Not reported" : `${numeric >= 0 ? "+" : ""}${numeric.toFixed(2)}%`; }
function formatPrice(value) { const numeric = numberOrNull(value); return numeric == null ? "Not reported" : `$${numeric.toFixed(2)}`; }
function formatMoney(value) { const numeric = numberOrNull(value); return numeric == null ? '<span class="value-muted">Not reported</span>' : `<span>${numeric >= 0 ? "+" : "-"}$${Math.abs(numeric / 100).toFixed(2)}</span>`; }
function formatMoneyText(value) { const numeric = numberOrNull(value); return numeric == null ? "Not reported" : `${numeric < 0 ? "-" : ""}$${Math.abs(numeric / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function returnContext(item) {
  const coverage = item.coverage?.coverage_pct == null ? "coverage not reported" : `coverage ${Number(item.coverage.coverage_pct).toFixed(1)}%`;
  const denominator = item.opening_equity_cents == null ? "opening-equity denominator not reported" : `opening-equity denominator ${formatCents(item.opening_equity_cents)}`;
  const sample = Number(item.realized_trade_count || 0) + Number(item.unrealized_trade_count || 0) + Number(item.missing_outcome_count || 0);
  return `Official paper · daily period ending ${item.market_date || "not reported"} · ${returnBasisLabel(item.return_basis)} · ${costStatusLabel(item.cost_status)} · ${denominator} · ${sample} observed/outcome row${sample === 1 ? "" : "s"} · ${coverage} · as of ${formatTimestamp(item.generated_at || item.calculated_at)}`;
}
function formatCents(value) { return `$${(Number(value) / 100).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`; }
function returnBasisLabel(value) { return ({ account_equity_identity_after_external_flows: "account equity change after explicit external flows", net_after_costs: "net after fees/slippage", gross_observed_or_missing: "gross observed; complete net result pending", gross_observed: "gross observed" }[value] || "return basis not reported"); }
function costStatusLabel(value) { return ({ complete: "fees/slippage complete", missing_cost_component: "fees/slippage incomplete", unknown: "cost treatment unknown" }[value] || "cost treatment not reported"); }
function safetyLabel(value) { return value?.state === "verified" ? "Verified" : value?.state === "blocked" ? "Blocked" : "Unknown — not reported"; }
function numberOrNull(value) { if (value == null || value === "") return null; const numeric = Number(value); return Number.isFinite(numeric) ? numeric : null; }
function valueClass(value) { const numeric = numberOrNull(value); return numeric == null ? "value-muted" : numeric < 0 ? "value-bad" : "value-good"; }
function sumReportedCounts(...values) { const numbers = values.map(numberOrNull); return numbers.some((value) => value == null) ? "Not reported" : String(numbers.reduce((total, value) => total + value, 0)); }
function shortDate(value) { return value ? value.slice(5) : "—"; }
function shortHash(value) { return value ? `${String(value).slice(0, 10)}…` : "Not reported"; }
function formatTimestamp(value) { if (!value) return "Not reported"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[char])); }

init();
