"""The schema library of the integration: probatio where Home Assistant has it, else voluptuous."""

import builtins
import importlib
import sys
from typing import Any

import pytest

from homeassistant.components.solcast_solar import validators


def test_probatio_already_loaded() -> None:
    """Home Assistant has loaded probatio, so the schemas are built with it."""

    module = importlib.reload(validators)
    assert module.validator is sys.modules["probatio"]
    assert module.Schema is sys.modules["probatio"].Schema


def test_probatio_imported_when_not_loaded_yet() -> None:
    """Probatio is installed but not loaded yet, so it is imported."""

    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(sys.modules, "probatio")
        module = importlib.reload(validators)
        assert module.validator.__name__ == "probatio"
        assert module.validator is sys.modules["probatio"]
    assert importlib.reload(validators).validator is sys.modules["probatio"]


def test_voluptuous_without_probatio() -> None:
    """A Home Assistant without probatio builds the schemas with voluptuous."""

    real_import = builtins.__import__

    def without_probatio(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "probatio":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.delitem(sys.modules, "probatio")
        patch.setattr(builtins, "__import__", without_probatio)
        module = importlib.reload(validators)
        assert module.validator is sys.modules["voluptuous"]
        assert module.Required is sys.modules["voluptuous"].Required
    assert importlib.reload(validators).validator is sys.modules["probatio"]
