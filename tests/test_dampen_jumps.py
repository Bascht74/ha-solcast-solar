import copy
from datetime import UTC, datetime as dt, timedelta, tzinfo
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
from custom_components.solcast_solar.const import (
    ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS,
    ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT,
    ALL,
    AUTO_DAMPEN,
    DAMPENING_FACTOR,
    ENTITY_DAMPEN,
    ESTIMATE,
    FACTOR,
    FACTORS,
    FORECASTS,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    INTERVAL,
    METHOD,
    PERIOD_START,
    RESOURCE_ID,
    SITE_DAMP,
    SITE_INFO,
    USE_ACTUALS,
)
from custom_components.solcast_solar.coordinator import SolcastUpdateCoordinator
from custom_components.solcast_solar.dampen import Dampening
from custom_components.solcast_solar.dates import DateTimeHelper
from freezegun import freeze_time
import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
)


def _api(tz: tzinfo = UTC, **advanced: Any) -> MagicMock:
    """Build an API with the options calculate() and get_factor() read."""
    api = MagicMock()
    api.tz = tz
    api.dt_helper = DateTimeHelper(tz)
    api.peak_intervals = dict.fromkeys(range(48), 0.0)
    api.options = SimpleNamespace(auto_dampen=True, tz=tz)
    api.entry_options = {SITE_DAMP: True}
    api.advanced_options = {
        ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS: False,
        ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS: 2,
        ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION: 2,
        ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR: 0.95,
        ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED: 0.95,
        ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT: False,
        ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT: False,
        ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL: 0,
        ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT: False,
    } | advanced
    for attribute in ("filename_generation", "filename_dampening"):
        with tempfile.NamedTemporaryFile() as handle:  # Only the name is used; the file goes when it closes
            setattr(api, attribute, handle.name)
    return api


class _NegativeDst(tzinfo):
    """A zone with negative summer time, as tzdata models Morocco during Ramadan."""

    def utcoffset(self, _dt: dt | None) -> timedelta:
        return timedelta(0)

    def dst(self, _dt: dt | None) -> timedelta:
        return timedelta(hours=-1)

    def tzname(self, _dt: dt | None) -> str:
        return "NEG"


@pytest.mark.parametrize(
    ("zone", "local", "dst", "interval"),
    [
        ("Australia/Lord_Howe", dt(2026, 12, 1, 12, 0), True, 23),  # Summer time is half an hour: 11:30 standard time
        ("Australia/Lord_Howe", dt(2026, 7, 1, 12, 0), False, 24),
        ("Australia/Sydney", dt(2026, 12, 1, 12, 0), True, 22),
        ("Europe/Dublin", dt(2026, 7, 1, 12, 0), True, 22),
        ("Europe/Dublin", dt(2026, 12, 1, 12, 0), False, 24),
        ("UTC", dt(2026, 7, 1, 12, 0), False, 24),
        ("negative", dt(2026, 3, 1, 23, 30), False, 47),
    ],
)
def test_half_hour_daylight_saving(zone: str, local: dt, dst: bool, interval: int) -> None:
    """Daylight saving time of half an hour moves the interval by one, negative summer time not at all."""
    tz: tzinfo = _NegativeDst() if zone == "negative" else (UTC if zone == "UTC" else ZoneInfo(zone))
    helper = DateTimeHelper(tz)
    stamp = local.replace(tzinfo=tz)
    assert helper.dst(stamp) is dst
    dampening = Dampening(_api(tz))
    assert dampening.adjusted_interval_dt(stamp) == interval


@freeze_time("2026-12-01T01:00:00+00:00")  # 12:00 summer time on Lord Howe Island
def test_lord_howe_labels() -> None:
    """The dampening attribute shows a standard-time factor at its local time, half an hour later in summer."""
    tz = ZoneInfo("Australia/Lord_Howe")
    coordinator = object.__new__(SolcastUpdateCoordinator)
    solcast = MagicMock()
    solcast.entry_options = {SITE_DAMP: True}
    solcast.options.auto_dampen = True
    solcast.options.tz = tz
    solcast.dt_helper = DateTimeHelper(tz)
    factors = [1.0] * 48
    factors[23] = 0.5
    solcast.dampening.factors = {ALL: factors}
    solcast.dampening.factors_mtime = 0
    solcast.advanced_options = {}
    coordinator.solcast = solcast
    coordinator._SolcastUpdateCoordinator__get_value = {ENTITY_DAMPEN: [{METHOD: lambda: True}]}  # pyright: ignore[reportAttributeAccessIssue]

    result = coordinator.get_sensor_extra_attributes(ENTITY_DAMPEN)
    assert result is not None
    labels = {entry[INTERVAL]: entry[FACTOR] for entry in result[FACTORS]}
    assert len(labels) == 48
    assert labels["12:00"] == 0.5
    assert "24:00" not in labels


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
