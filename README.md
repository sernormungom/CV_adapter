# CV Adapter

Local tool for importing a consultant CV, extracting structured profile data with Anthropic, and generating a tailored CV from a job description.

## What this app does

- Runs a FastAPI backend
- Serves the HTML UI from the backend root URL
- Accepts PDF or DOCX CV uploads
- Uses Anthropic to structure CV data
- Generates a tailored CV based on a pasted job description

## Requirements

- Windows with PowerShell or Git Bash
- Python 3.14 or compatible Python 3.x
- An Anthropic API key

## Project layout

- [backend/main.py](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/backend/main.py): FastAPI entrypoint
- [requirements.txt](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/requirements.txt): Python dependencies
- [html/cv-builder-mpya-import_ver5 1.html](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/html/cv-builder-mpya-import_ver5%201.html): main UI
- [backend/cv_importer/file_receiver.py](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/backend/cv_importer/file_receiver.py): CV import endpoint
- [backend/cv_pipeline/handoff_receiver.py](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/backend/cv_pipeline/handoff_receiver.py): tailored CV generation endpoint

## First-time setup

From the project root:

```bash
cd /c/Users/NorbertoMuñozGómez/Desktop/CV_adapter
python -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you are using PowerShell instead of Git Bash:

```powershell
cd C:\Users\NorbertoMuñozGómez\Desktop\CV_adapter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## API key setup

Create a local `.env` file in the project root:

```bash
cp .env.example .env
```

Then edit `.env` so it contains your real key:

```env
ANTHROPIC_API_KEY=your_real_anthropic_key_here
DATA_DIR=data/profiles
```

Notes:

- `.env` is loaded automatically by the backend via `python-dotenv`
- Keep real secrets only in `.env`
- Do not store a real API key in `.env.example`
- Do not commit `.env`

## Start the app

Activate the virtual environment first, then run:

```bash
cd /c/Users/NorbertoMuñozGómez/Desktop/CV_adapter
source .venv/Scripts/activate
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

PowerShell equivalent:

```powershell
cd C:\Users\NorbertoMuñozGómez\Desktop\CV_adapter
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

## Open the tool

Once `uvicorn` is running, open:

```text
http://127.0.0.1:8000/
```

The backend serves the HTML UI directly, so you do not need a separate `python -m http.server` for normal use.

## Health check

To confirm the backend is alive, open:

```text
http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Typical workflow

1. Start the backend with `uvicorn`
2. Open `http://127.0.0.1:8000/`
3. Click `Import CV`
4. Upload a PDF or DOCX
5. Wait for the profile import to finish
6. Click `Generate CV`
7. Paste the target job description
8. Review the generated CV in the new tab

## API endpoints

- `GET /`: serves the HTML UI
- `GET /health`: health check
- `POST /api/import-cv`: imports and structures a CV
- `POST /api/generate-cv`: generates a tailored CV

## Troubleshooting

### `Failed to fetch` in the browser

Usually means the backend is not reachable.

Check:

- Is `uvicorn` still running in the terminal?
- Does `http://127.0.0.1:8000/health` work?
- Are you opening the UI from `http://127.0.0.1:8000/`?

### `WinError 10013` when starting `uvicorn`

Port `8000` is already in use or blocked.

Try a different port:

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8010
```

If you change the backend port, you must also update any frontend code that hardcodes `localhost:8000`.

### `KeyError: 'ANTHROPIC_API_KEY'`

The backend cannot find your API key.

Check:

- `.env` exists in the project root
- the variable name is exactly `ANTHROPIC_API_KEY`
- you started `uvicorn` from the project root

### `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'`

This means `anthropic` is paired with an incompatible `httpx` version.

This project pins a working version in [requirements.txt](/abs/path/C:/Users/NorbertoMuñozGómez/Desktop/CV_adapter/requirements.txt):

```text
httpx<0.28
```

If needed, reinstall dependencies:

```bash
python -m pip install -r requirements.txt
```

### Import fails after upload

Check the `uvicorn` terminal output. Common causes:

- invalid or expired Anthropic API key
- unsupported file type
- PDF/DOCX text extraction failure
- upstream API or rate limit issues

## Optional: serve the HTML separately

This is usually not necessary, but if you want to open the HTML through a separate local file server:

```bash
cd /c/Users/NorbertoMuñozGómez/Desktop/CV_adapter/html
python -m http.server 5500
```

Then open:

```text
http://localhost:5500/cv-builder-mpya-import_ver5%201.html
```

Note: if you use the standalone HTML server, the frontend must still be able to reach the FastAPI backend on `http://localhost:8000` or whichever port you configured.
