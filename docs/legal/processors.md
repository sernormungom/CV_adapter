# Sub-processor Register

**Not legal advice** — this is an engineering-oriented compliance artefact, produced for qualified Swedish counsel to review before being relied on as a complete sub-processor register.

**Status:** Session 5 governance artefact of `docs/legal/decisions.md`, describing the system **as remediated by Sessions 1–4**. This document records what the *code* actually does and what is *externally verified* about each processor's contractual posture (`docs/legal/decisions.md` §1.2); it explicitly does **not** — and cannot — verify who accepted a DPA on the Company's behalf or when. See "Action items" at the end.

**Controller:** the Company (the employer; name withheld in this portfolio copy).

---

## 1. Anthropic PBC — Claude API (primary LLM processor)

| Field | Detail |
| --- | --- |
| **Service used** | Claude API — CV-text extraction, structured-fact extraction, CV content generation, chat-based profile editing, matching-config bootstrap from profile data. |
| **Call sites in code** | `backend/llm.py` (the single wrapper around the Anthropic SDK) — invoked from `backend/cv_importer/experience_extractor.py`, `backend/cv_importer/profile_merger.py`, `backend/cv_pipeline/cv_content_generator.py`, `backend/cv_assistant/chat_engine.py`, `backend/cv_assistant/profile_patcher.py`, `backend/opportunity_pipeline/learning_module/config_bootstrapper.py`. |
| **Data sent** | Raw CV text (first ~10k chars), full profile YAML, job descriptions, CV editor field snapshots. Matching-config bootstrap uses only profile-derived content. See `docs/legal/ropa.md` §1–§2 for the current purpose-by-purpose breakdown; §3 records the retired evaluation activity. |
| **Location / transfer mechanism** | Anthropic PBC, United States. **Active EU-US Data Privacy Framework (DPF) certification** + **Standard Contractual Clauses (Module 2, controller→processor)** in place as a fallback/supplementary mechanism. Per `docs/legal/decisions.md` §1.2 (verified 2026-07-07). |
| **DPA status** | GDPR Art 28 Data Processing Addendum is **auto-incorporated into Anthropic's Commercial Terms** (the version effective 2026-01-01) — i.e., using the Claude API under those Commercial Terms means the DPA terms already apply; there is no separate document to countersign, but there is a specific version/date to have on file. |
| **Sub-processing / training** | **No training on API inputs or outputs.** |
| **Data retention at processor** | API request/response logs retained **7 days** (Anthropic policy since 2025-09-14). |
| **Assessment** | Sound processor posture for this use case: valid transfer mechanism (DPF certification is the primary basis, SCCs as a documented fallback), contractually governed via the auto-incorporated DPA, no training exposure, short log retention. `docs/legal/decisions.md` §3 decision #4 records the decision to keep Anthropic and only revisit EU-region inference (e.g., Bedrock/Vertex) if counsel specifically requires data-in-EU-only for LLM calls. |

## 2. AWS — hosting (compute, database)

| Field | Detail |
| --- | --- |
| **Service used** | ECS Fargate (application hosting) + the production Postgres database (RDS or equivalent), per `DEPLOY.md`. |
| **Data processed** | All personal data described in `docs/legal/ropa.md` passes through or is stored on AWS infrastructure as the hosting layer — this is infrastructure-level processing (storage/compute), not content inspection. |
| **Location** | **eu-north-1 (Stockholm)** — data at rest stays in the EU; no international transfer issue for the hosting layer itself. |
| **DPA status** | AWS's standard GDPR Data Processing Addendum (part of the AWS Service Terms / accepted via the AWS account) governs this relationship. **Not independently verified from this repository** — see Action items. |
| **Assessment** | Region choice (Stockholm) is the right call for EU data residency and is already documented in `DEPLOY.md`/`docs/architecture`. The open item is purely evidentiary: confirming DPA acceptance is on file for the account actually running this workload. |

## 3. AWS SES — outbound notification email (conditional sub-processor)

| Field | Detail |
| --- | --- |
| **Service used** | Simple Email Service, for best-effort outbound email notifications (e.g., a TA's `cv_viewed` inbox alert delivered by email in addition to the in-app notification). |
| **Code status** | **Wired but disabled by default.** `backend/notifications/channels.py::send_email` calls `boto3.client("ses", region_name=config.AWS_REGION).send_email(...)`, gated by `email_available()`, which requires `NOTIFICATIONS_EMAIL_ENABLED=true` **and** `AWS_REGION` **and** `SES_SENDER_EMAIL` all being set (`backend/config.py`). All three default to off/unset, so in the default configuration this code path never calls AWS and only logs what it *would* send (`logger.info("[notify:email:log] ...")`). |
| **Data that would be sent if enabled** | Recipient email address (a TA's, e.g. per `cv_publish/service.py::record_view`'s `cv_viewed` notification), a notification subject/body that may reference a consultant's name and the job/CV in question. |
| **Location** | Determined by `AWS_REGION` at the time it's enabled — should be set to `eu-north-1` to match the hosting region and avoid introducing an unplanned transfer. **This is a configuration choice to be made when/if the feature is turned on, not something the code enforces today.** |
| **DPA status** | Same AWS DPA as §2 covers SES as an AWS service, once actually in use. |
| **Assessment / action if enabled** | Before setting `NOTIFICATIONS_EMAIL_ENABLED=true` in any environment: (a) set `AWS_REGION=eu-north-1` (or confirm whatever region is chosen and document it here), (b) verify the `SES_SENDER_EMAIL` sending identity, (c) update `docs/legal/ropa.md` §1 recipients list and `html/privacy.html` to name SES as an active recipient (it is currently only implicitly covered by the general "AWS" processor entry, which is accurate for hosting but should be made explicit once email is live), (d) update this table's "Code status" row. |

## 4. AWS SNS — outbound SMS notification (conditional sub-processor, unused)

| Field | Detail |
| --- | --- |
| **Service used** | Simple Notification Service, for best-effort outbound SMS. |
| **Code status** | Same gating pattern as SES: `backend/notifications/channels.py::send_sms`, gated by `sms_available()` (`NOTIFICATIONS_SMS_ENABLED` + `AWS_REGION`, both requiring explicit config), off by default. No phone-number field currently appears to be collected/used elsewhere in the data model surveyed for `docs/legal/ropa.md` — treat this as **effectively unused today**, listed here only because the code path exists and is reachable if the flag is ever flipped. |
| **Assessment / action if enabled** | Same as SES §3: confirm region, confirm what phone-number data would actually be sourced from, and update `docs/legal/ropa.md` and the privacy notice before turning this on. |

## 5. Sub-processors not currently in use

No other third-party processors were found wired into the code surveyed for this document (no analytics SDKs, no third-party job boards accessed via API rather than scraping, no external CV-parsing service beyond the in-house `cv_parser.py` + Anthropic extraction). If a future feature adds one (e.g., a transactional-email provider other than SES, a third-party OCR service), add it here before shipping, per the same table shape.

---

## 6. Action items (cannot be verified or completed from this repository)

This repo can verify *what the code does* (which processor is called, with what data, gated by what flag) but cannot verify *who accepted a DPA on the Company's behalf, when, or where the record of that acceptance lives*. The following must be completed and recorded by whoever holds the relevant vendor accounts:

1. **Anthropic:** confirm which Company account/individual holds the Anthropic Commercial Terms acceptance covering the `ANTHROPIC_API_KEY` in production use, and record the acceptance date + a link/reference to the accepted terms version (effective 2026-01-01) here or in whatever internal vendor-contract register the Company maintains.
2. **AWS:** confirm the AWS DPA is accepted under the account running the production ECS/RDS workload, and record that evidence similarly.
3. **If/when SES or SNS is enabled:** confirm the sending region and identity, and update both this document and `html/privacy.html` to name the channel as an active recipient — do not treat "AWS" as sufficiently specific once a customer-facing communication channel is live through it.
4. **General:** consider whether the Company's own vendor/contract management process already has a canonical place for DPA-acceptance evidence — if so, this document should point to it rather than duplicate it, to avoid the two going out of sync.
