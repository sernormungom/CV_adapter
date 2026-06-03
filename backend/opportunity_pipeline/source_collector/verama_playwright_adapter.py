"""
Playwright adapter for Verama/Ework job portal.

Isolated from core collection so the rest of the pipeline does not require
Playwright. Uses a persistent Chromium profile — log in once and reuse.

Config keys accepted (sources.yaml):
  start_url / url          — Verama app URL
  location / location_filter — location to filter/validate
  browser_profile_path / user_data_dir — persistent Chromium profile folder
  headless                 — true/false (default false)
  headed                   — alternative to headless:false (MVP compat)
  wait_for_manual_filter   — pause for terminal Enter after browser opens
  max_jobs                 — max detail pages to collect (default 50)
  timeout_ms               — page load timeout in ms (default 30000)
  allowed_domains          — list of domains to stay on (default: any)
  strict_location          — skip jobs whose text lacks the location (default true)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse


JOB_DETAIL_PATH_RE = re.compile(r"^/app/job-requests/\d+/?$")

JOB_LINK_HINTS = (
    "assignment", "assignments", "job", "jobs",
    "job-request", "job-requests", "opportunity", "opportunities",
    "uppdrag", "consultant", "request", "requests",
)

FOOTER_MARKERS = [
    "Kontakt Ework Group AB",
    "Inköpsprocessen hanteras av Ework Group AB",
    "This request is managed by Ework Group",
    "Få betalt snabbt och enkelt!",
    "PayExpress:",
    "Terms of use",
    "Senast visade",
    "Hitta uppdrag",
]

COOKIE_BUTTON_NAMES = (
    "accept", "accept all", "allow all", "i agree", "ok",
    "got it", "godkann", "acceptera",
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _ascii_fold(value: str) -> str:
    table = str.maketrans({"å": "a", "ä": "a", "ö": "o", "Å": "a", "Ä": "a", "Ö": "o"})
    return value.translate(table).lower()


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _is_job_detail(url: str) -> bool:
    return bool(JOB_DETAIL_PATH_RE.match(urlparse(url).path))


def _same_domain(url: str, domains: List[str]) -> bool:
    if not domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _text_contains_location(text: str, location: str) -> bool:
    hay = _ascii_fold(text)
    needle = _ascii_fold(location)
    if needle in hay:
        return True
    if needle in {"gothenburg", "goteborg"}:
        return "gothenburg" in hay or "goteborg" in hay
    return False


def _looks_like_location(value: str) -> bool:
    low = value.lower()
    if not value or len(value) > 80 or "terms of use" in low:
        return False
    if "(se)" in low:
        return True
    return bool(re.search(r"(gothenburg|göteborg|stockholm|malmö|sweden)", low))


def _trim_footer(lines: List[str]) -> List[str]:
    for i, line in enumerate(lines):
        if any(m.lower() in line.lower() for m in FOOTER_MARKERS):
            return lines[:i]
    return lines


# ---------------------------------------------------------------------------
# Page interaction helpers
# ---------------------------------------------------------------------------

def _dismiss_cookies(page: Any) -> None:
    for name in COOKIE_BUTTON_NAMES:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I))
            if btn.count():
                btn.first.click(timeout=1200)
                return
        except Exception:
            pass


def _wait_ready(page: Any, timeout_ms: int) -> None:
    for state in ("domcontentloaded", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=min(timeout_ms, 10000))
        except Exception:
            pass


def _auto_scroll(page: Any) -> None:
    page.evaluate("""
        async () => {
          const delay = ms => new Promise(r => setTimeout(r, ms));
          let last = 0;
          for (let i = 0; i < 8; i++) {
            window.scrollTo(0, document.body.scrollHeight);
            await delay(350);
            const h = document.body.scrollHeight;
            if (h === last) break;
            last = h;
          }
          window.scrollTo(0, 0);
        }
    """)


def _try_fill_location(page: Any, location: str) -> bool:
    patterns = [re.compile(r"location|city|place|ort|plats|stad", re.I)]
    locator_factories = []
    for pattern in patterns:
        locator_factories.extend([
            lambda p=pattern: page.get_by_label(p),
            lambda p=pattern: page.get_by_placeholder(p),
            lambda p=pattern: page.get_by_role("textbox", name=p),
            lambda p=pattern: page.get_by_role("combobox", name=p),
        ])
    css_selectors = [
        "input[name*='location' i]", "input[placeholder*='location' i]",
        "input[aria-label*='location' i]", "input[name*='city' i]",
        "input[placeholder*='city' i]", "input[name*='ort' i]",
        "input[placeholder*='ort' i]", "input[name*='plats' i]",
        "input[placeholder*='plats' i]",
    ]
    for make in locator_factories:
        try:
            loc = make()
            if loc.count():
                loc.first.click(timeout=1500)
                loc.first.fill(location, timeout=1500)
                loc.first.press("Enter", timeout=1500)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    for sel in css_selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=1500)
                loc.first.fill(location, timeout=1500)
                loc.first.press("Enter", timeout=1500)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

def _extract_page_title(page: Any) -> Optional[str]:
    for sel in ["h1", "[data-testid*='title' i]", "[class*='title' i]"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                title = _clean_text(loc.first.inner_text(timeout=1500))
                if title:
                    return title
        except Exception:
            pass
    try:
        return _clean_text(page.title()) or None
    except Exception:
        return None


def _extract_visible_text(page: Any) -> str:
    for sel in ["main", "article", "[role='main']", "body"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                text = _clean_text(loc.first.inner_text(timeout=2500))
                if len(text) >= 200:
                    return text
        except Exception:
            pass
    return ""


def _extract_job_fields(raw: str) -> Dict[str, str]:
    lines = [l.strip() for l in _clean_text(raw).splitlines() if l.strip()]
    lines = _trim_footer(lines)
    fields: Dict[str, str] = {"company": "", "role": "", "seniority": "", "location": "", "deadline": ""}
    for i, line in enumerate(lines):
        if re.match(r"^publicerad den .* av$", line, flags=re.I) and i + 1 < len(lines):
            fields["company"] = lines[i + 1]
        if line.lower() == "roll" and i + 1 < len(lines):
            fields["role"] = lines[i + 1]
        if line.lower() == "senioritetsnivå" and i + 1 < len(lines):
            fields["seniority"] = lines[i + 1]
        if line.lower() == "plats" and i + 1 < len(lines) and _looks_like_location(lines[i + 1]) and not fields["location"]:
            fields["location"] = lines[i + 1]
        if line.lower() == "ansökningstiden löper ut" and i + 1 < len(lines):
            fields["deadline"] = lines[i + 1]
    return fields


def _clean_job_text(raw: str, title: str = "") -> str:
    lines = [l.strip() for l in _clean_text(raw).splitlines() if l.strip()]
    lines = _trim_footer(lines)
    nav_prefixes = ("Hem", "Uppdragsannonser", "Visa", "Ework+", "Tidsrapporter",
                    "Help center", "Tillbaka till uppdrag", "Spara uppdrag", "Dela")
    noisy_exact = {"Du kan inte ansöka själv. Be din Manager att ansöka.", "Plats", "Visa profil"}
    filtered = [l for l in lines
                if l not in noisy_exact
                and not l.startswith(nav_prefixes)
                and not re.match(r"^för \d+ .* sedan$", l, flags=re.I)]
    if title:
        try:
            idx = next(i for i, l in enumerate(filtered) if _ascii_fold(title) == _ascii_fold(l))
            filtered = filtered[idx:]
        except StopIteration:
            pass
    return _clean_text("\n".join(filtered))


# ---------------------------------------------------------------------------
# Link collection
# ---------------------------------------------------------------------------

def _collect_links(
    page: Any, start_url: str,
    location: Optional[str], domains: List[str], max_jobs: int,
) -> List[Dict[str, str]]:
    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]')).map(a => {
          const c = a.closest(
            'article, li, tr, [role="row"], [role="listitem"], [data-testid], .card, .job, .assignment'
          ) || a;
          return {
            href: a.href,
            linkText: a.innerText || a.textContent || '',
            containerText: c.innerText || c.textContent || '',
            ariaLabel: a.getAttribute('aria-label') || '',
            title: a.getAttribute('title') || ''
          };
        })
    """)
    seen: set = set()
    candidates: List[Dict[str, str]] = []
    for row in rows:
        href = str(row.get("href") or "").strip()
        if not href or href.startswith("javascript:") or href in seen:
            continue
        absolute = urljoin(start_url, href)
        if not _same_domain(absolute, domains) or not _is_job_detail(absolute):
            continue
        text = _clean_text(" ".join(
            str(row.get(k) or "") for k in ["linkText", "containerText", "ariaLabel", "title"]
        ))
        hint_text = f"{absolute} {text}".lower()
        if not any(hint in hint_text for hint in JOB_LINK_HINTS):
            continue
        seen.add(absolute)
        candidates.append({"url": absolute, "summary": text[:500]})
        if len(candidates) >= max_jobs:
            break
    return candidates


# ---------------------------------------------------------------------------
# Detail page collection
# ---------------------------------------------------------------------------

def _collect_detail(context: Any, url: str, source: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_ready(page, timeout_ms)
        _dismiss_cookies(page)
        _auto_scroll(page)
        title = _extract_page_title(page)
        raw_text = _extract_visible_text(page)
        fields = _extract_job_fields(raw_text)
        text = _clean_job_text(raw_text, title=title or "")
        return {
            "source_id": source.get("id", "verama"),
            "source_type": "verama_playwright",
            "source_url": page.url,
            "title_hint": title,
            "company_hint": fields.get("company") or source.get("company_hint"),
            "text": text,
        }
    finally:
        page.close()


# ---------------------------------------------------------------------------
# User signal — stdin (terminal) with sentinel-file fallback for uvicorn
# ---------------------------------------------------------------------------

def _wait_for_user_signal(project_root: Path, location: str) -> None:
    """
    Wait for the user to signal that they have logged in and set the filter.
    Primary:  press Enter in the terminal (works when running directly).
    Fallback: poll for data/ta_config/verama_continue.flag (used when stdin
              is not a real TTY — e.g. uvicorn --reload spawns a subprocess).
    The dashboard 'Continue Verama' button creates that file via the API.
    """
    import time

    sentinel = project_root / "data" / "ta_config" / "verama_continue.flag"
    sentinel.unlink(missing_ok=True)

    print(
        f"\n{'='*62}\n"
        f"VERAMA: Browser is open.\n"
        f"  1. Log in to Verama if needed.\n"
        f"  2. Confirm the '{location}' location filter is applied.\n"
        f"  3. Then signal ready by ONE of:\n"
        f"     a) Pressing Enter here in this terminal window, OR\n"
        f"     b) Clicking 'Continue Verama' in the dashboard UI.\n"
        f"{'='*62}\n",
        file=sys.stderr,
    )

    # Try real terminal stdin first
    try:
        if sys.stdin.isatty():
            input()
            return
    except (EOFError, OSError, AttributeError):
        pass

    # Stdin not a TTY (uvicorn subprocess) — poll sentinel file (10 min)
    print("  (stdin not available — waiting for sentinel file or dashboard button…)", file=sys.stderr)
    deadline = time.time() + 600
    while time.time() < deadline:
        if sentinel.exists():
            sentinel.unlink(missing_ok=True)
            print("  Verama continue signal received.", file=sys.stderr)
            return
        time.sleep(2)

    raise RuntimeError("Verama: timed out waiting for user signal (10 min). Create the file to continue: " + str(sentinel))


# ---------------------------------------------------------------------------
# Main entry point (called by board_connector)
# ---------------------------------------------------------------------------

def collect_verama_jobs(source: Dict[str, Any], project_root: Path) -> List[Dict[str, Any]]:
    sync_playwright = _import_playwright()

    start_url = source.get("start_url") or source.get("url")
    if not start_url:
        raise RuntimeError("verama_playwright source requires start_url or url")

    # Accept both MVP and legacy key names
    location = source.get("location") or source.get("location_filter")
    max_jobs = int(source.get("max_jobs", 50) or 50)
    timeout_ms = int(source.get("timeout_ms", 30000) or 30000)
    headed = _truthy(source.get("headed"), default=False)
    headless = False if headed else _truthy(source.get("headless"), default=False)
    wait_for_manual_filter = _truthy(source.get("wait_for_manual_filter"), default=False)
    strict_location = _truthy(source.get("strict_location"), default=True)
    allowed_domains = [str(x) for x in (source.get("allowed_domains") or [])]

    # Accept both browser_profile_path (legacy) and user_data_dir (MVP)
    raw_profile = source.get("browser_profile_path") or source.get("user_data_dir") or "data/ta_config/browser_profiles/verama"
    profile_dir = Path(str(raw_profile))
    if not profile_dir.is_absolute():
        profile_dir = project_root / profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(str(start_url), wait_until="domcontentloaded", timeout=timeout_ms)
            _wait_ready(page, timeout_ms)
            _dismiss_cookies(page)

            if location:
                _try_fill_location(page, str(location))

            if wait_for_manual_filter and not headless:
                _wait_for_user_signal(project_root, location or "Gothenburg")
                _wait_ready(page, timeout_ms)

            _auto_scroll(page)
            candidates = _collect_links(page, page.url, location, allowed_domains, max_jobs)

            # Fallback: if we're already on a detail page and found no listing links
            if not candidates and _is_job_detail(page.url):
                current_text = _clean_job_text(_extract_visible_text(page))
                if len(current_text) >= 200:
                    candidates = [{"url": page.url, "summary": current_text[:500]}]

            jobs: List[Dict[str, Any]] = []
            seen_urls: set = set()
            for cand in candidates:
                url = cand["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    item = _collect_detail(ctx, url, source, timeout_ms)
                    text = item.get("text", "") or ""
                    if len(text) < 200:
                        item["error"] = "Extracted text too short — page may not be a job detail."
                    elif strict_location and location and not _text_contains_location(
                        " ".join([text, str(item.get("title_hint") or "")]), str(location)
                    ):
                        item["error"] = f"Skipped — text does not contain location {location!r}."
                    jobs.append(item)
                except Exception as exc:
                    jobs.append({
                        "source_id": source.get("id", "verama"),
                        "source_type": "verama_playwright",
                        "source_url": url,
                        "title_hint": None,
                        "company_hint": source.get("company_hint"),
                        "text": "",
                        "error": str(exc),
                    })
            return jobs
        finally:
            ctx.close()
