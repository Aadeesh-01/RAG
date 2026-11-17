// Run with: node --test frontend/tests/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  citedSources, createSSEParser, focusSubgraph, middleTruncate, normalizeCitations, renderAnswer, viaLabel,
} from "../lib.js";

test("SSE parser handles events split across chunks", () => {
  const feed = createSSEParser();
  assert.deepEqual(feed('event: sources\ndata: {"sources": [1'), []);
  assert.deepEqual(feed(']}\n\nevent: token\r\ndata: {"text": "hi"}\r\n\r\nevent: do'), [
    { type: "sources", data: { sources: [1] } },
    { type: "token", data: { text: "hi" } },
  ]);
  assert.deepEqual(feed('ne\ndata: {"cited": [2]}\n\n'), [{ type: "done", data: { cited: [2] } }]);
});

test("citation variants are normalized", () => {
  assert.equal(normalizeCitations("a【S2】 b [​S3]"), "a[S2] b [S3]");
  assert.deepEqual(citedSources("x【S7】 [S1, S3] [S9]", 8), [1, 3, 7]);
  assert.equal(normalizeCitations("object [S3†L9-L20] and 【S7†L1-L9】"), "object [S3] and [S7]");
});

const identity = { marked: (s) => s, sanitize: (s) => s };

test("citations become chips; out-of-range ones are flagged", () => {
  const html = renderAnswer("calls it【S2】 and [S1, S4]", 3, identity);
  assert.match(html, /<a href="#" class="cite" data-source="2">S2<\/a>/);
  assert.match(html, /data-source="1">S1<\/a> <span class="cite invalid"/);
});

test("answer HTML always goes through the sanitizer", () => {
  const seen = [];
  const sanitize = (s) => { seen.push(s); return s.replace(/<script.*?<\/script>/g, ""); };
  const html = renderAnswer("hi <script>alert(1)</script> [S1]", 1, { marked: (s) => s, sanitize });
  assert.equal(seen.length, 1);
  assert.doesNotMatch(html, /script/);
  assert.match(html, /data-source="1"/);
});

test("a fake chip in the answer text cannot inject markup", () => {
  // the placeholder characters are inserted by us after sanitizing; literal
  // ones in the answer only ever become a numeric chip
  const html = renderAnswer('⟦S1" onmouseover="x⟧', 1, identity);
  assert.doesNotMatch(html, /onmouseover="x"/);
});

test("focus subgraph keeps retrieved nodes and their neighbours", () => {
  const graph = {
    nodes: [0, 1, 2, 3].map((id) => ({ id })),
    links: [{ source: 0, target: 1 }, { source: 1, target: 2 }, { source: 3, target: 3 }],
  };
  const sub = focusSubgraph(graph, new Map([[1, 0]]));
  assert.deepEqual(sub.nodes.map((n) => n.id), [0, 1, 2]);
  assert.equal(sub.links.length, 2);
});

test("a direct match says nothing; a graph source names its path", () => {
  assert.equal(viaLabel({ hop: 0 }), "");
  assert.equal(viaLabel({ hop: 1, via: ["BehaviorTree.tick"] }), "via BehaviorTree.tick");
  assert.equal(viaLabel({ hop: 2, via: ["A", "B"] }), "via A → B");
  assert.equal(viaLabel({ hop: 2 }), "via the call graph, 2 steps");      // older cached answers
});

test("paths are shortened in the middle, never losing the file name", () => {
  assert.equal(middleTruncate("pipeline/processing.py"), "pipeline/processing.py");
  const long = "PathPlanning/ParticleSwarmOptimization/particle_swarm_optimization.py";
  assert.equal(middleTruncate(long), "PathPlanning/…/particle_swarm_optimization.py");
  assert.ok(middleTruncate(long, 40).endsWith("/particle_swarm_optimization.py"));
  assert.ok(middleTruncate(long, 40).length <= 40);
  assert.ok(middleTruncate("a/" + "x".repeat(60) + ".py", 30).endsWith(".py"));
});

import { repoOverview, suggestedQuestions, topFolder } from "../lib.js";

const ZOO = {
  nodes: [
    { id: 0, name: "Animal", kind: "class", file: "zoo/animals.py" },
    { id: 1, name: "Mammal", kind: "class", file: "zoo/animals.py" },
    { id: 2, name: "Bat", kind: "class", file: "zoo/animals.py" },
    { id: 3, name: "normalize_name", kind: "function", file: "pipeline/processing.py" },
    { id: 4, name: "transform", label: "DataProcessor.transform", kind: "method", file: "pipeline/processing.py" },
    { id: 5, name: "load", label: "CSVLoader.load", kind: "method", file: "pipeline/loaders.py" },
    { id: 6, name: "main", kind: "function", file: "main.py" },
    { id: 7, name: "report", kind: "function", file: "main.py" },
    { id: 8, name: "main.py", kind: "file", file: "main.py" },
  ],
  links: [
    { source: 1, target: 0, type: "inherits" }, { source: 2, target: 1, type: "inherits" },
    { source: 4, target: 3, type: "calls" }, { source: 5, target: 3, type: "calls" },
    { source: 6, target: 5, type: "calls" }, { source: 6, target: 4, type: "calls" }, { source: 6, target: 7, type: "calls" },
  ],
};

test("overview counts in people's words and finds the repo's shape", () => {
  const o = repoOverview(ZOO);
  assert.deepEqual(o.counts, { files: 4, functions: 5, classes: 3 });
  assert.deepEqual(o.packages, [{ name: "pipeline/", functions: 3 }, { name: "top level", functions: 2 }]);
  assert.deepEqual(o.mostCalled.map((x) => [x.node.name, x.callers]), [["normalize_name", 2]]);
  assert.deepEqual(o.entryPoints.map((n) => n.name), ["main"]);
  assert.deepEqual(o.hierarchy.map((n) => n.name), ["Animal", "Mammal", "Bat"]);
});

test("suggested questions come from the repo's own facts", () => {
  const q = suggestedQuestions(repoOverview(ZOO));
  assert.deepEqual(q, [
    "What happens when `main` runs?",
    "What does `normalize_name` do, and why do 2 places call it?",
    "How does `Bat` extend `Animal`?",
  ]);
});

test("top folder of a path", () => {
  assert.equal(topFolder("main.py"), "");
  assert.equal(topFolder("pipeline/loaders.py"), "pipeline/");
});

test("a chip stays on the line of the word before it", () => {
  const html = renderAnswer("It calls normalize_name [S2]. Then [S1, S3] too", 3, { marked: (x) => x, sanitize: (x) => x });
  assert.match(html, /<span class="cite-run">normalize_name <a href="#" class="cite" data-source="2">S2<\/a>\.<\/span>/);
  assert.match(html, /<span class="cite-run">Then <a[^>]*>S1<\/a> <a[^>]*>S3<\/a><\/span>/);
});

import { safeNext } from "../auth.js";
import { avatarSrc, initials } from "../lib.js";

test("after sign-in, only paths on this site are followed", () => {
  assert.equal(safeNext("/app?repo=sample&q=x"), "/app?repo=sample&q=x");
  for (const bad of ["https://evil.example", "//evil.example/app", "/\\evil.example", "/\t/evil.example",
                     "/\n/evil.example", "javascript:alert(1)", "evil.example", "", null]) {
    assert.equal(safeNext(bad), "/app", String(bad));
  }
});

test("avatar initials", () => {
  assert.equal(initials("Ada Lovelace"), "AL");
  assert.equal(initials("ada@example.com"), "AD");
  assert.equal(initials("x"), "X");
});

test("avatar picture: GitHub's https URL at the asked size, nothing otherwise", () => {
  assert.equal(avatarSrc({ avatar_url: "https://avatars.githubusercontent.com/u/42?v=4" }, 64),
               "https://avatars.githubusercontent.com/u/42?v=4&s=64");
  for (const user of [null, {}, { avatar_url: "" }, { avatar_url: "http://x.example/a.png" },
                      { avatar_url: "javascript:alert(1)" }, { avatar_url: "not a url" }]) {
    assert.equal(avatarSrc(user), null, JSON.stringify(user));
  }
});

import { bridgeCounts, formatDate, middleTruncateName, moduleGraph, plural, questionsPerDay, repoStatus } from "../lib.js";

test("counts in people's words, with correct plurals", () => {
  assert.equal(plural(1, "file"), "1 file");
  assert.equal(plural(2, "file"), "2 files");
  assert.equal(plural(1, "class", "classes"), "1 class");
  assert.equal(plural(3, "class", "classes"), "3 classes");
  assert.equal(plural(undefined, "file"), "0 files");
});

test("long names shorten in the middle; dates never render raw", () => {
  assert.equal(middleTruncateName("short"), "short");
  const long = "a_very_long_function_name_indeed";
  assert.ok(middleTruncateName(long, 20).includes("…"));
  assert.equal(middleTruncateName(long, 20).length, 20);
  assert.equal(formatDate("not a date"), "");
  assert.equal(formatDate(null), "");
  assert.match(formatDate("2026-11-03T13:11:00+00:00", "en-GB"), /2026/);
});

test("questions per day: today last, older days first, bad entries skipped", () => {
  const now = new Date(2026, 10, 17, 12);
  const times = ["2026-11-17T01:00:00", "2026-11-17T09:00:00", "2026-11-16T23:00:00", { t: "2026-11-11T12:00:00" }, "2026-11-01", "nope"];
  assert.deepEqual(questionsPerDay(times, 7, now), [1, 0, 0, 0, 0, 1, 2]);
  assert.deepEqual(questionsPerDay([], 3, now), [0, 0, 0]);
});

test("bridges are counted by mechanism", () => {
  const links = [{ type: "calls" }, { type: "bridge", mechanism: "http" }, { type: "bridge", mechanism: "http" }, { type: "bridge", mechanism: "jni" }];
  assert.deepEqual(bridgeCounts(links), { total: 3, byMechanism: { http: 2, jni: 1 } });
  assert.deepEqual(bridgeCounts([]), { total: 0, byMechanism: {} });
});

test("a repository's status: job first, then an older preset, else indexed", () => {
  assert.equal(repoStatus({}, { status: "running", progress: 0.4 }).key, "indexing");
  assert.equal(repoStatus({}, { status: "queued" }).label, "Queued");
  assert.equal(repoStatus({}, { status: "failed" }).tone, "bad");
  assert.equal(repoStatus({ reindex_to: "v10" }, null).key, "update");
  assert.deepEqual(repoStatus({ id: "x" }, undefined), { key: "indexed", label: "Indexed", tone: "ok" });
});

test("a large graph collapses to one node per file, links counted between files", () => {
  const g = {
    nodes: [{ id: 0, file: "a.py", language: "python" }, { id: 1, file: "a.py" }, { id: 2, file: "b/c.py" }, { id: 3, file: "web.js", language: "javascript" }],
    links: [{ source: 0, target: 1, type: "calls" }, { source: 0, target: 2, type: "calls" }, { source: 1, target: 2, type: "calls" },
            { source: 3, target: 2, type: "bridge", mechanism: "http", label: "HTTP POST /x" }, { source: 2, target: 0, type: "member" }],
  };
  const m = moduleGraph(g);
  assert.deepEqual(m.nodes.map((n) => [n.id, n.name, n.members]), [["m:a.py", "a.py", 2], ["m:b/c.py", "c.py", 1], ["m:web.js", "web.js", 1]]);
  assert.deepEqual(m.links.map((l) => [l.source, l.target, l.type, l.count]),
    [["m:a.py", "m:b/c.py", "calls", 2], ["m:web.js", "m:b/c.py", "bridge", 1]]);
  assert.equal(m.links[1].label, "HTTP POST /x");
});

// ── answer rendering never rewrites sanitized HTML (DEPLOY_AUDIT.md #8) ─────────
import { safeHref } from "../lib.js";

// the crafted answers from the audit: citations inside attributes and code
const CRAFTED = [
  '<img src=x onerror="window.pwned=1">',
  'See <img src="x" alt="a b[S1]<img src=y onerror=window.pwned=2>"> here.',
  '<a href="#" title="x y[S1]&quot; onmouseover=window.pwned=3 z">t</a>',
  "`a [S1] <b>`",
];

test("the sanitizer is the last step: its output is returned untouched", () => {
  for (const answer of CRAFTED) {
    const seen = [];
    const sanitize = (s) => { seen.push(s); return `«sanitized ${seen.length}»`; };
    const html = renderAnswer(answer, 3, { marked: (s) => s, sanitize });
    assert.equal(seen.length, 1, answer);
    assert.equal(html, "«sanitized 1»", answer);                 // nothing added or rewritten afterwards
  }
});

test("the chip markup is built before sanitizing", () => {
  const seen = [];
  renderAnswer("calls it [S1] and [S9]", 2, { marked: (s) => s, sanitize: (s) => { seen.push(s); return s; } });
  assert.match(seen[0], /<a href="#" class="cite" data-source="1">S1<\/a>/);
  assert.match(seen[0], /<span class="cite invalid" title="No such source">S9<\/span>/);
  assert.match(seen[0], /<span class="cite-run">it <a /);
});

test("crafted answers come out without handlers when the sanitizer strips them", () => {
  // a stand-in for DOMPurify's job (event handlers and their values go); the point is that
  // whatever it returns is final, so a citation inside an attribute can't revive one
  const strip = (s) => s.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  for (const answer of CRAFTED) {
    const html = renderAnswer(answer, 3, { marked: (s) => s, sanitize: strip });
    assert.doesNotMatch(html, /\son\w+\s*=/i, answer);
  }
});

// ── links: http(s) and relative only (DEPLOY_AUDIT.md #9) ─────────────────────
test("safeHref keeps http(s) and relative links", () => {
  for (const ok of ["https://github.com/o/r", "http://example.com", "/app?repo=x", "#S2", "?q=1", "a/b.html",
                    "//cdn.example.com/x"]) {
    assert.equal(safeHref(ok), ok, ok);
  }
});

test("safeHref drops script and data URLs in every spelling", () => {
  for (const bad of ["javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)",
                     "java\nscript:alert(1)", "\u0001javascript:x", "data:text/html,<b>x", "vbscript:x",
                     "file:///etc/passwd", "\\\\evil.example\\x", "", null, undefined]) {
    assert.equal(safeHref(bad), null, String(bad));
  }
});
