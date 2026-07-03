const formatValue = (value) => {
  if (value === null || value === undefined || value === "") return "n/a";
  return String(value);
};

const text = (id, value) => {
  const element = document.getElementById(id);
  if (element) element.textContent = formatValue(value);
};

const toneClass = (value) => {
  const raw = String(value || "").toLowerCase();
  if (raw.includes("-") || raw.includes("loss") || raw.includes("blocked")) return "negative";
  if (raw.includes("+") || raw.includes("win") || raw === "positive") return "positive";
  return "neutral";
};

const cell = (value, className = "") => {
  const td = document.createElement("td");
  td.textContent = formatValue(value);
  if (className) td.className = className;
  return td;
};

const renderMetrics = (metrics = []) => {
  const grid = document.getElementById("metric-grid");
  grid.replaceChildren();
  metrics.forEach((metric) => {
    const article = document.createElement("article");
    const label = document.createElement("span");
    label.className = "metric-title";
    label.textContent = formatValue(metric.label);
    const strong = document.createElement("strong");
    strong.textContent = formatValue(metric.value);
    const detail = document.createElement("p");
    detail.textContent = formatValue(metric.context);
    article.append(label, strong, detail);
    grid.appendChild(article);
  });
};

const renderCurrent = (current) => {
  const stack = document.getElementById("current-records");
  stack.replaceChildren();
  (current.records || []).forEach((row) => {
    const item = document.createElement("article");
    item.className = "mini-ticket";
    item.innerHTML = `
      <div>
        <span>${formatValue(row.date)}</span>
        <strong>${formatValue(row.symbol)}</strong>
      </div>
      <dl>
        <div><dt>Strategy</dt><dd>${formatValue(row.strategy)}</dd></div>
        <div><dt>Entry</dt><dd>${formatValue(row.entry)}</dd></div>
        <div><dt>Stop</dt><dd>${formatValue(row.stop)}</dd></div>
        <div><dt>Target</dt><dd>${formatValue(row.target)}</dd></div>
      </dl>
    `;
    stack.appendChild(item);
  });
  text("current-note", current.note);
};

const renderTopFive = (topFive) => {
  text("top-five-date", `${formatValue(topFive.label)} - ${formatValue(topFive.date)}`);
  const body = document.getElementById("top-five-body");
  body.replaceChildren();
  (topFive.rows || []).forEach((row, index) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(index + 1),
      cell(row.symbol),
      cell(row.strategy),
      cell(row.entry),
      cell(row.stop),
      cell(row.target),
      cell(row.r, toneClass(row.r)),
      cell(row.state)
    );
    body.appendChild(tr);
  });
};

const renderCalendar = (calendar) => {
  const summary = document.getElementById("calendar-summary");
  const s = calendar.summary || {};
  summary.innerHTML = `
    <span>${formatValue(calendar.currentMonth)}</span>
    <span>${formatValue(s.monthlyReturn)} month</span>
    <span>${formatValue(s.totalTrades)} trades</span>
    <span>${formatValue(s.noTradeDays)} no-trade</span>
  `;

  const grid = document.getElementById("calendar-grid");
  grid.replaceChildren();
  (calendar.tiles || []).forEach((tile) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `calendar-day ${tile.noTrade ? "no-trade" : ""} ${tile.warning ? "warning" : ""}`;
    button.setAttribute(
      "aria-label",
      `${formatValue(tile.date)} return ${formatValue(tile.dailyReturn)} trades ${formatValue(tile.tradeCount)}`
    );
    button.innerHTML = `
      <span>${formatValue(tile.day)}</span>
      <strong>${formatValue(tile.dailyReturn)}</strong>
      <small>${formatValue(tile.tradeCount)} trades</small>
    `;
    grid.appendChild(button);
  });
};

const renderPaper = (paper) => {
  text("paper-count", `${formatValue(paper.totalRows)} paper rows`);
  const body = document.getElementById("paper-body");
  body.replaceChildren();
  (paper.recentRows || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(row.date),
      cell(row.symbol),
      cell(row.strategy),
      cell(row.entry),
      cell(row.stop),
      cell(row.target),
      cell(row.pnl, toneClass(row.pnl)),
      cell(row.r, toneClass(row.r))
    );
    body.appendChild(tr);
  });
};

const renderRisk = (risk) => {
  text("no-picks-headline", risk.headline || "Risk status");
  text("risk-watch", risk.watchCount);
  text("risk-accepted", risk.acceptedCount);
  text("risk-blocked", risk.blockedCount);
  const list = document.getElementById("risk-reasons");
  list.replaceChildren();
  (risk.topReasons || []).forEach((reason) => {
    const item = document.createElement("li");
    item.textContent = formatValue(reason);
    list.appendChild(item);
  });
};

const renderStrategies = (strategies = []) => {
  const body = document.getElementById("strategy-body");
  body.replaceChildren();
  strategies.forEach((row) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(row.name),
      cell(row.status),
      cell(row.trades),
      cell(row.winRate),
      cell(row.return, toneClass(row.return)),
      cell(row.drawdown, toneClass(row.drawdown)),
      cell(row.validation)
    );
    body.appendChild(tr);
  });
};

const renderDashboard = (data) => {
  text("headline", data.headline);
  text("subheadline", data.subheadline);
  text("latest-run-date", data.latestRunDate);
  text("overall-status", data.overallStatus);
  text("source-line", `Source ${formatValue(data.sourceCommit)} - generated ${formatValue(data.generatedAt)}`);
  renderMetrics(data.topMetrics || []);
  renderCurrent(data.current || {});
  renderTopFive(data.topFive || {});
  renderCalendar(data.calendar || {});
  renderPaper(data.paperTrading || {});
  renderRisk(data.noPicks || {});
  renderStrategies(data.strategies || []);
};

fetch("/assets/dashboard-data.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`Dashboard data failed: ${response.status}`);
    return response.json();
  })
  .then(renderDashboard)
  .catch((error) => {
    text("headline", "Dashboard data unavailable");
    text("subheadline", error.message);
    text("status-deployment", "Data load error");
  });
