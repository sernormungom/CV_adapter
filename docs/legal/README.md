# Privacy & AI-regulation analysis

While building CV Adapter I worked through what EU and Swedish law requires of
a tool that processes employee CVs and uses an LLM to match consultants to
assignments, and turned the answers into concrete design decisions and code.

| Document | What it covers |
| --- | --- |
| [decisions.md](decisions.md) | The starting point: a personal-data inventory of the code, the analysis through four lenses (GDPR lawyer, Swedish employment counsel, AI Act specialist, security engineer), and the decisions that came out of it |
| [dpia.md](dpia.md) | Data Protection Impact Assessment (Art 35): risks, and the mitigation that shipped for each, with file references |
| [lia.md](lia.md) | Legitimate Interest Assessment (Art 6(1)(f)): why the legal basis is legitimate interest and contract, never employee consent |
| [ropa.md](ropa.md) | Record of Processing Activities (Art 30), one entry per purpose, with a retention table |
| [processors.md](processors.md) | Sub-processor register (Anthropic, AWS): transfer mechanisms (DPF + SCCs), DPA status, data sent |
| [ai-act-assessment.md](ai-act-assessment.md) | EU AI Act classification (Annex III §4, Art 6(3)), the provider/deployer roles, and deployer obligations mapped to the system |
| [breach-runbook.md](breach-runbook.md) | Art 33/34 breach response: when the 72-hour clock starts, decision trees for notifying the authority and data subjects, scenarios |
| [backup-policy.md](backup-policy.md) | How erasure propagates to backups (Art 17 / Art 32) |

These decisions are enforced in the code itself: human-in-the-loop for every
outward action (GDPR Art 22), export and hard-delete endpoints (Art 15/17/20),
configurable retention limits (Art 5(1)(e)), and a privacy notice
(`html/privacy.html`).

**Notes on this copy.** The employer that ran the pilot is referred to as "the
Company", and internal HR material and live-infrastructure identifiers were
removed. The documents are engineering-side compliance work written for review
by qualified counsel. They are not legal advice.
