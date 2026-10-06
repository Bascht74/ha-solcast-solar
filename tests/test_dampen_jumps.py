"""Tests against jumps of the dampened forecast between neighbouring half hours.

The day series are the model 0 factors of the review probe on real data of 5 October 2026 (entries Süd-West and Ost,
estimated actuals and generation of the 7 days before), in standard time.
"""

from collections import OrderedDict
import copy
from datetime import UTC, datetime as dt, timedelta, tzinfo
from itertools import pairwise
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from freezegun import freeze_time
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS,
    ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT,
    ADVANCED_OPTIONS,
    ALL,
    AUTO_DAMPEN,
    DAMPENING_FACTOR,
    DEFAULT,
    ENTITY_DAMPEN,
    ESTIMATE,
    EXPORT_LIMITING,
    FACTOR,
    FACTORS,
    FORECASTS,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    INTERVAL,
    LAST_UPDATED,
    METHOD,
    PERIOD_START,
    RESOURCE_ID,
    SITE_ATTRIBUTE_AZIMUTH,
    SITE_ATTRIBUTE_TILT,
    SITE_DAMP,
    SITE_INFO,
    USE_ACTUALS,
    VERSION,
)
from homeassistant.components.solcast_solar.coordinator import SolcastUpdateCoordinator
from homeassistant.components.solcast_solar.dampen import Dampening
from homeassistant.components.solcast_solar.dates import DateTimeHelper
from homeassistant.components.solcast_solar.forecast import ForecastQuery
from homeassistant.components.solcast_solar.util import ease_insignificant
from homeassistant.core import HomeAssistant

from . import (
    DEFAULT_INPUT2,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
)

DAY = dt(2026, 10, 2, tzinfo=UTC)  # Matched days are 2, 3 and 4 October

# Standard-time interval: "peak" generation reached the peak (a measured 1.0), "few" too few matches, else the raw factor.
SUED_WEST: dict[int, str | float] = {
    13: "peak", 14: "few", 15: 0.946, 16: 0.872, 17: 0.934, 18: 0.918, 19: 0.934, 20: 0.937, 21: 0.966, 22: "peak", 23: "peak",
    24: 0.953, 25: 0.991, 26: 0.983, 27: 0.978, 28: 0.948, 29: 0.945, 30: 0.941, 31: 0.857, 32: 0.752, 33: 0.396, 34: "few",
    35: 0.885,
}  # fmt: skip
OST: dict[int, str | float] = {
    13: "peak", 14: "few", 15: 0.964, 16: "peak", 17: "peak", 18: "peak", 19: 0.881, 20: "peak", 21: "peak", 22: "peak", 23: 0.986,
    24: 0.911, 25: 0.888, 26: 0.802, 27: 0.621, 28: 0.486, 29: 0.556, 30: 0.442, 31: 0.517, 32: "few", 33: 0.918, 34: "few",
    35: "peak",
}  # fmt: skip


@pytest.fixture(autouse=True)
def frozen_time() -> None:
    """Override the autouse frozen_time fixture for this module."""
    return


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


def _model_inputs(
    series: dict[int, str | float], api: MagicMock, drop: dict[int, int] | None = None
) -> tuple[dict[int, list[dt]], dict[dt, float], OrderedDict[dt, float]]:
    """Return matching intervals, generation and estimated actuals that give the series with the model 0 peak of 1.0.

    drop leaves the generation of the given number of days out of an interval, as a day left out for a gap does.
    """
    matching: dict[int, list[dt]] = {}
    generation: dict[dt, float] = {}
    actuals: OrderedDict[dt, float] = OrderedDict()
    for interval, kind in series.items():
        api.peak_intervals[interval] = 1.0
        stamps = [DAY + timedelta(days=day, minutes=30 * interval) for day in range(3)]
        match kind:
            case "few":
                stamps, values = stamps[:1], [0.5]
            case "peak":
                stamps, values = stamps[:2], [1.0, 0.9]
            case _:
                values = [float(kind), float(kind) * 0.9, float(kind) * 0.7]
        values = values[: len(values) - (drop or {}).get(interval, 0)] + [0.0] * (drop or {}).get(interval, 0)
        matching[interval] = stamps
        for stamp, value in zip(stamps, values, strict=True):
            actuals[stamp] = 1.0
            generation[stamp] = value
    return matching, generation, actuals


def _isolated_ones(factors: list[float]) -> list[int]:
    return [i for i in range(1, 47) if factors[i] >= 0.999 and factors[i - 1] < 0.95 and factors[i + 1] < 0.95]


@pytest.mark.parametrize(
    ("factor", "threshold", "start", "expected"),
    [
        (0.85, 0.95, 0.0, 0.85),  # Below the band: unchanged
        (0.90, 0.95, 0.0, 0.90),
        (0.92, 0.95, 0.0, 0.94),
        (0.949, 0.95, 0.0, 0.998),  # Was 0.949, while 0.950 became 1.0
        (0.95, 0.95, 0.0, 1.0),
        (0.99, 0.95, 0.0, 1.0),
        (1.0, 0.95, 0.0, 1.0),
        (0.97, 1.0, 0.0, 0.97),  # A threshold of 1.0 ignores nothing
        (0.94, 0.95, 0.94, 0.94),  # Eased once already, or left alone by delta adjustment: not raised again
        (0.95, 0.95, 0.94, 0.96),
        (0.97, 0.95, 0.94, 1.0),
    ],
)
def test_ease_insignificant(factor: float, threshold: float, start: float, expected: float) -> None:
    """A factor near the insignificant threshold fades towards 1.0 instead of jumping to it."""
    assert ease_insignificant(factor, threshold, start) == pytest.approx(expected)


@pytest.mark.parametrize(("name", "series"), [("Süd-West", SUED_WEST), ("Ost", OST)])
async def test_no_isolated_one_between_dampened_neighbours(name: str, series: dict[int, str | float]) -> None:
    """Matched intervals without enough samples take their neighbours' line, and no 1.0 stays between dampened neighbours."""
    api = _api()
    dampening = Dampening(api)
    factors = await dampening.calculate(*_model_inputs(series, api), [], 0)

    assert not _isolated_ones(factors), name
    assert factors[:13] == [1.0] * 13  # Night: no matches, nothing filled or smoothed
    assert factors[36:] == [1.0] * 12
    for interval, kind in series.items():
        if kind == "few" and series[interval - 1] != "peak":
            assert (
                min(factors[interval - 1], factors[interval + 1]) <= factors[interval] <= max(factors[interval - 1], factors[interval + 1])
            )
            assert factors[interval] < 1.0


async def test_interpolated_values() -> None:
    """The Ost evening: intervals without enough matches lie on the line between their neighbours, measured factors stay."""
    api = _api()
    dampening = Dampening(api)
    factors = await dampening.calculate(*_model_inputs(OST, api), [], 0)

    assert factors[33] == pytest.approx(0.936)  # 0.918 eased towards 1.0
    assert factors[32] == pytest.approx((0.517 + 0.936) / 2, abs=0.001)  # Was 1.0 between 0.517 and 0.918
    assert factors[34] == pytest.approx(0.968, abs=0.001)  # Between 0.936 and the measured 1.0 at 35
    assert factors[19] == 0.881  # Measured shading between measured 1.0 values stays
    assert factors[27:32] == [0.621, 0.486, 0.556, 0.442, 0.517]


async def test_isolated_one_takes_the_higher_neighbour(caplog: pytest.LogCaptureFixture) -> None:
    """A measured 1.0 between dampened neighbours, from generation at the peak or an insignificant factor, takes their median."""
    api = _api()
    dampening = Dampening(api)
    series: dict[int, str | float] = {30: 0.80, 31: "peak", 32: 0.70, 33: 0.97, 34: 0.60, 35: "peak", 36: "peak"}
    factors = await dampening.calculate(*_model_inputs(series, api), [], 0)

    assert factors[30:37] == [0.80, 0.80, 0.70, 0.70, 0.60, 1.0, 1.0]
    assert "Smoothed factor for 15:30 is 0.800 (was 1.000)" in caplog.text


@pytest.mark.parametrize("preserve", [False, True])
async def test_edges_are_not_extended(preserve: bool) -> None:
    """An interval with too few samples and no modelled neighbour on one side stays at 1.0 (or its preserved factor)."""
    api = _api(**{ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS: preserve})
    dampening = Dampening(api)
    dampening.factors = {ALL: [0.8] * 48}
    series: dict[int, str | float] = {12: "few", 13: 0.7, 14: 0.75, 15: 0.72, 16: "few"}
    factors = await dampening.calculate(*_model_inputs(series, api), [], 0)

    edge = 0.8 if preserve else 1.0
    assert factors[12] == edge
    assert factors[16] == edge
    assert factors[13:16] == [0.7, 0.75, 0.72]


async def test_gap_day_does_not_flip_an_interval() -> None:
    """A day left out of generation leaves an interval one sample short; it is filled from its neighbours, not set to 1.0."""
    series: dict[int, str | float] = {30: 0.941, 31: 0.857, 32: 0.752, 33: 0.396, 34: 0.885}
    api = _api(**{ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION: 3})
    dampening = Dampening(api)
    full = await dampening.calculate(*_model_inputs(series, api), [], 3)
    gap = await dampening.calculate(*_model_inputs(series, api, drop={32: 1}), [], 3)

    assert full[32] < 1.0
    assert gap[32] < 1.0
    assert min(gap[31], gap[33]) <= gap[32] <= max(gap[31], gap[33])
    assert not _isolated_ones(gap)


async def test_raw_factor_rounded_before_threshold() -> None:
    """Model 0 rounds the factor before the threshold, so a stored 0.950 that delta adjustment turns into 1.0 is not left."""
    api = _api()
    dampening = Dampening(api)
    stamps = [DAY + timedelta(days=day, hours=10) for day in range(2)]
    api.peak_intervals[20] = 3.0
    factors = await dampening.calculate({20: stamps}, dict.fromkeys(stamps, 2.849), OrderedDict.fromkeys(stamps, 3.0), [], 0)
    assert factors[20] == 1.0  # 2.849 / 3.0 = 0.94967 was stored as 0.950


@pytest.mark.parametrize("delta_model", [0, 1])
def test_adjusted_factor_has_no_step(delta_model: int) -> None:
    """A forecast falling below the peak loosens a factor smoothly up to 1.0, without a step at the threshold."""
    api = _api(**{ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL: delta_model})
    dampening = Dampening(api)
    dampening.factors = {ALL: [1.0] * 48}
    dampening.factors[ALL][26] = 0.70
    dampening.target_peak_intervals = dict.fromkeys(range(48), 0.0) | {26: 2.0}
    when = dt(2026, 10, 6, 13, 0, tzinfo=UTC)
    applied = [dampening.get_factor("site", when, 2.0 * share / 100) for share in range(100, 0, -2)]

    assert applied[0] == 0.70  # At the peak the factor is kept
    assert applied == sorted(applied)
    assert max(b - a for a, b in pairwise(applied)) < 0.03  # Was a step of 0.054 to 1.0
    assert applied[-1] == 1.0
    dampening.factors[ALL][26] = 0.94  # A stored factor eased already is not raised again at the peak
    assert dampening.get_factor("site", when, 2.0) == 0.94


async def test_matching_against_normalised_peak(monkeypatch: pytest.MonkeyPatch) -> None:
    """With elevation adjustment the days are matched against the same normalised peak the default model divides by.

    The Süd-West evening of 5 October: at 17:00 standard time the oldest day had the highest estimated actual (0.349),
    while normalised to 5 October's lower sun the three days are alike (peak 0.241). Against the recorded peak only one
    day matched, the interval went to 1.0 and the dampened forecast rose by 53 % while the forecast fell by 38 %.
    """
    tz = UTC
    api = _api(tz, **{ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT: True})
    api.advanced_options = {k: copy.deepcopy(v[DEFAULT]) for k, v in ADVANCED_OPTIONS.items() if DEFAULT in v} | api.advanced_options
    api.options = SimpleNamespace(auto_dampen=True, get_actuals=True, tz=tz)
    api.sites = [{RESOURCE_ID: "site", SITE_ATTRIBUTE_TILT: 22, SITE_ATTRIBUTE_AZIMUTH: -146}]
    api.query = ForecastQuery(api)
    with freeze_time("2026-10-05 00:30:00"):
        today = api.dt_helper.day_start_utc()
        # Estimated actual, generation and elevation ratio to 5 October per day (oldest first), standard time.
        evening = {
            32: ([1.36, 1.30, 1.29], [1.0, 1.0, 1.0], [0.97, 0.99, 1.0]),
            33: ([0.86, 0.83, 0.82], [0.33, 0.32, 0.30], [0.95, 0.98, 1.0]),
            34: ([0.349, 0.245, 0.241], [0.13, 0.12, 0.13], [0.69, 0.98, 1.0]),
            35: ([0.026, 0.024, 0.025], [0.02, 0.02, 0.02], [1.0, 1.0, 1.0]),
        }
        ratios: dict[dt, float] = {}
        actuals: list[dict[str, Any]] = []
        generation: list[dict[str, Any]] = []
        for day in range(3):
            for interval, (estimates, generated, ratio) in evening.items():
                stamp = today - timedelta(days=3 - day) + timedelta(minutes=30 * interval)
                actuals.append({PERIOD_START: stamp, ESTIMATE: estimates[day] * 2})
                generation.append({PERIOD_START: stamp, GENERATION: generated[day], EXPORT_LIMITING: False})
                ratios[stamp] = ratio[day]
        api.data_actuals = {SITE_INFO: {"site": {FORECASTS: sorted(actuals, key=lambda a: a[PERIOD_START])}}}
        dampening = Dampening(api)
        dampening.data_generation = {LAST_UPDATED: today, GENERATION: generation, VERSION: 1}
        monkeypatch.setattr(dampening, "elevation_adjustment_ratio", lambda past, _target: ratios.get(past, 1.0))

        prepared = await dampening.prepare_data()
        assert api.peak_intervals[34] == 0.349
        assert len(prepared[3][34]) == 1  # Models 1-3 match against the recorded peak, as before
        assert dampening.target_peak_intervals is not None
        assert dampening.target_peak_intervals[34] == 0.241

        factors = await dampening.calculate(prepared[3], prepared[2], prepared[0], prepared[1], 0)
        assert factors[34] == pytest.approx(0.13 / 0.241, abs=0.001)  # Three days match the normalised peak
        forecast = [1.841, 1.216, 0.754, 0.060]  # Undampened 17:00 to 18:30 local time on 5 October
        dampened = [value * factor for value, factor in zip(forecast, factors[32:36], strict=True)]
        assert dampened == sorted(dampened, reverse=True)  # Falls with the forecast, no reversal

        # Delta adjustment compares a forecast with the same normalised peak: a clear evening keeps its factor.
        when = today + timedelta(minutes=30 * 34)
        dampening.factors = {ALL: factors}
        assert dampening.get_factor("site", when, 0.241) == factors[34]
        assert dampening.apply_adjustment(0.241, 0.6, 34, 0) == 0.6  # 0.748 against the recorded peak

        # Without automated dampening the recorded peaks are used again.
        api.options.auto_dampen = False
        await dampening.model_automated()
        assert dampening.target_peak_intervals is None
        assert dampening.adjustment_peak(34) == 0.349


def test_quarter_hour_zone_manual_factors() -> None:
    """In a zone with quarter-hour offsets 09:15 takes the factor of 09:00, as the automated factors do."""
    tz = ZoneInfo("Asia/Kathmandu")
    api = _api(tz)
    dampening = Dampening(api)
    dampening.factors = {"site": [round(i / 100, 2) for i in range(48)]}
    assert dampening.get_factor("site", dt(2026, 10, 6, 9, 15, tzinfo=tz), -1.0) == 0.18  # Was 0.19, the factor of 09:30
    assert dampening.adjusted_interval_dt(dt(2026, 10, 6, 9, 15, tzinfo=tz)) == 18


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
