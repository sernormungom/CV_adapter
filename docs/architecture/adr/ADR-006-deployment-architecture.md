# ADR-00X: Deployment Architecture for MVP 1.0

- **Status:** Proposed
- **Date:** 2026-05-27
- **Deciders:** [Project Lead], [Talent Advisor stakeholder]
- **Scope:** MVP 1.0 — co-located pilot with 1 Talent Advisor and 1–5 Consultants in a small Swedish consultancy office

---

## Context

The Consultant Opportunity Matching Platform consists of two Python pipelines (Opportunity Matching, CV Preparation), a shared Data Layer with four logical stores plus observability, and an HTML Dashboard. The application architecture is defined in `/workspace_1.dsl`. This ADR addresses only **how and where it runs**, not what it does.

The defining constraints:

- **Pilot scale and co-location.** MVP 1.0 targets one Talent Advisor team with 1–5 Consultants, all working from the same small office. Users access the system from desks in the same room as the host machine, on the firm's own trusted LAN, during working hours. Peak concurrent users: ≤6. Load is trivial.
- **Operator is technical and present.** The project lead is the operator and the System Administrator actor in the model. The operator works from the same office as the users, on the same network, during the same hours.
- **Users are non-programmers.** Consultants and the Talent Advisor cannot be expected to install or troubleshoot software. Access must be a URL and (eventually) a login.
- **Data residency: EU.** The pilot operates in Sweden. Consultant CVs, verdict history, and profile data are personal data under GDPR.
- **Budget is minimal.** The deciders prefer the cheapest viable option meeting the constraints above.

### What co-location changes

The earlier drafts of this ADR assumed remote access. Co-location materially changes the deployment:

- The host PC does **not** need to be reachable from the public internet. Users connect over the office LAN.
- The host PC does **not** need to be always-on. It needs to be on during office hours, which it is anyway.
- The "operator is unavailable" risk shrinks: if the operator is in the office, so is the system; if the operator is out, the users are likely out too.
- Authentication, while still needed eventually, is not an urgent week-one requirement on a trusted LAN with six known users.

This ADR is written for the co-located reality, not for a notional production-shaped pilot. Decisions appropriate to remote access (public tunnel, magic-link auth from day one, always-on hardware) are explicitly deferred.

### Note on GDPR

Hosting on the operator's office PC does **not** remove the system from GDPR scope. Personal data of EU residents is in scope regardless of where the hardware sits. What office-hosting does change is the set of third-party processors involved: no cloud compute or database vendor, no DPA needed with those parties. GDPR obligations (lawful basis, privacy notice to consultants, reasonable security, breach notification, data subject rights) still apply and are handled separately in the project's privacy documentation. The remaining third-party processors that **do** require DPAs are the LLM provider and (if added) the transactional email service.

### Architectural facts that influence deployment

1. **The two pipelines share the Data Layer.** Both read the Profile & Configuration Store. The Dashboard and the CV Pipeline both write to the Application Tracker. The CV Pipeline reads from the Job Store. Logical pipeline separation does not imply data separation.
2. **The Source Collector runs on a daily schedule.** Needs a scheduling primitive but not a queue or worker fleet.
3. **The CV Pipeline does one LLM call that may take 10–60 seconds.** Either the Dashboard tolerates long HTTP responses, or CV generation runs as a background task with notification on completion. The model already provides `cvNotifier` for the latter.

---

## Decision

Deploy MVP 1.0 as a **LAN-only application on the operator's office PC**, with the following shape:

### Hosting

- **Host:** The operator's existing office desktop, or any other machine in the office that is on during working hours. No new hardware purchase. If the existing host turns out to be unsuitable (e.g. it gets rebooted often, or the operator switches it off at the end of the day), revisit with a small mini-PC.
- **Network exposure:** **LAN only.** The application binds to the host's LAN IP (or `0.0.0.0`) on a fixed port. Users access it via a stable URL like `http://consultancy-platform.local:8000` (using mDNS / `.local` resolution) or `http://<host-IP>:8000`. No public exposure, no tunnel service, no DNS provider, no TLS certificate required at pilot start.
- **Operating system:** Linux (Ubuntu LTS or Debian stable) recommended; macOS acceptable. Windows is workable but requires more care around Docker, networking, and avoiding scheduled reboots.

### Application packaging

- **One Python application, one process group.** Both pipelines run as modules inside a single application served by a small web framework (FastAPI recommended). Logical separation between pipelines is preserved in the codebase, not at the process level.
- **Daily collection** runs as an embedded scheduler (APScheduler), or as a host-level cron triggering a CLI entry point. Either is fine; the embedded scheduler keeps everything inside `docker compose`.
- **CV generation** runs as a background task within the same process (Python `asyncio` task). The Dashboard returns immediately on CV trigger; the Talent Advisor is notified when rendering is complete (via `cvNotifier`).
- **Deployment is via Docker Compose.** One compose file defines the application container and Postgres. Deployment to the host is `git pull && docker compose up -d --build`, run from a terminal on the host.
- **Database migrations** run via Alembic as a one-shot container before bringing up the app.

### Data Layer

- **Single Postgres instance running locally** in a Docker container with a persistent volume on the host. The four logical stores in the model (Job Store, Profile & Configuration Store, Application Tracker, TA Configuration Store) become **separate schemas** in one database.
- **Backups:** Weekly `pg_dump` to a USB drive kept in the office, plus monthly encrypted copy taken home or stored in a different physical location. Backups are not load-bearing at pilot start — consultants can re-upload CVs, verdict history is short — but they exist and a quarterly restore drill confirms they work.
- **Observability** is a logging library (`structlog` recommended) writing JSON logs to local files, plus `docker compose logs` for live tailing. No external sink at launch. If log search becomes painful, add a free-tier EU-region sink (Grafana Cloud Free, Better Stack) later.

### Authentication

- **No authentication at pilot launch.** Access is restricted by network location: only users on the trusted office LAN can reach the URL. The user population is six known people in the same room as the operator.
- **Identity is by self-declaration:** the Dashboard prompts the user to pick their name from a short list (the Talent Advisor configures the consultant roster anyway, per the TA Configuration Store in the model). This is sufficient to attribute verdicts and trigger the right CV generation. It is not security; it is identification.
- **Triggers for adding real auth:**
  1. First request to access the system from outside the office (sick day, client visit, working from home).
  2. First non-firm user (contractor, visitor, second team).
  3. First sign that LAN-only access is materially limiting usage.
  At any of these triggers, add magic-link (passwordless email) auth. This is a one-day change, not a re-architecture.
- **System Administrator access** to the host and database is via local console / SSH, separate from any Dashboard auth that gets added later.

### LLM access

- **Operator-pays model with a single shared API key**, stored in a `.env` file on the host (mode `600`) and loaded into the application container at startup. Predictable bounded cost at pilot scale; BYOK is deferred until there is a second tenant.
- **LLM provider** must offer an endpoint with a DPA compatible with EU data residency. Provider selection is a separate ADR.
- A **monthly spend cap** is configured at the LLM provider to bound runaway cost from bugs or misuse.

### Email (notifications)

- **The `cvNotifier` notification to the Talent Advisor** is the only outbound email in the system at pilot start. Since the Talent Advisor sits in the same room as the operator and the consultant, this can begin as an **in-app notification on the Dashboard** (the Talent Advisor sees "CV ready for [consultant] / [position]" the next time they open the page) or as a verbal "the CV's done" across the office.
- **Trigger for adding transactional email:** when the Talent Advisor genuinely needs notification while away from the dashboard. At that point, add an EU-resident transactional email service (Postmark EU, Brevo, AWS SES `eu-north-1`). This is a half-day change.

---

## Consequences

### Positive

- **Effectively zero infrastructure cost.** Estimated total non-LLM cost: €0/month at launch, rising to ~€5/month if/when off-site cloud backup or transactional email gets added.
- **Maximum simplicity.** No tunnel, no public DNS, no TLS cert, no auth flow, no email service. The pilot ships when the application ships.
- **Maximum operator control and learning.** The operator sees the whole system at every layer.
- **No vendor lock-in.** Only third-party processors with personal data are the LLM provider (unavoidable) and, eventually, an email provider.
- **The architecture model is preserved.** Two pipelines stay logically separate (modules), four stores stay logically separate (schemas). The deployment is an honest minimum-viable collapse, not a contradiction of the C4 model.
- **EU residency by physical location.** No region-selection mistakes possible.

### Negative / accepted tradeoffs

- **LAN-only is a hard limit on access.** A consultant working from home cannot use the system. Accepted: at the moment, users sit in the same office. The trigger for revisiting this is explicit and the change is small.
- **No real authentication.** The system trusts whoever is on the office LAN. Accepted on the basis that the LAN is trusted (only firm employees), the user count is small and known, and the data on the system is not classified beyond standard HR-grade sensitivity. Anyone walking up to an unlocked office computer could already access far more sensitive things; the deployment is not the weakest link in office security.
- **The operator is the SLA.** If the operator's machine has a problem and the operator is not there to fix it, the pilot pauses. Accepted because operator and users are co-located.
- **Single point of failure (the host PC).** Disk failure or hardware fault takes the system down until a restore. Mitigated by weekly backups; further mitigated because pilot data is recoverable (consultants can re-upload CVs, verdict history rebuilds over a few cycles).
- **No staging environment.** Bugs reach pilot users. Mitigated by: users are early collaborators expecting rough edges, and `docker compose` makes a fast local dev environment available for pre-deploy testing.
- **Pipelines share a process.** A bug in one can crash the other; a restart recovers both. Acceptable because both pipelines are owned by the same operator and deployed together.
- **No horizontal scaling, no multi-tenancy.** Both deliberately out of scope.

### Neutral / to be revisited

- **In-process background tasks vs. dedicated worker.** Start with `asyncio`. Promote to a small in-process queue (e.g. `arq`) if CV generation reliability becomes a problem.
- **Off-site backup strategy.** Weekly USB plus monthly off-site copy is the floor. Upgrade to nightly encrypted upload to a Hetzner Storage Box (~€3/month) once the data on the system is harder to reconstruct.

---

## Alternatives considered

### A. Managed European PaaS (Fly.io / Render / Railway) with managed Postgres

**Rejected for MVP 1.0; remains the migration target once the pilot outgrows LAN-only.** Cost ~€50–100/month is small but non-zero, and at co-located pilot scale the cloud's main values (always-on, managed backups, remote access) do not yet justify it. The Docker Compose layout chosen here ports to any of these providers with low effort.

### B. Self-hosted on the operator's PC, exposed via public tunnel

**Rejected for MVP 1.0 launch; becomes the next step if remote access is needed.** A previous draft of this ADR included Cloudflare Tunnel by default. With co-located usage confirmed, the tunnel adds a public attack surface and a dependency on a third party for no current benefit. Adding a tunnel later is a 30-minute change.

### C. Two separate processes for the two pipelines

**Rejected.** The pipelines share the Data Layer. Splitting processes does not split data, and adds operational cost (two services to monitor, two log streams to correlate). Logical separation is preserved by keeping the pipelines as separate Python modules; physical separation can come later if one pipeline outgrows the other.

### D. Self-hosted on the Talent Advisor's hardware

**Rejected.** The Talent Advisor is a non-programmer and cannot operate the host. Putting the host on the operator's machine puts the hardware where the technical operator already is.

### E. Hybrid: one pipeline local, the other in the cloud

**Rejected.** The pipelines share data. A cloud-hosted pipeline would either need network access back to the office Postgres (complex, fragile, would require the tunnel anyway) or a duplicated data layer (incoherent). The architectural cost is not worth the marginal load-balancing benefit, which doesn't apply at pilot scale.

### F. Magic-link auth from day one

**Rejected for launch.** Adds a working email integration, a token table, and a login flow before any real user need for them exists. On a trusted office LAN with six known users in the same room, identification (not authentication) is sufficient. Magic-link is the chosen approach when auth is needed; this ADR defers adding it until a stated trigger fires.

### G. Kubernetes / Docker Swarm

**Rejected.** Vastly disproportionate to the problem.

---

## Open questions / follow-ups

1. **Host machine choice** — confirm the operator's existing office PC is suitable (on during working hours, not rebooted aggressively, has enough RAM for Postgres + the app + Docker — 8 GB is a reasonable minimum). If not, a small mini-PC purchase (~€200–400 one-time) is the recommended fix.
2. **LLM provider selection** — separate ADR.
3. **Privacy notice and lawful basis** — separate document, owned by the project lead with the Talent Advisor. Launch blocker.
4. **DPA with the LLM provider** — procurement step. Launch blocker.
5. **Backup verification drill** — define a quarterly restore-from-backup check. Operational, not architectural.

---

## Migration path

This deployment is designed to be honest about pilot scale and easy to leave behind. The migration ladder, in order of likely triggers:

1. **Add auth (magic-link).** Triggered by first remote-access need, first non-firm user, or first sign that LAN-only is limiting. Half-day to one day of work.
2. **Add a public tunnel (Cloudflare Tunnel).** Triggered by recurring remote access need. 30 minutes.
3. **Add transactional email.** Triggered by Talent Advisor needing notifications away from the dashboard. Half a day.
4. **Migrate to managed PaaS.** Triggered by reliability becoming a binding constraint, operator availability becoming a bottleneck, or a second customer signing on. The same Docker Compose layout ports to Fly.io, Render, or Railway in 1–2 days.
5. **Per-tenant deployments.** Triggered by the second customer. Each new tenant gets a fresh deployment of the same artifact; no code changes.
6. **Multi-tenant within one deployment.** Triggered only by customer count making per-tenant deployments operationally painful — likely 5+ tenants. Real work; intentionally deferred until forced.

Each step is small, and none requires undoing the previous one.

---

## Related decisions

- ADR-005 (referenced in the C4 model component `contextAssembler`) — context is assembled in a single pass and not re-queried mid-run. Consistent with single-instance deployment; no implications for this ADR.
