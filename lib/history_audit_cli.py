#!/usr/bin/env python3
"""Command-line entry points for the provider-neutral history audit ABI."""

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys

try:
    from lib import provider_adapters
except ImportError:
    import provider_adapters


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "history/provider-adapters-v1.json"
MIRROR_PLACEHOLDER = "/__AI_IDEAS_PORTABLE_MIRROR__"
PROMPT_PLACEHOLDER = "__AI_IDEAS_PORTABLE_PROMPT__"


def _diagnostic_probe(provider, executable_path, model, reasoning):
    """Bind requested grammar without claiming a provider capability probe."""
    material = json.dumps(
        {
            "executable_path": executable_path,
            "model_override": model,
            "provider": provider,
            "reasoning_override": reasoning,
            "scope": "grammar-only",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "cli_revision": "unprobed",
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": model,
        "effective_reasoning": reasoning,
        "model_override_applied": True,
        "reasoning_override_applied": True,
        "immutable_capacity_identity": None,
        "evidence_sha256": hashlib.sha256(material).hexdigest(),
    }


def _provider_command(arguments):
    registry = provider_adapters.load_registry(REGISTRY)
    capability = provider_adapters.resolve_provider(
        registry,
        arguments.surface,
        arguments.provider,
        model=arguments.model,
        reasoning=arguments.reasoning,
        version_probe=_diagnostic_probe,
    )
    argv, environment = provider_adapters.render_command(
        capability,
        MIRROR_PLACEHOLDER,
        PROMPT_PLACEHOLDER,
    )
    value = dataclasses.asdict(capability)
    value.update(
        {
            "schema_version": "provider-command-v1",
            "model_default": arguments.model is None,
            "reasoning_default": arguments.reasoning is None,
            "argv": argv,
            "environment": environment,
            "execution_boundary": "portable-mirror-v1",
            "diagnostic_scope": "grammar-only",
        }
    )
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _parser():
    parser = argparse.ArgumentParser(prog="history_audit_cli.py")
    commands = parser.add_subparsers(dest="command", required=True)
    provider = commands.add_parser(
        "provider-command",
        help="print a canonical no-launch provider command diagnostic",
    )
    provider.add_argument("--surface", choices=("hunt", "awr"), required=True)
    provider.add_argument("--provider", required=True)
    provider.add_argument("--model")
    provider.add_argument("--reasoning")
    provider.set_defaults(handler=_provider_command)
    return parser


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except provider_adapters.ProviderResolutionError as exc:
        print(f"history-audit: {arguments.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
