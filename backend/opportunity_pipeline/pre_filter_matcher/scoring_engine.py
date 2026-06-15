"""
Deterministic scoring engine.

For each active job in the Job Store, extracts explicit terms from the raw
text, matches them against the consultant's evidence blocks, and computes
five fit dimensions weighted by matching_config.yaml.

Scoring dimensions:
  expertise_fit  — term coverage in consultant's evidence
  role_fit       — archetype + title + high-signal term density
  growth_fit     — growth signals present in job text
  interest_fit   — preferred/avoid work-shape signals
  practical_fit  — location / work-mode score

overall_score = weighted sum of five dimensions - risk_deduction
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _in_text(pattern: str, text_folded: str) -> bool:
    escaped = re.escape(_fold(pattern))
    return bool(re.search(r"\b" + escaped + r"\b", text_folded))


# ---------------------------------------------------------------------------
# Evidence index builder
# ---------------------------------------------------------------------------

def build_evidence_index(profile_yaml: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Flatten all terms from role_groups → blocks into a searchable index.
    Returns {category: [term, ...]} with all terms the consultant has evidence for.
    """
    index: Dict[str, set] = {
        "languages": set(), "tools": set(), "methods": set(),
        "standards": set(), "verification": set(), "domains": set(), "evidence_text": set(),
    }

    for rg in (profile_yaml.get("career_history") or {}).get("role_groups") or []:
        for block in rg.get("blocks") or []:
            for lang in block.get("languages") or []:
                index["languages"].add(_fold(lang))
            for tool in block.get("tools") or []:
                index["tools"].add(_fold(tool))
            for std in block.get("processes_standards") or []:
                index["methods"].add(_fold(std))
            for vv in block.get("verification_validation") or []:
                index["verification"].add(_fold(vv))
            for domain in block.get("domains") or []:
                index["domains"].add(_fold(domain))
            for ev in block.get("evidence_items") or []:
                text = ev.get("text")
                if text:
                    index["evidence_text"].add(_fold(text))

    return {k: list(v) for k, v in index.items()}


# ---------------------------------------------------------------------------
# Term extraction from job text
# ---------------------------------------------------------------------------

def extract_job_terms(text: str, term_catalogs: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Extract explicit terms from raw job text using the catalog from matching_config.
    Returns {category: [matched_term, ...]} for found terms.
    """
    folded = _fold(text)
    found: Dict[str, List[str]] = {}

    for category, tiers in term_catalogs.items():
        hits = []
        if isinstance(tiers, dict):
            for tier_name, terms in tiers.items():
                for term in terms or []:
                    if _in_text(term, folded):
                        hits.append(term)
        elif isinstance(tiers, list):
            for term in tiers or []:
                if _in_text(term, folded):
                    hits.append(term)
        if hits:
            found[category] = hits

    return found


# ---------------------------------------------------------------------------
# Match terms against evidence
# ---------------------------------------------------------------------------

def _term_weight(term: str, tier_map: Dict[str, List[str]]) -> float:
    """Return signal weight for a term based on which tier it's in."""
    f = _fold(term)
    high = [_fold(t) for t in (tier_map.get("high_signal") or [])]
    standard = [_fold(t) for t in (tier_map.get("standard") or [])]
    low = [_fold(t) for t in (tier_map.get("low_signal") or [])]
    if f in high:
        return 1.45
    if f in standard:
        return 1.00
    if f in low:
        return 0.35
    return 1.00


def match_terms_to_evidence(
    job_terms: Dict[str, List[str]],
    evidence_index: Dict[str, List[str]],
    term_catalogs: Dict[str, Any],
) -> Tuple[float, int, int]:
    """
    Returns (weighted_match_score 0-100, matched_count, total_count).
    Weighted match = sum(weight * 1.0 for matched terms) / sum(weight * 1.0 for all job terms) * 100
    """
    total_weight = 0.0
    matched_weight = 0.0

    for category, terms in job_terms.items():
        tier_map = term_catalogs.get(category) or {}
        evidence = evidence_index.get(category) or []
        evidence_text = evidence_index.get("evidence_text") or []

        for term in terms:
            weight = _term_weight(term, tier_map if isinstance(tier_map, dict) else {})
            total_weight += weight
            folded_term = _fold(term)
            if any(folded_term in ev or ev in folded_term for ev in evidence + evidence_text):
                matched_weight += weight

    if total_weight == 0:
        return 0.0, 0, 0

    score = min(100.0, (matched_weight / total_weight) * 100.0)
    return score, int(matched_weight), int(total_weight)


# ---------------------------------------------------------------------------
# Five fit dimensions
# ---------------------------------------------------------------------------

def score_expertise_fit(
    job_terms: Dict[str, List[str]],
    evidence_index: Dict[str, List[str]],
    term_catalogs: Dict[str, Any],
) -> float:
    """Expertise = 75% tools + 25% domains (both scored via term coverage)."""
    tool_terms = {k: v for k, v in job_terms.items() if k in ("languages", "tools", "methods", "standards", "verification")}
    domain_terms = {k: v for k, v in job_terms.items() if k == "domains"}

    tool_score, _, _ = match_terms_to_evidence(tool_terms, evidence_index, term_catalogs)
    domain_score, _, _ = match_terms_to_evidence(domain_terms, evidence_index, term_catalogs)

    return tool_score * 0.75 + domain_score * 0.25


def score_role_fit(text_folded: str, job_terms: Dict[str, List[str]], config: Dict[str, Any]) -> float:
    """
    Role fit = archetype alignment + seniority bonus + high-signal term density.
    """
    archetypes = config.get("role_archetypes") or []
    best_score = 0.0

    for arch in archetypes:
        arch_score = 0.0
        title_patterns = [_fold(p) for p in (arch.get("title_patterns") or [])]
        body_patterns = [_fold(p) for p in (arch.get("body_patterns") or [])]
        seniority_bonus = float(arch.get("seniority_bonus") or 0.0)

        title_hits = sum(1 for p in title_patterns if re.search(r"\b" + re.escape(p) + r"\b", text_folded[:200]))
        body_hits = sum(1 for p in body_patterns if re.search(r"\b" + re.escape(p) + r"\b", text_folded))
        total_patterns = len(title_patterns) + len(body_patterns)

        if total_patterns > 0:
            arch_score = min(90.0, ((title_hits * 2 + body_hits) / (total_patterns + len(title_patterns))) * 100.0)
            if title_hits > 0:
                arch_score += seniority_bonus * 100
            arch_score = min(100.0, arch_score)

        if arch_score > best_score:
            best_score = arch_score

    # High-signal term density bonus (caps at +15)
    high_signal_cats = {k: v for k, v in job_terms.items()
                        if k in ("languages", "tools", "standards", "verification")}
    total_high_signal = sum(len(terms) for terms in high_signal_cats.values())
    density_bonus = min(15.0, total_high_signal * 2.5)

    return min(100.0, best_score + density_bonus)


def score_growth_fit(text_folded: str, config: Dict[str, Any]) -> float:
    """Growth fit = primary signals weighted 1.0, secondary 0.5, penalize -0.3."""
    growth = config.get("growth_signals") or {}
    primary = [_fold(s) for s in (growth.get("primary") or [])]
    secondary = [_fold(s) for s in (growth.get("secondary") or [])]
    penalize = [_fold(s) for s in (growth.get("penalize") or [])]

    score = 0.0
    for sig in primary:
        if re.search(r"\b" + re.escape(sig) + r"\b", text_folded):
            score += 20.0
    for sig in secondary:
        if re.search(r"\b" + re.escape(sig) + r"\b", text_folded):
            score += 8.0
    for sig in penalize:
        if re.search(r"\b" + re.escape(sig) + r"\b", text_folded):
            score -= 15.0

    return max(0.0, min(100.0, score))


def score_interest_fit(text_folded: str, config: Dict[str, Any]) -> float:
    """Interest fit = preferred work-shape signals vs. avoid signals."""
    interest = config.get("interest_signals") or {}
    preferred = [_fold(s) for s in (interest.get("preferred") or [])]
    avoid = [_fold(s) for s in (interest.get("avoid") or [])]

    score = 50.0  # neutral baseline
    for sig in preferred:
        if re.search(r"\b" + re.escape(sig) + r"\b", text_folded):
            score += 7.0
    for sig in avoid:
        if re.search(r"\b" + re.escape(sig) + r"\b", text_folded):
            score -= 12.0

    return max(0.0, min(100.0, score))


def score_practical_fit(text_folded: str, config: Dict[str, Any]) -> float:
    """Practical fit from location/work-mode rules."""
    loc_scores = config.get("location_scores") or {}
    rules = loc_scores.get("rules") or []
    default = float(loc_scores.get("default_score") or 60)

    for rule in rules:
        pattern = _fold(str(rule.get("match") or ""))
        if pattern and re.search(re.escape(pattern), text_folded):
            return float(rule.get("score") or default)
    return default


def compute_blockers(text_folded: str, config: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (hard_blockers_found, soft_risks_found)."""
    blockers_cfg = config.get("blockers") or {}
    hard_patterns = [_fold(b) for b in (blockers_cfg.get("hard") or [])]
    soft_patterns = [_fold(b) for b in (blockers_cfg.get("soft") or [])]

    hard_found = [p for p in hard_patterns if re.search(re.escape(p), text_folded)]
    soft_found = [p for p in soft_patterns if re.search(re.escape(p), text_folded)]
    return hard_found, soft_found


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_job(
    job: Dict[str, Any],
    profile_yaml: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Score a single job against the consultant's profile and config.

    Returns a score_result dict with:
      overall_score, score_breakdown, recommended_status,
      hard_blockers, soft_risks, job_terms_found
    """
    text = job.get("raw_text") or ""
    text_folded = _fold(text)

    evidence_index = build_evidence_index(profile_yaml)
    term_catalogs = config.get("term_catalogs") or {}
    job_terms = extract_job_terms(text, term_catalogs)
    weights = config.get("scoring_weights") or {}

    expertise = score_expertise_fit(job_terms, evidence_index, term_catalogs)
    role = score_role_fit(text_folded, job_terms, config)
    growth = score_growth_fit(text_folded, config)
    interest = score_interest_fit(text_folded, config)
    practical = score_practical_fit(text_folded, config)

    hard_blockers, soft_risks = compute_blockers(text_folded, config)

    risk_deduction = len(hard_blockers) * 70.0 + len(soft_risks) * 15.0

    raw_score = (
        expertise * float(weights.get("expertise_fit", 0.30))
        + role * float(weights.get("role_fit", 0.30))
        + growth * float(weights.get("growth_fit", 0.20))
        + interest * float(weights.get("interest_fit", 0.15))
        + practical * float(weights.get("practical_fit", 0.05))
    )
    raw_score -= risk_deduction * 0.35
    overall = max(0.0, min(100.0, raw_score))

    thresholds = config.get("thresholds") or {}
    keep_threshold = int(thresholds.get("keep", 76))
    maybe_threshold = int(thresholds.get("maybe", 58))

    if hard_blockers:
        status = "reject"
    elif overall >= keep_threshold:
        status = "keep"
    elif overall >= maybe_threshold:
        status = "maybe"
    else:
        status = "reject"

    return {
        "overall_score": round(overall, 1),
        "score_breakdown": {
            "expertise_fit": round(expertise, 1),
            "role_fit": round(role, 1),
            "growth_fit": round(growth, 1),
            "interest_fit": round(interest, 1),
            "practical_fit": round(practical, 1),
            "risk_deduction": round(risk_deduction * 0.35, 1),
        },
        "recommended_status": status,
        "hard_blockers": hard_blockers,
        "soft_risks": soft_risks,
        "job_terms_found": {k: v for k, v in job_terms.items()},
    }
