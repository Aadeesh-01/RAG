import { currentUser, initials } from "./auth.js";

const $ = (s) => document.querySelector(s);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// signed in: the header and the main call to action go straight to the app
const user = currentUser();
if (user) {
  $("#account").replaceChildren(
    el("a", { class: "avatar", href: "/app", title: `Signed in as ${user.name}`, "aria-label": `Open coderag (signed in as ${user.name})` },
      el("span", { "aria-hidden": "true", text: initials(user.name) })));
  $("#primary-cta").textContent = "Open coderag";
  $("#primary-cta").href = "/app";
}

const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

async function loadRepos() {
  const list = $("#repo-list");
  try {
    const [demos, health] = await Promise.all([
      fetch("/api/demos").then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
      fetch("/api/health").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    if (health?.indexing) $("#limits").textContent = ` (up to ${plural(health.indexing.max_py_files, "Python file")})`;
    if (!demos.length) {
      list.replaceChildren(el("li", { class: "loading", text: "No repositories are indexed yet. Add one from the app." }));
      return;
    }
    list.replaceChildren(...demos.map((d) => {
      const c = d.counts || {};
      const href = `/app?repo=${encodeURIComponent(d.id)}`;
      const question = d.questions?.[0];
      return el("li", {},
        el("div", { class: "repo-name" },
          el("a", { href, text: d.name }),
          d.sha ? el("code", { title: "Indexed commit", text: d.sha.slice(0, 7) }) : null),
        el("p", { class: "repo-desc", text: d.description || "" }),
        el("p", { class: "repo-stats" },
          `${plural(c.files ?? 0, "file")}, ${plural(c.functions ?? 0, "function")}, ${plural(c.classes ?? 0, "class", "classes")}`),
        question ? el("a", { class: "repo-ask", href: `${href}&q=${encodeURIComponent(question)}` },
          "Ask: ", el("span", { text: question })) : null);
    }));
  } catch {
    list.replaceChildren(el("li", { class: "loading", text: "The repositories couldn't be loaded. The server may be waking up; reload in a moment." }));
  }
}
loadRepos();
