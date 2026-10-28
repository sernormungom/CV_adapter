# Job fixture provenance and sanitization record

**Origin:** Created by the superseded V2 M1 planning cycle to replace an ignored,
non-portable Job fixture. Retained because committed,
sanitized fixtures keep the test suite self-contained.

## Why these files exist

Four tests in `tests/test_ingest.py` used to read
`data/ta_config/local_jobs/VOLGJP00042625.txt`. `data/` is gitignored, and that
particular file is not even present in the current working tree, so the four
tests failed on every machine — the exact G-037 finding ("four ingest tests
require an ignored file"). These committed fixtures replace that dependency so
the suite is self-contained.

## Derivation method

These files are **structurally derived, not copied**. The pre-cleanup corpus at
`data/ta_config/local_jobs/` (20 forwarded job-lead emails) was read to
establish the *shape* the parser and the email cleaner actually meet in
production. Every line of prose, every identifier, and every address in these
fixtures was then written from scratch to reproduce that shape. No sentence of
corpus text is committed here.

Structural features originally preserved because production code depended on
them. **STAB-7 (2026-09-04) removed `shared/collection.py` and
`shared/job_record.py`'s identity-derivation chain as dead code** (zero
remaining callers — Slice 6, the email-ingestion path both would have served,
was discarded, P-018), so most of the rows below now describe features these
fixtures happen to still carry rather than code that reads them. Left as
historical record of why the fixtures look the way they do; the file
provenance/derivation method above is unaffected either way.

| Feature | Originally depended on by | Status |
| --- | --- | --- |
| Leading `-----` separator before any content | `shared.job_record.extract_title_guess` line scanning | Removed in STAB-7 |
| `Från:` / `Skickat:` / `Till:` / `Ämne:` header block | `shared.collection._clean_email_noise` header stripping | Removed in STAB-7 (`shared/collection.py` deleted) |
| `Job Posting ID:   <ID>` line | `shared.job_record._NATIVE_ID_PATTERNS[0]` | Removed in STAB-7 |
| `Job Posting Start/End Date:` lines | close-date parsing | No current reader — the V1 collection engine's close-date field was never wired into the relational `jobs` schema |
| `Location:` line | location scoping | No current reader — same as above |
| `Price: NNN kr/h` line | realistic rate text surviving into `raw_text` | No current reader (the TA Cockpit's rate search was retired with the Cockpit, ADR-010) |
| Trailing response-form and unsubscribe/footer blocks | email-noise stripping | Removed in STAB-7 (`shared/collection.py` deleted) |

The two fixtures' only remaining live consumer is `tests/test_job_fixtures.py`:
`clean_text` (still live, `backend/jobs/service.py`'s `build_input`, reached
from `POST /api/jobs`) and the sanitization guard
(`test_fixtures_carry_no_real_identifiers`). Neither exercises the
identity-derivation or email-cleaning features the table above once tested.

## Sanitization applied

| Category in the corpus | Replaced with |
| --- | --- |
| Broker company name and its role-based submission mailbox | `Example Broker <assignments@example-broker.invalid>` |
| Named Mpya employee recipient (real personal name and work address) | `Example Recipient <recipient@example.invalid>` |
| Client and legal-entity names | `Example Client AB`, `Example Client Subsidiary AB` |
| Native posting IDs in the client's real `VOLGJP…` / `VOLVCAJP…` scheme | synthetic `EXJP00042625` — deliberately **not** matching `_NATIVE_ID_PATTERNS[2]`, so the fixture cannot be mistaken for a real client identifier |
| Live tracking/unsubscribe URLs on a third-party mailing-list host | `links.example.invalid/…` |
| Job-description prose | rewritten from scratch |

The removed real values are deliberately **not** reproduced in this document.
The substring guard in `tests/test_job_fixtures.py::test_fixtures_carry_no_real_identifiers`
lists only company-name and ID-scheme fragments, never an address.

All substituted domains use RFC 2606 reserved TLDs (`.invalid`), so nothing in
these files resolves or can be contacted.

No consultant personal data was ever present in this corpus (these are job
advertisements); the personal data removed is the third-party
recruiter/hiring-contact category recorded in `docs/legal/ropa.md` §4.

## The two files

- **`email_posting_with_native_id.txt`** — the dominant production shape: a
  forwarded job-lead email carrying a native `Job Posting ID`. Originally
  exercised the `native:` identity branch of `build_identity_key`, removed in
  STAB-7 along with the rest of the identity-derivation chain (see the table
  above).
- **`plain_posting_no_native_id.txt`** — a posting with **no** source-owned
  identifier. Originally exercised the `content:` (SHA-1 body hash) identity
  branch, likewise removed. Both fixtures are still used for `clean_text` and
  sanitization coverage (`tests/test_job_fixtures.py`).

Both files clean to well over 200 characters — historically the retired
paste-ingest endpoint's minimum-length floor (that endpoint and its
`_MIN_PASTE_LEN` constant are gone, retired in V2 Slice 1), noted here only so
neither fixture is mistaken for too short to be realistic input.

## Maintenance rule

These are **contract fixtures**. Changing a byte changes the derived
`identity_key` and `job_id`, which is exactly what the tests assert against. Do
not reformat them casually. If a fixture must change, treat it as a change to
the job-record contract and re-read `shared/job_record.py` first.
