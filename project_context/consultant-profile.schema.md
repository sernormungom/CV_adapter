# Consultant Profile Schema

- **Schema version:** 1.3
- **Status:** Draft
- **Date:** 2026-06-09
- **Format:** YAML (the schema is format-agnostic; examples use YAML)
- **Filename convention:** `profile.yaml` inside the consultant's folder
- **Changes since 1.2:** added `client` field to block (§2.5.1) for `consultant_via_employer` engagements — separates the end-client from the employer organisation so block `started`/`ended` unambiguously mean the client-assignment period, not the employer's tenure; added producer rule for consultant date handling (§5.2).
- **Changes since 1.1:** removed `provenance` block from role_groups and blocks (audit metadata is not LLM signal); removed `keywords` and `tools_referenced` from evidence_items (redundant with block-level fields and evidence `text`); replaced `reviewed_by_consultant` flag with `verified: true` at block level (absent = unconfirmed); removed `provenance_source` vocabulary; renumbered §2.6 IDs, §2.7 gaps.
- **Changes since 1.0:** added the compression principle (§0.1) and producer compression rules (§5.1); added the structured **gaps** mechanism and `needs_review` markers (§2.7, §3, §4); changed missing-required behavior from hard error to gap (flag-don't-guess); added the raw-CV archive backstop (§0.2).

---

## 0. Purpose and design principles

A **consultant profile** is the authoritative factual record of a consultant's career, education, and stated preferences. It is populated and maintained by LLMs from uploaded CVs and from in-session corrections, and read by the CV Preparation Pipeline to generate position-specific CVs.

### 0.1 Facts, not judgment — and compressed like memory

The profile is **working memory**, modeled loosely on how human memory keeps the gist and drops the incidental. Two principles follow:

**Facts, not judgment.** The profile stores *what happened* — dates, roles, evidence of work done, tools used, things built. It does **not** store *how to present* what happened. Editorial judgment (what to emphasize, how to phrase, what to lead with) is decided by the LLM at generation time, using the job description, the generation prompt, and the consultant's learned presentation preferences (`style.md`, a separate document). The schema therefore excludes presentation fields — no narrative weight, no core/supporting tags, no pre-written summaries-for-pitching, no pre-computed skill aggregates.

**Compressed like memory, graded by relevance.** The profile keeps the *atoms that have evidential or inferential value* and drops the rest. The test for any piece of content is not "is this small?" but "would any future CV, match, or question ever need this?"
- **Skeleton** (who/where/when/what role) is always kept — it is cheap and it is the index for everything else.
- **Evidence atoms** (specific, credible claims — achievements, impacts, artifacts, decisions, concrete responsibilities) are kept **specific and uncompressed**. They are the cargo. Economy does **not** come from blurring these into vague summaries.
- **Reconstructable detail** (polished CV phrasing, prose paragraphs, filler such as boilerplate responsibilities, zero-value specifics like office addresses) is **not stored**. The generator rewrites phrasing per position anyway.
- **Compression is graded by recency and importance.** Recent or core roles keep all their atoms; old or minor roles collapse toward skeleton-only. A first job from twenty years ago may be a single line; the current role is rich. This mirrors human memory and keeps the profile high signal-to-noise.

### 0.2 The raw-CV archive backstop

Compression is lossy and irreversible, and the importer is an LLM making judgment calls. To make compression safe, **the original uploaded CV is archived in the consultant's folder** (e.g. `uploads/2026-05-25_cv.pdf`), separate from `profile.yaml`. The profile is the compressed working memory; the archived CV is the "look it up if needed" backstop, enabling a re-import if the importer wrongly dropped something. When unsure whether a specific, concrete claim is an atom or filler, the importer **keeps it** — under-compression is cheap, lost evidence is not.

### 0.3 Who reads what

| Reader | Reads | For |
|---|---|---|
| **CV Importer** (Experience Extractor + Writer) | §1–5 | Building/growing the profile from an uploaded CV |
| **Experience Capture / chatbot** | §1–4 | Applying consultant corrections and resolving gaps |
| **CV Content Generator** | §1, §2, §6 | Reading facts to generate a position-specific CV |
| **Schema maintainer** | All | Evolving the schema |

---

## 1. Structure

```
profile
├── metadata        (schema version, consultant id, timestamps)
├── identity        (name, contact, location, spoken languages, links)
├── preferences     (what the consultant wants next — including aspirational interests)
├── education       (degrees, certifications, training — facts only)
├── career_history
│   └── role_groups[]      (one per employer OR per personal/self-directed body of work)
│       └── blocks[]       (a coherent body of work within a role group)
│           └── evidence_items[]   (atomic claims about what was done)
└── gaps            (structured list of missing/unconfirmed information for the chatbot)
```

**Cardinality:**
- One profile per consultant; the consultant's folder name is the consultant ID.
- A complete profile has ≥1 role group; each role group ≥1 block; each block ≥1 evidence item.
- Every evidence item has exactly one parent block; every block exactly one parent role group.

**Attachment principle** (used by the chatbot/Experience Capture when placing new information):
> Information attaches at the lowest level where it cleanly fits. A new responsibility or achievement within existing work → an **evidence item**. A distinct body of work within an existing employment → a **block**. Work at a new organization, or a new personal project → a **role group**.

---

## 2. Field reference

**Types:** `string`; `string<enum>` (controlled vocabulary, §3); `date_ym` (`"YYYY-MM"`; `"present"` allowed only on `ended`); `integer`; `number`; `boolean`; `array<T>`; `object{...}`.

**Disposition** tells the producer LLM how to behave when source data is silent:
- **required** — must be present. If it cannot be sourced or reasonably inferred, **do not guess and do not block the object**: omit the field, mark the containing object `needs_review: true`, and record a **gap** (§2.8). See §4 for the flag-don't-guess rule.
- **optional** — include if the source supports it; omit (don't emit `null`) otherwise.
- **inferred** — the LLM may infer from context; if it cannot infer with reasonable confidence, omit (optionally record a `missing_useful` gap if the field is valuable).
- **consultant-owned** — never populated from CV import; filled only via the chatbot/Experience Capture flow.

### 2.1 `metadata`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `schema_version` | `string` | required | E.g. `"1.1"`. |
| `consultant_id` | `string` | required | Folder name. Lowercase snake_case. Stable. |
| `created_at` | ISO 8601 | required | Set by Writer on creation. |
| `updated_at` | ISO 8601 | required | Set by Writer on every write. |
| `as_of` | `date_ym` | required | Month through which the profile is current (recency/duration math). |
| `source_archive` | `string` | optional | Path(s) to the archived raw CV(s) this profile was built from. |

### 2.2 `identity`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `full_name` | `string` | required | As it should appear on CVs. |
| `preferred_name` | `string` | optional | |
| `email` | `string` | required | If absent from source → gap, not a guess. |
| `phone` | `string` | optional | |
| `location` | `object{city, region, country, country_code}` | required | `country_code` = ISO 3166-1 alpha-2. |
| `spoken_languages` | `array<object{language, proficiency, is_native}>` | required | `proficiency` ∈ `language_proficiency` (§3). Natural languages only. |
| `work_authorization` | `array<object{country_code, status}>` | optional | From import only if explicitly stated. |
| `public_links` | `array<object{kind, url}>` | optional | `kind` ∈ `link_kind` (§3). |
| `personal_interests` | `array<string>` | optional | Non-professional. Low priority. |

### 2.3 `preferences` (all consultant-owned)
Not populated from CV import. Gathered through the chatbot/Experience Capture flow. Holds **aspirational interests** ("wants to move into AI") as distinct from *demonstrated* work (which goes in `career_history`).

| Field | Type | Disposition | Notes |
|---|---|---|---|
| `target_roles` | `array<string>` | consultant-owned | |
| `target_industries` | `array<string<enum>>` | consultant-owned | `industry` vocab; includes aspirational targets, e.g. `ai_ml`. |
| `avoid_industries` | `array<string<enum>>` | consultant-owned | Negative filter. |
| `seniority_target` | `string<enum>` | consultant-owned | `seniority` vocab. |
| `employment_types` | `array<string<enum>>` | consultant-owned | `employment_type` vocab. |
| `work_mode` | `array<string<enum>>` | consultant-owned | `work_mode` vocab. |
| `location_preference` | `object{primary_cities[], willing_to_relocate, willing_to_travel_pct}` | consultant-owned | |
| `availability` | `object{available_from, notice_period_weeks, notes}` | consultant-owned | |
| `notes` | `string` | consultant-owned | Free text. |

### 2.4 `education` (array)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `education_id` | `string` | required | Writer-assigned (§2.7). |
| `type` | `string<enum>` | required | `education_type` vocab. |
| `institution` | `string` | required | |
| `qualification` | `string` | required | |
| `field_of_study` | `string` | optional | |
| `location` | `object{city, country_code}` | optional | |
| `started` | `date_ym` | optional | |
| `ended` | `date_ym` | optional | `"present"` allowed. |
| `expires_at` | `date_ym` | optional | For certifications with expiry. |
| `description` | `string` | optional | Brief; don't restate the qualification. Generic workshops/filler may be dropped per §0.1. |
| `needs_review` | `boolean` | optional | Set with an associated gap when something required is missing. |

### 2.5 `career_history.role_groups`
A role group is a contiguous engagement with one organization — **or** a personal/self-directed body of work (`employment_type: personal_project` / `open_source`), structured identically.

| Field | Type | Disposition | Notes |
|---|---|---|---|
| `role_group_id` | `string` | required | Writer-assigned. |
| `organization` | `object{name, parent_name, country_code, industries[]}` | required | `parent_name` optional (e.g. consultancy-via-client). `industries` ∈ `industry` vocab. |
| `display_role_title` | `string` | required | Umbrella title; may be a slash-joined list of distinct roles held. |
| `started` | `date_ym` | required | If absent → omit + `needs_review` + gap. |
| `ended` | `date_ym` | required | `"present"` allowed. If absent → omit + `needs_review` + gap. |
| `employment_type` | `string<enum>` | required | `employment_type` vocab. |
| `summary` | `string` | optional | One-paragraph *factual* summary (what it was, not how to pitch it). Used for old/foundational role groups compressed toward skeleton. |
| `blocks` | `array<block>` | required | ≥1. |
| `needs_review` | `boolean` | optional | True when this role group has open gaps. |

#### 2.5.1 `block`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `block_id` | `string` | required | Writer-assigned. |
| `role_title` | `string` | required | May differ from the role group's `display_role_title`. For `consultant_via_employer` blocks, omit the client name from this string — put it in `client.name` instead. |
| `client` | `object{name, department, country_code}` | optional | **Required for `consultant_via_employer` blocks.** The end-client organisation, distinct from the employer in the role_group. `name` is required within the object; `department` and `country_code` are optional. Block `started`/`ended` are the **client-assignment dates**, not the employer's tenure. |
| `started` | `date_ym` | required | Within parent range. If absent → omit + `needs_review` + gap. For `consultant_via_employer` blocks: the client-assignment start date — if unknown, create an `ambiguous_dates` gap; do **not** default to the employer's start date. |
| `ended` | `date_ym` | required | Within parent range. `"present"` allowed. For `consultant_via_employer` blocks: the client-assignment end date. |
| `block_type` | `string<enum>` | required | `block_type` vocab. Describes what the work *was*. |
| `seniority` | `string<enum>` | inferred | `seniority` vocab. |
| `domains` | `array<string<enum>>` | inferred | `domain` vocab. |
| `industries` | `array<string<enum>>` | inferred | `industry` vocab; usually inherited from the organization. |
| `languages` | `array<string>` | optional | Programming languages used. |
| `tools` | `array<string>` | optional | Tools/platforms. |
| `processes_standards` | `array<string>` | optional | Prefer `process_standard` values (§3) where applicable. |
| `verification_validation` | `array<string>` | optional | V&V techniques/tools. |
| `collaboration` | `object{team_size, reports_to, direct_reports, stakeholders[]}` | optional | Factual context for senior/lead roles. |
| `evidence_items` | `array<evidence_item>` | required | ≥1. |
| `needs_review` | `boolean` | optional | True when this block has open gaps. |
| `verified` | `boolean` | optional | Present and `true` only after the consultant has explicitly confirmed this block's content. Absent means imported or inferred but not yet reviewed. |

#### 2.5.2 `evidence_item`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `evidence_id` | `string` | required | Writer-assigned. |
| `type` | `string<enum>` | required | `evidence_type` vocab. A fact about the claim's nature. |
| `text` | `string` | required | The claim, past tense, ≤60 words. State facts plainly; store the *claim*, not polished CV prose, and never editorialize ("single-handedly", "world-class"). |
| `quantification` | `object{metric, value, unit, baseline}` | optional | For `type: impact`. |

### 2.6 IDs
IDs are assigned by the **Writer**, not the producer LLM. The producer leaves ID fields blank (`""`) on new objects; the Writer mints them and preserves existing IDs on modification. This prevents ID collisions from LLM invention.

### 2.7 `gaps` — structured missing/unconfirmed information

A top-level array. Each gap is a thing the chatbot can turn into a question for the consultant. Gaps are how the profile stays honest about what it doesn't know, instead of guessing. The chatbot reads `gaps`, asks the consultant, and on answer either fills the profile (and removes the gap) or records that the consultant declined.

| Field | Type | Disposition | Notes |
|---|---|---|---|
| `gap_id` | `string` | required | Writer-assigned. |
| `kind` | `string<enum>` | required | `gap_kind` vocab (§3). |
| `severity` | `string<enum>` | required | `gap_severity` vocab. `blocking` = a required field is missing (profile incomplete); `valuable` = optional but worth chasing; `minor` = nice-to-have. The chatbot asks blocking first. |
| `target_ref` | `string` | optional | The `role_group_id` / `block_id` / `education_id` / field path the gap concerns. Omit for profile-level gaps (e.g. missing email). |
| `description` | `string` | required | Human-readable statement of what's missing and where. |
| `suggested_question` | `string` | optional | A ready-to-ask question the chatbot may use or rephrase. |
| `status` | `string<enum>` | required | `gap_status` vocab. `open` / `resolved` / `declined`. |

---

## 3. Controlled vocabularies

Producer LLMs prefer these values. On **extendable** vocabularies, an unlisted value may be emitted with `vocab_extension: true`. On **closed** vocabularies, an unlisted value is a validation failure.

**`language_proficiency`** *(closed)* — `native`, `fluent`, `professional`, `conversational`, `basic`

**`link_kind`** *(closed)* — `linkedin`, `github`, `gitlab`, `personal_website`, `publications`, `orcid`, `portfolio`, `other`

**`industry`** *(extendable)* — `automotive`, `aerospace`, `defense`, `heavy_vehicles`, `telecom`, `energy`, `power_generation`, `manufacturing`, `medical_devices`, `pharmaceuticals`, `finance`, `fintech`, `insurance`, `retail`, `consumer_software`, `enterprise_software`, `ai_ml`, `public_sector`, `education`, `research`, `consulting`, `other`

**`domain`** *(extendable)* — subject-matter areas, e.g. `embedded systems`, `safety-critical software`, `autonomous driving`, `control systems`, `turbine engine control`, `signal processing`, `cloud infrastructure`, `data engineering`, `machine learning`, `AI-assisted development`, `developer tooling`, `telecom systems`, `industrial automation`, `robotics`, `systems architecture`. Extend freely with `vocab_extension: true`.

**`seniority`** *(closed)* — `junior`, `mid`, `senior`, `staff`, `principal`, `lead`, `manager`, `director`, `executive`

**`employment_type`** *(closed)* — `permanent`, `fixed_term`, `contract`, `consultant_via_employer`, `freelance`, `internship`, `academic`, `personal_project`, `open_source`, `other`

**`work_mode`** *(closed)* — `onsite`, `hybrid`, `remote`

**`block_type`** *(closed)* — `technical_delivery`, `technical_leadership`, `people_management`, `project_management`, `innovation`, `teaching_mentoring`, `research`, `other`

**`evidence_type`** *(closed)* — `responsibility` (tasked with) · `achievement` (produced/delivered) · `impact` (measurable outcome) · `artifact` (specific thing built) · `decision` (consequential choice + rationale)

**`education_type`** *(closed)* — `degree`, `certification`, `course`, `training`, `bootcamp`, `other`

**`process_standard`** *(extendable, suggestions)* — `ISO 26262`, `ISO 9001`, `ISO 27001`, `IEC 61508`, `IEC 62304`, `DO-178C`, `V-model`, `Agile`, `Scrum`, `SAFe`, `Kanban`, `DevOps`, `CMMI`, `6sigma (DMAIC)`, `TOPS 8D`

**`gap_kind`** *(closed)* —
`missing_required` (a required field could not be sourced) ·
`ambiguous_dates` (dates absent or imprecise) ·
`unattributed_skill` (a tool/skill appears but maps to no role; candidate for a project to demonstrate it) ·
`unconfirmed_inference` (an inferred value the consultant should confirm) ·
`missing_useful` (valuable optional info absent) ·
`data_conflict` (source contains contradictory information) ·
`other`

**`gap_severity`** *(closed)* — `blocking`, `valuable`, `minor`

**`gap_status`** *(closed)* — `open`, `resolved`, `declined`

---

## 4. Validation rules (Experience Writer, before persisting)

### 4.1 Structural
- Conforms to field types and required-ness in §2.
- `schema_version` recognized.
- `date_ym` values match `YYYY-MM` (month `01`–`12`); `"present"` only where permitted.

### 4.2 Cross-field
- Each `block.started`/`ended` falls within its parent role group's range (`"present"` propagates from an open role group to its blocks). *Skipped for objects whose dates are recorded as gaps.*
- No two role groups share the same `organization.name` with overlapping dates (use blocks). Personal projects are exempt.
- Every gap's `target_ref` (when present) references an existing object.

### 4.3 Vocabulary
- Every `string<enum>` value is listed or paired with `vocab_extension: true`.
- `vocab_extension` permitted only on extendable vocabularies (`industry`, `domain`, `process_standard`).

### 4.4 Flag-don't-guess (the missing-required rule)
When a **required** field cannot be sourced or reasonably inferred, the producer LLM **does not guess and does not discard the object**. It:
1. Omits the field (no placeholder value).
2. Sets `needs_review: true` on the containing object.
3. Adds a `gap` (§2.7) with `kind` (usually `missing_required` or `ambiguous_dates`), `severity: blocking`, a `target_ref`, a `description`, and a `suggested_question`.

A genuine **hard error** (input that cannot yield a valid object at all — e.g. contradictory information with no defensible reading, or no identifiable role) is still reported as a producer error and not persisted:
```yaml
error:
  kind: "conflicting_information" | "unparseable" | "other"
  context: "What and where"
  suggested_action: "What would resolve it"
```
The distinction: a *missing* required field becomes a gap (persist + ask later); *unusable* input becomes an error (don't persist). Prefer gaps; reserve errors for truly unusable input.

### 4.5 Completeness vs. draft
- A profile with ≥1 role group, ≥1 block, ≥1 evidence item, and **no `blocking` gaps** is **complete**.
- A profile that is otherwise well-formed but has open `blocking` gaps is a **draft-with-gaps**: valid to store and to generate from, but the pipeline surfaces the gaps to the chatbot.

---

## 5. Producer instructions (CV Importer & Experience Capture)

### 5.1 Compression — what to keep, what to drop
Apply §0.1. Concretely, when importing:
- **Always keep skeleton:** organization, role titles, dates (or a dates gap), employment type.
- **Keep evidence atoms specific and verbatim-in-meaning:** quantified results, named projects, specific technical work, consequential decisions. When unsure whether a concrete claim is an atom or filler, **keep it**.
- **Drop reconstructable phrasing and filler:** the CV's summary/objective paragraph; polished sentence wording (store the claim, not the sentence); boilerplate responsibilities ("covered the complete lifecycle", "responsible for various tasks"); zero-value specifics (addresses, generic course titles like "Effective meetings").
- **Grade by recency/importance:** recent and core roles keep all atoms; old or minor roles compress toward skeleton + `summary`, with only the few atoms that retain evidential value. Early-career or trivial roles may be a single skeleton block with one atom.
- **Distribute flat skill lists onto the blocks where used.** Tools/skills listed in a CV's competence section attach to the role(s) where context supports them. A tool that maps to no role → record an `unattributed_skill` gap rather than inventing a home for it.
- **Archive the raw CV** (§0.2) and set `metadata.source_archive`.

### 5.2 Handling missing/ambiguous source data
Apply §4.4 — flag, don't guess. Common cases:
- **No months on dates** ("2014–2020"): this is `ambiguous_dates`. Do not fabricate months. If the year-level range is enough to place the role in sequence, keep the role with a dates gap; if even the years are unclear, gap them too.
- **No dates at all** (e.g. an "additional experience" line): keep the role as skeleton, omit dates, `needs_review: true`, `ambiguous_dates` gap, `severity: blocking`.
- **Aspirational signals** (the CV reads as "I want AI work"): do not invent employment. Record as a `preferences` candidate (consultant-owned) and, if useful, a `missing_useful` gap to confirm direction in capture.
- **Unattributed tools/skills:** `unattributed_skill` gap with a `suggested_question` inviting the consultant to point to a project (professional or personal) demonstrating the skill.
- **Consultant client assignments (`employment_type: consultant_via_employer`):** The role_group organisation is the **employer** (e.g. the consulting firm); the `client` field on each block is the **end client** (e.g. the company where the work happens). Block `started`/`ended` are the **client-assignment dates**. If the CV does not state explicit dates for a client assignment, create an `ambiguous_dates` gap — do **not** default to the employer's start or end date. The `role_title` should describe the role only (e.g. "Senior Software Function Developer"), not embed the client name — that belongs in `client.name`.

### 5.3 Experience Capture / chatbot behavior
- Reads `gaps`, prioritizes `blocking` then `valuable`, and asks the consultant using `suggested_question` (rephrasing as natural).
- On an answer: route the *factual* content into the profile (filling the gapped field, adding a block/evidence item, or creating a `personal_project` role group as appropriate per the attachment principle), mark the gap `resolved`, and set `verified: true` on any block the consultant explicitly confirmed. If the consultant declines, mark it `declined` (don't keep re-asking).
- Applies the same compression discipline (§5.1) to captured input: store claims, not the consultant's verbatim phrasing.
- Style/presentation signals discovered during capture go to `style.md`, not here (see the style design doc).

---

## 6. Reading the profile (CV Content Generator)

- The profile is **facts**. Selection, emphasis, ordering, phrasing are the generator's job, informed by the job description, the generation prompt, and `style.md`.
- **Relevance scoring:** score each block against the target position using `industries`, `domains`, `languages`, `tools`, `processes_standards`, `seniority`, and recency (`as_of − ended`). Select evidence items from high-scoring blocks.
- **Evidence types** shape phrasing: `impact` leads with `quantification`; `achievement` with the outcome; `responsibility` with scope; `decision` with the choice/rationale; `artifact` with the thing built.
- **Personal projects** (`personal_project` / `open_source`) are real evidence; surface when relevant and **never imply they were professional engagements** — label honestly.
- **Aspirational interests** live in `preferences`; combine with a related personal project to honestly frame direction when the position warrants. Default to omitting such framing unless relevant.
- **`needs_review` / gapped data:** usable, but treat unconfirmed fields cautiously — don't state a gapped date as if certain; prefer ranges or omission.
- **Trust:** `verified: true` on a block → consultant has confirmed its content, use freely. Absent → imported or inferred but unconfirmed; faithful but treat with a lighter touch on high-stakes specifics.
- **Time math:** block duration = `ended − started` months (`"present"` = `as_of`); don't double-count overlapping blocks.

---

## 7. Worked example (abbreviated, showing gaps)

```yaml
metadata:
  schema_version: "1.2"
  consultant_id: "norberto_munoz"
  created_at: "2026-05-27T12:00:00Z"
  updated_at: "2026-06-09T12:00:00Z"
  as_of: "2026-06"
  source_archive: "uploads/2026-05-25_cv.pdf"

identity:
  full_name: "Norberto Muñoz Gómez"
  # email omitted — not on CV — see gap g1
  location: { city: "Gothenburg", country: "Sweden", country_code: "SE" }
  spoken_languages:
    - { language: "English", proficiency: "native", is_native: true }
    - { language: "Spanish", proficiency: "native", is_native: true }
    - { language: "Swedish", proficiency: "fluent" }

career_history:
  role_groups:
    - role_group_id: "rg_ge"
      organization: { name: "General Electric", country_code: "MX", industries: ["power_generation"] }
      display_role_title: "Embedded Control SW Developer / Team Manager / Technical Software Lead"
      started: "2005-09"
      ended: "2020-12"
      employment_type: "permanent"
      summary: "15 years at GE across embedded control, team management and technical leadership for turbine engines."
      needs_review: true
      blocks:
        - block_id: "b_ge_mgr"
          role_title: "Team Manager"
          # started/ended omitted — CV gives a range but verify — see gap g2
          block_type: "people_management"
          seniority: "manager"
          needs_review: true
          evidence_items:
            - { evidence_id: "", type: "responsibility",
                text: "Led a software development team: resourcing, goals, career development, appraisals and delivery against targets." }
        - block_id: "b_ge_dev"
          role_title: "Embedded Control SW Developer"
          started: "2005-09"
          ended: "2011-01"
          block_type: "technical_delivery"
          seniority: "mid"
          verified: true
          domains: ["embedded systems", "control systems", "turbine engine control"]
          languages: ["Fortran", "Perl"]
          tools: ["beacon7", "IBM Synergy", "Unix"]
          processes_standards: ["V-model", "6sigma (DMAIC)"]
          evidence_items:
            - { evidence_id: "", type: "responsibility",
                text: "Developed embedded control software for turbine engines (marine and industrial), end to end from budget negotiation through validation in test cells." }

gaps:
  - gap_id: "g1"
    kind: "missing_required"
    severity: "blocking"
    description: "No email address found in the CV."
    suggested_question: "What email address should appear on your CVs?"
    status: "open"
  - gap_id: "g2"
    kind: "ambiguous_dates"
    severity: "blocking"
    target_ref: "b_ge_mgr"
    description: "Team Manager role at GE has no confirmed start/end months."
    suggested_question: "From when to when did you act as Team Manager at General Electric?"
    status: "open"
  - gap_id: "g3"
    kind: "unattributed_skill"
    severity: "valuable"
    description: "Claude Code, Codex and openClaw appear in the competence list but map to no described role."
    suggested_question: "Do you have projects — personal or professional — where you used Claude Code, Codex or openClaw? When, and what did you build?"
    status: "open"
```

---

## 8. Appendix: versioning & deferred items

**Versioning.** `metadata.schema_version` required; Writer rejects unknown versions. Semantic versioning: major = breaking (needs migration), minor = additive (existing profiles stay valid), patch = clarification. Migrations are deterministic and never invent data.

**1.2 → 1.3 migration:** No removal of existing data. Optionally backfill `client.name` on blocks within `consultant_via_employer` role_groups by parsing the client name out of `role_title` (e.g. `"Senior Software Function Developer — Volvo Group"` → `client.name: "Volvo Group"`, `role_title: "Senior Software Function Developer"`). Update `metadata.schema_version` to `"1.3"`.

**1.1 → 1.2 migration:** Remove all `provenance` objects from role_groups and blocks. Remove `keywords` and `tools_referenced` from all evidence_items. If any block had `provenance.reviewed_by_consultant: true`, add `verified: true` directly on that block; discard all other provenance fields. Update `metadata.schema_version` to `"1.2"`.

**Deferred (out of scope for now):**
- `skill_claims` — queryable aggregated-skills layer; generator derives from blocks at read time for now.
- Retrieval/RAG fields (embeddings, denormalized views) — added when retrieval is built; evidence items are already atomic, the only structural prerequisite.
- `narrative_anchors` / pre-written summaries / weights — presentation lives in `style.md` and the generation prompt.
- Multi-language profile content; references/endorsements; application history (lives in the Application Tracker).

**Companion documents (separate from this schema):**
- `style.md` — per-consultant learned presentation preferences. See the style design doc.
- Global CV generation prompt — the system's general CV-writing craft, operator-curated.
- Archived raw CV(s) — lossless backstop in the consultant's `uploads/` folder.
