"""Tests for assumed (post-command) attribute values."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from conftest import PACKAGE, load_component_module
from pysmartthings import Status

SENT_AT = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
KEY = ("main", "switch", "switch")


@pytest.fixture
def assumed_state(monkeypatch):
    """Load assumed_state with a const stand-in (the real one imports Home Assistant)."""
    constants = ModuleType(f"{PACKAGE}.const")
    constants.ASSUMED_STATE_VALIDITY = 60
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.const", constants)
    return load_component_module("assumed_state")


def _assumed(module, value="on"):
    return {KEY: module.AssumedValue(value, SENT_AT)}


def test_no_assumed_value_returns_polled(assumed_state):
    status = Status("off", timestamp=SENT_AT)

    assert assumed_state.resolve_attribute_value({}, KEY, status, SENT_AT) == "off"


def test_assumed_beats_report_older_than_command(assumed_state):
    """A poll in flight when the command was sent must not undo it."""
    assumed = _assumed(assumed_state)
    status = Status("off", timestamp=SENT_AT - timedelta(seconds=5))
    now = SENT_AT + timedelta(seconds=2)

    assert assumed_state.resolve_attribute_value(assumed, KEY, status, now) == "on"
    assert KEY in assumed


def test_assumed_held_when_report_has_no_timestamp(assumed_state):
    assumed = _assumed(assumed_state)
    now = SENT_AT + timedelta(seconds=2)

    assert (
        assumed_state.resolve_attribute_value(assumed, KEY, Status("off"), now) == "on"
    )


def test_newer_report_wins_and_clears_assumed(assumed_state):
    """A report after the command is real state, even if it disagrees."""
    assumed = _assumed(assumed_state)
    status = Status("off", timestamp=SENT_AT + timedelta(seconds=1))
    now = SENT_AT + timedelta(seconds=3)

    assert assumed_state.resolve_attribute_value(assumed, KEY, status, now) == "off"
    assert KEY not in assumed


def test_assumed_expires(assumed_state):
    """A command the device ignored falls back to the polled value."""
    assumed = _assumed(assumed_state)
    status = Status("off", timestamp=SENT_AT - timedelta(seconds=5))
    now = SENT_AT + timedelta(seconds=60)

    assert assumed_state.resolve_attribute_value(assumed, KEY, status, now) == "off"
    assert KEY not in assumed
