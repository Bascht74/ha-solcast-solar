"""Tests for generation reading, export detection, granular factor indexing and the generation entities key."""

import copy
import datetime
from datetime import datetime as dt, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from custom_components.solcast_solar.const import (
    AUTO_DAMPEN,
    AUTO_UPDATE,
    EXPORT_LIMITING,
    GENERATION,
    GENERATION_ENTITIES,
    GENERATION_VERSION,
    GET_ACTUALS,
    LAST_UPDATED,
    PERIOD_START,
    USE_ACTUALS,
    VERSION,
)
from custom_components.solcast_solar.dampen import Dampening, _is_number
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant, State

from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    reload_integration,
)

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


async def test_cached_gap_days_rechecked_once(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation cache from before gaps were checked loses its days with a reading gap once; a purged day stays."""

    entities = ["sensor.solar_export_sensor_1111_1111_1111_1111", "sensor.solar_export_sensor_2222_2222_2222_2222"]
    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[AUTO_UPDATE] = 0
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 1
        options[AUTO_DAMPEN] = True
        options[GENERATION_ENTITIES] = entities
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast
        today = solcast.dt_helper.day_start_utc()
        generation_file = Path(solcast.filename_generation)

        # Days ago: 1 complete, 2 read by one entity only, 3 unknown from 09:00 to 21:00, 9 no longer in the recorder.
        def day_start(days_ago: int) -> dt:
            return solcast.dt_helper.day_start_utc(future=-days_ago)

        cached = {
            LAST_UPDATED: today,
            GENERATION_ENTITIES: entities,
            GENERATION: [
                {PERIOD_START: day_start(days_ago) + timedelta(minutes=30 * i), GENERATION: 0.3, EXPORT_LIMITING: False}
                for days_ago in (9, 3, 2, 1)
                for i in range(48)
            ],
        }
        assert await solcast.sites_cache.serialise_data(cached, str(generation_file))
        calls: list[int] = []

        async def history(_self: Any, _recorder: Any, start: dt, _end: dt, entity: str, *_args: Any) -> dict[str, list[State]]:
            days_ago = (today - start).days
            calls.append(days_ago)
            if days_ago == 9 or (days_ago == 2 and entity == entities[1]):
                return {}
            value = 0.0
            readings: list[State] = []
            for minute in range(0, 1440, 10):
                value += 0.1
                unknown = days_ago == 3 and entity == entities[0] and 540 <= minute < 1260
                readings.append(
                    State(
                        entity,
                        "unknown" if unknown else f"{value:.1f}",
                        {"unit_of_measurement": "kWh"},
                        last_updated=start + timedelta(minutes=minute),
                    )
                )
            return {entity: readings}

        monkeypatch.setattr(Dampening, "_get_entity_history", history)
        caplog.clear()
        await reload_integration(hass, entry)
        solcast = entry.runtime_data.coordinator.solcast

        kept = {gen[PERIOD_START] for gen in solcast.dampening.data_generation[GENERATION]}
        for days_ago, stays in ((1, True), (2, False), (3, False), (9, True)):
            assert (day_start(days_ago) in kept) is stays, days_ago
        assert len(kept) == 96
        stored = json.loads(generation_file.read_text(encoding="utf-8"))
        assert stored[VERSION] == GENERATION_VERSION
        assert len(stored[GENERATION]) == 96
        assert "Generation days left out, so building the dampening history again" in caplog.text
        assert sorted(set(calls)) == [1, 2, 3, 9]

        # Checked once: the next start reads no history for the cached days.
        calls.clear()
        await reload_integration(hass, entry)
        assert calls == []
        assert len(entry.runtime_data.coordinator.solcast.dampening.data_generation[GENERATION]) == 96

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
