"""
Playwright adapter for Inkopio VMS (asociety.inkopio.com).

Traditional server-rendered app. Link collection uses three strategies:
  1. <a href> containing action=view&id=
  2. onclick attributes containing action=view&id= (with optional cpro_ct)
  3. Numeric IDs extracted from table rows → construct URL directly

The cpro_ct token appears optional (double && in the URL is a tell).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .board_connector import clean_text


DETAIL_ID_RE = re.compile(r"action=view&id=(\d+)", re.I)

FOOTER_MARKERS = [
    "Copyright", "All rights reserved", "Privacy Policy", "Powered by",
]

OVERVIEW_LABELS = [
    "overview", "description", "job description", "assignment description",
    "requisition description", "details", "uppdragsbeskrivning",
    "beskrivning", "om uppdraget",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

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


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _wait_ready(page: Any, timeout_ms: int) -> None:
    for state in ("domcontentloaded", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=min(timeout_ms, 12000))
        except Exception:
            pass


def _dismiss_cookies(page: Any) -> None:
    for name in ("accept", "accept all", "ok", "i agree", "got it"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I))
            if btn.count():
                btn.first.click(timeout=1200)
                return
        except Exception:
            pass


def _trim_footer(lines: List[str]) -> List[str]:
    for i, line in enumerate(lines):
        if any(m.lower() in line.lower() for m in FOOTER_MARKERS):
            return lines[:i]
    return lines


# ---------------------------------------------------------------------------
# Link / ID collection
# ---------------------------------------------------------------------------

def _collect_requisition_links(page: Any, base_url: str, max_jobs: int) -> List[str]:
    """
    Return a list of detail-page URLs using Playwright's native Python API
    (avoids evaluate() context-destruction errors on post-filter navigation).
    Three strategies tried in order.
    """
    seen: set = set()
    links: List[str] = []

    def _add(url: str) -> None:
        url = url.strip()
        if url and url not in seen and len(links) < max_jobs:
            seen.add(url)
            links.append(url)

    # Strategy 1: <a href> containing action=view&id=
    try:
        for el in page.locator("a[href*='action=view&id=']").all():
            try:
                href = el.get_attribute("href", timeout=1000) or ""
                if href:
                    _add(urljoin(base_url, href))
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 2: onclick attributes containing action=view&id=
    if not links:
        try:
            for el in page.locator("[onclick*='action=view']").all():
                try:
                    onclick = el.get_attribute("onclick", timeout=1000) or ""
                    m = re.search(r"action=view&id=(\d+)", onclick, re.I)
                    if not m:
                        continue
                    ct = re.search(r"cpro_ct=([a-f0-9]+)", onclick, re.I)
                    url = base_url + "?action=view&id=" + m.group(1)
                    if ct:
                        url += "&&cpro_ct=" + ct.group(1)
                    _add(url)
                except Exception:
                    pass
        except Exception:
            pass

    # Strategy 3: numeric IDs (4-6 digits) in first table cell
    if not links:
        try:
            for tr in page.locator("tr").all():
                try:
                    tds = tr.locator("td").all()
                    if not tds:
                        continue
                    first = (tds[0].inner_text(timeout=500) or "").strip()
                    if not re.match(r"^\d{4,6}$", first):
                        continue
                    # Prefer any link in the row, else construct from ID
                    row_link = tr.locator("a[href]")
                    if row_link.count():
                        href = row_link.first.get_attribute("href", timeout=500) or ""
                        _add(urljoin(base_url, href) if href else base_url + "?action=view&id=" + first)
                    else:
                        _add(base_url + "?action=view&id=" + first)
                except Exception:
                    pass
        except Exception:
            pass

    return links


# ---------------------------------------------------------------------------
# Expand collapsed content ("...More" / "Läs mer" buttons)
# ---------------------------------------------------------------------------

def _expand_content(page: Any) -> None:
    """Click any 'More' / 'Läs mer' / 'Read more' buttons to reveal full text."""
    patterns = [
        re.compile(r"^\s*\.{0,3}\s*more\s*$", re.I),
        re.compile(r"^\s*l[aä]s\s*mer\s*$", re.I),
        re.compile(r"^\s*read\s*more\s*$", re.I),
        re.compile(r"^\s*visa\s*mer\s*$", re.I),
        re.compile(r"^\s*show\s*more\s*$", re.I),
        re.compile(r"^\s*se\s*mer\s*$", re.I),
    ]
    for pattern in patterns:
        try:
            buttons = page.get_by_text(pattern).all()
            for btn in buttons:
                try:
                    if btn.is_visible(timeout=500):
                        btn.click(timeout=1500)
                        page.wait_for_timeout(500)
                except Exception:
                    pass
        except Exception:
            pass

    # Also try role=button with matching text
    for label in ("More", "...More", "Läs mer", "Read more", "Visa mer", "Show more"):
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if btn.count() and btn.first.is_visible(timeout=300):
                btn.first.click(timeout=1500)
                page.wait_for_timeout(500)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Detail page extraction
# ---------------------------------------------------------------------------

def _extract_overview(page: Any) -> str:
    """
    Try labelled-cell extraction first, then textarea/div, then full text.
    """
    # Strategy 1: label cell → adjacent sibling cell (table layout)
    for label in OVERVIEW_LABELS:
        try:
            loc = page.get_by_text(
                re.compile(rf"^\s*{re.escape(label)}\s*[:\-]?\s*$", re.I)
            )
            if not loc.count():
                continue
            sibling_text: Optional[str] = loc.first.evaluate(
                """el => {
                    const td = el.closest('td') || el.closest('th');
                    if (td) {
                        const nx = td.nextElementSibling;
                        return nx ? nx.innerText : null;
                    }
                    const row = el.closest('tr') || el.closest('div');
                    if (row) {
                        const nx = row.nextElementSibling;
                        return nx ? nx.innerText : null;
                    }
                    return null;
                }"""
            )
            if sibling_text and len(sibling_text.strip()) > 50:
                return clean_text(sibling_text)
        except Exception:
            pass

    # Strategy 2: named textarea / content div
    for sel in [
        "textarea[name*='description' i]", "textarea[name*='overview' i]",
        "textarea[id*='description' i]",   "textarea[id*='overview' i]",
        "div[id*='description' i]",         "div[class*='description' i]",
        "td[id*='description' i]",          "span[id*='description' i]",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count():
                text = loc.first.inner_text(timeout=2000)
                if text and len(text.strip()) > 50:
                    return clean_text(text)
        except Exception:
            pass

    # Strategy 3: full visible text from main content area
    for sel in ["main", "[role='main']", "form", "#content", ".content", "body"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                text = clean_text(loc.first.inner_text(timeout=3000))
                lines = _trim_footer([l for l in text.splitlines() if l.strip()])
                result = clean_text("\n".join(lines))
                if len(result) >= 150:
                    return result
        except Exception:
            pass

    return ""


def _extract_title(page: Any) -> Optional[str]:
    for sel in ["h1", "h2", "[class*='title' i]", "[id*='title' i]", "td.label + td"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                title = clean_text(loc.first.inner_text(timeout=1500))
                if title and len(title) < 200:
                    return title
        except Exception:
            pass
    try:
        return clean_text(page.title()) or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional search / filter
# ---------------------------------------------------------------------------

def _try_search(page: Any, query: str) -> bool:
    css_candidates = [
        "input[name*='search' i]", "input[placeholder*='search' i]",
        "input[name*='filter' i]", "input[placeholder*='filter' i]",
        "input[type='search']",
    ]
    for sel in css_candidates:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=1500)
                loc.first.fill(query, timeout=1500)
                loc.first.press("Enter", timeout=1500)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# User signal (stdin + sentinel file fallback — same pattern as Verama)
# ---------------------------------------------------------------------------

def _wait_for_user_signal(project_root: Path, label: str) -> None:
    import time
    sentinel = project_root / "data" / "ta_config" / "inkopio_continue.flag"
    sentinel.unlink(missing_ok=True)
    print(
        f"\n{'='*62}\n"
        f"INKOPIO: Browser is open.\n"
        f"  1. Log in if needed.\n"
        f"  2. The requisition list should be visible.\n"
        f"     If needed, apply the '{label}' filter manually.\n"
        f"  3. Press Enter here to start collecting.\n"
        f"{'='*62}\n",
        file=sys.stderr,
    )
    try:
        if sys.stdin.isatty():
            input()
            return
    except (EOFError, OSError, AttributeError):
        pass
    print("  (stdin not a TTY — waiting for sentinel file…)", file=sys.stderr)
    deadline = time.time() + 600
    while time.time() < deadline:
        if sentinel.exists():
            sentinel.unlink(missing_ok=True)
            return
        time.sleep(2)
    raise RuntimeError("Inkopio: timed out waiting for user signal (10 min)")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def collect_inkopio_jobs(source: Dict[str, Any], project_root: Path) -> List[Dict[str, Any]]:
    sync_playwright = _import_playwright()

    start_url = source.get("url") or source.get("start_url")
    if not start_url:
        raise RuntimeError("inkopio_playwright source requires url or start_url")

    search_query   = source.get("search_query") or source.get("location") or ""
    max_jobs       = int(source.get("max_jobs", 50) or 50)
    timeout_ms     = int(source.get("timeout_ms", 30000) or 30000)
    headed         = _truthy(source.get("headed"), default=False)
    headless       = False if headed else _truthy(source.get("headless"), default=False)
    wait_for_manual = _truthy(source.get("wait_for_manual_filter"), default=False)
    company_hint   = source.get("company_hint") or ""

    raw_profile = (
        source.get("browser_profile_path")
        or source.get("user_data_dir")
        or "data/ta_config/browser_profiles/inkopio"
    )
    profile_dir = Path(str(raw_profile))
    if not profile_dir.is_absolute():
        profile_dir = project_root / profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(str(start_url), wait_until="domcontentloaded", timeout=timeout_ms)
            _wait_ready(page, timeout_ms)
            _dismiss_cookies(page)

            # Try to apply search filter (optional — skip if it fails)
            if search_query and not wait_for_manual:
                _try_search(page, search_query)
                _wait_ready(page, timeout_ms)

            # Pause for manual login / filter confirmation if configured
            if wait_for_manual:
                _wait_for_user_signal(project_root, search_query or "Göteborg")
                # Wait for any pending navigation/filter reload to finish
                _wait_ready(page, timeout_ms)
                page.wait_for_timeout(2000)

            current_url = page.url
            base_url = start_url.split("?")[0]
            links = _collect_requisition_links(page, base_url, max_jobs)

            if not links:
                return [{
                    "source_id": source.get("id", "inkopio"),
                    "source_type": "inkopio_playwright",
                    "source_url": current_url,
                    "title_hint": None, "company_hint": company_hint, "text": "",
                    "error": (
                        "No requisition links found on the listing page. "
                        "The session may have expired (got a login redirect) or "
                        "the page structure changed. "
                        f"Current URL: {current_url}"
                    ),
                }]

            jobs: List[Dict[str, Any]] = []
            seen: set = set()
            for url in links:
                if url in seen:
                    continue
                seen.add(url)
                detail = ctx.new_page()
                try:
                    detail.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    _wait_ready(detail, timeout_ms)
                    _dismiss_cookies(detail)
                    _expand_content(detail)
                    title = _extract_title(detail)
                    text  = _extract_overview(detail)
                    if len(text) < 100:
                        raise RuntimeError(
                            f"Text too short ({len(text)} chars) — possible login redirect. "
                            f"URL: {detail.url}"
                        )
                    jobs.append({
                        "source_id":   source.get("id", "inkopio"),
                        "source_type": "inkopio_playwright",
                        "source_url":  detail.url,
                        "title_hint":  title,
                        "company_hint": company_hint,
                        "text": text,
                    })
                except Exception as exc:
                    jobs.append({
                        "source_id":   source.get("id", "inkopio"),
                        "source_type": "inkopio_playwright",
                        "source_url":  url,
                        "title_hint":  None,
                        "company_hint": company_hint,
                        "text": "",
                        "error": str(exc),
                    })
                finally:
                    detail.close()
            return jobs
        finally:
            ctx.close()
