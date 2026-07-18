const tableState = new Map();
const DASHBOARD_SCHEMA = "dawnstrike.static-dashboard.v3";

const formatValue = (value) => {
  if (value === null || value === undefined || value === "") return "n/a";
  return String(value);
};

const byId = (id) => document.getElementById(id);

const text = (id, value) => {
  const element = byId(id);
  if (element) element.textContent = formatValue(value);
};

const resolveFreshness = (data, now = Date.now()) => {
  if (data.schemaVersion !== DASHBOARD_SCHEMA) {
    return {
      healthy: false,
      deployment: "Evidence contract unsupported",
      label: "Freshness unavailable",
      detail: "The loaded dashboard payload does not use the current evidence contract.",
    };
  }

  const freshness = data.freshness || {};
  const asOfDate = freshness.asOfDate || data.latestRunDate;
  const deadlineAt = freshness.deadlineAt || data.freshnessDeadline;
  const statusAtGeneration = freshness.statusAtGeneration;
  const deadline = Date.parse(deadlineAt);
  const generated = Date.parse(data.generatedAt);
  if (
    !asOfDate ||
    !Number.isFinite(deadline) ||
    !Number.isFinite(generated) ||
    !["fresh", "stale"].includes(statusAtGeneration)
  ) {
    return {
      healthy: false,
      deployment: "Evidence freshness unknown",
      label: "Freshness unavailable",
      detail: "The payload is missing a valid generation time, as-of date, or freshness deadline.",
    };
  }

  const stale = statusAtGeneration === "stale" || now > deadline;
  return {
    healthy: !stale,
    deployment: stale ? "Evidence stale" : "Current evidence loaded",
    label: stale ? `Stale · as of ${asOfDate}` : `Fresh · as of ${asOfDate}`,
    detail: `${stale ? "Freshness deadline passed" : "Fresh through"} ${deadlineAt}`,
  };
};

const clear = (element) => {
  if (element) element.replaceChildren();
};

const numeric = (value) => {
  const raw = formatValue(value).replace(/[$,%R,]/g, "");
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : null;
};

const toneClass = (value) => {
  const raw = formatValue(value).toLowerCase();
  const parsed = numeric(raw);
  if (raw.includes("blocked") || raw.includes("loss") || (parsed !== null && parsed < 0)) return "negative";
  if (
    raw.includes("pass") ||
    raw.includes("ready") ||
    raw.includes("verified") ||
    raw.includes("official") ||
    raw.includes("delivery recorded") ||
    raw.includes("win") ||
    (parsed !== null && parsed > 0)
  ) {
    return "positive";
  }
  if (
    raw.includes("warn") ||
    raw.includes("experimental") ||
    raw.includes("pending") ||
    raw.includes("confirm") ||
    raw.includes("not yet") ||
    raw.includes("starts")
  ) {
    return "warning";
  }
  return "neutral";
};

const badgeTone = (value) => {
  const tone = toneClass(value);
  if (tone === "negative") return "bad";
  if (tone === "positive") return "ok";
  if (tone === "warning") return "warn";
  return "quiet";
};

const appendText = (parent, tag, value, className = "") => {
  const element = document.createElement(tag);
  element.textContent = formatValue(value);
  if (className) element.className = className;
  parent.appendChild(element);
  return element;
};

const cell = (value, className = "") => {
  const td = document.createElement("td");
  td.textContent = formatValue(value);
  if (className) td.className = className;
  return td;
};

const badge = (value, tone = "quiet") => {
  const span = document.createElement("span");
  span.className = `status-pill ${tone}`;
  span.textContent = formatValue(value);
  return span;
};

const sortRows = (rows, key, direction) => {
  const dir = direction === "desc" ? -1 : 1;
  return [...rows].sort((a, b) => {
    const leftNumber = numeric(a[key]);
    const rightNumber = numeric(b[key]);
    if (leftNumber !== null && rightNumber !== null) return (leftNumber - rightNumber) * dir;
    return formatValue(a[key]).localeCompare(formatValue(b[key]), undefined, {
      numeric: true,
      sensitivity: "base",
    }) * dir;
  });
};

const bindSortButtons = (tableId, rows, renderRows) => {
  const table = byId(tableId);
  if (!table) return;
  table.querySelectorAll("button[data-sort]").forEach((button) => {
    button.onclick = () => {
      const key = button.getAttribute("data-sort");
      const current = tableState.get(tableId) || {};
      const direction = current.key === key && current.direction === "asc" ? "desc" : "asc";
      tableState.set(tableId, { key, direction });
      renderRows(sortRows(rows, key, direction));
    };
  });
};

const renderQuickActions = (actions = []) => {
  const row = byId("quick-actions");
  clear(row);
  actions.forEach((action) => {
    const link = document.createElement("a");
    link.href = formatValue(action.href);
    link.className = action.tone === "primary" ? "button primary" : "button";
    link.textContent = formatValue(action.label);
    row.appendChild(link);
  });
};

const renderMetrics = (metrics = []) => {
  const grid = byId("metric-grid");
  clear(grid);
  metrics.forEach((metric) => {
    const article = document.createElement("article");
    article.className = `metric-tone-${formatValue(metric.tone)}`;
    appendText(article, "span", metric.label, "metric-title");
    appendText(article, "strong", metric.value);
    appendText(article, "p", metric.context);
    grid.appendChild(article);
  });
};

const renderWatchlist = (watchlist = {}) => {
  const rows = watchlist.rows || [];
  text("watchlist-date", `${formatValue(watchlist.title)} - ${formatValue(watchlist.date)}`);
  text("watchlist-count", `${formatValue(watchlist.candidateCount)} candidates`);
  text("watchlist-note", watchlist.note);
  text(
    "hero-top-symbol",
    rows[0] ? `${formatValue(rows[0].ticker)} / ${formatValue(rows[0].gate)}` : "n/a"
  );
  text(
    "status-gate",
    watchlist.gateSummary ||
      `${formatValue(watchlist.blockedCount)} blocked / ${formatValue(watchlist.clearedCount)} cleared`
  );

  const renderRows = (displayRows) => {
    const body = byId("watchlist-body");
    clear(body);
    displayRows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.append(cell(row.rank));

      const ticker = document.createElement("td");
      ticker.className = "ticker-cell";
      appendText(ticker, "strong", row.ticker);
      appendText(ticker, "span", row.company);
      tr.appendChild(ticker);

      tr.append(cell(row.score, toneClass(row.score)));
      tr.append(cell(row.gate, toneClass(row.gate)));
      tr.append(cell(row.gapPct, toneClass(row.gapPct)));
      tr.append(cell(row.entryTrigger));
      tr.append(cell(row.rewardRisk, toneClass(row.rewardRisk)));

      const source = document.createElement("td");
      source.className = "source-stack";
      appendText(source, "strong", row.source);
      appendText(source, "span", `${formatValue(row.sourceCount)} source / ${formatValue(row.sourceConfidence)}`);
      tr.appendChild(source);

      body.appendChild(tr);
    });
  };

  renderRows(rows);
  bindSortButtons("watchlist-table", rows, renderRows);
};

const renderEvidenceRail = (items = []) => {
  const rail = byId("evidence-rail");
  clear(rail);
  items.forEach((item) => {
    const article = document.createElement("article");
    article.className = "rail-item";
    appendText(article, "span", item.label);
    appendText(article, "strong", item.value);
    article.appendChild(badge(item.status, toneClass(item.status) === "negative" ? "bad" : "quiet"));
    appendText(article, "p", item.detail);
    rail.appendChild(article);
  });
};

const renderCurrent = (current = {}) => {
  const grid = byId("current-records");
  clear(grid);
  (current.records || []).forEach((row) => {
    const card = document.createElement("article");
    card.className = "ticket-card";

    const header = document.createElement("header");
    appendText(header, "span", row.date);
    appendText(header, "strong", row.symbol);
    header.appendChild(badge(row.status || "paper", "quiet"));
    card.appendChild(header);

    const dl = document.createElement("dl");
    [
      ["Strategy", row.strategy],
      ["Direction", row.direction],
      ["Entry", row.entry],
      ["Stop", row.stop],
      ["Target", row.target],
      ["P&L", row.pnl],
      ["R", row.r],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      appendText(item, "dt", label);
      appendText(item, "dd", value, toneClass(value));
      dl.appendChild(item);
    });
    card.appendChild(dl);
    grid.appendChild(card);
  });
  text("current-note", current.note);
};

const updateCalendarDetail = (tile, button) => {
  document.querySelectorAll(".calendar-day.selected").forEach((day) => {
    day.classList.remove("selected");
    day.removeAttribute("aria-current");
  });
  if (button) {
    button.classList.add("selected");
    button.setAttribute("aria-current", "date");
  }
  text("calendar-selected-date", tile.date);
  text("calendar-selected-return", tile.dailyReturn);
  text("calendar-selected-trades", tile.activity || tile.tradeCount);
  text(
    "calendar-selected-state",
    tile.observed === false ? "not observed" : tile.state || (tile.noTrade ? "no-trade" : "active")
  );
  text("calendar-selected-note", tile.detailNote);

  const strategyList = byId("calendar-selected-strategies");
  clear(strategyList);
  const strategyRows = Array.isArray(tile.strategyReturns) ? tile.strategyReturns : [];
  if (strategyRows.length === 0) {
    appendText(
      strategyList,
      "p",
      tile.detailNote || "No official per-strategy evidence is retained for this date.",
      "calendar-empty-state"
    );
  }
  strategyRows.forEach((row) => {
    const article = document.createElement("article");
    article.className = "calendar-strategy-row";
    article.setAttribute("role", "listitem");

    const identity = document.createElement("div");
    appendText(identity, "strong", row.name);
    appendText(identity, "span", row.status);
    article.appendChild(identity);

    const result = document.createElement("div");
    appendText(result, "strong", row.return, toneClass(row.return));
    appendText(result, "span", row.pnl);
    article.appendChild(result);

    appendText(article, "small", row.activity);
    strategyList.appendChild(article);
  });
};

const renderCalendar = (calendar = {}) => {
  const summary = byId("calendar-summary");
  clear(summary);
  const s = calendar.summary || {};
  [
    calendar.currentMonth,
    `${formatValue(s.monthlyReturn)} month`,
    `${formatValue(s.totalTrades)} trades`,
    `${formatValue(s.noTradeDays)} no-trade`,
    `${formatValue(s.observedDays)} observed`,
  ].forEach((item) => appendText(summary, "span", item));

  const grid = byId("calendar-grid");
  clear(grid);
  const renderedTiles = [];
  (calendar.tiles || []).forEach((tile) => {
    const button = document.createElement("button");
    button.type = "button";
    const tone = ["positive", "negative", "flat"].includes(tile.tone)
      ? `tone-${tile.tone}`
      : "";
    button.className = [
      "calendar-day",
      tile.observed === false ? "not-observed" : "",
      tile.noTrade ? "no-trade" : "",
      tile.warning ? "warning" : "",
      tone,
    ]
      .filter(Boolean)
      .join(" ");
    button.setAttribute(
      "aria-label",
      `${formatValue(tile.date)} return ${formatValue(tile.dailyReturn)} ${formatValue(tile.activity)}`
    );
    appendText(button, "span", tile.day);
    appendText(button, "strong", tile.dailyReturn);
    appendText(button, "small", tile.activity || `${formatValue(tile.tradeCount)} trades`);
    button.addEventListener("click", () => updateCalendarDetail(tile, button));
    grid.appendChild(button);
    renderedTiles.push({ tile, button });
  });
  const initial = [...renderedTiles].reverse().find((item) => item.tile.observed !== false);
  if (initial) updateCalendarDetail(initial.tile, initial.button);
};

const renderPaper = (paper = {}) => {
  const rows = paper.recentRows || [];
  const rowLabel = paper.rowLabel || `${formatValue(paper.totalRows)} paper rows`;
  text("paper-count", rowLabel);
  text("status-paper", rowLabel);

  const renderRows = (displayRows) => {
    const body = byId("paper-body");
    clear(body);
    displayRows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.append(
        cell(row.date),
        cell(row.symbol),
        cell(row.strategy),
        cell(row.entry),
        cell(row.stop),
        cell(row.target),
        cell(row.pnl, toneClass(row.pnl)),
        cell(row.r, toneClass(row.r)),
        cell(row.status, toneClass(row.status))
      );
      body.appendChild(tr);
    });
  };

  renderRows(rows);
  bindSortButtons("paper-table", rows, renderRows);
};

const renderRisk = (risk = {}) => {
  text("no-picks-headline", risk.headline || "Risk status");
  text("risk-watch", risk.watchCount);
  text("risk-accepted", risk.acceptedCount);
  text("risk-blocked", risk.blockedCount);
  const list = byId("risk-reasons");
  clear(list);
  (risk.topReasons || []).forEach((reason) => {
    appendText(list, "li", reason);
  });
};

const renderStrategies = (strategies = []) => {
  const grid = byId("strategy-grid");
  clear(grid);
  strategies.forEach((row) => {
    const card = document.createElement("article");
    card.className = "strategy-card";
    appendText(card, "h3", row.name);
    card.appendChild(badge(row.status, badgeTone(row.status)));

    const bar = document.createElement("div");
    bar.className = "health-bar";
    const fill = document.createElement("span");
    const observedWinRate = numeric(row.winRate);
    const winRate = observedWinRate === null ? 0 : Math.max(0, Math.min(100, observedWinRate));
    if (observedWinRate === null) bar.classList.add("unavailable");
    bar.setAttribute(
      "aria-label",
      observedWinRate === null ? "Win rate unavailable" : `Win rate ${winRate}%`
    );
    fill.style.width = `${winRate}%`;
    bar.appendChild(fill);
    card.appendChild(bar);

    const dl = document.createElement("dl");
    dl.className = "strategy-stats";
    [
      ["Trades", row.trades],
      ["Win rate", row.winRate],
      ["Return", row.return],
      ["Drawdown", row.drawdown],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      appendText(item, "dt", label);
      appendText(item, "dd", value, toneClass(value));
      dl.appendChild(item);
    });
    card.appendChild(dl);
    appendText(card, "p", row.validation);
    grid.appendChild(card);
  });
};

const renderSystem = (system = {}) => {
  text("system-status", `${formatValue(system.schedulerStatus)} / ${formatValue(system.telegramReadiness)}`);

  const flow = byId("system-flow");
  clear(flow);
  (system.flow || []).forEach((item) => {
    const card = document.createElement("article");
    card.className = "flow-card";
    appendText(card, "strong", item.name);
    appendText(card, "p", item.description);
    card.appendChild(badge(item.status, badgeTone(item.status)));
    flow.appendChild(card);
  });

  const timeline = byId("audit-timeline");
  clear(timeline);
  (system.taskStatuses || []).forEach((task) => {
    const card = document.createElement("article");
    card.className = "timeline-card";
    appendText(card, "strong", task.task_name);
    appendText(card, "p", `Last ${formatValue(task.last_run_time)} / next ${formatValue(task.next_run_time)}`);
    card.appendChild(badge(`${formatValue(task.state)} result ${formatValue(task.last_result)}`, "quiet"));
    timeline.appendChild(card);
  });
};

const renderDashboard = (data) => {
  const freshness = resolveFreshness(data);
  text("status-deployment", freshness.deployment);
  const deployment = byId("status-deployment");
  if (deployment) deployment.className = `status-pill ${freshness.healthy ? "ok" : "bad"}`;
  text("subheadline", data.subheadline);
  text("latest-run-date", data.latestRunDate);
  text(
    "overall-status",
    freshness.healthy
      ? data.overallStatus
      : `${formatValue(data.overallStatus)} · ${freshness.detail}`
  );
  text("status-freshness", freshness.label);
  const freshnessBadge = byId("status-freshness");
  if (freshnessBadge) {
    freshnessBadge.className = `status-pill ${freshness.healthy ? "ok" : "bad"}`;
  }
  text(
    "source-line",
    `Source ${formatValue(data.sourceCommit)} - generated ${formatValue(data.generatedAt)} - ${freshness.detail}`
  );

  renderQuickActions(data.quickActions || []);
  renderMetrics(data.topMetrics || []);
  renderWatchlist(data.operatorWatchlist || {});
  renderEvidenceRail(data.evidenceRail || []);
  renderCurrent(data.current || {});
  renderCalendar(data.calendar || {});
  renderPaper(data.paperTrading || {});
  renderRisk(data.noPicks || {});
  renderStrategies(data.strategies || []);
  renderSystem(data.system || {});
};

fetch("/assets/dashboard-data.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`Dashboard data failed: ${response.status}`);
    return response.json();
  })
  .then(renderDashboard)
  .catch((error) => {
    text("subheadline", error.message);
    text("status-deployment", "Data load error");
    const deployment = byId("status-deployment");
    if (deployment) deployment.className = "status-pill bad";
  });
