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
    .replace(/】/g, "]");
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
  const html = sanitize(marked(marked_));
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
  return hop === 0 ? "direct match" : `call graph · hop ${hop}`;
}

export function formatLocation(source) {
  return source.start_line ? `${source.file}:${source.start_line}–${source.end_line}` : source.file;
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
