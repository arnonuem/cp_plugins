"""Fail-soft adapters into code-puppy internals.

Patched against the DEFINITION module in each case, because ``sources``
imports every symbol function-locally on purpose: two of these sources have
side effects at call time and plugins are imported partway through
``cli_runner``'s own import.

Covers AC-25..AC-29.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import patch

import pytest

from wmux import sources


class FakeUsage:
    """Mirrors the shape of ``token_usage.ContextUsage`` we actually read."""

    def __init__(self, total: int, capacity: int, percent: float) -> None:
        self.total_tokens = total
        self.capacity = capacity
        self.percent = percent


class FakeAgent:
    def __init__(self, model: Any = "gpt-5") -> None:
        self._model = model

    def get_model_name(self):
        return self._model


# --- metadata (AC-25..AC-27) -----------------------------------------------


def test_ac25_no_usage_yields_no_metadata():
    with patch("code_puppy.token_usage.get_current_usage", return_value=None):
        assert sources.current_metadata() is None


def test_ac26_metadata_shape_and_types():
    usage = FakeUsage(total=48_200, capacity=200_000, percent=24.1)
    with (
        patch("code_puppy.token_usage.get_current_usage", return_value=usage),
        patch(
            "code_puppy.agents.agent_manager.get_current_agent",
            return_value=FakeAgent("gpt-5"),
        ),
    ):
        payload = sources.current_metadata()
    assert payload == {"tokens": "48k/200k", "context_pct": 24, "model": "gpt-5"}
    assert isinstance(payload["context_pct"], int)


def test_ac26_model_is_omitted_not_nulled_when_unknown():
    usage = FakeUsage(total=1, capacity=2, percent=50.0)
    with (
        patch("code_puppy.token_usage.get_current_usage", return_value=usage),
        patch("code_puppy.agents.agent_manager.get_current_agent", return_value=None),
    ):
        payload = sources.current_metadata()
    assert "model" not in payload


@pytest.mark.parametrize(
    "count, expected",
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1k"),
        (48_200, "48k"),
        (999_999, "999k"),
        (1_000_000, "1M"),
        (2_500_000, "2M"),
    ],
)
def test_ac26_token_compaction_is_floor_division_decimal_k(count, expected):
    assert sources._compact(count) == expected


def test_ac26_all_three_magnitudes_in_one_payload():
    for total, capacity, expected in [
        (999, 200_000, "999/200k"),
        (48_200, 200_000, "48k/200k"),
        (2_500_000, 4_000_000, "2M/4M"),
    ]:
        usage = FakeUsage(total=total, capacity=capacity, percent=1.0)
        with (
            patch("code_puppy.token_usage.get_current_usage", return_value=usage),
            patch(
                "code_puppy.agents.agent_manager.get_current_agent", return_value=None
            ),
        ):
            assert sources.current_metadata()["tokens"] == expected


def test_ac27_a_raising_usage_source_degrades_to_none():
    with patch(
        "code_puppy.token_usage.get_current_usage", side_effect=RuntimeError("boom")
    ):
        assert sources.current_metadata() is None


def test_ac27_a_raising_agent_source_degrades_to_an_omitted_model():
    # get_current_agent is NOT a pure accessor -- on a cold cache it calls
    # load_agent() and can raise.
    usage = FakeUsage(total=10, capacity=100, percent=10.0)
    with (
        patch("code_puppy.token_usage.get_current_usage", return_value=usage),
        patch(
            "code_puppy.agents.agent_manager.get_current_agent",
            side_effect=RuntimeError("cold cache"),
        ),
    ):
        payload = sources.current_metadata()
        assert sources.current_model() is None
    assert payload is not None and "model" not in payload


def test_ac27_a_raising_session_source_degrades_to_none():
    with patch(
        "code_puppy.config.get_current_session_name",
        side_effect=RuntimeError("boom"),
    ):
        assert sources.current_session_id() is None


# --- session (AC-28 source half, AC-29) ------------------------------------


def test_ac28_session_id_is_read_from_config():
    with patch("code_puppy.config.get_current_session_name", return_value="my-session"):
        assert sources.current_session_id() == "my-session"


def test_empty_session_name_is_treated_as_absent():
    with patch("code_puppy.config.get_current_session_name", return_value=""):
        assert sources.current_session_id() is None


def test_ac29_import_never_mints_a_session_name():
    """Importing must not call the minting accessor (``config.py:1931-1946``)."""
    with patch("code_puppy.config.get_current_session_name") as mint:
        importlib.reload(sys.modules["wmux.sources"])
    assert mint.call_count == 0


# --- AC-53: a dead source is VISIBLE once ----------------------------------


@pytest.mark.parametrize(
    "target, call, marker",
    [
        pytest.param(
            "code_puppy.agents.agent_manager.get_current_agent",
            lambda: sources.current_model(),
            "model",
            id="model",
        ),
        pytest.param(
            "code_puppy.token_usage.get_current_usage",
            lambda: sources.current_metadata(),
            "metadata",
            id="metadata",
        ),
        pytest.param(
            "code_puppy.config.get_current_session_name",
            lambda: sources.current_session_id(),
            "session",
            id="session",
        ),
    ],
)
def test_ac53_the_first_source_failure_warns_exactly_once(caplog, target, call, marker):
    """A core API rename must not disable a source with ZERO symptom.

    These handlers logged at ``logger.debug``, and measured, every debug log
    in this plugin is discarded (R-6b): core installs no logging config, so
    ``logging.lastResort`` applies at a fixed WARNING. So a renamed core
    accessor turned metadata off permanently and looked like nothing at all.

    The FIRST failure per function warns; the rest stay at debug, because
    these are called on every turn and a persistent failure would otherwise
    bury the terminal.
    """
    with caplog.at_level("DEBUG", logger="wmux.diagnostics"):
        with patch(target, side_effect=RuntimeError("core renamed it")):
            call()
            call()
            call()
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and marker in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one warning for {marker}, got {len(warnings)}"
    )


def test_ac53_a_healthy_source_never_warns(caplog):
    """The control: the warning must be a FAILURE signal, not chatter."""
    usage = FakeUsage(total=10, capacity=100, percent=10.0)
    with caplog.at_level("WARNING"):
        with (
            patch("code_puppy.token_usage.get_current_usage", return_value=usage),
            patch(
                "code_puppy.agents.agent_manager.get_current_agent",
                return_value=FakeAgent("gpt-5"),
            ),
            patch("code_puppy.config.get_current_session_name", return_value="sess"),
        ):
            assert sources.current_metadata() is not None
            assert sources.current_session_id() == "sess"
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
