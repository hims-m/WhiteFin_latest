/* WhiteFIN dashboard — plain JS, no build step.
 * Talks to the FastAPI backend, streams agent steps live (Server-Sent Events
 * over fetch, since EventSource can't POST), and renders the result. */

const params = new URLSearchParams(location.search);
// Same origin whenever FastAPI serves this page (local :8000, Docker, Hugging Face
// Spaces...). Only VS Code Live Server (:5500) or opening the file directly
// needs the local backend address. ?api=https://... overrides everything.
const API = params.get("api")
  ?? (location.protocol.startsWith("http") && location.port !== "5500" ? "" : "http://127.0.0.1:8000");

const $ = (id) => document.getElementById(id);
const charts = {};
let lastResult = null;
let lastRequest = null;
let inputLevel = "";          // "" = auto-detect from the prompt
let viewLevel = "beginner";

const EXAMPLES = [
  "I have ₹1,00,000. I'm 25, moderate risk, investing for 15 years.",
  "₹20,000, aggressive, I'm 21, 10 years",
  "2 lakh for my masters in 2 years, I'm 23, I don't want high risk",
  "SIP of ₹5,000 per month, I'm 22, retire at 60, aggressive",
  "I want to invest some money",
];

const STAGES = [
  [["user_profiling", "Profiling"]],
  [["clarify", "Clarify"], ["market_intelligence", "Market data"]],
  [["fundamental_analysis", "Fundamentals"], ["risk_assessment", "Risk"], ["news_sentiment", "News"]],
  [["strategy_selector", "Strategy"]],
  [["portfolio_optimization", "Optimizer"], ["validator", "Validator"]],
  [["scenario_simulation", "Monte Carlo"], ["backtest", "Backtest"]],
  [["explainability", "Explainability"]],
];
const STAGE_NOTES = { 1: "either / or", 2: "parallel", 3: "MPT vs HRP", 4: "loop until approved", 5: "parallel" };

/* ---------------------------------------------------------------- utils */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (x) => x == null ? "—" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(x);
const pct = (x, d = 1) => x == null ? "—" : `${(x * 100).toFixed(d)}%`;
const num = (x, d = 2) => x == null ? "—" : Number(x).toFixed(d);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const alpha = (hex, a) => {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
};
const short = (t) => (t || "").replace(".NS", "");

/* ---------------------------------------------------------------- startup */
function init() {
  $("docs-link").href = `${API}/docs`;
  const ex = $("examples");
  EXAMPLES.forEach((e) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = e;
    b.onclick = () => { $("prompt").value = e; $("prompt").focus(); };
    ex.appendChild(b);
  });
  renderPipeline([]);
  renderLegend();
  $("analyze-form").addEventListener("submit", (ev) => { ev.preventDefault(); analyze(collectRequest()); });
  $("real-toggle").addEventListener("change", () => lastResult && renderSimulation(lastResult.simulation));
  $("history-btn").onclick = openHistory;
  segmented("level-input", (lvl) => { inputLevel = lvl; });
  segmented("level-view", (lvl) => { viewLevel = lvl; if (lastResult) applyView(); });
  $("history-close").onclick = () => $("history-dialog").close();
  checkHealth();
}

async function checkHealth() {
  const chip = $("status-chip");
  try {
    const h = await (await fetch(`${API}/api/health`)).json();
    const llm = h.llm_provider ? ` · LLM: ${h.llm_provider}` : " · no LLM (templates)";
    chip.textContent = (h.offline_mode ? "Offline demo data" : "Live data mode") + llm;
    chip.className = "chip " + (h.offline_mode ? "warn" : "ok");
  } catch {
    chip.textContent = "Backend not reachable — start uvicorn on :8000";
    chip.className = "chip bad";
  }
}

function segmented(id, onPick) {
  $(id).querySelectorAll("button").forEach((b) => {
    b.onclick = () => { setSegmented(id, b.dataset.level); onPick(b.dataset.level); };
  });
}
function setSegmented(id, level) {
  $(id).querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.level === level));
}

function collectRequest(extra = {}) {
  const v = (id) => $(id).value.trim();
  const req = { prompt: v("prompt") };
  if (inputLevel) req.experience = inputLevel;
  if (v("f-amount")) req.amount = Number(v("f-amount"));
  if (v("f-sip")) req.monthly_sip = Number(v("f-sip"));
  if (v("f-age")) req.age = Number(v("f-age"));
  if (v("f-horizon")) req.horizon_years = Number(v("f-horizon"));
  if (v("f-risk")) req.risk_tolerance = v("f-risk");
  return { ...req, ...extra };
}

/* ---------------------------------------------------------------- run */
async function analyze(req) {
  lastRequest = req;
  const btn = $("analyze-btn");
  btn.disabled = true; btn.textContent = "Agents working…";
  $("clarify-card").hidden = true;
  $("trace").innerHTML = "";
  $("trace-total").textContent = "";
  const steps = [];
  renderPipeline(steps);

  try {
    const resp = await fetch(`${API}/api/analyze/stream`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(formatError(err) || `HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const event = (raw.match(/^event: (.*)$/m) || [])[1];
        const data = JSON.parse((raw.match(/^data: (.*)$/m) || [])[1] || "{}");
        if (event === "step") { steps.push(data); addTrace(data); renderPipeline(steps); }
        else if (event === "result") handleResult(data, steps);
        else if (event === "error") throw new Error(data.detail);
      }
    }
  } catch (e) {
    const li = document.createElement("li");
    li.className = "error"; li.textContent = `Error: ${e.message}`;
    $("trace").appendChild(li);
  } finally {
    btn.disabled = false; btn.textContent = "Analyze & build my portfolio";
  }
}

function formatError(err) {
  if (!err || !err.detail) return "";
  if (Array.isArray(err.detail)) return err.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
  return err.detail;
}

function handleResult(result, steps) {
  const total = steps.reduce((s, x) => s + x.duration_ms, 0);
  $("trace-total").textContent = `${steps.length} steps · ${(total / 1000).toFixed(1)}s`;
  if (result.status === "needs_clarification") return renderClarify(result);
  renderResult(result);
}

/* ---------------------------------------------------------------- trace */
function addTrace(step) {
  const li = document.createElement("li");
  if (step.agent === "validator" && /sending back/.test(step.message)) li.className = "revise";
  if (step.agent === "clarify") li.className = "clarify";
  li.innerHTML = `<div class="t-head"><span>${esc(step.label)}</span><span class="t-time">${step.duration_ms} ms</span></div>
    <div class="t-msg">${esc(step.message)}</div>`;
  $("trace").appendChild(li);
  li.scrollIntoView({ block: "nearest" });
}

function renderPipeline(steps) {
  const counts = {};
  steps.forEach((s) => { counts[s.agent] = (counts[s.agent] || 0) + 1; });
  const finished = steps.length > 0 && (counts.explainability || counts.clarify);
  const html = STAGES.map((stage, i) => {
    const nodes = stage.map(([id, label]) => {
      const n = counts[id] || 0;
      let cls = "node";
      if (n > 1) cls += " looped"; else if (n === 1) cls += " done";
      else if (finished) cls += " skipped";
      return `<span class="${cls}">${esc(label)}${n > 1 ? ` ×${n}` : ""}</span>`;
    }).join("");
    const note = STAGE_NOTES[i] ? `<div class="stage-note">${STAGE_NOTES[i]}</div>` : "";
    return `<li>${note}<div class="stage">${nodes}</div></li>`;
  }).join("");
  $("pipeline").innerHTML = html;
}

function renderLegend() {
  $("graph-legend").innerHTML = [
    `<span class="lf">Profiling</span>`, `<span class="arrow">→ asks you if info is missing →</span>`,
    `<span class="lf">Market data</span>`, `<span class="arrow">→</span>`,
    `<span class="lf par">Fundamentals ∥ Risk ∥ News</span>`, `<span class="arrow">→</span>`,
    `<span class="lf">Strategy (MPT vs HRP)</span>`, `<span class="arrow">→</span>`,
    `<span class="lf loop">Optimizer ⇄ Validator</span>`, `<span class="arrow">→</span>`,
    `<span class="lf par">Monte Carlo ∥ Backtest</span>`, `<span class="arrow">→</span>`,
    `<span class="lf">Explainability</span>`,
  ].join("");
}

/* ---------------------------------------------------------------- clarification */
function renderClarify(result) {
  const form = $("clarify-form");
  form.innerHTML = result.questions.map((q) => {
    if (q.type === "choice") {
      return `<div class="q"><div class="q-title">${esc(q.question)}</div>${q.options.map((o, i) =>
        `<label class="choice"><input type="radio" name="${q.field}" value="${esc(o.value)}" ${i === 1 ? "checked" : ""}> ${esc(o.label)}</label>`).join("")}</div>`;
    }
    return `<div class="q"><label><span class="q-title">${esc(q.question)}</span>
      <input type="number" name="${q.field}" min="0" step="any" placeholder="${esc(q.placeholder || "")}" required></label></div>`;
  }).join("") + `<div class="btn-row">
      <button class="btn primary" type="submit">Continue</button>
      <button class="btn ghost" type="button" id="use-defaults">Just use defaults</button></div>`;
  $("clarify-card").hidden = false;
  $("clarify-card").scrollIntoView({ behavior: "smooth", block: "nearest" });

  form.onsubmit = (ev) => {
    ev.preventDefault();
    const answers = {};
    new FormData(form).forEach((v, k) => { answers[k] = k === "risk_tolerance" ? v : Number(v); });
    analyze({ ...lastRequest, ...answers });
  };
  $("use-defaults").onclick = () => analyze({ ...lastRequest, use_defaults: true });
}

/* ---------------------------------------------------------------- results */
function renderResult(r) {
  lastResult = r;
  $("placeholder").hidden = true;
  $("result-body").hidden = false;

  const md = r.market_data;
  const banner = $("data-banner");
  if (md.data_source !== "live") {
    banner.hidden = false;
    banner.textContent = md.data_source === "offline_fallback"
      ? "Demo mode: market data is synthetic (no live data reachable). Numbers illustrate the method, not real markets."
      : `Mixed data: ${pct(1 - md.live_ratio, 0)} of tickers fell back to synthetic data and were excluded from optimization where possible.`;
  } else banner.hidden = true;

  viewLevel = r.explanation.level || r.user_profile.experience || "beginner";
  renderAllocation(r.optimized_portfolio);
  renderHoldings(r.optimized_portfolio);
  renderSimulation(r.simulation);
  renderBacktest(r.backtest);
  renderValidator(r.validation, r.validation_history);
  renderStrategy(r.strategy, r.optimized_portfolio);
  renderUniverse(r);
  $("disclaimer").textContent = r.explanation.disclaimer;
  applyView();
}

/* Everything that differs between Beginner and Experienced. Switching re-renders
 * only these parts — no new request, because the backend sends both versions. */
function applyView() {
  const r = lastResult, lvl = viewLevel, adv = lvl === "advanced";
  setSegmented("level-view", lvl);
  $("result-body").className = adv ? "view-advanced" : "view-beginner";
  const titles = adv
    ? { alloc: "Strategic allocation", explain: "Rationale", sim: "Monte Carlo projection", bt: "Out-of-sample backtest" }
    : { alloc: "Your four jars", explain: "What this means for you", sim: "Your money over time", bt: "Did we test it?" };
  $("alloc-title").textContent = titles.alloc;
  $("explain-title").textContent = titles.explain;
  $("sim-title").textContent = titles.sim;
  $("bt-title").textContent = titles.bt;
  renderNutshell(r.explanation, lvl);
  renderKpis(r, lvl);
  renderExplanation(r.explanation, lvl);
  if (adv && r.optimized_portfolio.analytics) {
    renderFrontier(r.optimized_portfolio);
    renderRiskContribution(r.optimized_portfolio);
    renderCorrelation(r.optimized_portfolio.analytics);
  }
}

function renderNutshell(ex, lvl) {
  const text = ex.overview ? ex.overview[lvl] : (ex.summary || "").split("\n")[0];
  let note = "";
  if (lvl === ex.level) {
    if (ex.used_llm) note = `Written by ${ex.llm_provider}; every number checked against the agents' output.`;
    else if (ex.llm_rejected_reason) note = "The LLM's version changed a number, so it was rejected and the verified template is shown.";
  }
  $("nutshell").innerHTML = `<div class="n-label">In a nutshell</div><p>${esc(text)}</p>`
    + (note ? `<div class="llm-note">${esc(note)}</div>` : "");
}

function renderKpis(r, lvl) {
  const opt = r.optimized_portfolio, sim = r.simulation, ex = r.explanation, bl = opt.blended;
  const fv = sim.final_value_percentiles, fr = sim.final_value_percentiles_real;
  const er = opt.equity_sleeve_risk || {};
  const items = lvl === "advanced" ? [
    ["E[R] (portfolio)", pct(opt.blended_expected_annual_return), "CAPM equilibrium, annual"],
    ["σ (portfolio)", pct(opt.blended_annual_volatility), `ρ-adjusted; −${pct(bl.diversification_benefit)} vs ρ=1`],
    ["Sharpe", num(bl.sharpe_ratio), "rf 6.5%"],
    ["VaR95 / CVaR95", `${pct(-er.daily_var_95)} / ${pct(-er.daily_cvar_95)}`, "1-day, equity sleeve"],
    ["Max drawdown", pct(er.max_drawdown), `β ${num(er.beta)} vs benchmark`],
    [`P50 at ${sim.horizon_years}y`, inr(fv.p50_median), `real ${inr(fr.p50_median)} · P(loss) ${pct(sim.probability_of_loss)}`],
    ["Data quality", `${ex.data_quality_score}/100`, md_label(r.market_data)],
  ] : [
    ["Growth per year", `~${pct(opt.blended_expected_annual_return, 0)}`, "on average, long run"],
    ["How bumpy", bumpiness(opt.blended_annual_volatility), `most years within ±${pct(opt.blended_annual_volatility, 0)}`],
    [`Typical result in ${sim.horizon_years} years`, inr(fv.p50_median), `≈ ${inr(fr.p50_median)} in today's money`],
    ["Chance of losing money", pct(sim.probability_of_loss, 0), `ending below ${inr(sim.total_invested)}`],
  ];
  $("kpis").innerHTML = items.map(([l, v, s]) =>
    `<div class="kpi"><div class="k-label">${esc(l)}</div><div class="k-value">${esc(v)}</div><div class="k-sub">${esc(s)}</div></div>`).join("");
}
const bumpiness = (vol) => vol < 0.07 ? "Gentle" : vol < 0.12 ? "Moderate" : vol < 0.17 ? "Bumpy" : "Very bumpy";
const md_label = (md) => md.data_source === "live" ? "all live data" : md.data_source === "mixed" ? `${pct(md.live_ratio, 0)} live` : "synthetic demo data";

function palette() {
  return [css("--accent"), css("--accent-2"), "#a855f7", "#f59e0b", "#ef4444", "#14b8a6", "#64748b", "#ec4899", "#84cc16", "#0ea5e9"];
}

function makeChart(id, config) {
  if (charts[id]) charts[id].destroy();
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  charts[id] = new Chart($(id), config);
}

function renderAllocation(opt) {
  const labels = [], data = [];
  opt.holdings.forEach((h) => {
    if (h.asset_class === "stocks" && opt.execution.equity_vehicle === "index_fund" && !h.is_index_fund) return;
    labels.push(h.ticker ? short(h.ticker) : h.name);
    data.push(+(h.weight * 100).toFixed(1));
  });
  const colors = palette();
  makeChart("alloc-chart", {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: labels.map((_, i) => colors[i % colors.length]), borderWidth: 1, borderColor: css("--surface") }] },
    options: { maintainAspectRatio: false, cutout: "60%",
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${c.raw}%` } } } },
  });
  const b = opt.asset_class_allocation, base = opt.base_allocation;
  const notes = [`${pct(b.stocks, 0)} stocks · ${pct(b.bonds, 0)} bonds · ${pct(b.gold, 0)} gold · ${pct(b.cash, 0)} cash`];
  opt.allocation_adjustments.forEach((a) => notes.push(a));
  if (!opt.allocation_adjustments.length) notes.push(`Standard ${opt.risk_tolerance} mix (base ${pct(base.stocks, 0)} stocks) — no glide-path change needed.`);
  $("adjustments").innerHTML = notes.map((n) => `<li>${esc(n)}</li>`).join("");
}

function renderHoldings(opt) {
  const ex = opt.execution;
  const rows = [];
  rows.push(`<tr><th>Asset</th><th>Name</th><th class="num">Weight</th><th class="num">Target</th><th class="num">Price</th><th class="num">Units</th><th class="num">Invest</th></tr>`);
  const groups = { stocks: "Stocks", gold: "Gold", bonds: "Bonds", cash: "Cash" };
  Object.entries(groups).forEach(([cls, label]) => {
    const hs = opt.holdings.filter((h) => h.asset_class === cls);
    if (!hs.length) return;
    rows.push(`<tr class="group"><td colspan="7">${label}</td></tr>`);
    hs.forEach((h) => {
      const indexMode = ex.equity_vehicle === "index_fund" && cls === "stocks" && !h.is_index_fund;
      rows.push(`<tr${indexMode ? ' class="muted"' : ""}><td>${esc(h.ticker ? short(h.ticker) : "—")}</td><td>${esc(h.name)}${indexMode ? " <span class='tag'>later</span>" : ""}</td>
        <td class="num">${pct(h.weight)}</td><td class="num">${inr(h.target_rupees)}</td>
        <td class="num">${h.price != null ? inr(h.price) : "—"}</td><td class="num">${h.shares != null ? h.shares : "—"}</td>
        <td class="num">${inr(indexMode ? 0 : h.invested_rupees)}</td></tr>`);
    });
  });
  $("holdings").innerHTML = rows.join("");
  $("exec-basis").textContent = `based on ${ex.basis} of ${inr(ex.investable_amount)}`;
  const notes = [];
  if (ex.equity_vehicle === "index_fund") notes.push(`Stock budget is under ${inr(ex.min_direct_equity_rupees)}, so the stock portion goes into a Nifty 50 index fund until it grows.`);
  else if (ex.equity_leftover_rupees > 0) notes.push(`${inr(ex.equity_leftover_rupees)} of the stock budget is left after buying whole shares — park it in the liquid fund.`);
  (opt.notes || []).forEach((n) => notes.push(n));
  $("exec-note").textContent = notes.join(" ");
}

function renderSimulation(sim) {
  const y = sim.yearly;
  const real = $("real-toggle").checked;
  const defl = y.years.map((t) => Math.pow(1 + sim.inflation_rate, t));
  const adj = (arr) => real ? arr.map((v, i) => v / defl[i]) : arr;
  const a = css("--accent"), b = css("--accent-2"), m = css("--muted");
  makeChart("sim-chart", {
    type: "line",
    data: {
      labels: y.years.map((t) => `Y${Math.round(t)}`),
      datasets: [
        { label: "5th pct", data: adj(y.p5), borderColor: "transparent", pointRadius: 0, fill: false },
        { label: "5–95% range", data: adj(y.p95), borderColor: "transparent", backgroundColor: alpha(a, 0.12), pointRadius: 0, fill: "-1" },
        { label: "25th pct", data: adj(y.p25), borderColor: "transparent", pointRadius: 0, fill: false },
        { label: "25–75% range", data: adj(y.p75), borderColor: "transparent", backgroundColor: alpha(a, 0.25), pointRadius: 0, fill: "-1" },
        { label: "Median", data: adj(y.p50), borderColor: a, borderWidth: 2.5, pointRadius: 0, fill: false },
        { label: "Amount invested", data: adj(y.invested), borderColor: m, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, fill: false },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { filter: (i) => !/pct$/.test(i.text), boxWidth: 12 } },
        tooltip: { filter: (i) => !/range/.test(i.dataset.label), callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.raw)}` } },
      },
      scales: { y: { ticks: { callback: (v) => inr(v) } } },
    },
  });
  $("sim-note").textContent = `${sim.num_simulations.toLocaleString("en-IN")} simulated paths · ${sim.distribution} · `
    + `expected ${pct(sim.expected_annual_return)} / volatility ${pct(sim.annual_volatility)} · inflation ${pct(sim.inflation_rate)} · `
    + `chance of losing to inflation ${pct(sim.probability_of_losing_to_inflation)}.`;
}

function renderBacktest(bt) {
  if (!bt.available) {
    if (charts["bt-chart"]) charts["bt-chart"].destroy();
    $("bt-table").innerHTML = "";
    $("bt-note").textContent = bt.reason;
    return;
  }
  const s = bt.series;
  makeChart("bt-chart", {
    type: "line",
    data: { labels: s.dates, datasets: [
      { label: "WhiteFIN weights (trained on past only)", data: s.strategy, borderColor: css("--accent"), borderWidth: 2.5, pointRadius: 0 },
      { label: bt.benchmark.name, data: s.benchmark, borderColor: css("--accent-2"), borderWidth: 2, pointRadius: 0 },
      { label: "Equal-weight shortlist", data: s.equal_weight, borderColor: css("--muted"), borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { boxWidth: 12 } } },
      scales: { x: { ticks: { maxTicksLimit: 6 } }, y: { title: { display: true, text: "Growth of 100" } } } },
  });
  const row = (name, m) => `<tr><td>${esc(name)}</td><td class="num">${pct(m.total_return)}</td><td class="num">${pct(m.annual_volatility)}</td>
    <td class="num">${num(m.sharpe_ratio)}</td><td class="num">${pct(m.max_drawdown)}</td></tr>`;
  $("bt-table").innerHTML = `<tr><th></th><th class="num">Return</th><th class="num">Volatility</th><th class="num">Sharpe</th><th class="num">Max drawdown</th></tr>`
    + row("WhiteFIN", bt.strategy) + row(bt.benchmark.name, bt.benchmark) + row("Equal weight", bt.equal_weight_shortlist);
  $("bt-note").textContent = `Trained ${bt.train_period[0]} → ${bt.train_period[1]}, tested ${bt.test_period[0]} → ${bt.test_period[1]}. ` + bt.caveats.join(" ");
}

function renderStrategy(sel, opt) {
  const card = $("strategy-card");
  if (!sel) { card.hidden = true; return; }      // older history items
  card.hidden = false;
  const used = (opt.strategy || "mpt").toUpperCase();
  const sc = sel.scores || {};
  if (sc.mpt && sc.hrp) {
    const row = (k, name) => { const m = sc[k];
      return `<tr class="${k === sel.strategy ? "selected" : ""}"><td><b>${name}</b><br><span class="muted small">${esc(m.method)}</span></td>
        <td class="num">${pct(m.total_return)}</td><td class="num">${pct(m.annual_volatility)}</td><td class="num">${num(m.sharpe_ratio)}</td>
        <td class="num">${pct(m.max_drawdown)}</td><td class="num">${pct(m.largest_weight, 0)}</td></tr>`; };
    $("strategy-table").innerHTML = `<tr><th>Method</th><th class="num">Return</th><th class="num">Volatility</th><th class="num">Sharpe</th>
      <th class="num">Max drawdown</th><th class="num">Largest weight</th></tr>` + row("mpt", "MPT") + row("hrp", "HRP");
  } else $("strategy-table").innerHTML = "";
  const tp = sel.test_period ? `Tested on ${sel.test_period[0]} → ${sel.test_period[1]} (held out). ` : "";
  const override = used !== sel.strategy.toUpperCase() ? ` The Validator then switched the optimizer to ${used}.` : "";
  $("strategy-note").textContent = `${tp}Selector chose ${sel.strategy.toUpperCase()}: ${sel.reason}${override} `
    + (sel.caveats || []).join(" ");
}

function renderValidator(v, history) {
  const labels = { approved: "Approved", approved_with_warnings: "Approved with warnings", revise: "Revise" };
  let html = `<div class="verdict ${v.verdict}">${labels[v.verdict]} after ${v.attempt} attempt${v.attempt > 1 ? "s" : ""}</div>`;
  history.forEach((h) => {
    html += `<div class="attempt"><div class="attempt-title">Attempt ${h.attempt} — ${labels[h.verdict]}</div>`;
    const shown = h === history[history.length - 1] ? h.checks : h.checks.filter((c) => !c.passed);
    shown.forEach((c) => {
      html += `<div class="check ${c.passed ? "pass" : "fail"}"><span class="icon">${c.passed ? "✓" : "✗"}</span>
        <span>${esc(c.check)}<br><span class="detail">${esc(c.detail)}${c.fix ? ` → fix: ${esc(c.fix)}` : ""}</span></span></div>`;
    });
    html += `</div>`;
  });
  $("validator").innerHTML = html;
}

function renderExplanation(ex, lvl) {
  const sections = ex.levels ? ex.levels[lvl] : ex.sections;   // older history items have one version
  $("explanation").innerHTML = sections.map((sec) => {
    if (sec.body) return `<section><h3>${esc(sec.title)}</h3><p>${esc(sec.body)}</p></section>`;
    const lead = sec.lead ? `<p class="lead">${esc(sec.lead)}</p>` : "";
    const cards = (sec.cards || []).length ? `<div class="stock-grid">${sec.cards.map(stockCard).join("")}</div>` : "";
    const points = (sec.points || []).length ? `<ul class="points">${sec.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
    return `<section><h3>${esc(sec.title)}</h3>${lead}${cards}${points}</section>`;
  }).join("");
}

function stockCard(c) {
  const metrics = Object.entries(c.metrics || {});
  return `<div class="stock-card">
    <div class="sc-top">
      <div><div class="sc-name">${esc(c.name)}</div><div class="sc-sector">${esc(c.short)} · ${esc(c.sector || "")}</div></div>
      <div class="sc-weight" title="Share of the stock portion">${pct(c.weight_in_sleeve, 0)}</div>
    </div>
    <div class="sc-bar"><span style="width:${Math.min(100, c.weight_in_sleeve * 100 / 0.4).toFixed(0)}%"></span></div>
    <div class="sc-buy">${esc(c.buy)}</div>
    <ul class="sc-list">${c.pros.map((p) => `<li class="pro">${esc(p)}</li>`).join("")}${c.cons.map((p) => `<li class="con">${esc(p)}</li>`).join("")}</ul>
    ${metrics.length ? `<div class="sc-metrics">${metrics.map(([k, v]) => `<div>${esc(k)}<b>${esc(v)}</b></div>`).join("")}</div>` : ""}
  </div>`;
}

/* ---------------------------------------------------------------- experienced-only charts */
function renderFrontier(opt) {
  const a = opt.analytics;
  const pts = (arr) => arr.map((p) => ({ x: +(p.vol * 100).toFixed(2), y: +(p.ret * 100).toFixed(2), t: p.ticker }));
  makeChart("frontier-chart", {
    type: "scatter",
    data: { datasets: [
      { label: "Efficient frontier", data: pts(a.frontier), showLine: true, borderColor: css("--accent"), backgroundColor: css("--accent"), pointRadius: 0, borderWidth: 2.5 },
      { label: "Individual stocks", data: pts(a.assets), backgroundColor: alpha(css("--accent-2"), 0.8), pointRadius: 5 },
      { label: "Chosen sleeve", data: pts([a.portfolio]), backgroundColor: css("--warn"), pointStyle: "star", pointRadius: 12, borderColor: css("--warn"), borderWidth: 2 },
    ] },
    options: { maintainAspectRatio: false,
      plugins: { legend: { labels: { boxWidth: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.raw.t ? short(c.raw.t) + ": " : c.dataset.label + ": "}σ ${c.raw.x}% · E[R] ${c.raw.y}%` } } },
      scales: { x: { title: { display: true, text: "Volatility σ (% p.a.)" } }, y: { title: { display: true, text: "Expected return (% p.a.)" } } } },
  });
}

function renderRiskContribution(opt) {
  const w = opt.equity_weights_within_stocks_sleeve, rc = opt.analytics.risk_contribution;
  const tickers = Object.keys(w).sort((a, b) => w[b] - w[a]);
  makeChart("riskc-chart", {
    type: "bar",
    data: { labels: tickers.map(short), datasets: [
      { label: "Share of capital", data: tickers.map((t) => +(w[t] * 100).toFixed(1)), backgroundColor: alpha(css("--accent"), 0.85) },
      { label: "Share of risk", data: tickers.map((t) => +((rc[t] || 0) * 100).toFixed(1)), backgroundColor: alpha(css("--warn"), 0.85) },
    ] },
    options: { indexAxis: "y", maintainAspectRatio: false,
      plugins: { legend: { labels: { boxWidth: 12 } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.raw}%` } } },
      scales: { x: { ticks: { callback: (v) => v + "%" } } } },
  });
}

function renderCorrelation(a) {
  const { tickers, matrix } = a.correlation;
  const accent = css("--accent"), warn = css("--warn");
  const cell = (v, i, j) => {
    const color = v >= 0 ? warn : accent;
    const strength = i === j ? 0.08 : Math.min(1, Math.abs(v)) * 0.85;
    return `<td style="background:${alpha(color, strength)}">${v.toFixed(2)}</td>`;
  };
  $("corr").innerHTML = `<tr><th></th>${tickers.map((t) => `<th>${esc(short(t))}</th>`).join("")}</tr>`
    + matrix.map((row, i) => `<tr><th>${esc(short(tickers[i]))}</th>${row.map((v, j) => cell(v, i, j)).join("")}</tr>`).join("");
  $("corr-note").textContent = `average pairwise ρ ${num(a.average_pairwise_correlation)} · lower = better diversification`;
}

function renderUniverse(r) {
  const f = r.fundamentals, risk = r.risk_metrics, opt = r.optimized_portfolio, md = r.market_data;
  const chosen = new Set(Object.keys(opt.equity_weights_within_stocks_sleeve));
  const news = (r.news && r.news.per_ticker) || {};
  const newsCell = (t) => {
    const n = news[t];
    if (!n || n.sentiment == null) return '<span class="muted">—</span>';
    const flag = n.severe ? ` <span class="tag flag" title="${esc(n.severe_reason)}">red flag</span>` : "";
    return `<span title="${esc((n.headlines[0] || {}).title || "")}">${n.sentiment > 0 ? "+" : ""}${num(n.sentiment)}</span>${flag}`;
  };
  const rows = Object.entries(f).sort((a, b) => b[1].fundamental_score - a[1].fundamental_score).map(([t, x]) => {
    const rk = risk[t] || {};
    const src = md.stocks[t].source === "live" ? '<span class="tag live">live</span>' : '<span class="tag synthetic">synthetic</span>';
    return `<tr class="${chosen.has(t) ? "selected" : ""}"><td><b>${esc(short(t))}</b></td><td>${esc(x.sector)}</td>
      <td class="num"><b>${num(x.fundamental_score, 0)}</b></td><td class="num">${num(x.pe, 1)}</td><td class="num">${num(x.pb, 1)}</td>
      <td class="num">${pct(x.roe)}</td><td class="num">${x.is_financial ? "n/a" : num(x.debt_to_equity)}</td><td class="num">${pct(x.profit_margin)}</td>
      <td class="num">${pct(rk.annual_volatility)}</td><td class="num">${num(rk.beta)}</td><td class="num">${pct(rk.max_drawdown)}</td>
      <td class="num">${opt.expected_returns_used[t] != null ? pct(opt.expected_returns_used[t]) : "—"}</td><td>${newsCell(t)}</td><td>${src}</td></tr>`;
  });
  $("universe").innerHTML = `<tr><th>Stock</th><th>Sector</th><th class="num">Score</th><th class="num">P/E</th><th class="num">P/B</th><th class="num">ROE</th>
    <th class="num">D/E</th><th class="num">Margin</th><th class="num">Vol</th><th class="num">Beta</th><th class="num">Max DD</th><th class="num">Exp. return</th><th>News</th><th>Data</th></tr>`
    + rows.join("");
}

/* ---------------------------------------------------------------- history */
async function openHistory() {
  const list = $("history-list");
  list.innerHTML = `<li class="muted">Loading…</li>`;
  $("history-dialog").showModal();
  try {
    const items = await (await fetch(`${API}/api/history?limit=30`)).json();
    if (!items.length) { list.innerHTML = `<li class="muted">No analyses yet.</li>`; return; }
    list.innerHTML = items.map((h) => {
      const p = h.user_profile;
      return `<li data-id="${h.id}"><div class="h-prompt">${esc(h.prompt)}</div>
        <div class="h-meta">#${h.id} · ${new Date(h.created_at).toLocaleString()} · ${esc(p.risk_tolerance)} · ${p.horizon_years}y · ${inr(p.amount)}</div></li>`;
    }).join("");
    list.querySelectorAll("li[data-id]").forEach((li) => {
      li.onclick = async () => {
        const r = await (await fetch(`${API}/api/history/${li.dataset.id}`)).json();
        $("history-dialog").close();
        $("trace").innerHTML = ""; (r.trace || []).forEach(addTrace); renderPipeline(r.trace || []);
        $("prompt").value = r.user_profile.raw_prompt || li.querySelector(".h-prompt").textContent;
        renderResult(r);
      };
    });
  } catch {
    list.innerHTML = `<li class="error">Couldn't load history.</li>`;
  }
}

init();
