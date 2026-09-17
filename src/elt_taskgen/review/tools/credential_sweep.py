"""Find credential-shaped files without exposing secret values.

The sweep matches sensitive filenames and non-placeholder values under secret-shaped
keys. Unparseable name-matched files fail closed. Findings include paths, sizes, rules,
and key names only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CredentialFinding",
    "DEFAULT_MAX_BYTES",
    "NAME_RULES",
    "SECRET_KEY_RE",
    "PUBLIC_SOURCE_FIXTURE_VALUES",
    "format_findings",
    "is_placeholder_value",
    "is_public_fixture_value",
    "is_secret_key",
    "sweep_credential_shaped_files",
]

#: Files larger than this are not parsed: a name-matched one is reported as
#: opaque (cannot be vetted), any other is skipped.
DEFAULT_MAX_BYTES = 1 << 20

#: (rule, basename pattern, opaque). An OPAQUE rule names key material this
#: sweep does not parse: any non-empty match is live by construction.
NAME_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("credential_json", re.compile(r"(?:^|_)credentials?\.json$", re.IGNORECASE), False),
    ("dotenv", re.compile(r"(?:^|\.)env(?:rc|\.[A-Za-z0-9_.-]+)?$", re.IGNORECASE), False),
    ("profiles_yml", re.compile(r"^profiles\.ya?ml$", re.IGNORECASE), False),
    ("tfstate", re.compile(r"\.tfstate(?:\.backup)?$", re.IGNORECASE), False),
    ("tfvars", re.compile(r"\.tfvars(?:\.json)?$", re.IGNORECASE), False),
    ("ini_credentials", re.compile(r"^\.?(?:aws_|azure_)?credentials$", re.IGNORECASE), False),
    (
        "key_material",
        re.compile(
            r"(?:\.(?:pem|key|p12|pfx|jks|ppk)$|^id_(?:rsa|dsa|ecdsa|ed25519)$"
            r"|^\.(?:netrc|pgpass)$)",
            re.IGNORECASE,
        ),
        True,
    ),
)

#: A key is secret-shaped when its LAST underscore-separated word names a
#: secret. Plural counters (`input_tokens`, `max_tokens`), identifiers
#: (`client_id`, `access_key_id`, `workspace_id`) and hashes
#: (`password_hash`) are not.
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|passphrase|pwd|pass|secret|api_?key|access_?key"
    r"|private_?key|signing_?key|token|bearer)$"
)

_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"  # ${VAR} / $VAR
    r"|\$?\{\{.*\}\}"  # Jinja {{ env_var('X') }} / Actions ${{ secrets.X }}
    r"|<[^<>]*>"  # <your-token-here>
    r"|\[[^\[\]]*\]"  # [REDACTED]
    r"|\*{3,}|x{3,}|#{3,}|-{3,}|\.{3,}|_{3,}"
    r"|placeholder|changeme|change[-_ ]me|redacted|todo|tbd|null|none|nil"
    r"|undefined|example|dummy|sample|fake|replace[-_ ]me|fill[-_ ]me[-_ ]in"
    r"|your[-_ ][\w -]*"
    r")$",
    re.IGNORECASE | re.DOTALL,
)

#: Exported fixture credentials are public connector settings, never live
#: secrets. Tests pin this allowlist to the exporter.
PUBLIC_SOURCE_FIXTURE_VALUES: frozenset[str] = frozenset({"testelt", "test"})

#: Suffixes whose content is scanned for secret-shaped values even when the
#: basename matches no name rule (an installed `config.yaml`, a solver's
#: `main.tf`, an operator's `*-attempt.json`).
_VALUE_SCAN_SUFFIXES = frozenset(
    {
        ".json", ".yaml", ".yml", ".env", ".tf", ".hcl", ".tfvars", ".ini",
        ".cfg", ".conf", ".properties", ".toml",
    }
)
_SKIPPED_DIRS = frozenset({".git", "__pycache__"})
_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*[=:]\s*(.*?)\s*,?\s*$"
)


@dataclass(frozen=True)
class CredentialFinding:
    """One credential-shaped file. Never carries a value."""

    #: POSIX path relative to the swept root.
    path: str
    size_bytes: int
    #: The `NAME_RULES` rule that matched the basename, else None.
    name_pattern: str | None
    #: Normalized key names whose value is a live secret-shaped string.
    secret_keys: tuple[str, ...]
    #: A name-matched file that could not be vetted (key material, binary,
    #: oversized): live by construction.
    opaque: bool = False

    @property
    def live(self) -> bool:
        return bool(self.secret_keys) or self.opaque

    def describe(self) -> str:
        parts = [f"{self.path} ({self.size_bytes} bytes)"]
        if self.name_pattern:
            parts.append(f"name={self.name_pattern}")
        if self.secret_keys:
            parts.append("keys=" + ",".join(self.secret_keys))
        if self.opaque:
            parts.append("opaque")
        return "; ".join(parts)


def is_secret_key(key: object) -> bool:
    """Is this mapping key / env-var name / HCL attribute secret-shaped?"""
    if not isinstance(key, str) or not key:
        return False
    return SECRET_KEY_RE.search(_normalize_key(key)) is not None


def is_placeholder_value(value: object) -> bool:
    """Is this string value empty or a placeholder (never a live secret)?
    Non-strings (numbers, booleans, None) are never live."""
    if not isinstance(value, str):
        return True
    text = _strip_quotes(value.strip())
    return not text or _PLACEHOLDER_RE.match(text) is not None


def is_public_fixture_value(value: object) -> bool:
    """Is this one of the exporter's public source-fixture values?"""
    if not isinstance(value, str):
        return False
    return _strip_quotes(value.strip()) in PUBLIC_SOURCE_FIXTURE_VALUES


def _is_live_value(value: object) -> bool:
    return not is_placeholder_value(value) and not is_public_fixture_value(value)


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s.\-]+", "_", key.strip().lower())


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _name_rule(basename: str) -> tuple[str, bool] | None:
    for rule, pattern, opaque in NAME_RULES:
        if pattern.search(basename):
            return rule, opaque
    return None


def _live_keys_in_structure(node: Any, out: set[str]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(value, (Mapping, list, tuple)):
                _live_keys_in_structure(value, out)
            elif is_secret_key(key) and _is_live_value(value):
                out.add(_normalize_key(str(key)))
    elif isinstance(node, (list, tuple)):
        for item in node:
            _live_keys_in_structure(item, out)


def _live_keys_in_lines(text: str, out: set[str]) -> None:
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        if "//" in line:
            line = line.split("//", 1)[0]
        match = _LINE_RE.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if is_secret_key(key) and _is_live_value(value):
            out.add(_normalize_key(key))


def _live_keys(path: Path, text: str) -> tuple[str, ...]:
    """Secret-shaped keys with live values, from the structured parse when the
    file parses and from a line scan otherwise."""
    out: set[str] = set()
    suffix = path.suffix.lower()
    parsed: Any = None
    structured = False
    try:
        if suffix == ".json":
            parsed, structured = json.loads(text), True
        elif suffix in {".yaml", ".yml"}:
            parsed, structured = yaml.safe_load(text), True
    except (ValueError, yaml.YAMLError):
        structured = False
    if structured:
        _live_keys_in_structure(parsed, out)
    else:
        _live_keys_in_lines(text, out)
    return tuple(sorted(out))


def _walk(root: Path, exclude: Callable[[Path], bool] | None) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        keep: list[str] = []
        for name in sorted(dirnames):
            child = current / name
            if name in _SKIPPED_DIRS or child.is_symlink():
                continue
            if exclude is not None and exclude(child.relative_to(root)):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            child = current / name
            if child.is_symlink():
                continue
            if exclude is not None and exclude(child.relative_to(root)):
                continue
            yield child


def sweep_credential_shaped_files(
    root: Path,
    *,
    live_only: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    exclude: Callable[[Path], bool] | None = None,
) -> list[CredentialFinding]:
    """Walk `root` (symlinks never followed) and report credential-shaped
    files, sorted by path.

    `live_only=True` (the default, the assertion form) returns only findings
    that carry a live secret-shaped value or are opaque; `live_only=False`
    also returns name-matched files whose values are all placeholders, for a
    listing. `exclude(relative_path) -> bool` prunes directories and files.
    Values are read to be judged and are never retained."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    findings: list[CredentialFinding] = []
    for path in _walk(root, exclude):
        rule = _name_rule(path.name)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        name_pattern = rule[0] if rule else None
        opaque_rule = bool(rule and rule[1])
        scan_values = rule is not None and not opaque_rule
        scan_values = scan_values or path.suffix.lower() in _VALUE_SCAN_SUFFIXES
        if rule is None and not scan_values:
            continue
        if size == 0:
            if rule is not None and not live_only:
                findings.append(CredentialFinding(rel, 0, name_pattern, ()))
            continue
        if opaque_rule or size > max_bytes:
            if rule is not None:
                findings.append(CredentialFinding(rel, size, name_pattern, (), opaque=True))
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            if rule is not None:
                findings.append(CredentialFinding(rel, size, name_pattern, (), opaque=True))
            continue
        keys = _live_keys(path, text)
        del text
        finding = CredentialFinding(rel, size, name_pattern, keys)
        if finding.live or (rule is not None and not live_only):
            findings.append(finding)
    findings.sort(key=lambda finding: finding.path)
    return findings


def format_findings(findings: list[CredentialFinding]) -> str:
    """One line per finding: path, size, name rule, key names. Never values."""
    if not findings:
        return "no credential-shaped files"
    return "\n".join(finding.describe() for finding in findings)
