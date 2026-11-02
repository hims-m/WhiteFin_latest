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
document.documentElement.classList.add("js");   // reveal animations only hide content when JS runs
if (params.get("palette")) document.documentElement.dataset.palette = params.get("palette");   // preview a colour palette
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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

/* The agent graph, row by row (top to bottom), and its edges. Ids match the backend's step.agent. */
const GRAPH_ROWS = [
  [["user_profiling", "Profiling"]],
  [["clarify", "Clarify"], ["market_intelligence", "Market data"]],
  [["fundamental_analysis", "Fundamentals"], ["risk_assessment", "Risk"], ["news_sentiment", "News"]],
  [["strategy_selector", "Strategy"]],
  [["portfolio_optimization", "Optimizer"], ["validator", "Validator"]],
  [["scenario_simulation", "Monte Carlo"], ["backtest", "Backtest"]],
  [["explainability", "Explain"]],
];
const GRAPH_EDGES = [
  ["user_profiling", "clarify"], ["user_profiling", "market_intelligence"],
  ["market_intelligence", "fundamental_analysis"], ["market_intelligence", "risk_assessment"], ["market_intelligence", "news_sentiment"],
  ["fundamental_analysis", "strategy_selector"], ["risk_assessment", "strategy_selector"], ["news_sentiment", "strategy_selector"],
  ["strategy_selector", "portfolio_optimization"],
  ["portfolio_optimization", "validator", "side"], ["validator", "portfolio_optimization", "loop"],
  ["validator", "scenario_simulation"], ["validator", "backtest"],
  ["scenario_simulation", "explainability"], ["backtest", "explainability"],
];

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
const ICONS = {
  check: '<path d="M3 8.5l3 3 7-7"/>',
  cross: '<path d="M4 4l8 8M12 4l-8 8"/>',
  alert: '<path d="M8 3v6"/><path d="M8 12.5v.5"/>',
};
const icon = (name) => `<svg class="icon-svg" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

/* ---------------------------------------------------------------- reveal on scroll
 * Each block of the results rises in as it enters the screen; its figures count up and its
 * chart draws then. Content is only hidden while JS runs (.js), and reduced motion skips it. */
const Reveal = (() => {
  const pending = new Map();
  let io = null;
  const ease = (t) => 1 - Math.pow(1 - t, 3);

  function count(root) {
    root.querySelectorAll(".k-value").forEach((node) => {
      const spec = node._count;
      if (!spec || reducedMotion()) return;
      const start = performance.now(), dur = 1200;
      const tick = (now) => {
        const t = Math.min(1, (now - start) / dur);
        node.textContent = spec.fmt(spec.to * ease(t));
        if (t < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }

  function show(block) {
    block.classList.add("in");
    count(block);
    pending.forEach((config, id) => { if (block.contains($(id))) makeChart(id, config); });
  }

  function setup() {
    const blocks = [...$("result-body").children];
    if (io) io.disconnect();
    if (reducedMotion() || !("IntersectionObserver" in window)) { blocks.forEach((b) => { b.classList.add("reveal"); show(b); }); return; }
    let lead = 0;
    io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      show(e.target);
    }), { threshold: 0.08, rootMargin: "0px 0px -6% 0px" });
    blocks.forEach((b) => {
      b.classList.remove("in");
      b.classList.add("reveal");
      // the first blocks on screen come in one after another; later ones as you reach them
      b.style.setProperty("--delay", `${Math.min(lead++, 4) * 0.09}s`);
      io.observe(b);
    });
  }

  return { setup, count, pending };
})();

/* ---------------------------------------------------------------- header menu
 * The staggered menu from the BCH Uniforms site (a React Bits design), rebuilt without GSAP
 * with the same timings: coloured layers sweep in, the panel follows, the links rise in a
 * stagger, the footer fades up; the toggle rolls "Menu" to "Close" and its + turns into an x. */
const Menu = (() => {
  const EASE_OUT = "cubic-bezier(.165,.84,.44,1)";     // GSAP power4.out
  const EASE_IN = "cubic-bezier(.55,.055,.675,.19)";   // power3.in
  let open = false, lastFocus = null, resetTimer = null;
  const q = (sel) => document.querySelector(sel);
  const qa = (sel) => [...document.querySelectorAll(sel)];
  const d = (sec) => (reducedMotion() ? 0 : sec);

  function setParts(visible, animate) {
    const labels = qa(".sm-panel-itemLabel"), foot = qa(".sm-foot > *");
    const panelStart = d(0.07 + 0.08), panelDur = d(0.65);
    labels.forEach((el, i) => {
      el.style.transition = animate ? `transform ${d(1)}s ${EASE_OUT} ${panelStart + panelDur * 0.15 + i * d(0.1)}s` : "none";
      el.style.transform = visible ? "none" : "translateY(140%) rotate(10deg)";
    });
    foot.forEach((el, i) => {
      el.style.transition = animate ? `transform ${d(0.55)}s cubic-bezier(.215,.61,.355,1) ${panelStart + panelDur * 0.4 + i * d(0.08)}s, opacity ${d(0.5)}s ease ${panelStart + panelDur * 0.4 + i * d(0.08)}s` : "none";
      el.style.transform = visible ? "none" : "translateY(25px)";
      el.style.opacity = visible ? "1" : "0";
    });
  }

  function slide(visible) {
    const layers = qa(".sm-prelayer"), panel = q(".sm-panel");
    layers.forEach((el, i) => {
      el.style.transition = visible ? `transform ${d(0.5)}s ${EASE_OUT} ${d(i * 0.07)}s` : `transform ${d(0.32)}s ${EASE_IN}`;
      el.style.transform = visible ? "translateX(0)" : "translateX(100%)";
    });
    panel.style.transition = visible ? `transform ${d(0.65)}s ${EASE_OUT} ${d((layers.length - 1) * 0.07 + 0.08)}s` : `transform ${d(0.32)}s ${EASE_IN}`;
    panel.style.transform = visible ? "translateX(0)" : "translateX(100%)";
  }

  function rollText(opening) {
    // Menu -> Close -> Menu -> Close ... ending on the target, scrolled like a ticker
    const inner = q("[data-sm-text]");
    const target = opening ? "Close" : "Menu";
    const seq = [opening ? "Menu" : "Close"];
    for (let i = 0; i < 3; i++) seq.push(seq[seq.length - 1] === "Menu" ? "Close" : "Menu");
    if (seq[seq.length - 1] !== target) seq.push(target);
    seq.push(target);
    inner.innerHTML = seq.map((l) => `<span class="sm-toggle-line">${l}</span>`).join("");
    inner.style.transition = "none";
    inner.style.transform = "translateY(0)";
    void inner.offsetWidth;
    inner.style.transition = `transform ${d(0.5 + seq.length * 0.07)}s ${EASE_OUT}`;
    inner.style.transform = `translateY(-${((seq.length - 1) / seq.length) * 100}%)`;
  }

  function set(target) {
    if (target === open) return;
    open = target;
    const btn = q("#menu-btn"), panel = q(".sm-panel"), wrapper = q("[data-sm-wrapper]");
    wrapper.toggleAttribute("data-open", open);
    panel.setAttribute("aria-hidden", String(!open));
    panel.toggleAttribute("inert", !open);
    btn.setAttribute("aria-expanded", String(open));
    btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    document.documentElement.classList.toggle("sm-lock", open);
    clearTimeout(resetTimer);
    if (open) {
      lastFocus = document.activeElement;
      setParts(false, false);
      void panel.offsetWidth;
      slide(true);
      setParts(true, true);
      panel.querySelector(".sm-panel-item")?.focus({ preventScroll: true });
    } else {
      slide(false);
      resetTimer = setTimeout(() => setParts(false, false), d(0.32) * 1000);
      if (panel.contains(document.activeElement)) btn.focus({ preventScroll: true });
    }
    rollText(open);
  }

  function init() {
    setParts(false, false);
    const btn = q("#menu-btn"), panel = q(".sm-panel");
    btn.addEventListener("click", () => set(!open));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) { set(false); btn.focus(); } });
    document.addEventListener("mousedown", (e) => {
      if (open && !panel.contains(e.target) && !btn.contains(e.target)) set(false);
    });
    $("menu-new").addEventListener("click", () => { set(false); setTimeout(() => $("prompt").focus({ preventScroll: true }), 350); });
    // the header gets a shadow once the page scrolls
    const header = q("[data-header]");
    const onScroll = () => header.toggleAttribute("data-scrolled", scrollY > 8);
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }
  return { init, close: () => set(false) };
})();

/* ---------------------------------------------------------------- startup */
function init() {
  $("docs-link").href = `${API}/docs`;
  const ex = $("examples");
  EXAMPLES.forEach((e) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = e; b.setAttribute("aria-label", `Use example: ${e}`);
    b.onclick = () => { $("prompt").value = e; $("prompt").focus(); };
    ex.appendChild(b);
  });
  Graph.draw();
  window.addEventListener("resize", debounce(() => Graph.draw(), 150));
  renderLegend();
  $("analyze-form").addEventListener("submit", (ev) => { ev.preventDefault(); analyze(collectRequest()); });
  $("real-toggle").addEventListener("change", () => lastResult && renderSimulation(lastResult.simulation));
  $("history-btn").onclick = () => { Menu.close(); openHistory(); };
  $("docs-link").addEventListener("click", () => Menu.close());
  Menu.init();
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
    setChip(chip, (h.offline_mode ? "Offline demo data" : "Live data mode") + llm, h.offline_mode ? "Demo data" : "Live data");
    chip.className = "chip " + (h.offline_mode ? "warn" : "ok");
  } catch {
    setChip(chip, "Backend not reachable. Start uvicorn on :8000", "Offline");
    chip.className = "chip bad";
  }
}
function setChip(chip, long) {
  chip.innerHTML = `<span class="long">${esc(long)}</span>`;
  chip.title = long;
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
  btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>Agents working…';
  $("clarify-card").hidden = true;
  $("trace").innerHTML = "";
  $("trace-total").textContent = "";
  if (lastResult) $("result-body").classList.add("stale");
  else { $("loading").hidden = false; if ($("placeholder")) $("placeholder").hidden = true; }
  const steps = [];
  Graph.start();

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
        if (event === "step") { steps.push(data); Graph.enqueue(data); }
        else if (event === "result") await handleResult(data, steps);
        else if (event === "error") throw new Error(data.detail);
      }
    }
  } catch (e) {
    await Graph.drained();
    Graph.finish();
    const li = document.createElement("li");
    li.className = "error"; li.textContent = `Error: ${e.message}`;
    $("trace").appendChild(li);
    Graph.say(`<b>Something went wrong.</b> ${esc(e.message)}`);
    $("loading").hidden = true;
    if (!lastResult && $("placeholder")) $("placeholder").hidden = false;
  } finally {
    $("result-body").classList.remove("stale");
    btn.disabled = false; btn.textContent = "Build my portfolio";
  }
}

function formatError(err) {
  if (!err || !err.detail) return "";
  if (Array.isArray(err.detail)) return err.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
  return err.detail;
}

async function handleResult(result, steps) {
  await Graph.drained();            // let the graph finish playing the run before the results arrive
  Graph.finish();
  const total = steps.reduce((s, x) => s + x.duration_ms, 0);
  $("trace-total").textContent = `${steps.length} steps · ${(total / 1000).toFixed(1)}s`;
  $("loading").hidden = true;
  if (result.status === "needs_clarification") {
    if (!lastResult && $("placeholder")) $("placeholder").hidden = false;
    Graph.say("<b>Profiling</b> needs a little more from you. Answer below and the run continues.");
    return renderClarify(result);
  }
  Graph.say(`<b>Done.</b> ${steps.length} steps in ${(total / 1000).toFixed(1)}s. Your plan is below.`);
  renderResult(result);
  $("results").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}

/* ---------------------------------------------------------------- trace */
function addTrace(step) {
  const li = document.createElement("li");
  if (step.agent === "validator" && /sending back/.test(step.message)) li.className = "revise";
  if (step.agent === "clarify") li.className = "clarify";
  const flag = li.className === "revise" ? '<span class="t-flag">sent back</span>' : li.className === "clarify" ? '<span class="t-flag">asked you</span>' : "";
  li.innerHTML = `<div class="t-head"><span>${esc(step.label)}${flag}</span><span class="t-time">${step.duration_ms} ms</span></div>
    <div class="t-msg">${esc(step.message)}</div>`;
  const list = $("trace");
  list.querySelector(".empty")?.remove();
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

/* ---------------------------------------------------------------- live agent graph
 * Drawn in SVG from GRAPH_ROWS. Steps stream in as each agent finishes; they are played
 * back at a readable pace (a run on demo data finishes in under a second), so you can see
 * the order: parallel branches, the optimizer/validator loop, the joins. */
const Graph = (() => {
  const nodes = {}, edges = [];
  let counts = {}, active = new Set(), started = false, finished = false;
  let queue = [], playing = false, waiters = [];
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

  function draw() {
    const svg = $("graph");
    const W = Math.max(300, Math.min(560, svg.parentElement.clientWidth - 40));
    const gap = 14, h = 38, rowH = W < 400 ? 62 : 70;
    const w = Math.min(152, (W - 2 * gap) / 3);
    const H = (GRAPH_ROWS.length - 1) * rowH + h + 16;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = "";
    const pos = {};
    GRAPH_ROWS.forEach((row, r) => {
      const g = row.length === 2 ? w * 0.45 : gap;
      const total = row.length * w + (row.length - 1) * g;
      row.forEach(([id], i) => { pos[id] = { x: (W - total) / 2 + i * (w + g), y: 8 + r * rowH }; });
    });
    const edgeLayer = el("g", {}), nodeLayer = el("g", {});
    svg.append(edgeLayer, nodeLayer);
    edges.length = 0;
    GRAPH_EDGES.forEach(([from, to, kind]) => {
      const a = pos[from], b = pos[to];
      let d;
      if (kind === "side") {
        const y = a.y + h / 2 - 7, x1 = a.x + w, x2 = b.x, mx = (x1 + x2) / 2;
        d = `M${x1},${y} Q${mx},${y - 16} ${x2},${y}`;
      } else if (kind === "loop") {
        const y = a.y + h / 2 + 7, x1 = a.x, x2 = b.x + w, mx = (x1 + x2) / 2;
        d = `M${x1},${y} Q${mx},${y + 16} ${x2},${y}`;
      } else {
        const x1 = a.x + w / 2, y1 = a.y + h, x2 = b.x + w / 2, y2 = b.y, my = (y1 + y2) / 2;
        d = `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
      }
      const path = el("path", { d, class: "edge" + (kind === "loop" ? " loop" : "") });
      edgeLayer.append(path);
      edges.push({ from, to, kind, path });
    });
    GRAPH_ROWS.forEach((row, r) => row.forEach(([id, label]) => {
      const p = pos[id];
      const g = el("g", { class: "node", transform: `translate(${p.x},${p.y})`, "data-id": id, style: `--d:${r * 0.25}s` });
      g.append(el("rect", { class: "halo", width: w, height: h, rx: 10 }));
      g.append(el("rect", { width: w, height: h, rx: 10 }));
      const t = el("text", { x: w / 2, y: h / 2 + 4.5, "text-anchor": "middle" }); t.textContent = label;
      const c = el("text", { class: "count", x: w - 9, y: 14, "text-anchor": "end" });
      const title = el("title", {}); title.textContent = label;
      g.append(t, c, title);
      nodeLayer.append(g);
      nodes[id] = g;
    }));
    paint();
  }

  function paint() {
    for (const id in nodes) {
      const n = counts[id] || 0, g = nodes[id];
      g.classList.toggle("idle-pulse", !started);
      g.classList.toggle("done", n === 1);
      g.classList.toggle("looped", n > 1);
      g.classList.toggle("active", active.has(id) && n === 0);
      g.classList.toggle("skipped", finished && n === 0);
      g.querySelector(".count").textContent = n > 1 ? `×${n}` : "";
    }
    edges.forEach((e) => {
      const from = counts[e.from] || 0, to = counts[e.to] || 0;
      const done = e.kind === "loop" ? to > 1 : from > 0 && to > 0;
      e.path.classList.toggle("idle", !started);
      e.path.classList.toggle("done", done);
      e.path.classList.toggle("live", !done && from > 0 && active.has(e.to));
    });
  }

  function apply(step) {
    counts[step.agent] = (counts[step.agent] || 0) + 1;
    active.delete(step.agent);
    if (step.agent === "validator" && /sending back/.test(step.message)) active.add("portfolio_optimization");
    else {
      // light up what runs next: the agent's successors that haven't run yet (Clarify only shows if it happens)
      GRAPH_EDGES.filter(([f, t, kind]) => f === step.agent && kind !== "loop" && t !== "clarify" && !counts[t])
        .forEach(([, t]) => active.add(t));
    }
    paint();
    say(`<b>${esc(step.label)}</b> ${esc(step.message)}`);
    addTrace(step);
  }

  async function play() {
    playing = true;
    while (queue.length) { apply(queue.shift()); await sleep(reducedMotion() ? 0 : 280); }
    playing = false;
    waiters.splice(0).forEach((r) => r());
  }

  function say(html) {
    const t = $("ticker");
    t.innerHTML = html;
    t.classList.remove("swap"); void t.offsetWidth; t.classList.add("swap");
  }

  return {
    draw,
    say,
    start() { counts = {}; active = new Set(["user_profiling"]); started = true; finished = false; queue = []; paint(); say("<b>Profiling</b> is reading your message…"); },
    enqueue(step) { queue.push(step); if (!playing) play(); },
    drained() { return playing || queue.length ? new Promise((r) => waiters.push(r)) : Promise.resolve(); },
    finish() { finished = true; active.clear(); paint(); },
    replay(steps) { counts = {}; active = new Set(); started = true; finished = false; steps.forEach((s) => { counts[s.agent] = (counts[s.agent] || 0) + 1; }); this.finish(); },
  };
})();

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function renderLegend() {
  const flow = [
    ["Profiling", "Reads amount, monthly SIP, age, horizon and risk from your words; asks you if any are missing."],
    ["Market data", "Downloads prices for 15 NSE stocks, gold and the benchmark in one batch, cached in SQLite."],
    ["Fundamentals, Risk, News", "Score each company, measure volatility, VaR, drawdown and beta, and scan headlines for red flags.", "parallel"],
    ["Strategy", "Builds the stock portion with MPT and with HRP on older data and keeps the one that held up better."],
    ["Optimizer and Validator", "Sets the four-jar mix and the stock weights; 9 checks can send it back with fixes, up to 3 times.", "loop"],
    ["Monte Carlo, Backtest", "Simulates 5,000 futures and replays the picks on a year they never saw.", "parallel"],
    ["Explainability", "Writes the plan for a beginner and for an experienced investor."],
  ];
  $("graph-legend").innerHTML = flow.map(([name, what, tag]) =>
    `<li><div class="f-name">${esc(name)}${tag ? ` <span class="tag">${tag}</span>` : ""}</div><div class="f-what">${esc(what)}</div></li>`).join("");
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
  $("placeholder")?.remove();   // its h1 must not compete with the results' h1
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
  renderHoldings(r.optimized_portfolio);
  renderSimulation(r.simulation);
  renderBacktest(r.backtest);
  renderValidator(r.validation, r.validation_history);
  renderStrategy(r.strategy, r.optimized_portfolio);
  renderUniverse(r);
  $("disclaimer").textContent = r.explanation.disclaimer;
  applyView();
  Reveal.setup();
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
  renderAllocation(r.optimized_portfolio, lvl);
  $("alloc-title").textContent = titles.alloc;
  $("explain-title").textContent = titles.explain;
  $("sim-title").textContent = titles.sim;
  $("bt-title").textContent = titles.bt;
  renderNutshell(r.explanation, lvl);
  renderKpis(r, lvl);
  if ($("kpis").classList.contains("in")) Reveal.count($("kpis"));
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
  $("nutshell").textContent = text;
  $("llm-note").hidden = !note;
  $("llm-note").textContent = note;
}

function renderKpis(r, lvl) {
  const opt = r.optimized_portfolio, sim = r.simulation, ex = r.explanation, bl = opt.blended;
  const fv = sim.final_value_percentiles, fr = sim.final_value_percentiles_real;
  const er = opt.equity_sleeve_risk || {};
  // [label, value, subline, count-up spec { to, fmt } for figures that animate]
  const pct0 = (v) => pct(v, 0);
  const items = lvl === "advanced" ? [
    [`P50 at ${sim.horizon_years}y`, inr(fv.p50_median), `real ${inr(fr.p50_median)} · P(loss) ${pct(sim.probability_of_loss)}`, { to: fv.p50_median, fmt: inr }],
    ["E[R] (portfolio)", pct(opt.blended_expected_annual_return), "CAPM equilibrium, annual", { to: opt.blended_expected_annual_return, fmt: (v) => pct(v) }],
    ["σ (portfolio)", pct(opt.blended_annual_volatility), `ρ-adjusted; −${pct(bl.diversification_benefit)} vs ρ=1`, { to: opt.blended_annual_volatility, fmt: (v) => pct(v) }],
    ["Sharpe", num(bl.sharpe_ratio), "rf 6.5%", { to: bl.sharpe_ratio, fmt: (v) => num(v) }],
    ["VaR95 / CVaR95", `${pct(-er.daily_var_95)} / ${pct(-er.daily_cvar_95)}`, "1-day, equity sleeve"],
    ["Max drawdown", pct(er.max_drawdown), `β ${num(er.beta)} vs benchmark`, { to: er.max_drawdown, fmt: (v) => pct(v) }],
    ["Data quality", `${ex.data_quality_score}/100`, md_label(r.market_data), { to: ex.data_quality_score, fmt: (v) => `${Math.round(v)}/100` }],
  ] : [
    [`Typical result in ${sim.horizon_years} years`, inr(fv.p50_median), `≈ ${inr(fr.p50_median)} after inflation`, { to: fv.p50_median, fmt: inr }],
    ["Growth per year", `~${pct(opt.blended_expected_annual_return, 0)}`, "on average, long run", { to: opt.blended_expected_annual_return, fmt: (v) => `~${pct0(v)}` }],
    ["How bumpy", bumpiness(opt.blended_annual_volatility), `most years within ±${pct(opt.blended_annual_volatility, 0)}`],
    ["Chance of losing money", pct(sim.probability_of_loss, 0), `ending below ${inr(sim.total_invested)}`, { to: sim.probability_of_loss, fmt: pct0 }],
  ];
  $("kpis").style.setProperty("--n", items.length);
  $("kpis").classList.toggle("two-rows", items.length > 5);
  $("kpis").innerHTML = items.map(([l, v, s], i) =>
    `<div class="kpi${i === 0 ? " lead" : ""}"><div class="k-label">${esc(l)}</div><div class="k-value">${esc(v)}</div><div class="k-sub">${esc(s)}</div></div>`).join("");
  $("kpis").querySelectorAll(".k-value").forEach((node, i) => { if (items[i][3] && items[i][3].to != null) node._count = items[i][3]; });
}
const bumpiness = (vol) => vol < 0.07 ? "Gentle" : vol < 0.12 ? "Moderate" : vol < 0.17 ? "Bumpy" : "Very bumpy";
const md_label = (md) => md.data_source === "live" ? "all live data" : md.data_source === "mixed" ? `${pct(md.live_ratio, 0)} live` : "synthetic demo data";

function makeChart(id, config) {
  if (charts[id]) charts[id].destroy();
  // If the chart library failed to load, show the rest of the results without this chart.
  if (typeof Chart === "undefined") {
    const box = $(id)?.closest(".chart-box");
    if (box) box.innerHTML = '<p class="muted small">Chart unavailable: the chart library did not load.</p>';
    return;
  }
  // Draw a chart when its card is on screen, so its animation is seen.
  const host = $(id).closest(".reveal");
  if (host && !host.classList.contains("in") && !reducedMotion()) { Reveal.pending.set(id, config); return; }
  Reveal.pending.delete(id);
  const d = Chart.defaults;
  d.animation.duration = reducedMotion() ? 0 : 1100;
  d.animation.easing = "easeOutQuart";
  d.color = css("--muted");
  d.borderColor = css("--grid");
  d.font.family = getComputedStyle(document.body).fontFamily;
  d.font.size = 12;
  d.plugins.legend.align = "start";
  d.plugins.legend.labels.usePointStyle = true;
  d.plugins.legend.labels.pointStyle = "circle";
  d.plugins.legend.labels.boxWidth = 8;
  d.plugins.legend.labels.boxHeight = 8;
  d.plugins.tooltip.backgroundColor = css("--text");
  d.plugins.tooltip.titleColor = css("--surface");
  d.plugins.tooltip.bodyColor = css("--surface");
  d.plugins.tooltip.cornerRadius = 4;
  d.scale.border = { display: false };
  d.scale.grid.drawTicks = false;
  d.scale.ticks.padding = 8;
  d.scale.ticks.maxTicksLimit = 6;
  charts[id] = new Chart($(id), config);
}

function renderAllocation(opt, lvl) {
  // The four jars: the strategic mix applied to the amount being invested. The parts are
  // rounded first and the total is their sum, so the figures on screen always add up.
  const b = opt.asset_class_allocation, investable = opt.execution.investable_amount;
  const jars = [
    ["stocks", "Stocks", lvl === "advanced" ? "equity sleeve" : "grows the most, goes up and down", css("--accent")],
    ["bonds", "Bonds", lvl === "advanced" ? "gilt / short-duration debt" : "steadier, cushions the falls", css("--muted")],
    ["gold", "Gold", lvl === "advanced" ? "gold ETF" : "often holds up when stocks drop", css("--compare")],
    ["cash", "Cash", lvl === "advanced" ? "liquid fund" : "ready money and leftovers", css("--control")],
  ].map(([cls, name, what, color]) => ({ cls, name, what, color, weight: b[cls] || 0, rupees: Math.round((b[cls] || 0) * investable) }));
  const total = jars.reduce((s, j) => s + j.rupees, 0);
  console.assert(Math.abs(total - investable) <= 2, "jars should sum to the investable amount", total, investable);
  $("alloc-total").textContent = `of ${inr(total)}`;
  $("jar-bar").innerHTML = jars.filter((j) => j.weight > 0)
    .map((j, i) => `<span style="flex:${j.weight.toFixed(4)};background:${j.color};--i:${i}"></span>`).join("");
  $("jar-bar").setAttribute("aria-label", jars.map((j) => `${j.name} ${pct(j.weight, 0)}`).join(", "));
  $("jars").innerHTML = jars.map((j) => `<div class="jar">
      <div class="j-name"><i style="background:${j.color}"></i>${j.name}</div>
      <div class="j-pct">${pct(j.weight, 0)}</div>
      <div class="j-amt">${inr(j.rupees)}</div>
      <div class="j-what">${esc(j.what)}</div></div>`).join("");
  const base = opt.base_allocation;
  const notes = [...opt.allocation_adjustments];
  if (!notes.length) notes.push(`Standard ${opt.risk_tolerance} mix (base ${pct(base.stocks, 0)} stocks). No glide-path change needed.`);
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
      rows.push(`<tr${indexMode ? ' class="muted"' : ""}><td>${esc(h.ticker ? short(h.ticker) : "—")}</td><td class="wrap">${esc(h.name)}${indexMode ? " <span class='tag'>later</span>" : ""}</td>
        <td class="num">${pct(h.weight)}</td><td class="num">${inr(h.target_rupees)}</td>
        <td class="num">${h.price != null ? inr(h.price) : "—"}</td><td class="num">${h.shares != null ? h.shares : "—"}</td>
        <td class="num">${inr(indexMode ? 0 : h.invested_rupees)}</td></tr>`);
    });
  });
  $("holdings").innerHTML = rows.join("");
  $("holdings").querySelectorAll("tr").forEach((tr, i) => { tr.classList.add("row-in"); tr.style.setProperty("--i", i); });
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
  const a = css("--accent");
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
        { label: "Amount invested", data: adj(y.invested), borderColor: css("--compare"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, fill: false },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { filter: (i) => !/pct$/.test(i.text) } },
        tooltip: { filter: (i) => !/range/.test(i.dataset.label), callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.raw)}` } },
      },
      scales: { x: { grid: { display: false } }, y: { ticks: { callback: (v) => inr(v) } } },
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
      { label: bt.benchmark.name, data: s.benchmark, borderColor: css("--text"), borderWidth: 1.5, pointRadius: 0 },
      { label: "Equal-weight shortlist", data: s.equal_weight, borderColor: css("--compare"), borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false } }, y: { title: { display: true, text: "Growth of 100" } } } },
  });
  const row = (name, m) => `<tr><td>${esc(name)}</td><td class="num">${pct(m.total_return)}</td><td class="num">${pct(m.annual_volatility)}</td>
    <td class="num">${num(m.sharpe_ratio)}</td><td class="num">${pct(m.max_drawdown)}</td></tr>`;
  $("bt-table").innerHTML = `<tr><th></th><th class="num">Return</th><th class="num">Volatility</th><th class="num">Sharpe</th><th class="num">Max drawdown</th></tr>`
    + row("WhiteFIN", bt.strategy) + row(bt.benchmark.name, bt.benchmark) + row("Equal weight", bt.equal_weight_shortlist);
  $("bt-note").textContent = `Trained ${bt.train_period[0]} to ${bt.train_period[1]}, tested ${bt.test_period[0]} to ${bt.test_period[1]}. ` + bt.caveats.join(" ");
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
  const failed = history.reduce((n, h) => n + h.checks.filter((c) => !c.passed).length, 0);
  let html = `<div class="verdict-line"><span class="verdict ${v.verdict}">${labels[v.verdict]}</span>
    <span class="muted small">after ${v.attempt} attempt${v.attempt > 1 ? "s" : ""}${failed ? `, ${failed} failed check${failed > 1 ? "s" : ""} fixed on the way` : ""}</span></div>`;
  history.forEach((h) => {
    const last = h === history[history.length - 1];
    const shown = last ? h.checks : h.checks.filter((c) => !c.passed);
    html += `<div class="attempt">${history.length > 1 ? `<div class="attempt-title">Attempt ${h.attempt}: ${labels[h.verdict]}</div>` : ""}<div class="checks">`;
    shown.forEach((c) => {
      html += `<div class="check ${c.passed ? "pass" : "fail"}">${icon(c.passed ? "check" : "cross")}
        <span>${esc(c.check)}<span class="detail">${esc(c.detail)}${c.fix ? `. Fix: ${esc(c.fix)}` : ""}</span></span></div>`;
    });
    html += `</div></div>`;
  });
  $("validator").innerHTML = html;
}

function renderExplanation(ex, lvl) {
  const sections = ex.levels ? ex.levels[lvl] : ex.sections;   // older history items have one version
  $("explanation").innerHTML = sections.map((sec) => {
    if (sec.body) return `<section><h3>${esc(sec.title)}</h3><p>${esc(sec.body)}</p></section>`;
    const lead = sec.lead ? `<p class="lead">${esc(sec.lead)}</p>` : "";
    const cards = (sec.cards || []).length ? `<div class="stock-grid">${sec.cards.map((c, i) => stockCard(c, i)).join("")}</div>` : "";
    const points = (sec.points || []).length ? `<ul class="points">${sec.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
    return `<section><h3>${esc(sec.title)}</h3>${lead}${cards}${points}</section>`;
  }).join("");
}

function stockCard(c, i = 0) {
  const metrics = Object.entries(c.metrics || {});
  return `<div class="stock-card" style="--i:${i}">
    <div class="sc-top">
      <div><div class="sc-name">${esc(c.name)}</div><div class="sc-sector">${esc(c.short)} · ${esc(c.sector || "")}</div></div>
      <div class="sc-weight" title="Share of the stock portion">${pct(c.weight_in_sleeve, 0)}</div>
    </div>
    <div class="sc-bar"><span style="width:${Math.min(100, c.weight_in_sleeve * 100 / 0.4).toFixed(0)}%"></span></div>
    <div class="sc-buy">${esc(c.buy)}</div>
    <ul class="sc-list">${c.pros.map((p) => `<li class="pro">${icon("check")}<span>${esc(p)}</span></li>`).join("")}${c.cons.map((p) => `<li class="con">${icon("alert")}<span>${esc(p)}</span></li>`).join("")}</ul>
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
      { label: "Chosen sleeve", data: pts([a.portfolio]), backgroundColor: css("--accent"), borderColor: css("--surface"), pointRadius: 8, borderWidth: 2 },
      { label: "Efficient frontier", data: pts(a.frontier), showLine: true, borderColor: css("--text"), backgroundColor: css("--text"), pointRadius: 0, borderWidth: 1.5 },
      { label: "Individual stocks", data: pts(a.assets), backgroundColor: css("--compare"), pointRadius: 4 },
    ] },
    options: { maintainAspectRatio: false,
      plugins: {
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
      { label: "Share of capital", data: tickers.map((t) => +(w[t] * 100).toFixed(1)), backgroundColor: css("--compare"), borderRadius: 2, barPercentage: 0.8 },
      { label: "Share of risk", data: tickers.map((t) => +((rc[t] || 0) * 100).toFixed(1)), backgroundColor: css("--accent"), borderRadius: 2, barPercentage: 0.8 },
    ] },
    options: { indexAxis: "y", maintainAspectRatio: false,
      plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.raw}%` } } },
      scales: { x: { ticks: { callback: (v) => v + "%" } }, y: { grid: { display: false } } } },
  });
}

function renderCorrelation(a) {
  const { tickers, matrix } = a.correlation;
  const accent = css("--accent"), grey = css("--compare");
  const cell = (v, i, j) => {
    if (i === j) return `<td class="muted">1.00</td>`;
    const strength = Math.min(1, Math.abs(v)) * 0.9;
    return `<td class="${strength > 0.55 ? "dark" : ""}" style="background:${alpha(v >= 0 ? accent : grey, strength)}">${v.toFixed(2)}</td>`;
  };
  $("corr").innerHTML = `<tr><th></th>${tickers.map((t) => `<th>${esc(short(t))}</th>`).join("")}</tr>`
    + matrix.map((row, i) => `<tr><th>${esc(short(tickers[i]))}</th>${row.map((v, j) => cell(v, i, j)).join("")}</tr>`).join("");
  $("corr-note").textContent = `average pairwise ρ ${num(a.average_pairwise_correlation)} · lower is better diversification`;
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
    if (!items.length) { list.innerHTML = `<li class="muted">No analyses yet. Run one and it will be listed here.</li>`; return; }
    list.innerHTML = items.map((h) => {
      const p = h.user_profile;
      return `<li data-id="${h.id}"><div class="h-prompt">${esc(h.prompt)}</div>
        <div class="h-meta">#${h.id} · ${new Date(h.created_at).toLocaleString()} · ${esc(p.risk_tolerance)} · ${p.horizon_years}y · ${inr(p.amount)}</div></li>`;
    }).join("");
    list.querySelectorAll("li[data-id]").forEach((li) => {
      li.onclick = async () => {
        const r = await (await fetch(`${API}/api/history/${li.dataset.id}`)).json();
        $("history-dialog").close();
        $("trace").innerHTML = ""; (r.trace || []).forEach(addTrace); Graph.replay(r.trace || []);
        Graph.say(`<b>Loaded from history.</b> ${(r.trace || []).length} steps.`);
        $("prompt").value = r.user_profile.raw_prompt || li.querySelector(".h-prompt").textContent;
        renderResult(r);
        $("results").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
      };
    });
  } catch {
    list.innerHTML = `<li class="error">Couldn't load history.</li>`;
  }
}

init();
