import {
  citedSources, createSSEParser, errorMessage, focusSubgraph, formatLocation, hopLabel,
  normalizeCitations, renderAnswer,
} from "./lib.js";

const $ = (selector) => document.querySelector(selector);
const SETTINGS_KEY = "coderag.settings";
const FULL_GRAPH_LIMIT = 120;          // draw the whole graph up front only for small repos

const state = {
  demos: [], demo: null, health: null,
  graphs: new Map(), graph: null, graphMode: "focus",
  retrieved: new Map(),                // chunk_id -> hop for the latest answer
  busy: false, turn: 0,
  debug: new URLSearchParams(window.location.search).get("debug") === "1",
  userRepos: [],                       // [{id, name}] indexed by this visitor
  chats: new Map(),                    // repo id -> {turns, retrieved} while another repo is open
};
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
  const defaults = { mode: "server", provider: "groq", apiKey: "", model: "", topK: "", maxHops: "", includeCallers: false };
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

function setupSettings() {
  const dialog = $("#settings");
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
    if (dialog.returnValue !== "save") return;
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
    });
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
  const tab = (repo, removable) => el("button", {
    type: "button", class: removable ? "user" : null, title: removable ? repo.name : null,
    "aria-pressed": String(repo.id === state.demo?.id), onclick: () => selectRepo(repo.id),
  }, repo.name, removable ? el("span", {
    class: "close", role: "button", "aria-label": `Remove ${repo.name}`, title: "Remove from this list",
    text: "×", onclick: (e) => { e.stopPropagation(); removeUserRepo(repo.id); },
  }) : null);
  nav.replaceChildren(...state.demos.map((d) => tab(d, false)),
                      ...state.userRepos.map((r) => tab(r, true)));
  // on a narrow bar the tab row scrolls: keep the open repo in view
  nav.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

// ── visitor-indexed repositories (remembered in this browser) ───────────────
const REPOS_KEY = "coderag.repos";

function loadUserRepos() {
  try { return JSON.parse(localStorage.getItem(REPOS_KEY) || "[]"); } catch { return []; }
}

function saveUserRepos() {
  try { localStorage.setItem(REPOS_KEY, JSON.stringify(state.userRepos)); } catch { /* not persisted */ }
}

function rememberUserRepo(id, name) {
  state.userRepos = [{ id, name }, ...state.userRepos.filter((r) => r.id !== id)].slice(0, 12);
  saveUserRepos();
}

function removeUserRepo(id) {
  state.userRepos = state.userRepos.filter((r) => r.id !== id);
  saveUserRepos();
  // switching away stores the open chat, so forget it only afterwards
  if (state.demo?.id === id) selectRepo(state.demos[0]?.id).then(() => state.chats.delete(id));
  else { state.chats.delete(id); renderRepoTabs(); }
}

const GENERIC_QUESTIONS = [
  "What does this project do, and what are its main modules?",
  "What is the main entry point and what does it call?",
];

function setupAddRepo() {
  const dialog = $("#add-repo");
  const error = $("#add-repo-error");
  const open = (url = "") => {
    $("#repo-url").value = url;
    error.hidden = true;
    const limits = state.health?.indexing;
    $("#index-limits").textContent = limits
      ? `Limits: ${limits.max_py_files.toLocaleString()} Python files / ${limits.max_py_mb} MB; `
        + `${limits.skipped_dirs.join(", ")} folders are skipped.` : "";
    dialog.showModal();
  };
  $("#add-repo-button").addEventListener("click", () => open());
  $("#add-repo-cancel").addEventListener("click", () => dialog.close());
  $("#add-repo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = $("#add-repo-submit");
    submit.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch("/api/repos", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: $("#repo-url").value.trim() }),
      });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || (job.detail && "Please enter a GitHub URL.") || `Request failed (${response.status})`);
      dialog.close();
      trackJob(job);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
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
  const card = $("#repo-card");
  card.replaceChildren(
    el("h1", {}, demo.url ? el("a", { href: `${demo.url}/tree/${demo.sha}`, target: "_blank", rel: "noopener", text: demo.name }) : demo.name),
    el("p", { text: demo.description }),
    el("div", { class: "stats" },
      el("span", {}, el("b", { text: s.files ?? 0 }), " files"),
      el("span", {}, el("b", { text: s.chunks ?? 0 }), " code chunks"),
      el("span", {}, el("b", { text: s.call_edges ?? 0 }), " resolved calls"),
      demo.sha ? el("span", {}, "commit ", el("code", { text: demo.sha.slice(0, 7) })) : null,
      el("button", { type: "button", class: "link-button clear-chat", text: "Clear chat",
        title: "Remove this repository's questions and answers (saved answers are kept)",
        onclick: clearChat })),
    el("div", { class: "chips" }, (demo.questions.length ? demo.questions : GENERIC_QUESTIONS)
      .map((q) => el("button", { type: "button", class: "chip", text: q, title: q, onclick: () => ask(q) }))),
  );
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
      turns: [...conversation.querySelectorAll(".turn")], retrieved: state.retrieved,
    });
  }
  const chat = state.chats.get(demo.id);
  state.demo = demo;
  state.retrieved = chat?.retrieved || new Map();
  state.graphMode = "focus";
  renderRepoTabs();
  renderRepoCard(demo);
  conversation.replaceChildren($("#repo-card"), ...(chat?.turns || []));
  conversation.scrollTop = chat ? conversation.scrollHeight : 0;
  updateUrl({ repo: demo.id, q: null });

  if (!state.graphs.has(demo.id)) {
    state.graphs.set(demo.id, await fetchJSON(`/api/repos/${demo.id}/graph`).catch(() => null));
  }
  if (state.demo === demo) {
    state.graph = state.graphs.get(demo.id);
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

async function ask(question) {
  question = question.trim();
  if (!question || state.busy || !state.demo) return;
  state.busy = true;
  $("#ask-button").disabled = true;
  $("#question").value = "";
  autosize();
  updateUrl({ repo: state.demo.id, q: question });
  const repo = state.demo;               // the answer belongs here even if the visitor switches

  const turnId = ++state.turn;
  const status = el("div", { class: "status" }, el("span", { class: "spinner" }), el("span", { text: "Searching the code…" }));
  const body = el("div", { class: "body" });
  const answer = el("div", { class: "answer" }, status, body);
  const turn = el("article", { class: "turn", "data-turn": turnId }, el("div", { class: "question", text: question }), answer);
  const conversation = $("#conversation");
  conversation.append(turn);
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

  try {
    const response = await fetch(`/api/repos/${repo.id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody(question)),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const wait = response.headers.get("Retry-After");
      throw new Error(response.status === 429
        ? `You've asked a lot of questions. Try again in ${Math.ceil((wait || 60) / 60)} min, or add your own API key in settings.`
        : detail.error || (detail.detail && JSON.stringify(detail.detail)) || `Request failed (${response.status})`);
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
          answer.append(renderSources(turnId, sources));
          const retrieved = new Map(sources.map((s) => [s.chunk_id, s.hop]));
          if (state.demo?.id === repo.id) {
            state.retrieved = retrieved;
            state.graphMode = "focus";
            drawGraph();
          } else if (state.chats.has(repo.id)) {
            state.chats.get(repo.id).retrieved = retrieved;    // visitor switched away
          }
          status.lastChild.textContent = "Writing the answer…";
        } else if (event.type === "token") {
          text += data.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (event.type === "done") {
          cancelAnimationFrame(frame);
          paint();
          status.remove();
          markCited(turn, citedSources(text, sources.length));
          const seconds = (data.llm_ms / 1000).toFixed(1);
          const copy = el("button", { type: "button", class: "link-button", text: "Copy answer",
            onclick: () => copyText(normalizeCitations(text), copy) });
          const save = saveButton({
            repoId: repo.id, repoName: repo.name, question, answer: normalizeCitations(text),
            backend: data.backend, sources: sources.map(slimSource),
          });
          answer.insertBefore(el("div", { class: "meta" },
            el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: data.backend }),
              data.cached ? "from the cache" : el("span", { class: "measured", text: `${seconds} s` })),
            el("span", { class: "actions" }, copy, save)), answer.querySelector(".sources"));
        } else if (event.type === "error") {
          status.remove();
          answer.insertBefore(el("div", { class: "notice", text: errorMessage(data.code, data.message) }), answer.querySelector(".sources"));
        }
      }
    }
  } catch (error) {
    status.remove();
    answer.insertBefore(el("div", { class: "notice", text: error.message }), answer.querySelector(".sources"));
  } finally {
    state.busy = false;
    $("#ask-button").disabled = false;
    status.remove();
  }
}

function renderSources(turnId, sources, { graph = true } = {}) {
  const rows = sources.map((s) => {
    const details = el("details", { class: "source", "data-sid": s.id, "data-chunk": s.chunk_id },
      el("summary", {},
        el("span", { class: "sid", text: `S${s.id}` }),
        el("span", { class: "what" },
          el("span", { class: "name", text: s.name }),
          el("span", { class: "where", text: formatLocation(s) }),
          // debug mode: how it was retrieved, visible without expanding the row
          s.debug ? el("span", { class: "debug", text: debugLine(s.debug) }) : null),
        el("span", { class: "hop" },
          el("i", { class: s.hop === 0 ? "key" : "key key--graph", "aria-hidden": "true" }), hopLabel(s.hop))),
      s.code ? el("pre", {}, el("code", { text: s.code })) : null,
      el("div", { class: "source-actions" },
        s.url ? el("a", { class: "open", href: s.url, target: "_blank", rel: "noopener", text: "Open on GitHub" }) : null,
        el("button", { type: "button", class: "link-button", text: "Copy location",
          onclick: (e) => copyText(`${formatLocation(s)}  ${s.name}`, e.currentTarget) })));
    if (graph) details.addEventListener("toggle", () => { if (details.open) pulseNode(s.chunk_id); });
    return details;
  });
  return el("section", { class: "sources" }, el("h2", { text: `Sources (${sources.length})` }), rows);
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

function slimSource(s) {          // what a saved answer keeps of a source (no code)
  const { id, name, kind, file, start_line, end_line, hop, url } = s;
  return { id, name, kind, file, start_line, end_line, hop, url };
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
    const copy = el("button", { type: "button", class: "link-button", text: "Copy",
      onclick: () => copyText(r.answer, copy) });
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
        renderSources(`saved-${r.id}`, r.sources, { graph: false })));
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

function clearChat() {
  $("#conversation").querySelectorAll(".turn").forEach((t) => t.remove());
  state.chats.delete(state.demo?.id);
  state.retrieved = new Map();
  drawGraph();
}

function markCited(turn, cited) {
  for (const n of cited) turn.querySelector(`.source[data-sid="${n}"] .sid`)?.classList.add("cited");
}

function openSource(turn, n) {
  const row = turn.querySelector(`.source[data-sid="${n}"]`);
  if (!row) return;
  row.open = true;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 1200);
  pulseNode(Number(row.dataset.chunk));
  showPanel("chat");
}

// ── graph ───────────────────────────────────────────────────────────────────
let graphView = null;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function drawGraph() {
  const svg = window.d3 && d3.select("#graph");
  const hint = $("#graph-hint");
  const toggle = $("#graph-toggle");
  if (!svg) { hint.textContent = "The graph library could not be loaded."; return; }
  svg.selectAll("*").remove();
  graphView = null;
  const graph = state.graph;
  if (!graph) { hint.textContent = "No graph for this repository."; toggle.hidden = true; return; }

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
    ? "Ask a question to see which part of the call graph the answer uses." : "";
  if (!data) return;

  const { width, height } = svg.node().getBoundingClientRect();
  if (!width || !height) return;                       // hidden panel (mobile): drawn when shown
  const nodes = data.nodes.map((n) => ({ ...n }));
  const links = data.links.map((l) => ({ ...l }));
  const big = nodes.length > 250;
  const radius = (n) => (state.retrieved.has(n.id) ? 7 : 3.5) + (n.kind === "class" ? 2 : 0);
  // label everything only in small views; otherwise just the retrieved code
  const labelled = (n) => state.retrieved.has(n.id) || nodes.length <= (state.retrieved.size ? 25 : 60);
  const labelText = (n) => n.label || n.name;          // shortest unique qualified name
  const labelWidth = (n) => (labelled(n) ? labelText(n).length * 6.8 + 12 : 0);

  const counts = [...state.retrieved.values()];
  $("#count-direct").textContent = counts.length ? `(${counts.filter((h) => h === 0).length})` : "";
  $("#count-graph").textContent = counts.length ? `(${counts.filter((h) => h > 0).length})` : "";

  const root = svg.append("g");
  const zoom = d3.zoom().scaleExtent([0.1, 4]).on("zoom", (e) => root.attr("transform", e.transform));
  svg.call(zoom);

  svg.append("defs").append("marker")                  // tip sits exactly at the line's end
    .attr("id", "arrow").attr("viewBox", "0 -4 8 8").attr("refX", 8).attr("markerWidth", 6)
    .attr("markerHeight", 6).attr("orient", "auto")
    .append("path").attr("d", "M0,-4L8,0L0,4").attr("fill", cssVar("--calls"));

  const link = root.append("g").selectAll("line").data(links).join("line")
    .attr("class", (l) => `link ${l.type}`)
    .attr("marker-end", (l) => (l.type === "calls" ? "url(#arrow)" : null));

  const node = root.append("g").selectAll("g").data(nodes).join("g")
    .attr("class", (n) => {
      const hop = state.retrieved.get(n.id);
      if (hop === undefined) return state.retrieved.size ? "node neighbour" : "node";
      return hop === 0 ? "node direct" : "node hop";
    })
    .call(d3.drag()
      .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on("click", (e, d) => showNodeCard(d));
  node.append("circle").attr("r", radius);
  node.append("title").text((n) => `${n.name}\n${formatLocation(n)}`);
  // labels centred under the node, so the collision circle below covers them
  node.filter(labelled).append("text").attr("text-anchor", "middle")
    .attr("y", (n) => radius(n) + 11).text(labelText);

  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id).distance((l) => (l.type === "member" ? 28 : 70)).strength(0.5))
    .force("charge", d3.forceManyBody().strength(big ? -35 : state.retrieved.size ? -260 : -170))
    .force("center", d3.forceCenter(width / 2, height / 2))
    // gentle gravity keeps disconnected parts of the graph from drifting apart
    .force("x", d3.forceX(width / 2).strength(big ? 0.12 : 0.07))
    .force("y", d3.forceY(height / 2).strength(big ? 0.12 : 0.07))
    // labels take room too: keep labelled nodes further apart
    .force("collide", d3.forceCollide().radius((n) => Math.min(Math.max(radius(n), labelWidth(n) / 2), 110) + 6))
    .stop();

  const render = () => {
    link.each(function draw(l) {
      // end lines at the target's edge so arrowheads don't sit on the node or its label
      const dx = l.target.x - l.source.x, dy = l.target.y - l.source.y;
      const len = Math.hypot(dx, dy) || 1;
      const back = l.type === "calls" ? radius(l.target) + 2 : 0;
      d3.select(this).attr("x1", l.source.x).attr("y1", l.source.y)
        .attr("x2", l.target.x - (dx / len) * back).attr("y2", l.target.y - (dy / len) * back);
    });
    node.attr("transform", (n) => `translate(${n.x},${n.y})`);
  };
  // lay out before the first paint: no mid-layout jitter, and fit() sees final positions
  for (let i = 0; i < 300; i++) sim.tick();
  render();
  sim.on("tick", render);                               // dragging restarts it

  const fit = (animate) => {
    const focus = nodes.filter((n) => state.retrieved.has(n.id));
    const pts = focus.length ? focus : nodes;
    // bounding box including labels, which extend to the right of each node
    const half = (n) => Math.max(radius(n), labelWidth(n) / 2);
    const x0 = Math.min(...pts.map((n) => n.x - half(n)));
    const x1 = Math.max(...pts.map((n) => n.x + half(n)));
    const y0 = Math.min(...pts.map((n) => n.y - radius(n) - 4));
    const y1 = Math.max(...pts.map((n) => n.y + radius(n) + 16));
    const scale = Math.min(1.5, 0.92 / Math.max((x1 - x0 + 40) / width, (y1 - y0 + 40) / height));
    const transform = d3.zoomIdentity.translate(width / 2, height / 2).scale(scale)
      .translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
    (animate ? svg.transition().duration(400) : svg).call(zoom.transform, transform);
  };
  fit(false);
  graphView = { node };
}

function pulseNode(chunkId) {
  if (!graphView) return;
  graphView.node.classed("pulse", (n) => n.id === chunkId);
  setTimeout(() => graphView?.node.classed("pulse", false), 1600);
}

function showNodeCard(n) {
  const card = $("#node-card");
  const url = githubUrl(state.demo, n.file, n.start_line, n.end_line);
  card.replaceChildren(
    el("h2", { text: n.name }),
    el("p", {}, `${n.kind[0].toUpperCase()}${n.kind.slice(1)} in `, el("code", { text: formatLocation(n) })),
    el("div", { class: "row" },
      el("button", { type: "button", class: "ghost", text: "Ask about this", onclick: () => {
        $("#question").value = `What does \`${n.label || n.name}\` do and who calls it?`;
        autosize();
        showPanel("chat");
        $("#question").focus();
      } }),
      url ? el("a", { class: "ghost", href: url, target: "_blank", rel: "noopener", text: "GitHub ↗" }) : null,
      el("button", { type: "button", class: "ghost", text: "Close", onclick: () => { card.hidden = true; } })));
  card.hidden = false;
}

// ── layout / startup ────────────────────────────────────────────────────────
function showPanel(name) {
  document.querySelectorAll(".panel-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.panel === name)));
  document.querySelectorAll(".layout [data-panel]").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  if (name === "graph") drawGraph();
}

function autosize() {
  const area = $("#question");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 160)}px`;
  area.style.overflowY = area.scrollHeight > 160 ? "auto" : "hidden";
}

async function start() {
  setupSettings();
  setupSaved();
  const openAddRepo = setupAddRepo();
  state.userRepos = loadUserRepos();
  showPanel("chat");
  document.querySelectorAll(".panel-tabs button").forEach((b) => b.addEventListener("click", () => showPanel(b.dataset.panel)));
  $("#graph-toggle").addEventListener("click", () => {
    state.graphMode = state.graphMode === "full" ? "focus" : "full";
    drawGraph();
  });
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#question").value); });
  $("#question").addEventListener("input", autosize);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#question").value); }
  });
  $("#conversation").addEventListener("click", (e) => {
    const cite = e.target.closest("a.cite");
    if (!cite) return;
    e.preventDefault();
    openSource(cite.closest(".turn"), Number(cite.dataset.source));
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      drawGraph();
      $('#repo-tabs [aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
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
