"""Closed provider resolution for portable history-audit execution."""

import dataclasses
import collections.abc
import hashlib
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import types
import unicodedata
import urllib.parse
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
_PROBE_FACT_FIELDS = frozenset(
    {
        "cli_revision",
        "serializer_revision",
        "effective_model",
        "effective_reasoning",
        "model_override_applied",
        "reasoning_override_applied",
        "immutable_capacity_identity",
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
        "default_probe_revision",
        "model_catalog_probe_revision",
        "model_catalog_sha256",
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
        "effective_model",
        "effective_reasoning",
        "default_probe_revision",
        "model_catalog_probe_revision",
        "model_catalog_sha256",
        "execution_request_profile_hash",
    }
)
_HOST_PROBE_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "exit_code",
        "stdout",
        "stderr",
        "config_bytes",
        "timed_out",
        "truncated",
    }
)
_HOST_PROBE_TIMEOUT_SECONDS = 5
_HOST_CATALOG_PROBE_TIMEOUT_SECONDS = 30
_HOST_PROBE_BYTE_LIMIT = 32768
_HOST_CATALOG_MODEL_LIMIT = 4096
_MULTI_BACKEND_PROVIDERS = frozenset({"opencode", "agy"})
_PROVIDER_REGISTRY_V1_SHA256 = (
    "9bc3335f1166ad0a050ae360504145b32cc2c35cf87b5f818b81ed6806a9afec"
)
_DYNAMIC_MODEL_ROUTE_MARKERS = frozenset(
    {"auto", "default", "current", "configured"}
)
_FORBIDDEN_MODEL_ROUTE_TOKENS = (
    "anthropic",
    "claude",
    "haiku",
    "opus",
    "sonnet",
)
_HOST_PROBE_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
    "XDG_CONFIG_HOME",
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
    default_probe_revision: object
    model_catalog_probe_revision: object
    model_catalog_sha256: object
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


def _issued_snapshot(value, expected_type, *, executable_identity=None):
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
        (
            _captured_executable_identity(attributes["executable_path"])
            if executable_identity is None
            else executable_identity
        ),
    )


def _issue_capability(value, *, executable_identity=None):
    _ISSUED_CAPABILITIES[value] = _issued_snapshot(
        value,
        ProviderCapability,
        executable_identity=executable_identity,
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
        raw = pathlib.Path(path).read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_probe_json_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResolutionError("provider registry is unreadable") from exc
    except ValueError as exc:
        raise ProviderResolutionError("provider registry is not canonical") from exc
    if hashlib.sha256(raw).hexdigest() != _PROVIDER_REGISTRY_V1_SHA256:
        raise ProviderResolutionError("provider registry does not match the tracked v1 ABI")
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
        if type(entry) is not dict or set(entry) != {
            "executable", "grammar_revision", "reasoning_values"
        }:
            raise ProviderResolutionError(f"provider entry is not closed: {name}")
        _require_text(entry["executable"], "executable")
        if entry["executable"] != name:
            raise ProviderResolutionError("provider executable aliases are forbidden")
        _require_text(entry["grammar_revision"], "grammar_revision")
        values = entry["reasoning_values"]
        if (
            type(values) is not list
            or len(values) != len(set(values))
            or any(
                _require_text(value, "reasoning value") != value
                for value in values
            )
        ):
            raise ProviderResolutionError(
                "provider reasoning grammar is invalid"
            )
    if any(_FORBIDDEN in item.lower() for item in _walk_strings(value)):
        raise ProviderResolutionError("forbidden provider path in registry")
    return _issue_registry(value)


def _default_probe(provider, executable_path, model, reasoning):
    return {
        "cli_revision": "unprobed",
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": None,
        "effective_reasoning": None,
        "model_override_applied": model is None,
        "reasoning_override_applied": reasoning is None,
        "immutable_capacity_identity": None,
    }


def _host_version_probe(provider, executable_path, model, reasoning):
    return _default_probe(provider, executable_path, model, reasoning)


def _normalized_model_route(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    for _ in range(64):
        decoded = unicodedata.normalize(
            "NFKC", urllib.parse.unquote(normalized)
        ).casefold()
        if decoded == normalized:
            return normalized.replace("\\", "/")
        normalized = decoded
    raise ProviderResolutionError("provider model route encoding is too deep")


def _model_route_is_forbidden(value):
    normalized = _normalized_model_route(value)
    return any(token in normalized for token in _FORBIDDEN_MODEL_ROUTE_TOKENS)


def _model_route_has_dynamic_marker(value):
    normalized = _normalized_model_route(value)
    words = {item for item in re.split(r"[^a-z0-9]+", normalized) if item}
    return bool(words & _DYNAMIC_MODEL_ROUTE_MARKERS)


def _validate_model_route_policy(provider, model):
    model = _require_text(model, "model")
    if _model_route_is_forbidden(model):
        raise ProviderResolutionError("forbidden provider model route")
    if _model_route_has_dynamic_marker(model):
        raise ProviderResolutionError("dynamic provider model route is forbidden")
    normalized = _normalized_model_route(model)
    if provider == "opencode":
        segments = normalized.split("/")
        if len(segments) < 2 or any(not segment for segment in segments):
            raise ProviderResolutionError(
                "OpenCode model must use an exact provider/model route"
            )
    return model


def _model_catalog_sha256(provider, models):
    material = {
        "schema_version": "provider-model-catalog-v1",
        "provider": provider,
        "models": list(models),
    }
    return history_contract_v2.framed_sha256(
        "provider-model-catalog-v1",
        history_contract_v2.canonical_bytes(material),
    )


def _validate_model_catalog(provider, evidence):
    expected_fields = {
        "schema_version",
        "provider",
        "models",
        "probe_revision",
        "catalog_sha256",
    }
    if (
        type(evidence) is not dict
        or set(evidence) != expected_fields
        or evidence.get("schema_version") != "provider-model-catalog-v1"
        or evidence.get("provider") != provider
    ):
        raise ProviderResolutionError("provider model catalog is unavailable")
    models = evidence.get("models")
    if (
        type(models) is not list
        or not models
        or len(models) > _HOST_CATALOG_MODEL_LIMIT
    ):
        raise ProviderResolutionError("provider model catalog is invalid")
    for model in models:
        _require_text(model, "catalog model")
        if model.strip() != model:
            raise ProviderResolutionError("provider model catalog is invalid")
    if models != sorted(models) or len(models) != len(set(models)):
        raise ProviderResolutionError("provider model catalog is not canonical")
    probe_revision = _require_text(
        evidence.get("probe_revision"), "model_catalog_probe_revision"
    )
    catalog_sha256 = _require_sha(
        evidence.get("catalog_sha256"), "model_catalog_sha256"
    )
    if catalog_sha256 != _model_catalog_sha256(provider, models):
        raise ProviderResolutionError("provider model catalog hash does not verify")
    return tuple(models), probe_revision, catalog_sha256


def _validate_model_catalog_choice(provider, model, evidence):
    model = _validate_model_route_policy(provider, model)
    models, probe_revision, catalog_sha256 = _validate_model_catalog(
        provider, evidence
    )
    if model not in models:
        raise ProviderResolutionError("provider model is absent from the host catalog")
    return model, probe_revision, catalog_sha256


def _validate_default_identity(provider, evidence):
    if provider != "opencode":
        raise ProviderResolutionError(
            "provider default identity is unavailable"
        )
    if (
        type(evidence) is not dict
        or set(evidence)
        != {"schema_version", "provider", "effective_model", "probe_revision"}
        or evidence.get("schema_version") != "provider-default-identity-v1"
        or evidence.get("provider") != provider
    ):
        raise ProviderResolutionError(
            "provider default identity is unavailable"
        )
    effective_model = _require_text(
        evidence.get("effective_model"), "effective_model"
    )
    _require_text(evidence.get("probe_revision"), "probe_revision")
    _validate_model_route_policy(provider, effective_model)
    return effective_model, evidence["probe_revision"]


def _host_probe_environment():
    return {
        key: value
        for key in _HOST_PROBE_ENVIRONMENT_KEYS
        if (
            type((value := os.environ.get(key))) is str
            and value
            and not any(character in value for character in "\0\r\n")
            and unicodedata.normalize("NFC", value) == value
        )
    }


def _host_default_identity_probe(provider, executable_path):
    """Resolve a multi-backend CLI default without starting a model workload."""
    if provider != "opencode":
        return None
    environment = _host_probe_environment()
    try:
        with tempfile.TemporaryDirectory(prefix="provider-default-probe-") as directory:
            observation = _run_bounded_default_probe(
                [executable_path, "--pure", "debug", "config"],
                cwd=directory,
                env=environment,
            )
    except OSError:
        return None
    if observation is None:
        return None
    returncode, stdout, stderr = observation
    if (
        returncode != 0
        or len(stdout) > _HOST_PROBE_BYTE_LIMIT
        or len(stderr) > _HOST_PROBE_BYTE_LIMIT
    ):
        return None
    try:
        config = json.loads(
            stdout.decode("utf-8"),
            object_pairs_hook=_unique_probe_json_object,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if type(config) is not dict or type(config.get("model")) is not str:
        return None
    return {
        "schema_version": "provider-default-identity-v1",
        "provider": provider,
        "effective_model": config["model"],
        "probe_revision": "opencode-pure-debug-config-v1",
    }


def _host_model_catalog_probe(provider, executable_path):
    """Read a bounded local CLI model catalog without starting a model workload."""
    argv_tail = {
        "opencode": ["models", "--pure"],
        "agy": ["models"],
    }.get(provider)
    if argv_tail is None:
        return None
    try:
        with tempfile.TemporaryDirectory(
            prefix="provider-model-catalog-"
        ) as directory:
            observation = _run_bounded_default_probe(
                [executable_path, *argv_tail],
                cwd=directory,
                env=_host_probe_environment(),
                timeout_seconds=_HOST_CATALOG_PROBE_TIMEOUT_SECONDS,
            )
    except OSError:
        return None
    if observation is None:
        return None
    returncode, stdout, stderr = observation
    if returncode != 0 or stderr or not stdout or not stdout.endswith(b"\n"):
        return None
    if b"\r" in stdout:
        return None
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = decoded[:-1].split("\n")
    if not lines or len(lines) > _HOST_CATALOG_MODEL_LIMIT:
        return None
    try:
        for line in lines:
            _require_text(line, "catalog model")
            if line.strip() != line:
                raise ProviderResolutionError("provider model catalog is invalid")
    except ProviderResolutionError:
        return None
    if len(lines) != len(set(lines)):
        return None
    models = sorted(lines)
    evidence = {
        "schema_version": "provider-model-catalog-v1",
        "provider": provider,
        "models": models,
        "probe_revision": {
            "opencode": "opencode-models-pure-v1",
            "agy": "agy-models-v1",
        }[provider],
        "catalog_sha256": _model_catalog_sha256(provider, models),
    }
    try:
        _validate_model_catalog(provider, evidence)
    except ProviderResolutionError:
        return None
    return evidence


def _unique_probe_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate provider probe JSON key")
        value[key] = item
    return value


def _kill_probe_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_bounded_default_probe(
    argv, *, cwd, env, timeout_seconds=_HOST_PROBE_TIMEOUT_SECONDS
):
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    captures = {}
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
            captures[stream] = bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_probe_process(process)
                return None
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                capture = captures[stream]
                if len(capture) + len(chunk) > _HOST_PROBE_BYTE_LIMIT:
                    _kill_probe_process(process)
                    return None
                capture.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_probe_process(process)
            return None
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_probe_process(process)
            return None
        return (
            process.returncode,
            bytes(captures[process.stdout]),
            bytes(captures[process.stderr]),
        )
    finally:
        selector.close()
        if process.poll() is None:
            _kill_probe_process(process)
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


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
    if provider in _MULTI_BACKEND_PROVIDERS and model is not None:
        _validate_model_route_policy(provider, model)
    allowed_reasoning = registry["providers"][provider]["reasoning_values"]
    if reasoning is not None and reasoning not in allowed_reasoning:
        raise ProviderResolutionError(
            f"{provider} reasoning override is unsupported"
        )
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
    if not callable(executable_lookup):
        raise ProviderResolutionError("provider executable lookup is unavailable")
    executable_path = executable_lookup(executable)
    if type(executable_path) is not str or not executable_path:
        raise ProviderResolutionError("provider executable is unavailable")
    executable_path = str(pathlib.Path(executable_path).resolve())
    if _FORBIDDEN in pathlib.Path(executable_path).name.lower():
        raise ProviderResolutionError("forbidden provider executable")
    return executable, executable_path, model, reasoning


def _reject_caller_host_services(*services):
    if any(service is not None for service in services):
        raise ProviderResolutionError(
            "provider host services cannot be caller-supplied"
        )


def _host_executable_lookup(executable):
    return shutil.which(executable)


def _command_profile_hash(
    registry,
    surface,
    provider,
    model,
    reasoning,
    effective_model=None,
    effective_reasoning=None,
    default_probe_revision=None,
    model_catalog_probe_revision=None,
    model_catalog_sha256=None,
):
    material = {
        "provider": provider,
        "surface": surface,
        "executable": provider,
        "requested_model": model,
        "requested_reasoning": reasoning,
        "effective_model": effective_model,
        "effective_reasoning": effective_reasoning,
        "default_probe_revision": default_probe_revision,
        "model_catalog_probe_revision": model_catalog_probe_revision,
        "model_catalog_sha256": model_catalog_sha256,
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
    effective_model = _require_text(
        descriptor.get("effective_model"),
        "effective_model",
        optional=True,
    )
    effective_reasoning = _require_text(
        descriptor.get("effective_reasoning"),
        "effective_reasoning",
        optional=True,
    )
    default_probe_revision = _require_text(
        descriptor.get("default_probe_revision"),
        "default_probe_revision",
        optional=True,
    )
    model_catalog_probe_revision = _require_text(
        descriptor.get("model_catalog_probe_revision"),
        "model_catalog_probe_revision",
        optional=True,
    )
    model_catalog_sha256 = descriptor.get("model_catalog_sha256")
    if model_catalog_sha256 is not None:
        _require_sha(model_catalog_sha256, "model_catalog_sha256")
    if effective_reasoning is not None:
        raise ProviderResolutionError("unexpected effective reasoning identity")
    if provider in _MULTI_BACKEND_PROVIDERS:
        if model is None:
            effective_model, default_probe_revision = _validate_default_identity(
                provider,
                {
                    "schema_version": "provider-default-identity-v1",
                    "provider": provider,
                    "effective_model": effective_model,
                    "probe_revision": default_probe_revision,
                },
            )
        elif effective_model != model or default_probe_revision is not None:
            raise ProviderResolutionError("provider effective model does not verify")
        _validate_model_route_policy(provider, effective_model)
        if model_catalog_probe_revision is None or model_catalog_sha256 is None:
            raise ProviderResolutionError("provider model catalog is unavailable")
    elif any(
        value is not None
        for value in (
            effective_model,
            default_probe_revision,
            model_catalog_probe_revision,
            model_catalog_sha256,
        )
    ):
        raise ProviderResolutionError("unexpected provider default identity")
    _require_sha(
        descriptor.get("execution_request_profile_hash"),
        "execution_request_profile_hash",
    )
    expected = {
        "surface": surface,
        "provider": provider,
        "requested_model": model,
        "requested_reasoning": reasoning,
        "effective_model": effective_model,
        "effective_reasoning": effective_reasoning,
        "default_probe_revision": default_probe_revision,
        "model_catalog_probe_revision": model_catalog_probe_revision,
        "model_catalog_sha256": model_catalog_sha256,
        "execution_request_profile_hash": _command_profile_hash(
            registry,
            surface,
            provider,
            model,
            reasoning,
            effective_model,
            effective_reasoning,
            default_probe_revision,
            model_catalog_probe_revision,
            model_catalog_sha256,
        ),
    }
    if observed_raw != _exact_json_bytes(expected):
        raise ProviderResolutionError("provider profile descriptor does not verify")
    return dict(descriptor)


def _resolve_command_intent(
    registry,
    surface,
    provider,
    model,
    reasoning,
    *,
    executable_lookup,
    default_identity_probe=None,
    model_catalog_probe=None,
):
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
    effective_model = None
    default_probe_revision = None
    model_catalog_probe_revision = None
    model_catalog_sha256 = None
    if provider in _MULTI_BACKEND_PROVIDERS:
        if model is None:
            if not callable(default_identity_probe):
                raise ProviderResolutionError(
                    "provider default identity is unavailable"
                )
            effective_model, default_probe_revision = _validate_default_identity(
                provider,
                default_identity_probe(provider, executable_path),
            )
        else:
            effective_model = model
        if not callable(model_catalog_probe):
            raise ProviderResolutionError("provider model catalog is unavailable")
        (
            effective_model,
            model_catalog_probe_revision,
            model_catalog_sha256,
        ) = _validate_model_catalog_choice(
            provider,
            effective_model,
            model_catalog_probe(provider, executable_path),
        )
    profile_hash = _command_profile_hash(
        registry,
        surface,
        provider,
        model,
        reasoning,
        effective_model,
        None,
        default_probe_revision,
        model_catalog_probe_revision,
        model_catalog_sha256,
    )
    return _issue_command_intent(ProviderCommandIntent(
        provider=provider,
        surface=surface,
        executable=executable,
        executable_path=executable_path,
        requested_model=model,
        requested_reasoning=reasoning,
        effective_model=effective_model,
        effective_reasoning=None,
        default_probe_revision=default_probe_revision,
        model_catalog_probe_revision=model_catalog_probe_revision,
        model_catalog_sha256=model_catalog_sha256,
        model_override_applied=None,
        reasoning_override_applied=None,
        grammar_revision=grammar_revision,
        serializer_revision=serializer_revision,
        provider_validation="unverified",
        execution_request_profile_hash=profile_hash,
        hard_complete_eligible=False,
        authority="shadow-only",
    ))


def resolve_command_intent(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    executable_lookup=None,
):
    """Return a host-resolved, grammar-only request with shadow authority."""
    _reject_caller_host_services(executable_lookup)
    return _resolve_command_intent(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup=_host_executable_lookup,
        default_identity_probe=_host_default_identity_probe,
        model_catalog_probe=_host_model_catalog_probe,
    )


def _resolve_command_intent_for_test(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    *,
    executable_lookup,
    default_identity_probe=None,
    model_catalog_probe=None,
):
    """Resolve a fake executable for offline tests; authority stays shadow-only."""
    return _resolve_command_intent(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup=executable_lookup,
        default_identity_probe=default_identity_probe,
        model_catalog_probe=model_catalog_probe,
    )


def revalidate_command_intent_for_launch(intent):
    """Re-probe multi-backend model authority immediately before launch."""
    if not command_intent_is_issued(intent):
        raise ProviderResolutionError("command intent is not resolver-issued")
    if intent.provider not in _MULTI_BACKEND_PROVIDERS:
        return True
    effective_model = intent.requested_model
    default_probe_revision = None
    if effective_model is None:
        effective_model, default_probe_revision = _validate_default_identity(
            intent.provider,
            _host_default_identity_probe(
                intent.provider,
                intent.executable_path,
            ),
        )
    (
        effective_model,
        model_catalog_probe_revision,
        model_catalog_sha256,
    ) = _validate_model_catalog_choice(
        intent.provider,
        effective_model,
        _host_model_catalog_probe(
            intent.provider,
            intent.executable_path,
        ),
    )
    if (
        effective_model != intent.effective_model
        or default_probe_revision != intent.default_probe_revision
        or model_catalog_probe_revision
        != intent.model_catalog_probe_revision
        or model_catalog_sha256 != intent.model_catalog_sha256
    ):
        raise ProviderResolutionError("provider model authority changed")
    return True


def revalidate_command_intent_default(intent):
    """Compatibility wrapper for the launch-time model authority check."""
    return revalidate_command_intent_for_launch(intent)


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
        effective_model=intent.effective_model,
        default_probe_revision=intent.default_probe_revision,
        model_catalog_probe_revision=intent.model_catalog_probe_revision,
        model_catalog_sha256=intent.model_catalog_sha256,
        profile_hash=intent.execution_request_profile_hash,
    )


def _command_record_from_fields(
    *, surface, provider, executable, model, reasoning, effective_model,
    default_probe_revision, model_catalog_probe_revision,
    model_catalog_sha256, profile_hash
):
    execution_model = model
    if provider in _MULTI_BACKEND_PROVIDERS:
        if effective_model is None:
            raise ProviderResolutionError(
                "provider effective model is unavailable"
            )
        execution_model = effective_model
    argv, environment = _render_command_fields(
        provider,
        executable,
        execution_model,
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
        "effective_model": effective_model,
        "effective_reasoning": None,
        "default_probe_revision": default_probe_revision,
        "model_catalog_probe_revision": model_catalog_probe_revision,
        "model_catalog_sha256": model_catalog_sha256,
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
    effective_model = record.get("effective_model")
    default_probe_revision = None
    model_catalog_probe_revision = record.get("model_catalog_probe_revision")
    model_catalog_sha256 = record.get("model_catalog_sha256")
    if provider in _MULTI_BACKEND_PROVIDERS:
        if model is None:
            effective_model, default_probe_revision = _validate_default_identity(
                provider,
                {
                    "schema_version": "provider-default-identity-v1",
                    "provider": provider,
                    "effective_model": effective_model,
                    "probe_revision": record.get("default_probe_revision"),
                },
            )
        elif effective_model != model or record.get("default_probe_revision") is not None:
            raise ProviderResolutionError("provider effective model does not verify")
        _validate_model_route_policy(provider, effective_model)
        _require_text(
            model_catalog_probe_revision, "model_catalog_probe_revision"
        )
        _require_sha(model_catalog_sha256, "model_catalog_sha256")
    elif any(
        value is not None
        for value in (
            effective_model,
            record.get("default_probe_revision"),
            model_catalog_probe_revision,
            model_catalog_sha256,
        )
    ):
        raise ProviderResolutionError("unexpected provider default identity")
    expected = _command_record_from_fields(
        surface=surface,
        provider=provider,
        executable=executable,
        model=model,
        reasoning=reasoning,
        effective_model=effective_model,
        default_probe_revision=default_probe_revision,
        model_catalog_probe_revision=model_catalog_probe_revision,
        model_catalog_sha256=model_catalog_sha256,
        profile_hash=_command_profile_hash(
            registry,
            surface,
            provider,
            model,
            reasoning,
            effective_model,
            None,
            default_probe_revision,
            model_catalog_probe_revision,
            model_catalog_sha256,
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
    _reject_caller_host_services(executable_lookup)
    validate_command_intent_record(registry, record)
    intent = resolve_command_intent(
        registry,
        record.get("surface"),
        record.get("provider"),
        model=record.get("requested_model"),
        reasoning=record.get("requested_reasoning"),
    )
    if _exact_json_bytes(record) != _exact_json_bytes(
        command_intent_record(intent)
    ):
        raise ProviderResolutionError("provider command record does not verify")
    return intent


def load_command_intent(path, registry, *, executable_lookup=None):
    """Load one canonical provider-command record and re-resolve its intent."""
    _reject_caller_host_services(executable_lookup)
    try:
        raw = pathlib.Path(path).read_bytes()
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResolutionError("provider command record is unreadable") from exc
    if raw != _exact_json_bytes(record):
        raise ProviderResolutionError("provider command record is not canonical")
    return command_intent_from_record(registry, record)


def _probe_evidence_sha256(
    registry,
    surface,
    provider,
    executable,
    executable_path,
    model,
    reasoning,
    evidence,
    executable_identity,
    issuance_scope,
    observation_sha256=None,
):
    identity_fields = (
        "device",
        "inode",
        "mode",
        "size",
        "mtime_ns",
        "ctime_ns",
    )
    material = {
        "schema_version": "provider-capability-evidence-v1",
        "issuance_scope": issuance_scope,
        "provider": provider,
        "surface": surface,
        "executable": executable,
        "executable_path": executable_path,
        "executable_identity": dict(zip(identity_fields, executable_identity)),
        "requested_model": model,
        "requested_reasoning": reasoning,
        "effective_model": evidence["effective_model"],
        "effective_reasoning": evidence["effective_reasoning"],
        "model_override_applied": evidence["model_override_applied"],
        "reasoning_override_applied": evidence["reasoning_override_applied"],
        "immutable_capacity_identity": evidence["immutable_capacity_identity"],
        "cli_revision": evidence["cli_revision"],
        "serializer_revision": evidence["serializer_revision"],
        "grammar_revision": registry["providers"][provider]["grammar_revision"],
    }
    if observation_sha256 is not None:
        material["host_observation_sha256"] = _require_sha(
            observation_sha256,
            "host_observation_sha256",
        )
    return history_contract_v2.framed_sha256(
        "provider-capability-evidence-v1",
        history_contract_v2.canonical_bytes(material),
    )


def _resolve_provider(
    registry,
    surface,
    provider,
    model,
    reasoning,
    *,
    executable_lookup,
    version_probe,
    issuance_scope,
    allow_hard_complete,
    observation_sha256=None,
):
    executable, executable_path, model, reasoning = _resolve_grammar(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup,
    )
    if not callable(version_probe):
        raise ProviderResolutionError("provider capability probe is unavailable")
    executable_identity = _captured_executable_identity(executable_path)
    evidence = version_probe(
        provider, executable_path, model, reasoning
    )
    if type(evidence) is not dict or set(evidence) != _PROBE_FACT_FIELDS:
        raise ProviderResolutionError("capability probe fields are not closed")
    _require_text(evidence["cli_revision"], "cli_revision")
    _require_text(evidence["serializer_revision"], "serializer_revision")
    if evidence["serializer_revision"] != "portable-agent-command-v1":
        raise ProviderResolutionError("serializer revision is unsupported")
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
    current_executable_identity = _captured_executable_identity(executable_path)
    if current_executable_identity != executable_identity:
        raise ProviderResolutionError("provider executable changed during probe")
    evidence = dict(evidence)
    evidence["evidence_sha256"] = _probe_evidence_sha256(
        registry,
        surface,
        provider,
        executable,
        executable_path,
        model,
        reasoning,
        evidence,
        executable_identity,
        issuance_scope,
        observation_sha256,
    )
    model_identity = evidence["effective_model"] or "provider-default"
    reasoning_identity = evidence["effective_reasoning"] or "provider-default"
    hard_complete = allow_hard_complete and bool(
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
    ), executable_identity=executable_identity)


def resolve_provider(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    executable_lookup=None,
    version_probe=None,
):
    """Return a capability issued only from host-owned lookup and probe services."""
    _reject_caller_host_services(executable_lookup, version_probe)
    return _resolve_provider(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup=_host_executable_lookup,
        version_probe=_host_version_probe,
        issuance_scope="host-owned",
        allow_hard_complete=True,
    )


def _resolve_provider_for_test(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    *,
    executable_lookup,
    version_probe,
):
    """Resolve fake offline evidence without granting hard-complete authority."""
    return _resolve_provider(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup=executable_lookup,
        version_probe=version_probe,
        issuance_scope="test-only-shadow",
        allow_hard_complete=False,
    )


def _sanitized_test_probe_environment():
    environment = {}
    for key in _HOST_PROBE_ENVIRONMENT_KEYS:
        value = os.environ.get(key)
        if (
            type(value) is str
            and value
            and not any(character in value for character in "\0\r\n")
            and unicodedata.normalize("NFC", value) == value
        ):
            environment[key] = value
    return types.MappingProxyType(environment)


def _test_host_probe_invocation(
    *, surface, provider, executable_path, model, reasoning
):
    return types.MappingProxyType(
        {
            "schema_version": "provider-host-probe-invocation-v1",
            "purpose": "capability-introspection",
            "surface": surface,
            "provider": provider,
            "executable_path": executable_path,
            "argv": (
                executable_path,
                "--test-only-capability-introspection-v1",
            ),
            "environment": _sanitized_test_probe_environment(),
            "timeout_seconds": _HOST_PROBE_TIMEOUT_SECONDS,
            "stdout_limit_bytes": _HOST_PROBE_BYTE_LIMIT,
            "stderr_limit_bytes": _HOST_PROBE_BYTE_LIMIT,
            "config_limit_bytes": _HOST_PROBE_BYTE_LIMIT,
            "requested_model": model,
            "requested_reasoning": reasoning,
        }
    )


def _validate_test_host_probe_observation(observation):
    if (
        type(observation) is not dict
        or set(observation) != _HOST_PROBE_OBSERVATION_FIELDS
    ):
        raise ProviderResolutionError("host probe observation fields are not closed")
    if observation["schema_version"] != "provider-host-probe-observation-v1":
        raise ProviderResolutionError("host probe observation schema is unsupported")
    if type(observation["exit_code"]) is not int:
        raise ProviderResolutionError("host probe exit code is invalid")
    for field in ("timed_out", "truncated"):
        if type(observation[field]) is not bool:
            raise ProviderResolutionError(f"host probe {field} marker is invalid")
    for field in ("stdout", "stderr", "config_bytes"):
        value = observation[field]
        if type(value) is not bytes or len(value) > _HOST_PROBE_BYTE_LIMIT:
            raise ProviderResolutionError(f"host probe {field} is invalid")
    return observation


def _test_host_probe_observation_sha256(invocation, observation):
    material = {
        "schema_version": "provider-host-probe-evidence-input-v1",
        "purpose": invocation["purpose"],
        "surface": invocation["surface"],
        "provider": invocation["provider"],
        "executable_path": invocation["executable_path"],
        "argv": list(invocation["argv"]),
        "environment_sha256": history_contract_v2.framed_sha256(
            "provider-host-probe-environment-v1",
            history_contract_v2.canonical_bytes(dict(invocation["environment"])),
        ),
        "timeout_seconds": invocation["timeout_seconds"],
        "stdout_limit_bytes": invocation["stdout_limit_bytes"],
        "stderr_limit_bytes": invocation["stderr_limit_bytes"],
        "config_limit_bytes": invocation["config_limit_bytes"],
        "requested_model": invocation["requested_model"],
        "requested_reasoning": invocation["requested_reasoning"],
        "observation_schema_version": observation["schema_version"],
        "exit_code": observation["exit_code"],
        "stdout_sha256": hashlib.sha256(observation["stdout"]).hexdigest(),
        "stdout_bytes": len(observation["stdout"]),
        "stderr_sha256": hashlib.sha256(observation["stderr"]).hexdigest(),
        "stderr_bytes": len(observation["stderr"]),
        "config_sha256": hashlib.sha256(observation["config_bytes"]).hexdigest(),
        "config_bytes": len(observation["config_bytes"]),
        "timed_out": observation["timed_out"],
        "truncated": observation["truncated"],
    }
    return history_contract_v2.framed_sha256(
        "provider-host-probe-observation-v1",
        history_contract_v2.canonical_bytes(material),
    )


def _parse_test_host_probe_observation(provider, model, reasoning, observation):
    if (
        observation["exit_code"] != 0
        or observation["timed_out"]
        or observation["truncated"]
    ):
        raise ProviderResolutionError("host probe observation is incomplete")

    try:
        version_output = observation["stdout"].decode("utf-8")
        config_output = observation["config_bytes"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderResolutionError("host probe observation is not UTF-8") from exc
    version_prefix = "fixture-version-output:"
    if (
        not version_output.startswith(version_prefix)
        or not version_output.endswith("\n")
        or version_output.count("\n") != 1
    ):
        raise ProviderResolutionError("host probe CLI revision is ambiguous")
    cli_revision = _require_text(
        version_output[len(version_prefix):-1],
        "cli_revision",
    )

    lines = config_output.splitlines(keepends=True)
    if not lines or any(not line.endswith("\n") for line in lines):
        raise ProviderResolutionError("host probe config is not line-framed")
    if lines[0] != "fixture-capability-config-v1\n":
        raise ProviderResolutionError("host probe config format is unknown")
    expected_keys = (
        "provider",
        "configured_model",
        "configured_reasoning",
        "model_source",
        "reasoning_source",
        "capacity_identity",
    )
    if len(lines) != len(expected_keys) + 1:
        raise ProviderResolutionError("host probe config fields are ambiguous")
    parsed = {}
    for expected_key, line in zip(expected_keys, lines[1:]):
        key, separator, value = line[:-1].partition("=")
        if not separator or key != expected_key or key in parsed:
            raise ProviderResolutionError("host probe config fields are ambiguous")
        parsed[key] = value
    if parsed["provider"] != provider:
        raise ProviderResolutionError("host probe provider does not match")
    effective_model = _require_text(parsed["configured_model"], "effective_model")
    effective_reasoning = _require_text(
        parsed["configured_reasoning"],
        "effective_reasoning",
    )
    capacity_identity = parsed["capacity_identity"] or None
    _require_text(
        capacity_identity,
        "immutable_capacity_identity",
        optional=True,
    )
    if parsed["model_source"] not in {"default", "argv-model-override"}:
        raise ProviderResolutionError("host probe model source is unknown")
    if parsed["reasoning_source"] not in {
        "default",
        "argv-reasoning-override",
    }:
        raise ProviderResolutionError("host probe reasoning source is unknown")
    if model is None and parsed["model_source"] != "default":
        raise ProviderResolutionError("host probe model source is ambiguous")
    if reasoning is None and parsed["reasoning_source"] != "default":
        raise ProviderResolutionError("host probe reasoning source is ambiguous")
    return {
        "cli_revision": cli_revision,
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": effective_model,
        "effective_reasoning": effective_reasoning,
        "model_override_applied": (
            model is None
            or (
                parsed["model_source"] == "argv-model-override"
                and effective_model == model
            )
        ),
        "reasoning_override_applied": (
            reasoning is None
            or (
                parsed["reasoning_source"] == "argv-reasoning-override"
                and effective_reasoning == reasoning
            )
        ),
        "immutable_capacity_identity": capacity_identity,
    }


def _resolve_provider_with_test_host_probe_runner(
    registry,
    surface,
    provider,
    model=None,
    reasoning=None,
    *,
    executable_lookup,
    probe_runner,
):
    """Exercise host observation capture with fake inputs and shadow authority."""
    executable, executable_path, model, reasoning = _resolve_grammar(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup,
    )
    if not callable(probe_runner):
        raise ProviderResolutionError("provider host probe runner is unavailable")
    executable_identity = _captured_executable_identity(executable_path)
    invocation = _test_host_probe_invocation(
        surface=surface,
        provider=provider,
        executable_path=executable_path,
        model=model,
        reasoning=reasoning,
    )
    strict_override = model is not None or reasoning is not None
    observation_sha256 = None
    try:
        observation = _validate_test_host_probe_observation(probe_runner(invocation))
        observation_sha256 = _test_host_probe_observation_sha256(
            invocation,
            observation,
        )
        evidence = _parse_test_host_probe_observation(
            provider,
            model,
            reasoning,
            observation,
        )
    except ProviderResolutionError:
        if strict_override:
            raise
        evidence = _default_probe(provider, executable_path, model, reasoning)
    if _captured_executable_identity(executable_path) != executable_identity:
        raise ProviderResolutionError("provider executable changed during probe")
    return _resolve_provider(
        registry,
        surface,
        provider,
        model,
        reasoning,
        executable_lookup=lambda requested: (
            executable_path if requested == executable else None
        ),
        version_probe=lambda *_: evidence,
        issuance_scope="test-only-shadow",
        allow_hard_complete=False,
        observation_sha256=observation_sha256,
    )


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
        if reasoning is not None:
            raise ProviderResolutionError("Kimi reasoning override is unsupported")
        argv += ["--auto", "--output-format", "text"]
        if model is not None:
            argv += ["-m", model]
        argv += ["-p", prompt]
    elif provider == "grok":
        argv += [
            "--always-approve", "--no-memory", "--no-subagents",
            "--output-format", "json", "--cwd", mirror,
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
    if (
        capability.provider in _MULTI_BACKEND_PROVIDERS
        and capability_is_issued(capability)
    ):
        raise ProviderResolutionError(
            "multi-backend execution requires catalog-bound command intent"
        )
    execution_model = capability.model_override
    if (
        capability.provider in _MULTI_BACKEND_PROVIDERS
        and execution_model is None
    ):
        if not command_intent_is_issued(capability):
            raise ProviderResolutionError(
                "multi-backend capability requires an explicit model"
            )
        if capability.effective_model is None:
            raise ProviderResolutionError(
                "provider default identity is unavailable"
            )
        execution_model = capability.effective_model
    return _render_command_fields(
        capability.provider,
        capability.executable_path,
        execution_model,
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
    _reject_caller_host_services(executable_lookup, version_probe)
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ProviderResolutionError("provider pool must be a non-empty ordered list")
    if len(set(providers)) != len(providers):
        raise ProviderResolutionError("provider pool cannot contain duplicates")
    return tuple(
        resolve_provider(
            registry,
            surface,
            provider,
        )
        for provider in providers
    )


def _resolve_pool_for_test(
    registry,
    surface,
    providers,
    *,
    executable_lookup,
    version_probe,
):
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ProviderResolutionError("provider pool must be a non-empty ordered list")
    if len(set(providers)) != len(providers):
        raise ProviderResolutionError("provider pool cannot contain duplicates")
    return tuple(
        _resolve_provider_for_test(
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
