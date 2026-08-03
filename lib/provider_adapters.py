"""Closed provider resolution for portable history-audit execution."""

import dataclasses
import collections.abc
import hashlib
import json
import pathlib
import shutil
import types
import weakref

try:
    from lib import history_contract_v2
except ImportError:
    import history_contract_v2


_PROVIDERS = ("codex", "kimi", "grok", "opencode", "agy")
_SURFACES = {
    "hunt": ("codex", "kimi", "grok"),
    "awr": _PROVIDERS,
}
_FORBIDDEN = "cl" + "aude"
_PROBE_FIELDS = frozenset(
    {
        "cli_revision",
        "serializer_revision",
        "effective_model",
        "effective_reasoning",
        "model_override_applied",
        "reasoning_override_applied",
        "immutable_capacity_identity",
        "evidence_sha256",
    }
)


class ProviderResolutionError(ValueError):
    pass


_VALIDATED_REGISTRIES = weakref.WeakKeyDictionary()


class ValidatedProviderRegistry(collections.abc.Mapping):
    """Opaque registry value issued only after closed-schema validation."""

    __slots__ = ("__weakref__",)
    __hash__ = object.__hash__
    __eq__ = object.__eq__
    __ne__ = object.__ne__

    def __new__(cls, *args, **kwargs):
        raise TypeError("provider registries are loader-issued")

    def __getitem__(self, key):
        value = _VALIDATED_REGISTRIES.get(self)
        if value is None:
            raise ProviderResolutionError("provider registry is unvalidated")
        return value[key]

    def __iter__(self):
        value = _VALIDATED_REGISTRIES.get(self)
        if value is None:
            return iter(())
        return iter(value)

    def __len__(self):
        value = _VALIDATED_REGISTRIES.get(self)
        return 0 if value is None else len(value)


def _issue_registry(value):
    registry = object.__new__(ValidatedProviderRegistry)
    _VALIDATED_REGISTRIES[registry] = _freeze(value)
    return registry


@dataclasses.dataclass(frozen=True)
class ProviderCapability:
    provider: str
    surface: str
    executable: str
    executable_path: str
    model_override: object
    reasoning_override: object
    model_identity: str
    reasoning_identity: str
    cli_revision: str
    serializer_revision: str
    immutable_capacity_identity: object
    evidence_sha256: str
    profile_hash: str
    hard_complete_eligible: bool
    authority: str


def _freeze(value):
    if isinstance(value, dict):
        return types.MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _walk_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def _require_text(value, name, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or any(character in value for character in "\0\r\n"):
        raise ProviderResolutionError(f"{name} must be a non-empty single-line string")
    return value


def _require_sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProviderResolutionError(f"{name} must be a lowercase SHA-256")
    return value


def load_registry(path):
    """Return a closed provider registry with surface eligibility."""
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResolutionError("provider registry is unreadable") from exc
    if set(value) != {"schema_version", "registry_revision", "providers", "surfaces"}:
        raise ProviderResolutionError("provider registry fields are not closed")
    if value["schema_version"] != "provider-adapters-v1":
        raise ProviderResolutionError("unsupported provider registry schema")
    if tuple(value["providers"]) != _PROVIDERS:
        raise ProviderResolutionError("provider registry is not the closed v1 set")
    if value["surfaces"] != {name: list(items) for name, items in _SURFACES.items()}:
        raise ProviderResolutionError("provider surface eligibility drifted")
    for name, entry in value["providers"].items():
        if set(entry) != {"executable", "grammar_revision"}:
            raise ProviderResolutionError(f"provider entry is not closed: {name}")
        if entry["executable"] != name:
            raise ProviderResolutionError("provider executable aliases are forbidden")
        _require_text(entry["grammar_revision"], "grammar_revision")
    if any(_FORBIDDEN in item.lower() for item in _walk_strings(value)):
        raise ProviderResolutionError("forbidden provider path in registry")
    return _issue_registry(value)


def _default_probe(provider, executable_path, model, reasoning):
    material = f"{provider}|{executable_path}|unprobed|{model}|{reasoning}".encode()
    return {
        "cli_revision": "unprobed",
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": None,
        "effective_reasoning": None,
        "model_override_applied": model is None,
        "reasoning_override_applied": reasoning is None,
        "immutable_capacity_identity": None,
        "evidence_sha256": hashlib.sha256(material).hexdigest(),
    }


def resolve_provider(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    executable_lookup=None,
    version_probe=None,
):
    """Return a frozen no-launch capability and command grammar."""
    if (
        type(registry) is not ValidatedProviderRegistry
        or registry not in _VALIDATED_REGISTRIES
    ):
        raise ProviderResolutionError("provider registry is unvalidated")
    if surface not in _SURFACES:
        raise ProviderResolutionError("unknown product surface")
    if provider not in _SURFACES[surface]:
        raise ProviderResolutionError("provider is not eligible for this surface")
    model = _require_text(model, "model", optional=True)
    reasoning = _require_text(reasoning, "reasoning", optional=True)
    if provider == "kimi" and reasoning is not None:
        raise ProviderResolutionError("kimi does not support a reasoning override")
    try:
        executable = registry["providers"][provider]["executable"]
    except (KeyError, TypeError) as exc:
        raise ProviderResolutionError("provider registry does not match resolver") from exc
    if executable != provider:
        raise ProviderResolutionError("provider executable aliases are forbidden")
    lookup = executable_lookup or shutil.which
    executable_path = lookup(executable)
    if not isinstance(executable_path, str) or not executable_path:
        raise ProviderResolutionError("provider executable is unavailable")
    executable_path = str(pathlib.Path(executable_path).resolve())
    if _FORBIDDEN in pathlib.Path(executable_path).name.lower():
        raise ProviderResolutionError("forbidden provider executable")
    evidence = (version_probe or _default_probe)(
        provider, executable_path, model, reasoning
    )
    if not isinstance(evidence, dict) or set(evidence) != _PROBE_FIELDS:
        raise ProviderResolutionError("capability probe fields are not closed")
    _require_text(evidence["cli_revision"], "cli_revision")
    if evidence["serializer_revision"] != "portable-agent-command-v1":
        raise ProviderResolutionError("serializer revision is unsupported")
    _require_sha(evidence["evidence_sha256"], "evidence_sha256")
    for field in ("effective_model", "effective_reasoning", "immutable_capacity_identity"):
        _require_text(evidence[field], field, optional=True)
    for field in ("model_override_applied", "reasoning_override_applied"):
        if type(evidence[field]) is not bool:
            raise ProviderResolutionError(f"{field} must be boolean")
    if model is not None and (
        not evidence["model_override_applied"] or evidence["effective_model"] != model
    ):
        raise ProviderResolutionError("model override was ignored or changed")
    if reasoning is not None and (
        not evidence["reasoning_override_applied"]
        or evidence["effective_reasoning"] != reasoning
    ):
        raise ProviderResolutionError("reasoning override was ignored or changed")
    model_identity = evidence["effective_model"] or "provider-default"
    reasoning_identity = evidence["effective_reasoning"] or "provider-default"
    hard_complete = bool(
        evidence["effective_model"]
        and evidence["effective_reasoning"]
        and evidence["immutable_capacity_identity"]
        and evidence["cli_revision"] != "unprobed"
    )
    material = {
        "provider": provider,
        "surface": surface,
        "executable": executable,
        "executable_path": executable_path,
        "model_override": model,
        "reasoning_override": reasoning,
        "model_identity": model_identity,
        "reasoning_identity": reasoning_identity,
        "cli_revision": evidence["cli_revision"],
        "serializer_revision": evidence["serializer_revision"],
        "immutable_capacity_identity": evidence["immutable_capacity_identity"],
        "evidence_sha256": evidence["evidence_sha256"],
        "grammar_revision": registry["providers"][provider]["grammar_revision"],
        "hard_complete_eligible": hard_complete,
    }
    profile_hash = history_contract_v2.framed_sha256(
        "provider-capability-v1", history_contract_v2.canonical_bytes(material)
    )
    return ProviderCapability(
        provider=provider,
        surface=surface,
        executable=executable,
        executable_path=executable_path,
        model_override=model,
        reasoning_override=reasoning,
        model_identity=model_identity,
        reasoning_identity=reasoning_identity,
        cli_revision=evidence["cli_revision"],
        serializer_revision=evidence["serializer_revision"],
        immutable_capacity_identity=evidence["immutable_capacity_identity"],
        evidence_sha256=evidence["evidence_sha256"],
        profile_hash=profile_hash,
        hard_complete_eligible=hard_complete,
        authority="hard-complete" if hard_complete else "shadow-only",
    )


def render_command(capability, mirror, prompt, schema_path=None):
    """Return closed argv and a minimal environment delta."""
    if not isinstance(capability, ProviderCapability):
        raise ProviderResolutionError("capability is not resolver-issued")
    if not isinstance(prompt, str):
        raise ProviderResolutionError("prompt must be text")
    if schema_path is not None:
        raise ProviderResolutionError("provider grammar has no schema-path flag")
    mirror = str(pathlib.Path(mirror))
    argv = [capability.executable_path]
    model = capability.model_override
    reasoning = capability.reasoning_override
    if capability.provider == "codex":
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["-c", f"model_reasoning_effort={reasoning}"]
        argv += [
            "-c", "approval_policy=never", "exec", "-s", "workspace-write",
            "--skip-git-repo-check", "--ephemeral", prompt,
        ]
    elif capability.provider == "kimi":
        argv += ["--auto", "--output-format", "text"]
        if model is not None:
            argv += ["-m", model]
        argv += ["-p", prompt]
    elif capability.provider == "grok":
        argv += [
            "--always-approve", "--no-memory", "--no-subagents",
            "--output-format", "plain", "--cwd", mirror,
        ]
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["--reasoning-effort", reasoning]
        argv += ["-p", prompt]
    elif capability.provider == "opencode":
        argv += ["run", "--pure", "--auto", "--dir", mirror]
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["--variant", reasoning]
        argv += [prompt]
    elif capability.provider == "agy":
        argv += [
            "--dangerously-skip-permissions", "--disable-slash-commands",
            "--output-format", "text", "--add-dir", mirror,
        ]
        if model is not None:
            argv += ["--model", model]
        if reasoning is not None:
            argv += ["--effort", reasoning]
        argv += ["--print", prompt]
    else:
        raise ProviderResolutionError("capability provider is not renderable")
    return argv, {}


def capability_is_current(capability, replacement):
    return (
        isinstance(capability, ProviderCapability)
        and isinstance(replacement, ProviderCapability)
        and capability.profile_hash == replacement.profile_hash
    )


def resolve_pool(
    registry,
    surface,
    providers,
    *,
    executable_lookup=None,
    version_probe=None,
):
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ProviderResolutionError("provider pool must be a non-empty ordered list")
    if len(set(providers)) != len(providers):
        raise ProviderResolutionError("provider pool cannot contain duplicates")
    return tuple(
        resolve_provider(
            registry,
            surface,
            provider,
            executable_lookup=executable_lookup,
            version_probe=version_probe,
        )
        for provider in providers
    )


def provider_for_attempt(pool, attempt_ordinal):
    if type(attempt_ordinal) is not int or attempt_ordinal < 0:
        raise ProviderResolutionError("attempt ordinal must be nonnegative")
    try:
        return pool[attempt_ordinal]
    except (IndexError, TypeError) as exc:
        raise ProviderResolutionError("declared provider pool is exhausted") from exc
