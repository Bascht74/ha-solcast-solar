"""Generation days with missing readings: weighed by the energy they miss, rechecked without loss, and no stale factors."""

from collections import defaultdict
import copy
from datetime import datetime as dt, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    ALL,
    AUTO_DAMPEN,
    ESTIMATE,
    EXPORT_LIMITING,
    FORECASTS,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    LAST_UPDATED,
    PERIOD_START,
    RESOURCE_ID,
    SITE_INFO,
    USE_ACTUALS,
)
from homeassistant.components.solcast_solar.dampen import Dampening
from homeassistant.components.solcast_solar.solcastapi import SolcastApi
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from . import (
    DEFAULT_INPUT1,
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    reload_integration,
)

POWER_ENTITY = "sensor.solar_export_sensor_2222_2222_2222_2222"
ENERGY_ENTITY = "sensor.solar_export_sensor_1111_1111_1111_1111"
HALF_HOUR = timedelta(minutes=30)


def _day_actuals(solcast: SolcastApi, start: dt, end: dt) -> dict[dt, float]:
    """Estimated actuals of all sites per half hour, as kW."""

    actuals: dict[dt, float] = defaultdict(float)
    for site in solcast.sites:
        for actual in solcast.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS]:
            if start <= actual[PERIOD_START] < end:
                actuals[actual[PERIOD_START]] += actual[ESTIMATE]
    return actuals


@pytest.mark.parametrize(
    ("asleep_minutes", "outage", "kept"),
    [
        pytest.param(0, False, True, id="unavailable_at_night"),
        pytest.param(40, False, True, id="unavailable_at_night_and_dusk"),
        pytest.param(0, True, False, id="an_hour_without_readings_at_noon"),
    ],
)
async def test_gap_weighed_by_expected_energy(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    asleep_minutes: int,
    outage: bool,
    kept: bool,
) -> None:
    """A power sensor that is unavailable while the inverter sleeps keeps its day; an outage around noon does not.

    The inverter sleeps from the last to the first daylight half hour, and asleep_minutes longer at dusk and dawn.
    """

    options = copy.deepcopy(DEFAULT_INPUT2) | {GET_ACTUALS: True, USE_ACTUALS: 1, AUTO_DAMPEN: True, GENERATION_ENTITIES: [POWER_ENTITY]}
    try:
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES_POWER)
        solcast: SolcastApi = entry.runtime_data.coordinator.solcast
        dampening = solcast.dampening
        await dampening.prepare_data(only_peaks=True)
        yesterday = solcast.dt_helper.day_start_utc(future=-1)
        today = solcast.dt_helper.day_start_utc()
        actuals = _day_actuals(solcast, yesterday, today)
        daylight = [
            interval
            for interval in dampening._build_half_hour_bool_intervals(yesterday, today)  # pyright: ignore[reportPrivateUsage]
            if solcast.peak_intervals.get(dampening.adjusted_interval_dt(interval), 0) > 0
        ]
        awake_from = min(daylight) + timedelta(minutes=asleep_minutes)
        awake_to = max(daylight) + HALF_HOUR - timedelta(minutes=asleep_minutes)
        noon = max(actuals, key=lambda interval: actuals[interval])
        missing = (noon - HALF_HOUR, noon + HALF_HOUR) if outage else (today, today)

        async def history(_recorder: Any, start: dt, end: dt, entity: str, *_args: Any) -> dict[str, list[State]]:
            if start != yesterday:
                return {}
            states: list[State] = []
            moment = start
            while moment < end:
                reading = awake_from <= moment < awake_to and not missing[0] <= moment < missing[1]
                state = f"{actuals.get(Dampening._bucket_interval_start(moment), 0.0):.3f}" if reading else "unavailable"  # pyright: ignore[reportPrivateUsage]
                states.append(State(entity, state, {"unit_of_measurement": "kW"}, last_updated=moment))
                moment += timedelta(minutes=5)
            return {entity: states}

        monkeypatch.setattr(dampening, "_get_entity_history", history)
        dampening.data_generation[GENERATION] = []
        await dampening.get_pv_generation()
        assert (yesterday in {generated[PERIOD_START] for generated in dampening.data_generation[GENERATION]}) is kept
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_recheck_keeps_days_the_recorder_no_longer_holds(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-time check of an old generation cache reads only the days the recorder still holds, and logs few dates."""

    options = copy.deepcopy(DEFAULT_INPUT1) | {GET_ACTUALS: True, USE_ACTUALS: 1, AUTO_DAMPEN: True, GENERATION_ENTITIES: [ENERGY_ENTITY]}
    try:
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast: SolcastApi = entry.runtime_data.coordinator.solcast
        today = solcast.dt_helper.day_start_utc()

        def day_start(days_ago: int) -> dt:
            return solcast.dt_helper.day_start_utc(future=-days_ago)

        cached_days = (1, 2, 3, 4, 5, 6, 7, 12, 15)
        cached = {
            LAST_UPDATED: today,
            GENERATION_ENTITIES: [ENERGY_ENTITY],
            GENERATION: [
                {PERIOD_START: day_start(days_ago) + HALF_HOUR * i, GENERATION: 0.3, EXPORT_LIMITING: False}
                for days_ago in sorted(cached_days, reverse=True)
                for i in range(48)
            ],
        }
        assert await solcast.sites_cache.serialise_data(cached, solcast.filename_generation)
        read: list[int] = []

        async def history(_self: Any, _recorder: Any, start: dt, _end: dt, entity: str, *_args: Any) -> dict[str, list[State]]:
            read.append((today - start).days)
            return {  # Readings all day, but unknown from 09:00 to 15:00 local time
                entity: [
                    State(
                        entity,
                        "unknown" if 540 <= minute < 900 else f"{minute / 100:.1f}",
                        {"unit_of_measurement": "kWh"},
                        last_updated=start + timedelta(minutes=minute),
                    )
                    for minute in range(0, 1440, 10)
                ]
            }

        monkeypatch.setattr(Dampening, "_get_entity_history", history)
        caplog.clear()
        await reload_integration(hass, entry)
        generation = entry.runtime_data.coordinator.solcast.dampening.data_generation[GENERATION]

        # The recorder keeps ten days: older days are neither read nor dropped.
        assert sorted(set(read)) == [1, 2, 3, 4, 5, 6, 7]
        assert {generated[PERIOD_START] for generated in generation} == {day_start(12) + HALF_HOUR * i for i in range(48)} | {
            day_start(15) + HALF_HOUR * i for i in range(48)
        }
        dates = sorted(day_start(days_ago).astimezone(solcast.tz).strftime("%Y-%m-%d") for days_ago in range(1, 8))
        assert (
            f"Cached PV generation of 7 day(s) has a gap in daylight readings, leaving it out: {', '.join(dates[:5])} and 2 more"
            in caplog.text
        )
        assert stored_version(solcast) == 2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


def stored_version(solcast: SolcastApi) -> int:
    """The version of the generation cache file."""

    return json.loads(Path(solcast.filename_generation).read_text(encoding="utf-8"))["version"]


async def test_factors_reset_when_generation_is_gone(
    recorder_mock: Recorder, hass: HomeAssistant, entity_registry: er.EntityRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Without generation to model from, automated factors go back to 1.0, warned about once, not kept from old generation."""

    options = copy.deepcopy(DEFAULT_INPUT1) | {GET_ACTUALS: True, USE_ACTUALS: 1, AUTO_DAMPEN: True, GENERATION_ENTITIES: [ENERGY_ENTITY]}
    warning = "Auto-dampening has no PV generation to model from, so its factors are reset to 1.0 until there is"
    try:
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)
        assert any(factor != 1.0 for factor in entry.runtime_data.coordinator.solcast.dampening.factors[ALL])

        # A new inverter sensor without history: the generation of the old one is dropped, and none can be read yet.
        new_entity = entity_registry.async_get_or_create(
            "sensor", "inverter", "new_power", suggested_object_id="new_inverter_energy"
        ).entity_id
        caplog.clear()
        hass.config_entries.async_update_entry(entry, options={**entry.options, GENERATION_ENTITIES: [new_entity]})
        await hass.async_block_till_done()
        solcast: SolcastApi = entry.runtime_data.coordinator.solcast
        assert "Generation entities changed" in caplog.text
        assert solcast.dampening.data_generation[GENERATION] == []
        assert caplog.text.count(warning) == 1
        assert solcast.dampening.factors[ALL] == [1.0] * 48
        assert json.loads(Path(solcast.dampening.get_filename()).read_text(encoding="utf-8"))[ALL] == [1.0] * 48

        # Once reset, the next start and the next model run only say why auto-dampening waits.
        caplog.clear()
        await reload_integration(hass, entry)
        await entry.runtime_data.coordinator.solcast.dampening.model_automated()
        assert warning not in caplog.text
        assert "Auto-dampening suppressed: No generation yet" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
