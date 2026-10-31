"""Conservative, review-only checks for the saved 8B couch quality pilot.

These rules use seller text and the model's stated photo evidence. They do not
claim to verify a couch from a catalog photo or change live price estimates.
"""

from __future__ import annotations

import re


GRADE_ORDER = {"poor": 0, "fair": 1, "good": 2, "like_new": 3, "new": 4}

SEVERE = re.compile(
    r"\b(?:feel (?:some |the )?springs?|springs? (?:can be felt|poking|exposed)|"
    r"broken (?:frame|recliner|mechanism|support|spring)|frame (?:is )?(?:broken|cracked)|"
    r"(?:collapsed|unusable|doesn['’]?t recline|won['’]?t recline))\b", re.I
)
MODERATE = re.compile(
    r"\b(?:holes?|tears?|rips?|stains?|scratches?|(?:un[ -]?attached|detached) cushions?|"
    r"cushions? (?:is |are )?(?:coming )?(?:un[ -]?attached|detached)|"
    r"(?:fading|faded|discoloration|discolored)|(?:pet|cat|dog) damage)\b", re.I
)
MINOR = re.compile(r"\b(?:snags?|scuffs?|pilling|dirty|worn fabric|light wear)\b", re.I)
NEW_CLAIM = re.compile(r"\b(?:brand[ -]?new|never used|unused|still in (?:the )?box)\b", re.I)
LIKE_NEW_CLAIM = re.compile(r"\b(?:like[ -]?new|barely used|used (?:only )?once)\b", re.I)
GOOD_CLAIM = re.compile(r"\b(?:excellent condition|very good condition|good condition)\b", re.I)
NEGATED_DEFECTS = re.compile(
    r"\b(?:no|without)\s+(?:(?:visible|major|obvious|any)\s+)?"
    r"(?:holes?|tears?|rips?|stains?|scratches?|damage)"
    r"(?:\s*(?:,|and|or|/)\s*(?:holes?|tears?|rips?|stains?|scratches?|damage))*\b",
    re.I,
)


def _cap(grade: str, maximum: str) -> str:
    if grade not in GRADE_ORDER:
        return maximum
    return grade if GRADE_ORDER[grade] <= GRADE_ORDER[maximum] else maximum


def apply_quality_guardrails(assessment: dict, title: str, description: str) -> dict:
    """Return explainable quality guidance without modifying model output."""
    raw_grade = str(assessment.get("quality_grade") or "unknown").lower()
    if raw_grade not in GRADE_ORDER:
        raw_grade = "unknown"
    raw_confidence = str(assessment.get("confidence") or "low").lower()
    if raw_confidence not in {"low", "medium", "high"}:
        raw_confidence = "low"

    photo_kind = str(assessment.get("photo_kind") or "unclear")
    actual_photo = photo_kind == "seller_photo"
    catalog = photo_kind == "catalog_or_graphic"
    photo_evidence = [str(x).strip() for x in assessment.get("photo_evidence") or [] if str(x).strip()]
    visible_issues = [str(x).strip() for x in assessment.get("visible_issues") or [] if str(x).strip()]
    # The model's interpretation of seller text is not independent evidence.
    seller_text = NEGATED_DEFECTS.sub(" ", f"{title}. {description}")
    photo_issues = NEGATED_DEFECTS.sub(" ", " ".join(visible_issues + photo_evidence)) if actual_photo else ""
    evidence_text = f"{seller_text} {photo_issues}"
    severe = bool(SEVERE.search(evidence_text))
    moderate = bool(MODERATE.search(evidence_text))
    minor = bool(MINOR.search(evidence_text))
    defect = severe or moderate or minor
    reasons: list[str] = []

    if severe:
        grade = _cap(raw_grade, "poor")
        reasons.append("Seller or actual-item photo reports a functional or support defect; cap at Poor.")
    elif moderate:
        grade = _cap(raw_grade, "fair")
        reasons.append("Seller or actual-item photo reports damage such as a hole, stain, scratch, pet damage, or detached cushion; cap at Fair.")
    elif minor:
        grade = _cap(raw_grade, "good")
        reasons.append("Minor wear is reported; cap at Good.")
    else:
        grade = raw_grade

    has_photo_evidence = actual_photo and bool(photo_evidence or visible_issues)
    has_condition_claim = bool(NEW_CLAIM.search(seller_text) or LIKE_NEW_CLAIM.search(seller_text)
                               or GOOD_CLAIM.search(seller_text))
    if catalog:
        reasons.append("Catalog-style photo cannot verify the actual couch's condition or color.")
        if not defect:
            grade = "unknown"
    elif not defect and not (has_photo_evidence or has_condition_claim):
        grade = "unknown"
        reasons.append("No specific condition evidence from the seller or actual-item photo.")

    if grade == "new" and not (actual_photo and NEW_CLAIM.search(seller_text)):
        grade = "unknown"
        reasons.append("New requires a seller claim and a photo of the actual item.")
    elif grade == "like_new" and not (actual_photo and LIKE_NEW_CLAIM.search(seller_text)):
        grade = "unknown"
        reasons.append("Like new requires an explicit seller claim and a photo of the actual item.")

    # A confident model answer alone is not corroboration. Actual-item photo
    # and seller details must agree before allowing high confidence.
    if grade == "unknown" or catalog or (not has_photo_evidence and not defect):
        confidence = "low"
    elif has_photo_evidence and (defect or has_condition_claim):
        confidence = raw_confidence
    else:
        confidence = "medium" if raw_confidence == "high" else raw_confidence
    if confidence != raw_confidence:
        reasons.append("Confidence reduced because the photo and seller details do not independently support the grade.")

    color = str(assessment.get("color") or "unknown").strip() or "unknown"
    if catalog:
        color = "unknown"
    return {
        "model_grade": raw_grade,
        "guarded_grade": grade,
        "model_confidence": raw_confidence,
        "guarded_confidence": confidence,
        "color_for_matching": color,
        "review_required": catalog or grade == "unknown" or grade != raw_grade,
        "reasons": reasons,
        "condition_cohort_eligible": (
            grade in {"new", "like_new", "good", "fair"}
            and confidence in {"medium", "high"} and actual_photo
        ),
    }
