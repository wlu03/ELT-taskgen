"""Role routing and agents configuration, shared by all provider backends."""

from __future__ import annotations
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.package_resources import resource_path
from elt_taskgen.review.session import SessionLimits
from elt_taskgen.review import provider_types as _review_provider_types

DEFAULT_SANDBOX_PIN: Mapping[str, str] = {
    "runtime": "none",
    "image_digest": "",
    "workspace_template_sha256": "",
}

_SANDBOX_PIN_KEYS = ("runtime", "image_digest", "workspace_template_sha256")


def default_agents_config_path() -> Path:
    """The checkout or wheel's single packaged agents configuration."""

    return resource_path("config/agents.yaml")


def _read_agents_document(path: Path) -> dict:
    """Parse one immutable byte snapshot of an agents configuration."""

    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(
            f"agents config not found: {source} (packaged resource required; fail closed)"
        ) from exc
    try:
        doc = yaml.safe_load(payload.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"agents config {source} is not valid UTF-8 YAML") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"agents config {source} is not a mapping (fail closed)")
    return doc


def _agents_doc() -> dict:
    """Read the packaged config/agents.yaml, without env interpolation.

    This helper deliberately does not cache by pathname: a new construction
    must observe a file changed since the previous construction. Callers that
    need routing and behaviour to agree retain and pass the returned document.
    """

    return _read_agents_document(default_agents_config_path())


def _agents_doc_at(resolved_path: str) -> dict:
    """Read one explicit agents document as written (no interpolation)."""

    return _read_agents_document(Path(resolved_path))


def _config_key(agents_config: Path | str | None) -> str:
    """The cache key of an agents document: '' for the repository default
    (checkout or packaged wheel resource), else the resolved path of the
    explicit document. The key selects which snapshot is hashed; the path
    itself never enters a digest."""
    if agents_config is None:
        return ""
    candidate = Path(agents_config)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    try:
        if resolved == default_agents_config_path().resolve():
            return ""
    except OSError:
        pass
    return str(resolved)


def _agents_doc_for(
    agents_config: Path | str | Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Resolve an agents document from a retained snapshot or a path.

    A mapping is already the construction-scoped byte snapshot and is never
    reopened. A path (or the packaged default) is read afresh, so independent
    later constructions keep the historical reload semantics.
    """
    if isinstance(agents_config, Mapping):
        return agents_config
    key = _config_key(agents_config)
    return _agents_doc() if not key else _agents_doc_at(key)


def _agents_document_of(routing: "RoleRouting") -> Mapping[str, Any]:
    """The raw document captured with `routing`, or one snapshot now.

    `load_role_routing` always supplies the captured document. The fallback
    supports hand-built/test routings without changing their public shape.
    """
    captured = getattr(routing, "agents_document", None)
    if isinstance(captured, Mapping):
        return captured
    return _agents_doc_for(agents_config_of(routing))


def agents_config_of(routing: "RoleRouting") -> Path | None:
    """The agents document `routing` was loaded from, as the path the
    behaviour manifest, transcript key and route block must hash: None for
    the packaged default or a routing built in code (`_agents_doc()` applies
    unchanged), else the explicit `--agents-config` path
    `load_role_routing` snapshotted."""
    source = str(getattr(routing, "source", "") or "")
    if not source:
        return None
    candidate = Path(source)
    if not candidate.is_file():
        return None
    return candidate if _config_key(candidate) else None


def role_loop_limits(
    role_name: str,
    *,
    agents_config: Path | str | Mapping[str, Any] | None = None,
) -> dict:
    """Return a role's canonical declared `session` block.

    An undeclared role returns `{}` for one-shot behavior. The supplied agents document
    is used when present; `role_manifest_limits` decides the hashed enabled/disabled
    form.
    """
    roles = _agents_doc_for(agents_config).get("roles") or {}
    spec = roles.get(role_name) if isinstance(roles, Mapping) else None
    block = spec.get("session") if isinstance(spec, Mapping) else None
    plain = dict(block) if isinstance(block, Mapping) else {}
    if role_name == "semantic_author" and plain:
        # Derive author validators and correction limits from the enforced limits;
        # reject routing config that would make the hashed declaration disagree.
        from elt_taskgen.review.tools import (
            validators as _author_tools,
        )

        derived = _author_tools.author_limits(plain).as_manifest()
        for key in ("harness_validators", "max_compile_corrections"):
            if key in plain and _review_provider_types._plain_data(plain[key]) != derived[key]:
                raise ValueError(
                    f"roles.semantic_author.session.{key} = {plain[key]!r} disagrees with "
                    f"the SoT derivation {derived[key]!r}; declare exactly that value or "
                    "omit the key (it is derived from max_revisions and the AUT validators)"
                )
        return derived
    return SessionLimits.from_block(plain).as_manifest()

SESSION_RUNNER_ROLES: tuple[str, ...] = (
    "independent_implementer",
    "independent_loader",
    "repair_proposer",
    "semantic_author",
)

WITNESS_RUNNER_ROLES: tuple[str, ...] = ("independent_implementer", "independent_loader")


def role_manifest_limits(
    role_name: str,
    *,
    agents_config: Path | str | Mapping[str, Any] | None = None,
) -> dict:
    """Return the loop limits hashed into a role's behavior manifest.

    Use the validated declared block when the runner enforces it, `{'enabled': false}`
    for a disabled declared runner, and `{}` for a disabled witness compatibility path.
    Disabled settings that no runner reads do not rekey one-shot transcripts.
    """
    declared = role_loop_limits(role_name, agents_config=agents_config)
    if not declared or role_name not in SESSION_RUNNER_ROLES:
        return declared
    if bool(declared.get("enabled", False)):
        return declared
    if role_name in WITNESS_RUNNER_ROLES:
        return {}
    return {"enabled": False}


def sandbox_pin(
    *, agents_config: Path | str | Mapping[str, Any] | None = None
) -> dict:
    """The pinned sandbox declaration `{runtime, image_digest,
    workspace_template_sha256}` from `metrology.sandbox` in
    config/agents.yaml (R-F) — or in the explicit `agents_config` document.
    A DECLARATION, never observed state: the attestation a run measures is
    compared against this pin at run start and stamped on records, but only
    the pin is hashed."""
    metrology = _agents_doc_for(agents_config).get("metrology") or {}
    declared = metrology.get("sandbox") if isinstance(metrology, Mapping) else None
    if not isinstance(declared, Mapping):
        return dict(DEFAULT_SANDBOX_PIN)
    unknown = sorted(set(declared) - set(_SANDBOX_PIN_KEYS))
    if unknown:
        raise ValueError(
            f"metrology.sandbox declares unknown key(s) {unknown}; the pin is "
            f"exactly {list(_SANDBOX_PIN_KEYS)}"
        )
    return {
        key: str(declared.get(key) if declared.get(key) is not None else DEFAULT_SANDBOX_PIN[key])
        for key in _SANDBOX_PIN_KEYS
    }


def agents_config_sha256(
    *, agents_config: Path | str | Mapping[str, Any] | None = None
) -> str:
    """Hash the complete parsed agents configuration.

    Use canonical JSON after interpolation and validation. This identity binds custom
    routing documents without depending on file path or formatting.
    """

    return sha256_hex(canonical_json(_agents_doc_for(agents_config)))


def agents_config_identity(
    *, agents_config: Path | str | Mapping[str, Any] | None = None
) -> tuple[dict[str, str], str]:
    """Return ``(sandbox_pin, config_sha256)`` from one document snapshot.

    Keeping both values on the same parsed snapshot avoids a configuration
    rewrite between two reads producing an internally inconsistent runtime
    attestation.
    """

    document = _agents_doc_for(agents_config)
    return (
        dict(sandbox_pin(agents_config=document)),
        sha256_hex(canonical_json(document)),
    )

HEADLESS_PROVIDER_KINDS: tuple[str, ...] = ("claude_headless", "codex_headless")


@dataclass(frozen=True)
class RoleRoute:
    role: str
    provider: str          # key into PROVIDER_BACKENDS
    model: str
    max_tokens: int
    effort: str | None = None
    #: `session.max_usd` caps enabled trajectories only and is not route binding;
    #: disabled one-shot exchanges may exceed it across schema retries.
    max_usd: float | None = None
    #: `session.enabled` of the role's block (False = one-shot today).
    session_enabled: bool = False
    #: True when `max_usd` was declared at the role's top level (outside any
    #: `session:` block): an explicit one-shot cap, enforced as declared.
    max_usd_top_level: bool = False
    #: Per-role prompt-caching flag (`session.prompt_cache`): OFF for every
    #: role today, so every one-shot payload stays byte-identical to the
    #: recorded fixtures; session roles turn it on in Phase 1.
    prompt_cache: bool = False

    @property
    def cap_enforced(self) -> bool:
        """True iff the declared cap TRIPS: an enabled session's
        `session.max_usd`, or a top-level `max_usd` (an explicit one-shot cap)."""
        return self.max_usd is not None and (self.session_enabled or self.max_usd_top_level)

    @property
    def cap_declared_but_disabled(self) -> bool:
        """True iff a `session.max_usd` is declared (hashed into the manifest)
        under `session.enabled: false`: the seat is one-shot and the cap must
        never trip its exchange, whatever map the meter was built from."""
        return self.max_usd is not None and not self.cap_enforced


@dataclass(frozen=True)
class RoleRouting:
    roles: Mapping[str, RoleRoute]
    provider_config: Mapping[str, Mapping[str, Any]]
    source: str = "(embedded defaults)"
    #: The uninterpolated document parsed from the SAME bytes as `roles` and
    #: `provider_config`. `load_role_routing` always sets it; None preserves
    #: compatibility for routings assembled directly in tests/integration
    #: code, whose provider captures one document when it is constructed.
    agents_document: Mapping[str, Any] | None = field(
        default=None, repr=False, compare=False
    )

    def for_role(self, role_name: str) -> RoleRoute:
        route = self.roles.get(role_name)
        if route is None:
            raise RuntimeError(
                f"no routing entry for role {role_name!r} in {self.source} "
                "(fail closed — every live role must be routed explicitly)"
            )
        return route

    def usd_caps(self) -> dict[str, float]:
        """The ENFORCED per-role caps (role -> max_usd), for CostMeter.

        A `session.max_usd` is enforced only when `session.enabled` is true;
        while a seat is one-shot its declared cap is part of the behaviour
        manifest but never trips (`--budget-per-task` still bounds it). A
        top-level `max_usd` outside any session block is an explicit one-shot
        cap and is enforced as declared."""
        return {
            name: float(route.max_usd)
            for name, route in sorted(self.roles.items())
            if route.cap_enforced
        }

    def declared_usd_caps(self) -> dict[str, float]:
        """Every declared cap, enforced or not (diagnostics)."""
        return {
            name: float(route.max_usd)
            for name, route in sorted(self.roles.items())
            if route.max_usd is not None
        }

MODEL_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("anthropic/", "anthropic"),
    ("anthropic.", "anthropic"),      # Bedrock-style 'anthropic.claude-…'
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("openai/", "openai"),
    ("gemini", "google"),
    ("google/", "google"),
    ("moonshotai/", "moonshot"),
    ("kimi", "moonshot"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("meta-llama/", "meta"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("x-ai/", "xai"),
    ("grok", "xai"),
)

_MODEL_TAG_RE = re.compile(r":[a-z0-9_-]+$")


def endpoint_host(provider_config: Mapping[str, Any] | None) -> str:
    """Hostname of a provider config's base_url ('' when unset/unparseable).
    Evidence for provenance records and the cross-family check."""
    if not provider_config:
        return ""
    base_url = str(provider_config.get("base_url") or "").strip()
    if not base_url:
        return ""
    from urllib.parse import urlparse

    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return ""
    return (host or "").lower()


def _is_anthropic_host(host: str) -> bool:
    return bool(host) and (host == "anthropic.com" or host.endswith(".anthropic.com"))


def model_family(
    provider: str, model: str, provider_config: Mapping[str, Any] | None = None
) -> str:
    """Resolve a route's model family for cross-family checks.

    Use known model prefixes plus endpoint-host evidence. Return a stable family label,
    leaving exotic aliases explicit rather than assuming equivalence.
    """
    if provider in ("anthropic", "claude_headless"):
        return "anthropic"
    if _is_anthropic_host(endpoint_host(provider_config)):
        return "anthropic"
    m = (model or "").lower().strip()
    if m.startswith("openrouter/"):
        m = m[len("openrouter/"):]
    m = _MODEL_TAG_RE.sub("", m)
    # Candidates: the whole id, the part after a gateway 'vendor/' prefix, and
    # the part after a Bedrock-style region prefix ('us.anthropic.claude-…').
    candidates = [m]
    if "/" in m:
        candidates.append(m.split("/", 1)[1])
    if "." in m:
        candidates.append(m.split(".", 1)[1])
    for prefix, family in MODEL_FAMILY_PREFIXES:
        if any(c.startswith(prefix) for c in candidates if c):
            return family
    return "unknown:" + (m.split("/", 1)[0] if "/" in m else m)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
        )
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value

DEFAULT_ROUTING_DOC: dict = _agents_doc()


def load_role_routing(path: Path | None = None) -> RoleRouting:
    """Load and validate role routing with environment interpolation.

    Resolve provider blocks, credentials, endpoints, models, effort, tokens, and session
    declarations. Preserve the parsed agents document so behavior and admission digests
    use the same configuration.
    """
    from elt_taskgen.review import provider_backends as _provider_backends
    source_path = Path(path) if path is not None else default_agents_config_path()
    if path is not None:
        if not source_path.is_file():
            raise FileNotFoundError(f"agents config not found: {path} (fail closed)")
    source = str(source_path)
    # ONE read/parse. `_interpolate_env` builds a new tree, leaving the exact
    # declaration as written for this routing's behavior/fingerprint hashing.
    agents_document = _read_agents_document(source_path)
    doc = _interpolate_env(agents_document)
    providers = doc.get("providers") or {}
    roles_raw = doc.get("roles") or {}
    roles: dict[str, RoleRoute] = {}
    for role_name, spec in roles_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"role {role_name!r} in {source} is not a mapping")
        provider = str(spec.get("provider") or "")
        if provider not in _provider_backends.PROVIDER_BACKENDS:
            raise ValueError(
                f"role {role_name!r} in {source} names unknown provider "
                f"{provider!r} (known: {sorted(_provider_backends.PROVIDER_BACKENDS)})"
            )
        # The optional `session:` block (SoT T1) carries the role's declared
        # cap and the prompt-cache flag; a top-level key of the same name is
        # accepted too. Every other session key is the runner's business.
        session = spec.get("session")
        session = session if isinstance(session, dict) else {}
        max_usd_raw = session.get("max_usd", spec.get("max_usd"))
        max_usd_top_level = "max_usd" not in session and spec.get("max_usd") is not None
        session_enabled = bool(session.get("enabled", False))
        max_usd = float(max_usd_raw) if max_usd_raw is not None else None
        if max_usd is not None and (not math.isfinite(max_usd) or max_usd < 0):
            raise ValueError(
                f"role {role_name!r} in {source} declares max_usd "
                f"{max_usd_raw!r}; a cap is a finite, non-negative USD amount"
            )
        prompt_cache = bool(session.get("prompt_cache", spec.get("prompt_cache", False)))
        # A role that leaves `model` empty takes the provider block's `model`
        # (the openai_compat and codex_headless blocks declare one), so one
        # role can be switched between providers by its `provider` alone.
        model = str(spec.get("model") or "")
        if not model:
            block = providers.get(provider) if isinstance(providers, dict) else None
            model = str((block or {}).get("model") or "") if isinstance(block, dict) else ""
        # A provider block may declare `max_tokens`, the per-step output
        # ceiling that transport actually observes (a headless harness takes
        # no per-call output cap; the reserve estimate reads this value): a
        # role's larger declaration is clamped to it for that provider.
        max_tokens = int(spec.get("max_tokens") or 4096)
        block_cap = None
        if isinstance(providers, dict) and isinstance(providers.get(provider), dict):
            block_cap = providers[provider].get("max_tokens")
        if block_cap is not None:
            max_tokens = min(max_tokens, int(block_cap))
        roles[role_name] = RoleRoute(
            role=role_name,
            provider=provider,
            model=model,
            max_tokens=max_tokens,
            effort=(str(spec["effort"]) if spec.get("effort") else None),
            max_usd=max_usd,
            prompt_cache=prompt_cache,
            session_enabled=session_enabled,
            max_usd_top_level=max_usd_top_level,
        )
    return RoleRouting(
        roles=roles,
        provider_config=providers,
        source=source,
        agents_document=agents_document,
    )


def credential_problems(routing: RoleRouting, role_names: list[str]) -> list[str]:
    """Human-readable reasons the named roles cannot run live (empty = ok)."""
    problems: list[str] = []
    needed_providers = set()
    for role_name in role_names:
        try:
            needed_providers.add(routing.for_role(role_name).provider)
        except RuntimeError as exc:
            problems.append(str(exc))
    if "anthropic" in needed_providers:
        cfg = routing.provider_config.get("anthropic") or {}
        if not cfg.get("api_key"):
            problems.append("ANTHROPIC_API_KEY is not set (anthropic provider)")
    if "openai_compat" in needed_providers:
        # ALL THREE are required, not just the URL: checking only base_url made
        # this guard fail OPEN, clearing the check and then dying on a 401 after
        # paying for every anthropic role. A server wanting no auth still needs
        # an explicit placeholder — an empty string is an oversight.
        cfg = routing.provider_config.get("openai_compat") or {}
        for key, env_var in (
            ("base_url", "ELT_TASKGEN_OSS_BASE_URL"),
            ("api_key", "ELT_TASKGEN_OSS_API_KEY"),
            ("model", "ELT_TASKGEN_OSS_MODEL"),
        ):
            if not str(cfg.get(key) or "").strip():
                problems.append(f"{env_var} is not set (openai_compat provider)")
    for kind in HEADLESS_PROVIDER_KINDS:
        if kind in needed_providers:
            from elt_taskgen.review import headless as headless_mod

            problems.extend(
                headless_mod.credential_problems_for(
                    kind, routing.provider_config.get(kind) or {}
                )
            )
    return problems
