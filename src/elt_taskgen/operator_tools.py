"""Expose allowlisted operator tools from a checkout or installed resources.

Each entry point delegates to the reviewed tool's ``main(argv)`` function.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from elt_taskgen.package_resources import resource_path


_SUPPORTED_TOOLS = frozenset({"parity_sample", "schemapile_index"})


def _load_operator_tool(name: str) -> ModuleType:
    """Load one allowlisted tool from the checkout or installed resources."""

    if name not in _SUPPORTED_TOOLS:
        raise RuntimeError(f"unsupported packaged operator tool: {name!r}")
    module_name = f"elt_taskgen._operator_tool_{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    path = resource_path(Path("tools") / f"{name}.py")
    if not path.is_file():
        raise RuntimeError(f"packaged operator tool not found at {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import packaged operator tool at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    if not callable(getattr(module, "main", None)):
        sys.modules.pop(module_name, None)
        raise RuntimeError(f"packaged operator tool has no main(argv): {path}")
    return module


def schemapile_index_main(argv: list[str] | None = None) -> int:
    """Run the supported SchemaPile index builder."""

    return int(_load_operator_tool("schemapile_index").main(argv))


def parity_sample_main(argv: list[str] | None = None) -> int:
    """Run the supported offline parity sampling/report driver."""

    return int(_load_operator_tool("parity_sample").main(argv))


__all__ = ["parity_sample_main", "schemapile_index_main"]
