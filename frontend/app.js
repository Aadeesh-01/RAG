import {
  bridgeCounts, citedSources, createSSEParser, errorMessage, focusSubgraph, formatDate, formatLocation, hopLabel, middleTruncate,
  middleTruncateName, moduleGraph, normalizeCitations, plural, questionsPerDay, renderAnswer, repoOverview, repoStatus, suggestedQuestions,
  viaLabel, initials, avatarSrc, safeHref,
} from "./lib.js";
import { currentUser, signIn, signOut } from "./auth.js";

const $ = (selector) => document.querySelector(selector);
const SETTINGS_KEY = "coderag.settings";
// kept in localStorage before the database; removed on start (history is on the server now)
const OLD_LOCAL_KEYS = ["coderag.asked", "coderag.saved"];
const FULL_GRAPH_LIMIT = 120;          // draw the whole graph up front only for small repos
const HISTORY_LIMIT = 4;               // thumbnails of the last answers' subgraphs

const state = {
  demos: [], demo: null, health: null,
  graphs: new Map(), graph: null, graphMode: "focus",
  retrieved: new Map(),                // chunk_id -> hop for the latest answer
  sids: new Map(),                     // chunk_id -> source number (S1, S2…) in that answer
  cited: null,                         // Set of chunk_ids the answer cites; null until it is written
  answerTurn: null,                    // the turn whose sources the graph shows
  sources: [],                         // that answer's sources (file, lines, chunk_id, id)
  selected: null,                      // the node (chunk id or "m:<file>") the panel shows
  view: "overview",                    // the pill nav: overview | ask | graph | repos
  positions: new Map(),                // repo id -> node id -> {x, y}: where the graph left things
  graphLayout: "free",                 // answer graph: "free" | "rings" (by call distance)
  trailsShown: null,                   // the answer turn whose paths have already flowed
  overviews: new Map(),                // repo id -> repoOverview(graph)
  history: new Map(),                  // repo id -> [{turn, evidence, thumb, qid}] this page's answers' subgraphs
  historyMeta: new Map(),              // repo id -> {items, next, total, per_day}: GET /api/repos/{id}/history
  sessionAsked: [],                    // signed out: [{t, repo}] asked in this session (memory only)
  sessionHistory: [],                  // signed out: answered questions in this session (memory only)
  jobCards: new Map(),                 // job id -> its progress card (jobs started on this page)
  busy: false, turn: 0, controller: null,
  debug: new URLSearchParams(window.location.search).get("debug") === "1",
  userRepos: [],                       // the signed-in user's repositories (server summaries)
  jobs: new Map(),                     // repo id -> its latest indexing job (GET /api/jobs)
  chats: new Map(),                    // repo id -> {turns, evidence} while another repo is open
  reposFilter: "all",
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
    else if (key === "href") {                 // http(s) and relative links only (repo facts are owner-set)
      const href = safeHref(value);
      if (href !== null) node.setAttribute("href", href);
    } else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** replaceChildren that skips null, undefined and false (never renders the word "null"). */
function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

/** A category dot beside its word: the same mark everywhere a category is named. */
function dot(kind, language) {
  return el("i", { class: `dot${kind ? ` dot--${kind}` : ""}`, "data-language": language || null, "aria-hidden": "true" });
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

// ── views: the pill nav ─────────────────────────────────────────────────────
function isNarrow() {
  return window.matchMedia("(max-width: 60rem)").matches;
}

function setView(name, { focus = false } = {}) {
  state.view = name;
  document.body.dataset.view = name;
  document.querySelectorAll(".pill-nav button").forEach((b) => {
    if (b.dataset.view === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  const repos = name === "repos";
  $("#view-work").hidden = repos;
  $("#view-repos").hidden = !repos;
  if (repos) { renderRepos(); syncJobs(); }
  else if (name === "graph") requestAnimationFrame(() => drawGraph());
  if (focus && name === "ask") $("#question").focus();
  if (focus && name === "overview") window.scrollTo({ top: 0 });
}

// ── repositories ────────────────────────────────────────────────────────────
function githubUrl(demo, file, start, end) {
  if (!demo.url || !demo.sha) return null;
  const path = demo.root ? `${demo.root}/${file}` : file;
  return `${demo.url}/blob/${demo.sha}/${path}#L${start}-L${end}`;
}

/** The repo switcher: a search box over Demos and Your repos. */
function setupRepoSwitcher() {
  const button = $("#repo-button");
  const menu = $("#repo-menu");
  const filter = $("#repo-filter");
  const open = (on) => {
    menu.hidden = !on;
    button.setAttribute("aria-expanded", String(on));
    if (on) { filter.value = ""; renderRepoMenu(); filter.focus(); }
  };
  button.addEventListener("click", () => open(menu.hidden));
  filter.addEventListener("input", renderRepoMenu);
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !button.contains(e.target)) open(false);
  });
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); open(false); button.focus(); return; }
    const items = [...menu.querySelectorAll(".repo-list li")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const to = e.key === "ArrowDown" ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
      items[to]?.focus();
    } else if ((e.key === "Enter" || e.key === " ") && i >= 0) {
      e.preventDefault();
      items[i].click();
    }
  });
  state.closeRepoMenu = () => open(false);
}

function renderRepoMenu() {
  const q = $("#repo-filter").value.trim().toLowerCase();
  const match = (r) => !q || r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q);
  const item = (repo, removable) => el("li", {
    role: "option", tabindex: "-1", "aria-selected": String(repo.id === state.demo?.id), title: repo.name,
    onclick: () => { state.closeRepoMenu(); selectRepo(repo.id); },
  },
  dot(null, mainLanguage(repo)),
  el("span", { class: "name", text: repo.name }),
  repo.private ? el("span", { class: "private-badge", text: "Private" }) : null,
  removable ? el("button", { type: "button", class: "icon-button remove", "aria-label": `Remove ${repo.name}`, title: "Remove from your repos",
    onclick: (e) => { e.stopPropagation(); removeUserRepo(repo.id); renderRepoMenu(); } }, svgIcon("M7 7l10 10M17 7L7 17")) : null);
  $("#repo-demos").replaceChildren(...state.demos.filter(match).map((d) => item(d, false)));
  const mine = state.userRepos.filter(match);
  $("#repo-mine").replaceChildren(...mine.map((r) => item(r, true)));
  const note = $("#repo-mine-empty");
  note.textContent = !currentUser() ? "Sign in to add your own repositories."
    : !state.userRepos.length ? "None yet. Add one below." : !mine.length ? `No repository matches “${$("#repo-filter").value.trim()}”.` : "";
  $("#repo-current").textContent = state.demo ? state.demo.name : "Choose a repository";
}

/** The language with the most files, in the indexer's key ("python"), or null. */
function mainLanguage(repo) {
  const entries = Object.entries(repo?.languages || {});
  if (!entries.length) return null;
  const [label] = entries.sort((a, b) => b[1] - a[1])[0];
  return languageKey(label);
}

// ── the signed-in user's repositories: kept on the server, per GitHub account ──
const OLD_REPOS_KEY = "coderag.repos";     // this browser's list before accounts; no longer used

async function loadUserRepos() {
  try { localStorage.removeItem(OLD_REPOS_KEY); } catch { /* storage blocked */ }
  try {
    return await fetchJSON("/api/repos/mine");
  } catch {
    return [];                 // signed out meanwhile, or the server is unreachable: no repos
  }
}

/** Show `id` as one of the user's repos, and keep it on their account. */
function rememberUserRepo(id, name, info = null) {
  const known = state.userRepos.find((r) => r.id === id);
  const entry = { ...(known || {}), ...(info || {}), id, name };
  state.userRepos = [entry, ...state.userRepos.filter((r) => r.id !== id)];
  if (!known && currentUser()) {
    fetch(`/api/repos/mine/${encodeURIComponent(id)}`, { method: "PUT" }).catch(() => {});
  }
  if (!info) fetchJSON(`/api/repos/${encodeURIComponent(id)}`).then((full) => {
    state.userRepos = state.userRepos.map((r) => (r.id === id ? { ...r, ...full } : r));
    if (state.view === "repos") renderRepos();
  }).catch(() => {});
}

function removeUserRepo(id) {
  state.userRepos = state.userRepos.filter((r) => r.id !== id);
  if (currentUser()) fetch(`/api/repos/mine/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
  // switching away stores the open chat, so forget it only afterwards
  if (state.demo?.id === id) selectRepo(state.demos[0]?.id).then(() => state.chats.delete(id));
  else { state.chats.delete(id); renderRepoMenu(); }
  if (state.view === "repos") renderRepos();
}

// ── languages: the indexer's (coderag/languages.py) labels and highlight.js names ──
const LANGUAGE_LABELS = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", java: "Java",
  kotlin: "Kotlin", c: "C", cpp: "C++", go: "Go", rust: "Rust", html: "HTML", config: "config" };
const EXTENSION_LANGUAGES = { py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "typescript", java: "java", kt: "kotlin", kts: "kotlin",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", hxx: "cpp", go: "go", rs: "rust",
  html: "html", htm: "html" };
const INDEXED_GITHUB_LANGUAGES = new Set(Object.values(LANGUAGE_LABELS).filter((l) => l !== "HTML" && l !== "config"));

/** "go", "python" ... for a file path (by extension), or null. */
function languageOfPath(path) {
  return EXTENSION_LANGUAGES[(path || "").split(".").pop().toLowerCase()] || null;
}

/** "Python" -> "python", "C++" -> "cpp": the key a label or a node language maps to. */
function languageKey(label) {
  if (!label) return null;
  const lower = String(label).toLowerCase();
  if (LANGUAGE_LABELS[lower]) return lower;
  const found = Object.entries(LANGUAGE_LABELS).find(([, name]) => name.toLowerCase() === lower);
  return found ? found[0] : "other";
}

/** The language word with its dot, or null for a Python-only repository's Python. */
function languageTag(language, { always = false } = {}) {
  if (!language) return null;
  const mixed = Object.keys(state.demo?.languages || {}).length > 1;
  if (!always && !mixed && language === "python") return null;
  return el("span", { class: "lang", title: LANGUAGE_LABELS[language] || language }, dot(null, languageKey(language)),
    el("span", { text: LANGUAGE_LABELS[language] || language }));
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
    const meta = [r.language || "No language", r.pushed_at && `pushed ${pushedAgo(r.pushed_at)}`].filter(Boolean).join(", ");
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
    const total = plural(repos.length, "repository", "repositories");
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
          el("button", { type: "button", class: "secondary small", text: "Reconnect GitHub",
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
    state.closeRepoMenu?.();
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
  const openOrSignIn = () => (currentUser() ? open() : toSignIn());
  $("#add-repo-button").addEventListener("click", openOrSignIn);
  $("#repos-add").addEventListener("click", openOrSignIn);
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

const ACTIVE = new Set(["queued", "fetching", "parsing", "embedding", "saving", "running"]);
const isActive = (j) => Boolean(j) && ACTIVE.has(j.status);
let jobsTimer = null;

/** A job as the Repos page reads it: queued, running (with its stage), ready or failed. */
function jobView(j) {
  return { ...j, stage: j.status, status: ACTIVE.has(j.status) && j.status !== "queued" ? "running" : j.status };
}

function renderJobCard(j) {
  const card = state.jobCards.get(j.job_id);
  if (!card) return;
  card.className = `job ${j.status}`;
  const waiting = j.status === "queued" && j.queue_position > 1 ? ` (${j.queue_position} in queue)` : "";
  card.replaceChildren(
    el("div", { class: "row" },
      el("span", {}, j.status === "ready" ? "Indexed " : j.status === "failed" ? "Could not index " : "Indexing ",
         el("b", { text: j.name })),
      j.status === "failed" || j.status === "ready"
        ? el("button", { type: "button", class: "link-button", text: "Dismiss", onclick: () => { card.remove(); state.jobCards.delete(j.job_id); } }) : null),
    el("div", { class: "msg", text: (j.status === "failed" ? j.error : j.message) + waiting }),
    j.status === "failed" ? null : el("div", { class: "progress" }, el("i", { style: `--progress:${j.progress}` })));
}

function jobFinished(j) {
  for (const cache of [state.graphs, state.overviews, state.positions, state.history]) cache?.delete?.(j.repo_id);
  rememberUserRepo(j.repo_id, j.name);
  const card = state.jobCards.get(j.job_id);
  if (card) {                                   // started on this page: open it
    selectRepo(j.repo_id);
    setTimeout(() => { card.remove(); state.jobCards.delete(j.job_id); }, 4000);
  }
}

/** The signed-in user's jobs from the server; polls every 2 s while any is running, then stops. */
async function syncJobs() {
  clearTimeout(jobsTimer);
  jobsTimer = null;
  if (!currentUser()) return;
  let jobs;
  try {
    jobs = await fetchJSON("/api/jobs");
  } catch {
    if ([...state.jobs.values()].some(isActive)) jobsTimer = setTimeout(syncJobs, 4000);
    return;
  }
  const latest = new Map();
  for (const j of jobs) if (!latest.has(j.repo_id)) latest.set(j.repo_id, j);       // newest first
  const finished = [...latest.values()].filter((j) => j.status === "ready" && isActive(state.jobs.get(j.repo_id)?.raw));
  state.jobs = new Map([...latest].map(([id, j]) => [id, { ...jobView(j), raw: j }]));
  jobs.forEach(renderJobCard);
  finished.forEach(jobFinished);
  for (const [id, card] of state.jobCards) {      // a job this page started that the list no longer has
    if (!jobs.some((j) => j.job_id === id) && !card.classList.contains("ready")) renderJobCard({ job_id: id, status: "failed", name: "", error: "Lost track of this job." });
  }
  if (state.view === "repos") renderRepos();
  if ([...latest.values()].some(isActive)) jobsTimer = setTimeout(syncJobs, 2000);
}

/** A job this page started: a progress card, kept up to date by syncJobs. */
function trackJob(job) {
  const card = el("div", { class: "job", "data-job": job.job_id });
  $("#jobs").append(card);
  state.jobCards.set(job.job_id, card);
  renderJobCard(job);
  state.jobs.set(job.repo_id, { ...jobView(job), raw: job });
  if (job.status === "ready") { jobFinished(job); return; }
  if (state.view === "repos") renderRepos();
  syncJobs();
}

/** Rebuild a visitor repo with the server's default version; the job card shows progress. */
async function reindexRepo(repo, button) {
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/repos/${encodeURIComponent(repo.id)}/reindex`, { method: "POST" });
    const job = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(job.error || `The server could not re-index it (error ${response.status}).`);
    trackJob(job);
  } catch (err) {
    banner(err.message, "error");
    if (button) button.disabled = false;
  }
}

/**
 * Repo facts: what GitHub records (homepage, Pages, deployments) and which platforms the config
 * files set up. Recorded, not proven: "Check if live" asks the server to try the URL now.
 */
function renderFacts(demo) {
  const f = demo.facts || {};
  const latest = (f.deployments || [])[0];
  const rows = [
    f.homepage ? ["Homepage", el("a", { href: f.homepage, target: "_blank", rel: "noopener", text: f.homepage })] : null,
    f.has_pages ? ["GitHub Pages", f.pages_url ? el("a", { href: f.pages_url, target: "_blank", rel: "noopener", text: f.pages_url }) : "enabled"] : null,
    latest ? ["Last deployment", `${latest.environment || "unknown"}: ${latest.state || "no status"}, ${formatDate(latest.created_at) || "undated"}`] : null,
    f.platforms?.length ? ["Configured for", f.platforms.map((p) => p.name).join(", ")] : null,
  ].filter(Boolean);
  if (!rows.length) return null;
  // the server only makes live checks when CODERAG_LIVE_CHECK is on (reported by /api/health)
  const canCheck = Boolean(state.health?.live_check)
    && Boolean(f.homepage || f.pages_url || (f.deployments || []).some((d) => d.environment_url));
  const result = el("span", { class: "live-result", role: "status" });
  const check = canCheck ? el("button", { type: "button", class: "link-button", text: "Check if live",
    onclick: async (e) => {
      const button = e.currentTarget;
      button.disabled = true;
      result.textContent = "Checking…";
      try {
        const r = await fetch(`/api/repos/${encodeURIComponent(demo.id)}/live`, { method: "POST" });
        const body = await r.json().catch(() => ({}));
        result.textContent = !r.ok ? body.error || `Check failed (${r.status}).`
          : body.error ? `${body.url}: ${body.error}`
          : `${body.url} answered ${body.status} in ${body.ms} ms${body.cached ? " (checked in the last 10 min)" : ""}`;
        result.dataset.ok = String(Boolean(body.ok));
      } catch {
        result.textContent = "The check could not be run.";
      } finally {
        button.disabled = false;
      }
    } }) : null;
  return el("section", { class: "repo-facts" },
    el("h3", { text: "Repo facts" }),
    el("dl", {}, rows.map(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)])),
    check ? el("div", { class: "row" }, check, " ", result) : null);
}

/** The repository's name, description and meta line above the tiles. */
function renderRepoHead(demo) {
  const head = $("#repo-head");
  if (!demo) {
    head.replaceChildren(el("div", { class: "repo-head-skeleton", "aria-hidden": "true" }, el("span"), el("span")));
    return;
  }
  const languages = Object.entries(demo.languages || {}).sort((a, b) => b[1] - a[1]);
  fill(head, 
    el("h1", {}, demo.url ? el("a", { href: `${demo.url}/tree/${demo.sha}`, target: "_blank", rel: "noopener", text: demo.name }) : demo.name),
    el("div", { class: "meta" },
      demo.sha ? el("span", {}, "commit ", el("code", { text: demo.sha.slice(0, 7) })) : null,
      demo.built && formatDate(demo.built) ? el("span", { text: `indexed ${formatDate(demo.built)}` }) : null,
      demo.indexed_with ? el("span", {}, el("code", { text: demo.indexed_with })) : null,
      languages.length ? el("span", { class: "dots", title: languages.map(([l, n]) => `${plural(n, `${l} file`)}`).join(", ") },
        languages.slice(0, 6).map(([label, n]) => el("span", { class: "lang", title: plural(n, `${label} file`) }, dot(null, languageKey(label)), label))) : null,
      demo.reindex_to ? el("button", { type: "button", class: "link-button reindex",
        title: `Indexed with ${demo.indexed_with || "an older version"}. Rebuild it with ${demo.reindex_to}: `
          + "other languages and links across them (HTTP, JNI, ctypes) are indexed too.",
        text: `Re-index with ${demo.reindex_to}`, onclick: (e) => reindexRepo(demo, e.currentTarget) }) : null),
    // the indexer's placeholder ("Indexed from GitHub.") says nothing the header doesn't
    demo.description && !/^Indexed from GitHub\b/.test(demo.description)
      ? el("p", { class: "description", text: demo.description.replace(/ -> /g, " → ") }) : null);
}

// ── the stat tiles ──────────────────────────────────────────────────────────
// Signed in: the counts come from the server's history (all your questions about the repo);
// signed out: this session's questions, kept in memory only.
function askedMeta(repoId) {
  if (currentUser()) {
    const h = state.historyMeta.get(repoId);
    return h ? { total: h.total, perDay: h.per_day } : null;
  }
  const asked = state.sessionAsked.filter((x) => x.repo === repoId);
  return { total: asked.length, perDay: questionsPerDay(asked, 7) };
}

function logAsked(repoId) {
  if (!currentUser()) {
    state.sessionAsked.push({ t: new Date().toISOString(), repo: repoId });
    return;
  }
  const h = state.historyMeta.get(repoId);
  if (h) { h.total += 1; h.per_day[h.per_day.length - 1] += 1; }    // until the server's count arrives
}

/** The signed-in user's questions about a repo (newest first), a page at a time. */
async function loadHistory(repoId, { more = false } = {}) {
  if (!currentUser() || !repoId) return null;
  const known = state.historyMeta.get(repoId);
  const before = more && known?.next ? `&before=${known.next}` : "";
  try {
    const page = await fetchJSON(`/api/repos/${encodeURIComponent(repoId)}/history?limit=20${before}`);
    state.historyMeta.set(repoId, { ...page, items: more && known ? [...known.items, ...page.items] : page.items });
  } catch { /* offline or signed out meanwhile: keep what we had */ }
  return state.historyMeta.get(repoId) || null;
}

/** A mini bar chart: one series, the current bar in the accent, each bar named on hover. */
function bars(values, { current = -1, empty = false, cat = null } = {}) {
  const max = Math.max(1, ...values.map((v) => v.value));
  return el("ul", { class: `bars${empty ? " empty" : ""}`, "aria-hidden": "true" },
    values.map((v, i) => el("li", { class: i === current ? "current" : null, title: v.title || null, style: v.cat ? `--cat:${v.cat}` : cat ? `--cat:${cat}` : null },
      el("i", { style: `--v:${(v.value / max).toFixed(3)}` }))));
}

function tile(label, value, note, chart) {
  return el("div", { class: "tile" },
    el("span", { class: "label", text: label }),
    value === null ? el("span", { class: "value pending", "aria-label": "loading" }) : el("span", { class: "value", text: value.toLocaleString() }),
    el("span", { class: "note" }, el("span", {}, note), chart));
}

function renderTiles(demo) {
  const box = $("#tiles");
  if (!demo) {
    box.replaceChildren(...["Files", "Functions", "Bridges", "Questions asked"].map((l) => tile(l, null, "", bars([]))));
    return;
  }
  const overview = state.overviews.get(demo.id);
  const graph = state.graphs.get(demo.id);
  const c = overview?.counts || demo.counts || {};
  const languages = Object.entries(demo.languages || {}).sort((a, b) => b[1] - a[1]);
  const filesNote = languages.length > 1 ? plural(languages.length, "language") : languages[0]?.[0] || "no languages recorded";
  const files = tile("Files", c.files ?? demo.counts?.files ?? 0, filesNote,
    bars(languages.slice(0, 8).map(([l, n]) => ({ value: n, title: plural(n, `${l} file`), cat: `var(--lang-${languageKey(l)})` })), { empty: !languages.length }));

  const packages = overview?.packages || [];
  const functions = tile("Functions", graph ? c.functions ?? 0 : null,
    overview ? `${plural(c.classes ?? 0, "class", "classes")}, ${plural(packages.length, "package")}` : "counting…",
    bars(packages.slice(0, 8).map((p) => ({ value: p.functions, title: `${p.name}: ${plural(p.functions, "function")}` })), { empty: !packages.length }));

  const b = bridgeCounts(graph?.links);
  const mechanisms = Object.entries(b.byMechanism).sort((x, y) => y[1] - x[1]);
  const BRIDGE_NAMES = { http: "HTTP", jni: "JNI", ctypes_cffi: "ctypes/cffi" };
  const bridgeNote = !graph ? "counting…" : b.total ? mechanisms.map(([m]) => BRIDGE_NAMES[m] || m).join(", ")
    : demo.bridges_checked ? (languages.length > 1 ? "none found" : "one language") : "not checked (re-index)";
  const bridges = tile("Bridges", graph ? b.total : null, bridgeNote,
    bars(mechanisms.map(([m, n]) => ({ value: n, title: `${BRIDGE_NAMES[m] || m}: ${plural(n, "bridge")}` })), { empty: !b.total, cat: "var(--cat-bridge)" }));

  const asked = askedMeta(demo.id);
  const perDay = asked?.perDay || [0, 0, 0, 0, 0, 0, 0];
  const today = perDay[perDay.length - 1];
  const dayName = (back) => { const d = new Date(); d.setDate(d.getDate() - back); return d.toLocaleDateString(undefined, { weekday: "short" }); };
  const questions = tile("Questions asked", asked ? asked.total : null,
    !asked ? "counting…" : today ? el("b", { class: "up", text: `+${today} today` })
      : asked.total ? "none today" : currentUser() ? "none yet" : "this session",
    bars(perDay.map((n, i) => ({ value: n, title: `${dayName(6 - i)}: ${plural(n, "question")}` })), { current: 6, empty: !asked?.total }));
  box.replaceChildren(files, functions, bridges, questions);
}

// ── the chat's first card: the repository's shape ───────────────────────────
function renderRepoCard(demo) {
  const overview = state.overviews.get(demo.id);
  const c = overview?.counts;
  const generated = overview ? suggestedQuestions(overview) : [];
  const questions = demo.questions.length ? demo.questions
    : generated.length ? generated : state.graphs.has(demo.id) ? GENERIC_QUESTIONS : [];
  const card = $("#repo-card");
  const intro = c ? `${plural(c.files, "file")}, ${plural(c.functions, "function")} and ${plural(c.classes, "class", "classes")}. Ask anything about this code; every answer cites the lines it came from.`
    : state.graphs.has(demo.id) ? "Ask anything about this code; every answer cites the lines it came from."
    : "Reading the call graph…";
  fill(card, 
    el("h2", { text: demo.name }),
    el("p", { class: "intro", text: intro }),
    overview ? renderOverview(overview) : null,
    renderFacts(demo),
  );
  $("#suggest").replaceChildren(...questions.map((q) => el("button", {
    type: "button", class: "chip", text: q, title: q, onclick: () => ask(q),
  })));
  syncRepoCard();
}

async function repoInfo(id) {
  // curated demo by id ("click") or GitHub name ("pallets/click"), else a visitor repo id
  const demo = state.demos.find((d) => d.id === id || d.name === id);
  if (demo) return demo;
  const known = state.userRepos.find((r) => r.id === id || r.name === id);
  try {
    return await loadRepo(known ? known.id : id);
  } catch (err) {
    if (err.status === 409 || err.status === 503 || err.status === "rebuild") {
      // being rebuilt, or the rebuild needs a fresh sign-in: keep it in the list
      if (err.status !== "rebuild") banner(err.message, "error");
      return null;
    }
    if (known) {
      removeUserRepo(known.id);
      banner(`${known.name} is no longer indexed on the server. Add it again to ask about it.`);
    }
    return null;
  }
}

/**
 * GET /api/repos/<id>. While the server rebuilds an index whose files were lost
 * (202 {rebuilding, job}), show "Rebuilding index…" with the job's progress and
 * ask again every 2 s; a failed rebuild throws with its error.
 */
async function loadRepo(id) {
  for (;;) {
    const response = await fetch(`/api/repos/${encodeURIComponent(id)}`);
    if (response.status !== 202) {
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw Object.assign(new Error(body.error || `${response.status} /api/repos/${id}`), { status: response.status });
      }
      if (rebuildingBanner) { banner(""); rebuildingBanner = false; }
      return response.json();
    }
    const info = await response.json();
    const job = info.job || {};
    if (job.status === "failed") {
      rebuildingBanner = false;
      banner(`Rebuilding the index of ${info.name || id} failed: ${job.error || "unknown error"}`, "error");
      throw Object.assign(new Error(job.error || "rebuild failed"), { status: "rebuild" });
    }
    const percent = Math.round((job.progress || 0) * 100);
    const stage = (job.message || "").replace(/^Rebuilding index…\s*/, "");
    banner(`Rebuilding index… ${info.name || id}: ${percent}%${stage ? ` · ${stage}` : ""}`);
    rebuildingBanner = true;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}
let rebuildingBanner = false;

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
  state.selected = null;
  renderNodePanel(null);
  renderRepoMenu();
  renderRepoHead(demo);
  renderTiles(demo);
  renderRepoCard(demo);
  renderThumbs();
  updateSavedCount();
  loadHistory(demo.id).then(() => {           // the tile's count and the thumbnails come from history
    if (state.demo === demo) { renderTiles(demo); renderThumbs(); updateSavedCount(); }
  });
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
    renderTiles(demo);
    if (state.selected === null) renderNodePanel(null);
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

/** The send button turns into Stop while an answer is being written. */
function setAskBusy(busy) {
  const button = $("#ask-button");
  button.classList.toggle("stop", busy);
  button.setAttribute("aria-label", busy ? "Stop" : "Send");
  button.title = busy ? "Stop writing this answer" : "Send (Enter)";
}

/** A skeleton the shape of the answer to come, with what the system is doing. */
function answerSkeleton(saying) {
  return el("div", { class: "status", role: "status" },
    el("span", { class: "skeleton-line" }), el("span", { class: "skeleton-line" }), el("span", { class: "skeleton-line" }),
    el("span", { class: "saying", text: saying }));
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy || !state.demo) return;
  state.busy = true;
  state.hintDone = true;                 // the graph's "ask a question" hint has done its job
  state.controller = new AbortController();
  setAskBusy(true);
  $("#question").value = "";
  autosize();
  if (isNarrow()) setView("ask");
  updateUrl({ repo: state.demo.id, q: question });
  const repo = state.demo;               // the answer belongs here even if the visitor switches
  $("#conversation .cleared")?.remove();
  logAsked(repo.id);
  renderTiles(repo);

  const turnId = ++state.turn;
  const status = answerSkeleton("Searching the code…");
  const body = el("div", { class: "body" });
  const answer = el("div", { class: "answer" }, status, body);
  const turn = el("article", { class: "turn", "data-turn": turnId }, el("div", { class: "question", text: question }), answer);
  const conversation = $("#conversation");
  conversation.append(turn);
  syncRepoCard();
  turn.scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "start" });

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
            drawGraph();
          } else if (state.chats.has(repo.id)) {
            state.chats.get(repo.id).evidence = found;          // visitor switched away
          }
          status.querySelector(".saying").textContent = "Writing the answer…";
        } else if (event.type === "token") {
          text += data.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (event.type === "done") {
          cancelAnimationFrame(frame);
          paint();
          status.remove();
          const cited = citedSources(text, sources.length);
          showCited(turn, repo.id, sources, cited);
          const seconds = (data.llm_ms / 1000).toFixed(1);
          const copy = el("button", { type: "button", class: "link-button", text: "Copy answer",
            onclick: () => copyText(normalizeCitations(text), copy) });
          if (data.question_id) turn.dataset.question = data.question_id;
          else state.sessionHistory.unshift({ repoId: repo.id, repoName: repo.name, question, answer: normalizeCitations(text),
            backend: data.backend, at: new Date().toISOString(), sources: sources.map((s) => slimSource(s, cited.includes(s.id))) });
          answer.insertBefore(el("div", { class: "meta" },
            el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: data.backend }),
              data.cached ? "from the cache" : el("span", { class: "measured", text: `${seconds} s` })),
            el("span", { class: "actions" }, copy,
              data.question_id ? shareControls(data.question_id, repo) : null,
              data.question_id ? feedbackButtons(data.question_id) : null)), answer.querySelector(".sources"));
          if (data.question_id) loadHistory(repo.id).then(() => { if (state.demo?.id === repo.id) { renderTiles(repo); renderThumbs(); updateSavedCount(); } });
          else updateSavedCount();
          announce(cited.length ? `Answer ready. It cites ${sourceList(cited)}.`
            : sources.length ? `Answer ready. It cites none of the ${sources.length} sources.` : "Answer ready.");
          if (sources.length) rememberAnswer(repo.id, turn, data.question_id);
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

/** Source rows: each is a button that selects its node, opens the panel and the overlay. */
function renderSources(sources, { graph = true } = {}) {
  const rows = sources.map((s) => {
    const row = el("button", { type: "button", class: "source", "data-sid": s.id, "data-chunk": s.chunk_id,
      "data-file": s.file, "data-start": s.start_line, "data-end": s.end_line,
      title: `${s.name}\n${formatLocation(s)}${relationLabel(s) ? `\n${relationLabel(s)}` : ""}`,
      onclick: () => (graph ? selectNode(s.chunk_id, { open: true, source: s }) : null) },
      el("span", { class: "sid", "data-relation": relationOf(s), title: relationWord(s), text: `S${s.id}` }),
      el("span", { class: "what" },
        el("span", { class: "name" }, el("span", { text: middleTruncateName(s.name, 40) }), languageTag(s.language)),
        el("span", { class: "where" },
          el("span", { title: formatLocation(s), text: `${middleTruncate(s.file)}${s.start_line ? `:${s.start_line}-${s.end_line}` : ""}` }),
          // a direct match is the default and says nothing; a graph source names its path
          relationLabel(s) ? el("span", { class: "hop", "data-relation": relationOf(s), "data-confidence": s.confidence || null,
            "data-bridge": s.bridge?.mechanism || null,
            title: s.bridge ? bridgeEvidence(s.bridge)
              : `${viaLabel(s)}. ${hopHelp(s.hop)}${s.confidence === "name_match" ? " Linked by name only, not resolved." : ""}` },
          relationLabel(s)) : null),
        // debug mode: how it was retrieved, visible without expanding the row
        s.debug ? el("span", { class: "debug", text: debugLine(s.debug) }) : null));
    if (graph) {
      for (const [on, off] of [["mouseenter", "mouseleave"], ["focus", "blur"]]) {
        row.addEventListener(on, () => graphView?.highlight(s.chunk_id));
        row.addEventListener(off, () => graphView?.highlight(null));
      }
    }
    return row;
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

/** The relation as a word, for titles, the node panel and screen readers. */
function relationWord(s) {
  return RELATION_WORDS[relationOf(s)];
}

// bridge edges (v10): code reached across a language or process boundary
const BRIDGE_LABELS = { http: "HTTP", jni: "JNI", ctypes_cffi: "ctypes/cffi" };

/** "HTTP POST /api/repos ← app.js:412": a bridge's evidence, one line per fact. */
function bridgeEvidence(b) {
  const e = b.evidence || {};
  const matched = (e.matched || []).join("  →  ");
  return [`${b.label} ${e.src ? `← ${e.src}` : ""}`.trim(), `${b.confidence === "exact" ? "exact match" : "pattern match"}`, matched,
    e.dst ? `implemented at ${e.dst}` : ""].filter(Boolean).join("\n");
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

/** Once the answer is written: cited sources first, the rest folded under "Also retrieved". */
function groupSources(section, cited) {
  const rows = [...section.querySelectorAll(".source")];
  const isCited = (row) => cited.includes(Number(row.dataset.sid));
  for (const row of rows) row.querySelector(".sid").classList.toggle("cited", isCited(row));
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

// ── history: your questions, from the server (signed in) or this session (signed out) ──
function slimSource(s, keepCode) {   // what a session answer keeps of a source: code only if cited
  const { id, chunk_id, name, kind, file, start_line, end_line, hop, via, relation, url, code, language, bridge } = s;
  return { id, chunk_id, name, kind, file, start_line, end_line, hop, via, relation, url, language, bridge,
           cited: keepCode, code: keepCode ? code : undefined };
}

const THUMB_UP = "M7 11v10M7 11l4-8a2 2 0 0 1 3.5 1.6L13.5 9H19a2 2 0 0 1 2 2.3l-1.4 8A2 2 0 0 1 17.6 21H7";
const THUMB_DOWN = "M17 13V3M17 13l-4 8a2 2 0 0 1-3.5-1.6l1-4.4H5a2 2 0 0 1-2-2.3l1.4-8A2 2 0 0 1 6.4 3H17";

/** Thumbs up/down on an answer, stored on the server (POST /api/questions/{id}/feedback). */
function feedbackButtons(questionId, rating = null) {
  const group = el("span", { class: "feedback", role: "group", "aria-label": "Was this answer useful?" });
  const sync = (value) => group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.rating === value)));
  const button = (value, label, d) => el("button", { type: "button", class: "icon-button", "data-rating": value,
    "aria-label": label, title: label, "aria-pressed": String(rating === value),
    onclick: async () => {
      sync(value);
      const r = await fetch(`/api/questions/${questionId}/feedback`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating: value }) }).catch(() => null);
      if (!r?.ok) { sync(rating); announce("Your feedback could not be saved."); return; }
      rating = value;
      announce(value === "up" ? "Marked as helpful." : "Marked as not helpful.");
    } }, svgIcon(d));
  group.append(button("up", "Helpful", THUMB_UP), button("down", "Not helpful", THUMB_DOWN));
  return group;
}

/** Share: a public read-only link to this answer (/a/<slug>); private repos ask first. */
function shareControls(questionId, repo, existing = null) {
  let slug = existing;
  const wrap = el("span", { class: "share" });
  const status = el("span", { class: "share-status", role: "status" });
  const link = () => `${window.location.origin}/a/${slug}`;
  const share = el("button", { type: "button", class: "link-button", text: slug ? "Copy link" : "Share" });
  const revoke = el("button", { type: "button", class: "link-button danger", text: "Revoke link", hidden: !slug });
  const create = async (confirm) => {
    status.replaceChildren();
    share.disabled = true;
    try {
      const r = await fetch(`/api/questions/${questionId}/share`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm_private: confirm }) });
      const body = await r.json().catch(() => ({}));
      if (r.status === 409) { askConfirm(); return; }
      if (!r.ok) throw new Error(body.error || `Couldn't share it (error ${r.status}).`);
      slug = body.slug;
      share.textContent = "Copy link";
      revoke.hidden = false;
      await copyText(link(), share);
      status.textContent = "Anyone with the link can read this answer.";
    } catch (err) {
      status.textContent = err.message;
    } finally {
      share.disabled = false;
    }
  };
  const askConfirm = () => status.replaceChildren(
    "This repository is private: the link shows this answer and the code it cites. ",
    el("button", { type: "button", class: "link-button", text: "Share anyway", onclick: () => create(true) }), " ",
    el("button", { type: "button", class: "link-button", text: "Cancel", onclick: () => status.replaceChildren() }));
  share.addEventListener("click", () => (slug ? copyText(link(), share) : repo?.private ? askConfirm() : create(false)));
  revoke.addEventListener("click", async () => {
    const r = await fetch(`/api/questions/${questionId}/share`, { method: "DELETE" }).catch(() => null);
    if (!r?.ok) { status.textContent = "Couldn't revoke the link."; return; }
    slug = null;
    share.textContent = "Share";
    revoke.hidden = true;
    status.textContent = "Link revoked.";
  });
  wrap.append(share, revoke, status);
  return wrap;
}

function updateSavedCount() {
  const id = state.demo?.id;
  const n = currentUser() ? state.historyMeta.get(id)?.total || 0 : state.sessionHistory.filter((r) => r.repoId === id).length;
  const badge = $("#saved-count");
  badge.textContent = n ? String(n) : "";
  badge.hidden = !n;
}

function setupSaved() {
  try { OLD_LOCAL_KEYS.forEach((k) => localStorage.removeItem(k)); } catch { /* storage blocked */ }
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
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    row.setAttribute("aria-current", "true");
    setTimeout(() => row.removeAttribute("aria-current"), 1200);
  });
  updateSavedCount();
}

function historyItemView(r) {
  const body = el("div", { class: "body" });
  body.innerHTML = renderAnswer(r.answer, r.sources.length, renderers);
  const copy = el("button", { type: "button", class: "link-button", text: "Copy answer",
    onclick: () => copyText(r.answer, copy) });
  const sources = renderSources(r.sources, { graph: false });
  groupSources(sources, citedSources(r.answer, r.sources.length));
  const at = r.created_at || r.at;
  return el("details", { class: "saved-item" },
    el("summary", {},
      el("span", { class: "q", text: r.question }),
      el("span", { class: "where" }, el("span", { text: r.repoName || state.demo?.name || "" }),
        el("time", { datetime: at, text: `Asked ${formatDate(at) || "earlier"}` }))),
    el("div", { class: "answer" },
      body,
      el("div", { class: "meta" },
        el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: r.model || r.backend || "" })),
        el("span", { class: "actions" },
          r.id ? el("button", { type: "button", class: "link-button", text: "Show here",
            onclick: () => { $("#saved").close(); restoreHistory(r); } })
            : el("button", { type: "button", class: "link-button", text: "Open repo", onclick: () => openSavedRepo(r) }),
          copy,
          r.id ? shareControls(r.id, state.demo, r.share) : null,
          r.id ? feedbackButtons(r.id, r.rating) : null)),
      sources));
}

async function renderSaved() {
  const box = $("#saved-list");
  const note = $("#saved .note");
  if (!currentUser()) {
    note.textContent = "Kept for this session only. Sign in with GitHub to keep your questions.";
    const list = state.sessionHistory;
    box.replaceChildren(...(list.length ? list.map(historyItemView)
      : [el("p", { class: "note", text: "No answers yet in this session." })]));
    return;
  }
  note.textContent = `Your questions about ${state.demo?.name || "this repository"}, kept on your account.`;
  box.replaceChildren(el("p", { class: "note", text: "Loading…" }));
  const h = await loadHistory(state.demo?.id);
  const items = h?.items || [];
  const more = h?.next ? el("button", { type: "button", class: "secondary small", text: "Load more",
    onclick: async () => { await loadHistory(state.demo.id, { more: true }); renderSaved(); } }) : null;
  box.replaceChildren(...(items.length ? items.map(historyItemView)
    : [el("p", { class: "note", text: "No questions yet. Ask one and it will be kept here." })]), ...(more ? [more] : []));
}

async function openSavedRepo(record) {
  $("#saved").close();
  const info = await repoInfo(record.repoId);
  if (!info) {
    banner(`${record.repoName} is no longer indexed on the server. Add it again to ask new questions.`);
    return;
  }
  if (info.kind === "user") rememberUserRepo(info.id, info.name, info);
  await selectRepo(info.id);
}

/** Once there is a conversation the repository card folds away; suggestions stay above the input. */
function syncRepoCard() {
  const hasTurns = Boolean($("#conversation .turn"));
  $("#repo-card").classList.toggle("compact", hasTurns);
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
  selectNode(null);
  drawGraph();
  syncRepoCard();
  conversation.querySelector(".cleared")?.remove();
  const note = el("p", { class: "cleared" }, "Chat cleared.",
    el("button", { type: "button", class: "link-button", text: "Undo", onclick: () => {
      note.remove();
      if (state.demo?.id !== repoId || conversation.querySelector(".turn")) return;
      conversation.append(...turns);
      setEvidence(kept);
      drawGraph();
      syncRepoCard();
      $("#question").focus();
    } }));
  $("#repo-card").after(note);
  note.querySelector("button").focus();
  setTimeout(() => note.remove(), 12000);
}

/** Scroll to a source row in an answer and mark it; the node and the panel follow. */
function openSource(turn, n) {
  const row = turn?.querySelector(`.source[data-sid="${n}"]`);
  if (!row) return;
  const group = row.closest("details.also");
  if (group) group.open = true;
  if (isNarrow()) setView("ask");
  row.scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "center" });
  selectNode(Number(row.dataset.chunk));
}

// ── selection: one state shared by the source row, the graph node and the panel ──
let graphView = null;

/** The drawn node for an id, from the graph or the collapsed module graph. */
function nodeById(id) {
  if (id === null || id === undefined) return null;
  return graphView?.byId.get(id) || state.graph?.nodes.find((n) => n.id === id) || null;
}

function selectNode(id, { open = false, source = null } = {}) {
  state.selected = id;
  const node = nodeById(id);
  document.querySelectorAll(".source[aria-current]").forEach((r) => r.removeAttribute("aria-current"));
  if (id !== null && state.answerTurn) {
    state.answerTurn.querySelector(`.source[data-chunk="${id}"]`)?.setAttribute("aria-current", "true");
  }
  graphView?.node.classed("selected", (n) => n.id === id);
  graphView?.relabel();
  renderNodePanel(node, source);
  if (open && node) openFocus(node);
  else if (open && source) openFocus({ ...source, id: source.chunk_id, name: source.name, kind: source.kind || "function" });
}

// ── graph ───────────────────────────────────────────────────────────────────
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
  const languages = Object.keys(state.demo?.languages || {}).filter((l) => l !== "HTML");
  noBridgesNote().hidden = !(state.graph && state.demo?.bridges_checked && languages.length > 1 && !mechanisms.length);
  bridges.querySelector("span").textContent = `Bridge: ${mechanisms.map((m) => BRIDGE_LABELS[m] || m).join(" / ")}`;
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

/** Said when a repo mixes languages but no link between them was found (bridges were looked for). */
function noBridgesNote() {
  let item = $("#legend-no-bridges");
  if (!item) {
    item = el("li", { id: "legend-no-bridges", class: "legend-note",
      text: "No cross-language links found (supported: JNI, ctypes, HTTP via fetch/axios/forms)." });
    bridgeLegend().after(item);
  }
  return item;
}

/** The bridge key: a dash-dot line drawn as SVG, the same stroke the graph uses. */
function bridgeKey() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "key-line key-line--bridge");
  svg.setAttribute("viewBox", "0 0 18 4");
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  for (const [k, v] of Object.entries({ x1: 0, y1: 2, x2: 18, y2: 2, "stroke-dasharray": "6 2 1.5 2" })) line.setAttribute(k, v);
  svg.append(line);
  return svg;
}

/** The legend key for bridge edges ("Bridge: HTTP / JNI"), added once. */
function bridgeLegend() {
  let item = $("#legend-bridge");
  if (!item) {
    item = el("li", { id: "legend-bridge", title: "Edges across a language or process boundary: an HTTP call to its route, "
      + "a JNI declaration to its C function, a ctypes/cffi call to its C function. Point at one to see its evidence." },
    bridgeKey(), el("span"));
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

/** "0", "1", "2", "3" (3 or more) for the hop scale, or null for a neighbour. */
function hopOf(n) {
  const hop = state.retrieved.get(n.id);
  return hop === undefined ? null : String(Math.min(hop, 3));
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
  graphView.node.attr("class", nodeClass).attr("data-hop", hopOf).attr("aria-label", nodeName).classed("selected", (n) => n.id === state.selected);
  if (state.selected !== null) renderNodePanel(nodeById(state.selected));
  // the answer is written: if its graph is on screen, its paths flow once
  if (state.cited && state.trailsShown !== state.answerTurn) {
    requestAnimationFrame(() => {
      if (!graphView || state.trailsShown === state.answerTurn || !$("#graph").getBoundingClientRect().width) return;
      state.trailsShown = state.answerTurn;
      graphView.playTrails();
    });
  }
}

// ── the chat's overview card: packages, most called, entry points ───────────
function renderOverview(o) {
  const codeButton = (n, text, note, extra = "") => el("li", {},
    el("button", { type: "button", class: `fact ${extra}`.trim(), title: `${n.label || n.name}\n${formatLocation(n)}`,
      onclick: () => selectNode(n.id, { open: true }) },
      el("code", { text }), note ? el("span", { class: "note", text: note }) : null));
  const group = (title, items) => el("section", { class: "facts" }, el("h3", { text: title }), el("ul", {}, items));
  const label = (n) => n.label || n.name;
  const groups = [];
  if (o.packages.length > 1) {
    groups.push(group("Packages", o.packages.map((p) => el("li", {},
      el("button", { type: "button", class: "fact", onclick: () => ask(p.name === "top level"
        ? "What do the files at the top level of this repository do?"
        : `What does the ${p.name.replace(/\/$/, "")} part of the code do?`) }, el("code", { text: p.name }),
      el("span", { class: "note", text: plural(p.functions, "function") }))))));
  }
  if (o.mostCalled.length) {
    groups.push(group("Most called", o.mostCalled.map((x) => codeButton(x.node, label(x.node), `× ${x.callers}`))));
  }
  if (o.entryPoints.length) {
    groups.push(group("Entry points", o.entryPoints.map((n) => codeButton(n, n.name, middleTruncate(n.file, 24)))));
  }
  if (o.hierarchy.length >= 3 || (o.hierarchy.length === 2 && groups.length < 3)) {
    const leaf = o.hierarchy.at(-1);
    groups.push(group("Deepest hierarchy", [codeButton(leaf, o.hierarchy.map((n) => n.name).join(" → "), null, "wrap")]));
  }
  return groups.length ? el("div", { class: "overview" }, groups) : null;
}

// ── the graph engine: one live force layout that remembers where things were ─
const MOTION_LIMIT = 600;                 // nodes: above this, views change without movement
const HULL_LIMIT = 150;                   // nodes: above this, no file outlines
const RING_RADII = [80, 185, 290, 395];   // By distance: direct match, 1 call, 2+ calls, neighbours
const RING_NAMES = ["direct match", "1 call away", "2+ calls away", "neighbours"];
const MORPH_MS = 560;                     // the authored moment: one view becoming the next
const ZOOMED_IN = 1.35;                   // past this scale, every function shows its name
const calm = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const jitter = (id, salt) => (((hashId(id) * (salt ? 7919 : 104729)) % 97) / 97) - 0.5;
const hashId = (id) => (typeof id === "number" ? id + 1 : [...String(id)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 100003, 7));
const nodeId = (end) => (typeof end === "object" ? end.id : end);
const pairKey = (a, b) => (String(a) < String(b) ? `${a}-${b}` : `${b}-${a}`);
const SHAPES = { function: "circle", method: "circle", class: "diamond", file: "square", module: "square", template: "triangle", config: "wye" };

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

/** The symbol path for a node: shape by kind, area by radius. */
function symbolPath(kind, r) {
  const shape = SHAPES[kind] || "circle";
  const type = { circle: d3.symbolCircle, diamond: d3.symbolDiamond, square: d3.symbolSquare, triangle: d3.symbolTriangle, wye: d3.symbolWye }[shape];
  return d3.symbol().type(type).size(Math.PI * r * r * (shape === "circle" ? 1 : 1.15))();
}

function drawGraph() {
  syncLegend();
  const svg = window.d3 && d3.select("#graph");
  const hint = $("#graph-hint");
  const toggle = $("#graph-toggle");
  const subtitle = $("#graph-subtitle");
  if (!svg) { hint.textContent = "The graph library could not be loaded."; return; }
  graphView?.stop();
  graphView = null;
  const graph = state.graph;
  const answer = state.retrieved.size > 0 && state.graphMode !== "full";
  $("#layout-toggle").hidden = !answer;
  $("#layout-toggle").setAttribute("aria-pressed", String(state.graphLayout === "rings"));
  if (!graph) {
    svg.selectAll("g.scene").remove();
    hint.textContent = state.demo ? "No graph for this repository yet." : "Choose a repository to see its call graph.";
    hint.classList.remove("caption");
    toggle.hidden = true;
    subtitle.textContent = "";
    $("#tray").hidden = true;
    return;
  }

  const small = graph.nodes.length <= FULL_GRAPH_LIMIT;
  let data;
  let collapsed = false;
  if (state.graphMode === "full") data = graph;
  else if (state.retrieved.size) data = focusSubgraph(graph, state.retrieved);
  else if (small) data = graph;
  else { data = moduleGraph(graph); collapsed = true; }      // a large repository: its modules

  toggle.hidden = small && !state.retrieved.size;
  toggle.setAttribute("aria-pressed", String(state.graphMode === "full"));
  const toggleLabel = state.graphMode === "full"
    ? (state.retrieved.size ? "Show the answer's subgraph" : "Show modules only")
    : `Show the full graph (${plural(graph.nodes.length, "node")})`;
  toggle.setAttribute("aria-label", toggleLabel);
  toggle.title = toggleLabel;
  subtitle.textContent = state.graphMode === "full" ? plural(graph.nodes.length, "node")
    : state.retrieved.size ? `${plural(state.retrieved.size, "source")} and their neighbours`
    : collapsed ? `${plural(data.nodes.length, "module")}, collapsed` : plural(graph.nodes.length, "node");
  // the hint: a pill in the canvas's top-left corner until the first question or the first touch
  const caption = !state.retrieved.size && state.graphMode !== "full" && !state.hintDone;
  hint.classList.toggle("caption", Boolean(caption));
  hint.textContent = caption ? "Ask a question to light up the code it uses. Point at a node to see its calls." : "";

  const { width, height } = svg.node().getBoundingClientRect();
  if (!width || !height) return;                       // hidden pane: drawn when shown, from where it was
  // the graph is laid out and fitted below the pill, so the two never meet
  const top = caption ? hint.offsetTop + hint.offsetHeight + 8 : 10;
  const bottom = 10;
  const midY = top + (height - top - bottom) / 2;

  const rings = answer && state.graphLayout === "rings";
  const positions = positionsFor(`${state.demo?.id || "_"}${collapsed ? ":modules" : ""}`);
  // isolated nodes wait in the tray (the answer's own sources always stay in the picture)
  const linked = new Set();
  for (const l of data.links) { linked.add(l.source); linked.add(l.target); }
  const isolated = data.nodes.filter((n) => !linked.has(n.id) && !state.retrieved.has(n.id));
  renderTray(isolated);
  const nodes = data.nodes.filter((n) => linked.has(n.id) || state.retrieved.has(n.id)).map((n) => ({ ...n }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = data.links.map((l) => ({ ...l }));
  const big = nodes.length > 250;
  const moving = !calm() && nodes.length <= MOTION_LIMIT;
  const outlined = !rings && !collapsed && nodes.length <= HULL_LIMIT;
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
    degree.set(l.source, (degree.get(l.source) || 0) + (l.count || 1));
    degree.set(l.target, (degree.get(l.target) || 0) + (l.count || 1));
  }
  const radius = (n) => (state.retrieved.has(n.id)
    ? 7 + (n.kind === "class" ? 2 : 0)
    : (collapsed ? 5 : 3) + Math.min(Math.sqrt(degree.get(n.id) || 0) * 0.9, 4) + (n.kind === "class" || n.kind === "file" ? 1.5 : 0));
  // modules, the answer's sources and (in small views) everything carry a label; the rest show theirs zoomed in
  const isModule = (n) => n.kind === "file" || n.kind === "module";
  const labelled = (n) => state.retrieved.has(n.id) || isModule(n) || nodes.length <= (state.retrieved.size ? 12 : 30);
  const labelText = (n) => middleTruncateName(n.label || n.name, 34);          // shortest unique qualified name
  const sidText = (n) => (state.sids.has(n.id) ? `S${state.sids.get(n.id)} ` : "");
  const labelWidth = (n) => (labelled(n) ? (sidText(n) + labelText(n)).length * 7.4 + 12 : 0);
  // rings shrink to the canvas, so the outer ring (and its nodes) stays inside it
  const ringScale = Math.min(1, Math.max(0.3, (Math.min(width, height - top - bottom) / 2 - 24) / RING_RADII[3]));
  const ringR = (i) => RING_RADII[i] * ringScale;
  const ringRadius = (n) => {
    const hop = state.retrieved.get(n.id);
    return ringR(hop === undefined ? 3 : Math.min(hop, 2));
  };
  // every node, with its label's padding, stays inside the canvas (the band under the hint excluded)
  const EDGE = 6;
  const bounded = (n, x, y) => {
    const r = radius(n);
    const half = Math.max(r, labelWidth(n) / 2) + EDGE;
    const under = r + (labelled(n) ? 22 : 0) + EDGE;
    return [Math.min(Math.max(x, half), Math.max(half, width - half)),
            Math.min(Math.max(y, top + r + EDGE), Math.max(top + r + EDGE, height - bottom - under))];
  };
  const clampAll = () => { for (const n of nodes) [n.x, n.y] = bounded(n, n.x, n.y); };

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
  el.classList.remove("hovering", "zoomed-in", "zoomed-out");
  const subject = (n) => overview || state.retrieved.has(n.id);

  // arrowheads coloured by CSS, so a theme change needs no redraw
  if (svg.select("defs").empty()) {
    const defs = svg.append("defs");
    for (const id of ["arrow", "arrow-lit", "arrow-trail", "arrow-bridge"]) {
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
  let zoomTimer = null;
  const dismissHint = () => {
    if (state.hintDone) return;
    state.hintDone = true;
    hint.textContent = "";
    hint.classList.remove("caption");
  };
  const zoom = d3.zoom().extent([[0, 0], [width, height]]).scaleExtent([0.1, 4]).on("zoom", (e) => {
    if (e.sourceEvent) dismissHint();                    // the user's own pan or zoom, not a fit
    root.attr("transform", e.transform);
    el.style.setProperty("--label-scale", String(labelScale(e.transform.k)));
    el.style.setProperty("--label-scale-all", String(Math.max(1, 1 / e.transform.k)));
    // zoomed out, only the answer's own labels stay; zoomed in, every function shows its name
    el.classList.toggle("zoomed-out", e.transform.k < 0.96);
    const zoomedIn = e.transform.k >= ZOOMED_IN;
    if (zoomedIn !== el.classList.contains("zoomed-in")) {
      el.classList.toggle("zoomed-in", zoomedIn);
      if (zoomedIn) placeQuiet(new Set(nodes.map((n) => n.id)), { hideOverlaps: true });
    } else if (zoomedIn) {
      clearTimeout(zoomTimer);
      zoomTimer = setTimeout(() => placeQuiet(new Set(nodes.map((n) => n.id)), { hideOverlaps: true }), 120);
    }
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("dblclick.fit", (e) => { if (e.target === el) fit(true); });   // double-click the background: fit
  svg.on("pointerdown.hint keydown.hint", dismissHint);

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
    .attr("data-hop", hopOf)
    .attr("data-kind", (n) => n.kind || null)
    .attr("data-relation", (n) => relationOfNode(n.id))
    .attr("data-language", (n) => n.language || null)
    .classed("off", (n) => hiddenRelations.has(relationOfNode(n.id)))
    .classed("selected", (n) => n.id === state.selected)
    .attr("role", "button")
    .attr("aria-label", nodeName)
    .attr("tabindex", (n) => (n === order[0] ? 0 : -1))
    .call(d3.drag()
      .on("start", (e, d) => { if (timer) finish(); if (!e.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { [d.fx, d.fy] = bounded(d, e.x, e.y); })
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
  node.append("path").attr("class", "ring").attr("d", (n) => symbolPath(n.kind, radius(n) + 4));
  node.append("path").attr("class", "dot").attr("d", (n) => symbolPath(n.kind, radius(n)));
  node.append("title").text((n) => `${n.label || n.name}\n${formatLocation(n)}${n.language ? `\n${LANGUAGE_LABELS[n.language] || n.language}` : ""}${collapsed ? `\n${plural(n.members, "symbol")}` : ""}`);
  const label = node.filter(labelled).append("text").attr("class", "label").attr("text-anchor", "middle")
    .attr("dominant-baseline", "hanging").attr("y", (n) => radius(n) + 4);
  label.filter((n) => state.sids.has(n.id)).append("tspan").attr("class", "label-sid").text(sidText);
  label.append("tspan").text(labelText);
  // every other node keeps a quiet label, shown zoomed in or while it or a neighbour is pointed at
  node.filter((n) => !labelled(n)).append("text").attr("class", "label quiet").attr("text-anchor", "middle")
    .attr("dominant-baseline", "hanging").attr("y", (n) => radius(n) + 4).text(labelText);

  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id).distance((l) => (l.type === "member" ? 28 : collapsed ? 90 : 70)).strength(rings ? 0.06 : 0.5))
    .force("charge", d3.forceManyBody().strength(big ? -35 : rings ? -150 : state.retrieved.size ? -260 : -170))
    .force("center", rings ? null : d3.forceCenter(width / 2, midY))
    // gentle gravity keeps disconnected parts of the graph from drifting apart
    .force("x", rings ? null : d3.forceX(width / 2).strength(big ? 0.12 : 0.07))
    .force("y", rings ? null : d3.forceY(midY).strength(big ? 0.12 : 0.07))
    // the closer to the centre, the more directly the code was found
    .force("ring", rings ? d3.forceRadial(ringRadius, width / 2, midY).strength(0.85) : null)
    .force("cluster", outlined ? forceCluster(0.3) : null)
    // labels take room too: keep labelled nodes further apart
    .force("collide", d3.forceCollide().radius((n) => Math.min(Math.max(radius(n), labelWidth(n) / 2), 120) + 6))
    .stop();
  for (let i = 0; i < 300; i++) sim.tick();           // lay out before the first paint
  // then into the canvas: scaled as a whole when it is larger (its shape kept), then every node clamped
  if (!rings) {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const n of nodes) {
      const half = Math.max(radius(n), labelWidth(n) / 2) + EDGE;
      x0 = Math.min(x0, n.x - half); x1 = Math.max(x1, n.x + half);
      y0 = Math.min(y0, n.y - radius(n) - EDGE); y1 = Math.max(y1, n.y + radius(n) + (labelled(n) ? 22 : 0) + EDGE);
    }
    const s = Math.min(1, width / Math.max(x1 - x0, 1), (height - top - bottom) / Math.max(y1 - y0, 1));
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    for (const n of nodes) { n.x = width / 2 + (n.x - cx) * s; n.y = midY + (n.y - cy) * s; }
  }
  clampAll();

  // rings: faint guides, named where they cross the top
  const ringIndex = (n) => { const hop = state.retrieved.get(n.id); return hop === undefined ? 3 : Math.min(hop, 2); };
  let ringText = d3.select(null);
  if (rings) {
    const used = [...new Set(nodes.map(ringIndex))].sort();
    const guide = ringLayer.selectAll("g").data(used).join("g");
    guide.append("circle").attr("cx", width / 2).attr("cy", midY).attr("r", ringR);
    ringText = guide.append("text").attr("x", width / 2).attr("y", (i) => midY - ringR(i) - 6).text((i) => RING_NAMES[i]);
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
    const back = l.type === "calls" || l.type === "bridge" ? radius(t) + 2.5 : 0;
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
  let pointed = null;                                  // the hovered node: its label outranks all but cited ones
  const highlight = (id) => {
    if (id === null || id === undefined || !byId.has(id)) {
      el.classList.remove("hovering");
      node.attr("data-lit", null);
      link.attr("data-lit", null).classed("on-path", false);
      hull.attr("data-lit", null);
      flow.classed("flowing", false);
      if (pointed !== null) { pointed = null; placeLabels(d3.zoomTransform(el).k); }
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
    pointed = id;
    placeLabels(d3.zoomTransform(el).k);
    placeQuiet(new Set([id, ...near, ...path.ids]), { hideOverlaps: true });
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
  const room = height - top - bottom;
  const SIDE_MARGIN = 10;
  const fitTransform = () => {
    // the scene as drawn (nodes, placed labels, file names, rings) is measured, not estimated, so
    // nothing is clipped; labels grow when zoomed out, so refit until the scale and their size agree
    render();
    let scale = 1, box = root.node().getBBox();
    for (let i = 0; i < 4; i++) {
      el.style.setProperty("--label-scale", String(labelScale(scale)));
      el.style.setProperty("--label-scale-all", String(Math.max(1, 1 / scale)));
      box = root.node().getBBox();
      const next = Math.min(1.5, (width - 2 * SIDE_MARGIN) / Math.max(box.width, 1), room / Math.max(box.height, 1));
      const settled = Math.abs(next - scale) < 0.005;
      scale = next;
      if (settled) break;
    }
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
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
  // how far a node's symbol reaches from its centre on each side (a diamond is taller than its radius)
  const reach = (n) => n.reach || { up: radius(n), down: radius(n), left: radius(n), right: radius(n) };
  // a label's box as drawn: its measured width (else ~7.7px a character) and 18px a line at 12px, plus a margin
  const labelBox = (n, side, k) => {
    const w = (n.textWidth || (sidText(n) + labelText(n)).length * 7.7) * k + 6, h = 18 * k, e = reach(n);
    if (side === "below") return { x0: n.x - w / 2, x1: n.x + w / 2, y0: n.y + e.down + 3, y1: n.y + e.down + 3 + h };
    if (side === "above") return { x0: n.x - w / 2, x1: n.x + w / 2, y0: n.y - e.up - 3 - h, y1: n.y - e.up - 3 };
    if (side === "right") return { x0: n.x + e.right + 3, x1: n.x + e.right + 3 + w, y0: n.y - h / 2, y1: n.y + h / 2 };
    return { x0: n.x - e.left - 3 - w, x1: n.x - e.left - 3, y0: n.y - h / 2, y1: n.y + h / 2 };
  };
  const overlap = (q, boxes) => boxes.reduce((sum, p) => sum + Math.max(0, Math.min(q.x1, p.x1) - Math.max(q.x0, p.x0))
    * Math.max(0, Math.min(q.y1, p.y1) - Math.max(q.y0, p.y0)), 0);
  // a side that would leave the canvas counts as covering something
  const outside = (q) => {
    const w = q.x1 - q.x0, h = q.y1 - q.y0;
    const inW = Math.max(0, Math.min(q.x1, width) - Math.max(q.x0, 0)), inH = Math.max(0, Math.min(q.y1, height - bottom) - Math.max(q.y0, top));
    return w * h - inW * inH;
  };
  const sideFor = (n, k, boxes) => {
    let best = null;
    for (const side of SIDES) {
      const q = labelBox(n, side, k), area = overlap(q, boxes) + outside(q);
      if (!best || area < best.area) best = { side, q, area };
      if (area === 0) break;
    }
    return best;
  };
  const setSide = (sel) => {
    const e = (n) => reach(n);
    sel.attr("x", (n) => (n.side === "right" ? e(n).right + 4 : n.side === "left" ? -e(n).left - 4 : 0))
      .attr("y", (n) => (n.side === "below" ? e(n).down + 4 : n.side === "above" ? -e(n).up - 4 : 0))
      .attr("text-anchor", (n) => (n.side === "right" ? "start" : n.side === "left" ? "end" : "middle"))
      .attr("dominant-baseline", (n) => (n.side === "below" ? "hanging" : n.side === "above" ? "auto" : "central"));
  };
  // label priority when two would overlap (the lower one hides): cited, then the pointed-at or
  // selected node, then modules by how many edges they have, then the rest by edges
  const rank = (n) => (state.cited?.has(n.id) ? 0 : n.id === pointed || n.id === state.selected ? 1 : isModule(n) ? 2 : 3);
  const byPriority = (a, b) => rank(a) - rank(b)
    || Number(state.retrieved.has(b.id)) - Number(state.retrieved.has(a.id))
    || (state.sids.get(a.id) ?? Infinity) - (state.sids.get(b.id) ?? Infinity)
    || (degree.get(b.id) || 0) - (degree.get(a.id) || 0);
  // names revealed by pointing or zooming go where they cover the fewest labels already showing;
  // one that would still cover another stays hidden (the pointed-at node's own name always shows)
  const placeQuiet = (ids, { hideOverlaps = false } = {}) => {
    const k = d3.zoomTransform(el).k;
    const boxes = nodes.filter((n) => labelled(n) && !n.collides).map((n) => labelBox(n, n.side || "below", subject(n) ? labelScale(k) : 1));
    const reveal = nodes.filter((n) => ids.has(n.id) && !labelled(n)).sort(byPriority);
    for (const n of reveal) {
      const best = sideFor(n, Math.max(1, 1 / k), boxes);
      n.side = best.side;
      n.hidden = hideOverlaps && best.area > 0 && n.id !== pointed;
      if (!n.hidden) boxes.push(best.q);
    }
    const quiet = node.filter((n) => ids.has(n.id) && !labelled(n)).select("text.quiet");
    setSide(quiet);
    quiet.classed("hide", (n) => Boolean(n.hidden));
  };
  // the dots: a label avoids covering other nodes where it can (they never hide it)
  const dotBoxes = () => nodes.map((n) => {
    const e = reach(n);
    return { id: n.id, x0: n.x - e.left - 1, x1: n.x + e.right + 1, y0: n.y - e.up - 1, y1: n.y + e.down + 1 };
  });
  const placeLabels = (scale) => {
    const placed = [];
    const dots = dotBoxes();
    for (const n of nodes.filter(labelled).sort(byPriority)) {
      const k = subject(n) ? labelScale(scale) : 1;
      const others = dots.filter((d) => d.id !== n.id);
      let best = null;
      for (const side of SIDES) {
        const q = labelBox(n, side, k);
        const labels = overlap(q, placed), covered = overlap(q, others), area = labels * 4 + covered + outside(q);
        if (!best || area < best.area) best = { side, q, area, labels, covered };
        if (area === 0) break;
      }
      n.side = best.side;
      // it would cover a higher-priority name, or sit under another node on every side: hidden
      // (cited, pointed-at and selected names always show)
      n.collides = best.labels > 0 || (best.covered > 6 && rank(n) > 1);
      if (!n.collides) placed.push(best.q);
    }
    setSide(label);
    label.classed("collide", (n) => Boolean(n.collides));
    node.filter((n) => rank(n) <= 1).raise();         // cited and selected names paint above other nodes
    // file and ring names rank lowest: one that would cover a node's name steps aside
    const named = (x, y, text, perChar) => {
      const k = Math.max(1, 1 / scale), w = text.length * perChar * k + 8, h = 17 * k;
      return { x0: x - w / 2, x1: x + w / 2, y0: y - h, y1: y + 3 };
    };
    // (larger files first; a file name also gives way to another file's name and to the dots)
    const fileNames = [...placed];
    const hidden = new Set();
    for (const [file, ns] of [...groups].sort((a, b) => b[1].length - a[1].length)) {
      const h = hullShape(ns);
      if (!h) continue;
      const q = named(h.top[0], h.top[1] - 5, file.split("/").pop(), 7.7);
      if (overlap(q, fileNames) > 0 || overlap(q, dots) > 12) hidden.add(file);
      else fileNames.push(q);
    }
    hull.select("text").classed("collide", ([file]) => hidden.has(file));
    ringText.classed("collide", (i) => overlap(named(width / 2, midY - ringR(i) - 6, RING_NAMES[i], 6.8), placed) > 0);
  };

  // ── the morph: final positions are known; the scene glides there from where it was ──
  const finals = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
  // measure each label once at its base size (the camera sets the scale again right after)
  el.style.setProperty("--label-scale", "1");
  el.style.setProperty("--label-scale-all", "1");
  label.each(function measure(n) { n.textWidth = this.getComputedTextLength() || 0; });
  node.select("path.dot").each(function measure(n) {
    const b = this.getBBox();
    if (b.width) n.reach = { up: -b.y, down: b.y + b.height, left: -b.x, right: b.x + b.width };
  });
  placeLabels(1);
  let target = fitTransform();
  placeLabels(target.scale);
  target = fitTransform();
  remember();
  const live = () => { sim.on("tick", () => { clampAll(); render(); }).on("end", remember); };
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
    node, byId, nodes, links, related, highlight, fit, zoomBy, playTrails, collapsed,
    relabel: () => placeLabels(d3.zoomTransform(el).k),            // the selection changed: it outranks the rest
    stop: () => {
      timer?.stop();
      timer = null;
      clearTimeout(flowTimer);
      clearTimeout(zoomTimer);
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

const TRAY_OPEN_MAX = 6;                  // more unconnected nodes than this: the tray starts collapsed
const TRAY_CHIPS = 200;                   // chips rendered; the rest are counted in "+N more"

/**
 * Isolated nodes, listed in a tray below the graph (never in it: they take no part in the
 * layout). Collapsible; when open it shows at most two rows of chips and "+N more".
 */
function renderTray(isolated) {
  const tray = $("#tray");
  tray.hidden = !isolated.length;
  if (!isolated.length) return;
  // a new set of isolated nodes: collapsed when there are many, and back to two rows
  const key = `${state.demo?.id}|${state.graphMode}|${state.retrieved.size}|${isolated.length}`;
  if (state.trayKey !== key) {
    state.trayKey = key;
    state.trayAll = false;
    tray.open = isolated.length <= TRAY_OPEN_MAX;
  }
  $("#tray-title").textContent = `Unconnected (${isolated.length})`;
  const list = $("#tray-list");
  list.dataset.extra = String(Math.max(0, isolated.length - TRAY_CHIPS));
  fill(list, ...isolated.slice(0, TRAY_CHIPS).map((n) => {
    const name = n.label || n.name || n.file || "";
    const where = formatLocation(n);
    return el("button", {
      type: "button", class: "fact", title: where && where !== name ? `${name}\n${where}` : name,
      onclick: () => selectNode(n.id, { open: true }),
    }, el("code", { text: middleTruncateName(name, 28) }));
  }), el("button", { type: "button", class: "fact tray-more", hidden: true,
                     onclick: () => { state.trayAll = !state.trayAll; layoutTray(); } }));
  layoutTray();
}

/** Two rows of chips at most: the ones that don't fit are counted in "+N more" (which lists them, scrolling). */
function layoutTray() {
  const tray = $("#tray"), list = $("#tray-list");
  if (tray.hidden || !tray.open) return;
  const chips = [...list.querySelectorAll(".fact:not(.tray-more)")];
  const more = list.querySelector(".tray-more");
  if (!chips.length || !more) return;
  for (const c of chips) c.hidden = false;
  more.hidden = true;
  list.classList.toggle("all", Boolean(state.trayAll));
  list.style.maxHeight = "";
  const rowH = chips[0].offsetHeight;
  if (!rowH) { requestAnimationFrame(layoutTray); return; }          // not laid out yet (the tray just opened)
  const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
  const twoRows = 2 * rowH + gap;
  if (state.trayAll) {
    more.hidden = false;
    more.textContent = "Show fewer";
    list.style.maxHeight = `${twoRows}px`;
    return;
  }
  // rows are measured from the list's own top: the tray is pinned to the card's bottom, so it moves as rows go
  const fits = (e) => e.getBoundingClientRect().top - list.getBoundingClientRect().top <= rowH + gap + 1;   // row one or two
  let rest = Number(list.dataset.extra || 0);
  for (const c of chips) if (!fits(c)) { c.hidden = true; rest += 1; }
  if (!rest) return;
  more.hidden = false;
  const shown = chips.filter((c) => !c.hidden);
  more.textContent = `+${rest} more`;
  while (!fits(more) && shown.length) {                 // "+N more" takes the last place in row two
    shown.pop().hidden = true;
    rest += 1;
    more.textContent = `+${rest} more`;
  }
}

/** Roving focus: one node is in the Tab order; the arrow keys move it. */
function focusNode(datum, move) {
  if (!graphView) return;
  graphView.node.attr("tabindex", (n) => (n === datum ? 0 : -1));
  if (move) graphView.node.filter((n) => n === datum).node()?.focus();
}

/** Open a node: select it everywhere, and open the focus overlay. */
function activateNode(n) {
  selectNode(n.id, { open: true });
  const sid = state.sids.get(n.id);
  if (sid && state.answerTurn && !isNarrow()) {
    const row = state.answerTurn.querySelector(`.source[data-sid="${sid}"]`);
    const group = row?.closest("details.also");
    if (group) group.open = true;
    row?.scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "nearest" });
  }
}

function pulseNode(chunkId) {
  if (!graphView) return;
  graphView.node.classed("pulse", (n) => n.id === chunkId);
  setTimeout(() => graphView?.node.classed("pulse", false), 1600);
}

// ── the last answers' subgraphs: thumbnails down the graph's left edge ───────
function rememberAnswer(repoId, turn, questionId = null) {
  const list = state.history.get(repoId) || state.history.set(repoId, []).get(repoId);
  const entry = { turn, evidence: evidence(), thumb: snapshotGraph(), n: turn.dataset.turn, qid: questionId };
  list.unshift(entry);
  if (list.length > HISTORY_LIMIT) list.length = HISTORY_LIMIT;
  renderThumbs();
}

/** A small picture of the graph as drawn now: dots in their hop colours, lines between them. */
function snapshotGraph() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  if (!graphView?.nodes.length) return svg;
  const pts = graphView.nodes.filter((n) => state.retrieved.size ? (state.retrieved.has(n.id) || graphView.related.get(n.id)?.size) : true);
  const xs = pts.map((n) => n.x), ys = pts.map((n) => n.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 64, H = 48, pad = 6;
  const sx = (x) => pad + ((x - x0) / Math.max(1, x1 - x0)) * (W - 2 * pad);
  const sy = (y) => pad + ((y - y0) / Math.max(1, y1 - y0)) * (H - 2 * pad);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const keep = new Set(pts.map((n) => n.id));
  for (const l of graphView.links.slice(0, 200)) {
    const s = l.source, t = l.target;
    if (!keep.has(s.id) || !keep.has(t.id)) continue;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", sx(s.x).toFixed(1)); line.setAttribute("y1", sy(s.y).toFixed(1));
    line.setAttribute("x2", sx(t.x).toFixed(1)); line.setAttribute("y2", sy(t.y).toFixed(1));
    svg.append(line);
  }
  for (const n of pts.slice(0, 200)) {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", sx(n.x).toFixed(1)); c.setAttribute("cy", sy(n.y).toFixed(1));
    c.setAttribute("r", state.retrieved.has(n.id) ? "2.6" : "1.6");
    const hop = hopOf(n);
    if (hop !== null) c.setAttribute("data-hop", hop);
    svg.append(c);
  }
  return svg;
}

function renderThumbs() {
  const live = state.history.get(state.demo?.id) || [];
  const seen = new Set(live.map((h) => h.qid).filter(Boolean));
  // signed in: earlier answers from the server's history, redrawn from their stored subgraph
  const stored = (state.historyMeta.get(state.demo?.id)?.items || []).filter((q) => !seen.has(q.id))
    .slice(0, Math.max(0, HISTORY_LIMIT - live.length));
  $("#thumbs").replaceChildren(...live.map((h) => {
    const q = h.turn.querySelector(".question")?.textContent || "";
    return el("button", { type: "button", class: "thumb", "aria-pressed": String(h.evidence.answerTurn === state.answerTurn && state.graphMode !== "full"),
      title: `Show this answer's graph again: ${middleTruncateName(q, 60)}`, "aria-label": `Answer ${h.n}: ${q}`,
      onclick: () => restoreAnswer(h) }, h.thumb.cloneNode(true), el("span", { class: "n", text: `#${h.n}` }));
  }), ...stored.map((item) => {
    const turn = $(`#conversation .turn[data-question="${item.id}"]`);
    return el("button", { type: "button", class: "thumb", "aria-pressed": String(Boolean(turn) && turn === state.answerTurn && state.graphMode !== "full"),
      title: `Show this earlier answer and its graph: ${middleTruncateName(item.question, 60)}`,
      "aria-label": `Earlier answer: ${item.question}`, onclick: () => restoreHistory(item) },
    thumbFromIds(item.subgraph || [], item.hops || {}), el("span", { class: "n", text: "earlier" }));
  }));
}

/** A thumbnail of a stored answer: its subgraph's nodes where the graph has them now, else in rings by hop. */
function thumbFromIds(ids, hops) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const W = 64, H = 48, pad = 6;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const placed = new Map((graphView?.nodes || []).filter((n) => Number.isFinite(n.x)).map((n) => [n.id, n]));
  let pts = ids.map((id) => placed.get(id)).filter(Boolean);
  if (pts.length < Math.min(2, ids.length)) {
    pts = ids.map((id, i) => { const r = 4 + 8 * (hops[id] ?? 1); return { id, x: Math.cos(i * 2.4) * r, y: Math.sin(i * 2.4) * r }; });
  }
  if (!pts.length) return svg;
  const xs = pts.map((n) => n.x), ys = pts.map((n) => n.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = (x) => pad + ((x - x0) / Math.max(1, x1 - x0)) * (W - 2 * pad);
  const sy = (y) => pad + ((y - y0) / Math.max(1, y1 - y0)) * (H - 2 * pad);
  const at = new Map(pts.map((n) => [n.id, n]));
  for (const l of state.graph?.links || []) {
    const a = at.get(typeof l.source === "object" ? l.source.id : l.source), b = at.get(typeof l.target === "object" ? l.target.id : l.target);
    if (!a || !b) continue;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", sx(a.x).toFixed(1)); line.setAttribute("y1", sy(a.y).toFixed(1));
    line.setAttribute("x2", sx(b.x).toFixed(1)); line.setAttribute("y2", sy(b.y).toFixed(1));
    svg.append(line);
  }
  for (const n of pts) {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", sx(n.x).toFixed(1)); c.setAttribute("cy", sy(n.y).toFixed(1));
    const hop = hops[n.id];
    c.setAttribute("r", hop === undefined ? "1.6" : "2.6");
    if (hop !== undefined) c.setAttribute("data-hop", Math.min(hop, 3));
    svg.append(c);
  }
  return svg;
}

/** The evidence (graph highlight) of a stored answer. */
function evidenceOf(item, turn) {
  const sources = item.sources || [];
  return { retrieved: new Map(sources.map((s) => [s.chunk_id, s.hop])), sids: new Map(sources.map((s) => [s.chunk_id, s.id])),
           cited: new Set(sources.filter((s) => s.cited).map((s) => s.chunk_id)), answerTurn: turn, sources };
}

/** Show an earlier answer: in the conversation (added if it isn't there) and on the graph. */
function restoreHistory(item) {
  let turn = $(`#conversation .turn[data-question="${item.id}"]`);
  if (!turn) {
    const view = historyItemView(item);
    const answer = view.querySelector(".answer");
    turn = el("article", { class: "turn earlier", "data-turn": `h${item.id}`, "data-question": item.id },
      el("div", { class: "question", text: item.question }), answer);
    $("#conversation").append(turn);
    syncRepoCard();
  }
  restoreAnswer({ turn, evidence: evidenceOf(item, turn) });
}

function restoreAnswer(h) {
  setEvidence(h.evidence);
  state.graphMode = "focus";
  state.trailsShown = h.evidence.answerTurn;
  selectNode(null);
  drawGraph();
  renderThumbs();
  if (h.turn.isConnected) h.turn.scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "start" });
}

// ── the node panel ──────────────────────────────────────────────────────────
const fileCache = new Map();             // "repo:path" -> Promise<{text, url}>

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

/** Degree of a node in the full graph: how many call it, how many it calls. */
function callCounts(n) {
  const links = state.graph?.links || [];
  if (typeof n.id === "string" && n.id.startsWith("m:")) {
    const file = n.file;
    const fileOf = new Map(state.graph.nodes.map((x) => [x.id, x.file]));
    const own = links.filter((l) => l.type !== "member" && fileOf.get(l.source) !== fileOf.get(l.target));
    return { callers: own.filter((l) => fileOf.get(l.target) === file).length, calls: own.filter((l) => fileOf.get(l.source) === file).length };
  }
  const own = links.filter((l) => l.type !== "member");
  return { callers: own.filter((l) => l.target === n.id).length, calls: own.filter((l) => l.source === n.id).length };
}

function codeSkeleton() {
  return el("div", { class: "code-skeleton", "aria-hidden": "true" }, el("span", { class: "skeleton" }), el("span", { class: "skeleton" }), el("span", { class: "skeleton" }), el("span", { class: "skeleton" }));
}

/** The state pill for a node: Cited / Retrieved / Neighbour / Module / Selected. */
function statePill(n) {
  if (!n) return null;
  if (state.cited?.has(n.id)) return el("span", { class: "status-pill", "data-tone": "accent", text: "Cited" });
  if (state.retrieved.has(n.id)) return el("span", { class: "status-pill", "data-tone": "ok", text: state.cited ? "Retrieved, not cited" : "Retrieved" });
  if (n.kind === "module") return el("span", { class: "status-pill", text: "Module" });
  if (state.retrieved.size) return el("span", { class: "status-pill", text: "Neighbour" });
  return el("span", { class: "status-pill", text: "Selected" });
}

function kindWord(kind) {
  return { function: "Function", method: "Method", class: "Class", file: "Module", module: "Module", template: "Template", config: "Config" }[kind] || "Symbol";
}

function renderNodePanel(n, source = null) {
  const panel = $("#node-panel");
  if (!n) {
    panel.replaceChildren(el("div", { class: "node-empty" }, el("h2", { class: "card-title", text: "Selected node" }),
      el("p", { text: state.graph ? "Select a node in the graph, or a source under an answer, to see what it is, who calls it, and its code."
        : "The repository's graph is loading." })));
    return;
  }
  const { callers, calls } = callCounts(n);
  const sid = state.sids.get(n.id);
  const lines = n.end_line && n.start_line ? n.end_line - n.start_line + 1 : null;
  const language = n.language || languageOfPath(n.file);
  const url = n.start_line && n.end_line ? githubUrl(state.demo, n.file, n.start_line, n.end_line) : null;
  const preview = el("div", { class: "code-view short", tabindex: "0", role: "region", "aria-label": "Code preview" }, codeSkeleton());
  const nodeTile = (label, value, text = false) => el("div", { class: "node-tile" }, el("span", { class: "label", text: label }),
    el("span", { class: `value${text ? " text" : ""}` }, value));
  const name = n.label || n.name || n.file || "";
  const where = formatLocation(n);
  fill(panel,
    el("div", { class: "node-head" },
      el("span", { class: "kind", text: kindWord(n.kind) }),
      el("h2", {}, sid ? el("span", { class: state.cited?.has(n.id) ? "sid cited" : "sid", text: `S${sid}` }) : null,
        el("span", { title: name, text: middleTruncateName(name, 34) })),
      where ? el("span", { class: "where", title: where, text: formatLocation({ ...n, file: middleTruncate(n.file, 40) }) }) : null,
      statePill(n)),
    el("div", { class: "node-tiles" },
      nodeTile("Called by", callers.toLocaleString()),
      nodeTile("Calls", calls.toLocaleString()),
      nodeTile("Language", language ? [dot(null, languageKey(language)), el("span", { text: LANGUAGE_LABELS[language] || language })] : el("span", { text: "unknown" }), true),
      lines ? nodeTile("Lines", lines.toLocaleString()) : null),
    el("div", { class: "node-preview" }, el("h3", { text: "Code" }), preview),
    el("div", { class: "node-actions" },
      el("button", { type: "button", class: "primary small", text: "Ask about this", onclick: () => askAbout(n) }),
      el("button", { type: "button", class: "secondary small", text: "Open code", onclick: () => openFocus(n) })),
    el("div", { class: "node-links" },
      sid && state.answerTurn ? el("button", { type: "button", class: "link-button", text: `Show S${sid} in the answer`, onclick: () => openSource(state.answerTurn, sid) }) : null,
      url ? el("a", { href: url, target: "_blank", rel: "noopener", text: "Open on GitHub" }) : null));
  renderPreview(preview, n, source);
}

/** The node's own lines (a source's code if we have it, else a slice of the file). */
async function renderPreview(view, n, source) {
  const code = source?.code || state.sources.find((s) => s.chunk_id === n.id)?.code;
  const start = n.start_line || 1;
  if (code) {
    paintLines(view, code.replace(/\n$/, "").split("\n"), start, n.file, { max: 40 });
    return;
  }
  try {
    const file = await loadFile(state.demo.id, n.file);
    if (!view.isConnected) return;
    const all = file.text.split("\n");
    const end = n.end_line || Math.min(all.length, start + 39);
    paintLines(view, all.slice(start - 1, end), start, n.file, { max: 40 });
  } catch (err) {
    if (view.isConnected) view.replaceChildren(el("p", { class: "code-status error", text: err.message }));
  }
}

function paintLines(view, lines, start, path, { max = Infinity } = {}) {
  const shown = lines.slice(0, max);
  view.innerHTML = `<div class="lines">${highlightLines(shown.join("\n"), path)
    .map((html, i) => `<span class="line" data-n="${start + i}">${html || " "}</span>`).join("")}</div>`;
  if (lines.length > max) view.append(el("p", { class: "code-status", text: `${plural(lines.length - max, "more line")}. Open code to read the whole file.` }));
}

function askAbout(n) {
  $("#question").value = n.kind === "module" || n.kind === "file" ? `What does \`${n.file}\` do and what depends on it?` : `What does \`${n.label || n.name}\` do and who calls it?`;
  autosize();
  if (isNarrow()) setView("ask");
  $("#question").focus();
}

// ── the focus overlay ───────────────────────────────────────────────────────
const focusState = { token: 0, opener: null };

function setupFocus() {
  const dialog = $("#focus");
  $("#focus-close").addEventListener("click", () => dialog.close());
  // a click on the backdrop (outside the card) closes it; focus is trapped by the dialog itself
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    focusState.token++;
    const back = focusState.opener;
    focusState.opener = null;
    if (back?.isConnected) back.focus();
    else graphView?.node.filter(function focused() { return this.tabIndex === 0; }).node()?.focus();
  });
}

/** Edges touching a node in the full graph, with their evidence. */
function edgesOf(n) {
  const links = state.graph?.links || [];
  const byId = new Map(state.graph?.nodes.map((x) => [x.id, x]) || []);
  const trails = trailEdges();
  const isModule = typeof n.id === "string" && n.id.startsWith("m:");
  const out = [];
  for (const l of links) {
    if (l.type === "member") continue;
    const s = byId.get(l.source), t = byId.get(l.target);
    if (!s || !t) continue;
    const mine = isModule ? (s.file === n.file) !== (t.file === n.file) && (s.file === n.file || t.file === n.file) : l.source === n.id || l.target === n.id;
    if (!mine) continue;
    const outgoing = isModule ? s.file === n.file : l.source === n.id;
    out.push({ link: l, other: outgoing ? t : s, outgoing, onPath: trails.has(pairKey(l.source, l.target)) });
  }
  out.sort((a, b) => Number(b.link.type === "bridge") - Number(a.link.type === "bridge") || Number(b.onPath) - Number(a.onPath));
  return out;
}

function edgeVerb(e) {
  const t = e.link.type;
  if (t === "bridge") return e.outgoing ? "reaches, across languages," : "reached, across languages, by";
  if (t === "inherits") return e.outgoing ? "inherits from" : "base class of";
  return e.outgoing ? "calls" : "called by";
}

function openFocus(n) {
  const dialog = $("#focus");
  focusState.opener = document.activeElement;
  const token = ++focusState.token;
  const sid = state.sids.get(n.id);
  const { callers, calls } = callCounts(n);
  const language = n.language || languageOfPath(n.file);
  const s = state.sources.find((x) => x.chunk_id === n.id);
  fill($("#focus-title"), sid ? el("span", { class: state.cited?.has(n.id) ? "sid cited" : "sid", text: `S${sid}` }) : null, " ", el("span", { text: n.label || n.name || n.file || "" }));
  fill($("#focus-where"), formatLocation(n) ? el("span", { text: formatLocation(n) }) : null, statePill(n), language ? languageTag(language, { always: true }) : null);
  const meta = (label, ...value) => el("div", {}, el("dt", { text: label }), el("dd", {}, ...value));
  fill($("#focus-meta"), 
    meta("Kind", el("span", { text: kindWord(n.kind) })),
    meta("Called by", el("span", { text: callers.toLocaleString() })),
    meta("Calls", el("span", { text: calls.toLocaleString() })),
    n.end_line && n.start_line ? meta("Lines", el("span", { text: plural(n.end_line - n.start_line + 1, "line") })) : null,
    s && s.hop ? meta("Reached", el("span", { title: relationLabel(s), text: relationLabel(s) })) : s ? meta("Found by", el("span", { text: "direct match" })) : null,
    s?.bridge ? meta("Bridge", el("span", { title: bridgeEvidence(s.bridge), text: s.bridge.label })) : null);

  // the edges, each with its evidence
  const edges = edgesOf(n);
  $("#focus-edges-title").textContent = edges.length ? `Edges (${edges.length})` : "Edges";
  fill($("#focus-edges"), ...(edges.length ? edges.slice(0, 40).map((e) => {
    const b = e.link.type === "bridge" ? e.link : null;
    const ev = b?.evidence || {};
    return el("li", {},
      dot(b ? "bridge" : e.onPath ? "cited" : null, b ? null : null),
      el("span", { class: "edge-what" },
        el("span", {}, el("span", { class: "verb", text: `${edgeVerb(e)} ` }),
          el("b", { text: e.other.label || e.other.name }), e.link.count > 1 ? el("span", { class: "verb", text: ` (${plural(e.link.count, "call")})` }) : null),
        b ? el("span", { text: `${b.label}${ev.src ? ` ← ${ev.src}` : ""}${ev.dst ? ` → ${ev.dst}` : ""} (${b.confidence === "exact" ? "exact" : "pattern"} match)` })
          : el("span", { text: `${formatLocation(e.other)}${e.link.confidence === "name_match" ? ", matched by name" : ""}${e.onPath ? ", on the path to this answer" : ""}` })));
  }) : [el("li", { class: "none", text: n.kind === "module" ? "No calls to or from other modules." : "No calls to or from other code were resolved." })]),
  edges.length > 40 ? el("li", { class: "none", text: `and ${plural(edges.length - 40, "more edge")}` }) : null);

  drawFocusGraph(n, edges);
  dialog.showModal();
  $("#focus-close").focus();

  // the whole file, the answer's sources tinted, scrolled to this node
  const view = $("#focus-code");
  const note = $("#focus-code-note");
  view.replaceChildren(codeSkeleton());
  note.textContent = "";
  loadFile(state.demo.id, n.file).then((file) => {
    if (token !== focusState.token) return;
    const lines = file.text.split("\n");
    view.innerHTML = `<div class="lines">${highlightLines(file.text, n.file)
      .map((html, i) => `<span class="line" data-n="${i + 1}">${html || " "}</span>`).join("")}</div>`;
    markLines(view, n);
    const target = view.querySelector(`.line[data-n="${n.start_line || 1}"]`);
    if (target) view.scrollTop = Math.max(0, target.offsetTop - view.clientHeight / 3);
    const url = n.start_line && n.end_line ? githubUrl(state.demo, n.file, n.start_line, n.end_line) : null;
    fill(note, `${plural(lines.length, "line")} in ${n.file}${file.origin === "github" ? ", fetched from GitHub" : ""}. `,
      url ? el("a", { href: url, target: "_blank", rel: "noopener", text: "Open on GitHub" }) : null);
  }).catch((err) => {
    if (token !== focusState.token) return;
    const s2 = state.sources.find((x) => x.chunk_id === n.id);
    if (s2?.code) { paintLines(view, s2.code.replace(/\n$/, "").split("\n"), s2.start_line || 1, n.file); markLines(view, n); note.textContent = "Only the retrieved lines are available: " + err.message; }
    else view.replaceChildren(el("p", { class: "code-status error", text: err.message }));
  });
}

/** Tint the answer's sources in this file: cited strong, retrieved faint; outline the node's own lines. */
function markLines(view, n) {
  const line = (k) => view.querySelector(`.line[data-n="${k}"]`);
  const sources = state.sources.filter((s) => s.file === n.file);
  for (const s of [...sources].sort((a, b) => (b.end_line - b.start_line) - (a.end_line - a.start_line))) {
    const cited = Boolean(state.cited?.has(s.chunk_id));
    for (let k = s.start_line; k <= s.end_line; k++) line(k)?.classList.add(cited ? "cited" : "retrieved");
    const head = line(s.start_line);
    if (head) {
      head.dataset.tag = [head.dataset.tag, `S${s.id}`].filter(Boolean).join(" ");
      if (cited) head.classList.add("tag-cited");
    }
  }
  if (n.start_line && n.end_line) for (let k = n.start_line; k <= n.end_line; k++) line(k)?.classList.add("target");
}

/** The node in the middle, its neighbours around it, the edges labelled by kind. */
function drawFocusGraph(n, edges) {
  const svg = d3.select("#focus-svg");
  svg.selectAll("*").remove();
  const box = svg.node().getBoundingClientRect();
  const width = Math.max(320, box.width || 560), height = Math.max(240, box.height || 420);
  svg.attr("viewBox", `0 0 ${width} ${height}`);
  const defs = svg.append("defs");
  for (const id of ["f-arrow", "f-arrow-bridge", "f-arrow-trail"]) {
    defs.append("marker").attr("id", id).attr("viewBox", "0 -4 8 8").attr("refX", 8).attr("markerWidth", 7).attr("markerHeight", 7).attr("orient", "auto")
      .append("path").attr("d", "M0,-4L8,0L0,4");
  }
  const shown = edges.slice(0, 16);
  const others = [...new Map(shown.map((e) => [e.other.id, e.other])).values()];
  const cx = width / 2, cy = height / 2, R = Math.min(width, height) / 2 - 70;
  const pos = new Map([[n.id, { x: cx, y: cy }]]);
  others.forEach((o, i) => {
    const a = (i / Math.max(1, others.length)) * Math.PI * 2 - Math.PI / 2;
    pos.set(o.id, { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) });
  });
  const g = svg.append("g");
  const edgeLayer = g.append("g");
  for (const e of shown) {
    const from = e.outgoing ? pos.get(n.id) : pos.get(e.other.id), to = e.outgoing ? pos.get(e.other.id) : pos.get(n.id);
    const dx = to.x - from.x, dy = to.y - from.y, len = Math.hypot(dx, dy) || 1;
    const back = 16;
    edgeLayer.append("line").attr("class", `f-link ${e.link.type}${e.onPath ? " trail" : ""}`)
      .attr("x1", from.x + (dx / len) * 12).attr("y1", from.y + (dy / len) * 12).attr("x2", to.x - (dx / len) * back).attr("y2", to.y - (dy / len) * back);
    const label = e.link.type === "bridge" ? e.link.label : e.link.type === "inherits" ? "inherits" : e.link.count > 1 ? `${e.link.count} calls` : "calls";
    edgeLayer.append("text").attr("class", "f-edge-label").attr("x", (from.x + to.x) / 2).attr("y", (from.y + to.y) / 2 - 6).text(label);
  }
  const nodeLayer = g.append("g");
  const draw = (node, centre) => {
    const p = pos.get(node.id);
    const grp = nodeLayer.append("g").attr("class", `f-node${centre ? " centre" : ""}`).attr("data-hop", hopOf(node)).attr("transform", `translate(${p.x},${p.y})`);
    const r = centre ? 11 : 7;
    if (centre) grp.append("path").attr("class", "ring").attr("d", symbolPath(node.kind, r + 5));
    grp.append("path").attr("class", "dot").attr("d", symbolPath(node.kind, r));
    const t = grp.append("text").attr("text-anchor", "middle").attr("dominant-baseline", "hanging").attr("y", r + 5);
    const sid = state.sids.get(node.id);
    if (sid) t.append("tspan").attr("class", "label-sid").text(`S${sid} `);
    t.append("tspan").text(middleTruncateName(node.label || node.name, centre ? 36 : 22));
    grp.append("title").text(`${node.label || node.name}\n${formatLocation(node)}`);
  };
  others.forEach((o) => draw(o, false));
  draw(n, true);
  $("#focus-caption").textContent = edges.length > shown.length
    ? `${shown.length} of ${plural(edges.length, "edge")} shown. Bridges first, then the path to this answer.`
    : edges.length ? `${plural(edges.length, "edge")}. Bridges are magenta; the answer's path is blue.` : "This node has no resolved edges.";
}

// ── the repos page ──────────────────────────────────────────────────────────
function setupRepos() {
  const pills = [...document.querySelectorAll("#view-repos [data-status]")];
  const setFilter = (value, focus = false) => {
    state.reposFilter = value;
    for (const b of pills) {
      const on = b.dataset.status === value;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    renderRepos();
  };
  pills.forEach((b, i) => {
    b.addEventListener("click", () => setFilter(b.dataset.status));
    b.addEventListener("keydown", (e) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      setFilter(pills[(i + step + pills.length) % pills.length].dataset.status, true);
    });
  });
  $("#repos-search").addEventListener("input", renderRepos);
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".row-menu")) document.querySelectorAll(".row-menu .menu:not([hidden])").forEach((m) => { m.hidden = true; m.previousElementSibling.setAttribute("aria-expanded", "false"); });
  });
}

function repoRow(repo, { demo = false } = {}) {
  const job = state.jobs.get(repo.id);
  const status = repoStatus(repo, job);
  const [owner, name] = repo.name.includes("/") ? repo.name.split("/") : [null, repo.name];
  const languages = Object.entries(repo.languages || {}).sort((a, b) => b[1] - a[1]);
  const version = repo.indexed_with || (repo.versions || []).at(-1) || "";
  const open = () => { selectRepo(repo.id); setView("overview", { focus: true }); };
  const menu = el("div", { class: "menu", role: "menu", hidden: true },
    el("button", { type: "button", role: "menuitem", text: "Open", onclick: open }),
    demo ? null : el("button", { type: "button", role: "menuitem", text: repo.reindex_to ? `Re-index with ${repo.reindex_to}` : "Re-index",
      disabled: status.key === "indexing", onclick: () => reindexRepo(repo) }),
    demo ? null : el("button", { type: "button", role: "menuitem", class: "danger", text: "Remove", onclick: () => removeUserRepo(repo.id) }));
  const trigger = el("button", { type: "button", class: "icon-button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": `Actions for ${repo.name}`,
    onclick: (e) => { const on = menu.hidden; document.querySelectorAll(".row-menu .menu").forEach((m) => { m.hidden = true; }); menu.hidden = !on; e.currentTarget.setAttribute("aria-expanded", String(on)); } },
  svgIcon("M5 12h.01M12 12h.01M19 12h.01"));
  trigger.querySelector("path").setAttribute("stroke-width", "3");
  return el("tr", { "aria-selected": String(repo.id === state.demo?.id) },
    el("td", {}, el("div", { class: "repo-cell" },
      el("div", { class: "name" },
        el("button", { type: "button", title: repo.name, onclick: open }, owner ? el("span", { class: "owner", text: `${owner}/` }) : null, middleTruncateName(name, 30)),
        repo.private ? el("span", { class: "private-badge", text: "Private" }) : null),
      repo.description ? el("span", { class: "desc", title: repo.description, text: repo.description }) : null)),
    el("td", {}, languages.length ? el("span", { class: "dots", title: languages.map(([l, k]) => plural(k, `${l} file`)).join(", ") },
      languages.slice(0, 5).map(([l]) => dot(null, languageKey(l))), el("span", { class: "muted", text: ` ${languages.slice(0, 2).map(([l]) => l).join(", ")}${languages.length > 2 ? ` +${languages.length - 2}` : ""}` }))
      : el("span", { class: "muted", text: "none recorded" })),
    el("td", { class: "version" }, version || el("span", { class: "muted", text: "unknown" })),
    el("td", { class: "when" }, formatDate(repo.built) || el("span", { class: "muted", text: "unknown" })),
    el("td", {}, el("span", { class: "status-pill", "data-tone": status.tone, text: status.label }),
      status.key === "indexing" ? el("div", { class: "progress", title: `${Math.round((status.progress || 0) * 100)}%` }, el("i", { style: `--progress:${status.progress || 0}` })) : null),
    el("td", {}, el("div", { class: "row-menu" }, trigger, menu)));
}

function renderRepos() {
  const q = $("#repos-search").value.trim().toLowerCase();
  const match = (r) => !q || r.name.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q);
  const byStatus = (r) => state.reposFilter === "all" || repoStatus(r, state.jobs.get(r.id)).key === state.reposFilter;
  // a repo being indexed for the first time has no row yet: show it from its job
  const pending = [...state.jobs.values()].filter((j) => isActive(j.raw) && !state.userRepos.some((r) => r.id === j.repo_id))
    .map((j) => ({ id: j.repo_id, name: j.name, languages: {}, built: "", description: j.message }));
  const mine = [...pending, ...state.userRepos].filter(match).filter(byStatus);
  const rows = $("#repos-rows");
  const empty = $("#repos-empty");
  rows.replaceChildren(...mine.map((r) => repoRow(r)));
  const signedIn = Boolean(currentUser());
  empty.hidden = mine.length > 0;
  empty.textContent = !signedIn ? "Sign in with GitHub to add your own repositories. The demos below are open to everyone."
    : !state.userRepos.length ? "No repositories yet. Use + Add repo to index one from GitHub."
    : q ? `No repository matches “${$("#repos-search").value.trim()}”.` : `No ${state.reposFilter === "update" ? "repositories with an update available" : `${state.reposFilter} repositories`}.`;
  $("#repos-table").hidden = !mine.length;
  $("#repos-lead").textContent = signedIn ? `${plural(state.userRepos.length + pending.length, "repository", "repositories")} on your account${pending.length ? `, ${pending.length} being indexed` : ""}.` : "The repositories you added, and where their indexes stand.";
  $("#demos-rows").replaceChildren(...state.demos.filter(match).map((d) => repoRow(d, { demo: true })));
}

// ── layout / startup ────────────────────────────────────────────────────────
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
  setupRepoSwitcher();
  setupFocus();
  setupRepos();
  const openAddRepo = setupAddRepo();
  renderRepoHead(null);
  renderTiles(null);
  renderNodePanel(null);
  state.userRepos = currentUser() ? await loadUserRepos() : [];   // your repositories need you signed in
  syncJobs();                                                       // indexing jobs, from the server
  document.querySelectorAll(".pill-nav button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view, { focus: true })));
  $("#layout-toggle").addEventListener("click", () => {
    state.graphLayout = state.graphLayout === "rings" ? "free" : "rings";
    drawGraph();
  });
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
    renderThumbs();
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
      if (isNarrow()) setView("ask");
      $("#question").focus();
    }
  });
  $(".skip-link").addEventListener("click", (e) => {
    e.preventDefault();
    if (isNarrow()) setView("ask");
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
    const turn = cite.closest(".turn");
    const row = turn?.querySelector(`.source[data-sid="${cite.dataset.source}"]`);
    if (!row) return;
    openSource(turn, Number(cite.dataset.source));
    const s = state.sources.find((x) => x.chunk_id === Number(row.dataset.chunk));
    selectNode(Number(row.dataset.chunk), { open: true, source: s });
  });
  // the canvas changes size with the window and with the tray: lay out and fit again
  let resizeTimer, canvasSize = "";
  new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect;
    const size = `${Math.round(width)}x${Math.round(height)}`;
    if (size === canvasSize) return;
    canvasSize = size;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => drawGraph(), 120);
  }).observe($(".graph-wrap"));
  let trayWidth = 0;
  new ResizeObserver(([entry]) => {                    // the chips re-flow only when the width changes
    if (Math.round(entry.contentRect.width) === trayWidth) return;
    trayWidth = Math.round(entry.contentRect.width);
    layoutTray();
  }).observe($("#tray-list"));
  $("#tray").addEventListener("toggle", () => requestAnimationFrame(layoutTray));

  // Free hosting sleeps when idle: say so if the first response is slow.
  const wakeTimer = setTimeout(() => banner("Waking up the server (free hosting sleeps when idle). This can take up to a minute…"), 3000);
  try {
    state.health = await fetchJSON("/api/health");
    state.demos = await fetchJSON("/api/demos");
  } catch {
    clearTimeout(wakeTimer);
    banner("Could not reach the server. Please reload the page in a moment.", "error");
    $("#repo-current").textContent = "Server unreachable";
    return;
  }
  clearTimeout(wakeTimer);
  $("#server-model").textContent = state.health.llm.server ? `(${state.health.llm.models[0]})` : "(not configured)";
  updateLlmBanner();
  renderRepoMenu();

  const params = new URL(window.location.href).searchParams;
  const wanted = params.get("repo");
  if (params.get("view") === "repos") setView("repos");
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
