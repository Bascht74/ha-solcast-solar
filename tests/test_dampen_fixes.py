"""Tests for generation reading, export detection, granular factor indexing and the generation entities key."""

import datetime
from datetime import datetime as dt, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from custom_components.solcast_solar.dampen import Dampening, _is_number
import pytest

from homeassistant.core import State


BERLIN = ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("12.5", True),
        ("-3", True),
        ("1e-05", True),
        ("0", True),
        ("unknown", False),
        ("unavailable", False),
        ("nan", False),
        ("inf", False),
    ],
)
def test_is_number(state: str, expected: bool) -> None:
    """Negative and exponent readings are numbers, non-finite and non-numeric states are not."""
    assert _is_number(state) is expected


def test_target_timestamp_uses_local_date() -> None:
    """In a zone ahead of UTC the target is on the local date of target_day, not one day earlier."""
    dampening = Dampening.__new__(Dampening)
    dampening.api = SimpleNamespace(tz=ZoneInfo("Australia/Sydney"))  # pyright: ignore[reportAttributeAccessIssue]
    past_ts = dt(2025, 1, 11, 2, 0, tzinfo=datetime.UTC)  # 13:00 on 11 January in Sydney
    target_day = dt(2025, 1, 25, tzinfo=ZoneInfo("Australia/Sydney"))
    assert dampening._target_timestamp(past_ts, target_day) == dt(2025, 1, 25, 2, 0, tzinfo=datetime.UTC)  # pyright: ignore[reportPrivateUsage]


async def _export_limiting(readings: list[tuple[dt, float]]) -> dict[dt, bool]:
    """Run export detection for one day of meter readings with a 5 kW limit."""
    prev_start = dt(2025, 6, 1, tzinfo=datetime.UTC)
    day_start = prev_start + timedelta(days=1)
    dampening = Dampening.__new__(Dampening)
    dampening.api = SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        tz=datetime.UTC,
        options=SimpleNamespace(site_export_limit=5.0, site_export_entity="sensor.export"),
    )
    history = [State("sensor.export", str(value), {"unit_of_measurement": "kWh"}, last_updated=when) for when, value in readings]
    dampening._get_entity_history = AsyncMock(return_value={"sensor.export": history})  # type: ignore[method-assign]
    registry = SimpleNamespace(async_get=lambda entity: SimpleNamespace(disabled_by=None))
    limiting = dampening._build_half_hour_bool_intervals(prev_start, day_start)  # pyright: ignore[reportPrivateUsage]
    await dampening._apply_site_export_limits(limiting, prev_start, day_start, registry, None)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    return limiting


async def test_export_spread_over_meter_interval() -> None:
    """A 15-minute meter exporting 4 kW is not export limited, a 5-minute burst of 12 kW is."""
    start = dt(2025, 6, 1, 10, 0, tzinfo=datetime.UTC)
    steady = [(start + timedelta(minutes=15 * i), float(i)) for i in range(5)]  # 1 kWh per 15 minutes
    steady.append(steady[-1])  # A repeated timestamp adds nothing
    limiting = await _export_limiting(steady)
    assert not any(limiting.values())

    burst = [(start, 0.0), (start + timedelta(minutes=5), 1.0)]  # 1 kWh in 5 minutes
    limiting = await _export_limiting(burst)
    assert [interval for interval, limited in limiting.items() if limited] == [start]
