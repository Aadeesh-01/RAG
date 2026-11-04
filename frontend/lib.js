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
 * is untrusted: everything goes through the sanitizer; the only HTML added
 * afterwards is our own chip markup around plain numbers.
 */
export function renderAnswer(text, nSources, { marked, sanitize }) {
  const marked_ = normalizeCitations(text).replace(CITATION, (_, group) =>
    group.split(/[,;]/).map((ref) => `⟦${ref.trim()}⟧`).join(" "));
  const html = sanitize(marked(marked_))
    // keep chips with the word before them (and the punctuation after), so a chip
    // never starts a line on its own; the text here is already sanitized, no tags
    .replace(/([^\s<>]+)\s*((?:⟦S\d+⟧\s*)*⟦S\d+⟧)([.,;:!?)]*)/g, '<span class="cite-run">$1 $2$3</span>');
  return html.replace(MARK, (_, n) => {
    const valid = Number(n) >= 1 && Number(n) <= nSources;
    return valid
      ? `<a href="#" class="cite" data-source="${Number(n)}">S${Number(n)}</a>`
      : `<span class="cite invalid" title="No such source">S${Number(n)}</span>`;
  });
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

export function formatLocation(source) {
  return source.start_line ? `${source.file}:${source.start_line}-${source.end_line}` : source.file;
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
