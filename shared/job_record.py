"""Job-record contract — the single text-normalization primitive still live.

`clean_text` is the one function here with a live caller:
`backend/jobs/service.py`'s `build_input`, reached from `POST /api/jobs`. The
identity-derivation chain that used to live in this module (native id →
identity key → stable job_id, plus the collection engine that called it) was
removed in STAB-7: it backed the retired paste-ingest endpoint and the
discarded Slice 6 email-ingestion primitive, and had no remaining caller.
"""

from __future__ import annotations

import html as html_lib
import re


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    text = html_lib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
