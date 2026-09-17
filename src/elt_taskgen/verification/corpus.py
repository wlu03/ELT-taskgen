"""Strictly parse every committed YAML and JSON corpus artifact.

The gate reports parser failures before release or runtime use and never repairs input.
Tolerant compatibility repair remains in the corpus adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import yaml

#: Artifact types the gate strict-parses; everything else (SQL, CSV, docs) is
#: out of scope for a PARSE gate and deliberately untouched.
DEFAULT_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".json")


def _iter_artifacts(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Regular files under `root` with a matching suffix, in sorted order."""
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def count_artifacts(root: Path, *, suffixes: tuple[str, ...] = DEFAULT_SUFFIXES) -> int:
    """How many files under `root` the gate would strict-parse.

    Callers MUST check this alongside `strict_parse_failures`: zero failures
    over zero artifacts is a sweep that saw nothing, not a passing corpus.
    """
    return sum(1 for _ in _iter_artifacts(root, suffixes))


def strict_parse_failures(
    root: Path, *, suffixes: tuple[str, ...] = DEFAULT_SUFFIXES
) -> list[tuple[Path, str]]:
    """Return every strict YAML or JSON parse failure as `(path, error)`.

    Visit paths deterministically and collapse messages to one line. Unreadable or
    non-UTF-8 files fail rather than skip.
    """
    failures: list[tuple[Path, str]] = []
    for path in _iter_artifacts(root, suffixes):
        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix.lower() == ".json":
                json.loads(text)
            else:
                yaml.safe_load(text)
        except (yaml.YAMLError, json.JSONDecodeError, UnicodeError, OSError) as exc:
            message = f"{type(exc).__name__}: {exc}".replace("\n", " | ")
            failures.append((path, message))
    return failures
