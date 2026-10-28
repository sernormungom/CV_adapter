# Repository guidance for AI-assisted work

CV Adapter is a FastAPI modular monolith on Postgres. Start with
`docs/product.md`, which covers what the product is, its pilot posture,
deliberate limitations, prerequisites for real data, and open items. Then read
`docs/testing.md` for the test gates.

This is a disposable-data, downtime-tolerant pre-production pilot. Do not propose
data migration, compatibility layers, freezes, recovery programmes, restore
testing, dual writes or zero-downtime rollout unless the product owner changes
that posture.

The pilot posture does not relax any of these:

- authentication and authorization
- secret and capability-token safety
- deletion of transient sources
- idempotency of long-running work
- CI

Destructive AWS work always needs separate, explicit approval that names the
exact targets.

Keep the application a modular monolith. Reuse the existing CV and scoring
components. Change one user-visible behavior at a time, and update the
maintainer documentation that describes that behavior in the same change.

## Subsystem contracts

Before you change a subsystem, read its **Contract** and **Guards**. The routing
index at `docs/architecture/areas/README.md` points you to the right file. These
files are provider-neutral and describe current behavior, and every constraint
you need to implement safely is in Guards.

Load unrelated areas only when the work actually reaches them. When you change
behavior, update the area file that describes it.

## Product rules that cut across areas

- No score or LLM output may submit a consultant, choose Apply/Reject,
  publish or send a CV, or delete or anonymize data without a human command.
- All access decisions live in `backend/authz/policy.py`.
- Change LLM tuning behavior through the prompt or schema
  (`project_context/*.schema.md`), never through hardcoded rule code.

`CLAUDE.md` keeps the numbered invariants that source comments cite.
