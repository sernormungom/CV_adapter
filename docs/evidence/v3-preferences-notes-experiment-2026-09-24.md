# Experiment evidence: can `preferences.notes` replace the structured preference fields?

Date: 2026-09-24. Observer session only: no code or planning state was changed. This is evidence for an owner decision; it does not decide anything.

## Background

- **Deferred question.** V3-P-017 decision 6 and its 2026-09-23 amendment ("S18's ... owner answers", item 4) put off deleting `preferences.availability`, `preferences.work_mode`, `preferences.employment_types`, `preferences.location_preference`, `education.field_of_study` and `education.location`. They wait until an experiment shows whether free text alone (`preferences.notes`, written through SuperCV's Career Goals) gives the matching-config generator enough signal. S18's brief (acceptance 6 and Exclusions) carried the same framing.
- **Follow-up work item.** A "profile schema simplification pass" is an open product item (see [`docs/product.md`](../product.md)).
- **How the generator works** (checked against the code):
  - "Update match configurator" (`html/jobs.html`, `#updateConfigBtn`) sends POST `/api/matching/{username}/config`.
  - That runs `backend/opportunity_pipeline/learning_module/config_bootstrapper.py::bootstrap_config`.
  - Every run regenerates the config from scratch. The LLM gets the whole `profile.md` YAML plus `project_context/matching-config.schema.md` and returns a complete `matching_config.yaml`.
  - The result is validated, then written. The previous config goes to `matching_config.backup.yaml`, and a failed run keeps the previous config.
- **Schema limit that matters here.** `matching-config.schema.md` has nowhere to put availability. `practical_fit` covers only location, work mode and contract type. Contract type has no dedicated field and can only show up as blockers or interest terms.

## Method

- **Profile:** `norberto.munoz`. `preferences` was `{}` before the test, so none of the structured fields under consideration were filled in. Every signal came from `notes` or from other profile facts (home country Sweden, work history in Gothenburg, Spain and Mexico).
- **Baseline:** the June config, `source: learning_module`, produced by the removed learning path. It is not a bootstrap output, so differences unrelated to preferences reflect bootstrap vs. learned tuning, not `notes`.
- **Runs:** the owner edited Career Goals, saved, pressed the button, waited for "succeeded", and each output was read from disk. There was one run per phrasing.

## Results

### Run A: explicit note (run `127778ce-…`, 15:27Z)

> Looking for consulting/contract assignments, not permanent employment. Prefer Stockholm, hybrid with at most two days a week on-site, or fully remote within Sweden; not willing to relocate abroad. Available from January 2027.

| Stated | Generated | Assessment |
|---|---|---|
| Prefer Stockholm | Stockholm 92, Stockholm hybrid 95. Gothenburg dropped from 100 (baseline) to 45 | Faithful. Followed the note over the Gothenburg work history |
| Hybrid ≤2 days on-site, or remote in Sweden | Fully remote 100, Sweden remote 95. Hard blockers: "full-time on-site required", "five days a week on-site". Soft blockers: "three days on-site", "four days on-site", "no remote" | Faithful and precise |
| Contract, not permanent | Hard blocker "permanent employment mandatory". Avoid term "permanent employment" | Faithful |
| No relocation abroad | Hard blockers "relocation required", "must relocate abroad". Abroad and relocation score 0. "Outside Sweden" is a soft blocker | Faithful. Soft is defensible because remote work for a foreign employer isn't relocating |
| Available Jan 2027 | Absent | Expected, since the schema has no place for it |
| (not stated) | Hard blockers "requires Swedish citizenship" and "requires security clearance level" | Unsupported. Clearance was already in the baseline; the citizenship blocker went from soft to hard |

`practical_fit` weight rose from 0.05 to 0.10 and the unknown-location default from 25 to 50.

### Run B: vague note (run `389d25c7-…`, 15:38Z)

> Open to relocating.

- **Old constraints dropped.** Stockholm is no longer favoured, and the Swedish cities are bunched together: Gothenburg 90, Stockholm 87, Malmö 83. All of Run A's contract and work-mode blockers were removed.
- **Scores moved the right way.** The "abroad/relocation = 0" rules are gone. Foreign locations now score Europe 70, Germany/Netherlands 68, UK 65, Spain 65, Mexico 55.
- **No invented destinations.** Spain and Mexico come from profile facts (native language and education in ES, work entries with country code MX).
- **Blockers contradict the note:**
  - Hard "must be located in": a relocatable consultant is exactly the candidate for such jobs.
  - Hard "no remote": nothing in the profile supports it.
- **Other blockers not supported by the note:** hard "requires citizenship", hard "requires native Swedish", soft "fixed-price only".

### Earlier unrecorded runs (low confidence)

Before Run A, the owner ran informal notes naming Malmö + contract and Gothenburg + permanent. The run just before Run A scored Malmö 100, Lund 90 and Helsingborg 80, and listed "permanent position/employment" as *preferred*. Nobody can recall the exact wording, so it's unresolved whether that output followed a mixed note faithfully or reversed a stated "contract" preference. Treat this as a data point, not a finding.

## Observations

1. **Explicit prose:** gave enough signal to replace location_preference, work_mode and employment_types in this run. The output was as precise as structured fields would plausibly have produced.
2. **Vague prose:** moved the scores in the right direction but produced hard blockers that contradict the note. That silently hides the jobs the note asks for.
3. **Unsupported hard blockers in every run** (citizenship, security clearance), whatever the phrasing. This looks like a tendency of the generator or its prompt, not a free-text problem. Structured fields go through the same LLM prompt as ordinary YAML, so they would not obviously prevent it.
4. **`preferences.availability`:** has no consumer in the matching config. Deleting it loses nothing on the matching side. Whether anything else reads it (CV generation, UI) was not checked.
5. **`education.field_of_study` / `education.location`:** not exercised by this experiment. The generator did use education location indirectly (Spain in Run B).

## Limits

- **One run per phrasing.** LLM output varies between runs and repeatability was not tested.
- **No head-to-head comparison.** The structured fields were empty throughout, so this shows how free text performs, not free text vs. structured fields on the same generator.
- **One consultant profile.**

## Open for the owner and planner

- Whether to open the "profile schema simplification pass" (an open product item, see [`docs/product.md`](../product.md)) based on observations 1, 2 and 4.
- Whether the unsupported or contradictory hard blockers (observation 3) deserve their own work item, for example a prompt/schema instruction that blockers must trace back to stated profile facts. Under invariant 3 / ADR-003 the fix belongs in the prompt or schema, not in rule code. This point is independent of the schema-deletion decision.

## State left behind

- `data/profiles/norberto.munoz/profile.md`: `preferences.notes` currently reads "Open to relocating.", and the live `matching_config.yaml` is Run B's output. The owner was advised to restore his real Career Goals and regenerate.
- `data/profiles/norberto.munoz/matching_config copy.yaml`: the owner's copy of the June baseline, safe to delete.
