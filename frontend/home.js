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

// signed in: the header and the secondary action lead to the app (the primary stays the demo)
const user = currentUser();
if (user) {
  const label = el("span", { "aria-hidden": "true", text: initials(user.name) });
  const src = avatarSrc(user);
  const picture = src && el("img", { alt: "", src, referrerpolicy: "no-referrer",
                                     onload: () => { label.hidden = true; }, onerror: (e) => e.target.remove() });
  $("#account").replaceChildren(
    el("a", { class: "avatar", href: "/app", title: `Signed in as ${user.name}`, "aria-label": `Open coderag (signed in as ${user.name})` },
      label, picture));
  const secondary = $("#hero-sign-in");
  secondary.textContent = "Open your repositories";
  secondary.href = "/app?view=repos";
}

// the hero: one real citation, found through the real call graph, once
window.coderagHero = mountHeroGraph($("#exhibit"));
