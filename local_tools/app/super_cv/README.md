# CV Tag Matcher — prototype

A small standalone webpage for exploring a "drop a job ad, get quizzed
against your Super CV, curate the results as tags" idea. It's a prototype,
kept deliberately **outside**
the `cv-adapter` repo (see `internal/cv-adapter-notes/` for the same
pattern with docs) so it can be iterated on freely without touching the
production app's codebase, CI, or deploy.

## What it is

Two columns:

- **Left — Drop job ad here.** A plain drop/paste zone, not a tag board: a
  textarea plus an "Analyze job ad" button. Paste (Ctrl+V) a job posting's
  text, or drag in a text selection from another tab/window — dropping
  starts analysis automatically, or hit the button after pasting/typing.
  The text is saved for a real Claude Code session to turn into questions
  (see **Claude bridge** below); nothing is rendered as a tag on the left
  at all.
- **Right — Super CV** (scrollable). The **full** content of a Super CV
  (`data.js` → `SUPER_CV_TAGS`, seeded with placeholder template examples —
  swap in your own CV content to actually use the tool), one tag per item —
  every bullet and every "Tools & Skills" entry from every job role, every
  language, degree, patent, and publication. No cherry-picking or per-role
  cap — this mirrors the source CV exactly, minus the intro/"About" paragraph
  (there used to be a **Profile** section for it, removed since the CV's own
  summary text isn't something to tag-match against). There also used to be
  board-wide
  **Programming**/**Tools**/**Other Skills** sections listing every skill
  flat across the whole CV, but that duplicated exactly what each
  experience's own "Tools & Skills" subsection already lists (see below) —
  removed rather than kept in sync by hand in two places at once. Tags are
  grouped under a shared header per role ("<Company> · <Role>",
  "Education", …) shown once above each contiguous run, instead of
  repeating the role on every card. Dragging can split a run into more than
  one — each fragment still gets its own header, so it always reflects
  what's directly below it (see `app.js`'s `renderGroupedList`).

  Most groups stack one tag per line (full width, since their text is a
  whole sentence). **Interests** is the exception: its tags are short, so
  they wrap several-to-a-line, each sized to fit just its own text (`app.js`
  → `WRAP_GROUP_ROLES` — add another role name there to give it the same
  compact layout). A job position's own "Tools & Skills" subsection (see
  below) always gets this same wrapped layout too, hardcoded there rather
  than listed in `WRAP_GROUP_ROLES`, since it's never a group's own
  top-level role. Dragging still works inside a wrapped row:
  `computeDropTarget` is row-aware, so within a multi-tag row it picks an
  insertion slot by horizontal position instead of only vertical.

  Every job position has its own group (one header per role, e.g.
  "<Company> · <Role>") whose header line shows **place** and **time
  period** as small pill tags on the right (seeded from `data.js` →
  `ROLE_META` into the persisted `state.roleMeta`, `app.js` →
  `groupHeaderEl`'s `.group-header-meta`). `period` is `yyyy-mm – yyyy-mm`,
  since `superCV.md` gives a month for every job position. These are plain
  `<span>`s, not `.tag` elements — no drag, invisible to
  `computeDropTarget` — same reasoning as the "+" tile below. Unlike a
  `.tag`, though, a place/period pill is **double-click to edit, never
  removable**: no × button, and (unlike a tag's own empty-commit-deletes
  rule) committing it empty or hitting Escape both just keep whatever value
  it already had (`app.js` → `metaTagEl`) — there's no such thing as a job
  position with no place or period, only a corrected one.

  Every job position also gets a **"Tools & Skills" subsection** at the
  bottom of its own group — the tools/skills mentioned for that specific
  job in `superCV.md` (its own `**Tools & Skills:**` list under each
  experience), rendered as a small uppercase "Tools & Skills" label
  (`app.js` → `appendGroup`'s `subsection-header`) followed by a wrapped row
  of pill tags. This is now the **only** place a tool/skill lives — see the
  removed board-wide sections note above. These tags are ordinary CV tags —
  draggable, double-click-to-edit, hover-to-delete, with their own **+**
  tile — just marked `section: "tools"` (`data.js`) instead of getting a
  separate `role`, so they stay part of the SAME contiguous run as that
  job's bullets (one header, one group to rename/delete/drag together) and
  only split into their own little pill-row at render time. Dragging a tag
  between the bullet list and the Tools & Skills row (or in from any other
  section) sets/clears `section` based on which of the two `.group-tags`
  containers it lands in (`app.js`'s drop handler reads `dataset.section`
  the same way it already reads `dataset.role`). These tags don't carry a
  level star — that's Interests-only (see below) — since a per-job tool/
  skill mention doesn't need a separate strength rating.

  A whole experience section (header, place/period, every bullet, and its
  Tools & Skills subsection together) is deletable in one action — the same
  hover-revealed × on the group header every group already has
  (`groupHeaderEl`), confirming first since it removes every tag in the
  run at once, not just one line. Adding a new one is a dedicated
  **"+ Add experience"** button (`app.js` → `addExperienceButtonEl`/
  `addExperience`) that sits right below the *last* experience's Tools &
  Skills — not a per-group "+" tile like the ones above, since this adds a
  whole section rather than one tag: a fresh `<Company> · <Role>`
  job-position header (with its own placeholder `state.roleMeta` place/
  period pills, exactly like a real job would have), one Accomplishment
  bullet, and one Tools & Skills tag, all template placeholders to rename/
  edit in place the same way you'd edit any other tag or header. Clicking
  it again reuses the same template role name with a numbered suffix
  (`<Company> · <Role> #2`, `#3`, ...) so consecutive additions don't
  silently merge into one group — `role` grouping is purely by exact string
  match (`renderGroupedList`), so two identically-named groups would
  otherwise read as a single run instead of two separate sections. With no
  experience section left at all (every one deleted, or a fully empty
  board), the button still appears — at the top of the list, or beside the
  empty-board hint — so there's always a way to add the first one.

  **Education** is a single shared group instead — the whole history in
  one "Education" header, not one per degree — since unlike a job position
  its place/period differ per entry rather than applying to everything
  under the role. Each degree renders as its own **row** of three tags:
  the description (draggable/editable/removable, same as any other CV tag)
  plus its own place and period pills sitting beside it (`data.js` → each
  Education tag's own `place`/`period` fields, `app.js` → `eduRowEl`). The
  pills use the exact same double-click-to-edit-but-never-removable
  behavior as a job position's header pills, just scoped to that one entry
  instead of the whole group. `period` stays plain `yyyy – yyyy` since
  `superCV.md`'s education section only ever gives years, not months —
  padding in an invented month would fabricate data the source doesn't
  have.

  Every group's tag list ends with a **+** tile, after its last real tag
  (`app.js` → `addTagButtonEl`/`addTagToRole`) — click it and a new, empty
  tag opens directly in inline-edit mode right where it landed (no popup):
  type the line and press Enter/click away to commit, or leave it blank and
  commit/Escape to cancel — either way an empty commit just removes the tag
  rather than leaving a blank line behind (`startEditingCvTag`). The
  **same** empty-commit-deletes rule applies to double-click-editing any
  *existing* tag too — clear its text and commit and the tag is gone, not
  left blank; Escape on an existing tag still just reverts to its old text,
  same as before (there's something real to revert to there, unlike a
  brand-new tag). The tile itself is styled to match its neighbors (a small
  pill in a wrapped group, a full-width dashed row otherwise) but
  deliberately not a real `.tag`, so it stays invisible to
  dragging/reordering/`computeDropTarget`'s row scan — it's always there,
  never something you can drag or drop onto. Every group and every tag is
  fully draggable/renamable/removable — there's no pinned section anymore
  (an earlier version pinned **Profile** as a fixed, undraggable first
  section; removing Profile removed that mechanism along with it).

  **Interests** sits at the very bottom, the last section on the board, and
  is the one group that isn't seeded from `superCV.md` at all — there's no
  real "interests" data to source from there, and personal interests aren't
  something to invent on a real, named person's behalf, so it starts empty.
  Its header always shows (`app.js` → `PHANTOM_GROUP_AFTER`, anchored right
  after **Publications** — the last real section — an "empty but
  always-anchored" group, the only case where a group header renders with
  zero real tags under it, just its own **+** tile) so there's always
  something to click to add the first one. Each interest tag carries a
  **level** — a star you click to toggle between strong (★, filled) and
  neutral (☆, outline) — shown inline before its text, independent of the
  tag's own text/delete/drag handling. Dragging any other CV tag into an
  empty Interests zone reassigns its role there too (the same generic
  cross-group merge every other group already supports), defaulting to a
  neutral level until you click the star.

Every Super CV tag — and every group header — is **click**able,
**draggable**, **editable**, and **removable**:

- Click a tag to select it (multi-select). Hover a tag or group header to
  reveal its **×** delete button.
- **Double-click** a tag or a group header to rename it inline (Enter or
  blur commits, Escape cancels); a tag's text edits in a textarea, a group
  title in a single-line input. Committing a tag's edit with the text
  cleared to empty deletes that tag outright, same as the × button — see
  the "+" tile note above for the full empty-commit-vs-Escape rule.
- **× delete** removes it. Deleting a tag just removes that one card.
  Deleting a group header removes the whole run of tags under it at
  once — that's the more destructive of the two, so it asks for
  confirmation first ("Delete this group and its N tags?").
- Drag a **tag** to reorder it within its group or move it into another
  group.
- Drag a **group header** to move its whole run of tags together and
  reorder the groups themselves.

Dragging shows a **live preview** of the result before you let go: the item
being dragged is lifted out (its drag-ghost image follows the cursor as
normal; the real card underneath just hides), an empty frame with a
pulsing/glowing dashed border appears at wherever it would land, and every
other tag/header in between FLIP-animates out of the way to make room —
move the cursor somewhere else and they slide right back. Nothing is
written to `state` until you actually drop; moving off a valid target (or
cancelling the drag entirely) reverts everything with no trace. See the
"live drag preview" section in `app.js` (`updateReorderPreview` /
`revertReorderPreview`) — implemented as a from-scratch FLIP (First-Last-
Invert-Play) animation plus a real placeholder element, since native HTML5
drag-and-drop doesn't reflow anything on its own.

State (positions, edits) persists to `localStorage` per browser profile;
use **Reset** in the header to restore the original sample tags.

**↶ Undo / ↷ Redo** sit to the left of Reset (also `Ctrl/Cmd+Z` and
`Ctrl/Cmd+Shift+Z`/`Ctrl/Cmd+Y`, skipped while an inline-edit input/textarea
is focused so they don't fight the browser's own native text-undo mid-edit)
and step back/forward through every content edit made since the page last
loaded (or since the last **Reset**, which restarts the history at the
fresh seed — "undo goes back to the Reset default version"). Implemented in
`app.js` as a plain snapshot stack of the whole `state` object
(`editHistory`/`editHistoryIndex`, named to avoid shadowing the browser's
own `window.history`), not a per-mutation command log: every kind of edit
(add/edit/remove a tag, drag-and-drop reorder, rename a group, edit a
place/period pill, toggle an Interests level star) already funnels through
`saveState()` right after mutating `state` in place, so recording history
there (`pushHistory`) covers all of them uniformly with no per-call-site
bookkeeping. Redoing after a fresh edit isn't possible — making a new edit
after an Undo drops whatever redo branch existed, the same as any other
undo/redo implementation. Both buttons disable themselves at either end of
the stack (`updateUndoRedoButtons`). Two things are deliberately **not**
undoable, since they aren't content edits: clicking a tag to select/
deselect it (`saveSelectionOnly` persists that to `localStorage` without
recording history) and cancelling a brand-new "+"-tile tag before typing
anything into it (nets back to exactly the pre-add state, so there's
nothing real to undo — see `addTagToRole`/`startEditingCvTag`, both of
which use the lower-level `persistToStorage` instead of `saveState` for
exactly that reason: the tag doesn't become a real, undoable edit until its
text is actually committed). History is **in-memory only**, not persisted
to `localStorage` — a page reload starts a fresh history anchored at
whatever just loaded (same as a Reset does), rather than growing an
ever-larger snapshot list in storage for what's meant to be transient
per-session edit history.

Below the two columns is a **chat box**, topped with four **reminder
chips** — ready-made shortcut prompts for the CV's next step, each grounded
in a specific part of the `super-cv-assistant` skill (see **Second bridge
flow: CV next-step reminders** below) — click one to kick off that request
through the same Claude bridge the job-ad review uses. One of them, **Check
CV vs hot job-market skills**, actually reuses this *first* bridge flow
directly rather than the second: it needs the same per-item multiple-choice
tab widget "Analyze job ad" produces, just comparing against a
market-researched skill keyword instead of a pasted job ad's bullet. Paste
or drop a job ad's text into the left column and the chat assistant takes
over the same way, mirroring the `super-cv-assistant` skill's single-job
mode (Step 2b: go through the *entire* requirements list, not a sampled
subset):

0. A real Claude Code session reads the job ad and drafts the requirement
   questions, relayed through `bridge_server.py` — see **Claude bridge**
   below for exactly how that hand-off works; nothing here is a client-side
   heuristic anymore.
1. Every question in a round is presented as a **tab widget** — one tab per
   question, laid out the same way this project's own AskUserQuestion tool
   presents a multi-question batch (in the VSCode plugin / Claude Code
   UI): click any tab to jump straight to that question, including ones
   already answered, to change the answer. Each tab shows two distinct
   things, both written by Claude Code: the job ad's own **requirement**
   bullet, quoted or closely paraphrased, in its own highlighted block, so
   it's always clear exactly which line is being checked — then, below it,
   a short **explanation** of what that requirement concretely means,
   grounded in both the job ad's wording and this specific CV (what
   matches, what's missing, any adjacent experience worth naming), followed
   by the actual ask (e.g. requirement *"10+ years of experience in
   strategic roles within automotive OEMs, technology companies, or
   adjacent industries"*, explanation *"This means formally holding a
   strategic (not just senior individual-contributor) role for that long —
   your CV shows deep hands-on engineering/tech-lead experience but no
   strategic-track roles. Do you have experience like this, even under a
   different title?"*) — never just the bare requirement restated as "Do
   you have this experience?" with nothing else, and never an explanation
   with no requirement shown at all. All 6 of the skill's options are
   offered below both, in order: extensive / some experience / learning /
   not applicable / **not sure — explain & check my CV** / **other — write
   your own answer**.
2. **Extensive/some experience** → reveals an inline, optional detail field
   right in that tab ("what project or example shows this?"); pressing
   Enter (even blank) drafts a new CV card from it, appended to the bottom
   of the Super CV board, and **auto-advances to the next tab**. Handled
   entirely client-side — no Claude round-trip needed for a clear-cut
   answer. Changing this answer later (by revisiting the tab) removes the
   old card before adding the new one, so nothing duplicates.
3. **Learning / not applicable** → acknowledged instantly, nothing added,
   auto-advances immediately (no extra input needed).
4. **Other** → reveals its own inline text field in that tab; Enter with it
   empty just shows a validation hint instead of advancing.
5. **Not sure** / **Other** → *not* resolved locally (there's no canned
   explanation or heuristic CV search left in `chat.js` for this — see
   **Claude bridge** below). The raw choice (plus whatever free text was
   typed for "Other") is what gets reported back through the bridge, and
   it's Claude Code that explains the requirement, checks the CV, and
   either resolves it directly or drafts real follow-up sub-questions —
   asked through this exact same tabbed flow, recursively, until every
   requirement lands on a final extensive/some/learning/not-applicable.
   Once every tab in the round has an answer (in whatever order you reached
   them), the panel submits as one batch and locks.

The chat also answers plain free-text messages in between (try "help").

All three blocks are **resizable**: drag the thin bar between the two
boards to resize their columns, or the bar between the boards and the chat
box to resize its height. Everything inside (tag lists, chat messages)
reflows with the new size rather than clipping, since those are already
flex/overflow-based. Sizes persist to `localStorage` (`resize.js`) and are
clamped to sane minimums so no block can be dragged to zero.

No backend, no build step, no external dependencies at all — styling is
plain Arial (`style.css` → `--sans`), no webfont link needed. All Super CV
data is inlined in `data.js` so the page works opened directly via `file://`
— nothing fetches anything (job ad text is typed/pasted/dropped in by hand,
not fetched from anywhere either).

Visual style is lifted from `internal/customer-searcher/main.html` (the
Mpya Sci & Tech brand palette — `--mpya-purple`/`--mpya-blue`/
`--mpya-yellow`/`--mpya-navy` — plus its pill-shaped short tags, dashed
`.tag-list` container border, and fixed corner `.page-deco` glow) so this
prototype reads as the same family of internal tool. Every existing class
name in `style.css` is unchanged from before the restyle — only the colors/
shapes/typography under them moved — so `app.js`/`chat.js`/`resize.js`
needed no changes.

**The tab-widget presentation is scripted, but the questions themselves are
real Claude output relayed through a human, not a live API call.** This is
still a static, dependency-free page with no backend, so there's nowhere
safe to hold an API key for the page to call Claude directly — but there's
no templated/substring-based logic left standing in for a model either
(that was removed along with the old "not sure" auto-resolve). See **Claude
bridge** below for how a real Claude Code session actually authors each
round of questions, and the file-level comments at the top of
`chat.js`/`bridge.js` for the split between the two.

## Running it

For just browsing/editing the Super CV board (no job-ad review), open
`index.html` directly in a browser (double-click it, or use the button
described below) — everything except the left column's job-ad flow works
the same as any `file://` page.

To use the **Claude bridge** (job-ad review), you need `bridge_server.py`
running instead, since that flow needs somewhere to write/read the session
files this page and a Claude Code session hand off through:

```bash
cd internal/cv-adapter-extension
python3 bridge_server.py       # serves the page AND the bridge API together
# then open http://localhost:8091/
```

(`python3 -m http.server 8090` still works too, if you just want a plain
`http://` origin with no bridge API behind it.)

## Claude bridge

The chat's job-ad Q&A is real Claude reasoning, not a canned script — but
since this page can't call an API on its own, a human relays each round
between the browser and a Claude Code session (this one, in a terminal)
instead. `bridge_server.py` is the piece that makes that possible: the
browser can POST/GET over HTTP but can't write files, and Claude Code can
read/write files but has no way to receive a request from the browser — the
server just holds a session file each side can reach.

**The loop, end to end:**

1. Drop or paste a job ad on the left, hit **Analyze job ad** (or just drop
   it in — that auto-submits). The page POSTs it to `bridge_server.py`,
   which writes `.bridge/<session-id>.json` and starts the exchange. The
   chat shows *"Saved. Tell Claude Code in your chat session to forward
   this job ad…"*, the job-ad zone disables, and a **⏹ Stop** button appears.
2. **You tell Claude Code, in this chat, to forward it** — any phrasing
   works ("forward it", "check the bridge"); Claude Code isn't watching the
   file on its own, so nothing happens until you say so. Claude Code then:
   - reads `jobAdText` (and `cvSnapshot`, the Super CV as it looked when the
     session started) from the session file,
   - goes through the job ad's requirements the way `super-cv-assistant`'s
     Step 2b does — full coverage, not a curated subset, batched a
     reasonable number per round rather than all at once,
   - writes those questions into the session file as a new round — each
     with its own `requirement` (the job ad's own bullet, quoted/
     paraphrased) and `text` (a short explanation grounded in both the job
     ad and this CV, plus the actual ask — see the schema comment at the
     top of `bridge_server.py`) — and sets `status` to `"awaiting_answers"`.
3. The page polls `GET /api/sessions/<id>` every ~2.5s (`bridge.js`) and
   picks up the new round automatically — no action needed on your side.
   Its questions render as a **tab widget** in the chat (`chat.js`), one tab
   per question, navigable in any order: **extensive/some** drafts a new CV
   bullet from an optional inline detail and inserts it immediately;
   **learning/not applicable** is just acknowledged. Neither needs Claude
   Code — they're clear-cut, so the client handles them fully on its own,
   and auto-advances to the next tab either way.
4. **Not sure / Other are never resolved locally.** `chat.js` reports the
   raw choice (and, for "Other", whatever was typed into that tab's inline
   text field) straight back through the bridge — no canned explanation, no
   keyword-substring CV search. Once every tab in the round has *an* answer
   (in whatever order you reached them — you can revisit and change any
   tab before that point), the panel locks, the page POSTs the answers back
   (`{"id", "level", "otherText"?}` per question) and shows *"Saved your
   answers. Tell Claude Code in your chat session to forward them…"*
5. **You tell Claude Code to forward the answers.** For each "not
   sure"/"other" answer, it does what the skill's own E-branch describes —
   explains the requirement in plain terms, checks the CV for anything
   related — then either:
   - resolves it directly (clear enough already, e.g. a legible "Other"
     answer) by appending `{"id", "level", "cvText"?}` to the session's
     top-level, append-only `resolutions` list — the page applies this the
     *moment* it sees it (inserts `cvText` as a new CV card for
     extensive/some, or just acknowledges for learning/not applicable),
     independently of whatever round is or isn't also pending, or
   - drafts 2–4 concrete follow-up sub-questions instead, written into a
     new round tagged with `"parentId"` pointing back at the original
     question's id, plus a `"note"` (the explanation + what it found in the
     CV) that the page shows as a plain message right before asking them.
     Sub-questions go through the exact same 6-option flow — including
     being able to come back "not sure" again, which just repeats this
     step — until Claude Code has enough to write a `resolutions` entry.

   It also decides whether more of the job ad's requirements are left to
   cover at all (→ a new round of fresh top-level questions, same as step
   2) or the review is fully resolved (→ writes
   `"conclusion": {"summary": "..."}` and sets `status` to `"done"`) — both
   can happen in the same response as resolving pending "not sure"/"other"
   items.
6. Repeat 3–5 until Claude Code sends the conclusion. The page shows
   `conclusion.summary` in the chat, stops polling, and re-enables the
   job-ad zone for a new review.

**Stopping early:** the **⏹ Stop** button (next to the chat status) cancels
the session at any point — `status` flips to `"cancelled"`, polling stops,
and the job-ad zone re-enables. Nothing more is expected from Claude Code
for that session once it's cancelled.

**Session file** (`.bridge/<id>.json`, one per review): see the schema
documented at the top of `bridge_server.py`. Claude Code reads/edits this
file directly with its own file tools — it never calls the server's HTTP
API, since it doesn't need to (only the browser does, for the parts of the
filesystem it can't reach itself). The server re-reads the file fresh on
every request, so an edit is picked up by the very next browser poll with
no restart needed.

**Only one review at a time**: the job-ad zone disables itself while a
session is active (matches how the old left board only ever ran one batch
through the chat at once) — finish or Stop the current one to start
another.

### Second bridge flow: CV next-step reminders

Four pill-shaped **reminder chips** sit above the chat log (`index.html` →
`#chatReminders`, populated by `reminders.js`), each a ready-made shortcut
grounded in one specific step of
`copilots/job_agent/skills/super-cv-assistant/SKILL.md`:

- **Check CV vs hot job-market skills** (Steps 1–4) — search the job market
  for skills frequently required right now that aren't in any role's Tools
  & Skills tags, then ask about each one the same way "Analyze job ad"
  asks about a requirement. Sourced the same way
  `internal/customer-searcher/CONTACT_SEARCH_AGENT.md`'s "Required sources
  checklist" gathers job-market signal: Platsbanken via Arbetsförmedlingen's
  JobTech API (not the arbetsformedlingen.se frontend), a general job-ad
  `WebSearch` sweep across the major job boards, and LinkedIn via
  `WebSearch` only, never scraped.
- **Re-sort skill/tool lists by demand** (Step 6, skill categories) —
  reorder each role's Tools & Skills list by real-world job-market demand.
- **Re-sort experience bullets by impact** (Step 6, narrative bullets) —
  reorder by significance, biggest impact first.
- **Review "not applicable" skills** (Negative list) — check
  `superCV_negative.md` for entries worth reconsidering now.

(A fifth, **Cross-check About vs Experience skills**, was retired along
with the board-wide Programming/Tools/Other Skills sections it depended on
— see the "Right — Super CV" note above.)

**Check CV vs hot job-market skills** is the odd one out: it needs the
exact same per-requirement multiple-choice tab widget the job-ad review
already has, so instead of the mechanism below it hands its own crafted
instructions to `bridge.js`'s `submitJobAd` directly (the same function
"Analyze job ad" calls) — Claude Code reads that text as instructions, not
literal ad copy, does the market research with its own tools, then drafts
one requirement-style question per missing skill into that session's next
round exactly like a real job-ad review. Everything in **Claude bridge**
above (the tab widget, the 6 options, CV-drafting on
extensive/some, `⏹ Stop`) applies unchanged.

The other three reminders use `reminders.js`, not `skillroles.js` (retired
along with the board-wide Programming/Tools/Other Skills sections it
served) — a separate session-file namespace and poll loop, since this is a
one-shot prompt-in/response-out task, not a back-and-forth Q&A:

1. Click a reminder chip. `postUserText` echoes the chip's label into the
   chat as if you'd typed it, then the page POSTs that reminder's
   ready-made `prompt` plus the current Super CV board (`role`+`text` only)
   to `bridge_server.py`, which writes `.bridge/reminder-<id>.json` (see the
   schema comment above `write_reminder` in `bridge_server.py`) and returns
   its id. All four chips disable while this one is in flight — one request
   at a time, same reasoning as the job-ad review.
2. **Tell Claude Code, in this chat, to forward it** — same manual relay as
   the job-ad flow. Claude Code reads `prompt` + `cvSnapshot` from the file
   and does the actual work with its own tools: re-sorting `superCV.md`,
   checking the negative list — whatever that reminder's step actually
   calls for. None of that happens inside the browser page; it plays out
   right here in this terminal chat, the same as any other request you'd
   type directly. Once done, Claude Code writes a short `response` summary
   (the answer, the proposal, or what it changed) and sets `status` to
   `"done"`.
3. The page polls `GET /api/reminders/<id>` every ~2.5s and, once `status`
   is `"done"`, posts `response` as one chat message and re-enables the
   chips.

**Only one bridge request of either kind is ever in flight at once** —
`session` (bridge.js's job-ad tracker) and `activeReminder` (reminders.js's
own) each check the other before starting, so a market-skill-scan review, a
pasted job ad, and a plain reminder can never overlap; `submitJobAd`/
`endSession` disable/re-enable the reminder chips alongside the job-ad
zone for the same reason.

If a reminder's work changes `superCV.md` on disk, remember that doesn't
update the board here automatically — the two are kept in sync by hand (see
`data.js` → `SUPER_CV_TAGS`), same as any other direct edit to the CV file.

## How it's wired into cv-adapter

`cv-adapter/html/cv-importer.html` (the app's home page, served at `/`) has
a **🏷 Tag Matcher** button in its top toolbar that calls `openTagMatcher()`,
which does `window.open(TAG_MATCHER_URL, '_blank')` with `TAG_MATCHER_URL`
hardcoded to this folder's `index.html` as a `file://` URL. That's it — no
backend route, no static mount, nothing else in `cv-adapter` changed. Note
this opens the plain `file://` page, so the Claude bridge isn't available
through this button — start `bridge_server.py` and open its
`http://localhost:8091/` URL manually for that.

This only works for local/dev use on this machine, since:
- it's an absolute local file path, not something that exists in a cloud
  deployment (CloudFront-served `cv-adapter` has no filesystem access to
  this folder), and
- it's tied to this specific OneDrive path.

If this prototype graduates into something that should work for other
users / in production, the real next step is to move it inside
`cv-adapter/html/`, add a route/static mount in `backend/main.py` (mirroring
the existing `app.mount("/html", ...)` / `serve_dashboard()` pattern), and
replace the `file://` URL with a relative link.

Note on `localStorage`: the key is versioned (`app.js` → `STORAGE_KEY`,
currently `cvExtTagBoard_v3`). Bump the suffix whenever a change means old
saved state could be structurally stale or just clutter you want everyone
to drop — old data under the previous key is simply ignored, so the seed
data loads fresh. (v2 retired stray "Added via Tag Matcher" cards that
earlier chat-flow testing had left sitting in `localStorage`. v3 dropped
`state.left` entirely when the left column stopped being a keyword-tag
board at all — its old `{left: [...], right: [...]}` shape would otherwise
fail the v3 loader's `Array.isArray(parsed.right)` check anyway, but the
version bump makes the intent explicit rather than relying on that as a
side effect.)

## Known prototype limitations

- Sample data only — no live connection to a consultant's real profile.
- Requirement extraction, question-drafting, explanations, CV-relatedness
  checks, and "not sure"/"other" resolution are all real Claude reasoning
  now (see **Claude bridge** above) — `chat.js` no longer has any canned
  template or keyword-substring logic left for those; only the clear-cut
  extensive/some/learning/not-applicable paths are handled locally, since
  there's nothing for Claude to decide there.
- The bridge is a manual relay, not a live connection — nothing happens
  until you explicitly tell Claude Code, in this chat, to forward a job ad
  or a round of answers. It won't notice new work on its own.
- The bridge needs `bridge_server.py` running and the page open at its
  `http://localhost:8091/` URL — opened as a plain `file://` page (e.g. via
  cv-adapter's Tag Matcher button), the job-ad zone will save fine locally
  but every fetch to `/api/sessions` will fail, since there's no server
  behind that origin.
- Right-column click-to-select doesn't drive anything yet (no batch action
  on selected CV lines) — it's a leftover from before the left column
  changed from a selectable keyword-tag board to a job-ad drop zone.
- Native HTML5 drag-and-drop has no touch support, so this is desktop/mouse
  only.
- Single hardcoded person's Super CV — not parameterized by URL/query yet.
- New CV cards added by the chat aren't written back to `superCV.md` —
  they only live in this page's `localStorage`.
- The live drag preview genuinely reflows the list as soon as you pick an
  item up (the same way Trello/SortableJS-style UIs do), which self-corrects
  every dragover tick against wherever the cursor actually is — fine for a
  real, continuously-moving mouse, but worth knowing if you're writing
  automated tests:
  - A single-shot `locator.dragTo()` across a **long distance** computes its
    target's center once and can land one element off after a big reflow.
    Prefer a short-distance `dragTo()`, or a manually-dispatched many-step
    `dragover` trace, when precision matters for a test.
  - `page.mouse.down()` + several `page.mouse.move()` steps + `page.mouse.up()`
    *does* trigger genuine native `dragstart`/`dragover` in Chromium (useful
    for inspecting the live preview mid-drag, which is how the bugs below
    were actually found and fixed) — but its final `mouse.up()` does **not**
    reliably fire a native `drop` event in headless Chromium, landing on
    `dragend` with no drop instead. That's a Playwright/headless limitation,
    not an app bug: `locator.dragTo()` fires `drop` correctly every time in
    the same scenarios. Use raw `page.mouse` only to observe the preview;
    use `dragTo()` to verify what actually gets committed.
  - Three real bugs were found and fixed this way: (1) an early version
    excluded the hidden, currently-dragged tag(s) from the candidate list
    *before* assigning positions, silently renumbering everything after it
    out of sync with what `moveItems` expected — `computeDropTarget` now
    skips excluded tags while building rows without ever renumbering the
    rest. (2) `getBoundingClientRect()` reflects an element's current
    *visual* (transformed) position, not its resting layout position — if a
    previous FLIP transition was still mid-flight when `dragover` fired
    again (routine, since dragover fires far more often than a 180ms
    transition takes), every subsequent measurement read a moving target.
    `updateReorderPreview`/`revertReorderPreview` now snap every tracked
    element's transform back to none *before* measuring anything. (3) `drop`
    used to call `computeDropTarget` a **second, independent** time instead
    of reusing what the last `dragover` had already resolved — for a real,
    fast-moving mouse this second measurement had a real chance of landing
    mid-transform too (same root cause as bug 2, just hitting the drop
    handler instead of the preview), so what actually got committed could
    silently disagree with what the placeholder had just shown, which reads
    as "the tags don't change place when I let go" even though the preview
    looked right. `drop` now reuses `previewTargetIndex` — the exact index
    the visible placeholder is standing at — whenever a live preview for
    that same board is active, so the commit can never diverge from the
    preview; it only re-measures from scratch as a fallback when there's no
    active preview to reuse (e.g. a drop with no prior dragover). (4) Even
    after (1)-(3), drop still "usually didn't do anything" for a real
    mouse. Root cause: `updateReorderPreview` was unconditionally removing
    and recreating the placeholder element and resetting every tracked
    tag/header's transform on *every single* `dragover` tick — and a real
    mouse fires dragover continuously on the exact container the native
    drag session is tracking as the drop target. Continuously mutating that
    container is a known way to destabilize native HTML5 drag-and-drop in
    real browsers (a paced test script never fired ticks densely enough to
    hit this). `updateReorderPreview` now compares the newly resolved
    `{index, container}` against what's already showing and returns
    immediately if unchanged — the common case for most ticks — and when it
    does need to move, it relocates the *same* placeholder element instead
    of destroying and recreating it. A real drag now touches the tracked
    DOM only when the target slot actually changes, not on every tick.
    `dragover`/`drop` are also both wrapped in `try/catch` now so that any
    future bug in the preview math degrades to "no visual preview" rather
    than silently killing the commit. (5) Dragging a tag into the middle of
    a *different* group's run moved it there positionally but left its
    `role` field untouched, so `renderGroupedList` (which groups purely by
    contiguous `role`) split the target group into two runs around the
    newly-inserted tag instead of the tag joining it — the tag also visibly
    kept looking like it belonged to its old group's label. `drop` now reads
    the landing spot's `.group-tags` wrapper (`dataset.role`, set in
    `renderGroupedList`) and reassigns the moved tag(s) to that role before
    calling `moveItems`, so it actually merges into the target group. A
    dragged **group header** is exempt from this (`currentDrag.isGroup`) —
    moving a whole group relocates it as its own block rather than merging
    it into whatever it lands next to. (6) Dragging a tag to the end of
    *any* group could preview (and, for some group shapes, actually commit)
    the tag joining the *next* group instead — first noticed as "dropping
    Python at the end of Skills previews it prepending to the next group,"
    then found to be broader: dragging almost any tag to just past the last
    tag of the group before an excluded row could land it silently
    reassigned to the wrong group's role. Root cause: every row in
    `computeDropTarget` decided "is the cursor still over this row" using
    only *its own* geometry (a hard bottom-edge cutoff for a wrapped row, or
    its own vertical midpoint for a single-tag row) — reasonable in
    isolation, but rows never accounted for what their *neighbor* looked
    like. That's fine normally, since adjacent rows' own edges/midpoints
    happen to partition the space between them fairly. It breaks down right
    next to the tag(s) actually being dragged: `computeDropTarget` excludes
    the dragged tag(s) from the candidate rows entirely, so the row(s)
    immediately before and after it suddenly face a much bigger gap — the
    dragged row's own full height, plus (for the row before a wrapped
    group) the group's own trailing margin and the next header. A
    by-my-own-edge check has no way to split that enlarged gap fairly, so a
    cursor anywhere in it — including squarely where the dragged tag used
    to sit — got awarded entirely to whichever row's check happened to run
    next, silently reassigning the drop to that neighboring group. Fixed by
    replacing every row's self-only check with a shared *zone*: each row now
    claims the vertical span up to the midpoint between its own bottom edge
    and the next surviving row's top edge (or to the end of the list, for
    the last row), computed the same way for every row shape. Once a row's
    zone claims the cursor, the existing before/after-this-tag logic
    (horizontal, for a wrapped row; the tag's own midpoint, for a
    single-tag row) still decides the exact slot within it — only *which*
    row gets first claim changed, not what happens once claimed.
    (This generalization also matters for testing: a `dragTo()` call with a
    pre-computed absolute target coordinate can be invalidated by the drag
    it starts — the dragged tag turns `display:none` on `dragstart`, which
    immediately reflows everything after it, so a coordinate aimed at "2px
    above tag X's bottom edge" can end up over a *different* tag once X has
    shifted into the space the dragged tag vacated. This only bites when
    the dragged tag sits *before* the target; dragging something *after*
    the target, or re-reading the target's `bounding_box()` mid-drag, avoids
    it. Not an app bug — a real mouse tracks the tag's current visual
    position continuously, so it never has a stale coordinate to begin
    with.) (7) Adding the **Interests** group (always-present, can start
    empty) exposed a gap `computeDropTarget` never had a reason to close
    before: it only ever reasons about `.tag` elements — the function scans
    `listEl.querySelectorAll(".tag")` and groups them into rows, full stop.
    A `.group-tags` container with zero tags inside it is invisible to that
    scan entirely, so there was no way to ever drop a tag INTO one — no
    matter how precisely the cursor was aimed at the empty zone on screen,
    the drop always resolved to whichever real row happened to sit
    above/below it instead (confirmed via a live-mouse trace: landing
    squarely inside the empty zone's own bounding box still resolved to the
    neighboring group). Fixed by scanning for empty `.group-tags` elements
    separately and inserting a synthetic single-item "row" for each one,
    positioned by its own rect and sorted in among the real rows by Y
    position, so it competes in the exact same gap-aware zone logic as
    everything else — matched, it resolves directly to that container
    instead of calling `insertBefore`/`insertAfter` on a tag that doesn't
    exist. This only works because the empty zone actually has visible area
    to aim at in the first place — a bare `.group-tags` div with no
    children collapses to 0 height by default; every group's own **+** add
    tile (added right after this fix, see above) now gives even a phantom
    empty group real content and height on its own, so there's no longer a
    genuinely childless `.group-tags` to worry about at all — its
    `localIndex` just resolves to `container.children.length` (append after
    the tile) instead of a hardcoded `0` (which would insert the drop
    placeholder *before* the tile).
