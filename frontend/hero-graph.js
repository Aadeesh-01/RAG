/**
 * hero-graph.js: the home page hero performs one real citation, once.
 *
 * The question is asked; retrieval lights the functions that matched; the call
 * from DataProcessor.transform to normalize_name is traced through the sample
 * repository's real call graph; the answer lands citing S3. Then it rests.
 * Any pointer, key, wheel or touch skips to the end; Replay plays it again.
 *
 * The page's markup is the final state, so without JavaScript (or under
 * prefers-reduced-motion) the hero simply shows the result. The sequence is
 * CSS: this module only adds stage classes. Colours are the page's tokens.
 */

const SVG = "http://www.w3.org/2000/svg";

// The sample repository's pipeline, as served by /api/repos/sample/graph. This copy is
// only used when the server can't be reached; both come from the same index.
const SNAPSHOT = {
  nodes: [
    ["BaseLoader", "class", "pipeline/loaders.py", 5, 12],
    ["CSVLoader", "class", "pipeline/loaders.py", 15, 23],
    ["JSONLoader", "class", "pipeline/loaders.py", 26, 31],
    ["run_pipeline", "function", "pipeline/main.py", 8, 16],
    ["report", "function", "pipeline/main.py", 19, 24],
    ["main", "function", "pipeline/main.py", 27, 32],
    ["BaseProcessor", "class", "pipeline/processing.py", 1, 14],
    ["BaseProcessor.process", "method", "pipeline/processing.py", 6, 8],
    ["BaseProcessor.validate", "method", "pipeline/processing.py", 10, 11],
    ["DataProcessor", "class", "pipeline/processing.py", 17, 26],
    ["DataProcessor.transform", "method", "pipeline/processing.py", 22, 26],
    ["normalize_name", "function", "pipeline/processing.py", 29, 31],
    ["SQLiteStore.save", "method", "pipeline/storage.py", 16, 20],
    ["SQLiteStore.fetch_all", "method", "pipeline/storage.py", 22, 23],
  ],
  links: [
    ["run_pipeline", "BaseProcessor.process", "calls"],
    ["run_pipeline", "SQLiteStore.save", "calls"],
    ["report", "SQLiteStore.fetch_all", "calls"],
    ["main", "run_pipeline", "calls"],
    ["main", "report", "calls"],
    ["BaseProcessor.process", "BaseProcessor", "calls"],
    ["BaseProcessor.process", "BaseProcessor.validate", "calls"],
    ["BaseProcessor.process", "DataProcessor.transform", "calls"],
    ["DataProcessor.transform", "normalize_name", "calls"],
    ["CSVLoader", "BaseLoader", "inherits"],
    ["JSONLoader", "BaseLoader", "inherits"],
    ["DataProcessor", "BaseProcessor", "inherits"],
  ],
};

// The example answer: retrieval's direct matches (S1-S4) and the one it cites.
const SOURCES = { "normalize_name": 1, "DataProcessor": 2, "DataProcessor.transform": 3, "BaseLoader": 4 };
const CITED = "DataProcessor.transform";
const TRACE = ["DataProcessor.transform", "normalize_name"];        // the call that makes it the answer
const CHAIN = new Set(["main", "run_pipeline", "BaseProcessor.process", "DataProcessor.transform", "normalize_name"]);

// Hand-placed, so it is the same on every load: [x, y] as fractions of the panel, and the label's side.
// The call chain runs along the middle; what it touches sits above and below it.
const WIDE = {
  "BaseLoader": [0.05, 0.1, "right"],
  "CSVLoader": [0.05, 0.31, "below-start"],
  "JSONLoader": [0.23, 0.31, "below"],
  "SQLiteStore.save": [0.355, 0.3, "above"],
  "BaseProcessor": [0.565, 0.2, "above"],
  "DataProcessor": [0.8, 0.2, "above"],
  // the chain's labels alternate above and below, so long names never meet
  "main": [0.045, 0.56, "below-start"],
  "run_pipeline": [0.235, 0.56, "above"],
  "BaseProcessor.process": [0.47, 0.56, "below"],
  "DataProcessor.transform": [0.715, 0.56, "above"],
  "normalize_name": [0.955, 0.56, "below-end"],
  "report": [0.15, 0.86, "below"],
  "SQLiteStore.fetch_all": [0.355, 0.86, "below"],
  "BaseProcessor.validate": [0.665, 0.86, "below"],
};
// A phone gets the chain alone, top to bottom.
const NARROW = {
  "main": [0.08, 0.08, "right"],
  "run_pipeline": [0.08, 0.29, "right"],
  "BaseProcessor.process": [0.08, 0.5, "right"],
  "DataProcessor.transform": [0.08, 0.71, "right"],
  "normalize_name": [0.08, 0.92, "right"],
};

// The sequence (ms). One easing and one duration family live in the CSS.
const TIMELINE = { type: [80, 650], retrieved: 700, traced: 1000, answered: 1450, rows: 1700, rest: 1950 };

const calm = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const el = (name, attrs = {}) => {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

async function loadGraph() {
  try {
    const response = await fetch("/api/repos/sample/graph");
    if (!response.ok) throw new Error(String(response.status));
    const g = await response.json();
    const byId = new Map(g.nodes.map((n) => [n.id, n]));
    const nodes = g.nodes.filter((n) => n.label in WIDE)
      .map((n) => [n.label, n.kind, n.file, n.start_line, n.end_line]);
    const links = g.links.filter((l) => l.type !== "member")
      .map((l) => [byId.get(l.source)?.label, byId.get(l.target)?.label, l.type])
      .filter(([a, b]) => a in WIDE && b in WIDE);
    if (nodes.length < 10) throw new Error("graph changed");          // not the sample we drew
    return { nodes, links };
  } catch {
    return SNAPSHOT;
  }
}

export function mountHeroGraph(exhibit) {
  const panel = exhibit.querySelector(".hero-graph");
  const prompt = exhibit.querySelector(".prompt-text");
  const replay = exhibit.querySelector("#replay");
  const question = prompt.textContent;
  let data = SNAPSHOT;
  let timers = [];
  let playing = false;

  // ── drawing ──────────────────────────────────────────────────────────────
  const svg = el("svg", { role: "group", "aria-label": "The sample repository's call graph, pipeline package" });
  panel.replaceChildren(svg);

  function draw() {
    const { width, height } = panel.getBoundingClientRect();
    if (!width || !height) return;
    const layout = width < 440 ? NARROW : WIDE;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    // a narrower panel keeps the labels that carry the story: the call chain and the answer's matches
    svg.classList.toggle("compact", width < 600);
    const at = (label) => ({ x: layout[label][0] * width, y: layout[label][1] * height });
    const nodes = data.nodes.filter(([label]) => label in layout);
    const links = data.links.filter(([a, b]) => a in layout && b in layout);

    const defs = el("defs");
    const marker = el("marker", { id: "hg-arrow", viewBox: "0 -4 8 8", refX: "8", markerWidth: "7", markerHeight: "7", orient: "auto" });
    marker.append(el("path", { d: "M0,-4L8,0L0,4" }));
    const markerTrace = marker.cloneNode(true);
    markerTrace.id = "hg-arrow-trace";
    defs.append(marker, markerTrace);

    const edges = el("g", { class: "hg-edges", "aria-hidden": "true" });
    for (const [a, b, type] of links) {
      const p = at(a), q = at(b);
      const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1;
      const back = type === "calls" ? 8 : 6;                         // stop at the node's edge
      const x2 = q.x - (dx / len) * back, y2 = q.y - (dy / len) * back;
      const trace = a === TRACE[0] && b === TRACE[1];
      const g = el("g", { class: `hg-edge ${type}${trace ? " trace" : ""}`, "data-ends": `${a}|${b}` });
      const line = el("line", { x1: p.x, y1: p.y, x2, y2 });
      if (trace) line.style.setProperty("--len", `${Math.hypot(x2 - p.x, y2 - p.y).toFixed(1)}px`);
      g.append(line);
      edges.append(g);
    }

    const dots = el("g", { class: "hg-nodes" });
    let order = 0;
    for (const [label, kind, file, start, end] of nodes) {
      const { x, y } = at(label);
      const sid = SOURCES[label];
      const cited = label === CITED;
      const focusable = sid === 1 || sid === 3;                      // the two rows the card shows
      const g = el("g", {
        class: `hg-node${sid ? " match" : ""}${cited ? " cited" : ""}${CHAIN.has(label) ? " chain" : ""}`,
        "data-label": label,
        transform: `translate(${x.toFixed(1)},${y.toFixed(1)})`,
        ...(sid ? { "data-sid": sid } : {}),
        ...(focusable ? { tabindex: "0", role: "button",
          "aria-label": `S${sid} ${label}${cited ? ", cited" : ", retrieved"}, ${file} lines ${start} to ${end}. Show its source row.` }
          : { "aria-hidden": "true" }),
      });
      if (sid) g.style.setProperty("--i", String(order++));
      // an invisible, larger circle so the pointer doesn't have to find a 5px dot
      g.append(el("circle", { class: "hit", r: "15" }), el("circle", { class: "ring", r: "9" }), el("circle", { class: "dot", r: sid ? "5.5" : "4" }));
      const title = el("title");
      title.textContent = `${label} (${kind}), ${file}:${start}-${end}`;
      g.append(title);
      const side = layout[label][2];
      const text = el("text", {
        x: side === "right" ? 12 : side === "below-start" ? -6 : side === "below-end" ? 6 : 0,
        y: side === "above" ? -13 : side === "right" ? 0 : 14,
        "text-anchor": side === "right" || side === "below-start" ? "start" : side === "below-end" ? "end" : "middle",
        "dominant-baseline": side === "above" ? "auto" : side === "right" ? "central" : "hanging",
      });
      if (sid) {
        const tag = el("tspan", { class: "sid-tag" });
        tag.textContent = `S${sid} `;
        text.append(tag);
      }
      text.append(document.createTextNode(label));
      g.append(text);
      dots.append(g);
    }
    svg.replaceChildren(defs, edges, dots);
  }

  // ── pointing: an S-number, its row and its node light each other ────────
  const related = (sid) => Object.keys(SOURCES).find((k) => SOURCES[k] === sid);
  function light(sid) {
    exhibit.classList.toggle("lighting", Boolean(sid));
    exhibit.querySelectorAll(".lit").forEach((n) => n.classList.remove("lit"));
    if (!sid) return;
    const label = related(Number(sid));
    exhibit.querySelectorAll(`[data-sid="${sid}"]`).forEach((n) => n.classList.add("lit"));
    // its calls and inheritance, and what's at their other end
    svg.querySelectorAll(".hg-edge").forEach((edge) => {
      const [a, b] = edge.dataset.ends.split("|");
      if (a !== label && b !== label) return;
      edge.classList.add("lit");
      const other = a === label ? b : a;
      svg.querySelector(`.hg-node[data-label="${other}"]`)?.classList.add("lit");
    });
  }
  const onPoint = (e) => {
    const target = e.target.closest?.("[data-sid]");
    if (target && exhibit.contains(target)) light(target.dataset.sid);
  };
  const onLeave = (e) => {
    const target = e.target.closest?.("[data-sid]");
    if (target && !target.contains(e.relatedTarget)) light(null);
  };
  exhibit.addEventListener("pointerover", onPoint);
  exhibit.addEventListener("pointerout", onLeave);
  exhibit.addEventListener("focusin", onPoint);
  exhibit.addEventListener("focusout", onLeave);
  // a node takes you to its row in the card (Enter, Space, or a click)
  const toRow = (e) => {
    const node = e.target.closest?.(".hg-node[tabindex]");
    if (!node || (e.type === "keydown" && e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    exhibit.querySelector(`.src-row[data-sid="${node.dataset.sid}"]`)?.focus();
  };
  svg.addEventListener("click", toRow);
  svg.addEventListener("keydown", toRow);

  // ── the sequence ─────────────────────────────────────────────────────────
  const STAGES = ["typing", "s-retrieved", "s-traced", "s-answered", "s-rows"];
  const at = (ms, fn) => timers.push(setTimeout(fn, ms));

  function finish() {
    timers.forEach(clearTimeout);
    timers = [];
    playing = false;
    exhibit.classList.add("instant");
    exhibit.classList.remove("playing", ...STAGES);
    document.documentElement.classList.remove("hero-intro");
    prompt.textContent = question;
    removeSkips();
    // two frames: let the final state paint without transitions, then restore them
    requestAnimationFrame(() => requestAnimationFrame(() => exhibit.classList.remove("instant")));
  }

  function play() {
    window.__heroStarted = true;
    if (calm()) { finish(); return; }
    timers.forEach(clearTimeout);
    timers = [];
    playing = true;
    exhibit.classList.add("instant", "playing");
    exhibit.classList.remove(...STAGES);
    document.documentElement.classList.remove("hero-intro");
    prompt.textContent = "";
    void exhibit.offsetWidth;                                         // commit the start state
    exhibit.classList.remove("instant");
    addSkips();
    const [typeStart, typeEnd] = TIMELINE.type;
    at(typeStart, () => exhibit.classList.add("typing"));
    const step = (typeEnd - typeStart) / question.length;
    for (let i = 1; i <= question.length; i++) at(typeStart + i * step, () => { prompt.textContent = question.slice(0, i); });
    at(TIMELINE.retrieved, () => exhibit.classList.add("s-retrieved"));
    at(TIMELINE.traced, () => exhibit.classList.add("s-traced"));
    at(TIMELINE.answered, () => exhibit.classList.add("s-answered"));
    at(TIMELINE.rows, () => exhibit.classList.add("s-rows"));
    at(TIMELINE.rest, () => {
      // rest: the final state is the markup; drop the stage classes without a visible change
      exhibit.classList.add("instant");
      exhibit.classList.remove("playing", ...STAGES);
      playing = false;
      removeSkips();
      requestAnimationFrame(() => requestAnimationFrame(() => exhibit.classList.remove("instant")));
    });
  }

  // any input during the sequence jumps to the end (the page is never held hostage)
  const SKIP = ["pointerdown", "keydown", "wheel", "touchstart"];
  const skip = (e) => {
    if (!playing) return;
    if (e.repeat) return;
    finish();
  };
  const addSkips = () => SKIP.forEach((t) => window.addEventListener(t, skip, { capture: true, passive: true }));
  const removeSkips = () => SKIP.forEach((t) => window.removeEventListener(t, skip, { capture: true }));
  const onHidden = () => { if (document.hidden && playing) finish(); };
  document.addEventListener("visibilitychange", onHidden);

  replay.addEventListener("click", (e) => { e.stopPropagation(); play(); });

  const ro = new ResizeObserver(() => draw());
  ro.observe(panel);
  draw();
  loadGraph().then((g) => { data = g; draw(); });
  play();

  return {
    play, finish,
    get playing() { return playing; },
    destroy() {
      finish(); ro.disconnect();
      document.removeEventListener("visibilitychange", onHidden);
      exhibit.removeEventListener("pointerover", onPoint);
      exhibit.removeEventListener("pointerout", onLeave);
      exhibit.removeEventListener("focusin", onPoint);
      exhibit.removeEventListener("focusout", onLeave);
    },
  };
}
