# CV Adapter

Local tool for importing consultant CVs, scoring job opportunities, and generating position-tailored CVs using Anthropic.

## Quick launch

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/ — health check at http://127.0.0.1:8000/health.

## Documentation

- [GUIDE.md](GUIDE.md) — for Consultants and Talent Advisors
- [ADMIN.md](ADMIN.md) — for system administrators
- [CLAUDE.md](CLAUDE.md) — for Claude Code and AI assistants working on this codebase
