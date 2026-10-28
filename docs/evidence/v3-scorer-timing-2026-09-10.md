# Deterministic scorer timing — 2026-09-10

**Question:** V3-P-008 decision 2 left the eager score fan-out provisional and
asked for the scorer to be timed before sizing it. Is `shared/scoring.py` fast
enough that filling a Jobs page on the request is acceptable, or must scores be
prepared in the background first?

**Answer: fast enough by three orders of magnitude.** 30 realistic postings
score in **~5.7 ms**. The eager fan-out is not needed, and V3-P-008 was amended
accordingly.

## Method

`score_job(job, profile, config)` timed in a loop, median of 7 repeats, on the
fabricated `_PROFILE` / `_CONFIG` fixture copied out of `tests/test_scoring.py`
(no `data/` read, no real profile). Because the unit-test job snippet is far
shorter than a real posting, a padded variant adds plausible recruitment
boilerplate to ~4.2 KB, which is representative of an actual advert. Both the
per-call path and the bulk `build_evidence_index` path were measured.

WSL Ubuntu, project `.venv`, CPython 3.12.3, warm process. Fixture scored
`overall_score=76.0`, `recommended_status=keep`, confirming a full scoring pass
rather than an early return.

## Result

| Jobs | Posting size | Evidence index | Median | Per job |
| --- | --- | --- | --- | --- |
| 20 | 842 B | per call | 1.62 ms | 0.081 ms |
| 20 | 842 B | shared | 1.56 ms | 0.078 ms |
| 20 | 4.2 KB | per call | 3.84 ms | 0.192 ms |
| 20 | 4.2 KB | shared | 3.68 ms | 0.184 ms |
| 30 | 842 B | per call | 2.41 ms | 0.080 ms |
| 30 | 842 B | shared | 2.25 ms | 0.075 ms |
| 30 | 4.2 KB | per call | 5.68 ms | 0.189 ms |
| 30 | 4.2 KB | shared | 5.74 ms | 0.191 ms |

Cost scales with posting length, not with profile handling: the bulk evidence
index saves 2–4% at this size and is not worth a code path of its own for a
single Consultant's page.

## What this does and does not measure

Measured: `score_job` alone, on an in-memory profile and config.

**Not measured:** `load_profile` and `load_config` — a disk read plus YAML
parse. Those are paid **once per Consultant**, not per Job, and the current
on-demand path already pays them on every Jobs request, so they are not new
cost introduced by lazy fill. They are, however, the likely dominant term now
that scoring is ~0.2 ms, which is worth knowing if the page ever feels slow:
the fix would be caching the parsed profile, not precomputing scores.

Also not measured: Postgres round trips for reading and upserting rows. A
single round trip plausibly costs more than scoring 30 pairs.

## Consequence

- V3-P-008 amended: the Job-creation fan-out is dropped and score preparation
  is purely lazy. S1 therefore needs no durable workflow handler for scoring
  and no registry change.
- Persistence is retained, but its justification changes from *latency* to
  *SQL-side sorting and minimum-score filtering* over stored rows.
- If the page is ever reported slow, profile/config parse is the first suspect.

Reproduce with the throwaway script recorded in the 2026-09-10 planning
session; it imports the fixture from `tests/test_scoring.py` and is not part of
the suite (it asserts nothing and is a measurement, not a gate).
