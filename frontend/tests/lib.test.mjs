// Run with: node --test frontend/tests/
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  citedSources, createSSEParser, focusSubgraph, normalizeCitations, renderAnswer,
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
