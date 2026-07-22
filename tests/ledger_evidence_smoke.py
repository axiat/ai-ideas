#!/usr/bin/env python3
import copy
import importlib.util
import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_product_contract",
    ROOT / "tests/verify_product_contract.py",
)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def changed(rows, baseline, key, label):
    if CONTRACT.ledger_evidence(rows)[key] == baseline[key]:
        raise AssertionError(f"ledger evidence missed {label}")


def unchanged(rows, baseline, key, label):
    if CONTRACT.ledger_evidence(rows)[key] != baseline[key]:
        raise AssertionError(f"ledger evidence over-constrained {label}")


def rejected(rows, label):
    try:
        CONTRACT.verify_ledger_evidence(rows)
    except AssertionError:
        return
    raise AssertionError(f"ledger evidence accepted {label}")


def replace_first(rows, pattern, replacement, strip_urls=False):
    mutated = copy.deepcopy(rows)
    for row in mutated:
        for field_index in (3, 5):
            value = row[field_index]
            search_value = value
            if strip_urls:
                search_value = CONTRACT.LEDGER_URL.sub(
                    lambda match: " " * len(match.group(0)), value
                )
            match = pattern.search(search_value)
            if match:
                row[field_index] = (
                    value[:match.start()]
                    + replacement(match.group(0))
                    + value[match.end():]
                )
                return mutated
    raise AssertionError(f"no fixture token for {pattern.pattern}")


def move_first_url(rows):
    mutated = copy.deepcopy(rows)
    for row in mutated:
        for source, target in ((5, 3), (3, 5)):
            match = CONTRACT.LEDGER_URL.search(row[source])
            if match:
                token = match.group(0)
                row[source] = row[source][:match.start()] + row[source][match.end():]
                row[target] = row[target] + " " + token
                return mutated
    raise AssertionError("no URL fixture")


def swap_first_technical_pair(rows):
    mutated = copy.deepcopy(rows)
    for row in mutated:
        for field_index in (3, 5):
            value = row[field_index]
            search_value = CONTRACT.LEDGER_URL.sub(
                lambda match: " " * len(match.group(0)), value
            )
            matches = list(CONTRACT.LEDGER_TECH_TOKEN.finditer(search_value))
            for first, second in zip(matches, matches[1:]):
                if first.group(0) == second.group(0):
                    continue
                row[field_index] = (
                    value[:first.start()]
                    + second.group(0)
                    + value[first.end():second.start()]
                    + first.group(0)
                    + value[second.end():]
                )
                return mutated
    raise AssertionError("no ordered technical-token fixture")


def main():
    rows = CONTRACT.ledger_rows()[1:]
    baseline = CONTRACT.ledger_evidence(rows)
    expected = {
        key: CONTRACT.EXPECTED[key]
        for key in (
            "row_urls",
            "row_technical_tokens",
            "row_count_units",
            "row_labeled_quantities",
            "row_numeric_operators",
            "row_code_spans",
            "row_symbols",
        )
    }
    if baseline != expected:
        raise AssertionError(f"ledger evidence baseline changed: {baseline}")

    changed(
        replace_first(rows, CONTRACT.LEDGER_URL, lambda token: token + "x"),
        baseline,
        "row_urls",
        "URL mutation",
    )
    changed(
        move_first_url(rows),
        baseline,
        "row_urls",
        "URL field reassignment",
    )
    changed(
        replace_first(
            rows,
            CONTRACT.LEDGER_TECH_TOKEN,
            lambda token: token + "0",
            strip_urls=True,
        ),
        baseline,
        "row_technical_tokens",
        "technical-token mutation",
    )
    unchanged(
        replace_first(
            rows,
            CONTRACT.LEDGER_TECH_TOKEN,
            lambda token: "—" + token + ":",
            strip_urls=True,
        ),
        baseline,
        "row_technical_tokens",
        "technical-token edge punctuation",
    )
    numeric_unit = re.compile(r"(?<=\d)\s+(?:rollouts?|seeds?)\b")
    unchanged(
        replace_first(
            rows,
            numeric_unit,
            lambda token: token[:-1] if token.endswith("s") else token + "s",
        ),
        baseline,
        "row_technical_tokens",
        "count-unit plurality",
    )
    unchanged(
        replace_first(
            rows,
            numeric_unit,
            lambda token: " seeds" if "rollout" in token else " rollouts",
        ),
        baseline,
        "row_technical_tokens",
        "count-unit wording in the source-frozen token projection",
    )
    changed(
        replace_first(
            rows,
            numeric_unit,
            lambda token: " seeds" if "rollout" in token else " rollouts",
        ),
        baseline,
        "row_count_units",
        "count-unit semantics",
    )
    changed(
        swap_first_technical_pair(rows),
        baseline,
        "row_technical_tokens",
        "technical-token reordering",
    )
    for pattern, label in (
        (re.compile(r"(?<=\d)\s*kg\b", re.I), "mass unit"),
        (re.compile(r"(?<=\d)\s*MAJOR\b", re.I), "review-severity unit"),
        (re.compile(r"(?<=\d)-state\b", re.I), "state-count label"),
    ):
        changed(
            replace_first(rows, pattern, lambda token: ""),
            baseline,
            "row_labeled_quantities",
            label,
        )
    for pattern, replacement, label in (
        (
            re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\+(?![A-Za-z0-9])"),
            lambda token: token[:-1],
            "postfix lower bound",
        ),
        (
            re.compile(r"(?<![A-Za-z0-9])\+[0-9]+(?:\.[0-9]+)?(?![A-Za-z0-9])"),
            lambda token: token[1:],
            "signed positive value",
        ),
        (
            re.compile(r"(?<![A-Za-z0-9])\d+/\d+(?![A-Za-z0-9])"),
            lambda token: token.replace("/", " "),
            "numeric ratio",
        ),
    ):
        changed(
            replace_first(rows, pattern, replacement, strip_urls=True),
            baseline,
            "row_numeric_operators",
            label,
        )
    changed(
        replace_first(rows, CONTRACT.LEDGER_SYMBOL, lambda token: token + "≥"),
        baseline,
        "row_symbols",
        "symbol mutation",
    )

    technical_identifier = CONTRACT.LEDGER_TECH_TOKEN.search("AC²-VLA")
    if not technical_identifier or technical_identifier.group(0) != "AC²-VLA":
        raise AssertionError("superscript technical identifier is not atomic")
    normalized_tokens = {
        "F1:": "F1",
        "——F1": "F1",
        ":2412.14355": "2412.14355",
        "3x.": "3x",
        "0.5": "0.5",
        "/100-500Hz/": "100-500Hz",
        "50rollout": "50",
        "50rollouts": "50",
        "4seed": "4",
        "4seeds": "4",
        "1MAJOR": "1",
        "4-state": "4",
        "2kg": "2",
        "2602.03203:oracle": "2602.03203",
    }
    for token, expected_token in normalized_tokens.items():
        if CONTRACT.normalize_ledger_technical_token(token) != expected_token:
            raise AssertionError(f"incorrect technical-token normalization: {token}")
    normalized_text = {
        "100-300 ms": "100-300ms",
        "100-500 Hz": "100-500Hz",
        "0.5-1 s": "0.5-1s",
        "50 rollouts": "50rollouts",
        "4 seeds": "4seeds",
    }
    for value, expected_value in normalized_text.items():
        if CONTRACT.normalize_ledger_technical_text(value) != expected_value:
            raise AssertionError(f"incorrect technical-text normalization: {value}")

    required_symbols = "①②③④−≡∈⇒∝⟂≫↑∧^$|~"
    ledger_text = "\n".join(
        row[field_index]
        for row in rows
        for field_index in (3, 5)
    )
    for symbol in required_symbols:
        if symbol not in ledger_text:
            raise AssertionError(f"missing symbol fixture: {symbol}")
        changed(
            replace_first(rows, re.compile(re.escape(symbol)), lambda token: ""),
            baseline,
            "row_symbols",
            f"{symbol} mutation",
        )

    unchanged(
        replace_first(rows, re.compile(r"\+"), lambda token: ""),
        baseline,
        "row_symbols",
        "prose plus-sign cleanup",
    )

    for delimiter in ",;，。；（）":
        sample = f"https://example.test/path{delimiter}tail"
        match = CONTRACT.LEDGER_URL.match(sample)
        if not match or match.group(0) != "https://example.test/path":
            raise AssertionError(f"URL consumed full-width delimiter: {delimiter}")

    seven_to_eight = copy.deepcopy(rows)
    next(row for row in seven_to_eight if len(row) == 7).append("")
    rejected(seven_to_eight, "7-to-8 field drift")

    eight_to_seven = copy.deepcopy(rows)
    next(row for row in eight_to_seven if len(row) == 8).pop()
    rejected(eight_to_seven, "8-to-7 field drift")

    added_code = copy.deepcopy(rows)
    added_code[0][3] += " `unexpected-code-span`"
    changed(
        added_code,
        baseline,
        "row_code_spans",
        "code-span insertion",
    )
    print("ok: ledger evidence smoke")


if __name__ == "__main__":
    main()
