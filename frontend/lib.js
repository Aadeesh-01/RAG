// Pure helpers shared by app.js and the Node tests (no DOM access here).

/**
 * Incremental Server-Sent Events parser. Feed it decoded text chunks as they
 * arrive; it returns complete events and keeps partial ones buffered.
 */
export function createSSEParser() {
  let buffer = "";
  return function feed(chunk) {
    buffer += chunk.replace(/\r\n/g, "\n");
    const events = [];
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let type = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) type = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length) events.push({ type, data: JSON.parse(data.join("\n")) });
    }
    return events;
  };
}

/** gpt-oss writes 【S2】 and [​S3]; turn every variant into [S2]. */
export function normalizeCitations(text) {
  return (text || "")
    .replace(/[​‌‍⁠﻿]/g, "")
    .replace(/【/g, "[")
    .replace(/】/g, "]")
    .replace(/(S\d+)†[^\]\s,;]*/g, "$1");        // [S3†L9-L20] -> [S3]
}

const CITATION = /\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]/g;
const MARK = /⟦S(\d+)⟧/g;                // ⟦S2⟧ placeholder survives markdown

/**
 * Markdown answer -> sanitized HTML with citations as clickable chips.
 * `marked` and `sanitize` are injected (DOMPurify in the browser). LLM output
 * is untrusted, so the sanitizer is the LAST step: the chip markup is built
 * first, and nothing ever rewrites the sanitized HTML (a regex over sanitized
 * HTML can split a tag and bring attribute text back to life).
 */
/**
 * DOMPurify options for answers (model output, which a question or indexed code can steer): on top of
 * DOMPurify's defaults (no scripts, event handlers or javascript: URLs), no forms or inputs (no fake
 * sign-in boxes), no <style> and no style attributes (no page-covering overlays), no frames or media.
 * data-* attributes stay: the citation chips use data-source.
 */
export const SANITIZE_OPTIONS = {
  FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "option", "iframe", "frame", "object", "embed",
                "video", "audio", "source", "dialog"],
  FORBID_ATTR: ["style", "srcset", "formaction", "autofocus"],
};

export function renderAnswer(text, nSources, { marked, sanitize }) {
  const marked_ = normalizeCitations(text).replace(CITATION, (_, group) =>
    group.split(/[,;]/).map((ref) => `⟦${ref.trim()}⟧`).join(" "));
  const html = marked(marked_)
    // keep chips with the word before them (and the punctuation after), so a chip
    // never starts a line on its own
    .replace(/([^\s<>]+)\s*((?:⟦S\d+⟧\s*)*⟦S\d+⟧)([.,;:!?)]*)/g, '<span class="cite-run">$1 $2$3</span>')
    .replace(MARK, (_, n) => {
      const valid = Number(n) >= 1 && Number(n) <= nSources;
      return valid
        ? `<a href="#" class="cite" data-source="${Number(n)}">S${Number(n)}</a>`
        : `<span class="cite invalid" title="No such source">S${Number(n)}</span>`;
    });
  return sanitize(html);
}

/**
 * `value` if it is safe as a link: a relative URL ("/app", "#S2", "?q=") or an
 * http(s) one; otherwise null (javascript:, data:, vbscript: ... including the
 * "java\tscript:" spellings browsers read as the same thing).
 */
export function safeHref(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text) return null;
  const probe = text.replace(/[\u0000-\u0020\u007f]/g, "");      // browsers drop tabs, newlines ...
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(probe);
  if (scheme) return /^https?$/i.test(scheme[1]) ? text : null;
  return /^[\\]/.test(probe) ? null : text;                       // "\\host" is read as //host on some
}

/** Valid cited source numbers in an answer, sorted. */
export function citedSources(text, nSources) {
  const found = new Set();
  for (const m of normalizeCitations(text).matchAll(CITATION)) {
    for (const ref of m[1].split(/[,;]/)) {
      const n = Number(ref.trim().slice(1));
      if (n >= 1 && n <= nSources) found.add(n);
    }
  }
  return [...found].sort((a, b) => a - b);
}

export function hopLabel(hop) {
  return hop === 0 ? "Direct match" : `Call graph, hop ${hop}`;
}

/**
 * How a source was found, shown only when it's not the default: a direct match
 * says nothing; a call-graph source names its path ("via A → B").
 */
export function viaLabel(source) {
  if (!source.hop) return "";
  if (source.via?.length) return `via ${source.via.join(" → ")}`;
  return `via the call graph, ${source.hop} ${source.hop === 1 ? "step" : "steps"}`;
}

/** Shorten a path in the middle so the file name survives: "PathPlanning/…/rrt.py". */
export function middleTruncate(path, max = 48) {
  if (path.length <= max) return path;
  const parts = path.split("/");
  const file = parts.pop();
  if (!parts.length || file.length + 2 >= max) return `…${path.slice(-(max - 1))}`;
  const head = parts[0];
  return head.length + file.length + 3 <= max ? `${head}/…/${file}` : `…/${file}`;
}

/**
 * "path:start-end" when the whole range is known; otherwise the path alone (a module or
 * file never shows a partial range). Never prints "undefined" or "null".
 */
export function formatLocation(source) {
  const file = source?.file || "";
  const { start_line: start, end_line: end } = source || {};
  if (file && start && end) return `${file}:${start}-${end}`;
  if (file && start && !(source.kind === "module" || source.kind === "file")) return `${file}:${start}`;
  return file;
}

const ERRORS = {
  rate_limited: "The free model is busy right now. Try again in a minute, or add your own API key in settings.",
  server_quota: "Today's free answers are used up. Add your own API key in settings, or read the sources below.",
  no_llm: "No language model is available, so here are the retrieved sources.",
  unavailable: "The language model could not be reached. The retrieved sources are below.",
  llm_error: "The language model returned an error.",
};

export function errorMessage(code, fallback) {
  return ERRORS[code] || fallback || "Something went wrong.";
}

/** Subgraph to draw: the retrieved nodes plus their direct neighbours. */
export function focusSubgraph(graph, retrieved) {
  const keep = new Set(retrieved.keys());
  for (const l of graph.links) {
    if (retrieved.has(l.source) || retrieved.has(l.target)) {
      keep.add(l.source);
      keep.add(l.target);
    }
  }
  return {
    nodes: graph.nodes.filter((n) => keep.has(n.id)),
    links: graph.links.filter((l) => keep.has(l.source) && keep.has(l.target)),
  };
}

// ── the repository's shape, from its call graph ────────────────────────────
const CODE_KINDS = new Set(["function", "method", "class"]);
const ENTRY_NAMES = new Set(["main", "cli", "run", "app", "serve", "entry", "entrypoint"]);
const ENTRY_FILES = new Set(["__main__.py", "main.py", "cli.py", "run.py", "app.py", "manage.py"]);

/** Top-level folder of a path ("pipeline/"), or "" for files at the root. */
export function topFolder(path) {
  const i = path.indexOf("/");
  return i < 0 ? "" : `${path.slice(0, i)}/`;
}

/**
 * Facts for the empty state: counts in people's words, the packages, the most
 * called code, likely entry points and the deepest class hierarchy.
 */
export function repoOverview(graph) {
  const nodes = graph.nodes.filter((n) => CODE_KINDS.has(n.kind));
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const calls = graph.links.filter((l) => l.type === "calls" && l.source !== l.target);
  const callers = new Map(), callees = new Map();
  for (const l of calls) {
    (callers.get(l.target) || callers.set(l.target, new Set()).get(l.target)).add(l.source);
    (callees.get(l.source) || callees.set(l.source, new Set()).get(l.source)).add(l.target);
  }
  const fn = (n) => n.kind === "function" || n.kind === "method";
  const counts = {
    files: new Set(graph.nodes.map((n) => n.file)).size,
    functions: nodes.filter(fn).length,
    classes: nodes.filter((n) => n.kind === "class").length,
  };

  const packages = new Map();
  for (const n of nodes.filter(fn)) {
    const key = topFolder(n.file) || "top level";
    packages.set(key, (packages.get(key) || 0) + 1);
  }

  const mostCalled = nodes
    .map((n) => ({ node: n, callers: callers.get(n.id)?.size || 0 }))
    .filter((x) => x.callers >= 2)
    .sort((a, b) => b.callers - a.callers || (a.node.label || a.node.name).localeCompare(b.node.label || b.node.name))
    .slice(0, 5);

  const uncalled = nodes.filter((n) => n.kind === "function" && !callers.get(n.id)?.size);
  const named = uncalled.filter((n) => ENTRY_NAMES.has(n.name) || ENTRY_FILES.has(n.file.split("/").pop()));
  const busy = uncalled.filter((n) => !named.includes(n) && (callees.get(n.id)?.size || 0) >= 2)
    .sort((a, b) => callees.get(b.id).size - callees.get(a.id).size);
  const entryPoints = [...named, ...busy].slice(0, 4);

  // deepest chain of inherits links, root base first
  const bases = new Map();
  for (const l of graph.links.filter((x) => x.type === "inherits")) {
    (bases.get(l.source) || bases.set(l.source, []).get(l.source)).push(l.target);
  }
  const chainFrom = (id, seen = new Set()) => {
    if (seen.has(id)) return [];
    seen.add(id);
    let best = [];
    for (const b of bases.get(id) || []) {
      const c = chainFrom(b, seen);
      if (c.length > best.length) best = c;
    }
    return [...best, id];
  };
  let hierarchy = [];
  for (const id of bases.keys()) {
    const c = chainFrom(id);
    if (c.length > hierarchy.length) hierarchy = c;
  }

  return {
    counts,
    packages: [...packages].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([name, functions]) => ({ name, functions })),
    mostCalled,
    entryPoints,
    hierarchy: hierarchy.length >= 2 ? hierarchy.map((id) => byId.get(id)) : [],
  };
}

/** Questions about this repository, from its own facts (different for every repo). */
export function suggestedQuestions(overview) {
  const name = (n) => n.label || n.name;
  const questions = [];
  const [entry] = overview.entryPoints;
  if (entry) questions.push(`What happens when \`${name(entry)}\` runs?`);
  const [top] = overview.mostCalled;
  if (top) questions.push(`What does \`${name(top.node)}\` do, and why do ${top.callers} places call it?`);
  const h = overview.hierarchy;
  if (h.length >= 2) questions.push(`How does \`${name(h.at(-1))}\` extend \`${name(h[0])}\`?`);
  if (questions.length < 3 && overview.packages.length > 1) {
    const [a, b] = overview.packages;
    questions.push(`How do the ${a.name.replace(/\/$/, "")} and ${b.name.replace(/\/$/, "")} parts work together?`);
  }
  return questions.slice(0, 3);
}

/** Two letters for the avatar: "Ada Lovelace" -> "AL", "ada@x.io" -> "AD". */
export function initials(name) {
  const words = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}

/** The user's GitHub picture at `size` px, or null (no picture, or not https). */
export function avatarSrc(user, size = 64) {
  if (!user || typeof user.avatar_url !== "string" || !user.avatar_url) return null;
  let url;
  try { url = new URL(user.avatar_url); } catch { return null; }
  if (url.protocol !== "https:") return null;
  url.searchParams.set("s", String(size));
  return url.href;
}

// ── the console: counts, dates and collapsed views (pure, tested in tests/lib.test.mjs) ──

/** "1 file", "2 files", "1 class", "3 classes": a count in people's words. */
export function plural(n, one, many = `${one}s`) {
  const count = Number.isFinite(n) ? n : 0;
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** Shorten any name in the middle: "a_very_long_function_name" -> "a_very_lo…ion_name". */
export function middleTruncateName(name, max = 28) {
  const text = String(name ?? "");
  if (text.length <= max) return text;
  const head = Math.ceil((max - 1) / 2), tail = Math.floor((max - 1) / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`;
}

/** "3 Nov 2026" for an ISO timestamp, or "" when it isn't one (never a raw ISO string). */
const dateFormats = new Map();             // one Intl formatter per locale: building one per call was slow

export function formatDate(iso, locale = undefined) {
  const at = Date.parse(iso || "");
  if (Number.isNaN(at)) return "";
  const key = locale || "";
  if (!dateFormats.has(key)) dateFormats.set(key, new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", year: "numeric" }));
  return dateFormats.get(key).format(new Date(at));
}

/**
 * Questions per day for the last `days` days, oldest first, from ISO timestamps.
 * Days are local calendar days; the last entry is today.
 */
export function questionsPerDay(times, days = 7, now = new Date()) {
  const counts = new Array(days).fill(0);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const DAY = 86400000;
  for (const t of times || []) {
    const at = Date.parse(typeof t === "string" ? t : t?.t || "");
    if (Number.isNaN(at)) continue;
    const day = new Date(at);
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
    const back = Math.round((today - start) / DAY);
    if (back >= 0 && back < days) counts[days - 1 - back] += 1;
  }
  return counts;
}

/** Bridge edges by mechanism: {total, byMechanism: {http: n, jni: n, ...}}. */
export function bridgeCounts(links) {
  const byMechanism = {};
  let total = 0;
  for (const l of links || []) {
    if (l.type !== "bridge") continue;
    total += 1;
    const m = l.mechanism || "other";
    byMechanism[m] = (byMechanism[m] || 0) + 1;
  }
  return { total, byMechanism };
}

/**
 * The state of a repository as the Repos table shows it: an indexing job wins, then a
 * failed one, then an index an older preset built ("update"), else indexed.
 */
export function repoStatus(info, job) {
  if (job && (job.status === "queued" || job.status === "running" || job.status === "indexing")) {
    // a rebuild (its files were lost) counts as indexing for the filters, and says what it is
    const label = job.status === "queued" ? "Queued" : job.kind === "rebuild" ? "Rebuilding" : "Indexing";
    return { key: "indexing", label, tone: "warn", progress: job.progress ?? 0 };
  }
  if (job && job.status === "failed") return { key: "failed", label: "Failed", tone: "bad" };
  if (info?.reindex_to) return { key: "update", label: "Update available", tone: "warn" };
  return { key: "indexed", label: "Indexed", tone: "ok" };
}

/**
 * The first `n` lines of an answer as plain text, for a preview: markdown markers and
 * code fences dropped, citations kept as plain "S1", blank lines skipped.
 */
export function answerLines(text, n = 2) {
  const out = [];
  let fenced = false;
  for (const raw of String(text || "").split("\n")) {
    if (/^\s*(```|~~~)/.test(raw)) { fenced = !fenced; continue; }
    if (fenced) continue;
    const line = raw
      .replace(/\[S(\d+)\]/g, "S$1")
      .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/, "")
      .replace(/(\*\*|__)(.*?)\1/g, "$2").replace(/(^|\W)[*_](\S.*?)[*_](?=\W|$)/g, "$1$2")
      .replace(/`([^`]*)`/g, "$1").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/\s+([.,;:!?])/g, "$1").replace(/\s{2,}/g, " ").trim();
    if (!line || /^[-*_]{3,}$/.test(line)) continue;
    out.push(line);
    if (out.length === n) break;
  }
  return out;
}

/** "just now", "5 minutes ago", "yesterday", "3 weeks ago" for an ISO time ("" if unknown). */
export function timeAgo(iso, now = Date.now(), locale = undefined) {
  const at = Date.parse(iso || "");
  if (Number.isNaN(at)) return "";
  const key = `rel:${locale || ""}`;
  if (!dateFormats.has(key)) dateFormats.set(key, new Intl.RelativeTimeFormat(locale, { numeric: "auto" }));
  const rtf = dateFormats.get(key);
  let value = (at - now) / 1000;
  for (const [size, unit] of [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.345, "week"], [12, "month"], [Infinity, "year"]]) {
    if (Math.abs(value) < size) return unit === "second" ? "just now" : rtf.format(Math.round(value), unit);
    value /= size;
  }
  return "";
}

/**
 * A large repository collapsed to its modules: one node per file, one link per pair of
 * files with a call, inheritance or bridge between them (counted). Ids are "m:<path>".
 */
export function moduleGraph(graph) {
  const fileOf = new Map(graph.nodes.map((n) => [n.id, n.file]));
  const files = new Map();
  for (const n of graph.nodes) {
    const f = files.get(n.file) || files.set(n.file, { id: `m:${n.file}`, kind: "module", file: n.file, name: n.file.split("/").pop(),
                                                       label: n.file.split("/").pop(), start_line: 1, members: 0, language: n.language }).get(n.file);
    f.members += 1;
    if (n.language && !f.language) f.language = n.language;
  }
  const pairs = new Map();
  for (const l of graph.links) {
    if (l.type === "member") continue;
    const a = fileOf.get(l.source), b = fileOf.get(l.target);
    if (!a || !b || a === b) continue;
    const key = `${a}>${b}:${l.type === "bridge" ? "bridge" : "calls"}`;
    const p = pairs.get(key) || pairs.set(key, { source: `m:${a}`, target: `m:${b}`, type: l.type === "bridge" ? "bridge" : "calls", count: 0,
                                                 mechanism: l.mechanism, label: l.label, evidence: l.evidence, confidence: l.confidence }).get(key);
    p.count += 1;
  }
  return { nodes: [...files.values()], links: [...pairs.values()], collapsed: true };
}
