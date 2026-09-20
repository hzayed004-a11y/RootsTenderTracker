"""Classification pipeline.

Three decisions, in order:

  1. is_pharmaceutical  -- keep or discard the tender
  2. therapeutic_area   -- Oncology, Hematology, ... , Other Pharmaceutical
  3. priority           -- High / Medium / Low, configurable

Every decision returns the evidence that produced it. Nothing is discarded on
a low-confidence negative: borderline cases are routed to the manual review
queue with the reason attached, per the "never silently drop" requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import terminology as T
from .normalize import fold, tokens


@dataclass
class Verdict:
    is_pharma: bool = False
    is_device: bool = False
    is_oncology: bool = False
    therapeutic_area: str = "Other Pharmaceutical"
    priority: str = "Low"
    priority_score: float = 0.0
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if msg not in self.evidence:
            self.evidence.append(msg)

    @property
    def evidence_text(self) -> str:
        return "; ".join(self.evidence)


def _hits(folded: str, index_name: str) -> list[str]:
    """Match against a pre-folded lexicon index. `folded` must come from fold()."""
    long_terms, rx = T.INDEX[index_name]
    found = {t for t in long_terms if t in folded}
    if rx:
        found.update(rx.findall(folded))
    return sorted(found)


def _area_hits(folded: str, area: str) -> list[str]:
    long_terms, rx = T.AREA_INDEX[area]
    found = {t for t in long_terms if t in folded}
    if rx:
        found.update(rx.findall(folded))
    return sorted(found)


def _suffix_hits(toks: list[str]) -> list[str]:
    out = []
    for tok in toks:
        if len(tok) < 7:
            continue
        for suf in T.ONCOLOGY_SUFFIXES:
            if tok.endswith(suf):
                out.append(tok)
                break
    return sorted(set(out))


# --------------------------------------------------------------------------


def score_oncology(text: str) -> tuple[float, list[str]]:
    """Return (0..1 score, evidence). Multiple weak signals compound."""
    folded = fold(text)
    toks = tokens(text)
    score, ev = 0.0, []

    ctx = _hits(folded, "onc_context")
    if ctx:
        score += min(0.6, 0.32 * len(ctx))
        ev.append(f"oncology context: {', '.join(ctx[:5])}")

    ind = _hits(folded, "onc_indication")
    if ind:
        score += min(0.7, 0.36 * len(ind))
        ev.append(f"cancer indication: {', '.join(ind[:5])}")

    mol = _hits(folded, "onc_molecule")
    if mol:
        score += min(0.9, 0.55 * len(mol))
        ev.append(f"antineoplastic molecule: {', '.join(mol[:6])}")

    mab = _hits(folded, "onc_mab")
    if mab:
        score += min(0.9, 0.55 * len(mab))
        ev.append(f"oncology monoclonal: {', '.join(mab[:6])}")

    # An INN stem is strong standalone evidence: no non-oncology molecule
    # ends in -tinib, -parib or -ciclib. Weighted just below a confirmed name.
    suf = _suffix_hits(toks)
    if suf:
        score += min(0.85, 0.50 * len(suf))
        ev.append(f"INN stem match: {', '.join(suf[:6])}")

    # Supportive care counts, but only pushes over the line alongside other
    # signals -- otherwise every antiemetic order becomes an oncology tender.
    sup = _hits(folded, "onc_supportive")
    if sup:
        score += min(0.30, 0.15 * len(sup))
        ev.append(f"supportive oncology agent: {', '.join(sup[:5])}")

    return min(score, 1.0), ev


def classify_therapeutic_area(text: str) -> tuple[str, float, list[str]]:
    folded = fold(text)
    best_area, best_hits = "Other Pharmaceutical", []
    for area in T.AREA_INDEX:
        hit = _area_hits(folded, area)
        if len(hit) > len(best_hits):
            best_area, best_hits = area, hit
    if not best_hits:
        return "Other Pharmaceutical", 0.25, []
    conf = min(0.95, 0.45 + 0.18 * len(best_hits))
    return best_area, conf, [f"{best_area}: {', '.join(best_hits[:5])}"]


def classify(text: str, *, config: dict | None = None) -> Verdict:
    """`text` should concatenate title + description + item names + doc text."""
    config = config or {}
    onc_threshold = float(config.get("oncology_threshold", 0.45))
    onc_review_floor = float(config.get("oncology_review_floor", 0.22))

    v = Verdict()
    folded = fold(text)
    if not folded:
        v.needs_review = True
        v.review_reason = "No text could be extracted for classification"
        return v

    pharma = _hits(folded, "pharma")
    nonpharma = _hits(folded, "non_pharma")
    device = _hits(folded, "device")

    onc_score, onc_ev = score_oncology(text)
    area, area_conf, area_ev = classify_therapeutic_area(text)

    # --- step 1: pharmaceutical? ------------------------------------------
    # A molecule name alone is sufficient: "Supply of Pembrolizumab 100mg"
    # contains no word from PHARMA_TERMS.
    if pharma:
        v.is_pharma = True
        v.note(f"pharma terms: {', '.join(pharma[:5])}")
    if onc_score >= onc_review_floor:
        v.is_pharma = True
        v.note("molecule/indication evidence implies a medicine")
    if area != "Other Pharmaceutical":
        # A named molecule in any therapeutic area is a medicine, even when
        # the listing never uses a word like "pharmaceutical" or "tablet".
        v.is_pharma = True

    if device:
        v.is_device = True
        v.note(f"device/consumable terms: {', '.join(device[:4])}")

    if nonpharma and not v.is_pharma:
        v.review_reason = f"Non-pharmaceutical procurement ({', '.join(nonpharma[:3])})"
        v.confidence = 0.8
        return v

    if not v.is_pharma:
        if v.is_device:
            v.review_reason = ("Medical device / consumable, no pharmaceutical "
                               "signal detected")
            v.needs_review = bool(config.get("review_device_tenders", False))
            v.confidence = 0.6
        else:
            v.needs_review = True
            v.review_reason = "Could not determine whether tender is pharmaceutical"
            v.confidence = 0.2
        return v

    # --- step 2: oncology / therapeutic area ------------------------------
    for e in onc_ev:
        v.note(e)

    if onc_score >= onc_threshold:
        v.is_oncology = True
        v.therapeutic_area = "Oncology"
        v.confidence = min(0.97, 0.5 + onc_score / 2)
        # Hemato-oncology is oncology, but tag it so it can be reported apart.
        if any(k in folded for k in ("leukaemia", "leukemia", "lymphoma",
                                     "myeloma", "myelodysplastic", "haemato",
                                     "hemato", "لوكيميا", "ليمفوما")):
            v.note("hematology-oncology")
    elif onc_score >= onc_review_floor:
        v.is_oncology = False
        v.needs_review = True
        v.review_reason = (f"Possible oncology relevance (score {onc_score:.2f}) "
                           f"below threshold {onc_threshold:.2f} - confirm manually")
        v.therapeutic_area, v.confidence = area, min(area_conf, 0.5)
        for e in area_ev:
            v.note(e)
    else:
        v.therapeutic_area, v.confidence = area, area_conf
        for e in area_ev:
            v.note(e)
        if area == "Other Pharmaceutical":
            v.needs_review = bool(config.get("review_unclassified", True))
            if v.needs_review:
                v.review_reason = "Pharmaceutical but therapeutic area unclear"

    # --- step 3: priority --------------------------------------------------
    v.priority, v.priority_score = score_priority(text, v, config)
    return v


def score_priority(text: str, v: Verdict, config: dict) -> tuple[str, float]:
    """Configurable. `config['portfolio']` is a list of your target molecules."""
    folded = fold(text)
    weights = config.get("priority_weights", {})
    score = 0.0

    if v.is_oncology:
        score += float(weights.get("oncology", 50))

    portfolio = [fold(p) for p in config.get("portfolio", []) if p]
    matched = [p for p in portfolio if p and p in folded]
    if matched:
        score += float(weights.get("portfolio_match", 35))
        v.note(f"portfolio match: {', '.join(matched[:5])}")

    specialty = config.get("specialty_areas",
                           ["Hematology", "Rare Disease", "Immunology"])
    if v.therapeutic_area in specialty:
        score += float(weights.get("specialty_area", 22))

    if _hits(folded, "commodity"):
        score += float(weights.get("commodity_penalty", -18))
        v.note("commodity/generic product")

    if v.is_device and not v.is_oncology:
        score += float(weights.get("device_penalty", -12))

    high = float(config.get("high_priority_cutoff", 45))
    medium = float(config.get("medium_priority_cutoff", 15))
    if score >= high:
        return "High", score
    if score >= medium:
        return "Medium", score
    return "Low", score


def apply_value_boost(score: float, value_kwd: float | None, config: dict) -> float:
    """Applied after estimated value is known (often only from documents)."""
    if value_kwd is None:
        return score
    threshold = float(config.get("high_value_threshold_kwd", 100_000))
    if value_kwd >= threshold:
        return score + float(config.get("priority_weights", {}).get("high_value", 20))
    return score
