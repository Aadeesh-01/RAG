---
target: coderag home page (frontend/index.html)
total_score: 21
max_score: 32
na_heuristics: 7,10
p0_count: 1
p1_count: 2
target_identity: "file:C:\\projects\\RAG\\frontend\\index.html"
target_fingerprint: "sha256:b95410ee2d5ae49dd702cf1147f8e0f211ef71cb0cb3095865a02555dd9d90e7"
target_path: "C:\\projects\\RAG\\frontend\\index.html"
timestamp: 2026-11-04T22-22-30Z
slug: frontend-index-html
closed: true
---
Method: dual-agent (A: isolated design review · B: detector + browser overlay)

Target: coderag home page (frontend/index.html). Note: earlier snapshots under this slug were of the app, when index.html was the app; scores are not comparable.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Repo list has loading/empty/error text; nothing warns that "Try the sample repository" leads to a sign-in wall. |
| 2 | Match System / Real World | 3 | Developer vocabulary fits; S1/S3 keys unexplained until the steps below. |
| 3 | User Control and Freedom | 2 | Every way into the product (both CTAs, every repo and "Ask:" link) redirects signed-out visitors to /login. |
| 4 | Consistency and Standards | 2 | "Cited (1)" heads two rows (S1 is not cited; no S2); the S3 chip changes on hover but does nothing; amber pulses break the Two Voices Rule. |
| 5 | Error Prevention | 3 | "Python only. Public GitHub repositories. Code is read, never run." sets limits up front. |
| 6 | Recognition Rather Than Recall | 3 | "Ask:" links show real questions. |
| 7 | Flexibility and Efficiency | n/a | Persuade surface. |
| 8 | Aesthetic and Minimalist Design | 3 | Restrained; the graph adds noise behind the caption, and behind the lead at 1024px. |
| 9 | Error Recovery | 2 | Repo error: "reload in a moment", no Retry; heading still says "Indexed and waiting". |
| 10 | Help and Documentation | n/a | Landing page. |
| **Total** | | **21/32** | **Solid foundation, persuasion mechanics fail** |

## Design Specificity Verdict

**LLM assessment:** specific in content, generic in two layout moves. The exhibit is built from the app's own components with verifiable data (processing.py:22-26 really is DataProcessor.transform), and "sent to the model as context, not cited" is copy no other product would write. But the background is a random plexus (nearest-neighbour edges between random points), the most common AI-landing trope, and the page describes its signature ("click one and you are at the exact lines") instead of performing it.

**Deterministic scan:** CLI `impeccable detect --json frontend/index.html`: 0 findings. Browser overlay on 5 views: one rule, `cream-palette` on `body` (rgb 250,248,244) in every light view; none in dark. Not a false positive in reading; it independently confirms the brief's point that the paper ground reads as the generated cream.

## Overall Impression

A credible, honest exhibit wrapped in a page that gates the very thing it wants you to try, and backed by decoration that says "any AI startup". The biggest opportunity: make the hero perform a real citation through the sample repo's real call graph, and let the sample open without the fake sign-in.

## What's Working

1. The hero is a real artefact: the app's own answer components, real file:line ranges, the model id and latency, and an honest "not cited" line.
2. The repo list is concrete and testable: commit hashes, counts in people's words, one-click "Ask:" deep links.
3. Accessibility and theming discipline: skip link first, logical 14-stop tab order, visible focus, re-derived dark colourway, the canvas pauses off-screen and is still under reduced motion.

## Priority Issues

**[P0] The sample is behind a sign-in that accepts anything, and sign-in owns the primary action.** Signed out, "Try the sample repository", every repo name and "Ask:" link go to /login; the amber primary is "Sign in to start". The page's one job is the sample; the gate costs trust at the moment of commitment. Fix: open the demo repos without sign-in; make "Try the sample repository" the primary; sign-in to the header. Command: onboard.

**[P1] The proof is told, not shown.** S3 is inert and the code never appears. Fix: perform it: a real citation found through the real call graph, and the cited lines 22-26 visible with S3 tagged. Command: bolder.

**[P1] The exhibit contradicts itself.** "Cited (1)" over two rows; S1 uncited; S2 absent. Fix: S3 under Cited; S1 under "Also retrieved (9)"; a one-line key. Command: clarify.

**[P2] The background is generic decoration in the way of text.** Random graph, barely visible in light, crosses the caption at 1440 and the lead at 1024; amber pulses and glow break the Two Voices and No-Lamp rules. Fix: the sample repo's real call graph with the answer's real path, never under text. Command: animate.

**[P2] Repo list error/empty states are dead ends.** No Retry; heading unconditional; aria-live announces the whole list. Fix: Retry, a static sample fallback, conditional heading, a separate status line. Command: harden.

## Persona Red Flags

**Skeptical senior developer:** S3 fills blue on hover, click does nothing; "Cited (1)" with two rows; "Try the sample" lands on a login after the footer said any password works; the sample is the only repo with no commit hash.

**First-time visitor on a phone:** the exhibit starts at y≈644; the cited rows are below the fold; either CTA leads to a password form.

**Keyboard / screen-reader user:** the exhibit has no tab stops; "Cited (1)" is an h2 inside the figure (a fake section in the outline); "S3" announced without meaning; the whole repo list is announced on load.

## Minor Observations

- Step numbers in mono Readout Blue are costume by DESIGN.md's own rule.
- "CSV -> clean -> SQLite" uses ASCII arrows.
- Signed in, "Open coderag" and "Try the sample repository" are near-duplicates.
- The page ends on the demo-sign-in caveat.
- Light primary button contrast ~4.6:1 (passes AA, little margin).
- The headline at 16ch runs 4 lines at 1440.
- The hero fade uses gradients the system's Don'ts exclude.

## Questions to Consider

- If the sign-in protects nothing, why does it own the primary button and gate the sample?
- What if the hero graph were the evidence: the real call graph with the answer's path lit?
- Would a skeptic believe a one-line answer about a toy repo, or a hard question about a library they know?
