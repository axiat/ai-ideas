"""Closed provider resolution for portable history-audit execution."""

import dataclasses
import collections.abc
import hashlib
import json
import os
import pathlib
import shutil
import stat
import types
import unicodedata
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
_COMMAND_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "surface",
        "provider",
        "requested_model",
        "requested_reasoning",
        "effective_model",
        "effective_reasoning",
        "model_override_applied",
        "reasoning_override_applied",
        "model_default",
        "reasoning_default",
        "hard_complete_eligible",
        "authority",
        "execution_boundary",
        "diagnostic_scope",
        "grammar_status",
        "provider_validation",
        "execution_request_profile_hash",
        "argv",
        "environment",
    }
)
_COMMAND_RECORD_MIRROR = "/portable-mirror"
_COMMAND_RECORD_PROMPT = "PROMPT"
_PROFILE_DESCRIPTOR_FIELDS = frozenset(
    {
        "surface",
        "provider",
        "requested_model",
        "requested_reasoning",
        "execution_request_profile_hash",
    }
)


class ProviderResolutionError(ValueError):
    pass


_VALIDATED_REGISTRIES = weakref.WeakKeyDictionary()
_ISSUED_CAPABILITIES = weakref.WeakKeyDictionary()
_ISSUED_COMMAND_INTENTS = weakref.WeakKeyDictionary()


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


@dataclasses.dataclass(frozen=True, eq=False)
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


@dataclasses.dataclass(frozen=True, eq=False)
class ProviderCommandIntent:
    """Grammar-checked execution request without provider capability claims."""

    provider: str
    surface: str
    executable: str
    executable_path: str
    requested_model: object
    requested_reasoning: object
    effective_model: object
    effective_reasoning: object
    model_override_applied: object
    reasoning_override_applied: object
    grammar_revision: str
    serializer_revision: str
    provider_validation: str
    execution_request_profile_hash: str
    hard_complete_eligible: bool
    authority: str

    @property
    def profile_hash(self):
        return self.execution_request_profile_hash

    @property
    def model_override(self):
        return self.requested_model

    @property
    def reasoning_override(self):
        return self.requested_reasoning


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
    if type(value) is not str or not value or any(character in value for character in "\0\r\n"):
        raise ProviderResolutionError(f"{name} must be a non-empty single-line string")
    if unicodedata.normalize("NFC", value) != value:
        raise ProviderResolutionError(f"{name} must be NFC-normalized")
    return value


def _require_exact_json_types(value):
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is list:
        for item in value:
            _require_exact_json_types(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProviderResolutionError(
                    "JSON object keys must be built-in strings"
                )
            _require_exact_json_types(item)
        return
    raise ProviderResolutionError("value contains a non-canonical JSON type")


def _exact_json_bytes(value):
    _require_exact_json_types(value)
    try:
        return history_contract_v2.canonical_bytes(value)
    except history_contract_v2.ContractV2Error as exc:
        raise ProviderResolutionError("value is not canonical JSON") from exc


def _captured_executable_identity(path):
    try:
        info = pathlib.Path(path).lstat()
    except OSError as exc:
        raise ProviderResolutionError("provider executable is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ProviderResolutionError("provider executable is unavailable")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _issued_snapshot(value, expected_type):
    if type(value) is not expected_type:
        raise ProviderResolutionError("resolver-issued value has the wrong type")
    field_names = tuple(field.name for field in dataclasses.fields(expected_type))
    attributes = vars(value)
    if set(attributes) != set(field_names):
        raise ProviderResolutionError("resolver-issued value was modified")
    return (
        _exact_json_bytes(
            {
                "record_type": expected_type.__name__,
                "fields": {
                    name: attributes[name] for name in field_names
                },
            }
        ),
        _captured_executable_identity(attributes["executable_path"]),
    )


def _issue_capability(value):
    _ISSUED_CAPABILITIES[value] = _issued_snapshot(
        value, ProviderCapability
    )
    return value


def _issue_command_intent(value):
    _ISSUED_COMMAND_INTENTS[value] = _issued_snapshot(
        value, ProviderCommandIntent
    )
    return value


def command_intent_is_issued(value):
    """Return whether value is an unmodified resolver-issued command intent."""
    if type(value) is not ProviderCommandIntent:
        return False
    expected = _ISSUED_COMMAND_INTENTS.get(value)
    if expected is None:
        return False
    try:
        return expected == _issued_snapshot(value, ProviderCommandIntent)
    except ProviderResolutionError:
        return False


def capability_is_issued(value):
    """Return whether value is an unmodified resolver-issued capability."""
    if type(value) is not ProviderCapability:
        return False
    expected = _ISSUED_CAPABILITIES.get(value)
    if expected is None:
        return False
    try:
        return expected == _issued_snapshot(value, ProviderCapability)
    except ProviderResolutionError:
        return False


def _require_sha(value, name):
    if (
        type(value) is not str
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
    if type(value) is not dict or set(value) != {
        "schema_version", "registry_revision", "providers", "surfaces"
    }:
        raise ProviderResolutionError("provider registry fields are not closed")
    _require_text(value["schema_version"], "schema_version")
    _require_text(value["registry_revision"], "registry_revision")
    if value["schema_version"] != "provider-adapters-v1":
        raise ProviderResolutionError("unsupported provider registry schema")
    if type(value["providers"]) is not dict or type(value["surfaces"]) is not dict:
        raise ProviderResolutionError("provider registry collections are invalid")
    if tuple(value["providers"]) != _PROVIDERS:
        raise ProviderResolutionError("provider registry is not the closed v1 set")
    for surface, providers in value["surfaces"].items():
        _require_text(surface, "surface")
        if type(providers) is not list:
            raise ProviderResolutionError("provider surface pool must be an array")
        for provider in providers:
            _require_text(provider, "provider")
    if value["surfaces"] != {name: list(items) for name, items in _SURFACES.items()}:
        raise ProviderResolutionError("provider surface eligibility drifted")
    for name, entry in value["providers"].items():
        _require_text(name, "provider")
        if type(entry) is not dict or set(entry) != {"executable", "grammar_revision"}:
            raise ProviderResolutionError(f"provider entry is not closed: {name}")
        _require_text(entry["executable"], "executable")
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


def _validate_grammar_request(registry, surface, provider, model, reasoning):
    if (
        type(registry) is not ValidatedProviderRegistry
        or registry not in _VALIDATED_REGISTRIES
    ):
        raise ProviderResolutionError("provider registry is unvalidated")
    surface = _require_text(surface, "surface")
    provider = _require_text(provider, "provider")
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
    return executable, model, reasoning


def _resolve_grammar(
    registry,
    surface,
    provider,
    model,
    reasoning,
    executable_lookup,
):
    executable, model, reasoning = _validate_grammar_request(
        registry, surface, provider, model, reasoning
    )
    lookup = executable_lookup or shutil.which
    executable_path = lookup(executable)
    if type(executable_path) is not str or not executable_path:
        raise ProviderResolutionError("provider executable is unavailable")
    executable_path = str(pathlib.Path(executable_path).resolve())
    if _FORBIDDEN in pathlib.Path(executable_path).name.lower():
        raise ProviderResolutionError("forbidden provider executable")
    return executable, executable_path, model, reasoning


def _command_profile_hash(registry, surface, provider, model, reasoning):
    material = {
        "provider": provider,
        "surface": surface,
        "executable": provider,
        "requested_model": model,
        "requested_reasoning": reasoning,
        "grammar_revision": registry["providers"][provider]["grammar_revision"],
        "serializer_revision": "portable-agent-command-v1",
        "provider_validation": "unverified",
        "hard_complete_eligible": False,
        "authority": "shadow-only",
    }
    return history_contract_v2.framed_sha256(
        "provider-command-intent-v1",
        history_contract_v2.canonical_bytes(material),
    )


def validate_command_profile_descriptor(registry, descriptor):
    """Verify a compact command profile without executable lookup or stat."""
    if type(descriptor) is not dict:
        raise ProviderResolutionError("provider profile descriptor must be an object")
    observed_raw = _exact_json_bytes(descriptor)
    if set(descriptor) != _PROFILE_DESCRIPTOR_FIELDS:
        raise ProviderResolutionError("provider profile descriptor fields are not closed")
    surface = descriptor.get("surface")
    provider = descriptor.get("provider")
    _, model, reasoning = _validate_grammar_request(
        registry,
        surface,
        provider,
        descriptor.get("requested_model"),
        descriptor.get("requested_reasoning"),
    )
    _require_sha(
        descriptor.get("execution_request_profile_hash"),
        "execution_request_profile_hash",
    )
    expected = {
        "surface": surface,
        "provider": provider,
        "requested_model": model,
        "requested_reasoning": reasoning,
        "execution_request_profile_hash": _command_profile_hash(
            registry, surface, provider, model, reasoning
        ),
    }
    if observed_raw != _exact_json_bytes(expected):
        raise ProviderResolutionError("provider profile descriptor does not verify")
    return dict(descriptor)


def resolve_command_intent(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    executable_lookup=None,
):
    """Return a no-launch, grammar-only request with shadow authority."""
    executable, executable_path, model, reasoning = _resolve_grammar(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup,
    )
    grammar_revision = registry["providers"][provider]["grammar_revision"]
    serializer_revision = "portable-agent-command-v1"
    profile_hash = _command_profile_hash(
        registry, surface, provider, model, reasoning
    )
    return _issue_command_intent(ProviderCommandIntent(
        provider=provider,
        surface=surface,
        executable=executable,
        executable_path=executable_path,
        requested_model=model,
        requested_reasoning=reasoning,
        effective_model=None,
        effective_reasoning=None,
        model_override_applied=None,
        reasoning_override_applied=None,
        grammar_revision=grammar_revision,
        serializer_revision=serializer_revision,
        provider_validation="unverified",
        execution_request_profile_hash=profile_hash,
        hard_complete_eligible=False,
        authority="shadow-only",
    ))


def command_intent_record(intent):
    """Render the closed, host-path-free diagnostic record for an intent."""
    if not command_intent_is_issued(intent):
        raise ProviderResolutionError("command intent is not resolver-issued")
    return _command_record_from_fields(
        surface=intent.surface,
        provider=intent.provider,
        executable=intent.executable,
        model=intent.requested_model,
        reasoning=intent.requested_reasoning,
        profile_hash=intent.execution_request_profile_hash,
    )


def _command_record_from_fields(
    *, surface, provider, executable, model, reasoning, profile_hash
):
    argv, environment = _render_command_fields(
        provider,
        executable,
        model,
        reasoning,
        _COMMAND_RECORD_MIRROR,
        _COMMAND_RECORD_PROMPT,
    )
    return {
        "schema_version": "provider-command-v1",
        "surface": surface,
        "provider": provider,
        "requested_model": model,
        "requested_reasoning": reasoning,
        "effective_model": None,
        "effective_reasoning": None,
        "model_override_applied": None,
        "reasoning_override_applied": None,
        "model_default": model is None,
        "reasoning_default": reasoning is None,
        "hard_complete_eligible": False,
        "authority": "shadow-only",
        "execution_boundary": "portable-mirror-v1",
        "diagnostic_scope": "grammar-only",
        "grammar_status": "accepted",
        "provider_validation": "unverified",
        "execution_request_profile_hash": profile_hash,
        "argv": argv,
        "environment": environment,
    }


def validate_command_intent_record(registry, record):
    """Verify one path-free command record from the closed grammar only."""
    if type(record) is not dict:
        raise ProviderResolutionError("provider command record must be an object")
    observed_raw = _exact_json_bytes(record)
    if set(record) != _COMMAND_RECORD_FIELDS:
        raise ProviderResolutionError("provider command record fields are not closed")
    surface = record.get("surface")
    provider = record.get("provider")
    executable, model, reasoning = _validate_grammar_request(
        registry,
        surface,
        provider,
        record.get("requested_model"),
        record.get("requested_reasoning"),
    )
    expected = _command_record_from_fields(
        surface=surface,
        provider=provider,
        executable=executable,
        model=model,
        reasoning=reasoning,
        profile_hash=_command_profile_hash(
            registry, surface, provider, model, reasoning
        ),
    )
    if observed_raw != _exact_json_bytes(expected):
        raise ProviderResolutionError("provider command record does not verify")
    return dict(record)


def command_intent_from_record(
    registry,
    record,
    *,
    executable_lookup=None,
):
    """Re-resolve and exactly verify one closed provider-command record."""
    validate_command_intent_record(registry, record)
    intent = resolve_command_intent(
        registry,
        record.get("surface"),
        record.get("provider"),
        model=record.get("requested_model"),
        reasoning=record.get("requested_reasoning"),
        executable_lookup=executable_lookup,
    )
    if _exact_json_bytes(record) != _exact_json_bytes(
        command_intent_record(intent)
    ):
        raise ProviderResolutionError("provider command record does not verify")
    return intent


def load_command_intent(path, registry, *, executable_lookup=None):
    """Load one canonical provider-command record and re-resolve its intent."""
    try:
        raw = pathlib.Path(path).read_bytes()
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResolutionError("provider command record is unreadable") from exc
    if raw != _exact_json_bytes(record):
        raise ProviderResolutionError("provider command record is not canonical")
    return command_intent_from_record(
        registry,
        record,
        executable_lookup=executable_lookup,
    )


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
    executable, executable_path, model, reasoning = _resolve_grammar(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup,
    )
    evidence = (version_probe or _default_probe)(
        provider, executable_path, model, reasoning
    )
    if type(evidence) is not dict or set(evidence) != _PROBE_FIELDS:
        raise ProviderResolutionError("capability probe fields are not closed")
    _require_text(evidence["cli_revision"], "cli_revision")
    _require_text(evidence["serializer_revision"], "serializer_revision")
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
    return _issue_capability(ProviderCapability(
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
    ))


def _render_command_fields(
    provider, executable_path, model, reasoning, mirror, prompt
):
    mirror = str(pathlib.Path(mirror))
    argv = [executable_path]
    if provider == "codex":
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["-c", f"model_reasoning_effort={reasoning}"]
        argv += [
            "-c", "approval_policy=never", "exec", "-s", "workspace-write",
            "--skip-git-repo-check", "--ephemeral", prompt,
        ]
    elif provider == "kimi":
        argv += ["--auto", "--output-format", "text"]
        if model is not None:
            argv += ["-m", model]
        argv += ["-p", prompt]
    elif provider == "grok":
        argv += [
            "--always-approve", "--no-memory", "--no-subagents",
            "--output-format", "plain", "--cwd", mirror,
        ]
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["--reasoning-effort", reasoning]
        argv += ["-p", prompt]
    elif provider == "opencode":
        argv += ["run", "--pure", "--auto", "--dir", mirror]
        if model is not None:
            argv += ["-m", model]
        if reasoning is not None:
            argv += ["--variant", reasoning]
        argv += [prompt]
    elif provider == "agy":
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


def render_command(capability, mirror, prompt, schema_path=None):
    """Return closed argv and a minimal environment delta."""
    if not (
        capability_is_issued(capability)
        or command_intent_is_issued(capability)
    ):
        raise ProviderResolutionError("capability is not resolver-issued")
    if type(prompt) is not str:
        raise ProviderResolutionError("prompt must be text")
    if schema_path is not None:
        raise ProviderResolutionError("provider grammar has no schema-path flag")
    return _render_command_fields(
        capability.provider,
        capability.executable_path,
        capability.model_override,
        capability.reasoning_override,
        mirror,
        prompt,
    )


def capability_is_current(capability, replacement):
    return (
        capability_is_issued(capability)
        and capability_is_issued(replacement)
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
