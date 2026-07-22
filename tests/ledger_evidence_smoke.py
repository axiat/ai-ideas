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
    changed(
        swap_first_technical_pair(rows),
        baseline,
        "row_technical_tokens",
        "technical-token reordering",
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

    required_symbols = "①②③④−≡∈⇒∝⟂≫↑∧^+=|~"
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

    for delimiter in "，。；（）":
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
