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

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT,
    ALL,
    AUTO_DAMPEN,
    AUTO_UPDATE,
    EXPORT_LIMITING,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    LAST_UPDATED,
    PERIOD_START,
    USE_ACTUALS,
)
import homeassistant.components.solcast_solar.dampen as dampen_module
from homeassistant.components.solcast_solar.dampen import Dampening, _is_number
from homeassistant.components.solcast_solar.dates import DateTimeHelper
from homeassistant.core import HomeAssistant, State

from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    get_config_dir,
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


def _granular_dampening(auto_dampen: bool, factors: list[float]) -> Dampening:
    dampening = Dampening.__new__(Dampening)
    dampening.api = SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        tz=BERLIN,
        dt_helper=DateTimeHelper(BERLIN),
        options=SimpleNamespace(auto_dampen=auto_dampen),
        advanced_options={ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT: True, ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT: True},
    )
    dampening.factors = {ALL: factors}
    return dampening


@pytest.mark.parametrize(("auto_dampen", "expected"), [(False, 0.24), (True, 0.22)])
def test_granular_all_factor_index(auto_dampen: bool, expected: float) -> None:
    """Manual ALL factors stay on local time with delta adjustment; automated factors are in standard time."""
    period_start = dt(2025, 7, 1, 12, 0, tzinfo=BERLIN)  # Daylight saving time
    dampening = _granular_dampening(auto_dampen, [round(i / 100, 2) for i in range(48)])
    assert dampening._get_granular_factor(ALL, period_start, 1.0) == expected  # pyright: ignore[reportPrivateUsage]
    hourly = _granular_dampening(False, [round(i / 100, 2) for i in range(24)])
    assert hourly._get_granular_factor(ALL, period_start, 1.0) == 0.12  # pyright: ignore[reportPrivateUsage]


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


@pytest.mark.parametrize(("load_days", "expected"), [(7, 3), (2, 2)])
async def test_generation_loads_days_missing_since_last(
    frozen_time: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch, load_days: int, expected: int
) -> None:
    """After an outage every day since the last stored interval is read, up to the load days, each local day 23-25 hours."""
    frozen_time.move_to(dt(2025, 10, 28, 10, 0, tzinfo=BERLIN))  # Daylight saving time ended on 26 October
    helper = DateTimeHelper(BERLIN)
    last = helper.day_start_utc(future=-3) - timedelta(minutes=30)  # Last interval of 24 October, read before the outage
    dampening = Dampening.__new__(Dampening)
    dampening.data_generation = {LAST_UPDATED: last, GENERATION_ENTITIES: [], GENERATION: [{PERIOD_START: last, GENERATION: 1.0}]}
    dampening.filename_generation = "unused"
    dampening.api = SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        tz=BERLIN,
        dt_helper=helper,
        hass=None,
        options=SimpleNamespace(generation_entities=[]),
        advanced_options={ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS: load_days},
        sites_cache=SimpleNamespace(serialise_data=AsyncMock()),
    )
    windows: list[tuple[dt, dt]] = []

    async def collect(prev_start: dt, day_start: dt, *_: Any) -> tuple[dict[dt, float], bool]:
        windows.append((prev_start, day_start))
        return dampening._build_float_intervals(prev_start, 30, end=day_start), False  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(dampen_module.er, "async_get", lambda hass: None)
    monkeypatch.setattr(dampen_module, "get_instance", lambda hass: None)
    dampening.prepare_data = AsyncMock()  # type: ignore[method-assign]
    dampening._collect_generation_intervals_for_day = collect  # type: ignore[method-assign,assignment]
    dampening._apply_suppression_entity_limits = AsyncMock()  # type: ignore[method-assign]
    dampening._apply_site_export_limits = AsyncMock()  # type: ignore[method-assign]

    await dampening.get_pv_generation()

    assert windows == [(helper.day_start_utc(future=-1 - day), helper.day_start_utc(future=-day)) for day in range(expected)]
    hours = [(end - start) / timedelta(hours=1) for start, end in windows]
    assert hours == [24, 25, 24][:expected]
    stored = [gen[PERIOD_START] for gen in dampening.data_generation[GENERATION]]
    assert len(stored) == len(set(stored)) == 1 + sum(int(hour * 2) for hour in hours)
    assert all(not gen[EXPORT_LIMITING] for gen in dampening.data_generation[GENERATION][1:])


async def test_generation_entities_key_written_at_load(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generation cache without the entities key gets it at load, so a later change of the entities is detected."""

    try:
        config_dir = get_config_dir(hass.config.config_dir, create=True)
        generation_file = Path(f"{config_dir}/solcast-generation.json")
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[AUTO_UPDATE] = 0
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 1
        options[AUTO_DAMPEN] = True
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)  # Loads the cache, reads generation and writes the generation file

        data = json.loads(generation_file.read_text(encoding="utf-8"))
        data.pop(GENERATION_ENTITIES)
        generation_file.write_text(json.dumps(data), encoding="utf-8")

        await reload_integration(hass, entry)
        assert json.loads(generation_file.read_text(encoding="utf-8"))[GENERATION_ENTITIES] == options[GENERATION_ENTITIES]

        caplog.clear()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, GENERATION_ENTITIES: ["sensor.solar_export_sensor_2222_2222_2222_2222"]}
        )
        await hass.async_block_till_done()
        assert "Generation entities changed" in caplog.text

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
