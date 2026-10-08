#!/usr/bin/env python3
"""Generation themes are model-selected enums with deterministic rendering."""

import copy
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import stage_contract
from lib import portable_agent
from lib import portable_stage


THEMES = ["Evaluation and Diagnostics", "Efficiency and Systems"]
BODY = (
    "One-Sentence Story: A bounded memory claim.\n"
    "Form: new mechanism or new problem\n"
    "Summary: A falsifiable mechanism.\n"
    "Minimal Falsification Experiment: Match histories and memory bytes.\n"
    "Why It May Be Novel: A hypothesis for downstream verification.\n"
)


def content():
    return {
        "assumption_removal_attempt": (
            "Assumption-Removal Attempt: incomplete — I1; blocked by: Crack Evidence"
        ),
        "candidates": [{"theme": THEMES[1], "markdown": BODY}],
    }


class GenerationStructuredContractRegression(unittest.TestCase):
    def test_host_rejects_enum_violations_even_if_provider_ignores_schema(self):
        schema = portable_stage._response_schema("generate", THEMES)
        contract = portable_agent._validate_response_schema_contract(schema)
        value = {"schema_version": 1, "stage": "generate", "request_attestation": {
            "schema_version": "portable-stage-response-attestation-v2", "response_echo_sha256": "a" * 64,
        }, "artifacts": [{"artifact_kind": "generation-candidates-json", "content": content()}]}
        portable_agent._validate_response_value(value, contract)
        value["artifacts"][0]["content"]["candidates"][0]["theme"] = "memory-placeholder"
        with self.assertRaises(portable_agent.PortableAgentError) as caught:
            portable_agent._validate_response_value(value, contract)
        self.assertEqual(caught.exception.code, "schema_mismatch")

    def test_typed_schema_is_closed_and_only_valid_for_generation(self):
        schema = portable_stage._response_schema("generate", THEMES)
        for mutation in ("extra", "boolean-minimum", "other-stage"):
            altered = copy.deepcopy(schema)
            payload = altered["properties"]["artifacts"]["items"]["properties"]["content"]
            if mutation == "extra":
                payload["properties"]["other"] = {"type": "string"}
            elif mutation == "boolean-minimum":
                payload["properties"]["candidates"]["minItems"] = True
            else:
                altered["properties"]["stage"]["enum"] = ["review"]
            with self.subTest(mutation=mutation), self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent._validate_response_schema_contract(altered)
            self.assertEqual(caught.exception.code, "invalid_response_schema")

    def test_schema_uses_only_the_captured_policy_vocabulary(self):
        policy = "# Policy\n\n## Theme Vocabulary\n\n" + " / ".join(THEMES)
        policy += "\n\nExplanation is not a theme.\n\n## Other\nunknown\n"
        themes = stage_contract.generation_theme_vocabulary(policy)
        self.assertEqual(themes, THEMES)
        schema = stage_contract.stage_response_schema("generate", themes)
        artifact = schema["properties"]["artifacts"]["items"]["properties"]
        self.assertEqual(artifact["artifact_kind"]["enum"], ["generation-candidates-json"])
        enum = artifact["content"]["properties"]["candidates"]["items"]["properties"]["theme"]["enum"]
        self.assertEqual(enum, THEMES)
        themes.append("Mutated input")
        self.assertEqual(enum, THEMES)

    def test_empty_or_ambiguous_vocabulary_fails(self):
        for policy in ("bounded policy", "## Theme Vocabulary\n\n## Other\nX", "## Theme Vocabulary\nA / A\n"):
            with self.subTest(policy=policy), self.assertRaises(stage_contract.StageError):
                stage_contract.generation_theme_vocabulary(policy)

    def test_render_preserves_body_and_model_theme(self):
        value = content()
        value["candidates"].append({"theme": THEMES[0], "markdown": BODY + "Extra evidence: literal text.\n"})
        original = copy.deepcopy(value)
        actual = stage_contract.render_generation_markdown(value, THEMES)
        expected = value["assumption_removal_attempt"] + "\n\n"
        expected += "## I1\nTheme: Efficiency and Systems\n" + BODY
        expected += "\n\n## I2\nTheme: Evaluation and Diagnostics\n" + value["candidates"][1]["markdown"] + "\n"
        self.assertEqual(actual, expected)
        self.assertEqual(value, original)
        self.assertEqual(stage_contract.build_generation_tsv_from_markdown(actual),
                         "I1\tA bounded memory claim.\tEfficiency and Systems\nI2\tA bounded memory claim.\tEvaluation and Diagnostics\n")

    def test_unknown_theme_is_never_mapped_or_defaulted(self):
        for theme in ("memory-placeholder", " Efficiency and Systems", "efficiency and systems", "", None):
            value = content()
            value["candidates"][0]["theme"] = theme
            with self.subTest(theme=theme), self.assertRaises(stage_contract.StageError):
                stage_contract.render_generation_markdown(value, THEMES)

    def test_body_cannot_supply_another_theme_heading_or_marker(self):
        for injected in ("Theme: memory-placeholder", "  Theme: Efficiency and Systems", "\t## I2", "##\tI2", "Assumption-Removal Attempt: complete I1", "\r\nTheme: memory-placeholder", "\u2028Theme: memory-placeholder"):
            value = content()
            value["candidates"][0]["markdown"] += injected + "\n"
            with self.subTest(injected=injected), self.assertRaises(stage_contract.StageError):
                stage_contract.render_generation_markdown(value, THEMES)

    def test_marker_is_one_line_and_candidate_count_is_bounded(self):
        for marker in ("Unrecognized marker", "Assumption-Removal Attempt: incomplete I1\n## I1", "Assumption-Removal Attempt: incomplete I1\rTheme: X"):
            value = content(); value["assumption_removal_attempt"] = marker
            with self.subTest(marker=marker), self.assertRaises(stage_contract.StageError):
                stage_contract.render_generation_markdown(value, THEMES)
        for count in (0, 21):
            value = content(); value["candidates"] *= count
            with self.subTest(count=count), self.assertRaises(stage_contract.StageError):
                stage_contract.render_generation_markdown(value, THEMES)

    def test_derived_markdown_keeps_utf8_byte_limit(self):
        value = content(); value["candidates"][0]["markdown"] = BODY + "\u00e9" * 33000
        with self.assertRaisesRegex(stage_contract.StageError, "byte bound"):
            stage_contract.render_generation_markdown(value, THEMES)

    def test_fresh_generation_rejects_legacy_string_and_extra_keys(self):
        for value in ("legacy markdown", {**content(), "extra": True}):
            raw = json.dumps({"schema_version": 1, "stage": "generate", "artifacts": [{"artifact_kind": "generation-candidates-json", "content": value}]}).encode()
            with self.subTest(value=value), self.assertRaises(stage_contract.StageError):
                stage_contract.parse_model_output("generate", raw, THEMES)
        raw = json.dumps({"schema_version": 1, "stage": "generate", "artifacts": [{"artifact_kind": "generation-ideas-markdown", "content": BODY}]}).encode()
        with self.assertRaises(ValueError):
            stage_contract.parse_model_output("generate", raw, THEMES)


class ModelEnvelopeSizeRegression(unittest.TestCase):
    def test_json_escaping_can_exceed_old_cap_without_exceeding_artifact_limit(self):
        content = '"' * 65536
        raw = portable_agent._canonical_json_bytes({
            "schema_version": 1,
            "stage": "review",
            "artifacts": [{"artifact_kind": "review-markdown", "content": content}],
        })
        self.assertGreater(len(raw), 128 * 1024)
        self.assertEqual(
            stage_contract.parse_model_output("review", raw),
            {"output/review.md": content.encode("utf-8")},
        )

    def test_decoded_artifact_still_rejects_content_above_its_limit(self):
        raw = portable_agent._canonical_json_bytes({
            "schema_version": 1,
            "stage": "review",
            "artifacts": [{"artifact_kind": "review-markdown", "content": "x" * 65537}],
        })
        with self.assertRaisesRegex(ValueError, "model artifact content is invalid"):
            stage_contract.parse_model_output("review", raw)


if __name__ == "__main__":
    unittest.main()
