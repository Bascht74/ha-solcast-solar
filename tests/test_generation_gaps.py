"""Generation days with missing readings: weighed by the energy they miss, rechecked without loss, and no stale factors."""

from collections import defaultdict
import copy
from datetime import datetime as dt, timedelta
from typing import Any

from custom_components.solcast_solar.const import (
    AUTO_DAMPEN,
    ESTIMATE,
    FORECASTS,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    PERIOD_START,
    RESOURCE_ID,
    SITE_INFO,
    USE_ACTUALS,
)
from custom_components.solcast_solar.dampen import Dampening
from custom_components.solcast_solar.solcastapi import SolcastApi
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant, State

from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
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
