---
name: coderag
description: Ask questions about a Python codebase; every answer cites the code it came from.
colors:
  operator-amber: "oklch(54% 0.14 70)"
  operator-amber-ink: "oklch(98.5% 0.006 85)"
  operator-amber-soft: "oklch(92% 0.05 80)"
  operator-amber-hover: "oklch(49% 0.13 70)"
  readout-blue: "oklch(52% 0.14 250)"
  readout-blue-soft: "oklch(92.5% 0.03 245)"
  paper: "oklch(98% 0.006 85)"
  paper-raised: "oklch(95.6% 0.008 85)"
  paper-deep: "oklch(93.2% 0.009 85)"
  ink: "oklch(19% 0.01 60)"
  ink-soft: "oklch(31% 0.01 60)"
  ink-muted: "oklch(45% 0.012 60)"
  hairline: "oklch(87.5% 0.009 85)"
  hairline-strong: "oklch(76% 0.011 85)"
  diff-ok: "oklch(50% 0.13 150)"
  diff-bad: "oklch(52% 0.17 25)"
  diff-bad-soft: "oklch(93.5% 0.03 25)"
typography:
  display:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.728rem"
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.44rem"
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: "-0.03em"
  title:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 500
    lineHeight: 1.3
  body:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "Host Grotesk, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 500
    lineHeight: 1.3
  measured:
    fontFamily: "Atkinson Hyperlegible Mono, ui-monospace, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: "\"tnum\" 1, \"zero\" 1"
rounded:
  chip: "4px"
  control: "6px"
  panel: "10px"
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
    backgroundColor: "{colors.operator-amber}"
    textColor: "{colors.operator-amber-ink}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "40px"
    typography: "{typography.label}"
  button-primary-hover:
    backgroundColor: "{colors.operator-amber-hover}"
  button-ghost:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
    typography: "{typography.label}"
  button-ghost-hover:
    backgroundColor: "{colors.paper-deep}"
  input:
    backgroundColor: "{colors.paper-deep}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
    height: "40px"
    typography: "{typography.body}"
  question-entry:
    backgroundColor: "{colors.operator-amber-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "8px 16px"
  answer-panel:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.panel}"
  citation-chip:
    backgroundColor: "{colors.readout-blue-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.chip}"
    padding: "0 4px"
    typography: "{typography.measured}"
  citation-chip-cited:
    backgroundColor: "{colors.readout-blue}"
    textColor: "{colors.paper-deep}"
---

# Design System: coderag

## Overview

**Creative North Star: "The Lab Notebook"**

Every answer is a notebook entry that carries its evidence (the sources it cites, where they live, and how they were reached), and the interface recedes so that record can be read and checked.

**Key Characteristics:**
- Three surface steps, depth by lightness, never by shadow.
- Amber means the person; blue means the system. Never swapped.
- Mono only for system-measured values: code, paths, line ranges, ids, counts, model names.
- Show the code, not a picture of it: the right pane opens on the cited lines.
- One filled primary action per view; everything else outlined or typographic.
- Paper is the default colourway; graphite is the same tokens re-derived for dark, chosen in Settings or following the system.

## Colors

A warm paper ground (graphite when the visitor asks for dark) carrying two signals of equal weight and opposite meaning.

### Primary
- **Operator Amber** (`operator-amber`): the person. The Ask button, the user's question entry (as its soft tint), the current repository and pane tabs' underline, and focus rings. Never a highlight the system makes. Its hover and ink pair travel with it.

### Secondary
- **Readout Blue** (`readout-blue`): the system. Citation chips in answers, source ids in the sources table, the answer's code in the graph and the code viewer, the row or node the system points at, names in highlighted code, indexing progress, commit hashes. The soft tint marks a reference at rest; the full blue marks one the answer actually cited.

### Tertiary
- **Diff Green and Diff Red** (`diff-ok`, `diff-bad`, `diff-bad-soft`): status only. A finished indexing job, an error notice, an invalid citation. Never decoration, never the only signal (always paired with text).

### Neutral
- **Paper** (`paper`): the page ground and the chat column.
- **Paper Raised** (`paper-raised`): panels one step up: answers, dialogs, the saved list, the node card.
- **Paper Deep** (`paper-deep`): the deepest step: the graph pane, the code viewer, code blocks, inputs, an expanded source.
- **Ink** (`ink`), **Ink Soft** (`ink-soft`), **Ink Muted** (`ink-muted`): headings and names; answer prose; labels, meta and helper text.
- **Hairline** (`hairline`) and **Hairline Strong** (`hairline-strong`): the 1px rules that cut panels from one surface; the strong rule edges controls and selected rows.

The dark colourway (graphite) re-derives every token with the same hues: ground `oklch(17% 0.008 250)`, ink `oklch(95% 0.005 250)`, amber `oklch(82% 0.15 80)`, blue `oklch(77% 0.12 245)`. Visitors choose it in Settings, or let it follow a dark system; the choice is applied before the first paint. Its full set lives in `frontend/tokens.css` and the sidecar.

### Named Rules
**The Two Voices Rule.** Amber is the operator and blue is the readout, in every colourway. An amber citation or a blue Ask button is the system lying about who said what. Amber marks only the person's own state: their questions, the Ask button, the open repository tab, the keyboard focus ring. When the system points at something (a source row it opens, a node it pulses), that highlight is Readout Blue.

**The Signal Budget Rule.** Amber covers under 5% of any viewport. Its rarity is how the eye finds the next action.

## Typography

**Display Font:** Host Grotesk (with ui-sans-serif, system-ui)
**Body Font:** Host Grotesk
**Label/Mono Font:** Atkinson Hyperlegible Mono (with ui-monospace), for measured values only

**Character:** A calm, slightly technical grotesk at weights 400 and 500 for everything a person reads, beside a mono whose slashed zero and tailed one exist to make identifiers unambiguous.

### Hierarchy
- **Display** (500, 1.728rem, 1.05, -0.03em): the repository name heading the chat column.
- **Headline** (500, 1.44rem, 1.05): dialog titles (Settings, Saved answers, Add a repository).
- **Title** (500, 1rem, 1.3): headings inside answers; the sources table heading at label size.
- **Body** (400, 1rem, 1.6): answer prose, at most 68ch per line; suggestions and the question field.
- **Label** (500, 0.875rem, sentence case, untracked): field labels, legends, tab names, meta lines, button text.
- **Measured** (400 or 500, 0.875rem, tabular and slashed-zero figures): symbol names, file paths and line ranges, source ids, citation chips, counts, the model that answered, commit hashes.

### Named Rules
**The Who-Wrote-It Rule.** Mono is reserved for what the system measured or the code contains. A label, heading or button in mono is costume.

**The Two Weights Rule.** 400 and 500 only. Hierarchy past that comes from size, colour and the surface a line sits on.

## Layout

Three pages. The home page (`/`) makes the case with the product's own signature, a real cited answer, then how an answer is made and the repositories ready to ask; it is a scrolling page on the same tokens and components as the app, with one amber action. Sign-in (`/login`) is one centred panel: GitHub first (the coming way in), then the demo account. The app (`/app`) is behind sign-in.

The app is a full-height application shell: a 56px top bar (wordmark, repository tabs that scroll sideways, Add repo, Saved, Settings), an optional banner row, then two panes. The chat pane (about 53%) holds the repository header, the suggested questions as a ruled list, the conversation, and the question field pinned to its foot; its content column is capped at 46rem for reading. Once the first question is asked the header compacts to one ruled line (name, counts, commit, Clear chat) and the suggestions leave, so the answer owns the top of the pane; they return when the chat is cleared. The right pane (about 47%) is two readouts behind a Code | Call graph tab pair: the code viewer, which an answer opens at its first cited source, and the graph. Before any question the chat pane shows the repository's shape (packages, most called, entry points, deepest hierarchy) and the graph shows the whole call graph when the repository is small enough to read at once (otherwise it waits for a question, with the full graph one click away).

Under 60rem the panes become one, switched by a Chat / Code / Graph tab strip, and nothing switches the pane without being asked; the wordmark collapses to its mark, and the repository tab row fades at whichever edge has more tabs scrolled out of view. Under 40rem the question entry widens to 92% and source rows restack so the location stays visible.

Spacing follows a fixed scale (2, 4, 8, 12, 16, 24, 40, 64px). Panels pad 16 to 24px; conversation turns sit 40px apart; controls sit 8px apart. Density is the design: controls are 32 to 40px tall with a 44px hit target.

## Elevation & Depth

Flat by construction. There are no shadows anywhere, in either colourway. Depth is three steps of lightness: the page ground, a raised panel one step lighter (dark) or darker (light), and a deepest step for code, inputs and the graph. A panel is a 1px hairline with the raised step inside; panels that belong together share one surface divided by hairlines rather than floating as separate cards. Dialogs are the raised step with a strong hairline over a dimmed backdrop, still without a shadow.

### Named Rules
**The No-Lamp Rule.** Nothing glows, blooms or casts a shadow. If something needs to stand forward, it moves one lightness step.

## Shapes

Three radii, one per nesting level: 10px on panels and dialogs, 6px on controls (buttons, inputs, fieldsets, code blocks), 4px on chips and citation keys. Rules are always 1px. Graph glyphs are the system's one recurring geometry, and they say what the answer did with the code: a filled, ringed circle for code the answer cites, a hollow ring for code that was retrieved but not cited, a small grey dot for a neighbour that never reached the model. The legend keys are the same glyphs, and the sources table repeats the meaning with its filled or soft S-badge. Hubs read larger (a node's size follows how many calls touch it); calls are gently curved lines, a call each way bowing apart; files are soft regions named at their top edge; the call-graph paths that reached the answer's sources are Readout Blue lines.

## Components

### Buttons
Quiet and exact: one filled action, the rest step back.
- **Shape:** gently squared (6px).
- **Primary:** Operator Amber fill with its dark ink, 40px tall, 16px side padding, label weight. One per view (Ask; in a dialog, its confirm).
- **Hover / Focus:** hover steps the amber one notch; focus is a 2px amber outline offset 2px, shown instantly; pressed drops 1px; disabled is 55% opacity with a not-allowed cursor and the native attribute.
- **Ghost:** raised-surface fill, strong hairline edge, 32px tall (Add repo, Show full graph, dialog secondaries). Hover deepens the surface.
- **Link button:** underlined text in ink-soft that turns amber on hover (Copy answer, Save, Clear chat, Open repo).
- **Icon button:** 32px square, transparent until hover, 44px hit area (Saved, Settings).

### Chips
- **Style:** citation and source-id chips are mono at 0.75rem on Readout Blue soft, 4px radius.
- **State:** a source the answer cited turns full Readout Blue with deep-paper text; an out-of-range citation turns the soft red.

### Cards / Containers
- **Corner Style:** 10px.
- **Background:** Paper Raised for answers, dialogs, saved items and the node card; Paper Deep inside them for code.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px hairline; strong hairline on dialogs and the node card.
- **Internal Padding:** 16 to 24px; table rows 8px by 24px.

### Inputs / Fields
- **Style:** Paper Deep fill, 1px muted edge (it needs 3:1, so never the hairline), 6px radius, 40px tall.
- **Focus:** a 2px amber outline inset 3px, with the edge deepening to ink-soft at the same width. The border never changes width.
- **Error / Disabled:** error edge in red with the message in a bordered soft-red line below; disabled at 55% with a not-allowed cursor.

### Navigation
- **Style:** repository tabs are text at label size in muted ink; the current tab turns ink with a 2px amber underline. Hover lifts the text to ink on the raised surface. The row scrolls sideways and keeps the current tab in view. Visitor-added repositories carry a small close control.
- **Pane tabs:** Code | Call graph above the right pane use the same underline language; they are a real tablist (arrow keys move between them).
- **Mobile:** the Chat / Code / Graph strip uses the same underline language at full width, 44px tall.

### The Code Viewer
The right pane's first readout: the whole file at the indexed commit, syntax-coloured in the system's two voices (names in Readout Blue, strings a quiet green, comments muted, amber nowhere), with real line numbers drawn in a gutter that copying leaves out. The answer's sources are tinted: cited lines Readout Blue soft, retrieved lines half that, each range tagged with its S-number at its first line, and the range in focus marked by a 2px blue rule in the gutter. A strip above lists the files the answer used. A newly opened file jumps to its range; moving within a file scrolls smoothly.

### The Call Graph
A live force layout on the deepest surface, and the system's one place for motion.
- **Continuity:** the graph remembers where every node was. A new view (an answer arriving, the full graph, another layout) is laid out from those positions and glides there (560ms, exponential ease-out) while the old view steps back; new nodes grow out of a neighbour that was already on screen.
- **Structure:** files are soft regions (a pull keeps each file's code together); once there is an answer they become hairline outlines so the evidence leads. Hubs read larger; calls curve.
- **Evidence:** the paths that pulled sources into the answer are drawn in Readout Blue, and the first time a written answer's graph is on screen, dots travel each path the way retrieval walked it, once (about 2.4s), then rest.
- **Pointing:** pointing at a node (or focusing it, or pointing at its row in the sources table) keeps it, its calls and its path; everything else steps back to 18%, and the names of what stays are shown at a readable size.
- **Layouts:** Free (the force finds the shape) or By distance: rings whose distance from the centre is the call distance from the question (direct match, 1 call away, 2+ calls away, neighbours), each ring named.
- **Camera:** drag nodes, wheel or pinch to zoom, drag the background to pan; − + and Fit buttons; double-click a node to zoom to its neighbourhood, the background to fit.
- **Keyboard:** one tab stop; the arrow keys move between nodes in S-number order; Enter opens a node's card, Esc closes it; + and − zoom, 0 fits.
- **Reduced motion:** views change in place and nothing flows; pointing, dimming and every state still show.

### The Evidence Entry (signature)
A conversation turn is one notebook entry: the person's question as an amber-tinted note aligned right, then one raised panel holding the answer prose with inline blue citation chips, a meta line (who answered, in mono, and how long it took) with Copy and Save, and the sources table cut from the same surface. Each source row shows its id chip, the symbol and its `file:start-end` in mono (shortened in the middle so the file name survives), and, only when it isn't a direct match, the call-graph path that reached it ("via Session.send"). Once the answer is written the table splits: the cited rows under "Cited (n)", the rest folded closed under "Also retrieved (n)". Every retrieved graph node is labelled with the same S-number, so chip, row and node are one key. Following a citation opens that row and, on a wide screen, the file at the cited lines; choosing a graph node opens its row in turn. Source code always shows its real line numbers.

## Content

The rules above are about looks; these are about what is shown, which matters more.

### Graph
- The answer graph shows the retrieved code and its direct neighbours as a live force layout: interactive first, not a static diagram.
- Motion explains: a view becomes the next one by moving what's already there, and the answer's paths flow once to show how the evidence was reached. Nothing loops while you read.
- Pointing at a node lights its calls and inheritance, not every member of its class.
- File regions, ring guides and their names are obstacles: node labels are placed around them.
- Labels keep at least 12px on screen at any zoom: the answer's labels (or, before a question, every label) hold their size while the graph shrinks, and the fit never zooms out past the point where that stops working. Past it, the view centres on what the answer cites, whole.
- A label that would cover another moves to the free side of its node (below, above, right, left), cited code first.
- The fit keeps the button row and the caption clear of the graph.
- Small repositories show their whole graph before any question; large ones show it on request. Above 600 nodes views change without movement, and above 150 files aren't outlined: the graph stays responsive.
- Calls on an object of unknown type are only drawn when they can't be a builtin (`path.append` is a list, not the repo's one class with `append`).

### Sources
- Show only exceptions: a direct match carries no annotation; a source reached through the call graph names its path ("via A → B").
- Paths shorten in the middle; the file name is never cut. Whole-file chunks are named "<module> (module)", not "__init__.py".
- Once an answer is written, the cited sources come first and the rest fold under "Also retrieved".

### Empty State
- Always shows the repository's shape: packages, the most called code, entry points and the deepest class hierarchy, each one click from its code.
- Suggested questions come from those facts, so they differ for every repository; curated ones win where they exist.

### Numbers and Words
- Counts use people's words (files, functions, classes), never internal ones (chunks, resolved calls).
- Nothing is shown as "(0)": an empty legend entry or count is hidden.
- Questions about the whole project get a map of every area in the model's context, and the answer says so.

## Do's and Don'ts

### Do:
- **Do** keep amber for the person and blue for the system in every new component and in both colourways.
- **Do** set code, paths, line ranges, ids, counts and model names in the measured mono with tabular figures; set everything else in Host Grotesk.
- **Do** separate panels that belong together with 1px hairlines on one surface, and step lightness for depth.
- **Do** give every control all its states: hover, instant 2px focus ring, pressed, disabled (opacity, cursor and attribute together), loading, error.
- **Do** key anything that refers to retrieved code like the graph: filled and ringed when the answer cites it, hollow when it was only retrieved, and label it with its S-number.
- **Do** keep the graph's labels at 12px or more on screen: the fit stops zooming out where they would shrink, and labels that can't hold that size give way to the answer's.

### Don't:
- **Don't** add shadows, glows or gradients to any surface.
- **Don't** use mono for labels, headings or buttons.
- **Don't** put a second filled button beside the primary.
- **Don't** join meta with middle dots or decorate labels with coloured dots; use columns, rules or separate elements.
- **Don't** set labels in tracked capitals; labels are sentence case at 0.875rem, untracked.
- **Don't** animate width, height or position; only transform and opacity, with a reduced-motion fallback. The one exception is the evidence flow on the answer's paths (a dash offset, on a handful of lines, briefly).
