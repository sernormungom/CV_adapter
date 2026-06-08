# CV Adapter — User Guide

For Consultants and Talent Advisors.

## What this tool does

Two things:

1. **CV tailoring** — Import your CV once; paste any job description; get a position-specific CV in seconds.
2. **Opportunity matching** — Collect open positions from job boards; the system scores them against your profile and surfaces the best fits; you give verdicts; the system learns your preferences over time.

---

## Start the app

Open PowerShell from the project folder and run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/ in your browser.

Leave the terminal open while you work — closing it stops the backend.

---

## CV tailoring

### Step 1 — Import your CV (once)

1. Open http://127.0.0.1:8000/
2. Click **Import CV**
3. Upload your PDF or DOCX file
4. Wait for "Import complete" — the system extracts and structures your profile

You only need to repeat this step if your CV changes materially.

### Step 2 — Generate a tailored CV

1. Click **Generate CV**
2. Paste the full job description into the text box
3. Click **Generate**
4. The tailored CV opens in a new tab, ready to review and export

The system adapts the content, emphasis, and wording to the specific role — not just formatting.

---

## Opportunity matching

### Collect new positions

1. Open http://127.0.0.1:8000/dashboard
2. Click **Collect Jobs**
3. Wait for collection to finish (typically 30–90 seconds) — the system scrapes configured job boards and scores the results against your profile

### Review matches

The dashboard organises positions into three buckets:

| Bucket | Meaning |
|---|---|
| **Keep** | Strong match — worth applying |
| **Maybe** | Partial match — worth a manual look |
| **Reject** | Poor match — filtered out |

Click any position to read its full description.

### Give verdicts

For each position you review, choose one:

- **Accept** — you want to apply; the system can generate a tailored CV for it immediately
- **Reject** — not a fit
- **Defer** — you want to revisit it later

Your verdicts are stored. After each batch, the system uses them to update your scoring configuration so future matches improve.

### Generate a CV from a matched position

From the dashboard, open any accepted position and click **Generate CV** — this pre-fills the job description and takes you straight to the generation step.

---

## Common issues

### "Failed to fetch" or the page does not load

The backend is not running or was stopped. Check that the `uvicorn` terminal is still open and running. If it was closed, restart it with the command in [Start the app](#start-the-app).

Confirm the backend is alive: http://127.0.0.1:8000/health should return `{"status":"ok"}`.

### Port already in use

If you see an error like `WinError 10013`, port 8000 is taken. Run on a different port:

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8010
```

Then open http://127.0.0.1:8010/ instead.

### Upload fails immediately

The file must be PDF or DOCX. Check the terminal output for the specific error message.

### CV generation returns something unexpected

Try pasting a shorter, more focused job description. Very long postings with lots of boilerplate can dilute the output. If the problem persists, check the terminal for API error messages and contact your admin.

### "Collect Jobs" returns nothing

The scraper may need a fresh browser session. Contact your admin to reset the browser profile for the relevant job board.
