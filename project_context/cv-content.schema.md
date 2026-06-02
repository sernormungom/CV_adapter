# CV Content Schema

- **Schema version:** 1.0
- **Status:** Draft
- **Date:** 2026-06-02
- **Format:** YAML (the schema is format-agnostic; examples use YAML)
- **Filename convention:** `cv-content.<position_slug>.yaml` inside the consultant's folder

---

## 0. Purpose

A **CV content document** is the structured, position-specific intermediate produced by the CV Content Generator in a single LLM call, given three inputs: the consultant's `profile.yaml` (facts), the relevant `style.md` Rules (presentation lens), and the target job description. It is the **contract between intelligence and rendering**: the LLM does all selection, tailoring, and phrasing here; the Step 9 Jinja2 template reads this document and produces HTML with **no further LLM calls**.

### Design principle: this document is fully resolved

Where `profile.yaml` holds *facts* and refuses presentation decisions, this document is the opposite: every field is a presentation decision already made. Job titles are tailored to the position (not copied from the profile), the summary is written fresh, skills are selected and ordered for relevance, bullets are rewritten from `evidence_items` to fit the role. The renderer is mechanical — it lays out exactly what is here, in the order given. Therefore the document must be **complete** (renderer needs nothing more), **typed** (programmatically validatable), and **unambiguous** (the generating LLM can produce it without guessing layout intent).

| Reader | Reads | For |
|---|---|---|
| **CV Content Generator** (LLM) | All | Producing this document from profile + style + job description |
| **CV Renderer** (Jinja2, Step 9) | All | Laying out HTML; performs no content decisions |
| **Validator** | All | Checking the document is renderable before handoff (§3) |

---

## 1. Structure

```
cv_content
├── meta              (schema version, consultant id, target position, length)
├── header            (name, tailored job title, contact line)
├── summary           (2–3 sentence position-tailored professional summary)
├── competencies      (6–12 selected skill/keyword strings → sidebar)
├── experience[]      (entries ordered by recency; bullets tailored from evidence)
├── education[]       (institution, qualification, years)
├── languages[]       (spoken language + proficiency)
├── courses[]         (optional: certifications/courses relevant to position)
├── interests[]       (optional: personal interests, only if position warrants)
└── render            (section_order, sidebar_sections, length)
```

**Cardinality:** one document per (consultant × position). `experience` ≥1; `education` ≥0; `languages` ≥1; `competencies` 6–12. Optional arrays are omitted entirely when empty (never `null` or `[]`).

---

## 2. Field reference

**Types:** `string`; `string<enum>` (controlled values, marked inline); `array<T>`; `object{...}`. **Disposition:** **required** (must be present), **optional** (include only when warranted; omit otherwise). All text is plain string — no markdown, no HTML; the renderer styles it.

### 2.1 `meta`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `schema_version` | `string` | required | E.g. `"1.0"`. |
| `consultant_id` | `string` | required | Matches `profile.yaml` `consultant_id`. |
| `target_position` | `string` | required | The role this CV targets, e.g. `"Senior Embedded Engineer — Zenseact"`. |
| `generated_at` | ISO 8601 | required | Set by the generator. |
| `source_job_ref` | `string` | optional | Identifier/URL of the job description used. |

### 2.2 `header`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `full_name` | `string` | required | From `profile.identity.full_name`. |
| `job_title` | `string` | required | **Tailored to the position**, not copied from any profile role. E.g. `"Senior Embedded Software Engineer"`. |
| `contact` | `object{email, phone, location, linkedin}` | required | `email` required; `phone`, `location`, `linkedin` optional (include `linkedin` only if a `linkedin` link exists in profile). `location` is a display string, e.g. `"Gothenburg, Sweden"`. |

### 2.3 `summary`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `summary` | `string` | required | 2–3 sentences, written fresh for this position. Never pre-existing in the profile. Honours `style.md` tone/avoidances. |

### 2.4 `competencies`
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `competencies` | `array<string>` | required | 6–12 keyword/skill strings, selected and ordered by relevance to the position, derived from profile blocks (`languages`, `tools`, `domains`, `processes_standards`). Short noun phrases, e.g. `"AUTOSAR"`, `"ISO 26262 functional safety"`. Rendered in the sidebar. |

### 2.5 `experience` (array; ordered most-recent first)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `organization` | `string` | required | Display name. For consultancy-via-client use the form your template expects, e.g. `"Volvo Group (via Mpya Sci & Tech)"`. |
| `role_title` | `string` | required | Title shown for this entry; may be tailored toward the position vocabulary while staying truthful to the profile. |
| `date_range` | `string` | required | Display string, e.g. `"Oct 2024 – Present"`. |
| `is_personal_project` | `boolean` | optional | `true` for `personal_project`/`open_source` role groups. Default `false`. Renderer labels these clearly; never present them as professional engagements. |
| `bullets` | `array<string>` | required | 3–5 bullets, past tense, tailored from the source block's `evidence_items` to the position. No markdown. |

### 2.6 `education` (array)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `institution` | `string` | required | |
| `qualification` | `string` | required | E.g. `"MSc in Electrical Engineering"`. |
| `years` | `string` | required | Display string, e.g. `"2003 – 2005"` or `"2005"`. |

### 2.7 `languages` (array)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `language` | `string` | required | E.g. `"Swedish"`. |
| `proficiency` | `string<enum>` | required | `native` · `fluent` · `professional` · `conversational` · `basic` (mirrors profile `language_proficiency`). |

### 2.8 `courses` (optional array — certifications/training)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `name` | `string` | required (if entry present) | E.g. `"ISTQB Foundation"`. |
| `issuer` | `string` | optional | |
| `year` | `string` | optional | |

Include the `courses` section only when at least one item is relevant to the position. Otherwise omit it (and drop it from `render.section_order`).

### 2.9 `interests` (optional array)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `interests` | `array<string>` | optional | Short strings. Include **only** when the position context warrants it (e.g. a culture-fit-heavy role) and the profile supports it. Omit by default. |

### 2.10 `render` (rendering metadata)
| Field | Type | Disposition | Notes |
|---|---|---|---|
| `length` | `string<enum>` | required | `one_page` · `two_page`. Default `two_page` for most engineers. |
| `section_order` | `array<string>` | required | Section names in render order. Allowed names: `summary`, `competencies`, `experience`, `education`, `languages`, `courses`, `interests`. Must list every populated section exactly once and no omitted one. `header` is implicit (always first) and not listed. |
| `sidebar_sections` | `array<string>` | required | Subset of `section_order` rendered in the 26.4% grey sidebar; the rest go in the 73.6% body. Typically `["competencies", "languages"]` (and `interests`/`courses` if short). |

---

## 3. Validation rules (checked before handoff to the renderer)

### 3.1 Structural
- Conforms to field types and required-ness in §2; `schema_version` is recognized.
- `competencies` has 6–12 items. Each `experience` entry has 3–5 `bullets`. `summary` is non-empty.
- `languages` has ≥1 item; each `proficiency` is in the closed enum (§2.7).
- Optional sections (`courses`, `interests`) are either fully populated or absent — never present-but-empty.

### 3.2 Cross-field (render integrity)
- Every name in `section_order` corresponds to a populated section in the document, and every populated section appears in `section_order` exactly once. No duplicates, no missing, no dangling names.
- `sidebar_sections` ⊆ `section_order`.
- `render.length: one_page` is consistent with content volume (validator may warn, not fail, if experience is long).
- `header.contact.linkedin` present only if the profile actually has a LinkedIn link.

### 3.3 Fidelity (lightweight, generator-side)
- `consultant_id` matches the source profile.
- `is_personal_project: true` exactly on entries sourced from `personal_project`/`open_source` role groups — these must not be phrased as professional employment.
- Bullets and summary state facts traceable to profile `evidence_items`; the generator must not invent claims (it tailors and rephrases only).

### 3.4 Generator error reporting
When the generator cannot produce a renderable document (e.g. too few relevant blocks to fill `competencies`, or no experience matches the position at all), it emits a structured error instead of padding:
```yaml
error:
  kind: "insufficient_evidence" | "no_relevant_experience" | "missing_required_field" | "other"
  context: "Short description of the problem"
  suggested_action: "What would resolve it (e.g. 'capture session to add recent embedded work')"
```

---

## 4. Worked example (minimal, valid)

```yaml
meta:
  schema_version: "1.0"
  consultant_id: "norberto_munoz"
  target_position: "Senior Embedded Software Engineer — Zenseact"
  generated_at: "2026-06-02T09:00:00Z"
  source_job_ref: "zenseact-emb-2026-04"

header:
  full_name: "Norberto Muñoz"
  job_title: "Senior Embedded Software Engineer"
  contact:
    email: "norberto@example.com"
    phone: "+46 70 123 45 67"
    location: "Gothenburg, Sweden"
    linkedin: "https://linkedin.com/in/example"

summary: >-
  Senior embedded software engineer with deep experience delivering
  safety-critical, fail-operational software for autonomous heavy vehicles.
  Works fluently across AUTOSAR and ISO 26262, and thrives in cross-functional
  teams shipping under real-time constraints.

competencies:
  - "Embedded C / C#"
  - "AUTOSAR"
  - "ISO 26262 functional safety"
  - "Real-time systems"
  - "Autonomous driving"
  - "Vector DaVinci"
  - "V-model & SAFe"
  - "Safety-critical software"

experience:
  - organization: "Volvo Group (via Mpya Sci & Tech)"
    role_title: "Senior Software Function Developer"
    date_range: "Oct 2024 – Present"
    bullets:
      - "Developed safety-critical, fail-operational embedded software for autonomous trucks under strict real-time and reliability constraints."
      - "Worked within an 8-person team applying ISO 26262 and the V-model across the software lifecycle."
      - "Built and integrated AUTOSAR software components using Vector DaVinci."
      - "Modelled system behaviour and interfaces in SystemWeaver to keep delivery aligned across functions."
  - organization: "Personal Project"
    role_title: "Creator — CV Preparation Pipeline"
    date_range: "Feb 2026 – Present"
    is_personal_project: true
    bullets:
      - "Built an LLM-driven pipeline that generates position-specific CVs from a structured career profile."
      - "Designed the schema contract separating facts, presentation style, and rendering."
      - "Implemented the Python/FastAPI service orchestrating profile, style, and job-description inputs."

education:
  - institution: "ITESM"
    qualification: "MSc in Electrical Engineering"
    years: "2005"

languages:
  - { language: "Spanish", proficiency: "native" }
  - { language: "English", proficiency: "fluent" }
  - { language: "Swedish", proficiency: "professional" }

render:
  length: "two_page"
  section_order: ["summary", "competencies", "experience", "education", "languages"]
  sidebar_sections: ["competencies", "languages"]
```
