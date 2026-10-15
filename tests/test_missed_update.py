"""Catching up a missed automated forecast update at start.

Solcast counts API calls per UTC day. These tests start the integration at 03:00 local time (01:00 UTC,
Europe/Berlin in summer time), one hour after the count started again.
"""

from datetime import UTC, datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components import solcast_solar
from homeassistant.components.solcast_solar.const import AUTO_UPDATED, LAST_ATTEMPT
from homeassistant.components.solcast_solar.enums import AutoUpdate

NOW = dt(2026, 10, 15, 1, 0, tzinfo=UTC)
# 15:48 local, the last update of yesterday
YESTERDAY_EVENING = dt(2026, 10, 14, 13, 48, tzinfo=UTC)
# The last update that was fetched
YESTERDAY_NOON = dt(2026, 10, 14, 11, 7, tzinfo=UTC)
# A forecast fetched two days before
TWO_DAYS_AGO = dt(2026, 10, 13, 11, 7, tzinfo=UTC)
# An update due after the count started again (all-day updates)
TODAY_EARLY = dt(2026, 10, 15, 0, 30, tzinfo=UTC)

check_missed = getattr(solcast_solar, "__check_auto_update_missed")


class FakeActions:
    """Record forecast updates instead of fetching."""

    def __init__(self) -> None:
        """Start with no updates."""
        self.updates = 0

    async def async_update_forecast(self, **kwargs: Any) -> None:
        """Count an update."""
        self.updates += 1


def fake_coordinator(interval_just_passed: dt, update_fits: bool = True, last_attempt: dt = YESTERDAY_NOON) -> Any:
    """Return a coordinator whose last update was fetched yesterday at noon, unless told otherwise."""
    solcast = SimpleNamespace(
        options=SimpleNamespace(auto_update=AutoUpdate.DAYLIGHT, tz=UTC),
        data={AUTO_UPDATED: 4, LAST_ATTEMPT: last_attempt},
        forecast_update_fits=update_fits,
        latest_period=None,
    )
    return SimpleNamespace(solcast=solcast, divisions=4, interval_just_passed=interval_just_passed)


@pytest.fixture(autouse=True)
def frozen_now(freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture) -> None:
    """Freeze the time at 03:00 local and record debug logs."""
    freezer.move_to(NOW)
    caplog.set_level(logging.DEBUG)


async def test_update_due_yesterday_is_not_caught_up(caplog: pytest.LogCaptureFixture) -> None:
    """Yesterday's evening update is not fetched at night: it would take a call from today's updates."""
    actions = FakeActions()
    assert await check_missed(fake_coordinator(YESTERDAY_EVENING), actions) is False
    assert actions.updates == 0
    assert "due before the API day started" in caplog.text


async def test_forecast_older_than_yesterday_is_caught_up() -> None:
    """A forecast from before yesterday is fetched, even though the missed update was due yesterday."""
    actions = FakeActions()
    assert await check_missed(fake_coordinator(YESTERDAY_EVENING, last_attempt=TWO_DAYS_AGO), actions) is True
    assert actions.updates == 1


async def test_update_due_today_is_caught_up() -> None:
    """An update due after the count started again is still fetched."""
    actions = FakeActions()
    assert await check_missed(fake_coordinator(TODAY_EARLY), actions) is True
    assert actions.updates == 1


async def test_update_without_a_call_left_is_not_caught_up(caplog: pytest.LogCaptureFixture) -> None:
    """With no call left for the update, nothing is tried and no warning is logged."""
    actions = FakeActions()
    assert await check_missed(fake_coordinator(TODAY_EARLY, update_fits=False), actions) is False
    assert actions.updates == 0
    assert "no API call is left" in caplog.text
    assert "WARNING" not in caplog.text
