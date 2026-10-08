#!/usr/bin/env python3
"""Host-owned stage output contracts shared by the portable runtime."""

import json
import re
import urllib.parse

try:
    from lib import direction_contract as direction_contract_lib
    from lib import review_assessment
except ImportError:
    import direction_contract as direction_contract_lib
    import review_assessment


class StageError(RuntimeError):
    pass


_MODEL_ARTIFACTS = {
    # Generate: model selects enum themes; host renders markdown and TSV.
    "generate": (
        ("generation-candidates-json", "output/ideas.md", 65536),
    ),
    "history-compare": (
        (
            "history-comparison-json",
            "output/history-comparison.json",
            65536,
        ),
    ),
    # Review: model writes markdown only. Host projects verdict.tsv.
    "review": (
        ("review-markdown", "output/review.md", 65536),
    ),
    "meta": (
        (
            "failure-distillation-json",
            "output/failure-distillation.json",
            65536,
        ),
    ),
}
# Independent of interpreter-specific json decoder recursion behavior.
JSON_MAX_NESTING_DEPTH = 128


def _validate_json_nesting(raw):
    stack = bytearray()
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            elif byte < 0x20:
                raise ValueError("invalid control character in JSON string")
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            if len(stack) >= JSON_MAX_NESTING_DEPTH:
                raise ValueError("JSON nesting depth exceeds its bound")
            stack.append(byte)
        elif byte == 0x5D:
            if not stack or stack.pop() != 0x5B:
                raise ValueError("JSON nesting is unbalanced")
        elif byte == 0x7D:
            if not stack or stack.pop() != 0x7B:
                raise ValueError("JSON nesting is unbalanced")
    if in_string or stack:
        raise ValueError("JSON nesting is unbalanced")


def _validate_theme_vocabulary(themes):
    if (
        type(themes) is not list
        or not themes
        or any(
            type(theme) is not str
            or not theme
            or theme != theme.strip()
            or any(character in theme for character in "\x00\t\r\n")
            for theme in themes
        )
        or len(set(themes)) != len(themes)
    ):
        raise StageError("generation theme vocabulary is missing or invalid")


def generation_theme_vocabulary(policy):
    """Read the first nonempty vocabulary paragraph from the captured policy."""
    themes = []
    inside = False
    for line in policy.splitlines():
        if line.rstrip() == "## Theme Vocabulary":
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            if themes and not line.strip():
                break
            themes.extend(
                value.strip() for value in line.split("/") if value.strip()
            )
    _validate_theme_vocabulary(themes)
    return themes


def generation_content_schema(themes):
    _validate_theme_vocabulary(themes)
    return {
        "additionalProperties": False,
        "type": "object",
        "required": ["assumption_removal_attempt", "candidates"],
        "properties": {
            "assumption_removal_attempt": {
                "type": "string", "minLength": 1, "maxLength": 4096,
            },
            "candidates": {
                "type": "array", "minItems": 1, "maxItems": 20,
                "items": {
                    "additionalProperties": False,
                    "type": "object",
                    "required": ["theme", "markdown"],
                    "properties": {
                        "theme": {"type": "string", "enum": list(themes)},
                        "markdown": {
                            "type": "string", "minLength": 1,
                            "maxLength": 65536,
                        },
                    },
                },
            },
        },
    }


def validate_generation_content(value, themes):
    """Validate the closed typed payload independently of provider decoding."""
    _validate_theme_vocabulary(themes)
    if (
        type(value) is not dict
        or set(value) != {"assumption_removal_attempt", "candidates"}
        or type(value.get("assumption_removal_attempt")) is not str
        or not 1 <= len(value["assumption_removal_attempt"]) <= 4096
        or type(value.get("candidates")) is not list
        or not 1 <= len(value["candidates"]) <= 20
    ):
        raise StageError("generation content shape is invalid")
    for candidate in value["candidates"]:
        if (
            type(candidate) is not dict
            or set(candidate) != {"theme", "markdown"}
            or type(candidate.get("theme")) is not str
            or candidate["theme"] not in themes
            or type(candidate.get("markdown")) is not str
            or not 1 <= len(candidate["markdown"]) <= 65536
        ):
            raise StageError("generation candidate shape or theme is invalid")


def render_generation_markdown(value, themes):
    """Render model-selected themes and bodies without guessing or rewriting."""
    validate_generation_content(value, themes)
    marker = value["assumption_removal_attempt"]
    if (
        not marker.startswith("Assumption-Removal Attempt: ")
        or len(marker.splitlines()) != 1
        or any(character in marker for character in "\x00\r\n")
    ):
        raise StageError("generation assumption-removal marker is invalid")
    sections = []
    for index, candidate in enumerate(value["candidates"], 1):
        body = candidate["markdown"]
        if any(character in body for character in "\x00\r"):
            raise StageError("generation candidate body contains a control character")
        for line in body.splitlines():
            stripped = line.lstrip()
            if (
                stripped.startswith(("Theme:", "Assumption-Removal Attempt:"))
                or re.match(r"^#{1,6}(?:\s|$)", stripped)
            ):
                raise StageError(
                    "generation candidate body contains a reserved field or heading"
                )
        sections.append(f"## I{index}\nTheme: {candidate['theme']}\n{body}")
    markdown = marker + "\n\n" + "\n\n".join(sections) + "\n"
    if len(markdown.encode("utf-8")) > 65536:
        raise StageError("generation markdown exceeds its byte bound")
    return markdown


def stage_response_schema(stage, theme_vocabulary=None):
    """Return the strict one-message schema used by the canonicalizer."""
    try:
        artifacts = _MODEL_ARTIFACTS[stage]
    except KeyError as exc:
        raise ValueError("unsupported stage") from exc
    schema = {
        "additionalProperties": False,
        "properties": {
            "artifacts": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "artifact_kind": {
                            "enum": [item[0] for item in artifacts],
                            "type": "string",
                        },
                        "content": {
                            # JSON Schema counts code points, while the parser
                            # enforces the authoritative UTF-8 byte ceiling.
                            "maxLength": max(
                                item[2] for item in artifacts
                            ),
                            "minLength": 1,
                            "type": "string",
                        },
                    },
                    "required": ["artifact_kind", "content"],
                    "type": "object",
                },
                "maxItems": len(artifacts),
                "minItems": len(artifacts),
                "type": "array",
            },
            "schema_version": {
                "enum": [1],
                "type": "integer",
            },
            "stage": {
                "enum": [stage],
                "type": "string",
            },
        },
        "required": ["schema_version", "stage", "artifacts"],
        "type": "object",
    }
    if stage == "generate":
        schema["properties"]["artifacts"]["items"]["properties"]["content"] = (
            generation_content_schema(theme_vocabulary)
        )
    return schema


def parse_model_output(stage, raw, theme_vocabulary=None):
    """Validate one structured final message and return artifact bytes."""
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("model output bytes are invalid")
    try:
        _validate_json_nesting(raw)
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("model output is not UTF-8 JSON") from exc
    expected = _MODEL_ARTIFACTS.get(stage)
    if (
        expected is None
        or not isinstance(value, dict)
        or set(value) != {"schema_version", "stage", "artifacts"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("stage") != stage
        or not isinstance(value.get("artifacts"), list)
        or len(value["artifacts"]) != len(expected)
    ):
        raise ValueError("model output envelope is invalid")
    rendered = {}
    for item, (kind, path, maximum) in zip(
        value["artifacts"],
        expected,
    ):
        if (
            not isinstance(item, dict)
            or set(item) != {"artifact_kind", "content"}
            or item.get("artifact_kind") != kind
        ):
            raise ValueError("model artifact envelope is invalid")
        if stage == "generate":
            content = render_generation_markdown(
                item.get("content"), theme_vocabulary
            ).encode("utf-8")
        else:
            if not isinstance(item.get("content"), str):
                raise ValueError("model artifact envelope is invalid")
            content = item["content"].encode("utf-8")
        if not content or len(content) > maximum:
            raise ValueError("model artifact content is invalid")
        rendered[path] = content
    return rendered


def _evidence_urls(evidence):
    """Return normalized, parseable http(s) URLs with hostnames."""
    urls = set()
    for match in re.finditer(r"https?://[^\s|<>]+", evidence, re.IGNORECASE):
        candidate = match.group(0).rstrip(".,;:!?)]}'\"")
        try:
            parsed = urllib.parse.urlsplit(candidate)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            continue
        default_port = 80 if parsed.scheme.lower() == "http" else 443
        authority = hostname.lower()
        if port is not None and port != default_port:
            authority += f":{port}"
        urls.add(
            urllib.parse.urlunsplit(
                (
                    parsed.scheme.lower(),
                    authority,
                    parsed.path or "/",
                    parsed.query,
                    parsed.fragment,
                )
            )
        )
    return urls


def build_generation_tsv_from_markdown(markdown, direction_contract=None):
    """Validate generate markdown and return host-projected ideas.tsv text.

    The TSV index is derived from each rendered section's One-Sentence Story
    and model-selected Theme.
    """
    lines = markdown.splitlines()
    markers = [
        index
        for index, line in enumerate(lines)
        if line.startswith("Assumption-Removal Attempt:")
    ]
    headings = [
        (index, line[3:].strip())
        for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    if (
        len(markers) != 1
        or not headings
        or len(headings) > 20
        or markers[0] >= headings[0][0]
        or [identifier for _, identifier in headings]
        != [f"I{index}" for index in range(1, len(headings) + 1)]
    ):
        raise StageError("generation markdown section mismatch")
    if any(
        line.strip()
        for index, line in enumerate(lines[:headings[0][0]])
        if index != markers[0]
    ):
        raise StageError(
            "generation markdown has content outside candidate sections"
        )
    marker = lines[markers[0]]
    incomplete = marker.startswith(
        "Assumption-Removal Attempt: incomplete "
    )
    required = (
        "One-Sentence Story",
        "Theme",
        "Form",
        "Summary",
        "Minimal Falsification Experiment",
        "Why It May Be Novel",
    )
    assumption_required = (
        "Assumption to Remove",
        "Why It Can Be Removed Now",
        "Forcing Constraint",
    )
    direction_required = ()
    if direction_contract is not None:
        direction_required = (
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
        )
    assumption_ids = set()
    # id -> number of Crack Evidence rows with a parseable http(s) URL
    assumption_url_cracks = {}
    rows = []
    for position, (start, identifier) in enumerate(headings):
        end = (
            headings[position + 1][0]
            if position + 1 < len(headings)
            else len(lines)
        )
        values = {}
        crack_evidence = []
        for line in lines[start + 1:end]:
            stripped = line.strip()
            for label in (
                *required,
                *assumption_required,
                *direction_required,
            ):
                prefix = label + ":"
                if stripped.startswith(prefix):
                    if label in values:
                        raise StageError(
                            f"generation field is duplicated: "
                            f"{identifier} {label}"
                        )
                    value = stripped[len(prefix):].strip()
                    if not value:
                        raise StageError(
                            f"generation field is empty: "
                            f"{identifier} {label}"
                        )
                    if len(value.encode("utf-8")) > 4096:
                        raise StageError(
                            f"generation field exceeds bound: "
                            f"{identifier} {label}"
                        )
                    values[label] = value
            if stripped.startswith("Crack Evidence:"):
                evidence = stripped[len("Crack Evidence:"):].strip()
                if (
                    not evidence
                    or len(evidence.encode("utf-8")) > 2048
                ):
                    raise StageError(
                        f"generation crack evidence is invalid: "
                        f"{identifier}"
                    )
                crack_evidence.append(evidence)
        missing = [
            label
            for label in (*required, *direction_required)
            if label not in values
        ]
        if missing:
            raise StageError(
                f"generation field is missing: {identifier} "
                f"{', '.join(missing)}"
            )
        if direction_contract is not None:
            try:
                direction_contract_lib.validate_candidate_fields(
                    {
                        label: values[label]
                        for label in direction_required
                    },
                    direction_contract,
                    identifier,
                )
            except direction_contract_lib.DirectionContractError as exc:
                raise StageError(
                    f"generation direction fields are invalid: {identifier}"
                ) from exc
        story = values["One-Sentence Story"]
        theme = values["Theme"]
        if (
            len(story.encode("utf-8")) > 1024
            or len(theme.encode("utf-8")) > 1024
            or "\t" in story
            or "\t" in theme
            or "\n" in story
            or "\n" in theme
        ):
            raise StageError(
                f"generation field is invalid: {identifier}"
            )
        rows.append(f"{identifier}\t{story}\t{theme}")
        if values["Form"] == "remove-load-bearing-assumption":
            assumption_ids.add(identifier)
            assumption_url_cracks[identifier] = sum(
                bool(_evidence_urls(evidence))
                for evidence in crack_evidence
            )
            if (
                any(
                    label not in values
                    for label in assumption_required
                )
                or (len(crack_evidence) < 2 and not incomplete)
            ):
                raise StageError(
                    f"assumption-removal evidence is incomplete: "
                    f"{identifier}"
                )
    complete = re.fullmatch(
        r"Assumption-Removal Attempt: complete (I[1-9][0-9]?)",
        marker,
    )
    if (
        complete is None
        and not incomplete
    ) or (
        complete is not None
        and complete.group(1) not in assumption_ids
    ):
        raise StageError("assumption-removal marker is invalid")
    # Complete attempts must carry real http(s) Crack Evidence URLs.
    # Incomplete markers satisfy the quota; missing or placeholder cracks OK.
    if complete is not None:
        complete_id = complete.group(1)
        if assumption_url_cracks.get(complete_id, 0) < 2:
            raise StageError(
                f"assumption-removal crack evidence lacks URLs: "
                f"{complete_id}"
            )
    return "\n".join(rows) + "\n"


def build_review_verdict_from_markdown(
    markdown, candidate_id, *, review_output_version=1,
    candidate_markdown=None, prior_work=None,
):
    """Validate review markdown and return host-projected verdict.tsv text.

    Markdown is authoritative; dual-write TSV drift is projected away.
    """
    if type(review_output_version) is not int or review_output_version not in {1, 2}:
        raise StageError("review output version is invalid")
    if review_output_version == 2 and ("\x00" in markdown or "\r" in markdown):
        raise StageError("review markdown contains invalid bytes")
    lines = [line for line in markdown.splitlines() if line.strip()]
    labels = (
        "Verdict",
        "CRITICAL",
        "MAJOR",
        "Headline",
        "Occupation",
        "Experiment",
        "Estimand",
        "Payoff",
        "Feasibility",
        "History",
        "Reason",
    )
    if review_output_version == 2:
        labels = labels[:-1] + ("Assessment", "Reason")
    if (
        len(lines) != len(labels) + 1
        or lines[0] != f"# {candidate_id}"
    ):
        raise StageError("review markdown schema mismatch")
    values = {}
    for line, label in zip(lines[1:], labels):
        prefix = label + ":"
        if not line.startswith(prefix):
            raise StageError("review markdown field order mismatch")
        value = line[len(prefix):].strip()
        if not value or len(value.encode("utf-8")) > 4096:
            raise StageError("review markdown field is invalid")
        values[label] = value
    if values["Verdict"] not in {
        "strong-accept",
        "accept-w-rev",
        "reject",
    }:
        raise StageError("review markdown verdict is invalid")
    if not values["CRITICAL"].isdigit() or not values["MAJOR"].isdigit():
        raise StageError("review markdown count fields are invalid")
    critical = int(values["CRITICAL"])
    major = int(values["MAJOR"])
    if (
        critical > 0
        and values["Verdict"] != "reject"
    ) or (
        major >= 2
        and values["Verdict"] == "strong-accept"
    ):
        raise StageError("review verdict violates a hard gate")
    if review_output_version == 2:
        try:
            review_assessment.parse(
                values["Assessment"], verdict=values["Verdict"],
                candidate_markdown=candidate_markdown, prior_work=prior_work,
            )
        except (ValueError, TypeError) as exc:
            raise StageError(str(exc)) from exc
    reason = values["Reason"]
    if "\t" in reason or "\n" in reason:
        raise StageError("review markdown reason is invalid")
    return f"{candidate_id}\t{values['Verdict']}\t{major}\t{reason}\n"


def validate_failure_distillation(value, batch):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "mappings"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("mappings"), list)
    ):
        raise StageError("failure distillation schema mismatch")
    required = {"source_id", "failure_code", "theme"}
    source_ids = [item["source_id"] for item in batch["items"]]
    if (
        len(value["mappings"]) != len(source_ids)
        or [
            item.get("source_id")
            for item in value["mappings"]
            if isinstance(item, dict)
        ]
        != source_ids
    ):
        raise StageError("failure mapping coverage mismatch")
    for item in value["mappings"]:
        if (
            not isinstance(item, dict)
            or set(item) != required
            or any(
                not isinstance(item[key], str)
                or not item[key]
                or len(item[key].encode("utf-8")) > 128
                for key in required
            )
            or item["failure_code"]
            not in batch["failure_codes"]
            or item["theme"] not in batch["themes"]
        ):
            raise StageError("failure mapping schema mismatch")
