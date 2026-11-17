---
name: coderag
description: Ask questions about a codebase; every answer cites the code it came from, and the graph shows how it got there.
colors:
  canvas: "oklch(96% 0.009 85)"
  card: "oklch(99.2% 0.004 90)"
  card-2: "oklch(94.2% 0.011 82)"
  card-3: "oklch(91.6% 0.013 80)"
  ink: "oklch(17% 0.012 60)"
  ink-2: "oklch(32% 0.012 60)"
  muted: "oklch(45% 0.014 60)"
  rule: "oklch(89% 0.01 82)"
  primary: "oklch(17% 0.012 60)"
  primary-ink: "oklch(98.5% 0.004 90)"
  secondary: "oklch(90.5% 0.012 80)"
  accent: "oklch(50% 0.16 252)"
  accent-soft: "oklch(93% 0.035 250)"
  ok: "oklch(47% 0.13 150)"
  bad: "oklch(50% 0.17 25)"
  warn: "oklch(55% 0.14 70)"
  cat-bridge: "oklch(56% 0.2 335)"
  hop-1: "oklch(60% 0.12 252)"
  hop-2: "oklch(70% 0.085 252)"
  hop-3: "oklch(80% 0.05 252)"
  hop-none: "oklch(72% 0.012 80)"
typography:
  hero:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(2.4rem, 1.6rem + 3.2vw, 4.25rem)"
    fontWeight: 500
    lineHeight: 1.02
    letterSpacing: "-0.03em"
  display:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.875rem"
    fontWeight: 500
    lineHeight: 1.02
    letterSpacing: "-0.03em"
  stat:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "2rem"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "-0.03em"
    fontFeature: "\"tnum\" 1"
  title:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 500
    lineHeight: 1.25
  body:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 500
    lineHeight: 1.25
  measured:
    fontFamily: "Atkinson Hyperlegible Mono, ui-monospace, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: "\"tnum\" 1, \"zero\" 1"
rounded:
  card: "18px"
  inner: "12px"
  code: "10px"
  pill: "999px"
spacing:
  3xs: "2px"
  2xs: "4px"
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  xl: "40px"
  2xl: "64px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-ink}"
    rounded: "{rounded.pill}"
    padding: "0 20px"
    height: "40px"
    typography: "{typography.label}"
  button-secondary:
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "40px"
    typography: "{typography.label}"
  filter-pill:
    backgroundColor: "{colors.card-2}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "32px"
    typography: "{typography.label}"
  filter-pill-active:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-ink}"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "20px"
  stat-tile:
    backgroundColor: "{colors.card-2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "20px"
  question-bubble:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-ink}"
    rounded: "{rounded.card}"
    padding: "10px 16px"
  answer-card:
    backgroundColor: "{colors.card-2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "16px 20px"
  input:
    backgroundColor: "{colors.card-3}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "10px 20px"
    height: "48px"
    typography: "{typography.body}"
  citation-chip:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0 7px"
    typography: "{typography.measured}"
  citation-chip-cited:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.primary-ink}"
  status-pill:
    backgroundColor: "{colors.ok-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0 10px"
    height: "24px"
---

# Design System: coderag

## Overview

**Creative North Star: "The Console"**

A calm instrument panel for reading code: warm paper by day, graphite by night. Cards float on the canvas without borders, every control is a pill, and one blue runs through everything the system found: citation chips, lit nodes, the active path, the highlighted lines. Nothing decorates; everything on screen is a readout or a control.

**Key Characteristics:**
- Cards in two or three warm tones on an off-white canvas; no borders, at most a very soft shadow. Depth is the tone of the card, not a line around it.
- Solid black pills for the one primary action, warm-grey pills for the rest, filter pills that turn black when active.
- The dark colourway is a token swap: the same layout, the same hierarchy, black cards on a near-black ground with the primary pill inverted to white.
- One accent, Readout Blue, for citations and the active path. Small coloured dots name categories (cited, retrieved, calls, bridge, one per language) and mean the same thing everywhere.
- A chat on the left, the graph in the centre, the selected node on the right; selection is one state shared by all three.
- Sans for the interface, mono only for code, paths, line ranges, ids and counts.

## Colors

### Surfaces
- **Canvas** (`canvas`): the page ground, a warm off-white.
- **Card** (`card`): the lightest card, for the chat, the graph viewer, the repos table and dialogs.
- **Card 2** (`card-2`): a warmer tone one step down: stat tiles, the node panel, answer cards, the focus overlay's details.
- **Card 3** (`card-3`): the deepest tone: inputs, code blocks, the graph ground, chart tracks.
- Cards never carry a border. A card on a card is allowed only when the inner one is a deeper tone (code inside an answer, a tile inside the panel).

### Ink
- **Ink** (`ink`): near-black; headings, names, numbers, the primary pill's fill.
- **Ink 2** (`ink-2`): prose.
- **Muted** (`muted`): labels, meta, placeholders. Every pair of text and surface reaches WCAG AA.

### Accent
- **Readout Blue** (`accent`, `accent-soft`): what the system found. Citation chips (soft at rest, full when cited), the hop-0 node, the active path, the cited lines in code, the selected row. Never a button.

### Hop distance
- `hop-0` is the accent; `hop-1`, `hop-2`, `hop-3` step lighter as the code gets further from a direct match; `hop-none` is a neighbour that never reached the model. The graph's nodes, the vertical scale beside the graph and the legend all use these four steps, so distance reads the same everywhere.

### Categories
Small dots, always beside a word: cited (accent), retrieved (hop-2), calls and inherits (grey), bridge (`cat-bridge`, magenta: the one edge that crosses a language), and one dot per language (`lang-python` green, `lang-javascript` yellow, `lang-typescript` indigo, `lang-java` brown, `lang-kotlin` purple, `lang-c` grey, `lang-cpp` pink, `lang-go` cyan, `lang-rust` orange, `lang-html` red, `lang-config` grey). They are identity marks, not decoration: a dot appears only where its word does.

### Status
- **Ok** (`ok`): Indexed, Completed; **Warn** (`warn`): Indexing, Update available; **Bad** (`bad`): Failed, an error notice, an invalid citation. Each is a pill with its word; never colour alone.

### The dark colourway
Graphite: ground `oklch(11.5% 0.006 250)`, cards at 17%, 21% and 13.5%, ink at 95%. The primary pill inverts to white with dark text; the accent lifts to `oklch(75% 0.13 245)`; shadows disappear. Chosen in Settings (or following the system), applied before the first paint.

### Named Rules
**The One Blue Rule.** Blue means "the system found this". A blue button, a blue heading or a blue decoration is the interface lying.
**The Dot Rule.** A category dot is never alone and never bigger than 8px. Colour identifies; the word says what.

## Typography

**Display and body:** Host Grotesk (ui-sans-serif fallback), weights 400 and 500 only.
**Measured:** Atkinson Hyperlegible Mono, tabular and slashed-zero figures, for code, paths, line ranges, source ids, commit hashes and model names.

### Hierarchy
- **Hero** (500, clamp 2.4 to 4.25rem, 1.02, -0.03em): the home page's two-line headline.
- **Display** (500, 1.875rem): the repository name at the top of the app; the sign-in title.
- **Stat** (500, 2rem, tabular): the big number in a stat tile.
- **Section** (500, 1.5rem): dialog titles, the repos page title.
- **Title** (500, 1rem): card headings and the selected node's name.
- **Body** (400, 1rem, 1.6): answers, at most 66ch per line.
- **Label** (500, 0.875rem, sentence case): pills, column headings, tile labels, meta.
- **Measured** (400 or 500, 0.875rem): everything the mono carries; code at 0.8125rem.

### Named Rules
**The Who-Wrote-It Rule.** Mono is only for what the code contains or the system measured. A label, heading or button in mono is costume.

## Layout

Three pages on one canvas.

**Home (`/`)** has one job: get the visitor to try a demo. A two-line headline, one sentence from the product, then the proof: a real answer card with its citation and the mini call graph that reached it. Two actions: "Try with psf/requests" (black pill) and "Sign in with GitHub" (grey pill). Nothing below the fold competes.

**Sign in (`/login`)** is one card centred on the canvas: a small status badge, a big title, a one-line subtitle, "Continue with GitHub" as a black pill, and a short note about what GitHub will ask. The signed-in state replaces the form with a Continue pill.

**App (`/app`)** at 1280px and above:
- Top bar: the wordmark; a dark pill nav (Overview · Ask · Graph · Repos); a repo switcher with a search box (Demos / Your repos); Saved, Settings and the avatar.
- A row of four stat tiles: Files, Functions, Bridges, Questions asked. A big number, a small subtitle, and a mini bar chart (files per language, functions per package, bridges per mechanism, questions per day).
- Three columns. Left: the Ask panel as a chat (questions as black bubbles on the right, answers as cards with citations and "Also retrieved"; suggestion chips above a pill input with a black send button). Centre: the graph viewer with an icon toolbar on top, the last four answers' subgraphs as thumbnails down the left edge, a vertical hop-distance scale on the right edge, and an "Unconnected (n)" tray for isolated nodes. Right: the selected node's panel (name, file:line, a state pill, four tiles for Called by / Calls / Language / Lines, a code preview, "Ask about this" in black and "Open code" in grey).
- Repos is a page of its own: filter pills (All / Indexed / Indexing / Failed / Update available), a search box, "+ Add repo" in black, and a table (repo with a Private badge, language dots, version, indexed date, a progress bar while indexing, a status pill, a row menu with Open / Re-index / Remove). Demos are a second, smaller table.
- Clicking a node or a source opens the focus overlay: a centred card over the blurred, dimmed app, with the enlarged subgraph on the left and the details on the right (metadata, the file with the cited lines highlighted, and every edge's evidence, bridges included). Esc or a click outside closes it; focus stays inside while it is open.

Under 1280px the node panel moves below the graph. At 390px everything is one column and the pill nav switches sections; the focus overlay fills the screen.

Spacing follows 2, 4, 8, 12, 16, 24, 40, 64px. Cards pad 20px (16px on phones) and sit 16px apart; controls are 40px tall (32px for small pills) with a 44px hit area.

## Elevation & Depth

Cards have no borders. On paper they carry a very soft shadow (1px and a long, faint 30px blur); on graphite they carry none and read by tone alone. Popovers and dialogs sit on the strongest shadow over a dimmed backdrop; the focus overlay also blurs the app behind it (not under reduced motion). Table rows are divided by 1px hairlines, the only lines in the system.

## Shapes

Cards 18px (16px on phones), inner cards 12px, code 10px, every control, chip, pill and badge fully round. Graph glyphs say what a node is: circle for a function or method, square for a module, diamond for a class, triangle for a template, wye for a config file. A cited node is filled and ringed; a retrieved one filled in its hop colour; a neighbour small and grey. Bridges are the most distinct edge: thicker, magenta, dash-dot.

## Components

### Buttons
- **Primary:** black pill, 40px, 20px side padding, label weight. One per view: Send, Ask about this, + Add repo, Try with psf/requests, Continue with GitHub.
- **Secondary:** warm-grey pill, same size: Open code, Sign in with GitHub, dialog secondaries.
- **Filter pill:** 32px, card-2 fill, black when active.
- **Icon button:** 32px round, transparent until hover, 44px hit area; the graph toolbar, Saved, Settings.
- **Link button:** underlined text in ink-2: Copy answer, Save, Dismiss.
- **States:** hover deepens the fill one notch; focus is a 2px ring offset 2px, instant; pressed drops 1px; disabled is 50% with a not-allowed cursor; loading shows a skeleton, never a spinner.

### Pill nav
A dark pill (ink) holding four text buttons; the active one is a lighter pill inside it. On a phone it is the only navigation.

### Stat tile
Card-2, 18px radius. Label (sentence case), the big number in Stat type, a muted subtitle, and a mini bar chart: thin bars with 4px rounded tops on a card-3 track, one series, the current bar in the accent. Hover names each bar.

### Chat
A question is a black bubble aligned right, at most 86% wide, 18px radius with a 6px corner at the tail. An answer is a card-2 card aligned left holding the prose with inline citation chips, a meta line (model, time, Copy, Save), the cited source rows, and "Also retrieved (n)" folded. Suggestion chips are grey pills above the input; the input is a 48px pill with a round black send button inside it.

### Source row
Id chip, name, language dot, `file:start-end` in mono (shortened in the middle, the file name kept), and the relation word when the source was reached through the graph. The row is a button: it selects the node, opens its panel and the focus overlay. The row the graph points at is tinted accent-soft.

### Node panel
Card-2. Name in Title type, `file:line` in mono, a state pill (Cited / Retrieved / Neighbour / Selected), four small tiles (Called by, Calls, Language with its dot, Lines), the node's own code in a card-3 block, then the two pills. Empty state: "Select a node or a source to see it here."

### Focus overlay
A centred card (up to 76rem wide, full screen on a phone) over the dimmed, blurred app. Left: the subgraph around the node, enlarged, with its edges labelled. Right: metadata, the file with the cited lines highlighted, and the evidence for every edge ("HTTP POST /api/repos ← app.js:412"). Close with Esc, the close button or a click outside.

### Repos table
Hairline rows, 48px tall; the repo name with its owner muted; language dots; version in mono; the indexed date; a 6px progress bar while indexing; a status pill; a round "⋯" menu button opening Open / Re-index / Remove.

### Inputs
Card-3 fill, no border, pill shape, 48px for the question field and 40px elsewhere; focus is a 2px ring. Errors show the message in a soft-red line under the field.

### Code
Card-3 ground, 10px radius, real line numbers in a gutter copying leaves out; the answer's sources tinted (cited in accent-soft, retrieved fainter), each range tagged with its S-number; names in accent, strings quiet green, comments muted.

## Content

### Real data
- Long names truncate in the middle with the full name in a tooltip; the file name is never cut.
- Large repositories collapse to modules in the overview graph; the full graph is one click away.
- Every card has a designed empty state that says what will appear and how to make it appear.
- Null and undefined are never rendered: a missing value is left out or shown as an em-dash-free word ("none", "unknown").
- Plurals are correct: 1 file, 2 files, 1 class, 2 classes.
- Loading states are skeletons the shape of the content, never spinners.

### Graph
- Modules, cited sources and the hovered or selected node are labelled by default; function labels appear when zoomed in, placed to avoid collisions.
- Isolated nodes go to the "Unconnected (n)" tray instead of floating in the layout.
- The answer's paths flow once when the answer is written; nothing loops while you read.

## Do's and Don'ts

### Do:
- **Do** use the hop colours for nodes and the scale, and the category dots beside their words, in both colourways.
- **Do** keep one black pill per view and make everything else a grey pill or text.
- **Do** keep every control reachable and visibly focused from the keyboard, including graph nodes and table menus.
- **Do** disable the blur and zoom transitions under prefers-reduced-motion.

### Don't:
- **Don't** put a border on a card, or a card inside a card of the same tone.
- **Don't** use blue for a control, or any colour for decoration.
- **Don't** show a spinner where a skeleton fits.
- **Don't** render "(0)", "undefined" or a raw ISO date.
- **Don't** animate width, height or position; only transform, opacity and (for the overlay) filter.
