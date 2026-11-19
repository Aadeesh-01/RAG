// The read-only shared answer page (/a/<slug>): GET /api/shares/<slug>, works signed out.
import { SANITIZE_OPTIONS, citedSources, formatDate, renderAnswer, safeHref } from "./lib.js";

const $ = (s) => document.querySelector(s);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "href") {                   // http(s) and relative links only
      const href = safeHref(v);
      if (href !== null) node.setAttribute("href", href);
    } else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const renderers = window.marked && window.DOMPurify
  ? { marked: (s) => window.marked.parse(s), sanitize: (s) => window.DOMPurify.sanitize(s, SANITIZE_OPTIONS) }
  : { marked: (s) => `<p>${s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))}</p>`, sanitize: (s) => s };

function snippet(source) {
  const code = el("code", { class: source.language ? `language-${source.language === "cpp" ? "cpp" : source.language}` : null,
    style: `counter-reset: line ${Math.max((source.start_line || 1) - 1, 0)}` });
  code.textContent = source.snippet;
  if (window.hljs && source.language && window.hljs.getLanguage(source.language)) window.hljs.highlightElement(code);
  return el("pre", { class: "snippet" }, code);
}

function sourceItem(s) {
  const where = `${s.file}${s.start_line ? `:${s.start_line}-${s.end_line}` : ""}`;
  return el("li", { class: "shared-source", id: `S${s.id}` },
    el("div", { class: "row" },
      el("span", { class: s.cited ? "sid cited" : "sid", text: `S${s.id}` }),
      el("span", { class: "name", text: s.name }),
      s.url ? el("a", { class: "where", href: s.url, target: "_blank", rel: "noopener", text: where })
        : el("span", { class: "where", text: where })),
    s.snippet ? snippet(s) : null,
    s.truncated ? el("p", { class: "note", text: "Shortened: the first 40 lines." }) : null);
}

async function main() {
  const box = $("#shared");
  const slug = decodeURIComponent(window.location.pathname.split("/").filter(Boolean).pop() || "");
  let data;
  try {
    const r = await fetch(`/api/shares/${encodeURIComponent(slug)}`);
    data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "This answer could not be loaded.");
  } catch (err) {
    box.removeAttribute("aria-busy");
    box.replaceChildren(el("h1", { text: "Answer not available" }), el("p", { text: err.message }),
      el("p", {}, el("a", { href: "/", text: "Go to coderag" })));
    return;
  }
  document.title = `${data.question} · coderag`;
  const body = el("div", { class: "body" });
  body.innerHTML = renderAnswer(data.answer, data.sources.length ? Math.max(...data.sources.map((s) => s.id)) : 0, renderers);
  const cited = citedSources(data.answer, data.sources.length ? Math.max(...data.sources.map((s) => s.id)) : 0);
  const repo = data.repo || {};
  box.removeAttribute("aria-busy");
  box.replaceChildren(...[
    el("p", { class: "eyebrow" }, "Shared answer about ",
      repo.url ? el("a", { href: repo.url, target: "_blank", rel: "noopener", text: repo.name }) : el("b", { text: repo.name })),
    el("div", { class: "question", text: data.question }),
    el("div", { class: "answer" }, body,
      el("div", { class: "meta" },
        el("span", { class: "by" }, "Answered by", el("span", { class: "measured", text: data.model || "" }),
          data.asked_at ? el("time", { datetime: data.asked_at, text: formatDate(data.asked_at) || "" }) : null))),
    repo.private ? el("p", { class: "note private-note",
      text: "From a private repository: only the answer and the code it cites are shown." }) : null,
    data.sources.length ? el("section", { class: "shared-sources", "aria-label": "Sources" },
      el("h2", { text: repo.private ? "Cited code" : "Sources" }),
      el("ol", {}, data.sources.filter((s) => s.cited || cited.includes(s.id) || !repo.private).map(sourceItem))) : null,
    el("p", { class: "note" }, "A read-only copy. ", el("a", { href: "/", text: "Ask your own questions on coderag" }), "."),
  ].filter(Boolean));                               // the DOM's replaceChildren prints null as text
  // citations [S2] jump to their source
  box.addEventListener("click", (e) => {
    const cite = e.target.closest("a.cite");
    if (!cite) return;
    e.preventDefault();
    document.getElementById(`S${cite.dataset.source}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

main();
