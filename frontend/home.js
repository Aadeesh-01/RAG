import { currentUser } from "./auth.js";
import { mountHeroGraph } from "./hero-graph.js";
import { avatarSrc, initials } from "./lib.js";

const $ = (s) => document.querySelector(s);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// signed in: the header leads to the app (the hero's action stays the sample)
const user = currentUser();
if (user) {
  const label = el("span", { "aria-hidden": "true", text: initials(user.name) });
  const src = avatarSrc(user);
  const picture = src && el("img", { alt: "", src, referrerpolicy: "no-referrer",
                                     onload: () => { label.hidden = true; }, onerror: (e) => e.target.remove() });
  $("#account").replaceChildren(
    el("a", { class: "avatar", href: "/app", title: `Signed in as ${user.name}`, "aria-label": `Open coderag (signed in as ${user.name})` },
      label, picture));
}

// the hero: one real citation, found through the real call graph, once
window.coderagHero = mountHeroGraph($("#exhibit"));

// ── repositories: loading, ready, empty and error each say what's true ────
const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
const list = $("#repo-list");
const status = $("#repo-status");

function repoItem(d) {
  const c = d.counts || {};
  const href = `/app?repo=${encodeURIComponent(d.id)}`;
  const question = d.questions?.[0];
  return el("li", { class: "repo" },
    el("div", { class: "repo-name" },
      el("a", { href, text: d.name }),
      d.sha ? el("code", { title: `Indexed at commit ${d.sha}`, text: d.sha.slice(0, 7) })
        : el("span", { class: "repo-local", text: "local sample" })),
    d.description ? el("p", { class: "repo-desc", text: d.description.replace(/ -> /g, " → ") }) : null,
    el("p", { class: "repo-stats", text: `${plural(c.files ?? 0, "file")}, ${plural(c.functions ?? 0, "function")}, ${plural(c.classes ?? 0, "class", "classes")}` }),
    question ? el("a", { class: "repo-ask", href: `${href}&q=${encodeURIComponent(question)}` }, "Ask: ", el("span", { text: question })) : null);
}

async function loadRepos() {
  list.setAttribute("aria-busy", "true");
  status.textContent = "Loading the repositories…";
  try {
    const [demos, health] = await Promise.all([
      fetch("/api/demos").then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status))))),
      fetch("/api/health").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    if (health?.indexing) $("#limits").textContent = ` (up to ${plural(health.indexing.max_py_files, "Python file")})`;
    if (!demos.length) {
      list.replaceChildren();
      status.replaceChildren("No repositories are indexed on this server yet. Sign in and add one from the app.");
      return;
    }
    list.replaceChildren(...demos.map(repoItem));
    status.textContent = "";
  } catch {
    list.replaceChildren();
    status.replaceChildren("The repositories couldn't be loaded; the server may be waking up. ",
      el("button", { type: "button", class: "link-button", text: "Try again", onclick: loadRepos }));
  } finally {
    list.setAttribute("aria-busy", "false");
  }
}
loadRepos();
