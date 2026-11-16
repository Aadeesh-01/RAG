# coderag

## What it is
coderag answers questions about a Python codebase. Each answer cites the exact functions and lines it came from, found by vector search plus the code's call graph. Clicking a citation opens the code at those lines; the call graph shows the path that reached it.

## Who it is for
Developers who don't trust vague AI answers about code: people reading an unfamiliar repository, reviewing, or onboarding, who want to check every claim against the source.

## What success looks like
- **Home page (`/`), Persuade:** the visitor believes the citations are real, then tries the sample repository. One job, one primary action.
- **App (`/app`), Operate:** the visitor asks, reads the answer, and verifies it against the cited code without leaving the page.

## Product truths (don't contradict these anywhere)
- Python only; public GitHub repositories; code is read, never run.
- Every answer cites numbered sources (S1, S2...); "cited" and "retrieved but not cited" are different and shown differently.
- The sample, requests and click repositories open without signing in. Signing in (a demo account for now; GitHub later) is needed to add repositories.

## Voice
Plain, specific, technical. Name the function and the line; never a slogan. Button labels say exactly what happens.

---
*Written from the owner's home-page brief (section 1: "What coderag is"). Inferred, not stated by the owner: the app's success definition, and the reading/reviewing/onboarding use cases. The demo-repos-open decision was confirmed by the owner on 2026-11-05.*
