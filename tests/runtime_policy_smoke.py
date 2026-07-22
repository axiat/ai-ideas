#!/usr/bin/env python3
import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_product_contract",
    ROOT / "tests/verify_product_contract.py",
)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def expect_lines(source, expected, label):
    actual = CONTRACT.claude_invocation_lines(source)
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, found {actual}")


def main():
    if CONTRACT.stable_calibration_title("Policy(OneDP)") != CONTRACT.stable_calibration_title("Policy (OneDP)"):
        raise AssertionError("calibration title projection over-constrains spacing before parentheses")
    if CONTRACT.stable_calibration_title("Policy(OneDP )") != CONTRACT.stable_calibration_title("Policy (OneDP)"):
        raise AssertionError("calibration title projection over-constrains spacing before a closing parenthesis")
    expect_lines("claude -p prompt\n", [1], "direct invocation")
    expect_lines("    claude -p prompt\n", [1], "indented direct invocation")
    expect_lines("env claude -p prompt\n", [1], "environment-wrapped direct invocation")
    expect_lines("if claude -p prompt; then :; fi\n", [1], "conditional direct invocation")
    expect_lines("! claude -p prompt\n", [1], "negated direct invocation")
    expect_lines("time claude -p prompt\n", [1], "timed direct invocation")
    expect_lines("sudo claude -p prompt\n", [1], "privilege-wrapped direct invocation")
    expect_lines(
        "sudo -u root claude -p prompt\n",
        [1],
        "optioned privilege-wrapped direct invocation",
    )
    expect_lines(
        "env -i claude -p prompt\n",
        [1],
        "optioned environment-wrapped direct invocation",
    )
    expect_lines(
        "command -- claude -p prompt\n",
        [1],
        "optioned command-wrapper direct invocation",
    )
    expect_lines("bash -c 'claude -p prompt'\n", [1], "shell-string direct invocation")
    expect_lines(
        "BACKEND='claude -p'\n$BACKEND prompt\n",
        [2],
        "tainted variable invocation",
    )
    expect_lines(
        "BACKEND='claude -p'\nRUNNER=$BACKEND\n$RUNNER prompt\n",
        [3],
        "transitively tainted variable invocation",
    )
    expect_lines(
        "BACKEND='claude -p'\nbash -c \"$BACKEND prompt\"\n",
        [2],
        "tainted shell-string invocation",
    )
    expect_lines(
        "BACKEND='claude -p'\nif $BACKEND prompt; then :; fi\n",
        [2],
        "conditional tainted-variable invocation",
    )
    for command, label in (
        ("env $BACKEND prompt", "environment-wrapped tainted variable"),
        ("env -i $BACKEND prompt", "optioned environment-wrapped tainted variable"),
        ("sudo -- $BACKEND prompt", "privilege-wrapped tainted variable"),
        ("command -- $BACKEND prompt", "command-wrapped tainted variable"),
    ):
        expect_lines(
            f"BACKEND='claude -p'\n{command}\n",
            [2],
            label,
        )
    expect_lines("echo '#' ; claude -p prompt\n", [1], "quoted hash before invocation")
    expect_lines(
        "# AGENT_CMD='claude -p' ./hunt.sh\ncp .claude/settings.json /tmp/settings.json\n",
        [],
        "non-executed opt-in example and path copy",
    )
    workflow = "steps:\n  - run: claude -p prompt\n  - run: |\n      echo safe\n      claude -p prompt\n"
    expect_lines(
        CONTRACT.workflow_shell_text(workflow),
        [1, 3],
        "workflow shell blocks",
    )
    workflow_positions = "steps:\n  - run: if claude -p prompt; then :; fi\n  - run: '! claude -p prompt'\n"
    expect_lines(
        CONTRACT.workflow_shell_text(workflow_positions),
        [1, 2],
        "workflow conditional command positions",
    )
    workflow_indirect = "env:\n  BACKEND: claude -p\nsteps:\n  - run: $BACKEND prompt\n"
    expect_lines(
        CONTRACT.workflow_shell_text(workflow_indirect),
        [2],
        "workflow environment indirection",
    )
    CONTRACT.verify_awr_state_aliases()
    print("ok: runtime policy smoke")


if __name__ == "__main__":
    main()
