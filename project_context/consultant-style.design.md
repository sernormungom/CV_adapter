# Consultant Style Profile (`style.md`)

- **Version:** 1.0
- **Status:** Draft
- **Date:** 2026-05-27
- **Scope:** One `style.md` per consultant, stored in the consultant's folder alongside `profile.yaml`.
- **Audience of this document:** the schema maintainer (you), and the LLMs that read, apply, and update `style.md`.

---

## 0. What `style.md` is

`style.md` is the **per-consultant presentation lens**: the system's accumulated understanding of how *this specific consultant* wants their career presented. It is the third input to CV generation, alongside the facts (`profile.yaml`) and the target (the job description).

- `profile.yaml` answers **what is true** about the consultant.
- `style.md` answers **how this consultant wants their truth told**.
- The global generation prompt holds **how good CVs are written in general** (craft that serves everyone).

The three are deliberately separate. `style.md` holds only what is (a) specific to this consultant and (b) stable across all their CVs. It is not facts, not universal craft, and not position-specific reasoning.

### The boundary tests

A candidate preference belongs in `style.md` only if it passes all three:

1. **Not a fact.** "I worked in a team at Volvo" → `profile.yaml`. "I prefer collaborative framing" → `style.md`.
2. **Not universal craft.** "Lead with measurable impact" is good practice for everyone → global prompt. Put it in `style.md` only if it is *this consultant's* preference and might differ from the default.
3. **Not position-specific.** "For this automotive role, emphasize ISO 26262" is a generation-time decision driven by the job description → written nowhere permanent. `style.md` holds what is true across *all* of this consultant's CVs.

When a correction fails test 1, it updates `profile.yaml` instead. When it fails test 2 or 3, it is not recorded as style at all.

---

## 1. The two-tier model: rules and observations

`style.md` separates what the generator **must apply now** from what the system is **still watching**.

- **Rules** are active. The CV Content Generator applies every rule. A rule is something the system is confident reflects a standing preference.
- **Observations** are staging. They are noticed but **not** applied to generation. They wait for corroboration before being promoted to rules.

This implements the **hybrid generalization** policy:

| Signal | Enters as | Rationale |
|---|---|---|
| **Explicit** — the consultant stated the preference directly ("never call me a rockstar"; "I prefer British spelling") | **Rule** immediately | The consultant told us. No inference risk. |
| **Inferred** — the system deduced a preference from a correction whose stated purpose was something else (e.g. a solo-credit claim softened to team framing) | **Observation** | One correction may be a one-off about that bullet, not a standing trait. Wait for a pattern. |

### Promotion, demotion, removal

- **Observation → Rule** when *either*: the same observation is seen a second time (pattern), **or** the consultant confirms it via the summary view (explicit signal).
- **Rule strengthened** when reinforced by a later consistent signal (bump `strength`, update `last_seen`).
- **Rule → demoted or removed** when the consultant contradicts it through the summary view. A consultant contradiction is always an explicit, strong signal and takes precedence over any inferred history.
- **Observation dropped** if it goes stale (not reinforced within a consolidation cycle and never confirmed).

A consultant correction made *through the summary view* (§4) is always treated as explicit — it can create a rule directly, or demote/remove one directly.

---

## 2. Categories

Every entry is filed under exactly one of five categories. The categories give the classifier stable buckets and give the consultant-facing summary a sensible structure.

1. **Tone & credit** — modesty vs. boldness; comfort with superlatives; "led" vs. "contributed to"; individual vs. collective credit. *(Norberto's team-framing correction lives here.)*
2. **Phrasing & vocabulary** — favored and disfavored words; preferred verbs; jargon level; person (first/third); spelling convention (British/American); terms the consultant uses for themselves.
3. **Emphasis & foregrounding** — what to foreground or downplay across all CVs (e.g. "always foreground hands-on engineering even in lead roles"; "downplay the management years"). Note: only standing preferences, not per-position emphasis.
4. **Hard avoidances** — claims that must never be made; words that must never appear; topics never to surface. These are absolute and override generation defaults. *(A hard avoidance is, by nature, usually explicit — so it typically enters as a rule.)*
5. **Structure** — the consultant's standing preferences on CV length, bullet length, section ordering, density — *to the extent these are the consultant's preference rather than the position's requirement*.

---

## 3. File format

`style.md` is markdown so an LLM reads it as prose, but each entry carries light structured metadata (as an inline tag) so the learning loop can edit entries surgically.

### Entry format

Each entry is a bullet whose text is the preference, followed by a metadata tag:

```
- <preference statement> ^[id=<short-id>; origin=<explicit|inferred>; strength=<1-3>; seen=<count>; last_seen=<YYYY-MM-DD>; confirmed=<true|false>]
```

- **id** — short stable identifier, assigned by the updater (e.g. `tc1`, `ph2`). Lets corrections target a specific entry.
- **origin** — `explicit` (consultant stated it) or `inferred` (system deduced it).
- **strength** — `1` (weak/tentative), `2` (established), `3` (strong/explicit or repeatedly reinforced). Generators may weight stronger rules more heavily when two preferences tension against each other.
- **seen** — how many times this preference has been signaled.
- **last_seen** — date of the most recent signal.
- **confirmed** — whether the consultant has explicitly confirmed it through the summary view.

### Section layout

```markdown
# Style Profile — <consultant_id>

_Last updated: <date> · Last consolidated: <date>_

## How to use this file
<one-line reminder that RULES apply to generation; OBSERVATIONS do not>

## Rules
### Tone & credit
- ...entries...
### Phrasing & vocabulary
- ...entries...
### Emphasis & foregrounding
- ...entries...
### Hard avoidances
- ...entries...
### Structure
- ...entries...

## Observations (not yet applied)
### Tone & credit
- ...entries...
### (other categories as needed)
```

Empty categories are simply omitted. A brand-new consultant has an empty or near-empty file — that is normal; the file grows as the consultant interacts.

---

## 4. The consultant-facing summary view

The consultant **never edits raw `style.md`**. Instead, on request (or proactively after a few sessions), the chatbot generates a plain-language summary from the **Rules** section and presents it for confirmation/correction.

- The summary reflects **rules only**, not observations. Observations are the system's private hypotheses; surfacing them would be noisy and presumptuous.
- The summary is plain prose, grouped loosely by category, no metadata tags, no IDs. Example tone: "Here's how I've learned to present you: I keep credit collaborative rather than claiming solo ownership, I avoid buzzwords like 'rockstar' and 'synergy', and I lead your CVs with hands-on engineering work even where you held lead roles. Anything you'd change?"
- The consultant's reply is routed back through the classifier (§5) as an **explicit** signal:
  - Agreement → relevant rules get `confirmed=true`, `strength` bumped.
  - "Actually I'm fine with bold framing" → the contradicted rule is demoted or removed.
  - A new preference stated here → enters directly as a **rule** (explicit origin).

This gives the consultant control over the *content* of what's learned without exposing them to file mechanics.

---

## 5. The update loop (classifier behavior)

After the consultant gives feedback on a generated CV (or responds to the summary view), a classifier LLM processes each piece of feedback:

**Step 1 — Classify the feedback.** For each correction, decide what it is:
- A **fact correction** → route to `profile.yaml` (not `style.md`). E.g. "I worked in a team here."
- A **style signal** → continue to step 2. E.g. the *generalizable trait* behind "I worked in a team here" — a preference for collaborative framing.
- **Both** → do both. The factual part updates `profile.yaml`; the trait updates `style.md`.
- **Neither** (position-specific, or universal craft) → record nothing in `style.md`.

> A single correction often carries both a fact and a style signal. "Norberto developed the complete architecture by himself" → "Norberto participated in a team developing the architecture" updates the *fact* (it was teamwork) **and** suggests a *style trait* (prefers collaborative framing). Handle them on their separate tracks.

**Step 2 — Determine origin and strength.**
- Did the consultant state the preference directly ("never say X")? → `explicit`, enters as a **rule**, `strength=3`.
- Did the system infer it from a correction aimed at something else? → `inferred`, enters as an **observation**, `strength=1`.

**Step 3 — Check for an existing entry.**
- If a matching entry exists, reinforce it: bump `seen`, update `last_seen`, and apply the promotion rules (§1). An inferred observation seen a second time graduates to a rule.
- If none exists, create a new entry under the right category and tier.

**Step 4 — Resolve conflicts.** If the new signal contradicts an existing entry, the more recent and more explicit signal wins. A consultant contradiction (always explicit) overrides any inferred history. Record the resolution; don't leave both.

**Step 5 — Be conservative on inference.** When unsure whether a correction reveals a standing trait or is a one-off, prefer treating it as a one-off (fact-only, or a weak observation) rather than minting a rule. Overfit style rules degrade *every* future CV; under-fitting only misses an optimization.

---

## 6. Consolidation ("sleep")

Periodically — on a cadence (e.g. every N sessions) or when the file crosses a size threshold — a consolidation pass rewrites `style.md` as a whole:

- **Merge** duplicate or near-duplicate entries, summing `seen` and keeping the strongest origin.
- **Resolve** lingering contradictions in favor of the most recent explicit signal.
- **Drop** stale observations: inferred, unconfirmed, `seen=1`, and `last_seen` older than the consolidation window.
- **Demote** rules that have not been reinforced and were never explicit, if they begin to conflict with newer evidence. (Explicit, consultant-confirmed rules are never auto-dropped — only the consultant removes those.)
- Update `Last consolidated` date.

Consolidation is a whole-file rewrite, which is why the file is designed to be regenerable rather than only append-only. It mirrors the profile.yaml philosophy: the document is memory; the intelligence periodically reinterprets and tidies it.

---

## 7. How the generator uses `style.md`

For the CV Content Generator (this is also noted in the profile schema §5):

- Apply **every rule** in the Rules section. Treat **hard avoidances** as absolute — they override generation defaults and even position-driven instincts.
- **Ignore observations** — they are not yet active preferences.
- When two rules tension against each other, prefer the higher `strength`, then the more recent `last_seen`.
- `style.md` governs *how* to present facts; it never invents or alters facts. If a rule cannot be honored without misstating a fact in `profile.yaml`, the fact wins and the generator notes the tension rather than bending the fact.
- Position-specific emphasis still comes from the job description, not from here. `style.md` is the consultant's constant; the job description is the variable.

---

## 8. Worked example

A `style.md` for Norberto after a few sessions:

```markdown
# Style Profile — norberto_munoz

_Last updated: 2026-05-22 · Last consolidated: 2026-05-10_

## How to use this file
Rules below are applied to every CV. Observations are watched but NOT applied until promoted.

## Rules

### Tone & credit
- Frame contributions collaboratively; use "contributed to" / "worked with the team to" rather than solo-credit claims like "single-handedly" or "developed the complete X alone." ^[id=tc1; origin=inferred; strength=2; seen=2; last_seen=2026-05-18; confirmed=true]

### Phrasing & vocabulary
- Use British spelling (organisation, optimise). ^[id=ph1; origin=explicit; strength=3; seen=1; last_seen=2026-05-08; confirmed=true]

### Hard avoidances
- Never use the words "rockstar", "ninja", "guru", or "synergy". ^[id=ha1; origin=explicit; strength=3; seen=1; last_seen=2026-05-08; confirmed=true]

### Emphasis & foregrounding
- Foreground hands-on engineering work even in lead/management roles; the consultant identifies primarily as an engineer. ^[id=em1; origin=inferred; strength=2; seen=2; last_seen=2026-05-20; confirmed=false]

## Observations (not yet applied)

### Structure
- May prefer shorter bullets (≤2 lines). Seen once when a long bullet was trimmed. ^[id=st1; origin=inferred; strength=1; seen=1; last_seen=2026-05-20; confirmed=false]
```

Reading this: the generator writes collaboratively-framed, British-spelled CVs, never uses the banned words, and foregrounds engineering — but does **not** yet shorten bullets, because that is still only an observation awaiting a second occurrence or consultant confirmation.

---

## 9. Relationship to the rest of the system

- **`profile.yaml`** — facts. Corrections that change *what is true* go here, not to `style.md`.
- **`style.md`** — this document's instances. Per-consultant presentation lens.
- **Global generation prompt** — universal CV craft; operator-curated; never auto-updated from one consultant's corrections.
- **Application Tracker** — outcomes/history; unrelated to style.
- **Classifier LLM** — the intelligence that routes each correction to the right destination (§5). This is where the "learning" actually happens; `profile.yaml` and `style.md` are just the stores.

---

## 10. Deferred / open

- **Cross-consultant style patterns.** If many consultants independently develop the same preference, that may be a signal the *global prompt* default is wrong. Detecting this is out of scope for v1 but worth revisiting.
- **Strength as a number vs. label.** v1 uses 1–3. If finer weighting proves useful in generation, revisit.
- **Per-audience styles.** A consultant might want different framing for recruiters vs. hiring managers. Deferred; would become a sub-grouping within categories if needed.
- **Confidence decay.** v1 drops stale observations at consolidation but does not decay rule strength over time. If preferences drift as careers change, time-decay on unreinforced rules may be worth adding.
