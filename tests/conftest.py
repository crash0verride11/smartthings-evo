"""Shared helpers for isolated unit tests.

The tests load individual integration modules without importing the integration
package's Home Assistant entry point. This keeps the fast unit suite independent
of a full Home Assistant installation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
COMPONENT_ROOT = PROJECT_ROOT / "custom_components" / "smartthings"
PACKAGE = "_smartthings_test"


def load_component_module(name: str) -> ModuleType:
    """Load one component module in an isolated synthetic package."""
    package = sys.modules.setdefault(PACKAGE, ModuleType(PACKAGE))
    package.__path__ = [str(COMPONENT_ROOT)]  # type: ignore[attr-defined]
    module_name = f"{PACKAGE}.{name}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, COMPONENT_ROOT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def samsung_auth() -> ModuleType:
    """Return a freshly loaded Samsung authentication module."""
    return load_component_module("samsung_auth")
