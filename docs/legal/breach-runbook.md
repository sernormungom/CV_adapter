# Personal Data Breach Runbook (Art 33/34 GDPR)

**Not legal advice** — this is an engineering-oriented compliance artefact, produced for qualified Swedish counsel to review before being relied on as a complete breach-response procedure.

**Status:** Session 5 governance artefact of `docs/legal/decisions.md`, describing the system **as remediated by Sessions 1–4**. This runbook was originally written against that system's architecture (cloud backend + TA-laptop cockpit + Anthropic/AWS processors). **V2 Step 1 (ADR-010) retired the TA Cockpit** — Scenario A below no longer applies to the current system; it is retained as historical record. The remaining architecture (cloud backend + Anthropic/AWS processors) is otherwise unchanged.

---

## 1. What counts as "becoming aware" (starts the 72-hour clock)

The Art 33(1) 72-hour clock to notify IMY starts when the Company (the controller) has a **reasonable degree of certainty** that a personal-data breach occurred — not merely a suspicion under investigation, but not waiting for full certainty either. In practice, for this system, "aware" is triggered by any of:

- Evidence of unauthorized access to the production database, the `data/` volume, or an admin/TA account.
- Evidence that an API bearer token or the `ANTHROPIC_API_KEY` was used by someone other than its intended holder.
- A report from Anthropic or AWS of a security incident affecting the Company's account or data.
- Discovery, during code review or an audit, that a defect (e.g., an authorization bug like the ones caught and fixed during Session 2's review — see `docs/legal/decisions.md`'s Session 2 deviations log) *may have* allowed unauthorized access to personal data in the past, even if there's no direct evidence it was exploited.

Document the exact time "awareness" was established for every incident — the 72-hour clock's start point is itself something IMY can ask about.

## 2. Immediate response checklist (any incident)

1. **Contain.** Revoke/rotate the specific credential involved (see per-scenario steps below) before doing anything else — containment is not optional pending investigation.
2. **Assess scope.** What data, how many data subjects (approximately), what categories (ordinary vs the employee-evaluation-flavoured data vs any incidental Art 9-adjacent data that might have ended up in a CV/comment despite the privacy notice's request not to include it). This category used to name the `verdicts` table (dropped with no writer left, V3-P-008 decision 5, V3 packet S2) and a Job Review's free-text Reject reason, `job_reviews.reason` (removed, V3 packet S20); what remains is a Reject marker — decision and timestamp, no free text.
3. **Document — always, regardless of outcome.** Art 33(5) requires the Company to keep an internal record of *every* personal-data breach, including ones ultimately assessed as not requiring notification. **This codebase does not currently have a breach register** — the `AuditLog` table (`backend/db/models/audit.py`) records privacy *actions* (export/erasure), not security *incidents*, and is not a substitute. Action item: maintain a simple incident log (a dated entry per incident with the facts below) until/unless a dedicated system exists — this can be as simple as an append-only internal doc, but it must exist and be kept.
4. **Decide on IMY notification (Art 33)** using the decision tree in §3.
5. **Decide on data-subject notification (Art 34)** using the decision tree in §4.
6. **Remediate** the root cause, not just the immediate exposure.

## 3. Decision tree — IMY notification (Art 33)

```
Is this a breach of confidentiality, integrity, or availability of personal data?
  NO  -> not a personal-data breach; document and stop.
  YES -> continue.

Is it unlikely to result in a risk to the rights and freedoms of natural persons?
  (Consider: was the data encrypted/inaccessible to the person who obtained it?
   Was the exposure very limited in scope? Was the data already public?)
  YES -> notification to IMY not required, but MUST still be documented
         internally (Art 33(5) register) with the reasoning for this conclusion.
  NO / UNSURE -> notify IMY within 72 hours of awareness. If full details
         aren't available within 72 hours, notify with what's known and
         supplement in phases (Art 33(4) explicitly allows this — do not
         delay the initial notification waiting for a complete picture).
```

**IMY notification must include (Art 33(3)):** nature of the breach; categories and approximate number of data subjects and records affected; the contact point for more information (name a specific role/person internally — with no DPO currently appointed, this defaults to whoever owns the Company's AWS/Anthropic accounts or a designated admin, pending the open DPO question in `docs/legal/decisions.md` §5); likely consequences; measures taken or proposed to address the breach and mitigate its effects. Submit via IMY's breach-reporting channel (imy.se).

## 4. Decision tree — data-subject notification (Art 34)

```
Does Art 33 apply (see §3)?
  NO  -> Art 34 does not apply either; stop.
  YES -> continue.

Is the breach LIKELY to result in a HIGH risk to the rights and freedoms
of the affected individuals?
  (Higher risk factors specific to this system: exposure of Reject
   markers / employee-evaluation data; exposure of a consultant's full
   profile+CV to someone outside the organization; large-scale exposure
   across many consultants at once, e.g. a compromised database rather
   than one laptop.)
  NO  -> Art 34 notification not required; document why.
  YES -> continue.

Was the affected data rendered unintelligible to any unauthorized party
(e.g., strong encryption, and the key was not also compromised)?
  YES -> Art 34(3)(a) exemption may apply (notification not required if the
         controller has implemented measures that make the data unintelligible
         to anyone not authorized to access it) -> document this basis
         explicitly if relied on.
  NO  -> continue.

Have effective measures since been taken that ensure the high risk is no
longer likely to materialize (e.g., all affected credentials rotated before
any misuse could plausibly have occurred)?
  YES -> Art 34(3)(b) exemption may apply -> document the measures and timing.
  NO  -> Notify affected data subjects directly, in clear and plain language,
         describing the nature of the breach and at minimum the same content
         as the Art 33(3) IMY notification (contact point, likely
         consequences, measures taken/proposed).
```

## 5. Scenario A — Lost or stolen TA laptop — RETIRED (V2 Step 1, ADR-010)

**No longer applicable.** The TA Cockpit, its local cache (`data/ta_cockpit/`), and the laptop-persisted API bearer token it used were all removed in V2 Step 1. There is no longer a TA-laptop-resident copy of consultant profile/matching-config data, so a lost/stolen TA laptop is no longer a personal-data exposure scenario by itself (it may still warrant revoking that person's own cloud session/password per Step 2.4 below, if their credentials were saved in a browser on the device — that containment step remains relevant and is repeated here for reference).

*Original scenario (V1, resolved), retained for historical record:* What was exposed: `configs_cache.json` (full profile + matching config in cleartext for every actively-looking consultant assigned to that TA), the SQLite triage pool (job records + triage state), and the API bearer token (persisted via `setx`). Response was to establish the laptop's BitLocker/lock state (determining Art 33/34 likelihood), then immediately revoke the laptop's API token (`DELETE /api/auth/tokens/{id}` or the admin path), confirm no other laptop shared that token, identify the affected consultant list from the TA's `actively_looking` set, and force-revoke the TA's live cloud sessions if their credentials might also have been exposed (`POST /api/auth/admin/users/{user_id}/revoke-sessions` — do this rather than only a password reset, since a reset alone does not invalidate an already-open session cookie).

## 6. Scenario B — Compromised Anthropic API key

**What's exposed / at risk:** the `ANTHROPIC_API_KEY` environment variable used by `backend/llm.py`. A holder of this key can make Claude API calls billed to and attributed to the Company's account — this is primarily a **billing/abuse risk**, not a direct route to the Company's stored personal data (an attacker with only the key cannot read Postgres, the `data/` filesystem, or past request history beyond Anthropic's own 7-day log window, which the Company does not have special access to either).

**Step 1 — immediate containment:**
1. Rotate the key in the Anthropic console immediately; revoke the old key.
2. Redeploy/restart the application with the new key (`ANTHROPIC_API_KEY` is read at call time via `backend/config.py`, not cached at process start in a way that would need more than a restart).
3. Check Anthropic's usage/billing dashboard for anomalous call volume or patterns in the days since the key may have leaked — do this promptly, since Anthropic's own API log retention is only 7 days, so evidence of misuse ages out fast.

**Step 2 — assess whether this is actually a personal-data breach, not just a cost/abuse incident.** Ask:
- **How did the key leak?** If it leaked via a compromised server, repo, or CI secret store, treat this as a **broader incident** — other secrets (database credentials, session-signing material, other API tokens) may have leaked via the same path, and the investigation must cover those too, not just the Anthropic key in isolation.
- **Did the attacker gain the ability to *read* any request/response content**, as opposed to only being able to *send new requests* using the key? A leaked API key alone typically grants send-capability, not read-access to the Company's own past request logs (those live in the Company's own systems, e.g., CloudWatch, not retrievable via the API key itself) — but if the key leaked *together with* log/CloudWatch access, then yes, personal data sent to Anthropic (CV text, profile YAML) would have been exposed to the attacker via those logs, not via Anthropic.
- **Conclusion drives the path:** if the leak is confined to unauthorized *usage* of the key with no evidence of data exposure, this may not meet the Art 33 threshold at all (no breach of confidentiality/integrity/availability of personal data) — document that conclusion and the reasoning. If the leak came bundled with broader access (logs, source, infra), run the full §3/§4 decision tree against whatever personal data that broader access actually exposed.

**Step 3 — document and decide** per §3/§4 based on Step 2's conclusion.

## 7. Roles

No DPO is currently appointed (`docs/legal/decisions.md` §5 flags the Art 37 decision as open). Until that's resolved, this runbook assumes:

- **Breach lead:** whoever holds the production AWS/Anthropic accounts, or a designated admin — name this explicitly in the Company's internal on-call/incident process (outside this repo's scope to assign).
- **IMY notification submitter:** the breach lead, or whoever HR/legal designates as the external-facing contact.
- **Internal breach register owner:** same as breach lead, until a dedicated tool exists (see §2 step 3's action item).

## 8. After-action

For every incident, regardless of whether IMY/data-subject notification was required: update this runbook if the incident revealed a gap (e.g., a missing revocation path, a scenario not anticipated here), and confirm the underlying vulnerability (not just the immediate exposure) was fixed — not merely the immediate exposure.
