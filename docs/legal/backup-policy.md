# Backup & erasure policy statement

**Status:** policy decision, Session 4 of `docs/legal/decisions.md` (Art
32). This is the number the app's own docs (`html/privacy.html` §6/§7) commit
to; it also feeds Session 5's `ropa.md`. **Verified against the actual
infra-repo backup configuration** — see the note at the bottom.

## The policy

- Database backups (automated snapshots) are retained for **at most 7 days**
  and then age out through normal rotation — no indefinite backup retention.
- When a consultant is erased (`DELETE /api/privacy/consultants/{username}`,
  Session 2), no separate backup-scrubbing step runs. The erased record stops
  existing in *new* backups immediately (it's gone from the live database);
  it is fully gone from *all* backups, including older ones taken before the
  erasure, within the 7-day retention window as those older backups roll off.
- This is deliberately the same shape as GDPR guidance and common practice
  elsewhere (e.g. AWS RDS's own backup-retention model): "erasure propagates
  to backups within N days by rotation," not "we hunt through every backup
  and edit it." Manually editing individual backup snapshots is out of scope
  — it's operationally impractical and backups are not queried directly in
  normal operation.
- The `./data` filesystem volume (profiles, uploads, job store, TA config)
  backs up on the same cadence and is subject to the same 7-day statement —
  whatever mechanism backs up the Docker volume / EFS mount in a given
  environment should be configured to this same retention ceiling.

## Where this must be enforced

This repo has no backup infrastructure of its own — Postgres runs as a plain
Docker Compose service locally, and the real cloud database (RDS or
equivalent) and its backup/snapshot settings live in the separate **infra
repo** (private, per `DEPLOY.md`), same as the ACM/HTTPS
item tracked in Session 4. **This document states the policy the infra repo's
backup-retention setting must match — it does not itself configure or verify
that setting.**

**Confirmed:** the database's automated-backup retention window is 7 days,
matching this policy. Verified directly against the infra repo's Terraform
(the database module's `backup_retention_period`, not overridden by the
environment), during the V2 cloud-reset readiness check. No infra change was
needed — the
policy figure was corrected to match the existing Terraform default instead
(P-019 follow-up).
