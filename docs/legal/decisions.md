# Legal/Compliance Decisions — GDPR / Sweden / EU AI Act

**Status:** Decision record extracted from the original 5-session remediation
plan (all sessions implemented and merged to `main` as of 2026-07-08). The
session-by-session build log, the multi-chat execution protocol, and the
post-merge review findings were dropped from the active tree on 2026-08-27 —
they have zero external citations and are fully preserved in Git history and
the `v1-final` tag (as the original legal-compliance plan). Section
numbers below are unchanged from the original so existing citations (`§1.2`,
`§2.4`, `§3`, etc.) continue to resolve. Outstanding: qualified-counsel review
of the privacy notice and every `docs/legal/` artefact (not yet done —
engineering review is not a substitute for it), and the HR/leadership
decisions listed in §5 (MBL §11, DPO necessity).
**Scope:** Make CV Adapter compliant with EU/Swedish data-protection and AI rules: GDPR (2016/679), the Swedish Data Protection Act (Dataskyddslagen 2018:218), IMY practice, and the EU AI Act (2024/1689 as amended by the 2026 Digital Omnibus).
**Not legal advice.** This is an engineering-oriented compliance record. The governance artefacts it produced (privacy notice, DPIA, ROPA, AI Act assessment) must be reviewed by qualified Swedish counsel before being relied on.

> **V2 amendment (2026-08-21):** This file is the V1 remediation record, not the
> V2 implementation authority. D-013/amended ADR-009 replace raw path-token CV
> access and destructive expiry with the hardened fragment/exchange viewer and
> human-reviewed expiry lifecycle. D-014 makes local-auth hardening a V2 production
> gate while deferring SSO/Cognito/MFA. ADR-010 removes scraping/the local Cockpit,
> ADR-011 replaces fragile in-memory background work, and ADR-012 stores versioned
> advisory matches. V2 also auto-applies a Learning proposal after schema/delta
> validation while guaranteeing that score/config changes ordering only and never
> suppresses an eligible Job. This triggers the old LIA and AI Act reassessment
> conditions: the V1 “not high-risk” and decisive-human-config-approval conclusions
> must not be relied on for V2. Revalidate the LIA, AI Act assessment, DPIA,
> ROPA, privacy notice, and processor/security statements against the implemented
> V2 system with qualified counsel before production/external launch.

---

## 1. The facts this plan is based on

### 1.1 What the app actually does with personal data (code inventory, 2026-07-07)

**Consultant (employee) personal data:**

| Data | Where | Notes |
| --- | --- | --- |
| Account: email, username, full name, role, city, bcrypt password hash | Postgres `users` (`backend/db/models/user.py:44-70`) | Sessions/invites/API tokens stored hash-only — good |
| Structured career profile (identity, education, career history) | `data/profiles/<username>/profile.md` + `style.md`, `matching_config.yaml`, `cv_state.json` (the base CV, carrying a `_cv_identity` revision/hash block since V2 Slice 3), photo | The core PII store |
| Application-tailored CVs | Postgres `tailored_cvs` — `cv_state` (a CV document) + `input_snapshot` (the profile YAML, style rules, base CV state, and job text the generation used) | A separate storage identity from the base CV (P-009); one per Application |
| **Original uploaded CV files — kept forever** | `data/profiles/<username>/uploads/` (`experience_writer.py:254-264`) | No retention; `DEBUG_CV_IMPORT` also writes raw extracted text there |
| Verdict history: yes/no/maybe + **free-text reason**, score snapshot, timestamps | Postgres `verdicts` JSONB (`db/models/verdict.py:32-40`) | Employee-evaluation data; free text is fed verbatim to the Opus config-update call |
| Rendered CV HTML in share tokens | `CVShareToken` rows (`cv_share/service.py:27-49`) | 7-day expiry stops *serving* but rows are never deleted |

*(This table reflects the system at the time of the original legal review. The row it listed for "full profiles cached cleartext on the TA laptop" no longer applies: the TA Cockpit and its local cache were removed in V2 Step 1, ADR-010 — see `docs/legal/dpia.md` Risk 2 and `docs/legal/ropa.md` §5 for the retirement record.)*

**Third-party personal data:** job-record `raw_text` retains contact-person names/emails/phones from scraped ads and job-lead emails verbatim (`shared/job_record.py:184-200`); the email cleaner (`shared/collection.py:143-153`) does **not** strip leading `From:/To:` headers or inline contact details.

**What is sent to Anthropic (7 call sites via `backend/llm.py`):** raw CV text (first 10k chars, `experience_extractor.py:58-77`), full profile YAML (`cv_content_generator.py`, `chat_engine.py`, `profile_patcher.py`, `config_bootstrapper.py`), job descriptions, CV editor field snapshots (name/email/phone), and verdict history incl. free-text reasons (`config_update_prompt.py:66-77`). Usernames appear in LLM log labels (`chat:{username}`).

**Hosting:** AWS ECS Fargate in **eu-north-1 (Stockholm)** per `DEPLOY.md` — data at rest stays in the EU. But: the documented dev ALB URL is plain HTTP, `SESSION_COOKIE_SECURE=false` in compose, CORS is `allow_origins=["*"]` (`main.py:38-43`), and API bearer tokens never expire.

**Logging:** INFO-level logs include 400-char previews of extracted profiles / generated CVs (`experience_extractor.py:80`, `cv_content_generator.py:88`, `profile_merger.py:120`, etc.) → CloudWatch on ECS.

**What does NOT exist today:** any privacy notice or consent text in the UI; any export endpoint; any true delete (the only delete, `DELETE /api/ta/consultants/{username}`, removes the TA↔consultant *association* only); any retention limit on profiles, uploads, or verdicts.

### 1.2 External legal facts (verified 2026-07-07)

- **Anthropic (Claude API):** GDPR Art 28 DPA is auto-incorporated into the Commercial Terms (updated version effective 2026-01-01); EU SCCs Module 2 (controller→processor) plus **active EU-US Data Privacy Framework certification**; **no training on API inputs/outputs**; API logs retained **7 days** (since 2025-09-14). Anthropic is a lawful processor with a valid transfer mechanism — the main work on our side is *documenting* it and *telling data subjects*.
- **EU AI Act timeline:** the **Digital Omnibus** was formally adopted (Parliament 2026-06-16, Council 2026-06-29): stand-alone **Annex III high-risk obligations are deferred to 2027-12-02** (Annex I embedded systems to 2028-08-02). Already in force regardless: prohibitions + **AI literacy (Art 4)** since 2025-02-02, GPAI rules since 2025-08-02.
- **Swedish layer:** IMY is the supervisory authority. Two Sweden-specific points beyond GDPR text:
  1. **Consent is the wrong legal basis for employee data.** IMY (following EDPB) holds that employer–employee power imbalance generally invalidates consent. Basis must be Art 6(1)(b) (employment contract) and/or 6(1)(f) (legitimate interest, documented via an LIA).
  2. **Co-determination (MBL 1976:580 §11):** introducing a system that evaluates employees / allocates assignments is typically a "significant change" requiring negotiation with the union before deployment, if the Company is bound by a collective agreement. HR/legal task, not code.

---

## 2. Analysis by expert lens

### 2.1 Data-protection (GDPR) lawyer

- **Roles:** The Company = **controller**. Processors: **Anthropic** (LLM), **AWS** (hosting eu-north-1; SES if notification emails are enabled). The TA laptop running the cockpit is *internal* processing by the controller, not a separate processor — but it extends the security perimeter to laptops.
- **Legal basis (fixes the request's framing):** we do **not** need consent, and should not ask for it — we need **transparency**. Basis: Art 6(1)(b) for profile/CV processing needed to staff consultants (core of the employment relationship), Art 6(1)(f) with a written Legitimate Interest Assessment for the matching/scoring/learning loop. The login-page text is an *information* notice (Art 13), not a consent gate.
- **Special-category risk:** CVs can incidentally contain Art 9 data (photo, union role, health hints). Mitigations: the extractor writes facts-only against a schema (good); add an upload-page instruction not to include sensitive data; keep raw uploads only briefly (see retention).
- **Art 22:** no solely-automated decision with legal/significant effect exists — TA curates, consultant verdicts, humans submit to clients. **Keep this true by design** (proposed as a new CLAUDE.md invariant).
- **Gaps → obligations:** Art 13 notice (none exists), Art 15/20 export (none), Art 17 erasure (none), Art 5(1)(e) storage limitation (uploads/verdicts/share-HTML unlimited), Art 30 ROPA (none), Art 35 DPIA — **likely mandatory**: IMY's Art 35(4) list covers systematic evaluation of employees with innovative tech; AI-assisted matching of employees to assignments fits.
- **Third-party PII in job ads:** rely on Art 14(5)(b) (disproportionate effort) — defensible because data is professional-context contact info, retention is short (job-store purge with 7-day grace exists), and it's never used to profile those people. Document this in the ROPA; tighten the email cleaner.

### 2.2 Swedish employment counsel

- Verdict free-text + score snapshots are **employee-evaluation records**: cover them explicitly in the notice and the LIA, apply a retention limit, and include them in export/erasure.
- **MBL §11**: brief and, if applicable, negotiate with the union **before** wider rollout. The AI Act will independently require informing affected workers before use of high-risk workplace AI (Art 26(7)) — do the worker-information pack once, satisfying both.
- Erasure of an *active employee's* profile conflicts with the employment relationship; erasure is an **offboarding action** (admin-triggered), plus honoring objections to specific processing (e.g., opting out of matching → `actively_looking=false` already exists).

### 2.3 AI Act specialist

- **Is CV Adapter high-risk (Annex III 4)?** Borderline, and worth a written assessment rather than a guess:
  - The **deterministic scorer** (`shared/scoring.py`) executes human-defined rules — under the Commission's guidelines on the AI-system definition, likely **not an AI system** by itself.
  - The **learning loop** (Opus proposes config changes, human approves, delta guards cap shifts) and **CV generation** are AI systems. The matching function *influences task allocation based on personal traits* → Annex III 4(b) territory; but human oversight is structural (TA triage, consultant verdicts, delta guard, pending-config approval), which supports a documented lower-risk conclusion under Art 6(3) — **except** the profiling caveat must be analysed honestly (per-consultant configs derived from verdict behaviour look like profiling).
  - **Timeline:** whatever the conclusion, Annex III obligations now bite **2027-12-02**, not next month. The cheap move: write the assessment now (Session 5), design as-if-deployer-obligations (logging, human oversight, worker information) since we mostly have them already.
- **Applicable today:** Art 4 AI literacy (train TAs/admins using the tool — a short internal doc suffices at this scale).

### 2.4 Security engineer (Art 32)

Solid: bcrypt, hash-only sessions/tokens, httponly+samesite cookie, localhost-only cockpit, subprocess isolation.
Gaps, in priority order:
1. **TLS end-to-end**: HTTPS listener + cert on the ALB (infra repo), then `SESSION_COOKIE_SECURE=true` everywhere, HSTS.
2. **CORS `["*"]`** → config-driven allowlist.
3. **PII in logs**: remove/flag-gate the 400-char profile/CV previews; drop usernames from LLM labels (use opaque IDs).
4. **Non-expiring API tokens** → add expiry + rotation + last-used tracking.
5. **Laptop perimeter**: BitLocker required (document in `docs/cockpit/ADMIN-SETUP.md`); `configs_cache.json` holds full profiles cleartext — acceptable *only* with disk encryption; note the no-TTL staleness issue.
6. **Backups**: define what backups of the `./data` volume / Postgres exist and how erasure propagates to them (policy statement in the notice: "erased from backups within N days by rotation").

---

## 3. Locked decisions

1. **Legal basis = contract + legitimate interest, never consent.** The login-page text is an information notice with a link to the full privacy page — no checkbox, no consent record.
2. **Erasure is admin-triggered** (offboarding / on request), not consultant-self-serve. It is a hard delete across Postgres + filesystem, and it must delete verdicts *before* attempting anything that the verdict-block guard (`job_store/service.py:273-284`) would stop.
3. **Export is a ZIP** of machine-readable JSON/Markdown (satisfies both Art 15 access and Art 20 portability in one artefact).
4. **Anthropic stays.** DPA auto-incorporated + DPF + 7-day retention + no-training is a sound processor setup; we document it and disclose it. Revisit EU-region inference (Bedrock/Vertex) only if counsel demands data-in-EU-only for LLM calls.
5. **Human-in-the-loop becomes a design invariant** (CLAUDE.md #9): scores and LLM outputs never trigger an automated outward action (submission to a client, config apply, CV send) without a human step. This keeps us out of GDPR Art 22 and strengthens the AI Act Art 6(3) position.
6. **Retention defaults** (all configurable in `backend/config.py`): original CV uploads **30 days** after successful import; verdicts **24 months**; expired share-token rows hard-deleted; job store keeps its existing expiry+7-day purge.

---

## 5. Outside this plan (people, not code)

- Counsel review of notice/DPIA/LIA; decision on whether a DPO is required (Art 37 — likely not at the Company's scale, but document the decision).
- Sign/record acceptance of the Anthropic Commercial Terms DPA under the account actually holding the API key; same for the AWS DPA.
- MBL §11 union information/negotiation before broad rollout, if collectively bound.
- AI-literacy briefing for TAs/admins (Art 4 — already applicable).
