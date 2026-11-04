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

import { initials, safeNext } from "../auth.js";

test("after sign-in, only paths on this site are followed", () => {
  assert.equal(safeNext("/app?repo=sample&q=x"), "/app?repo=sample&q=x");
  for (const bad of ["https://evil.example", "//evil.example/app", "/\\evil.example", "javascript:alert(1)", "", null]) {
    assert.equal(safeNext(bad), "/app", String(bad));
  }
});

test("avatar initials", () => {
  assert.equal(initials("Ada Lovelace"), "AL");
  assert.equal(initials("ada@example.com"), "AD");
  assert.equal(initials("x"), "X");
});
