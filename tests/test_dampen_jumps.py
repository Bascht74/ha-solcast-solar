import copy
from custom_components.solcast_solar.const import (
    AUTO_DAMPEN,
    DAMPENING_FACTOR,
    ESTIMATE,
    FORECASTS,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    PERIOD_START,
    RESOURCE_ID,
    SITE_INFO,
    USE_ACTUALS,
)
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
)


async def test_attribute_factor_of_a_period_only_a_later_site_reaches(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """The dampening_factor attribute of a period only the second site reaches is this run's factor, not an old one."""
    options = copy.deepcopy(DEFAULT_INPUT2) | {
        GET_ACTUALS: True,
        USE_ACTUALS: 1,
        AUTO_DAMPEN: True,
        GENERATION_ENTITIES: ["sensor.solar_export_sensor_2222_2222_2222_2222"],
    }
    try:
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast
        dampening = solcast.dampening
        first, second = (site[RESOURCE_ID] for site in solcast.sites[:2])
        last = solcast.data_undampened[SITE_INFO][second][FORECASTS][-1]
        for data in (solcast.data, solcast.data_undampened):
            data[SITE_INFO][first][FORECASTS] = [f for f in data[SITE_INFO][first][FORECASTS] if f[PERIOD_START] < last[PERIOD_START]]
        dampening.auto_factors[last[PERIOD_START]] = 0.123  # Recorded by an earlier run

        await dampening.apply_forward()
        pv50 = 0.5 * sum(
            f[ESTIMATE]
            for data in solcast.data_undampened[SITE_INFO].values()
            for f in data[FORECASTS]
            if f[PERIOD_START] == last[PERIOD_START]
        )
        fresh = dampening.get_factor(second, last[PERIOD_START].astimezone(solcast.tz), pv50)
        assert dampening.auto_factors[last[PERIOD_START]] == fresh != 0.123
        await solcast.build_forecast_data()
        shown = next(f.get(DAMPENING_FACTOR) for f in solcast.data_forecasts if f[PERIOD_START] == last[PERIOD_START])
        assert shown == round(fresh, 4)

        # A site left out of a run keeps the factors of its periods.
        dampening.auto_factors[last[PERIOD_START]] = 0.456
        await dampening.apply_forward(applicable_sites=[first])
        assert dampening.auto_factors[last[PERIOD_START]] == 0.456
    finally:
        assert await async_cleanup_integration_tests(hass)
