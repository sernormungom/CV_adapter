"""
Source connector — iterates enabled job board sources and yields raw job items.

Each item is a dict:
  job_id       str   — stable SHA-1 hash of URL or content
  source_id    str   — id field from sources.yaml
  source_type  str   — type field from sources.yaml
  source_url   str | None
  title_guess  str   — first meaningful line of text
  company_hint str   — extracted or configured company name
  raw_text     str   — cleaned full text
  collected_at str   — ISO-8601 UTC timestamp
  error        str   — non-empty if fetch failed
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.skip_depth = 0
        self.title: Optional[str] = None
        self._in_title = False
        self._title_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag == "title":
            self._in_title = False
            title = " ".join(self._title_parts).strip()
            if title:
                self.title = clean_text(title)
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
        self.parts.append(text + " ")

    def get_text(self) -> str:
        return clean_text("".join(self.parts))


def clean_text(text: str) -> str:
    text = html_lib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower().strip()


def _normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    skip_prefixes = ("utm_",)
    skip_exact = {"fbclid", "gclid"}
    clean_q = [(k, v) for k, v in query if not k.startswith(skip_prefixes) and k not in skip_exact]
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"),
         urllib.parse.urlencode(clean_q), "")
    )


def stable_job_id(raw_text: str, source_url: Optional[str] = None) -> str:
    if source_url:
        key = "url:" + _normalize_url(source_url)
    else:
        body = re.sub(r"\s+", " ", raw_text.lower()).strip()
        key = "content:" + body[:5000]
    return "job_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def extract_title_guess(text: str, title_hint: Optional[str] = None) -> str:
    if title_hint:
        return title_hint[:120]
    for line in text.splitlines():
        line = line.strip(" \t#-*:")
        if 6 <= len(line) <= 120 and not line.lower().startswith("source url"):
            return line
    return "Unspecified role"


def _read_source_url_header(text: str) -> Optional[str]:
    for line in text.splitlines()[:20]:
        m = re.match(r"^\s*Source URL\s*:\s*(https?://\S+)\s*$", line, flags=re.I)
        if m:
            return m.group(1)
    return None


def _fetch_url(url: str, timeout: float = 20.0) -> Tuple[str, Optional[str]]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CVAdapterBot/2.0",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        content_type = resp.headers.get("content-type", "")
    charset = "utf-8"
    m = re.search(r"charset=([^;]+)", content_type, flags=re.I)
    if m:
        charset = m.group(1).strip()
    decoded = raw.decode(charset, errors="replace")
    if "html" in content_type.lower() or "<html" in decoded[:1000].lower():
        parser = _HTMLTextExtractor()
        parser.feed(decoded)
        return parser.get_text(), parser.title
    return clean_text(decoded), None


# ---------------------------------------------------------------------------
# Source iterators
# ---------------------------------------------------------------------------

def _iter_local_folder(source: Dict[str, Any], project_root: Path) -> Iterator[Dict[str, Any]]:
    folder_path = source.get("folder_path") or source.get("path", "")
    folder = Path(folder_path)
    if not folder.is_absolute():
        folder = project_root / folder
    if not folder.exists():
        return
    for path in sorted(folder.glob("*.txt")):
        if not path.is_file():
            continue
        text = clean_text(path.read_text(encoding="utf-8", errors="replace"))
        yield {
            "source_id": source.get("id", "local_folder"),
            "source_type": "local_folder",
            "source_url": _read_source_url_header(text),
            "title_hint": None,
            "company_hint": None,
            "text": text,
        }


def _iter_url_list(source: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for item in source.get("urls", []) or []:
        if isinstance(item, str):
            url, title_hint, company_hint = item, None, None
        else:
            url = item.get("url") or ""
            title_hint = item.get("title_hint")
            company_hint = item.get("company_hint")
        if not url:
            continue
        try:
            text, page_title = _fetch_url(url)
            if len(text) < 200:
                raise RuntimeError("Fetched page too short — may require JavaScript or login.")
            effective_title = title_hint or page_title
            if effective_title:
                text = f"{effective_title}\n\n{text}"
            yield {
                "source_id": source.get("id", "url_list"),
                "source_type": "url_list",
                "source_url": url,
                "title_hint": effective_title,
                "company_hint": company_hint,
                "text": text,
            }
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            yield {
                "source_id": source.get("id", "url_list"),
                "source_type": "url_list",
                "source_url": url,
                "title_hint": title_hint,
                "company_hint": company_hint,
                "text": "",
                "error": str(exc),
            }


def _iter_saab_public(source: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    base_url = str(source.get("base_url") or "https://www.saab.com").rstrip("/")
    listing_url = source.get("listings_url") or source.get("url") or f"{base_url}/career/job-opportunities"
    location_filter = _fold(str(source.get("location_filter") or source.get("location") or ""))
    max_jobs = int(source.get("max_jobs") or 0)

    try:
        req = urllib.request.Request(
            str(listing_url),
            headers={"User-Agent": "Mozilla/5.0 CVAdapterBot/2.0", "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            listing_html = resp.read().decode("utf-8", errors="replace")
        listing_html = html_lib.unescape(listing_html)
    except (urllib.error.URLError, TimeoutError) as exc:
        yield {
            "source_id": source.get("id", "saab_public"),
            "source_type": "saab_public",
            "source_url": str(listing_url),
            "title_hint": None,
            "company_hint": "Saab",
            "text": "",
            "error": f"Cannot fetch Saab listing page: {exc}",
        }
        return

    item_pattern = re.compile(
        r'<div class="item">(.*?)</div>\s*</div>\s*<div class="item-listing__job-end-date">',
        re.I | re.S,
    )
    link_pattern = re.compile(r'href="(/career/job-opportunities/[^"]+)"', re.I)
    loc_pattern = re.compile(r'<div class="location">\s*([^<]+)\s*</div>', re.I)
    title_pattern = re.compile(r'>([^<>]+)<span class="icon">', re.I)

    seen: set = set()
    items: List[Tuple[str, Optional[str], str]] = []

    for block in item_pattern.findall(listing_html):
        link_m = link_pattern.search(block)
        if not link_m:
            continue
        rel = link_m.group(1).strip()
        full_url = urllib.parse.urljoin(base_url + "/", rel.lstrip("/"))
        loc_m = loc_pattern.search(block)
        item_location = (loc_m.group(1).strip() if loc_m else "")
        if location_filter and location_filter not in _fold(item_location):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        title_m = title_pattern.search(block)
        title_hint = clean_text(title_m.group(1)) if title_m else None
        items.append((full_url, title_hint, item_location))

    if max_jobs > 0:
        items = items[:max_jobs]

    if not items:
        yield {
            "source_id": source.get("id", "saab_public"),
            "source_type": "saab_public",
            "source_url": str(listing_url),
            "title_hint": None,
            "company_hint": source.get("company_hint") or "Saab",
            "text": "",
            "error": (
                f"saab_public: 0 listings matched"
                f" (location_filter='{location_filter or 'none'}')."
                " Saab's HTML structure may have changed — check the regex patterns in board_connector.py."
            ),
        }
        return

    for job_url, title_hint, item_location in items:
        try:
            text, page_title = _fetch_url(job_url)
            if len(text) < 200:
                raise RuntimeError("Saab job page text too short.")
            yield {
                "source_id": source.get("id", "saab_public"),
                "source_type": "saab_public",
                "source_url": job_url,
                "title_hint": title_hint or page_title,
                "company_hint": source.get("company_hint") or "Saab",
                "text": text,
            }
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            yield {
                "source_id": source.get("id", "saab_public"),
                "source_type": "saab_public",
                "source_url": job_url,
                "title_hint": title_hint,
                "company_hint": source.get("company_hint") or "Saab",
                "text": "",
                "error": str(exc),
            }


def _iter_verama_playwright(source: Dict[str, Any], project_root: Path) -> Iterator[Dict[str, Any]]:
    try:
        from .verama_playwright_adapter import collect_verama_jobs
    except Exception as exc:
        yield {
            "source_id": source.get("id", "verama"),
            "source_type": "verama_playwright",
            "source_url": source.get("start_url") or "",
            "title_hint": None,
            "company_hint": None,
            "text": "",
            "error": f"Cannot import Playwright adapter: {exc}",
        }
        return
    try:
        jobs = collect_verama_jobs(source, project_root)
        yield from jobs
    except Exception as exc:
        yield {
            "source_id": source.get("id", "verama"),
            "source_type": "verama_playwright",
            "source_url": source.get("start_url") or "",
            "title_hint": None,
            "company_hint": None,
            "text": "",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Main entry point
def _iter_inkopio_playwright(source: Dict[str, Any], project_root: Path) -> Iterator[Dict[str, Any]]:
    try:
        from .inkopio_playwright_adapter import collect_inkopio_jobs
    except Exception as exc:
        yield {
            "source_id": source.get("id", "inkopio"),
            "source_type": "inkopio_playwright",
            "source_url": source.get("url") or source.get("start_url") or "",
            "title_hint": None, "company_hint": None, "text": "",
            "error": f"Cannot import Inkopio adapter: {exc}",
        }
        return
    try:
        yield from collect_inkopio_jobs(source, project_root)
    except Exception as exc:
        yield {
            "source_id": source.get("id", "inkopio"),
            "source_type": "inkopio_playwright",
            "source_url": source.get("url") or source.get("start_url") or "",
            "title_hint": None, "company_hint": None, "text": "",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------

def collect_from_sources(
    sources_config: List[Dict[str, Any]],
    project_root: Path,
    collected_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Iterate enabled sources and return a flat list of raw job items.
    Each item has: job_id, source_id, source_type, source_url, title_guess,
                   company_hint, raw_text, collected_at, error.
    """
    if collected_at is None:
        collected_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    items: List[Dict[str, Any]] = []

    for source in sources_config:
        if not source.get("enabled", False):
            continue
        source_type = source.get("type", "")

        if source_type == "local_folder":
            iterator: Iterable = _iter_local_folder(source, project_root)
        elif source_type == "url_list":
            iterator = _iter_url_list(source)
        elif source_type == "saab_public":
            iterator = _iter_saab_public(source)
        elif source_type == "verama_playwright":
            iterator = _iter_verama_playwright(source, project_root)
        elif source_type == "inkopio_playwright":
            iterator = _iter_inkopio_playwright(source, project_root)
        else:
            continue

        for raw in iterator:
            text = raw.get("text") or ""
            error = raw.get("error") or ""
            source_url = raw.get("source_url")
            job_id = stable_job_id(text, source_url) if (text or source_url) else ""
            items.append({
                "job_id": job_id,
                "source_id": raw.get("source_id") or source.get("id", ""),
                "source_type": raw.get("source_type") or source_type,
                "source_url": source_url or "",
                "title_guess": extract_title_guess(text, raw.get("title_hint")),
                "company_hint": raw.get("company_hint") or "",
                "raw_text": text,
                "collected_at": collected_at,
                "error": error,
            })

    return items
