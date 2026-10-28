# ADR-010: Retire Job Scraping; Use Email as the Only Automatic Job-Ingestion Channel

**Status:** Accepted
**Date:** 2026-08-21
**Supersedes:** ADR-008 (Job Triage Stays Local)
**Related:** ADR-001 (pipeline separation), ADR-005 (Job Store lifecycle),
ADR-007 (per-consultant scoring), V2 Slice 6

---

## Context

ADR-008 placed browser collection, local Job triage, local score-at-collection,
notifications, and scheduled collection in the TA Cockpit. That decision assumed
the local desktop was the practical place to collect Job postings from external
websites and curate them before cloud storage.

The pilot invalidated those assumptions.

### Technical and operational findings

- Job sites differ substantially in structure, formats, and access requirements.
  Some offer APIs; others require browser automation such as Playwright; some
  require authorization or an interactive login.
- Supporting these combinations creates a growing adapter and operations burden.
  An administrator must configure and support source-specific access for each
  Talent Advisor rather than giving TAs one understandable workflow.
- Local collection daemons can run or appear unexpectedly, including during
  meetings, creating operational uncertainty and distracting user experience.

### Legal and commercial findings

- A review of target-site policies found that many prohibit bots, robots, or
  automated access.
- Automated JavaScript/browser access can be detected as bot activity. The risk is
  not merely a failed collection run: the company could lose access to a Job-posting
  provider and harm a valuable client relationship.

### User-experience findings

- In the pilot demo, the split between a local Cockpit and the cloud product was
  confusing. The Talent Advisor went directly to the cloud UI and asked where Jobs
  should be entered.
- The expected mental model is a single cloud product where Jobs live and are
  managed. A separate local triage application introduces unexplained state and
  support questions.

Selected Outlook emails provide a more viable automatic source. They are already
available to the company/user, have a consistent message-oriented transport, and
do not require automating third-party Job-site access. This eliminates the
identified site-policy and bot-detection risk, although ordinary privacy,
authorization, email-security, and data-minimization obligations still apply.

---

## Decision

**Email is the sole automatic Job-ingestion channel in V2. Job persistence,
validation, deduplication, matching, and all user-facing Job workflows are
cloud-owned. Browser scraping, scraping daemons, scheduled collection, local Job
triage, local matching, and local score notifications are retired.**

The initial email route is:

1. Power Automate moves selected Outlook emails to a designated local folder/feed.
2. A small local email-forwarding utility, invoked by that email flow, forwards the
   relevant payload to the authenticated cloud endpoint.
3. The cloud validates the payload, derives required Job fields where possible,
   performs Site-scoped deduplication, creates the Job, and schedules background
   matching.
4. The resulting shared Job appears in the cloud Jobs experience. There is no local
   review queue, local Job Store, or local scoring state.

The local utility is a transport adapter, not a second product. It has no browser,
scraper, Playwright dependency, local database of Jobs, notification center, or
resident scheduler. It retains only the minimum bounded delivery state required for
reliable forwarding.

V2 retains the local-folder and local-forwarder step. A direct Outlook-to-cloud
route is a post-V2 option requiring a new integration decision; V2 neither assumes
nor requires Power Automate Premium/HTTP connector licensing.

### Boundaries of this decision

- TAs, Consultants, and Admins may still create or edit a Job directly in the cloud
  through the V2 `+ Job` flow.
- A user may request best-effort pre-fill from one pasted public Job URL. This is a
  manual form convenience, not automatic ingestion, scraping, crawling, or a
  scheduled collection service. It remains subject to the V2 SSRF, timeout,
  response-size, content-type, redirect, and blacklist protections.
- No automatic Job ingestion may be introduced from external websites, including
  through scheduled website scraping, browser automation, source-specific APIs, or
  a cloud scraper, without a new ADR that changes this decision.

---

## Consequences

### Positive

- Removes the technical complexity of maintaining heterogeneous scrapers, browser
  automation, login flows, API adapters, and per-TA local configuration.
- Removes the identified third-party Job-site policy and bot-detection risk from
  automatic ingestion.
- Gives TAs one clear primary workflow: Jobs are created and managed in the cloud.
- Removes the local/cloud split that confused pilot users and eliminates the risk of
  a local collection daemon running at an inappropriate time.
- Makes automatic ingestion use the same cloud validation, Site assignment,
  deduplication, lifecycle, and matching path as manually created Jobs.
- Reduces local installation, support, cached-data, and cleanup obligations.

### Trade-offs and obligations

- Automatic coverage is limited to Jobs received through selected emails. The
  operating process must ensure relevant Job emails reach the designated Outlook
  flow; manual cloud Job entry remains available for other opportunities.
- Email messages and attachments can still vary in quality. Extraction must validate
  required fields and return a clear result when a usable Job cannot be created.
- The email route still processes personal and potentially sensitive data. It must
  authenticate the sender/forwarder, minimize retained email content, avoid logging
  sensitive bodies unnecessarily, and delete transient artifacts according to the
  V2 retention rules.
- The selected local-folder Power Automate flow and forwarder need their own
  credentials, permission review, support model, and failure/retry behavior. A
  paid/direct HTTP connector is not part of V2.
- Existing local Cockpit artifacts and cached data require a separate inventory,
  migration/cleanup plan, and explicit deletion verification before retirement.

---

## Implementation implications

1. Replace the current local Cockpit with the minimal forwarding utility described
   above; remove scraping adapters, collection runners, local pools, local matching
   configuration sync, toasts, and schedulers from the supported product path.
2. Make the cloud email-ingestion endpoint use the same Job-creation service as the
   V2 UI and any other authorized manual source. Site assignment comes from the
   authenticated TA identity, not from untrusted email content.
3. Ensure the cloud service can return a durable processing result for an email:
   created, duplicate, rejected for missing/invalid required data, or failed with a
   recoverable reason.
4. Add background processing and idempotency so duplicate delivery or retry does not
   create duplicate Jobs or duplicate match work.
5. Remove all product documentation, setup instructions, and deployment assumptions
   that present local scraping, local triage, or collection daemons as active.
6. Retain manual `+ Job` and safe single-URL pre-fill as cloud features; neither may
   silently become a general-purpose scraper.
7. Before retiring existing local state, inventory its contents and verify cleanup
   targets. Do not delete unverified local data merely because this ADR changes the
   future direction.

---

## Alternatives considered

### Keep the local TA Cockpit and improve its scrapers

Rejected. It preserves the fundamental problem: a growing set of incompatible
formats, authentication requirements, automated browser behavior, administrator
setup work, and the third-party bot/terms risk. Better local tooling cannot make
prohibited or fragile automated access a dependable product foundation.

### Move scraping to cloud infrastructure

Rejected. Moving the execution location does not resolve website terms,
bot-detection risk, source-specific authentication, or format variance. It would
also centralize the operational and commercial risk rather than remove it.

### Continue supporting source-specific APIs as automatic ingestion

Rejected for V2. APIs reduce some technical difficulty but retain a fragmented,
per-provider integration and configuration model. V2 has one automatic channel:
selected email. A future provider integration would require a separate product and
architecture decision.

### Start with direct Outlook-to-cloud integration

Deferred beyond V2. It may eventually simplify deployment, but needs a separate
decision on licensing, permissions, reliability, security, and support. The Power
Automate-to-local-folder forwarding path is the locked V2 route.

---

## Supersession notes

ADR-008 is superseded in full for the active product direction:

- local Job triage is retired;
- local score-at-collection and Windows toast notifications are retired;
- local Job pools and config synchronization are retired;
- local and scheduled website collection are retired; and
- the cloud backend becomes the sole owner of Jobs and matching workflows.

ADR-008 remains valuable historical context for why the Cockpit was built, but it
must not be used to justify new scraping, local triage, or local matching work.
