# CV Adapter — User Guide

For Consultants and Talent Advisors.

## What this tool does

Two things:

1. **CV tailoring** — Keep your own CV in the editor; apply to a job; get a position-specific CV that leaves yours untouched.
2. **Job matching** — Enter an open position once; everyone at your Site who should see it does; the system gives each consultant an advisory score for it, and you record Apply or Reject.

---

## Open the app

Open the address your administrator gave you and sign in. (Maintainers running
it locally: see `docs/running.md`; the local address is http://127.0.0.1:8000/.)

---

## CV tailoring

### Step 1 — Your own CV

1. Open the CV editor (the start page).
2. Fill in or edit your CV directly in the editor — it saves as you go.
3. Or click **↑ Import CV** and upload a PDF, Word or Markdown (`.md`) file. It
   runs in the background, keeps going even if you close the page, and shows
   progress until it finishes. When it succeeds, the facts it found are saved
   into your profile and loaded into the editor's fields for you to review.
4. Add a profile photo — a tailored CV cannot be generated without one.

Import only ever changes your own profile facts (`profile.md`), never a
tailored CV you are viewing for a specific position.

Your CV here is **yours**. Tailoring it for a position never changes it — see
Step 2.

### Step 2 — Generate a tailored CV

A tailored CV belongs to an **application**, so you apply to the job first and
then tailor a CV for it. That is also what keeps your own CV safe: the tailored
version is a separate document, and editing it never changes yours.

1. Open **Jobs**, add the job if it is not there, and click **Apply**
2. On **Applications**, click **Tailor CV**
3. It says "Generating…" and keeps going even if you close the page — a full
   generation can take longer than a browser request is allowed to
4. When it is ready, click **Open CV** to review and edit it, or **Download
   PDF** to print or save it

If you have no profile or no photo yet, generation is refused with a message
saying what is missing.

The system adapts the content, emphasis, and wording to the specific role — not
just formatting. It uses your profile and the posting exactly as they were when
you asked, so editing your profile while it works will not change that CV.

**Editing a tailored CV.** Opening one puts the editor in a green-banner mode
that says which position it is for. Everything you change is saved to that
application's CV. **Back to my CV** returns to your own. **Regenerate** starts
over from your current profile and the posting — if it fails, the version you
already had is kept.

---

## Jobs

Open **Jobs** from the top banner. A Talent Advisor can also open a
consultant's jobs from the **Jobs →** link next to their name on the TA
dashboard.

### Add a job

Fill in the title and paste the full posting into the description, plus the
company, location, closing date, and source link if you have them. Click
**Create job**.

Jobs are **shared across your Site**: every consultant there who is actively
looking, and their Talent Advisors, see the same job, and it can be edited by
those who can reach it. If the same (or a very similar) posting is already open
at your Site, the app tells you so and points to the existing one rather than
making a second copy.

There is no automatic collection — nothing scrapes job boards. Entering the
posting is the only way a job appears.

### See how well a job fits

Each job shows an advisory score out of 100 for the selected consultant. It is
worked out from the consultant's profile and matching settings the first time
it is needed and then kept for that consultant–job pair. It is not refreshed
when the profile, the settings or the job are edited later. It never changes
what you are allowed to do with the job.

If a profile or its matching settings are not set up yet, the app says so
plainly instead of showing a misleading zero. The job still works normally.
Matching settings are created or refreshed by **Update match configurator**.

**A score is advice, not a decision.** Nothing is filtered out or applied for on
your behalf.

### Reject a job

Click **Reject**. This records that this job is not a fit for you — no reason
is asked for. Clicking it twice changes nothing — there is one marker per
person per job.

Once you reject a job or apply to it, it leaves your Jobs list; a line at the
top of the list says where it went. Rejected jobs are under **Show → Rejected
jobs**, where **Undo reject** puts one back. Jobs you applied to are on the
**Applications** page, where clicking a job's title shows its description and,
when there is one, a link to the original posting. The job is only hidden for
you: everyone else it is shared with still sees it.

There is no "Maybe": a job is either one you are pursuing or one you have
rejected. A job holds one decision per person, so applying needs any reject
withdrawn first (**Undo reject**), and rejecting needs a live application
withdrawn or closed first.

### Close a job

Click **Close** when a position is no longer open. It stays visible with a
"closed" label under **Show → Closed jobs** or **All jobs**; nothing is
deleted. A closed job you had already rejected or applied to stays where you
left it: under **Rejected jobs**, or on **Applications**.

### Generate a CV for a job

Apply first, then click **Tailor CV** on the application — see
[Step 2](#step-2--generate-a-tailored-cv).

**Download what you need before an application ends.** When an application is
withdrawn or closed, its tailored CV is deleted and any shared link to it stops
working, in the same step. Save the PDF first if you need a copy of what was
sent.

---

## Common issues

### The page does not load, or says "Failed to fetch"

Reload the page and sign in again. If it persists, contact your admin — the
service may be restarting.

### Upload fails immediately

The file must be PDF, Word (`.doc`/`.docx`) or Markdown (`.md`), within the
size limit. A scanned PDF with no readable text is refused with a message
explaining what to do instead.

### CV generation returns something unexpected

Try pasting a shorter, more focused job description. Very long postings with
lots of boilerplate can dilute the output. If the problem persists, contact your
admin.

### No new jobs are showing up

Jobs are added by hand — nothing collects them automatically. You see the open
jobs at your own Site while you are marked as actively looking; your Talent
Advisor sets that. If you expected a new job to appear, contact your TA.
