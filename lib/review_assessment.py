"""Versioned review facts and conservative archive classification."""

import json
import re
import urllib.parse

PROTOCOL_V2 = {"review_output_version": 2, "aggregation_version": 2}
COVERAGE = {"covered", "not-covered", "unknown", "disputed"}
CAUSES = {
    "contribution-covered", "value-insufficient", "evidence-insufficient",
    "design-invalid", "infeasible", "unknown",
}
REENTRY_CATEGORIES = {"design-fixable", "evidence-incomplete"}


def _closed_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate review JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("nonfinite review JSON value")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_closed_object,
                      parse_constant=_invalid_constant)


def protocol_bytes():
    return (json.dumps(PROTOCOL_V2, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate_protocol(value):
    if (not isinstance(value, dict) or value != PROTOCOL_V2
            or any(type(item) is not int for item in value.values())):
        raise ValueError("review protocol is invalid")
    return value


def protocol_version(raw):
    """Absence is legacy; a present host protocol must be canonical v2."""
    if raw is None:
        return 1
    value = _json(raw)
    validate_protocol(value)
    if raw != protocol_bytes():
        raise ValueError("review protocol is not canonical")
    return 2


def _references(value, sources):
    if not isinstance(value, list):
        raise ValueError("review evidence must be a list")
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {"source", "quote"}
                or not isinstance(ref["source"], str) or ref["source"] not in sources
                or not isinstance(ref["quote"], str) or not ref["quote"].strip()
                or ref["quote"] not in sources[ref["source"]]):
            raise ValueError("review evidence is not a frozen-source quote")
    return value


def _urls(text):
    result = set()
    for value in re.findall(r'https?://[^\s<>"\']+', text):
        value = value.rstrip(".,;:)]}")
        try:
            if urllib.parse.urlsplit(value).netloc:
                result.add(value)
        except ValueError:
            pass
    return result


def parse(raw, *, verdict, candidate_markdown, prior_work):
    """Validate references, not the scientific truth of a cited interpretation."""
    if not isinstance(candidate_markdown, str) or not isinstance(prior_work, str):
        raise ValueError("review assessment needs frozen source text")
    value = _json(raw)
    if (not isinstance(value, dict)
            or set(value) != {"coverage", "coverage_evidence", "causes"}
            or not isinstance(value["coverage"], str) or value["coverage"] not in COVERAGE
            or not isinstance(value["causes"], list)):
        raise ValueError("review assessment schema is invalid")
    sources = {"candidate": candidate_markdown, "prior-work": prior_work}
    refs = _references(value["coverage_evidence"], sources)
    coverage = value["coverage"]
    if coverage in {"covered", "not-covered"} and {ref["source"] for ref in refs} != set(sources):
        raise ValueError("coverage requires candidate and prior-work evidence")
    if coverage == "covered" and not any(
            _urls(ref["quote"]) & _urls(prior_work)
            for ref in refs if ref["source"] == "prior-work"):
        raise ValueError("covered contribution requires a supplied work URL")
    if coverage == "disputed" and len({(ref["source"], ref["quote"]) for ref in refs}) < 2:
        raise ValueError("disputed coverage requires distinct evidence")
    codes = []
    for cause in value["causes"]:
        if (not isinstance(cause, dict) or set(cause) != {"code", "evidence"}
                or not isinstance(cause["code"], str) or cause["code"] not in CAUSES):
            raise ValueError("review cause schema is invalid")
        evidence = _references(cause["evidence"], sources)
        if cause["code"] != "unknown" and not evidence:
            raise ValueError("known review cause requires evidence")
        codes.append(cause["code"])
    if len(codes) != len(set(codes)) or ("unknown" in codes and len(codes) != 1):
        raise ValueError("review causes repeat or mix unknown with known facts")
    if (coverage == "covered") != ("contribution-covered" in codes):
        raise ValueError("coverage and contribution-covered cause differ")
    if verdict == "strong-accept":
        valid = not codes and coverage == "not-covered"
    elif verdict == "accept-w-rev":
        valid = bool(codes) and "unknown" not in codes and coverage == "not-covered"
    elif verdict == "reject":
        valid = bool(codes) and ("unknown" not in codes or coverage in {"unknown", "disputed"})
    else:
        valid = False
    if not valid:
        raise ValueError("review assessment contradicts its verdict")
    return value


def aggregate_coverage(assessments):
    values = {item["coverage"] for item in assessments}
    if "disputed" in values or {"covered", "not-covered"} <= values:
        return "disputed"
    if "covered" in values:
        return "covered"
    if "not-covered" in values:
        return "not-covered"
    return "unknown"


def legacy_category(*, final_rank, raw_min, downgraded, overlap):
    if final_rank == 2:
        return "-"
    if downgraded:
        return "evidence-incomplete"
    if overlap == "high":
        return "novelty-dead"
    if raw_min == 1 and overlap == "low":
        return "design-fixable"
    if raw_min == 1:
        return "ceiling-limited"
    return "novelty-dead"


def classify(*, final_rank, raw_min, downgraded, overlap, assessments):
    if final_rank == 2:
        return "-"
    if downgraded:
        return "evidence-incomplete"
    coverage = aggregate_coverage(assessments)
    if coverage == "disputed":
        return "review-unresolved"
    if final_rank == 0 and coverage == "covered":
        return "novelty-dead"
    if raw_min == 1:
        return "design-fixable" if overlap == "low" else "ceiling-limited"
    return "review-unresolved"


def eligible_for_reentry(*, final_rank, raw_min, downgraded, overlap, category,
                         sa_votes, story_count_after_append):
    legacy = legacy_category(final_rank=final_rank, raw_min=raw_min,
                             downgraded=downgraded, overlap=overlap)
    return (final_rank != 2 and legacy in REENTRY_CATEGORIES
            and category in REENTRY_CATEGORIES and sa_votes >= 1
            and story_count_after_append < 2)
