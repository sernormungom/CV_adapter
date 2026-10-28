# Legitimate Interest Assessment — Matching, Scoring & the Learning Loop (Art 6(1)(f) GDPR)

**Not legal advice** — this is an engineering-oriented compliance artefact, produced for qualified Swedish counsel to review before being relied on as a complete LIA.

**Status:** V1 balancing assessment; V2 amendment added 2026-08-21; V3
correction added 2026-09-10. Qualified privacy/employment review is required
before relying on any of these conclusions.

**V3 correction (2026-09-10):** the "learning loop" this LIA is titled around
and weighs throughout — a Learning Module adapting the matching config from a
consultant's accumulated accept/reject verdicts, bounded by a delta guard and
gated by human pending-config approval — never had a live writer and is now
deleted outright, not merely deferred (V3-P-008
decision 5, V3 packet S2). Every claim below that leans on delta guards,
verdict-driven adaptation, or pending-config approval as the mitigation for a
risk is evidence for a mechanism that no longer exists. The processing that
remains — deterministic scoring against a matching config that is itself
LLM-generated from `profile.md` alone, regenerated only on an explicit human
"Update match configurator" action — still needs an Art 6(1)(f) balance, but
it is a smaller and different one: there is no behavioural-adaptation
mechanism to weigh a filter-bubble or drift risk against. Treat Parts 2–3
below as V1/V2 evidence for the deleted mechanism except where a note in this
file says otherwise, and re-run this LIA properly once the surviving,
profile-only processing needs a documented balance of its own.

**Scope note:** this LIA covers only the **matching / scoring** processing — collecting opportunities and scoring them against a consultant's profile and matching config. It does **not** cover the core CV-import/profile/CV-generation processing, which rests on Art 6(1)(b) (necessary to perform the consultant engagement) and needs no LIA. See `docs/legal/ropa.md` §1 vs §2 for where the line is drawn.

> **V2 material change:** V2 removes fixed curated batches/pending config approval,
> stores versioned Consultant–Job projections, and auto-applies an LLM proposal
> after schema/delta-guard validation. The V1 conclusion below explicitly said that
> auto-apply required a new balancing test; that trigger has occurred. V2 adds a
> different protection: matching changes ordering only and can never reject,
> discard, hide, or make a Job ineligible. A person still chooses Apply/Reject and
> every outward action. Invalid/out-of-guard changes leave the prior config active,
> inputs/results are versioned and explainable, stale/error states are visible, and
> raw Reject reasons are minimized after last use. These controls support a revised
> Art 6(1)(f) balance but do not automatically make it favourable. Automation bias,
> unequal ordering, profiling, worker expectations/information, practical objection
> handling, and monitoring/rollback require documented qualified review before V2
> production use. The remaining text is retained as V1 evidence where it mentions
> batches, Maybe, or human config confirmation; as of V2 Slice 1 that code is
> deleted, and job records live in the Postgres `jobs` table rather than
> `data/job_store/jobs/`.

---

## Part 1 — Purpose test

**What is the interest, and whose is it?**

- **The Company's interest:** more efficient staffing — surfacing relevant assignments to consultants faster and with less manual TA effort than fully manual matching, and improving match quality over time by learning from what consultants actually accept or reject.
- **The consultant's own interest, materially aligned with the Company's:** being shown opportunities that fit their actual skills and preferences, rather than an undifferentiated firehose of every job a TA collects. A better-tuned match reduces the number of irrelevant opportunities a consultant has to review.

**Is the interest real, specific, and present (not speculative or future)?** Yes. The matching pipeline is live functionality serving current consultants, not a hypothetical future feature. The interest is specific: efficient, relevant assignment-matching for staffing consultants — not a general-purpose "we might want this data for something later" interest.

**Is it lawful?** Yes — recruitment/staffing efficiency and internal process improvement are ordinary, lawful commercial interests, and nothing about scoring job-fit or learning from accept/reject history is itself unlawful conduct.

## Part 2 — Necessity test

**Is this processing necessary to achieve the interest, or is there a less intrusive way to get there?**

- **The deterministic scorer (`shared/scoring.py`) is intentionally not black-box ML.** It executes a human-authored, per-consultant `matching_config.yaml` (weights, thresholds, term catalogs) — every score is explicable in terms of visible configuration, not a hidden model. This matters for necessity: the system does not process more, or more opaquely, than needed to rank opportunities by fit.
- **Data used is limited to what scoring/matching actually needs:** the consultant's own profile facts and matching config, and the job's own intrinsic text/fields (`data/job_store/jobs/`, no scores stored per ADR-007). No behavioural profiling data beyond the consultant's own verdict history on jobs *they* were shown is used.
- **The learning loop is necessary because a static config drifts.** A one-time, never-updated scoring config would either over-match (wasting the consultant's and the TA's time on irrelevant opportunities) or under-match (missing good-fit assignments) as a consultant's stated and revealed preferences diverge over time. Learning from the consultant's own explicit verdicts — rather than, say, silently inferring preferences from browsing behaviour or other proxies — is the least intrusive way to keep the config current: it uses only data the consultant already deliberately provided (a yes/no/maybe on a specific opportunity), not inferred or purchased signals.
- **Could a manual, non-learning alternative achieve the same purpose with less processing?** A pure-manual model (TA hand-matches every consultant to every opportunity, no scoring at all) was considered and is, in effect, still partially in place — the TA curates the batch regardless of score. But removing scoring/learning entirely would not reduce the personal data processed (the TA would still need to read the same profile and job data to hand-match) while making the process slower and less consistent — it fails the "less intrusive AND equally effective" test, since it isn't actually less intrusive on the data, just less effective.
- **Conclusion:** the processing is necessary — no materially less intrusive method achieves the same purpose with the same effectiveness.

## Part 3 — Balancing test

This is the substantive test: do the consultant's interests, fundamental rights, and reasonable expectations override the Company's legitimate interest?

### Reasonable expectations

A consultant engaged by a staffing company reasonably expects to be considered for, and matched to, assignments as the basic service the Company provides — that expectation is disclosed explicitly in `html/privacy.html` §3/§4 (purposes; legal basis, including the Art 6(1)(f) basis for "matching, scoring, and the learning loop"), and again at CV-upload time (`html/cv-importer.html`'s upload-time notice). Nothing about being scored against job postings is a surprising or hidden use of the data — it is the visible, named function of the tool the consultant is using.

### Impact on the consultant

- **Positive/neutral in the ordinary case:** better-targeted opportunities, less noise, faster staffing — this is the intended and typical effect.
- **Potential negative impact, and why it's bounded:**
  - *Being scored/evaluated at all* could feel like an evaluation of the consultant rather than of job-fit. Mitigation: the scorer evaluates **job-to-profile fit**, not the consultant's general competence or performance — inputs are the consultant's own stated skills/experience and a job's requirements, not a productivity or performance metric.
  - *The learning loop adapting to a consultant's own verdicts* could, if unbounded, drift into an increasingly narrow or skewed set of surfaced opportunities based on a small, noisy sample of past decisions. Mitigation: `delta_guard.py` (CLAUDE.md invariant #4) hard-caps how much any single learning cycle can move — ±0.30 per scoring-weight dimension, ±10 per threshold, more than 5 term removals from a single tier triggers a hold rather than an apply. This is a direct, code-enforced bound on the magnitude of impact any one learning cycle can have on what a consultant sees next.
  - *A wrong or biased config change* reaching a consultant's live opportunity feed without anyone checking it. Mitigation: **human-in-the-loop is the key balancing factor** (see below) — nothing the learning loop proposes takes effect without a human confirming it.

### Human-in-the-loop as the decisive balancing factor

This is the strongest fact tipping the balance toward the legitimate interest being proportionate to its impact, and is worth stating plainly since it is the crux of this LIA:

1. **The TA curates the batch.** Even a high score does not automatically put a job in front of a consultant without the TA's collection/curation step (`batch_assembler.py`) — nothing scored ever reaches a client or a consultant fully automatically.
2. **The consultant submits their own verdict.** No opportunity is acted on (applied to, submitted to a client) without the consultant explicitly saying yes.
3. **Delta guards cap the learning module's per-cycle changes**, and — critically — **a proposed config change is never auto-applied.** It is written to `matching_config.pending.yaml` and requires a human (the consultant, via the dashboard's confirm/discard flow backed by `learning_module/routes.py::get_pending_config`) to accept or reject it before it affects future scoring.

Together these mean there is no point in the pipeline where a score or an LLM output autonomously produces a consequence for the consultant — a human is always the final step. This is now a codified design invariant (**CLAUDE.md #9**, added in this Session 5) specifically to keep this true going forward, not just true today. It is also the reason this processing does not trigger GDPR Art 22 (no solely-automated decision with legal or similarly significant effect) — there is no solely-automated decision in the loop at all.

### The consultant's ability to object / opt out

**Enforced (STAB-1).** A consultant's TA (or an Admin) can mark them not
`actively_looking` via the existing toggle (`POST
/api/ta/consultants/{username}/looking`, `backend/tenancy/routes.py`,
`User.actively_looking`). The one live matching/scoring code path,
`backend/matching/service.py::score_job_for_consultant` (the sole entry point
`GET /api/jobs/{job_id}/score` calls — Slice 1 retired the review-batch
matcher and dashboard), checks that flag before doing anything else: an
opted-out consultant is refused a score (`scored: false`,
`reason: "opted_out"`, `match_score: null`) and their profile is never read
for the attempt. This is the Art 21 opt-out mechanism this section previously
described as removed by the Cockpit's retirement (ADR-010) and open pending a
product decision; the product owner made that decision entering Stabilization,
and this is its implementation. The flag does not affect Job visibility, Apply,
or Reject — CLAUDE.md invariant 7 and P-007 — nor the
Learning Module, which no longer exists to gate at all
(`docs/architecture/areas/shared-and-legacy.md` G1, V3 packet S2).

## Conclusion

Legitimate interest (Art 6(1)(f)) is a valid and proportionate basis for the matching/scoring/learning loop:

- The purpose is real, specific, and lawful (Part 1).
- No less intrusive alternative achieves the same effectiveness (Part 2).
- The balancing test favours the Company's interest given (a) the interest is substantially aligned with the consultant's own, (b) the consultant's reasonable expectations already cover this use (disclosed in the privacy notice), (c) impact is bounded by delta guards, and (d) — decisively — **human-in-the-loop design means no automated output of this loop ever autonomously affects the consultant** (CLAUDE.md invariant #9).

**This conclusion is conditional on the human-in-the-loop invariant holding.** If a future change ever lets a score, an LLM output, or a learning-module proposal trigger an outward action without a human step (e.g., auto-applying config changes, auto-submitting a consultant to a client), this LIA's balancing conclusion no longer holds and must be redone — at that point the processing would look materially more like an automated decision with a significant effect, both weakening the Art 6(1)(f) balance and raising a live Art 22 question. Re-assess this LIA if: the human-in-the-loop invariant is weakened, the delta-guard limits are loosened, or the scope of what the learning loop is allowed to change expands materially.
