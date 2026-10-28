# EU AI Act assessment — CV Adapter

**Status:** V1 assessment drafted 2026-07-08; V2 design amendment added
2026-08-21. **V2 classification is open and requires qualified counsel before
production/external use.**

**Not legal advice.** This is an engineering-oriented classification analysis written to be *usable by* qualified Swedish counsel and a future auditor — it takes reasoned positions on purpose so they can be pushed back on, but it is not itself a legal opinion. Counsel must review it, and the AI Act text it relies on (2024/1689 as amended by the 2026 Digital Omnibus) should be checked against the consolidated in-force version before anything here is relied on externally. Where the analysis depends on the amended/renumbered text, that is flagged.

**Scope.** Classification of CV Adapter under Regulation (EU) 2024/1689 (the AI Act): (1) which components are "AI systems" at all; (2) whether the system is high-risk under Annex III point 4 (employment); (3) the Article 6(3) reasoning if not; (4) the timeline; (5) a deployer-obligation readiness checklist against Article 26; (6) what already applies today regardless of classification.

---

## 0. V2 executive conclusion and launch gate

**V3 correction (2026-09-10):** the "automatically applies an LLM-proposed
matching configuration after schema/delta-guard validation" design this
section describes was never built, and V3-P-008
decision 5 now orders it deleted rather than built (V3 packet S2): there is no
verdict-fed proposal, no delta guard, and no auto-apply. What replaces it is
*less* automated, not more — a human presses "Update match configurator" and
an LLM generates a whole config from `profile.md` alone, with a failed or
invalid generation leaving the previous valid config in place (decision 4).
This reduces the classification concern the paragraph below raises (an
automatically-applied, behaviour-derived config change with no human step),
but does not eliminate the broader question below of whether an LLM-generated,
per-person `matching_config.yaml` — now authored from the consultant's own
profile text rather than their accumulated verdict history — constitutes
"profiling" under §2.4. Re-read the paragraph below with that substitution;
Consultant–Job score persistence (V3-P-006/P-007/P-008) is a separate, still
open V3 item unaffected by this correction.

**Do not carry the V1 “not high-risk” conclusion into V2.** V2 deliberately removes
TA-curated review batches and the pending-config approval click, automatically
applies an LLM-proposed matching configuration after schema/delta-guard validation,
and stores versioned Consultant–Job score projections. Three facts still reduce
risk: scores only order Jobs; no score/config can reject, discard, suppress, or make
a Job ineligible; and Apply/Reject/publication remain deliberate human actions.
Those controls are important but they do not recreate the V1 Article 6(3) argument.

The current consolidated AI Act places Annex III systems in the high-risk category
subject to Article 6(3), lists employment recruitment/selection and certain
work-allocation/evaluation uses in Annex III point 4, and states that an Annex III
system performing profiling is always high-risk. Human oversight is a required
control for high-risk systems, not by itself proof that a system falls outside the
category. Official source: [Regulation (EU) 2024/1689, current consolidated
version](https://eur-lex.europa.eu/eli/reg/2024/1689/oj?locale=en).

The engineering posture for V2 is therefore:

1. Treat V2 as **potentially high-risk pending qualified Swedish/EU counsel**, not
   as proven high-risk and not as covered by the old exemption.
2. Keep scores strictly advisory and eligibility-independent, with plain-language
   explanations, visible missing/stale states, deterministic auditability, and no
   automated client submission.
3. Retain human authority over every outward or employment-process action: a person
   chooses Apply/Reject, reviews/edits the CV, deliberately publishes/shares it, and
   chooses Application outcomes.
4. Log config proposal validation/application and input versions without retaining
   rejection free text longer than required; test bias/quality and allow operational
   suspension/rollback to the last valid config.
5. Make the final V2 classification, provider/deployer obligations, worker
   information, instructions for use, conformity/registration implications, and
   applicable dates a **production launch gate**. Do not rely externally on the
   dates or conclusions in the historical V1 sections without revalidation against
   the then-current consolidated text.

The remainder of this document preserves the July 2026 V1 analysis so counsel can
see what changed. Where it refers to pending config approval, TA batch curation,
ephemeral scores, or `yes/no/maybe`, it is historical and not a description of V2.
As of V2 Slice 1 the code it cites for those controls is **deleted**, not merely
superseded — `dashboard/routes.py` (`submit_verdict`, `confirm_config`), the
review batch, and the JSON Job Store are gone. In their place: a person records
Reject (or, from Slice 2, Apply) directly on a shared relational Job, there is no
Maybe, no cycle, and no score is stored on a Job or on a review marker. Cite §0
and the current subsystem contracts (`docs/architecture/areas/`), never a path from the sections below.

---

## 1. AI-system definition analysis (Article 3(1)), per component

> **V1 historical analysis.** V2's legal position is the launch-gate conclusion in
> §0. ADR-012 stores advisory projections, and valid Learning proposals auto-apply.

The Act only regulates "AI systems" (and separately, general-purpose AI *models*). Getting this boundary right is the first substantive judgment, because the deterministic scorer is the component doing the actual opportunity-filtering, and whether *it* is an AI system materially changes the Annex III analysis. The components must be assessed individually, not lumped together.

The Commission's guidelines on the Article 3(1) definition (published February 2025) treat as decisive whether a system *infers, from the input it receives, how to generate outputs* (predictions, content, recommendations, decisions) — as opposed to executing rules **defined solely by natural persons** to automatically carry out operations. Systems in the latter category are expressly *not* AI systems.

### 1.1 The deterministic scorer — **not an AI system**

`shared/scoring.py` (`score_job` and its five fit-dimension functions) is pure, human-authored rule execution: case-folded substring/`\b`-boundary term matching (`_in_text`), weighted sums over `scoring_weights`, fixed bonuses/penalties, and threshold comparison against `thresholds.keep`/`thresholds.maybe`. It does not learn, does not infer, and produces the same output for the same `(profile, config, job)` every time. Every number that gives it behaviour — weights, term catalogs, thresholds, archetypes — is external data in `matching_config.yaml`, not learned parameters inside the scorer. Under the Commission guidelines this is a classic rule-based system: **not an AI system.**

This matters more than it first appears. The scorer is the component that actually ranks and (via the batch) filters opportunities. So the node exerting the most direct "influence" over which jobs reach a consultant is *not* an AI system — which is central to the Article 6(3) material-influence analysis in §3.

**Honest nuance:** the *config the scorer consumes* is produced, in part, by an AI system (the learning loop, §1.2). The scorer node stays non-AI, but it sits downstream of an AI system. The right framing is therefore "the AI system is the learning loop that tunes the config, not the scorer that applies it" — not "there is no AI in the matching pipeline." The two must not be conflated.

### 1.2 The LLM-backed components — **AI systems** (built on Anthropic Claude, a GPAI model)

The following components call the Claude API via `backend/llm.py` and each *infers content/recommendations from input*. Each is an AI system:

- **CV fact extraction** — `backend/cv_importer/experience_extractor.py`: extracts structured facts from uploaded CV text.
- **CV generation** — `backend/cv_pipeline/cv_content_generator.py`, invoked by the durable handler `backend/tailored_cv/handler.py` (V2 Slice 3; the synchronous `handoff_receiver.py` endpoint is retired): drafts tailored CV content from a frozen snapshot of the profile + job description.
- **Chat assistant** — `backend/cv_assistant/chat_engine.py`.
- **Profile patcher** — `backend/cv_importer/`/profile-editing LLM call.
- **Learning loop** — `backend/opportunity_pipeline/learning_module/config_updater.py` and `config_bootstrapper.py`: Claude proposes/bootstraps `matching_config.yaml`. This is the AI system that shapes the matching pipeline's behaviour, and the one closest to the employment use-case.

These "may exhibit adaptiveness" and generate outputs from inference — squarely AI systems.

### 1.3 Roles — provider, deployer, and GPAI-model provider

This distinction changes *which* obligations attach if the system is ever high-risk, so it is recorded here even though the conclusion is "not high-risk":

- **Anthropic** is the provider of the general-purpose AI *model* (Claude). Anthropic's own GPAI-model obligations (in force since 2025-08-02) are Anthropic's, not the Company's. Anthropic is also a GDPR processor for our data (see `processors.md`).
- **The Company** builds the CV Adapter *AI systems* on top of Claude and puts them into service for its own use. For AI Act purposes the Company is therefore **both the provider and the deployer** of those AI systems. Putting an AI system into service for one's own internal use still triggers *provider* status.
- **Consequence if reclassified high-risk:** the Company would face **provider** duties (Article 16 — conformity assessment, technical documentation, quality management, registration, CE marking) *and* **deployer** duties (Article 26), not merely the Article 26 checklist in §5. This is a material reason the "not high-risk" conclusion is worth getting right and worth protecting by design.

---

## 2. Annex III point 4 (employment) applicability

> **V1 historical analysis.** The V2 removal of batch curation and config approval
> materially weakens §§2.3–3; those passages must not be quoted as V2 conclusions.

Annex III point 4 covers:

- **4(a):** AI systems intended for the **recruitment or selection** of natural persons — in particular to place targeted job ads, to analyse and filter applications, and to **evaluate candidates**.
- **4(b):** AI systems intended to make **decisions affecting terms of work-related relationships**, promotion/termination, **to allocate tasks based on individual behaviour or personal traits or characteristics**, or to **monitor and evaluate performance/behaviour** in such relationships.

### 2.1 Which limb is the closest fit

The "candidates" here are the Company's **own employed consultants**, not external applicants being recruited *into* the Company. The tool surfaces external client assignments to internal consultants and helps present them. So:

- **4(a) is a weak, secondary fit.** The CV-generation feature assists in presenting a consultant for a client's selection, which brushes against "selection / evaluate candidates" — but the selecting party is the *client*, and the tool only drafts presentation material a human edits. It does not analyse/filter applications submitted to the Company.
- **4(b) is the closest fit and the one to reason honestly about.** Allocating client assignments to consultants, based on a profile (personal traits/characteristics) and a config partly derived from verdict *behaviour* (individual behaviour), inside an existing employer–employee relationship, is the natural reading of "allocate tasks based on individual behaviour or personal traits." This is the borderline the rest of §2 addresses.

### 2.2 The case **FOR** high-risk (4(b))

Stated at its strongest, so counsel can weigh it:

1. **Functional purpose.** The pipeline's whole point is to route opportunities (client assignments) to consultants — economically consequential task allocation within an employment relationship.
2. **Behaviour-derived personalisation.** The per-consultant `matching_config.yaml` is tuned by the learning loop from the consultant's own verdict history (`history_aggregator.py` → `config_update_prompt.py`), including free-text reasons. That is "individual behaviour" driving allocation.
3. **A real filtering effect on the person's opportunity set.** A consultant only reviews the batch the TA curates from the scored list (`batch_assembler.run_matching` → `batch_store`). Jobs the scorer ranks low may never reach the consultant. The AI-tuned config therefore shapes, one step upstream, *which opportunities a person is exposed to* — the kind of effect Annex III 4(b) is concerned with.
4. **The Article 6(3) profiling proviso** (see §2.4) is designed precisely to pull behaviour-derived employment tools back into high-risk despite human oversight.

### 2.3 The case **AGAINST** high-risk

1. **The AI systems do not make the decision or perform the allocation.** Annex III 4(b) reaches AI systems "intended to be used to make decisions … [or] to allocate tasks." In CV Adapter the allocation-shaping computation is done by a **non-AI** deterministic scorer (§1.1), and every *decision* is a human act: the TA curates the batch, the consultant renders the verdict (`dashboard/routes.py::submit_verdict`), a human account manager submits a consultant to a client. The AI systems' role is confined to *drafting* (CV), *extracting* (facts), *assisting* (chat), and *proposing tunings a human approves* (learning loop).
2. **The learning loop never auto-applies.** `config_updater.propose_config_update` only ever writes `matching_config.pending.yaml`; it takes effect **only** when a human calls `POST /api/dashboard/{id}/confirm-config` (`dashboard/routes.py::confirm_config` → `config_writer.apply_pending_config`). Even a proposal that passes the delta guard is not applied without that human click. The AI's output is a *proposal*, not an operative change.
3. **The filtering effect is deliberately diluted and human-supervised.** The batch reserves exploration slots that inject non-top-scored jobs (`batch_assembler.py` → `exploration_selector.select_exploration_slots`), so the pipeline is not pure score-gating; and the TA sees and can override the curation. Scores themselves are **ephemeral and never stored on the person** (invariant #7): recomputed on demand per `(consultant, job)`, never persisted as an evaluative attribute of the consultant.
4. **CV generation is preparatory presentation help**, not candidate evaluation — it drafts content a human edits in `html/cv-importer.html` before any use. V2 Slice 3 does not weaken this and arguably strengthens it: the draft is written to an Application-owned artifact a human then opens in the editor's application mode, it never touches the consultant's own CV (P-009), and nothing is auto-sent. A human still asks for each generation (`POST /api/applications/{id}/cv/generate`).

### 2.4 The crux — the Article 6(3) profiling proviso

> **V3 note (2026-09-10):** the analysis below reads `matching_config.yaml` as
> "derived from the consultant's behaviour (their verdicts) and traits (their
> profile)." The verdicts half is gone — V3-P-008 decision 5 deletes the
> verdict-fed learning path (V3 packet S2), and the config is now generated
> from `profile.md` alone, regenerated only on an explicit human "Update match
> configurator" action. This narrows, but does not resolve, the profiling
> question: a config authored from the consultant's own explicitly-written
> career-interest text is a weaker "profiling" fit than one adapted from their
> accumulated accept/reject behaviour, but it remains a per-person,
> LLM-generated artefact that shapes ranking, so counsel should re-read this
> section with the verdict-derived half removed rather than treat it as
> resolved in either direction.

Article 6(3) lets an Annex III system escape high-risk classification when it does not pose a significant risk to health, safety or fundamental rights (see §3). **But its final subparagraph provides that an Annex III system is *always* high-risk where it performs *profiling of natural persons*** (profiling as defined in GDPR Article 4(4): automated processing of personal data to evaluate personal aspects, in particular to analyse or predict performance at work). This proviso is the single most decision-determining sentence in the whole assessment.

**Honest analysis:**

- **Cutting toward "profiling":** the persisted `matching_config.yaml` is an automated, per-person artefact derived from the consultant's behaviour (their verdicts) and traits (their profile). A broad reading of Article 4(4) — a per-person behavioural model that then governs how opportunities are ranked for them — can be characterised as profiling. This is a defensible view for a regulator to take, and it is the risk the plan (§2.3) flagged.
- **Cutting away from "profiling":** the *scores* — the thing that actually evaluates fit — are ephemeral, per-`(consultant, job)`, and never stored on the person (invariant #7). The system evaluates *the job against the person*, to rank opportunities for a human, not to produce or store an evaluation *of the person* that is applied to them. The only persisted per-person matching artefact is the config, which is a set of *matching parameters*, and it is human-approved before use. On this reading the system does not "profile" in the operative sense.

This is genuine, non-resolvable-by-engineering uncertainty. The prudent posture is to **assume the profiling reading is plausible** and therefore to *not* rely on Article 6(3) as the sole leg — which is exactly why the conclusion below leads with the scope leg.

### 2.5 Reasoned conclusion for §2

**CV Adapter is not high-risk under Annex III point 4**, primarily because its AI-system components fall outside the operative reach of 4(b): they do not make employment decisions or allocate tasks — humans do, aided by a deterministic (non-AI) ranking and by AI outputs that are always proposals/drafts subject to a human step. Article 6(3) provides an independent, alternative ground (§3), but it is treated as the *secondary* leg precisely because the profiling proviso (§2.4) could remove it. The conclusion is real and actionable, not a hedge — but it is **conditional on the human-in-the-loop design**, and it would flip if that design were removed (see §7).

---

## 3. Article 6(3) carve-out reasoning, tied to code

Where Annex III is arguably in play (the matching/learning pipeline), the Article 6(3) exemption is independently supported. Article 6(3) applies where the system does not pose a significant risk of harm to fundamental rights, *including by not materially influencing the outcome of decision-making*, and where at least one condition is met:

- **6(3)(d) — preparatory task to an assessment:** the learning loop prepares a *tuning proposal* for a human's assessment; it does not perform the assessment. Mechanism: `config_updater.propose_config_update` writes only `matching_config.pending.yaml`.
- **6(3)(c) — detecting patterns in / deviations from prior human decisions, without replacing the human assessment:** the learning loop reads the consultant's *prior verdicts* (`history_aggregator.py`) to detect their decision pattern and propose a config, and by construction does not replace the human assessment — the human re-approves.
- **6(3)(a)/(b) — narrow procedural / result-improving task:** CV generation and fact extraction improve/format material the human already authored (their real career facts) and are edited before use.

**Why there is no *material* influence on the outcome — the concrete mechanisms:**

1. **Delta guards** (`delta_guard.py`): a proposed config is *held* if any `scoring_weights` dimension shifts > ±0.30, `thresholds.keep`/`maybe` shift > ±10, or > 5 terms are removed from any one tier — capping how far one AI-proposed cycle can move behaviour.
2. **Mandatory human approval gate:** *no* proposed config — held or not — takes effect without `confirm-config` (`config_writer.apply_pending_config`); the previous config is archived (`config_archive/`) so changes are reversible and auditable.
3. **Batch curation:** the pre-filter batch + dashboard review decide which scored jobs reach a consultant; exploration slots prevent pure score-gating (`exploration_selector.py`).
4. **Consultant verdict authority:** the consultant makes the yes/no/maybe decision (`dashboard/routes.py::submit_verdict`); the score is a displayed aid, not a gate.
5. **CV draft review before use:** generated content lands in an editable artifact a human opens in the editor (`backend/tailored_cv/handler.py` → `/api/applications/{id}/cv` → `html/cv-importer.html` in application mode); nothing is auto-sent to a client.

These are not policy promises; they are enforced in code, and the plan is elevating "AI outputs never trigger an automated outward action without a human step" to CLAUDE.md invariant #9 (locked decision #5) so the property is protected against future changes. **Caveat:** as §2.4 states, Article 6(3) is unavailable *if* the config counts as profiling — so this leg is offered in the alternative, not as the sole basis.

---

## 4. Timeline — real runway, but classify now

Per `docs/legal/decisions.md` §1.2 (verified 2026-07-07), the **Digital Omnibus** (adopted Parliament 2026-06-16, Council 2026-06-29) **deferred stand-alone Annex III high-risk obligations to 2027-12-02** (Annex I embedded systems to 2028-08-02). Already in force regardless of any of this: the prohibited-practice rules and **Article 4 AI literacy** since 2025-02-02, and the GPAI-model rules since 2025-08-02.

**Plainly:** even on the worst-case assumption that CV Adapter *is* high-risk, no high-risk obligation binds before **2027-12-02** — roughly 17 months of runway from now. Nothing here is an emergency.

**But classifying now is still the cheap move**, for three concrete reasons: (1) the system already has most of the oversight machinery a high-risk deployer would need (human Apply/Reject decisions, human-triggered config generation, audit logging from Session 2, retention limits from Session 3) — documenting it now is far cheaper than reverse-engineering it under a deadline; (2) building oversight-by-design keeps the "not high-risk" conclusion *true*, whereas a future feature added without this lens (e.g. an auto-apply toggle) could silently flip the classification; (3) a written, reasoned assessment is what counsel and any auditor will ask for first, and it is far better produced deliberately than retrofitted.

---

## 5. Deployer-obligation readiness checklist (Article 26), target 2027-12-02

Prepared **as if** the system were a high-risk deployer's, so that reclassification would not require a scramble. Each item is marked *satisfied* / *partial* / *gap*. **Article sub-numbering should be confirmed against the consolidated Digital-Omnibus text — it may renumber; the substance is what matters.**

| Art 26 duty | Substance | Status today |
|---|---|---|
| **26(1)** Use per instructions-for-use | Technical/organisational measures to use the system per the provider's IFU | **Partial → gap.** The Company is its own provider, so no external IFU exists; but `cv_content_prompt.md`, the four `project_context/*.schema.md` contracts, and CLAUDE.md already document intended behaviour and could seed a formal IFU. **Gap:** author a short IFU (intended purpose, limits, required human steps) if/when high-risk. |
| **26(2)** Human oversight by competent, trained persons with authority | Assign oversight to natural persons with competence, training, authority, support | **Substantially satisfied by design.** Oversight is structural: a Consultant or TA chooses Apply/Reject on every Job, and matching-config generation only ever runs from an explicit human "Update match configurator" action (V3-P-008 decision 4) — the TA-curation, consultant-verdict, and admin-config-approval flow this row used to describe was V1 design and is gone (decision 5, V3 packet S2). **Gap:** *formally assign* the oversight role. Assign to the **admin/TA function** (a role, not a named person): the TA function owns Job/Consultant management; the **admin** role owns erasure/export and retention runs. Record this assignment in `ropa.md`. |
| **26(3)** Input-data control | Ensure input data is relevant/representative to the extent the deployer controls it | **Largely satisfied.** Profile inputs are the consultant's own CV, written facts-only against a schema (invariant #2); Session 3 added retention limits (stale-data minimisation). The email cleaner's third-party-PII stripping never applied to V2's manual Job entry and the code itself was removed as dead in STAB-7 (`docs/legal/dpia.md` Risk 4, `docs/legal/ropa.md` §4) — do not cite it as a control here either. **Gap:** no documented input-quality check — note it as a light procedure. |
| **26(5)** Monitor operation; suspend & notify on Art 79(1) risk | Monitor per IFU; if use may present a risk, inform provider/authority and suspend | **Partial.** Provider = deployer = the Company, so "inform the provider" collapses into internal incident handling → the `breach-runbook.md` (Session 5, separate doc) is the path. **Gap:** add an AI-malfunction trigger (not only a data breach) to that runbook. |
| **26(6)** Keep automatically generated logs ≥ 6 months | Retain the system's logs, to the extent under the deployer's control, ≥ 6 months | **Partial.** The verdict snapshots (24-month retention, Session 3) this row used to cite as part of the durable decision trail no longer exist — the `verdicts` table and its retention purge were dropped with no writer left (V3-P-008 decision 5, V3 packet S2). What remains: `Application`/`JobReview` rows (indefinite, until offboarding erasure) recording each Apply/Reject decision, and the Session 2 `AuditLog` (export/erasure actor + hashed target). The `config_archive/` history and pending-config `diff`/`rationale` this row also cited belonged to the deleted config-update proposal writer and are likewise gone; a regenerated `matching_config.yaml` carries only its own `metadata.updated_at`, not a diff against the version it replaced. **Gap:** (a) set CloudWatch app-log retention to ≥ 6 months in the infra repo; (b) *designate* the Application/JobReview history plus the audit trail as "the AI-system logs" in writing so the obligation maps to an actual artefact; (c) decide whether the loss of config-change history matters for this obligation, now that a regeneration is not diffed or archived. |
| **26(7)** Inform workers / worker representatives before use at the workplace | Inform affected workers and their reps before putting a high-risk system into use | **Gap — owned by HR, not code.** Handled by the Session 5 HR worker-information memo + the **MBL §11** co-determination step (`docs/legal/decisions.md` §1.2, §2.2). Do the worker-information pack once; it also satisfies the GDPR transparency angle. |
| **26(9)** DPIA leveraging IFU | Use the IFU to perform the GDPR Art 35 DPIA | **Cross-ref.** The `dpia.md` (Session 5, separate) covers systematic employee evaluation with novel tech; ensure it cites this assessment. |
| **26(11)** Inform affected natural persons | Tell persons subject to high-risk decisions that they are | **Partial.** The Session 1 privacy notice (`html/privacy.html`) already discloses the matching/scoring processing to consultants; extend it with the explicit AI-decision-support statement if high-risk. |
| Art 27 FRIA | Fundamental-rights impact assessment | **Likely N/A.** Art 27 targets public-body and specified-sector deployers; a private staffing firm is likely out of scope. **Action:** document the "N/A" decision with counsel rather than leave it unaddressed. |
| GDPR duties unaffected (26(4)) | Deployer GDPR obligations stand regardless | **Satisfied** by the GDPR workstream (Sessions 1–4). |

**Net readiness:** the substantive oversight and logging machinery mostly exists; the true gaps are *documentation* (IFU, formal oversight-role assignment, designating the log trail) and *people/HR* (worker information, MBL §11) — not new engineering.

---

## 6. Applicable today regardless of classification — Article 4 AI literacy

Article 4 (AI literacy) has applied since **2025-02-02** and does not wait for any classification. It requires that staff dealing with the system have a sufficient understanding of it. At the Company's scale a short internal briefing suffices; **what *this document* requires** is that TAs and admins understand these specific points:

1. **The match score is deterministic rule output, not an AI judgment.** It is a weighted keyword/term calculation (`shared/scoring.py`) and a *ranking aid* — never a decision. V2 stores the Consultant–Job projection for sorting and exposes its version/staleness, but a low/missing/stale score never hides, rejects, discards, or makes a Job ineligible.
2. **The learning loop uses Claude (Anthropic) to propose config changes from Reject feedback.** V2 auto-applies only after schema validation and the delta guard pass. This is an AI-influenced ranking change, not a human editorial approval. If validation/guard fails, the active config stays unchanged and Admin action is required.
3. **CV extraction/generation and the chat assistant use Claude and produce *drafts to review*.** The uploaded CV text, profile, and CV fields are sent to Anthropic under a processor DPA (no training on inputs, ~7-day API-log retention). This is disclosed in the privacy notice.
4. **Automation-bias warning:** Apply/Reject and every outward Application/CV action remain human choices. The ranking must not be treated as authoritative. This reduces risk but, for V2, is not represented as sufficient to keep the system outside high-risk classification.
5. **Sensitive-data hygiene:** do not paste GDPR Art 9 data (health, union membership, etc.) into CVs or the chat; the upload notice already warns consultants of this.

This is the literacy floor this assessment relies on — not a training curriculum. The full curriculum is the HR/Art 4 item in `docs/legal/decisions.md` §5.

---

## 7. Flagged uncertainties and re-review triggers

**V2/V3 re-review is already triggered.** Stored score projections remain an
accepted, still-open V3 decision (§0). Automatic validated config application —
this section's other original trigger — is no longer the design: V3-P-008
decision 5 deletes that mechanism, and config generation is now human-triggered
and profile-only (decision 4). Counsel must assess the actual intended purpose
and end-to-end system rather than relying on the July V1 component separation.
Until that review is recorded, potentially-high-risk engineering and
governance controls are the conservative baseline and production/external use
is gated.

**Genuine uncertainties (for counsel):**

1. **The profiling proviso (§2.4) is the live risk.** Whether the behaviour-derived `matching_config.yaml` constitutes "profiling of natural persons" under Article 6(3)'s final subparagraph is a real coin-flip a regulator could call either way. If it is profiling *and* the system is in Annex III, Article 6(3) is unavailable and the classification rests entirely on the scope leg (§2.5). Counsel should give a view on this specifically.
2. **Annex III scope for an *own-workforce* opportunity-allocation tool** (as opposed to external recruitment) is not squarely settled by the text; the 4(b) "allocate tasks based on individual behaviour" reading is plausible but not certain.
3. **The provider/deployer duality (§1.3):** if reclassified high-risk, the Company carries provider obligations too — a materially larger burden than the Article 26 checklist. Counsel should confirm the own-use "putting into service" characterisation.
4. **Amended text / renumbering:** this assessment relies on the AI Act as amended by the 2026 Digital Omnibus (per `docs/legal/decisions.md` §1.2). Article sub-numbering in §5 and the exact 6(3) wording must be verified against the consolidated in-force text. (Author's note: the Digital Omnibus post-dates this assistant's training data; the timeline and amendments are taken from the plan's verified facts, not independent recall.)

**After the required V2 review, re-review again if any of these change:**

- Learning auto-apply gains weaker validation, changes eligibility, or expands beyond advisory ordering.
- The scorer/batch begins **auto-submitting** consultants to clients or otherwise taking an outward action without a human — **would flip to high-risk.**
- Scores/config begin being used as durable evaluation of the person rather than a versioned Consultant–Job ordering aid.
- The tool is repurposed to **screen external applicants** (moving it squarely into 4(a) recruitment).
- Human authority over Apply/Reject, CV review/publication, or client submission is weakened or removed.
- The Digital Omnibus timeline or Article 6(3) text is further amended.

The V2 review must use the consolidated law and implementation actually in force at
launch; the earlier **2027-12-02** planning date is not independently relied on by
this amendment.

---

*Prepared for review by qualified Swedish counsel. Do not rely on this externally until that review is complete.*
