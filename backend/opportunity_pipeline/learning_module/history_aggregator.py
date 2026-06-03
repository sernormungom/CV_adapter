"""
Reads a consultant's verdict history and formats it for the config-update LLM prompt.

Verdicts live in data/application_tracker/{consultant_id}_verdicts.json keyed by job_id.
We group them by cycle_id, keep the most recent N cycles, and render a compact,
human-readable summary the LLM can reason over.
"""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List

from backend.config import APPLICATION_TRACKER_DIR


def _verdicts_path(consultant_id: str) -> Path:
    return APPLICATION_TRACKER_DIR / f"{consultant_id}_verdicts.json"


def load_verdicts(consultant_id: str) -> Dict[str, Any]:
    path = _verdicts_path(consultant_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _group_by_cycle(verdicts: Dict[str, Any]) -> "OrderedDict[str, List[dict]]":
    """Group verdict entries by cycle_id, ordered by submitted_at (oldest first)."""
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for entry in verdicts.values():
        cycle = entry.get("cycle_id") or "uncycled"
        grouped[cycle].append(entry)

    def cycle_time(items: List[dict]) -> str:
        return min((i.get("submitted_at") or "") for i in items)

    ordered = OrderedDict(sorted(grouped.items(), key=lambda kv: cycle_time(kv[1])))
    return ordered


def aggregate_history(consultant_id: str, last_n_cycles: int = 5) -> dict:
    """
    Return {cycles: [...], totals: {...}} for the most recent N cycles.

    Each cycle: {cycle_id, verdicts: [{verdict, reason, title_guess, source_id,
    match_score, recommended_status, hard_blockers, soft_risks}, ...]}.
    """
    verdicts = load_verdicts(consultant_id)
    grouped = _group_by_cycle(verdicts)

    recent_items = list(grouped.items())[-last_n_cycles:]

    cycles: List[dict] = []
    totals = {"yes": 0, "no": 0, "maybe": 0}
    for cycle_id, entries in recent_items:
        entries = sorted(entries, key=lambda e: e.get("match_score") or 0.0, reverse=True)
        cycle_verdicts = []
        for e in entries:
            v = (e.get("verdict") or "").lower()
            if v in totals:
                totals[v] += 1
            cycle_verdicts.append({
                "verdict": v,
                "reason": e.get("reason") or "",
                "title_guess": e.get("title_guess") or "",
                "source_id": e.get("source_id") or "",
                "match_score": e.get("match_score"),
                "recommended_status": e.get("recommended_status"),
                "hard_blockers": e.get("hard_blockers") or [],
                "soft_risks": e.get("soft_risks") or [],
            })
        cycles.append({"cycle_id": cycle_id, "verdicts": cycle_verdicts})

    return {"cycles": cycles, "totals": totals, "total_verdicts": sum(totals.values())}


def format_history_for_prompt(history: dict) -> str:
    """Render aggregated history into a compact text block for the LLM prompt."""
    cycles = history.get("cycles") or []
    if not cycles:
        return "(no verdict history yet)"

    lines: List[str] = []
    totals = history.get("totals", {})
    lines.append(
        f"Overall across {len(cycles)} cycle(s): "
        f"{totals.get('yes', 0)} yes, {totals.get('no', 0)} no, {totals.get('maybe', 0)} maybe."
    )
    lines.append("")

    for cycle in cycles:
        lines.append(f"=== Cycle {cycle['cycle_id']} ===")
        for v in cycle["verdicts"]:
            score = v["match_score"]
            score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "n/a"
            verdict_label = (v["verdict"] or "?").upper()
            line = (
                f"  [{verdict_label}] {v['title_guess'] or '(untitled)'} "
                f"({v['source_id'] or 'unknown'}) — score {score_str}, "
                f"recommended: {v['recommended_status'] or 'n/a'}"
            )
            lines.append(line)
            if v["reason"]:
                lines.append(f"      reason: {v['reason']}")
            if v["hard_blockers"]:
                lines.append(f"      hard_blockers: {', '.join(map(str, v['hard_blockers']))}")
            if v["soft_risks"]:
                lines.append(f"      soft_risks: {', '.join(map(str, v['soft_risks']))}")
        lines.append("")

    return "\n".join(lines).rstrip()
