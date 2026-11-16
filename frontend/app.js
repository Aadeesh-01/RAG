import {
  citedSources, createSSEParser, errorMessage, focusSubgraph, formatLocation, hopLabel, middleTruncate,
  normalizeCitations, renderAnswer, repoOverview, suggestedQuestions, viaLabel, initials, avatarSrc,
} from "./lib.js";
import { currentUser, signIn, signOut } from "./auth.js";

const $ = (selector) => document.querySelector(selector);
const SETTINGS_KEY = "coderag.settings";
const FULL_GRAPH_LIMIT = 120;          // draw the whole graph up front only for small repos

const state = {
  demos: [], demo: null, health: null,
  graphs: new Map(), graph: null, graphMode: "focus",
  retrieved: new Map(),                // chunk_id -> hop for the latest answer
  sids: new Map(),                     // chunk_id -> source number (S1, S2…) in that answer
  cited: null,                         // Set of chunk_ids the answer cites; null until it is written
  answerTurn: null,                    // the turn whose sources the graph shows
  sources: [],                         // that answer's sources (file, lines, chunk_id, id)
  pane: "graph", panel: "chat",        // right pane on wide screens; the one pane on narrow
  paneChosen: false,                   // the visitor picked Code or Call graph themselves
  positions: new Map(),                // repo id -> node id -> {x, y}: where the graph left things
  graphLayout: "free",                 // answer graph: "free" | "rings" (by call distance)
  trailsShown: null,                   // the answer turn whose paths have already flowed
  overviews: new Map(),                // repo id -> repoOverview(graph)
  busy: false, turn: 0, controller: null,
  debug: new URLSearchParams(window.location.search).get("debug") === "1",
  userRepos: [],                       // [{id, name}] the signed-in user's repositories (from the server)
  chats: new Map(),                    // repo id -> {turns, evidence} while another repo is open
};

// what the graph and legend show for one answer; kept per repo while another is open
function evidence() {
  return { retrieved: state.retrieved, sids: state.sids, cited: state.cited, answerTurn: state.answerTurn,
           sources: state.sources };
}
function setEvidence(e = {}) {
  state.retrieved = e.retrieved || new Map();
  state.sids = e.sids || new Map();
  state.cited = e.cited || null;
  state.answerTurn = e.answerTurn || null;
  state.sources = e.sources || [];
}
const settings = loadSettings();

// ── small DOM helpers ───────────────────────────────────────────────────────
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Markdown + sanitizer; if the CDN libraries failed to load, show plain escaped text.
const renderers = window.marked && window.DOMPurify
  ? { marked: (s) => window.marked.parse(s), sanitize: (s) => window.DOMPurify.sanitize(s) }
  : { marked: (s) => `<p>${escapeHtml(s)}</p>`, sanitize: (s) => s };

function banner(message, kind = "") {
  const node = $("#banner");
  node.hidden = !message;
  node.className = `banner ${kind}`;
  node.textContent = message || "";
}

// ── settings (browser storage may be unavailable: private mode etc.) ────────
function loadSettings() {
  const defaults = { mode: "server", provider: "groq", apiKey: "", model: "", topK: "", maxHops: "", includeCallers: false,
                     theme: "light" };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return defaults;
  }
}

function saveSettings() {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch { /* not persisted */ }
}

function llmChoice() {
  if (settings.mode === "none") return { provider: "none" };
  if (settings.mode === "own" && settings.apiKey) {
    return { provider: settings.provider, api_key: settings.apiKey, model: settings.model || undefined };
  }
  return { provider: "server" };
}

/** Paper by default; graphite when chosen, or when "system" and the system is dark. */
function applyTheme(choice = settings.theme) {
  const dark = choice === "dark" || (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const mode = dark ? "dark" : "light";
  if (document.documentElement.dataset.mode === mode) return;
  document.documentElement.dataset.mode = mode;
  drawGraph();                                         // arrowheads read their colour when drawn
}

function setupSettings() {
  const dialog = $("#settings");
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme());
  // the look changes as soon as it's picked; Cancel puts the saved one back
  dialog.querySelectorAll("input[name=theme]").forEach((r) => r.addEventListener("change", () => applyTheme(r.value)));
  const syncOwnKey = () => { $("#own-key-fields").hidden = dialog.querySelector("input[name=llm]:checked").value !== "own"; };
  dialog.querySelectorAll("input[name=llm]").forEach((r) => r.addEventListener("change", syncOwnKey));

  const defaults = () => state.health?.defaults || { top_k: 4, max_hops: 3 };
  const fillRetrieval = (topK, maxHops, includeCallers) => {
    $("#top-k").value = topK === "" ? defaults().top_k : topK;
    $("#max-hops").value = maxHops === "" ? defaults().max_hops : maxHops;
    $("#include-callers").checked = includeCallers;
  };

  $("#settings-button").addEventListener("click", () => {
    dialog.querySelector(`input[name=llm][value=${settings.mode}]`).checked = true;
    dialog.querySelector(`input[name=theme][value=${settings.theme}]`).checked = true;
    $("#provider").value = settings.provider;
    $("#api-key").value = settings.apiKey;
    $("#model").value = settings.model;
    $("#top-k-default").textContent = `Default: ${defaults().top_k}`;
    $("#max-hops-default").textContent = `Default: ${defaults().max_hops}. Higher means more context, slower answers and more tokens.`;
    fillRetrieval(settings.topK, settings.maxHops, settings.includeCallers);
    syncOwnKey();
    dialog.showModal();
  });
  $("#settings-reset").addEventListener("click", () => fillRetrieval("", "", false));

  dialog.addEventListener("close", () => {
    if (dialog.returnValue !== "save") { applyTheme(); return; }
    // values equal to the server default are stored as "" so they follow future defaults
    const own = (value, fallback) => (value === "" || Number(value) === fallback ? "" : value);
    Object.assign(settings, {
      mode: dialog.querySelector("input[name=llm]:checked").value,
      provider: $("#provider").value,
      apiKey: $("#api-key").value.trim(),
      model: $("#model").value.trim(),
      topK: own($("#top-k").value, defaults().top_k),
      maxHops: own($("#max-hops").value, defaults().max_hops),
      includeCallers: $("#include-callers").checked,
      theme: dialog.querySelector("input[name=theme]:checked").value,
    });
    applyTheme();
    saveSettings();
    updateLlmBanner();
  });
}

function updateLlmBanner() {
  if (!state.health) return;
  if (settings.mode === "own" && !settings.apiKey) {
    banner("“My own API key” is selected but no key is set, so the free model will be used.");
  } else if (settings.mode === "server" && !state.health.llm.server) {
    banner("This server has no free model configured. Add your own API key in settings, or use sources only.");
  } else {
    banner("");
  }
}

// ── repositories ────────────────────────────────────────────────────────────
function githubUrl(demo, file, start, end) {
  if (!demo.url || !demo.sha) return null;
  const path = demo.root ? `${demo.root}/${file}` : file;
  return `${demo.url}/blob/${demo.sha}/${path}#L${start}-L${end}`;
}

function renderRepoTabs() {
  const nav = $("#repo-tabs");
  const tab = (repo, removable) => {
    const open = el("button", {
      type: "button", class: "tab-open", title: removable ? repo.name : null,
      "aria-pressed": String(repo.id === state.demo?.id), onclick: () => selectRepo(repo.id),
    }, repo.name);
    if (!removable) return el("span", { class: "tab" }, open);
    // a sibling button, not nested in the tab: both are reachable with Tab
    const close = el("button", {
      type: "button", class: "close", "aria-label": `Remove ${repo.name}`, title: "Remove from this list",
      onclick: () => removeUserRepo(repo.id),
    }, svgIcon("M7 7l10 10M17 7L7 17"));
    return el("span", { class: "tab user" }, open, close);
  };
  nav.replaceChildren(...state.demos.map((d) => tab(d, false)),
                      ...state.userRepos.map((r) => tab(r, true)));
  // on a narrow bar the tab row scrolls: keep the open repo in view
  nav.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  syncTabFades();
}

/** Fade the tab row's edges when more tabs are scrolled out of view. */
function syncTabFades() {
  const nav = $("#repo-tabs");
  const more = nav.scrollWidth - nav.clientWidth;
  nav.classList.toggle("fade-start", more > 1 && nav.scrollLeft > 1);
  nav.classList.toggle("fade-end", more > 1 && nav.scrollLeft < more - 1);
}

function svgIcon(d) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  svg.append(path);
  return svg;
}

// ── the signed-in user's repositories: kept on the server, per GitHub account ──
const OLD_REPOS_KEY = "coderag.repos";     // this browser's list before accounts; no longer used

async function loadUserRepos() {
  try { localStorage.removeItem(OLD_REPOS_KEY); } catch { /* storage blocked */ }
  try {
    const mine = await fetchJSON("/api/repos/mine");
    return mine.map((r) => ({ id: r.id, name: r.name }));
  } catch {
    return [];                 // signed out meanwhile, or the server is unreachable: no tabs
  }
}

/** Show `id` as one of the user's tabs, and keep it on their account. */
function rememberUserRepo(id, name) {
  const known = state.userRepos.some((r) => r.id === id);
  state.userRepos = [{ id, name }, ...state.userRepos.filter((r) => r.id !== id)];
  if (!known && currentUser()) {
    fetch(`/api/repos/mine/${encodeURIComponent(id)}`, { method: "PUT" }).catch(() => {});
  }
}

function removeUserRepo(id) {
  state.userRepos = state.userRepos.filter((r) => r.id !== id);
  if (currentUser()) fetch(`/api/repos/mine/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
  // switching away stores the open chat, so forget it only afterwards
  if (state.demo?.id === id) selectRepo(state.demos[0]?.id).then(() => state.chats.delete(id));
  else { state.chats.delete(id); renderRepoTabs(); }
}

// ── languages: the indexer's (coderag/languages.py) labels and highlight.js names ──
const LANGUAGE_LABELS = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", java: "Java",
  kotlin: "Kotlin", c: "C", cpp: "C++", go: "Go", rust: "Rust" };
const EXTENSION_LANGUAGES = { py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "typescript", java: "java", kt: "kotlin", kts: "kotlin",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", hxx: "cpp", go: "go", rs: "rust" };
const INDEXED_GITHUB_LANGUAGES = new Set(Object.values(LANGUAGE_LABELS));

/** "go", "python" ... for a file path (by extension), or null. */
function languageOfPath(path) {
  return EXTENSION_LANGUAGES[(path || "").split(".").pop().toLowerCase()] || null;
}

/** Language tags say something only when a repository mixes languages, or isn't Python. */
function languageTag(language) {
  if (!language) return null;
  const mixed = Object.keys(state.demo?.languages || {}).length > 1;
  if (!mixed && language === "python") return null;
  return el("span", { class: "lang-tag", "data-language": language, text: LANGUAGE_LABELS[language] || language });
}

const GENERIC_QUESTIONS = [
  "What does this project do, and what are its main modules?",
  "What is the main entry point and what does it call?",
];

/** "3 days ago", "last month", "just now": when a repository was last pushed to. */
function pushedAgo(iso, now = Date.now()) {
  const at = Date.parse(iso || "");
  if (Number.isNaN(at)) return "";
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let value = (at - now) / 1000;
  for (const [size, unit] of [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.345, "week"], [12, "month"], [Infinity, "year"]]) {
    if (Math.abs(value) < size) return unit === "second" ? "just now" : rtf.format(Math.round(value), unit);
    value /= size;
  }
  return "";
}

/**
 * The signed-in user's GitHub repositories above the URL field: search, an
 * All / Private / Public filter, and a listbox driven from the search box
 * (arrows move, Enter adds or opens). Already added repositories open instead.
 */
function setupRepoPicker({ onChange, onAdd, onOpen }) {
  const box = $("#repo-picker");
  const search = $("#repo-search");
  const list = $("#repo-options");
  const status = $("#repo-picker-status");
  const filters = [...box.querySelectorAll("[data-filter]")];
  let repos = null;          // null while loading or unavailable
  let shown = [];
  let filter = "all";
  let selected = null;       // full_name of the chosen row
  let request = 0;           // only the newest load may draw

  const optionId = (i) => `repo-option-${i}`;
  const setStatus = (...children) => status.replaceChildren(...children);

  function sync() {
    const i = shown.findIndex((r) => r.full_name === selected);
    [...list.children].forEach((o, j) => o.setAttribute("aria-selected", String(j === i)));
    for (const host of [search, list]) {
      if (i >= 0) host.setAttribute("aria-activedescendant", optionId(i));
      else host.removeAttribute("aria-activedescendant");
    }
    if (i >= 0) list.children[i].scrollIntoView({ block: "nearest" });
    onChange();
  }

  function choose(fullName) {
    selected = fullName;
    sync();
  }

  function option(r, i) {
    const [owner, name] = r.full_name.split("/");
    const meta = [r.language || "No language", r.pushed_at && `pushed ${pushedAgo(r.pushed_at)}`].filter(Boolean).join(" · ");
    return el("li", {
      id: optionId(i), role: "option", class: `repo-option${r.indexed ? " indexed" : ""}`,
      "aria-selected": String(r.full_name === selected), "data-full-name": r.full_name, title: r.description || null,
      onclick: () => (r.indexed ? onOpen(r) : choose(r.full_name)),
      ondblclick: () => { if (!r.indexed) { choose(r.full_name); onAdd(); } },
    },
    el("span", { class: "repo-option-name" },
      el("span", { class: "owner", text: `${owner}/` }), el("b", { text: name }),
      r.private ? el("span", { class: "private-badge", text: "Private" }) : null),
    el("span", { class: "repo-option-meta" }, meta,
      INDEXED_GITHUB_LANGUAGES.has(r.language) ? null : el("span", { class: "py-hint", text: "few/no source files" })),
    el("span", { class: "repo-option-action", text: r.indexed ? "Open" : "Add" }));
  }

  function render() {
    if (!repos) return;
    const q = search.value.trim().toLowerCase();
    shown = repos.filter((r) => (filter === "all" || (filter === "private") === Boolean(r.private))
                                && (!q || r.full_name.toLowerCase().includes(q)));
    if (!shown.some((r) => r.full_name === selected)) selected = null;
    list.replaceChildren(...shown.map(option));
    const total = `${repos.length.toLocaleString()} ${repos.length === 1 ? "repository" : "repositories"}`;
    if (!repos.length) setStatus("No repositories on your GitHub account yet. Paste a URL below to add someone else's.");
    else if (!shown.length) setStatus(q ? `No repositories match “${search.value.trim()}”.` : `No ${filter} repositories.`);
    else setStatus(shown.length === repos.length ? total : `${shown.length.toLocaleString()} of ${total}`);
    sync();
  }

  function showProblem(...children) {
    repos = null;
    shown = [];
    list.removeAttribute("aria-busy");
    list.replaceChildren();
    setStatus(...children);
    sync();
  }

  async function load() {
    const mine = ++request;
    repos = null;
    shown = [];
    selected = null;
    list.setAttribute("aria-busy", "true");
    list.replaceChildren(...Array.from({ length: 5 }, () =>
      el("li", { class: "repo-option skeleton", "aria-hidden": "true" }, el("span"), el("span"))));
    setStatus("Loading your repositories…");
    sync();
    try {
      const response = await fetch("/api/github/repos", { headers: { Accept: "application/json" } });
      const body = await response.json().catch(() => ({}));
      if (mine !== request) return;
      if (response.status === 401) {
        // the token was revoked or expired (or the session ended): sign in with GitHub again
        showProblem(el("span", { text: "GitHub needs you to sign in again to list your repositories." }),
          el("button", { type: "button", class: "ghost", text: "Reconnect GitHub",
                         onclick: () => signIn(window.location.pathname + window.location.search) }));
        return;
      }
      if (!response.ok || !Array.isArray(body)) throw new Error(body.error || `error ${response.status}`);
      repos = body;
    } catch {
      if (mine !== request) return;
      showProblem(el("span", { text: "Couldn't load your repositories. You can still paste a URL below." }),
        el("button", { type: "button", class: "link-button", text: "Try again", onclick: load }));
      return;
    }
    list.removeAttribute("aria-busy");
    render();
  }

  function setFilter(value, focus = false) {
    filter = value;
    for (const b of filters) {
      const on = b.dataset.filter === value;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    render();
  }

  filters.forEach((b, i) => {
    b.addEventListener("click", () => setFilter(b.dataset.filter));
    b.addEventListener("keydown", (e) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      setFilter(filters[(i + step + filters.length) % filters.length].dataset.filter, true);
    });
  });

  search.addEventListener("input", render);
  const onKey = (e) => {
    if (e.key === "Enter") e.preventDefault();          // never submit an empty pick from the search box
    if (!shown.length) return;
    const i = shown.findIndex((r) => r.full_name === selected);
    const inSearch = e.target === search;
    const to = { ArrowDown: i + 1, ArrowUp: i < 0 ? shown.length - 1 : i - 1,
                 Home: inSearch ? undefined : 0, End: inSearch ? undefined : shown.length - 1 }[e.key];
    if (to !== undefined) {
      e.preventDefault();
      choose(shown[Math.max(0, Math.min(shown.length - 1, to))].full_name);
    } else if (e.key === "Enter" && i >= 0) {
      if (shown[i].indexed) onOpen(shown[i]);
      else onAdd();
    }
  };
  search.addEventListener("keydown", onKey);
  list.addEventListener("keydown", onKey);

  const api = {
    active: false,
    selected: () => repos?.find((r) => r.full_name === selected) || null,
    clearSelection() { if (selected) { selected = null; sync(); } },
    show(on) {
      api.active = on;
      box.hidden = !on;
      if (!on) return;
      search.value = "";
      load();
      setFilter("all");
    },
  };
  return api;
}

function setupAddRepo() {
  const dialog = $("#add-repo");
  const form = $("#add-repo-form");
  const error = $("#add-repo-error");
  const input = $("#repo-url");
  const submit = $("#add-repo-submit");
  const showError = (message) => {
    error.textContent = message || "";
    error.hidden = !message;
    if (message) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  };
  const syncSubmit = () => {
    submit.disabled = picker.active && !input.value.trim() && !picker.selected();
  };
  const openRepo = (repo) => {
    dialog.close();
    rememberUserRepo(repo.repo_id, repo.full_name);
    selectRepo(repo.repo_id);
  };
  const picker = setupRepoPicker({
    onChange: () => {
      if (picker.selected()) { input.value = ""; showError(""); }     // one choice at a time: the row or the URL
      syncSubmit();
    },
    onAdd: () => form.requestSubmit(),
    onOpen: openRepo,
  });
  input.addEventListener("input", () => {
    showError("");
    if (input.value.trim()) picker.clearSelection();
    syncSubmit();
  });
  const open = (url = "") => {
    input.value = url;
    showError("");
    const limits = state.health?.indexing;
    $("#index-limits").textContent = limits
      ? `Limits: ${limits.max_py_files.toLocaleString()} source files / ${limits.max_py_mb} MB; `
        + `${limits.skipped_dirs.join(", ")} folders are skipped.` : "";
    // signed in: pick from your GitHub repositories, or paste any URL below
    const signedIn = Boolean(currentUser());
    picker.show(signedIn);
    input.required = !signedIn;
    $("#repo-url-label").textContent = signedIn ? "Or paste a URL" : "Repository";
    syncSubmit();
    dialog.showModal();
    (signedIn && !url ? $("#repo-search") : input).focus();
  };
  // adding a repository needs an account (it costs the server an index); the demos don't
  $("#add-repo-button").addEventListener("click", () => (currentUser() ? open() : toSignIn()));
  $("#add-repo-cancel").addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const chosen = input.value.trim() ? null : picker.selected();
    if (chosen?.indexed) { openRepo(chosen); return; }
    const url = input.value.trim() || (chosen ? `https://github.com/${chosen.full_name}` : "");
    if (!url) { showError("Pick one of your repositories, or paste a GitHub URL."); return; }
    submit.disabled = true;
    showError("");
    try {
      const response = await fetch("/api/repos", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const job = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(job.error || (job.detail && "Enter a GitHub repository like https://github.com/owner/repo.")
          || `The server could not add it (error ${response.status}). Try again in a moment.`);
      }
      dialog.close();
      trackJob(job);
    } catch (err) {
      showError(err.message);
      (chosen ? $("#repo-search") : input).focus();
    } finally {
      syncSubmit();
    }
  });
  return open;
}

function trackJob(job) {
  const card = el("div", { class: "job", "data-job": job.job_id });
  $("#jobs").append(card);
  const render = (j) => {
    card.className = `job ${j.status}`;
    const waiting = j.status === "queued" && j.queue_position > 1 ? ` (${j.queue_position} in queue)` : "";
    card.replaceChildren(
      el("div", { class: "row" },
        el("span", {}, j.status === "ready" ? "Indexed " : j.status === "failed" ? "Could not index " : "Indexing ",
           el("b", { text: j.name })),
        j.status === "failed" || j.status === "ready"
          ? el("button", { type: "button", class: "link-button", text: "Dismiss", onclick: () => card.remove() }) : null),
      el("div", { class: "msg", text: (j.status === "failed" ? j.error : j.message) + waiting }),
      j.status === "failed" ? null : el("div", { class: "bar" }, el("i", { style: `--progress:${j.progress}` })));
  };
  const finish = (j) => {
    render(j);
    rememberUserRepo(j.repo_id, j.name);
    selectRepo(j.repo_id);
    setTimeout(() => card.remove(), 4000);
  };
  render(job);
  if (job.status === "ready") { finish(job); return; }
  const poll = setInterval(async () => {
    try {
      const j = await fetchJSON(`/api/jobs/${job.job_id}`);
      if (j.status === "ready") { clearInterval(poll); finish(j); }
      else if (j.status === "failed") { clearInterval(poll); render(j); }
      else render(j);
    } catch {
      clearInterval(poll);
      render({ ...job, status: "failed", error: "Lost track of this job (the server may have restarted)." });
    }
  }, 1000);
}

function renderRepoCard(demo) {
  const s = demo.stats || {};
  const overview = state.overviews.get(demo.id);
  const c = overview?.counts;
  const stat = (n, one, many) => el("span", {}, el("b", { text: n }), ` ${n === 1 ? one : many}`);
  const generated = overview ? suggestedQuestions(overview) : [];
  const questions = demo.questions.length ? demo.questions
    : generated.length ? generated : state.graphs.has(demo.id) ? GENERIC_QUESTIONS : [];
  const card = $("#repo-card");
  card.replaceChildren(
    el("h1", {}, demo.url ? el("a", { href: `${demo.url}/tree/${demo.sha}`, target: "_blank", rel: "noopener", text: demo.name }) : demo.name),
    el("p", { class: "description", text: demo.description }),
    el("div", { class: "stats" },
      stat(c?.files ?? s.files ?? 0, "file", "files"),
      c ? stat(c.functions, "function", "functions") : null,
      c ? stat(c.classes, "class", "classes") : null,
      demo.sha ? el("span", {}, "commit ", el("code", { text: demo.sha.slice(0, 7) })) : null,
      Object.keys(demo.languages || {}).length ? el("span", { class: "lang-counts" },
        Object.entries(demo.languages).map(([label, n]) => el("span", {
          class: "lang-count", title: `${n.toLocaleString()} ${label} ${n === 1 ? "file" : "files"}` },
        el("b", { text: n.toLocaleString() }), ` ${label}`))) : null,
      el("button", { type: "button", class: "link-button clear-chat", text: "Clear chat", hidden: true,
        title: "Remove this repository's questions and answers (saved answers are kept)",
        onclick: clearChat })),
    overview ? renderOverview(overview) : null,
    questions.length ? el("section", { class: "suggest" }, el("h2", { text: "Try asking" }),
      el("div", { class: "chips" }, questions.map((q) => el("button", {
        type: "button", class: "chip", text: q, onclick: () => ask(q),
        // a tooltip only when the question is cut off
        onmouseenter: (e) => { const b = e.currentTarget; b.title = b.scrollWidth > b.clientWidth ? q : ""; },
      })))) : null,
  );
  syncRepoCard();
}

async function repoInfo(id) {
  // curated demo by id ("click") or GitHub name ("pallets/click"), else a visitor repo id
  const demo = state.demos.find((d) => d.id === id || d.name === id);
  if (demo) return demo;
  const known = state.userRepos.find((r) => r.id === id || r.name === id);
  try {
    return await fetchJSON(`/api/repos/${encodeURIComponent(known ? known.id : id)}`);
  } catch {
    if (known) {
      removeUserRepo(known.id);
      banner(`${known.name} is no longer indexed on the server. Add it again to ask about it.`);
    }
    return null;
  }
}

async function selectRepo(id) {
  const demo = (id && await repoInfo(id)) || state.demos[0];
  if (!demo) return;
  const conversation = $("#conversation");
  if (state.demo) {
    // keep the conversation we're leaving: its DOM (with listeners) and graph highlight
    state.chats.set(state.demo.id, {
      turns: [...conversation.querySelectorAll(".turn")], evidence: evidence(),
    });
  }
  const chat = state.chats.get(demo.id);
  state.demo = demo;
  setEvidence(chat?.evidence);
  state.graphMode = "focus";
  hideNodeCard();
  if (state.sources.length) showAnswerCode();
  else renderCodeEmpty();
  renderRepoTabs();
  renderRepoCard(demo);
  conversation.replaceChildren($("#repo-card"), ...(chat?.turns || []));
  syncRepoCard();
  syncLegend();
  conversation.scrollTop = chat ? conversation.scrollHeight : 0;
  updateUrl({ repo: demo.id, q: null });

  if (!state.graphs.has(demo.id)) {
    state.graphs.set(demo.id, await fetchJSON(`/api/repos/${demo.id}/graph`).catch(() => null));
  }
  const graph = state.graphs.get(demo.id);
  if (graph && !state.overviews.has(demo.id)) state.overviews.set(demo.id, repoOverview(graph));
  if (state.demo === demo) {
    state.graph = graph;
    renderRepoCard(demo);
    drawGraph();
  }
}

function updateUrl(params) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  history.replaceState(null, "", url);
}

async function fetchJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return response.json();
}

// ── asking ──────────────────────────────────────────────────────────────────
function requestBody(question) {
  const body = { question, include_code: true, llm: llmChoice() };
  if (settings.topK) body.top_k = Number(settings.topK);
  if (settings.maxHops !== "") body.max_hops = Number(settings.maxHops);
  if (settings.includeCallers) body.include_callers = true;
  if (state.debug) body.debug = true;
  return body;
}

function isNearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 80;
}

/** Say something to screen readers through the polite status line. */
function announce(message) {
  const node = $("#announce");
  node.textContent = "";
  // set on the next frame so a repeated message is announced again
  requestAnimationFrame(() => { node.textContent = message; });
}

/** [1, 5] -> "S1 and S5" */
function sourceList(ids) {
  const names = ids.map((n) => `S${n}`);
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
}

/** The Ask button turns into Stop while an answer is being written. */
function setAskBusy(busy) {
  const button = $("#ask-button");
  button.textContent = busy ? "Stop" : "Ask";
  button.classList.toggle("stop", busy);
  button.title = busy ? "Stop writing this answer" : "";
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy || !state.demo) return;
  state.busy = true;
  state.controller = new AbortController();
  setAskBusy(true);
  $("#question").value = "";
  autosize();
  updateUrl({ repo: state.demo.id, q: question });
  const repo = state.demo;               // the answer belongs here even if the visitor switches
  $("#conversation .cleared")?.remove();

  const turnId = ++state.turn;
  const status = el("div", { class: "status" }, el("span", { class: "spinner" }), el("span", { text: "Searching the code…" }));
  const body = el("div", { class: "body" });
  const answer = el("div", { class: "answer" }, status, body);
  const turn = el("article", { class: "turn", "data-turn": turnId }, el("div", { class: "question", text: question }), answer);
  const conversation = $("#conversation");
  conversation.append(turn);
  syncRepoCard();
  turn.scrollIntoView({ behavior: "smooth", block: "start" });

  let text = "";
  let sources = [];
  let frame = 0;
  const paint = () => {
    frame = 0;
    const follow = turn.isConnected && isNearBottom(conversation);
    body.innerHTML = renderAnswer(text, sources.length, renderers);
    if (follow) conversation.scrollTop = conversation.scrollHeight;
  };
  const notice = (message, kind = "") => {
    status.remove();
    answer.insertBefore(el("div", { class: `notice ${kind}`.trim(), text: message }), answer.querySelector(".sources"));
    announce(message);
  };

  try {
    const response = await fetch(`/api/repos/${repo.id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody(question)),
      signal: state.controller.signal,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const wait = response.headers.get("Retry-After");
      throw new Error(response.status === 429
        ? `You've asked a lot of questions. Try again in ${Math.ceil((wait || 60) / 60)} min, or add your own API key in settings.`
        : detail.error
          || (detail.detail && "The question could not be sent. Keep it under 500 characters and try again.")
          || `The server returned an error (${response.status}). Try again in a moment.`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const feed = createSSEParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const event of feed(decoder.decode(value, { stream: true }))) {
        const data = event.data;
        if (event.type === "sources") {
          sources = data.sources;
          const section = renderSources(sources);
          if (data.repo_map) {
            section.querySelector("h2").after(el("p", { class: "context-note",
              text: "This asks about the whole project, so the model also got a map of every area of the repository." }));
          }
          answer.append(section);
          const found = {
            retrieved: new Map(sources.map((s) => [s.chunk_id, s.hop])),
            sids: new Map(sources.map((s) => [s.chunk_id, s.id])),
            cited: null, answerTurn: turn, sources,
          };
          if (state.demo?.id === repo.id) {
            setEvidence(found);
            state.graphMode = "focus";
            hideNodeCard();
            drawGraph();
            renderCodeFiles();
          } else if (state.chats.has(repo.id)) {
            state.chats.get(repo.id).evidence = found;          // visitor switched away
          }
          status.lastChild.textContent = "Writing the answer…";
        } else if (event.type === "token") {
          text += data.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (event.type === "done") {
          cancelAnimationFrame(frame);
          paint();
          status.remove();
          const cited = citedSources(text, sources.length);
          showCited(turn, repo.id, sources, cited);
          if (state.demo?.id === repo.id && state.answerTurn === turn) {
            showAnswerCode({ reveal: isWide() && !state.paneChosen });
          }
          const seconds = (data.llm_ms / 1000).toFixed(1);
          const copy = el("button", { type: "button", class: "link-button", text: "Copy answer",
            onclick: () => copyText(normalizeCitations(text), copy) });
          const save = saveButton({
            repoId: repo.id, repoName: repo.name, question, answer: normalizeCitations(text),
            backend: data.backend, sources: sources.map((s) => slimSource(s, cited.includes(s.id))),
          });
          answer.insertBefore(el("div", { class: "meta" },
            el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: data.backend }),
              data.cached ? "from the cache" : el("span", { class: "measured", text: `${seconds} s` })),
            el("span", { class: "actions" }, copy, save)), answer.querySelector(".sources"));
          announce(cited.length ? `Answer ready. It cites ${sourceList(cited)}.`
            : sources.length ? `Answer ready. It cites none of the ${sources.length} sources.` : "Answer ready.");
        } else if (event.type === "error") {
          notice(errorMessage(data.code, data.message));
        }
      }
    }
  } catch (error) {
    if (error.name === "AbortError") {
      cancelAnimationFrame(frame);
      if (text) paint();
      if (sources.length) showCited(turn, repo.id, sources, citedSources(text, sources.length));
      notice(text ? "Stopped. The answer above is incomplete." : "Stopped before the answer was written.", "stopped");
    } else {
      notice(error.message);
    }
  } finally {
    state.busy = false;
    state.controller = null;
    setAskBusy(false);
    status.remove();
  }
}

/** Light the cited sources: badge, row glyph, graph node and legend. */
function showCited(turn, repoId, sources, cited) {
  const section = turn.querySelector(".sources");
  if (section) groupSources(section, cited);
  const chunks = new Set(sources.filter((s) => cited.includes(s.id)).map((s) => s.chunk_id));
  if (state.demo?.id === repoId) {
    if (state.answerTurn === turn) {
      state.cited = chunks;
      markGraph();
    }
  } else {
    const kept = state.chats.get(repoId)?.evidence;
    if (kept?.answerTurn === turn) kept.cited = chunks;
  }
}

function renderSources(sources, { graph = true } = {}) {
  const rows = sources.map((s) => {
    const details = el("details", { class: "source", "data-sid": s.id, "data-chunk": s.chunk_id,
      "data-file": s.file, "data-start": s.start_line, "data-end": s.end_line },
      el("summary", {},
        // the " " text nodes keep screen-reader names apart; grid layout ignores them
        el("span", { class: "sid", "data-relation": relationOf(s), title: relationWord(s), text: `S${s.id}` }), " ",
        el("span", { class: "what" },
          el("span", { class: "name", text: s.name }), " ",
          languageTag(s.language), " ",
          el("span", { class: "where", title: formatLocation(s),
            text: `${middleTruncate(s.file)}${s.start_line ? `:${s.start_line}-${s.end_line}` : ""}` }),
          // debug mode: how it was retrieved, visible without expanding the row
          s.debug ? el("span", { class: "debug", text: debugLine(s.debug) }) : null), " ",
        // a direct match is the default and says nothing; a graph source names its path
        relationLabel(s) ? el("span", { class: "hop", "data-relation": relationOf(s), "data-confidence": s.confidence || null,
          "data-bridge": s.bridge?.mechanism || null,
          title: s.bridge ? bridgeEvidence(s.bridge)
            : `${viaLabel(s)}. ${hopHelp(s.hop)}${s.confidence === "name_match" ? " Linked by name only, not resolved." : ""}` },
        relationLabel(s)) : null),
      s.code ? codeBlock(s) : null,
      el("div", { class: "source-actions" },
        graph ? el("button", { type: "button", class: "link-button", text: "Show in file",
          onclick: () => showCode(s.file, { start: s.start_line, end: s.end_line, reveal: true }) }) : null,
        s.url ? el("a", { class: "open", href: s.url, target: "_blank", rel: "noopener", text: "Open on GitHub" }) : null,
        el("button", { type: "button", class: "link-button", text: "Copy location",
          onclick: (e) => copyText(`${formatLocation(s)}  ${s.name}`, e.currentTarget) })));
    if (graph) {
      const summary = details.querySelector("summary");
      for (const [on, off] of [["mouseenter", "mouseleave"], ["focus", "blur"]]) {
        summary.addEventListener(on, () => graphView?.highlight(s.chunk_id));
        summary.addEventListener(off, () => graphView?.highlight(null));
      }
      details.addEventListener("toggle", () => {
        if (!details.open) return;
        pulseNode(s.chunk_id);
        // a wide screen already showing code follows the row that was opened
        if (isWide() && state.pane === "code") showCode(s.file, { start: s.start_line, end: s.end_line });
      });
    }
    return details;
  });
  return el("section", { class: "sources" }, el("h2", { text: `Sources (${sources.length})` }), rows);
}

function hopHelp(hop) {
  return hop === 0 ? "Found by searching the code for the question"
    : `Reached by following calls from a direct match, ${hop} ${hop === 1 ? "step" : "steps"} away`;
}

// ── relations: how a source relates to the step that reached it (the server's `relation`) ──
const RELATIONS = ["match", "callee", "caller", "parent", "child", "method"];
const RELATION_WORDS = { match: "Direct match", callee: "Called", caller: "Caller", parent: "Base class", child: "Subclass", method: "Member" };

function relationOf(s) {
  return RELATIONS.includes(s?.relation) ? s.relation : s?.hop ? "callee" : "match";
}

/** The relation as a word, for titles, the node card and screen readers. */
function relationWord(s) {
  return RELATION_WORDS[relationOf(s)];
}

/** "called by main", "calls Session.send", "base class of Bat", "class of Zoo.run": the previous step named. */
// bridge edges (v10): code reached across a language or process boundary
const BRIDGE_LABELS = { http: "HTTP", jni: "JNI", ctypes_cffi: "ctypes/cffi" };

/** "HTTP POST /api/repos (exact)" plus the matched strings and both locations: a bridge's evidence. */
function bridgeEvidence(b) {
  const e = b.evidence || {};
  const matched = (e.matched || []).join("  →  ");
  return [`${b.label} (${b.confidence === "exact" ? "exact match" : "pattern match"})`, matched,
    e.src && e.dst ? `${e.src}  →  ${e.dst}` : ""].filter(Boolean).join("\n");
}

function relationLabel(s) {
  if (!s.hop) return "";
  if (s.bridge) return `via ${s.bridge.label}`;          // "via HTTP POST /api/repos"
  const prev = s.via?.length ? s.via[s.via.length - 1] : "";
  if (!prev) return viaLabel(s);
  return { callee: `called by ${prev}`, caller: `calls ${prev}`, parent: `base class of ${prev}`, child: `subclass of ${prev}`,
    method: s.kind === "class" ? `class of ${prev}` : `method of ${prev}` }[relationOf(s)] || viaLabel(s);
}

function relationOfNode(id) {
  const s = state.sources.find((x) => x.chunk_id === id);
  return s ? relationOf(s) : null;
}

/** Source code with its real line numbers (drawn by CSS, so copying skips them). */
function codeBlock(s) {
  const lines = s.code.replace(/\n$/, "").split("\n");
  return el("pre", {}, el("code", { style: `counter-reset: line ${Math.max((s.start_line || 1) - 1, 0)}` },
    lines.map((line) => el("span", { class: "ln" }, line, "\n"))));
}

/** Once the answer is written: cited sources first, the rest folded under "Also retrieved". */
function groupSources(section, cited) {
  const rows = [...section.querySelectorAll(".source")];
  const isCited = (row) => cited.includes(Number(row.dataset.sid));
  for (const row of rows) {
    row.querySelector(".sid").classList.toggle("cited", isCited(row));
    row.querySelector(".hop .key")?.classList.toggle("key--cited", isCited(row));
  }
  if (!cited.length) return;
  const heading = section.querySelector("h2");
  const citedRows = rows.filter(isCited);
  const rest = rows.filter((row) => !isCited(row));
  heading.textContent = `Cited (${citedRows.length})`;
  section.replaceChildren(heading, ...section.querySelectorAll(":scope > .context-note"), ...citedRows);
  if (rest.length) {
    section.append(el("details", { class: "also" },
      el("summary", {},
        el("span", { class: "also-title", text: `Also retrieved (${rest.length})` }), " ",
        el("span", { class: "also-note", text: "sent to the model as context, not cited" })),
      rest));
  }
}

/** "vector rank 2, similarity 0.51, 1,056 chars" or "should_bypass_proxies → is_valid_cidr (calls:exact), 893 chars" */
function debugLine(d) {
  const size = `${d.context_chars.toLocaleString()} chars in context`;
  if (!d.path) return `vector rank ${d.rank}${d.similarity !== null ? `, similarity ${d.similarity}` : ""}, ${size}`;
  const hops = d.path.map((e) => ` → ${e.to} (${e.edge})`).join("");
  return `${d.seed}${hops}, ${size}`;
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1200);
  } catch {
    button.textContent = "Copy failed";
  }
}

// ── saved answers (kept in this browser across reloads) ─────────────────────
const SAVED_KEY = "coderag.saved";

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(SAVED_KEY) || "[]"); } catch { return []; }
}

function storeSaved(list) {
  try { localStorage.setItem(SAVED_KEY, JSON.stringify(list)); return true; } catch { return false; }
}

function slimSource(s, keepCode) {   // what a saved answer keeps of a source: code only if cited
  const { id, name, kind, file, start_line, end_line, hop, via, relation, url, code, language, bridge } = s;
  return { id, name, kind, file, start_line, end_line, hop, via, relation, url, language, bridge,
           code: keepCode ? code : undefined };
}

function saveButton(record) {
  let savedId = null;
  const button = el("button", { type: "button", class: "link-button save",
    title: "Keep this answer in this browser. Unsaved chats are cleared when the page is closed or reloaded." });
  const sync = () => {
    button.textContent = savedId ? "Saved" : "Save";
    button.setAttribute("aria-pressed", String(Boolean(savedId)));
  };
  button.addEventListener("click", () => {
    const list = loadSaved();
    if (savedId) {
      storeSaved(list.filter((r) => r.id !== savedId));
      savedId = null;
    } else {
      const entry = { ...record, id: `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`,
                      savedAt: new Date().toISOString() };
      if (!storeSaved([entry, ...list])) {
        button.textContent = "Couldn't save (browser storage is full or blocked)";
        return;
      }
      savedId = entry.id;
    }
    sync();
    updateSavedCount();
  });
  sync();
  return button;
}

function updateSavedCount() {
  const n = loadSaved().length;
  const badge = $("#saved-count");
  badge.textContent = n ? String(n) : "";
  badge.hidden = !n;
}

function setupSaved() {
  const dialog = $("#saved");
  $("#saved-button").addEventListener("click", () => { renderSaved(); dialog.showModal(); });
  $("#saved-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => {           // citations jump to the source in the same item
    const cite = e.target.closest("a.cite");
    if (!cite) return;
    e.preventDefault();
    const row = cite.closest(".saved-item")?.querySelector(`.source[data-sid="${cite.dataset.source}"]`);
    if (!row) return;
    const group = row.closest("details.also");
    if (group) group.open = true;
    row.open = true;
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1200);
  });
  updateSavedCount();
}

function renderSaved() {
  const list = loadSaved();
  const box = $("#saved-list");
  if (!list.length) {
    box.replaceChildren(el("p", { class: "note",
      text: "No saved answers yet. Use Save under an answer to keep it after the page is closed." }));
    return;
  }
  box.replaceChildren(...list.map((r) => {
    const body = el("div", { class: "body" });
    body.innerHTML = renderAnswer(r.answer, r.sources.length, renderers);
    const copy = el("button", { type: "button", class: "link-button", text: "Copy answer",
      onclick: () => copyText(r.answer, copy) });
    const sources = renderSources(r.sources, { graph: false });
    groupSources(sources, citedSources(r.answer, r.sources.length));
    return el("details", { class: "saved-item" },
      el("summary", {},
        el("span", { class: "q", text: r.question }),
        el("span", { class: "where" }, el("span", { text: r.repoName }),
          el("time", { datetime: r.savedAt, text: `Saved ${new Date(r.savedAt).toLocaleDateString()}` }))),
      el("div", { class: "answer" },
        body,
        el("div", { class: "meta" },
          el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: r.backend })),
          el("span", { class: "actions" },
            el("button", { type: "button", class: "link-button", text: "Open repo",
              onclick: () => openSavedRepo(r) }), copy,
            el("button", { type: "button", class: "link-button danger", text: "Delete",
              onclick: () => { storeSaved(loadSaved().filter((x) => x.id !== r.id)); updateSavedCount(); renderSaved(); } }))),
        sources));
  }));
}

async function openSavedRepo(record) {
  $("#saved").close();
  const info = await repoInfo(record.repoId);
  if (!info) {
    banner(`${record.repoName} is no longer indexed on the server. Add it again to ask new questions.`);
    return;
  }
  if (info.kind === "user") rememberUserRepo(info.id, info.name);
  await selectRepo(info.id);
}

/** Compact the repo header once there is a conversation; suggestions only when empty. */
function syncRepoCard() {
  const hasTurns = Boolean($("#conversation .turn"));
  const card = $("#repo-card");
  card.classList.toggle("compact", hasTurns);
  card.querySelector(".clear-chat")?.toggleAttribute("hidden", !hasTurns);
}

function clearChat() {
  const conversation = $("#conversation");
  const turns = [...conversation.querySelectorAll(".turn")];
  if (!turns.length) return;
  const repoId = state.demo?.id;
  const kept = evidence();
  turns.forEach((t) => t.remove());
  state.chats.delete(repoId);
  setEvidence();
  hideNodeCard();
  drawGraph();
  renderCodeEmpty();
  syncRepoCard();
  conversation.querySelector(".cleared")?.remove();
  const note = el("p", { class: "cleared" }, "Chat cleared.",
    el("button", { type: "button", class: "link-button", text: "Undo", onclick: () => {
      note.remove();
      if (state.demo?.id !== repoId || conversation.querySelector(".turn")) return;
      conversation.append(...turns);
      setEvidence(kept);
      drawGraph();
      showAnswerCode();
      syncRepoCard();
      $("#question").focus();
    } }));
  $("#repo-card").after(note);
  note.querySelector("button").focus();        // the Clear chat button is gone: keep focus nearby
  setTimeout(() => note.remove(), 12000);
}

function openSource(turn, n, { panel = true, code: revealCode = false } = {}) {
  const row = turn?.querySelector(`.source[data-sid="${n}"]`);
  if (!row) return;
  if (revealCode && isWide()) {
    showCode(row.dataset.file, { start: Number(row.dataset.start), end: Number(row.dataset.end), reveal: true });
  }
  const group = row.closest("details.also");
  if (group) group.open = true;
  if (panel) showPanel("chat");
  row.open = true;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 1200);
  pulseNode(Number(row.dataset.chunk));
}

// ── graph ───────────────────────────────────────────────────────────────────
let graphView = null;


/** Legend counts for the answer the graph shows (empty when there is none). */
function syncLegend() {
  const n = state.retrieved.size;
  const cited = state.cited ? state.cited.size : 0;
  $("#count-cited").textContent = state.cited ? `(${cited})` : "";
  $("#count-retrieved").textContent = n ? `(${n - cited})` : "";
  // only keys that mean something now: no "(0)", no answer keys before an answer
  $("#legend-cited").hidden = !n || (state.cited !== null && cited === 0);
  $("#legend-retrieved").hidden = !n || n - cited === 0;
  $("#legend-neighbour").hidden = !n;
  $("#legend-inherits").hidden = !(state.graph?.links || []).some((l) => l.type === "inherits");
  nameMatchLegend().hidden = !(state.graph?.links || []).some((l) => l.confidence === "name_match");
  const mechanisms = [...new Set((state.graph?.links || []).filter((l) => l.type === "bridge").map((l) => l.mechanism))];
  const bridges = bridgeLegend();
  bridges.hidden = !mechanisms.length;
  bridges.querySelector("span").textContent = `Crosses language: ${mechanisms.map((m) => BRIDGE_LABELS[m] || m).join(" / ")}`;
  $("#legend-trail").hidden = !n || !state.sources.some((s) => s.via_ids?.length);
  // relation keys: only the relations this answer has, each a toggle for its nodes
  const present = new Set(state.sources.map(relationOf));
  document.querySelectorAll(".legend-relation").forEach((li) => { li.hidden = !n || !present.has(li.dataset.relation); });
}

/** The legend key for calls linked by name only (other languages, older indexes), added once. */
function nameMatchLegend() {
  let item = $("#legend-name-match");
  if (!item) {
    item = el("li", { id: "legend-name-match", title: "Calls linked by matching names, without resolving imports or scope" },
      el("i", { class: "key-line key-line--name-match", "aria-hidden": "true" }), "Matched by name");
    $("#legend-inherits").after(item);
  }
  return item;
}

/** The legend key for bridge edges ("Crosses language: HTTP / JNI"), added once. */
function bridgeLegend() {
  let item = $("#legend-bridge");
  if (!item) {
    item = el("li", { id: "legend-bridge", title: "Edges across a language or process boundary: an HTTP call to its route, "
      + "a JNI declaration to its C function, a ctypes/cffi call to its C function. Point at one to see its evidence." },
    el("i", { class: "key-line key-line--bridge", "aria-hidden": "true" }), el("span"));
    nameMatchLegend().after(item);
  }
  return item;
}

/** Relations the visitor hid from the legend (kept across answers). */
const hiddenRelations = new Set();
function applyRelationToggles() {
  document.querySelectorAll(".legend-relation button").forEach((b) => {
    b.setAttribute("aria-pressed", String(!hiddenRelations.has(b.closest("li").dataset.relation)));
  });
  graphView?.node.classed("off", (n) => hiddenRelations.has(relationOfNode(n.id)));
}

function nodeClass(n) {
  if (!state.retrieved.has(n.id)) return state.retrieved.size ? "node neighbour" : "node";
  if (state.cited?.has(n.id)) return "node retrieved cited";
  return state.cited?.size ? "node retrieved dimmed" : "node retrieved";
}

function nodeStatus(n) {
  const hop = state.retrieved.get(n.id);
  if (hop === undefined) return state.retrieved.size ? "Neighbour, not sent to the model" : "";
  const use = state.cited?.has(n.id) ? "Cited" : state.cited ? "Retrieved, not cited" : "Retrieved";
  const s = state.sources.find((x) => x.chunk_id === n.id);
  const how = s && s.hop ? relationLabel(s) : hopLabel(hop).toLowerCase();
  return `${use}, ${how}`;
}

function nodeName(n) {
  const sid = state.sids.get(n.id);
  return [sid ? `S${sid} ${n.name}` : n.name, nodeStatus(n).toLowerCase(), formatLocation(n)].filter(Boolean).join(", ");
}

/** Re-key the drawn nodes without a new layout (the answer finished, so we now know what it cites). */
function markGraph() {
  syncLegend();
  if (!graphView) return;
  graphView.node.attr("class", graphView.cls || nodeClass).attr("aria-label", nodeName);
  // the answer is written: if its graph is on screen, its paths flow once. Checked after this
  // frame, because a wide screen may be switching to the code pane; then they flow when the
  // graph is next shown (drawGraph plays them)
  if (state.cited && state.trailsShown !== state.answerTurn) {
    requestAnimationFrame(() => {
      if (!graphView || state.trailsShown === state.answerTurn || !$("#graph").getBoundingClientRect().width) return;
      state.trailsShown = state.answerTurn;
      graphView.playTrails();
    });
  }
}

// ── the repo header's overview: packages, most called, entry points ───────
function renderOverview(o) {
  const codeButton = (n, text, note, extra = "") => el("li", {},
    el("button", { type: "button", class: `fact ${extra}`.trim(), title: formatLocation(n),
      onclick: () => showCode(n.file, { start: n.start_line, end: n.end_line, reveal: true }) },
      el("code", { text })),
    note ? el("span", { class: "note", text: note }) : null);
  const group = (title, items) => el("section", { class: "facts" }, el("h2", { text: title }), el("ul", {}, items));
  const label = (n) => n.label || n.name;
  const groups = [];
  if (o.packages.length > 1) {
    groups.push(group("Packages", o.packages.map((p) => el("li", {},
      el("button", { type: "button", class: "fact", onclick: () => ask(p.name === "top level"
        ? "What do the files at the top level of this repository do?"
        : `What does the ${p.name.replace(/\/$/, "")} part of the code do?`) }, el("code", { text: p.name })),
      el("span", { class: "note", text: `${p.functions} functions` })))));
  }
  if (o.mostCalled.length) {
    groups.push(group("Most called", o.mostCalled.map((x) => codeButton(x.node, label(x.node), `called by ${x.callers}`))));
  }
  if (o.entryPoints.length) {
    groups.push(group("Entry points", o.entryPoints.map((n) => codeButton(n, n.name, middleTruncate(n.file, 34)))));
  }
  if (o.hierarchy.length >= 3 || (o.hierarchy.length === 2 && groups.length < 3)) {
    const leaf = o.hierarchy.at(-1);
    groups.push(group("Deepest hierarchy", [codeButton(leaf, o.hierarchy.map((n) => n.name).join(" → "), null, "wrap")]));
  }
  return groups.length ? el("div", { class: "overview" }, groups) : null;
}

// ── the graph engine: one live force layout that remembers where things were ─
//   continuity: a new view starts from the old positions and glides there;
//   structure: files as soft outlines, hubs larger, curved calls;
//   evidence: the paths that reached the answer's sources, flowing once.
const MOTION_LIMIT = 600;                 // nodes: above this, views change without movement
const HULL_LIMIT = 150;                   // nodes: above this, no file outlines
const RING_RADII = [80, 185, 290, 395];   // By distance: direct match, 1 call, 2+ calls, neighbours
const RING_NAMES = ["direct match", "1 call away", "2+ calls away", "neighbours"];
const MORPH_MS = 560;                     // the authored moment: one view becoming the next
const calm = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const jitter = (id, salt) => ((((id + 1) * (salt ? 7919 : 104729)) % 97) / 97) - 0.5;
const nodeId = (end) => (typeof end === "object" ? end.id : end);
const pairKey = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);

function positionsFor(repoId) {
  if (!state.positions.has(repoId)) state.positions.set(repoId, new Map());
  return state.positions.get(repoId);
}

/** Edges on the call-graph paths that reached the answer's sources: key -> {from, to}. */
function trailEdges() {
  const edges = new Map();
  for (const s of state.sources) {
    if (!s.via_ids?.length) continue;
    const path = [...s.via_ids, s.chunk_id];
    for (let i = 0; i + 1 < path.length; i++) edges.set(pairKey(path[i], path[i + 1]), { from: path[i], to: path[i + 1] });
  }
  return edges;
}

/** The path that reached one node: its node ids and edge keys (empty for a direct match). */
function trailOf(id) {
  const s = state.sources.find((x) => x.chunk_id === id);
  const path = s?.via_ids?.length ? [...s.via_ids, id] : [];
  const keys = new Set();
  for (let i = 0; i + 1 < path.length; i++) keys.add(pairKey(path[i], path[i + 1]));
  return { ids: new Set(path), keys };
}

/** Pull nodes of the same file towards each other, so files read as regions. */
function forceCluster(strength) {
  let nodes = [];
  const force = (alpha) => {
    const centres = new Map();
    for (const n of nodes) {
      const c = centres.get(n.file) || centres.set(n.file, { x: 0, y: 0, k: 0 }).get(n.file);
      c.x += n.x; c.y += n.y; c.k += 1;
    }
    for (const n of nodes) {
      const c = centres.get(n.file);
      if (c.k < 2) continue;
      n.vx += (c.x / c.k - n.x) * strength * alpha;
      n.vy += (c.y / c.k - n.y) * strength * alpha;
    }
  };
  force.initialize = (ns) => { nodes = ns; };
  return force;
}

function syncLayoutButtons() {
  document.querySelectorAll("#graph-layout button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layout === state.graphLayout)));
}

function drawGraph() {
  syncLegend();
  const svg = window.d3 && d3.select("#graph");
  const hint = $("#graph-hint");
  const toggle = $("#graph-toggle");
  if (!svg) { hint.textContent = "The graph library could not be loaded."; return; }
  graphView?.stop();
  graphView = null;
  const graph = state.graph;
  const answer = state.retrieved.size > 0 && state.graphMode !== "full";
  $("#graph-layout").hidden = !answer;
  syncLayoutButtons();
  if (!graph) { svg.selectAll("g.scene").remove(); hint.textContent = "No graph for this repository."; toggle.hidden = true; return; }

  const small = graph.nodes.length <= FULL_GRAPH_LIMIT;
  let data;
  if (state.graphMode === "full" || (small && !state.retrieved.size)) data = graph;
  else if (state.retrieved.size) data = focusSubgraph(graph, state.retrieved);
  else data = null;

  toggle.hidden = small && !state.retrieved.size;
  toggle.textContent = state.graphMode === "full"
    ? (state.retrieved.size ? "Show answer subgraph" : "Hide full graph")
    : `Show full graph (${graph.nodes.length} nodes)`;
  const caption = data && !state.retrieved.size && state.graphMode !== "full";
  hint.classList.toggle("caption", Boolean(caption));
  hint.textContent = !data || caption
    ? "Ask a question to light up the code it uses. Point at a node to see its calls." : "";
  if (!data) { svg.selectAll("g.scene").remove(); return; }

  const { width, height } = svg.node().getBoundingClientRect();
  if (!width || !height) return;                       // hidden pane: drawn when shown, from where it was

  const rings = answer && state.graphLayout === "rings";
  const positions = positionsFor(state.demo?.id || "_");
  const nodes = data.nodes.map((n) => ({ ...n }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = data.links.map((l) => ({ ...l }));
  const big = nodes.length > 250;
  const moving = !calm() && nodes.length <= MOTION_LIMIT;
  const outlined = !rings && nodes.length <= HULL_LIMIT;
  const trails = state.retrieved.size ? trailEdges() : new Map();

  // hubs read larger; the answer's code is always the largest
  const degree = new Map();
  const adjacency = new Map(nodes.map((n) => [n.id, new Set()]));   // any link: where new nodes start
  const related = new Map(nodes.map((n) => [n.id, new Set()]));     // calls and inheritance: what pointing lights
  for (const l of links) {
    adjacency.get(l.source)?.add(l.target);
    adjacency.get(l.target)?.add(l.source);
    if (l.type === "member") continue;
    related.get(l.source)?.add(l.target);
    related.get(l.target)?.add(l.source);
    degree.set(l.source, (degree.get(l.source) || 0) + 1);
    degree.set(l.target, (degree.get(l.target) || 0) + 1);
  }
  const radius = (n) => (state.retrieved.has(n.id)
    ? 7 + (n.kind === "class" ? 2 : 0)
    : 3 + Math.min(Math.sqrt(degree.get(n.id) || 0) * 0.9, 4) + (n.kind === "class" ? 1.5 : 0));
  // label everything only in small views; otherwise just the retrieved code
  const labelled = (n) => state.retrieved.has(n.id) || nodes.length <= (state.retrieved.size ? 25 : 60);
  const labelText = (n) => n.label || n.name;          // shortest unique qualified name
  const sidText = (n) => (state.sids.has(n.id) ? `S${state.sids.get(n.id)} ` : "");
  const labelWidth = (n) => (labelled(n) ? (sidText(n) + labelText(n)).length * 7.4 + 12 : 0);
  const ringRadius = (n) => {
    const hop = state.retrieved.get(n.id);
    return RING_RADII[hop === undefined ? 3 : Math.min(hop, 2)];
  };

  // start where things were; new nodes start beside a neighbour that was already there
  for (const n of nodes) {
    const p = positions.get(n.id);
    if (p) { n.x = p.x; n.y = p.y; }
  }
  for (const n of nodes) {
    if (n.x !== undefined) continue;
    const near = [...adjacency.get(n.id)].map((id) => byId.get(id)).find((m) => m && positions.has(m.id));
    if (near) { n.x = near.x + jitter(n.id, 0) * 40; n.y = near.y + jitter(n.id, 1) * 40; n.born = true; }
  }
  const start = new Map(nodes.filter((n) => n.x !== undefined && !Number.isNaN(n.x)).map((n) => [n.id, { x: n.x, y: n.y }]));

  // with no answer yet, every node is the subject: all labels keep 12px, none hide
  const overview = !state.retrieved.size;
  const el = svg.node();
  el.classList.toggle("overview", overview);
  el.classList.remove("hovering");
  const subject = (n) => overview || state.retrieved.has(n.id);

  // arrowheads coloured by CSS, so a theme change needs no redraw
  if (svg.select("defs").empty()) {
    const defs = svg.append("defs");
    for (const id of ["arrow", "arrow-lit", "arrow-trail"]) {
      defs.append("marker").attr("id", id).attr("viewBox", "0 -4 8 8").attr("refX", 8)
        .attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto")
        .append("path").attr("d", "M0,-4L8,0L0,4");
    }
  }
  // the old scene steps back while the new one takes its place
  const old = svg.selectAll("g.scene").attr("aria-hidden", "true").style("pointer-events", "none");
  if (moving && !old.empty()) old.transition().duration(200).style("opacity", 0).remove();
  else old.remove();
  // never start from a camera a hidden pane left broken
  if (!Number.isFinite(d3.zoomTransform(el).k)) el.__zoom = d3.zoomIdentity;
  const root = svg.append("g").attr("class", "scene").attr("transform", d3.zoomTransform(el));
  const ringLayer = root.append("g").attr("class", "rings").attr("aria-hidden", "true");
  const hullLayer = root.append("g").attr("class", "hulls").attr("aria-hidden", "true");
  const linkLayer = root.append("g").attr("class", "links").attr("aria-hidden", "true");
  const trailLayer = root.append("g").attr("class", "trails").attr("aria-hidden", "true");
  const nodeLayer = root.append("g").attr("class", "nodes");

  // the fit never zooms out past MIN_FIT, where the answer's labels still render at 12px;
  // further out, only by hand, they shrink (and other labels hide below ~11.5px)
  const MIN_FIT = 0.6;
  const labelScale = (k) => Math.min(1 / MIN_FIT, Math.max(1, 1 / k));
  // the extent is fixed at draw time: a camera move that starts after the pane is hidden
  // (a wide screen switching to code) must not read a zero-size box and divide by it
  const zoom = d3.zoom().extent([[0, 0], [width, height]]).scaleExtent([0.1, 4]).on("zoom", (e) => {
    root.attr("transform", e.transform);
    el.style.setProperty("--label-scale", String(labelScale(e.transform.k)));
    el.style.setProperty("--label-scale-all", String(Math.max(1, 1 / e.transform.k)));
    // zoomed out, only the answer's own labels stay; the rest would be unreadably small
    el.classList.toggle("zoomed-out", e.transform.k < 0.96);
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("dblclick.fit", (e) => { if (e.target === el) fit(true); });   // double-click the background: fit

  const keyOf = (l) => pairKey(nodeId(l.source), nodeId(l.target));
  const onTrail = (l) => trails.has(keyOf(l));
  // confidence comes from the full graph: a link matched by name (not resolved) is drawn dashed
  const confidence = new Map(graph.links.map((l) => [`${nodeId(l.source)}>${nodeId(l.target)}:${l.type}`, l.confidence]));
  const nameMatched = (l) => (l.confidence || confidence.get(`${nodeId(l.source)}>${nodeId(l.target)}:${l.type}`)) === "name_match";
  const bridgeOf = new Map(graph.links.filter((l) => l.type === "bridge")
    .map((l) => [`${nodeId(l.source)}>${nodeId(l.target)}`, l]));
  const link = linkLayer.selectAll("path").data(links).join("path")
    .attr("class", (l) => `link ${l.type}${onTrail(l) ? " trail" : ""}${nameMatched(l) ? " name-match" : ""}`)
    .attr("data-mechanism", (l) => (l.type === "bridge" ? l.mechanism || bridgeOf.get(`${nodeId(l.source)}>${nodeId(l.target)}`)?.mechanism : null))
    // a path edge takes the relation of the source it leads to
    .attr("data-relation", (l) => (onTrail(l) ? relationOfNode(trails.get(keyOf(l)).to) : null));
  // pointing at a bridge shows its evidence: the matched strings and both file:line locations
  link.filter((l) => l.type === "bridge").append("title").text((l) => {
    const b = l.evidence ? l : bridgeOf.get(`${nodeId(l.source)}>${nodeId(l.target)}`);
    return b ? bridgeEvidence(b) : "Crosses a language boundary";
  });
  const trailLinks = links.filter(onTrail);
  const flow = trailLayer.selectAll("path").data(trailLinks).join("path").attr("class", "trail-flow")
    // the dots travel the way retrieval walked, whichever way the call points
    .attr("data-dir", (l) => (nodeId(l.source) === trails.get(keyOf(l)).from ? "fwd" : "rev"));

  // keyboard order: the answer's sources by number, then everything else by name
  const order = [...nodes].sort((a, b) => (state.sids.get(a.id) ?? Infinity) - (state.sids.get(b.id) ?? Infinity)
    || labelText(a).localeCompare(labelText(b)));
  let timer = null;
  const node = nodeLayer.selectAll("g").data(nodes).join("g")
    .attr("class", nodeClass)
    .attr("data-relation", (n) => relationOfNode(n.id))
    .attr("data-language", (n) => n.language || null)
    .classed("off", (n) => hiddenRelations.has(relationOfNode(n.id)))
    .attr("role", "button")
    .attr("aria-label", nodeName)
    .attr("tabindex", (n) => (n === order[0] ? 0 : -1))
    .call(d3.drag()
      .on("start", (e, d) => { if (timer) finish(); if (!e.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on("click", (e, d) => { focusNode(d, false); activateNode(d); })
    .on("dblclick", (e, d) => { e.stopPropagation(); zoomTo([d.id, ...related.get(d.id)]); })
    .on("mouseenter.lit focus.lit", (e, d) => highlight(d.id))
    .on("mouseleave.lit blur.lit", () => highlight(null))
    .on("keydown", (e, d) => {
      const i = order.indexOf(d);
      const target = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: order.length - 1 }[e.key];
      if (target !== undefined) {
        e.preventDefault();
        focusNode(order[(target + order.length) % order.length], true);
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activateNode(d);
      } else if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(1.5); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); zoomBy(1 / 1.5); }
      else if (e.key === "0") { e.preventDefault(); fit(true); }
    });
  node.append("circle").attr("class", "ring").attr("r", (n) => radius(n) + 4);
  node.append("circle").attr("class", "dot").attr("r", radius);
  node.append("title").text((n) => `${n.name}\n${formatLocation(n)}${n.language ? ` · ${LANGUAGE_LABELS[n.language] || n.language}` : ""}`);
  const label = node.filter(labelled).append("text").attr("class", "label").attr("text-anchor", "middle")
    .attr("dominant-baseline", "hanging").attr("y", (n) => radius(n) + 4);
  label.filter((n) => state.sids.has(n.id)).append("tspan").attr("class", "label-sid").text(sidText);
  label.append("tspan").text(labelText);
  // every other node keeps a quiet label, shown while it or a neighbour is pointed at
  node.filter((n) => !labelled(n)).append("text").attr("class", "label quiet").attr("text-anchor", "middle")
    .attr("dominant-baseline", "hanging").attr("y", (n) => radius(n) + 4).text(labelText);

  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id).distance((l) => (l.type === "member" ? 28 : 70)).strength(rings ? 0.06 : 0.5))
    .force("charge", d3.forceManyBody().strength(big ? -35 : rings ? -150 : state.retrieved.size ? -260 : -170))
    .force("center", rings ? null : d3.forceCenter(width / 2, height / 2))
    // gentle gravity keeps disconnected parts of the graph from drifting apart
    .force("x", rings ? null : d3.forceX(width / 2).strength(big ? 0.12 : 0.07))
    .force("y", rings ? null : d3.forceY(height / 2).strength(big ? 0.12 : 0.07))
    // the closer to the centre, the more directly the code was found
    .force("ring", rings ? d3.forceRadial(ringRadius, width / 2, height / 2).strength(0.85) : null)
    .force("cluster", outlined ? forceCluster(0.3) : null)
    // labels take room too: keep labelled nodes further apart
    .force("collide", d3.forceCollide().radius((n) => Math.min(Math.max(radius(n), labelWidth(n) / 2), 120) + 6))
    .stop();
  for (let i = 0; i < 300; i++) sim.tick();           // lay out before the first paint

  // rings: faint guides, named where they cross the top
  if (rings) {
    const used = [...new Set(nodes.map((n) => RING_RADII.indexOf(ringRadius(n))))].sort();
    const guide = ringLayer.selectAll("g").data(used).join("g");
    guide.append("circle").attr("cx", width / 2).attr("cy", height / 2).attr("r", (i) => RING_RADII[i]);
    guide.append("text").attr("x", width / 2).attr("y", (i) => height / 2 - RING_RADII[i] - 6).text((i) => RING_NAMES[i]);
  }
  // files as soft outlines, named at their top edge
  const groups = outlined ? d3.groups(nodes, (n) => n.file).filter(([, ns]) => ns.length > 1) : [];
  const hull = hullLayer.selectAll("g").data(groups).join("g").attr("class", "hull");
  hull.append("path");
  hull.append("text").text(([file]) => file.split("/").pop());
  hull.append("title").text(([file]) => file);
  const hullLine = d3.line().curve(d3.curveCatmullRomClosed.alpha(0.5));
  const hullShape = (ns) => {
    const pts = [];
    for (const n of ns) {
      const r = radius(n) + 12;
      for (let a = 0; a < 8; a++) pts.push([n.x + r * Math.cos((a * Math.PI) / 4), n.y + r * Math.sin((a * Math.PI) / 4)]);
    }
    const shape = d3.polygonHull(pts);
    return shape && { shape, top: shape.reduce((m, p) => (p[1] < m[1] ? p : m)) };
  };
  const drawHulls = () => hull.each(function outline([, ns]) {
    const h = hullShape(ns);
    const g = d3.select(this);
    g.select("path").attr("d", h ? hullLine(h.shape) : null);
    if (h) g.select("text").attr("x", h.top[0]).attr("y", h.top[1] - 5);
  });

  // calls curve gently (a call each way bows apart); arrowheads stop at the node's edge
  const curve = (l) => {
    const s = l.source, t = l.target;
    const dx = t.x - s.x, dy = t.y - s.y;
    const bend = l.type === "member" ? 0 : 0.1;
    const cx = (s.x + t.x) / 2 - dy * bend, cy = (s.y + t.y) / 2 + dx * bend;
    const ex = t.x - cx, ey = t.y - cy, len = Math.hypot(ex, ey) || 1;
    const back = l.type === "calls" ? radius(t) + 2.5 : 0;
    return `M${s.x},${s.y}Q${cx},${cy} ${t.x - (ex / len) * back},${t.y - (ey / len) * back}`;
  };
  const render = () => {
    link.attr("d", curve);
    flow.attr("d", curve);
    node.attr("transform", (n) => `translate(${n.x},${n.y})`);
    if (outlined) drawHulls();
  };
  const remember = () => { for (const n of nodes) positions.set(n.id, { x: n.x, y: n.y }); };

  // ── pointing: a node's calls stay, the rest steps back; its path flows ──
  const highlight = (id) => {
    if (id === null || id === undefined || !byId.has(id)) {
      el.classList.remove("hovering");
      node.attr("data-lit", null);
      link.attr("data-lit", null).classed("on-path", false);
      hull.attr("data-lit", null);
      flow.classed("flowing", false);
      return;
    }
    const near = related.get(id);
    const path = trailOf(id);
    el.classList.add("hovering");
    node.attr("data-lit", (n) => (n.id === id ? "self" : near.has(n.id) ? "near" : path.ids.has(n.id) ? "path" : null));
    link.attr("data-lit", (l) => (l.type === "member" ? null : nodeId(l.source) === id ? "out" : nodeId(l.target) === id ? "in" : null))
      .classed("on-path", (l) => path.keys.has(keyOf(l)));
    const file = byId.get(id).file;
    hull.attr("data-lit", ([f]) => (f === file ? "" : null));
    flow.classed("flowing", (l) => path.keys.has(keyOf(l)));
    placeQuiet(new Set([id, ...near, ...path.ids]));
  };
  // the answer's paths flow once when it is first shown
  let flowTimer = null;
  const playTrails = () => {
    if (!trailLinks.length || calm()) return;
    flow.classed("flowing", true);
    clearTimeout(flowTimer);
    flowTimer = setTimeout(() => { if (!el.classList.contains("hovering")) flow.classed("flowing", false); }, 2400);
  };

  // ── camera ──
  const bar = document.querySelector(".graph-actions");
  const top = bar ? bar.getBoundingClientRect().height + 12 : 0;
  const room = height - top - (caption ? 40 : 0);
  const fitTransform = () => {
    const focus = rings ? nodes : nodes.filter((n) => state.retrieved.has(n.id));
    const pts = focus.length ? focus : nodes;
    // bounding box including the labels hanging under each node; the answer's labels grow
    // when zoomed out, so refit until the scale and the label size agree
    let scale = 1, x0, x1, y0, y1;
    for (let i = 0; i < 4; i++) {
      const grow = (n) => (subject(n) ? labelScale(scale) : 1);
      const half = (n) => Math.max(radius(n), (labelWidth(n) * grow(n)) / 2);
      x0 = Math.min(...pts.map((n) => n.x - half(n)));
      x1 = Math.max(...pts.map((n) => n.x + half(n)));
      y0 = Math.min(...pts.map((n) => n.y - radius(n) - (rings ? 24 : 4)));
      y1 = Math.max(...pts.map((n) => n.y + radius(n) + 6 + 16 * grow(n)));
      scale = Math.min(1.5, 0.92 / Math.max((x1 - x0 + 40) / width, (y1 - y0 + 40) / room));
    }
    let cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    if (scale < MIN_FIT) {
      // too much to show legibly at once (a phone, a big answer): centre on what the answer cites
      scale = MIN_FIT;
      const core = nodes.filter((n) => state.cited?.has(n.id))
        .sort((a, b) => (state.sids.get(a.id) ?? Infinity) - (state.sids.get(b.id) ?? Infinity));
      const half = (n) => Math.max(radius(n), (labelWidth(n) * labelScale(MIN_FIT)) / 2);
      let c = core.length ? core : pts;
      const spread = (Math.max(...c.map((n) => n.x + half(n))) - Math.min(...c.map((n) => n.x - half(n)))) * MIN_FIT;
      if (core.length > 1 && spread > width - 24) c = [core[0]];    // can't show all cited labels: the first one, whole
      cx = (Math.min(...c.map((n) => n.x - half(n))) + Math.max(...c.map((n) => n.x + half(n)))) / 2;
      cy = (Math.min(...c.map((n) => n.y)) + Math.max(...c.map((n) => n.y))) / 2 + 8;
    }
    return { scale, transform: d3.zoomIdentity.translate(width / 2, top + room / 2).scale(scale).translate(-cx, -cy) };
  };
  const camera = (transform, ms) => {
    if (ms && !calm()) svg.transition().duration(ms).ease(d3.easeExpOut).call(zoom.transform, transform);
    else svg.call(zoom.transform, transform);
  };
  const fit = (animate) => camera(fitTransform().transform, animate ? 420 : 0);
  const zoomBy = (k) => (calm() ? svg.call(zoom.scaleBy, k) : svg.transition().duration(220).ease(d3.easeExpOut).call(zoom.scaleBy, k));
  const zoomTo = (ids) => {
    const ns = ids.map((id) => byId.get(id)).filter(Boolean);
    const x0 = Math.min(...ns.map((n) => n.x)) - 60, x1 = Math.max(...ns.map((n) => n.x)) + 60;
    const y0 = Math.min(...ns.map((n) => n.y)) - 40, y1 = Math.max(...ns.map((n) => n.y)) + 60;
    const k = Math.min(2.2, 0.9 / Math.max((x1 - x0) / width, (y1 - y0) / room));
    camera(d3.zoomIdentity.translate(width / 2, top + room / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2), 420);
  };

  // labels go below their node unless that covers a label already placed; then above,
  // right or left (cited first, then the answer's code by S-number, then the rest)
  const SIDES = ["below", "above", "right", "left"];
  // a label's box as drawn (measured: ~7.6px a character, 17px a line at 12px), plus a margin
  const labelBox = (n, side, k) => {
    const w = (sidText(n) + labelText(n)).length * 7.7 * k + 6, h = 18 * k, r = radius(n) + 3;
    if (side === "below") return { x0: n.x - w / 2, x1: n.x + w / 2, y0: n.y + r, y1: n.y + r + h };
    if (side === "above") return { x0: n.x - w / 2, x1: n.x + w / 2, y0: n.y - r - h, y1: n.y - r };
    if (side === "right") return { x0: n.x + r, x1: n.x + r + w, y0: n.y - h / 2, y1: n.y + h / 2 };
    return { x0: n.x - r - w, x1: n.x - r, y0: n.y - h / 2, y1: n.y + h / 2 };
  };
  const overlap = (q, boxes) => boxes.reduce((sum, p) => sum + Math.max(0, Math.min(q.x1, p.x1) - Math.max(q.x0, p.x0))
    * Math.max(0, Math.min(q.y1, p.y1) - Math.max(q.y0, p.y0)), 0);
  const sideFor = (n, k, boxes) => {
    let best = null;
    for (const side of SIDES) {
      const q = labelBox(n, side, k), area = overlap(q, boxes);
      if (!best || area < best.area) best = { side, q, area };
      if (area === 0) break;
    }
    return best;
  };
  const setSide = (sel) => {
    const gap = (n) => radius(n) + 4;
    sel.attr("x", (n) => (n.side === "right" ? gap(n) : n.side === "left" ? -gap(n) : 0))
      .attr("y", (n) => (n.side === "below" ? gap(n) : n.side === "above" ? -gap(n) : 0))
      .attr("text-anchor", (n) => (n.side === "right" ? "start" : n.side === "left" ? "end" : "middle"))
      .attr("dominant-baseline", (n) => (n.side === "below" ? "hanging" : n.side === "above" ? "auto" : "central"));
  };
  // names revealed by pointing go where they cover the fewest labels already showing
  const placeQuiet = (ids) => {
    const k = d3.zoomTransform(el).k;
    const boxes = nodes.filter(labelled).map((n) => labelBox(n, n.side || "below", subject(n) ? labelScale(k) : 1));
    const reveal = nodes.filter((n) => ids.has(n.id) && !labelled(n));
    for (const n of reveal) {
      const best = sideFor(n, Math.max(1, 1 / k), boxes);
      n.side = best.side;
      boxes.push(best.q);
    }
    setSide(node.filter((n) => ids.has(n.id) && !labelled(n)).select("text.quiet"));
  };
  const placeLabels = (scale) => {
    const placed = [];
    // the names already on the canvas (files, rings) are obstacles: node labels go around them
    const named = (x, y, text, perChar) => {
      const k = Math.max(1, 1 / scale), w = text.length * perChar * k + 8, h = 17 * k;
      return { x0: x - w / 2, x1: x + w / 2, y0: y - h, y1: y + 3 };
    };
    for (const [file, ns] of groups) {
      const h = hullShape(ns);
      if (h) placed.push(named(h.top[0], h.top[1] - 5, file.split("/").pop(), 7.7));
    }
    if (rings) {
      for (const i of new Set(nodes.map((n) => RING_RADII.indexOf(ringRadius(n))))) {
        placed.push(named(width / 2, height / 2 - RING_RADII[i] - 6, RING_NAMES[i], 6.8));
      }
    }
    const first = (n) => (state.cited?.has(n.id) ? 0 : state.retrieved.has(n.id) ? 1 : 2);
    for (const n of nodes.filter(labelled).sort((a, b) => first(a) - first(b)
      || (state.sids.get(a.id) ?? Infinity) - (state.sids.get(b.id) ?? Infinity))) {
      const best = sideFor(n, subject(n) ? labelScale(scale) : 1, placed);
      n.side = best.side;
      placed.push(best.q);
    }
    setSide(label);
  };

  // ── the morph: final positions are known; the scene glides there from where it was ──
  const finals = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
  const target = fitTransform();
  placeLabels(target.scale);
  remember();
  const live = () => { sim.on("tick", render).on("end", remember); };
  const finish = () => {
    timer?.stop();
    timer = null;
    for (const n of nodes) Object.assign(n, finals.get(n.id));
    node.style("opacity", null);
    render();
    live();
  };
  const glide = moving && start.size > 0;
  if (glide) {
    for (const n of nodes) {
      const s = start.get(n.id);
      if (s) Object.assign(n, s);
    }
    // new nodes grow out of a neighbour (or simply appear where they belong)
    node.filter((n) => !start.has(n.id) || n.born).style("opacity", 0)
      .transition().delay(140).duration(MORPH_MS - 140).ease(d3.easeExpOut).style("opacity", 1);
    render();
    timer = d3.timer((elapsed) => {
      const t = Math.min(1, elapsed / MORPH_MS), e = d3.easeExpOut(t);
      for (const n of nodes) {
        const s = start.get(n.id), f = finals.get(n.id);
        if (s) { n.x = s.x + (f.x - s.x) * e; n.y = s.y + (f.y - s.y) * e; }
      }
      render();
      if (t === 1) finish();
    });
    camera(target.transform, MORPH_MS);
  } else {
    render();
    camera(target.transform, 0);
    live();
  }

  graphView = {
    node, highlight, fit, zoomBy, playTrails,
    stop: () => {
      timer?.stop();
      timer = null;
      clearTimeout(flowTimer);
      sim.stop();
      svg.interrupt();
      remember();                                       // a view interrupted mid-glide continues from here
    },
  };
  // first sight of a written answer: its paths flow once
  if (answer && state.cited && state.trailsShown !== state.answerTurn) {
    state.trailsShown = state.answerTurn;
    setTimeout(() => graphView?.playTrails(), glide ? MORPH_MS : 0);
  }
}

/** Roving focus: one node is in the Tab order; the arrow keys move it. */
function focusNode(datum, move) {
  if (!graphView) return;
  graphView.node.attr("tabindex", (n) => (n === datum ? 0 : -1));
  if (move) graphView.node.filter((n) => n === datum).node()?.focus();
}

/** Open a node: its card, and on a wide screen its source row in the answer. */
function activateNode(n) {
  showNodeCard(n);
  const sid = state.sids.get(n.id);
  if (sid && state.answerTurn && !window.matchMedia("(max-width: 60rem)").matches) {
    openSource(state.answerTurn, sid, { panel: false });
  }
}

function pulseNode(chunkId) {
  if (!graphView) return;
  graphView.node.classed("pulse", (n) => n.id === chunkId);
  setTimeout(() => graphView?.node.classed("pulse", false), 1600);
}

function hideNodeCard() {
  $("#node-card").hidden = true;
}

function showNodeCard(n) {
  const card = $("#node-card");
  const url = githubUrl(state.demo, n.file, n.start_line, n.end_line);
  const sid = state.sids.get(n.id);
  const status = nodeStatus(n);
  card.replaceChildren(
    el("div", { class: "node-head" },
      sid ? el("span", { class: state.cited?.has(n.id) ? "sid cited" : "sid", "data-relation": relationOfNode(n.id), title: RELATION_WORDS[relationOfNode(n.id)], text: `S${sid}` }) : null,
      el("h2", { text: n.name }),
      el("button", { type: "button", class: "icon-button", "aria-label": "Close", title: "Close (Esc)", onclick: () => {
        hideNodeCard();
        graphView?.node.filter(function focused() { return this.tabIndex === 0; }).node()?.focus();
      } }, svgIcon("M7 7l10 10M17 7L7 17"))),
    el("p", {}, `${n.kind[0].toUpperCase()}${n.kind.slice(1)} in `, el("code", { text: formatLocation(n) }),
      languageTag(n.language) ? " " : null, languageTag(n.language)),
    status ? el("p", { class: "node-status", text: status }) : null,
    el("div", { class: "row" },
      sid && state.answerTurn ? el("button", { type: "button", class: "link-button", text: `Show S${sid} in the answer`,
        onclick: () => openSource(state.answerTurn, sid) }) : null,
      el("button", { type: "button", class: "link-button", text: "Show in code", onclick: () => {
        showCode(n.file, { start: n.start_line, end: n.end_line, reveal: true });
      } }),
      el("button", { type: "button", class: "link-button", text: "Ask about this", onclick: () => {
        $("#question").value = `What does \`${n.label || n.name}\` do and who calls it?`;
        autosize();
        showPanel("chat");
        $("#question").focus();
      } }),
      url ? el("a", { class: "open", href: url, target: "_blank", rel: "noopener", text: "Open on GitHub" }) : null));
  card.hidden = false;
}

// ── code viewer ─────────────────────────────────────────────────────────────
const fileCache = new Map();             // "repo:path" -> Promise<{text, url}>
const code = { repo: null, path: null, target: null, rendered: false, token: 0 };

function isWide() {
  return !window.matchMedia("(max-width: 60rem)").matches;
}

function loadFile(repoId, path) {
  const key = `${repoId}:${path}`;
  if (!fileCache.has(key)) {
    const request = fetch(`/api/repos/${encodeURIComponent(repoId)}/file?path=${encodeURIComponent(path)}`)
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `Couldn't load ${path}.`);
        return body;
      });
    request.catch(() => fileCache.delete(key));            // let a failed load be retried
    fileCache.set(key, request);
  }
  return fileCache.get(key);
}

/** Highlighted HTML per line; spans that cross a line break are closed and reopened. */
function highlightLines(text, path = "") {
  const language = languageOfPath(path) || "python";
  if (!window.hljs || !window.hljs.getLanguage(language)) return text.split("\n").map(escapeHtml);
  const html = window.hljs.highlight(text, { language, ignoreIllegals: true }).value;
  const lines = [];
  const open = [];
  let line = "";
  for (const m of html.matchAll(/(<span[^>]*>)|(<\/span>)|([^<]+)/g)) {
    if (m[1]) { open.push(m[1]); line += m[1]; }
    else if (m[2]) { open.pop(); line += m[2]; }
    else {
      m[3].split("\n").forEach((part, i) => {
        if (i > 0) { lines.push(line + "</span>".repeat(open.length)); line = open.join(""); }
        line += part;
      });
    }
  }
  lines.push(line + "</span>".repeat(open.length));
  return lines;
}

/** Files the current answer used, in source order. */
function answerFiles() {
  return [...new Set(state.sources.map((s) => s.file))];
}

function renderCodeFiles() {
  const nav = $("#code-files");
  const files = answerFiles();
  nav.hidden = !files.length;
  nav.replaceChildren(...files.map((file) => el("button", {
    type: "button", "aria-pressed": String(file === code.path), title: file,
    onclick: () => showCode(file, { first: true }),
  }, file.split("/").pop())));
  nav.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderCodeEmpty(message = "Ask a question, then choose a citation or a source to read its code here.") {
  code.token++;
  Object.assign(code, { repo: null, path: null, target: null, rendered: false });
  $("#code-title").textContent = "";
  $("#code-github").hidden = true;
  $("#code-view").replaceChildren(el("p", { class: "code-status", text: message }));
  renderCodeFiles();
}

/**
 * Open `path` in the code viewer. start/end: the range to scroll to and outline;
 * first: scroll to the first source of the answer in this file; reveal: show the pane.
 */
async function showCode(path, { start = null, end = null, first = false, reveal = false } = {}) {
  if (!path || !state.demo) return;
  if (reveal) showPanel("code");
  const repo = state.demo;
  if (first) {
    const s = state.sources.filter((x) => x.file === path)
      .sort((a, b) => Number(!state.cited?.has(a.chunk_id)) - Number(!state.cited?.has(b.chunk_id)) || a.start_line - b.start_line)[0];
    if (s) ({ start_line: start, end_line: end } = s);
  }
  code.target = start ? { start, end: end || start } : null;
  if (code.repo === repo.id && code.path === path && code.rendered) {
    markCode();
    renderCodeFiles();
    scrollToTarget({ smooth: true });          // within one file the motion shows the direction
    return;
  }
  const token = ++code.token;
  Object.assign(code, { repo: repo.id, path, rendered: false });
  $("#code-title").textContent = path;
  $("#code-github").hidden = true;
  renderCodeFiles();
  const view = $("#code-view");
  view.setAttribute("aria-busy", "true");
  view.replaceChildren(el("p", { class: "code-status" }, el("span", { class: "spinner" }), ` Loading ${path}…`));
  try {
    const file = await loadFile(repo.id, path);
    if (token !== code.token) return;                    // another file was asked for meanwhile
    // hljs output is escaped text in spans; .lines is as wide as the longest line,
    // so tinted ranges stay full width when the view scrolls sideways
    view.innerHTML = `<div class="lines">${highlightLines(file.text, path)
      .map((html, i) => `<span class="line" data-n="${i + 1}">${html || " "}</span>`).join("")}</div>`;
    code.rendered = true;
    code.url = file.url;
    markCode();
    scrollToTarget();
  } catch (error) {
    if (token !== code.token) return;
    view.replaceChildren(el("p", { class: "code-status error", text: error.message }));
  } finally {
    if (token === code.token) view.removeAttribute("aria-busy");
  }
}

/** Tint the answer's sources in this file: cited strong, retrieved faint; outline the target. */
function markCode() {
  const view = $("#code-view");
  view.querySelectorAll(".line.cited, .line.retrieved, .line.target, .line[data-tag]").forEach((line) => {
    line.classList.remove("cited", "retrieved", "target", "tag-cited");
    line.removeAttribute("data-tag");
  });
  const line = (n) => view.querySelector(`.line[data-n="${n}"]`);
  const sources = state.sources.filter((s) => s.file === code.path);
  // wide ranges first, so a cited method inside a retrieved class ends up cited
  for (const s of [...sources].sort((a, b) => (b.end_line - b.start_line) - (a.end_line - a.start_line))) {
    const cited = Boolean(state.cited?.has(s.chunk_id));
    for (let n = s.start_line; n <= s.end_line; n++) line(n)?.classList.add(cited ? "cited" : "retrieved");
    const head = line(s.start_line);
    if (head) {
      head.dataset.tag = [head.dataset.tag, `S${s.id}`].filter(Boolean).join(" ");
      if (cited) head.classList.add("tag-cited");
    }
  }
  if (code.target) {
    for (let n = code.target.start; n <= code.target.end; n++) line(n)?.classList.add("target");
  }
  const link = $("#code-github");
  link.hidden = !code.url;
  if (code.url) link.href = code.target ? `${code.url}#L${code.target.start}-L${code.target.end}` : code.url;
}

/** Scroll the code view to the target (or the first source); a newly opened file jumps there. */
function scrollToTarget({ smooth = false } = {}) {
  const view = $("#code-view");
  if (!code.rendered || !view.clientHeight) return;      // hidden pane: scrolled when shown
  const n = code.target?.start || Number(view.querySelector(".line.cited, .line.retrieved")?.dataset.n || 1);
  const line = view.querySelector(`.line[data-n="${n}"]`);
  if (!line) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) smooth = false;
  // position inside the scrolling view (offsetTop would be relative to the page)
  const top = line.getBoundingClientRect().top - view.getBoundingClientRect().top + view.scrollTop;
  view.scrollTo({ top: Math.max(top - 48, 0), behavior: smooth ? "smooth" : "auto" });
}

/** After an answer (or a repo switch): show the first cited source, without taking over the pane. */
function showAnswerCode({ reveal = false } = {}) {
  const s = state.sources.find((x) => state.cited?.has(x.chunk_id)) || state.sources[0];
  if (!s) { renderCodeEmpty(); return; }
  showCode(s.file, { start: s.start_line, end: s.end_line, reveal });
}

// ── layout / startup ────────────────────────────────────────────────────────
function showPanel(name) {
  if (name !== "chat") state.pane = name;
  state.panel = name;
  document.querySelectorAll(".panel-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.panel === state.panel)));
  document.querySelectorAll(".pane-tabs button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.panel === state.pane));
    b.tabIndex = b.dataset.panel === state.pane ? 0 : -1;
  });
  document.querySelectorAll(".layout [data-panel]").forEach((p) => {
    p.classList.toggle("active", p.dataset.panel === state.panel);       // narrow: the one pane
    p.classList.toggle("current", p.dataset.panel === state.pane);       // wide: the right pane
  });
  if (name === "graph") drawGraph();
  if (name === "code") scrollToTarget();
}

function autosize() {
  const area = $("#question");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 160)}px`;
  area.style.overflowY = area.scrollHeight > 160 ? "auto" : "hidden";
}

/** The account button: who is signed in, and the way out. */
function setupAccount() {
  const user = currentUser();
  // signed out: the demo repositories are open; the header offers sign-in instead of an account
  $("#sign-in-link").hidden = Boolean(user);
  $("#user-button").hidden = !user;
  $("#sign-in-link").href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
  if (!user) return;
  $("#user-initials").textContent = initials(user.name);
  showAvatar($("#user-button"), $("#user-initials"), avatarSrc(user));
  $("#user-button").title = `Signed in as ${user.name}`;
  $("#user-name").textContent = user.name;
  $("#user-provider").textContent = user.login ? `@${user.login} on GitHub` : "GitHub";
  $("#sign-out").addEventListener("click", async () => {
    await signOut();                  // the session cookie is gone before the next page asks
    window.location.href = "/";
  });
}

/** The GitHub picture over the initials; the initials stay if it can't load. */
function showAvatar(button, label, src) {
  if (!src) return;
  const img = document.createElement("img");
  img.alt = "";
  img.referrerPolicy = "no-referrer";
  img.addEventListener("load", () => { label.hidden = true; });
  img.addEventListener("error", () => img.remove());
  img.src = src;
  button.append(img);
}

/** To sign-in, coming back to this exact view afterwards. */
function toSignIn() {
  window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

async function start() {
  setupAccount();
  setupSettings();
  setupSaved();
  const openAddRepo = setupAddRepo();
  state.userRepos = currentUser() ? await loadUserRepos() : [];   // your repositories need you signed in
  showPanel("chat");
  document.querySelectorAll(".panel-tabs button").forEach((b) => b.addEventListener("click", () => showPanel(b.dataset.panel)));
  // Code | Call graph: a tablist, so the arrow keys move between the two tabs
  document.querySelectorAll(".pane-tabs button").forEach((b) => {
    b.addEventListener("click", () => { state.paneChosen = true; showPanel(b.dataset.panel); });
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const other = [...document.querySelectorAll(".pane-tabs button")].find((x) => x !== b);
      state.paneChosen = true;
      showPanel(other.dataset.panel);
      other.focus();
    });
  });
  renderCodeEmpty();
  document.querySelectorAll("#graph-layout button").forEach((b) => b.addEventListener("click", () => {
    if (state.graphLayout === b.dataset.layout) return;
    state.graphLayout = b.dataset.layout;
    hideNodeCard();
    drawGraph();
  }));
  $("#zoom-in").addEventListener("click", () => graphView?.zoomBy(1.5));
  $("#zoom-out").addEventListener("click", () => graphView?.zoomBy(1 / 1.5));
  $("#zoom-fit").addEventListener("click", () => graphView?.fit(true));
  document.querySelectorAll(".legend-relation button").forEach((b) => b.addEventListener("click", () => {
    const relation = b.closest("li").dataset.relation;
    if (hiddenRelations.has(relation)) hiddenRelations.delete(relation); else hiddenRelations.add(relation);
    applyRelationToggles();
  }));
  $("#graph-toggle").addEventListener("click", () => {
    state.graphMode = state.graphMode === "full" ? "focus" : "full";
    drawGraph();
  });
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#question").value); });
  // while an answer is being written the button is Stop (the empty field must not block it)
  $("#ask-button").addEventListener("click", (e) => {
    if (!state.busy) return;
    e.preventDefault();
    state.controller?.abort();
  });
  document.addEventListener("keydown", (e) => {
    if (document.querySelector("dialog[open]")) return;
    const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
    if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      showPanel("chat");
      $("#question").focus();
    } else if (e.key === "Escape" && !$("#node-card").hidden) {
      const inCard = $("#node-card").contains(document.activeElement);
      hideNodeCard();
      if (inCard) graphView?.node.filter(function focused() { return this.tabIndex === 0; }).node()?.focus();
    }
  });
  $("#repo-tabs").addEventListener("scroll", syncTabFades, { passive: true });
  $(".skip-link").addEventListener("click", (e) => {
    e.preventDefault();
    showPanel("chat");
    $("#question").focus();
  });
  $("#question").addEventListener("input", autosize);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#question").value); }
  });
  $("#conversation").addEventListener("click", (e) => {
    const cite = e.target.closest("a.cite");
    if (!cite) return;
    e.preventDefault();
    openSource(cite.closest(".turn"), Number(cite.dataset.source), { code: true });
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      drawGraph();
      $('#repo-tabs [aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
      syncTabFades();
    }, 200);
  });

  // Free hosting sleeps when idle: say so if the first response is slow.
  const wakeTimer = setTimeout(() => banner("Waking up the server (free hosting sleeps when idle). This can take up to a minute…"), 3000);
  try {
    state.health = await fetchJSON("/api/health");
    state.demos = await fetchJSON("/api/demos");
  } catch {
    clearTimeout(wakeTimer);
    banner("Could not reach the server. Please reload the page in a moment.", "error");
    return;
  }
  clearTimeout(wakeTimer);
  $("#server-model").textContent = state.health.llm.server ? `(${state.health.llm.models[0]})` : "(not configured)";
  updateLlmBanner();

  const params = new URL(window.location.href).searchParams;
  const wanted = params.get("repo");
  // a link to a repository someone added (not a demo) needs an account, as adding it did
  const demo = wanted && state.demos.some((d) => d.id === wanted || d.name === wanted);
  if (wanted && !demo && !currentUser()) { toSignIn(); return; }
  const info = wanted ? await repoInfo(wanted) : null;
  await selectRepo(info ? info.id : state.demos[0]?.id);
  if (wanted && !info) {
    // a shared link to a repo that isn't indexed yet: offer to index it
    if (/^[\w.-]+\/[\w.-]+$/.test(wanted)) openAddRepo(`https://github.com/${wanted}`);
    return;
  }
  // shared links (?repo=...&q=...) open the repo and run the question; popular
  // questions are served from the server's answer cache
  if (params.get("q")) ask(params.get("q"));
}

start();
