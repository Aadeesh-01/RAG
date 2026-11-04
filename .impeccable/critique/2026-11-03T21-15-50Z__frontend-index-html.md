---
target: coderag web UI (frontend/index.html)
total_score: 26
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
target_identity: "file:C:\\projects\\RAG\\frontend\\index.html"
target_fingerprint: "sha256:a5a53a6dcc639849edec142f722487fcfbfd453e600819afd06426830579082b"
target_path: "C:\\projects\\RAG\\frontend\\index.html"
timestamp: 2026-11-03T21-15-50Z
slug: frontend-index-html
---
Method: dual-agent (A: isolated design review · B: detector + browser overlay)

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Good: streaming phases, cache/time line, job progress. After a repo switch, the legend counts and node card show stale state. |
| 2 | Match System / Real World | 3 | Developer vocabulary fits. "hop 3", "resolved calls" and "Neighbour, not used" are never explained. |
| 3 | User Control and Freedom | 2 | No Stop while streaming. Clear chat has no undo. Settings Close drops edits silently. Chats are lost on reload. |
| 4 | Consistency and Standards | 3 | Saved dialog's "Sources (n)" is oversized. The node card uses ghost buttons where answers use link buttons. "Copy" vs "Copy answer". |
| 5 | Error Prevention | 3 | maxlength, min/max, and up-front Add-repo limits. Clear chat is unguarded. |
| 6 | Recognition Rather Than Recall | 2 | Graph nodes carry no S-ids, so matching a node to a source means remembering function names across panes. |
| 7 | Flexibility and Efficiency | 2 | Enter submits and links can be shared. No `/` shortcut. 25 Tab stops back to the question. Graph not keyboard-reachable. |
| 8 | Aesthetic and Minimalist Design | 3 | Restrained, coherent Console. The repo header and suggestions hold about 340px forever. Large subgraphs become a hairball. |
| 9 | Error Recovery | 3 | Add-repo and quota errors name the fix. Fallbacks can leak `Request failed (400)` or raw JSON. No aria-invalid. |
| 10 | Help and Documentation | 2 | Tooltips and a legend only. Nothing explains hops, why 10 sources show when 1 is cited, or what graph colours prove. |
| **Total** | | **26/40** | **Acceptable** |

## Design Specificity Verdict

**LLM assessment: partly grounded.**
- **The evidence chain is unmistakably this product's.** An `[S3]` chip in the prose matches the S3 badge in the Sources ledger. Its row glyph is the graph's own node glyph. `path:start-end` is set in the mono face, and the commit hash is pinned in the header.
- **The shell is the standard dark LLM-chat template.** Tabs, a repo title, three suggestion rows, an amber user bubble, a bordered answer card, and a bottom ask bar would front any "chat with your docs" product.
- **The graph is decorative today.** It shows everything retrieved rather than what was cited, so it does not yet work as a verification instrument.
- **Amber = person, blue = system holds in code, with two leaks:** amber also marks system-driven highlights (`.source.flash` and `.node.pulse`).

**Deterministic scan.**
- **CLI:** `impeccable detect --json frontend/index.html` found 0 issues. It sees only the static shell, because app.js builds most of the DOM.
- **Browser overlay:** ran on 5 views (initial, answered, Settings, Add repo, 375px). The only rule hit was `tiny-text`: 5 in the initial, Settings and Add repo views, 3 when answered, 0 on mobile.
  - All hits are SVG node labels at 11px, from `.node text` (frontend/styles.css:277).
  - As "body text" this is a false positive, because they are diagram labels. The rule also fired only on the longer dotted names.
  - The underlying fact is real: 11px labels that shrink to about 8px at fit scale. The design review independently flagged the same thing (Sam, and the minor observations).
- **The two disagree on one point only:** neither the detector nor the overlay can see the interaction-level problems (stale state, missing live regions, unfocusable graph). Those come from the design review alone.

**Visual overlays.** Injection succeeded in a headless browser, so there is no open [Human] tab to look at. The overlay screenshots were captured, and the live server has been stopped.

## Overall Impression

This is a disciplined, honest interface whose real signature, the citation-to-source-to-node chain, is half built. Clicking S3 opens the row and pulses the node: that is the best moment in the product. The biggest opportunity is to make the graph and the ledger show what was **cited**, not just what was retrieved. That way verification is the thing the design is about, not something the user reconstructs from memory.

## What's Working

1. **The evidence ledger.** Each source row shows an S-badge, a mono name, `file:lines` and the hop glyph that matches the graph. Cited badges fill solid blue. A citation click opens the row, centres and flashes it, and pulses the node. That makes it possible to check the answer.
2. **Type and theme discipline.**
   - Host Grotesk prose against Atkinson Hyperlegible Mono for measured values reads like an instrument.
   - The paper colourway keeps every meaning.
   - One 2px amber focus ring is visible on every stop.
3. **Honest system copy at the trust points.** Every answer shows the model, the time and "from the cache". Add repo states "Only Python files are downloaded and parsed. Code is never run." API-key and Saved notes say exactly where data lives.

## Priority Issues

**[P1] The graph and Sources show what was retrieved, not what was cited**
- **Why it matters:** verification is the main task.
  - On the requests answer, 2 of 12 sources were cited, yet all 12 nodes light identically.
  - The legend says "Direct match (4) / Via call graph (8)", which are retrieval counts.
  - Nodes carry no S-id, and clicking a node never leads back to its row.
- **Fix:**
  - Give cited nodes their own treatment (full blue plus a ring) and label them `S1`. Dim retrieved-but-uncited nodes.
  - Make a node click open and flash its source row.
  - Split Sources into "Cited (2)", open first, and "Also retrieved (10)", collapsed.
  - Change the legend to Cited / Retrieved / Neighbour.
- **Suggested command:** `$impeccable clarify`, then `$impeccable layout`.

**[P1] State from the previous repo leaks across a tab switch**
- **Why it matters:** after switching from the sample repo to psf/requests, the legend still reads "(4)/(6)". The node card for `BaseLoader` stays open. Its "Ask about this" would send a question about a class that does not exist in requests.
- **Cause:**
  - `drawGraph` returns (app.js:637, 653) before the counts are reset (app.js:666).
  - `selectRepo` (app.js:290) and `clearChat` (app.js:600) never hide `#node-card`.
- **Fix:** reset the counts at the top of `drawGraph`, and hide the node card on a repo switch and on Clear chat.
- **Suggested command:** `$impeccable harden`.

**[P1] The graph and answer events are unreachable without a mouse or sight**
- **Why it matters:** half the product is invisible to keyboard and screen-reader users.
  - `#graph` is one `role="img"` and none of its nodes can be focused, so the node card's "Ask about this" and "GitHub" links are mouse-only.
  - The only live region is `#jobs`, so answer completion and errors are never announced.
  - The remove "x" is a `span role="button"` nested inside a tab `<button>` (app.js:143). That nesting is invalid and cannot be focused.
  - Add-repo errors are not wired to the input.
- **Fix:**
  - Make nodes focusable with an aria-label like "DataProcessor.transform, cited S3, direct match", and open the card on Enter.
  - Add a polite status line: "Answer ready, cites S1 and S5" (or the error).
  - Make the remove control a sibling button.
  - Add `aria-invalid` and `aria-describedby` on the repo URL field.
- **Suggested command:** `$impeccable audit`, then `$impeccable harden`.

**[P2] The repo header and suggestions occupy the top of the chat forever**
- **Why it matters:** about 340px of welcome block stays above every turn. On a 375px screen the first answer starts below the fold, and citation clicks scroll the question away.
- **Fix:** after the first turn, collapse the header to one line (repo, commit, counts, Clear chat). Show suggestions only in the empty state.
- **Suggested command:** `$impeccable distill`.

**[P2] No fast path for keyboard users or narrow screens**
- **Why it matters:**
  - Keyboard: no `/` to focus the question, no skip link, and Esc doesn't close the node card. Each source summary is a Tab stop, so it takes 25 stops to get back. There is no Stop while streaming.
  - At 375px the tab strip hides its scrollbar with no edge cue, and the first tab is clipped to "e repo".
- **Fix:**
  - Add a `/` shortcut and a skip link. Esc closes the node card. Add a Stop button while busy.
  - Fade the tab strip's edges, or use a repo select below 40rem.
- **Suggested command:** `$impeccable adapt`, plus `$impeccable harden`.

## Persona Red Flags

**Alex (power user):**
- No shortcut to the question, and 25 Tab stops to reach it.
- Cannot stop a streaming answer.
- No question history, and chats vanish on reload.
- Settings Close discards edits without a word.

**Sam (keyboard + screen reader):**
- Graph nodes are unfocusable, so the node card is unreachable.
- Answers and errors are not announced.
- The tab close control is nested inside a button and unfocusable.
- Neighbour labels at 0.6 opacity and about 8px at fit scale fail contrast and zoom.

**Riley (stress tester):**
- A repo switch leaves stale legend counts and a stale node card.
- Saved answers drop the code (`slimSource`), so saved evidence can't be checked on repos without a GitHub link.
- The `JSON.stringify(detail)` fallback can print raw validation JSON.
- Clear chat shows with no chat.

**Casey (mobile):**
- The first answer starts below the fold.
- Other repos are invisible in the clipped tab strip.
- A citation click pulses a graph node that is hidden at that moment.
- Settings' action buttons sit below the dialog's scroll edge.

## Minor Observations

- **Graph:**
  - The toolbar grows from 48px to about 76px when "Show full graph" appears, which shifts the legend.
  - The answer-focus fit leaves a large empty band at the top.
  - Node labels are 11px (styles.css:277) and are the only text below the 12px floor.
  - The node card shows no S-id or hop.
- **Sources:** `pre` blocks clip long lines and show no line numbers, even though the rows advertise line ranges.
- **Saved dialog:** "Sources (10)" is oversized, and the first source row looks highlighted on open.
- **Settings at 375px:** the "Free model on this server (…)" label breaks into three awkward columns.
- **Wording:** the top-bar "Add repo" and the dialog's "Index" submit could share one verb.

## Questions to Consider

- If an answer cites 2 of 12 sources, what is the graph for? Should it show the path between cited nodes and drop the retrieval neighbourhood?
- Is a transcript that disappears on reload the right model for exploring a repo? Or should answers be pinned to the code they cite?
- Will anyone decode amber = you, blue = the system? If the rule matters, system highlights (row flash, node pulse) should stop borrowing amber.
