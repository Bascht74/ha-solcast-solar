"""Solcast automated dampening."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
import copy
from datetime import UTC, date, datetime as dt, timedelta
from itertools import pairwise
import json
import math
from operator import itemgetter
from pathlib import Path
from statistics import mean
import time
from typing import TYPE_CHECKING, Any, Final, cast

import aiofiles

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY,
    ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS,
    ADVANCED_AUTOMATED_DAMPENING_SIMILAR_PEAK,
    ADVANCED_AUTOMATED_DAMPENING_SUPPRESSION_ENTITY,
    ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT,
    ADVANCED_HISTORY_MAX_DAYS,
    ALL,
    DEFAULT_DAMPENING_SIMILAR_PEAK,
    DOMAIN,
    DT_DATE_FORMAT,
    DT_DATE_FORMAT_SHORT,
    DT_DATE_MONTH_DAY,
    DT_DATE_ONLY_FORMAT,
    ESTIMATE,
    ESTIMATE10,
    ESTIMATE90,
    EXCEPTION_DAMP_NOT_FOR_SITE,
    EXCEPTION_DAMP_USE_ALL,
    EXCEPTION_NOT_A_SITE,
    EXPORT_LIMITING,
    FORECASTS,
    GENERATION,
    GENERATION_ENTITIES,
    GENERATION_VERSION,
    HALF_HOUR_MINUTES,
    INTERVALS_PER_DAY,
    LAST_UPDATED,
    PERIOD_START,
    PLATFORM_BINARY_SENSOR,
    PLATFORM_SENSOR,
    PLATFORM_SWITCH,
    RESOURCE_ID,
    SITE,
    SITE_ATTRIBUTE_AZIMUTH,
    SITE_ATTRIBUTE_TILT,
    SITE_DAMP,
    SITE_INFO,
    VERSION,
)
from .dampen_adapt import DampeningAdaptive
from .dates import JSONDecoder, NoIndentEncoder
from .enums import EnergyResult
from .log import get_logger
from .redact import format_site_key
from .util import (
    azimuth_to_compass_degrees,
    diff,
    ease_insignificant,
    interquartile_bounds,
    percentile,
    write_file_atomic,
)

if TYPE_CHECKING:
    from .solcastapi import SolcastApi

GRANULAR_DAMPENING_OFF: Final[bool] = False
GRANULAR_DAMPENING_ON: Final[bool] = True
SET_ALLOW_RESET: Final[bool] = True

_POWER_UNIT_FACTORS: Final[dict[str, float]] = {"mW": 1e-6, "W": 0.001, "kW": 1.0, "MW": 1000.0}
_ENERGY_UNIT_FACTORS: Final[dict[str, float]] = {"mWh": 1e-6, "Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_SUPPRESSION_ENTITY_ON_STATES: Final[tuple[str, ...]] = ("on", "1", "true", "True")
_SUPPRESSION_ENTITY_STATES: Final[tuple[str, ...]] = ("on", "off", "1", "0", "true", "false", "True", "False")
_SITE_EXPORT_INTERVAL_MINUTES: Final[int] = 5
_GENERATION_GAP_FRACTION: Final[float] = 0.1  # Share of a day's expected energy without readings that excludes a generation day
_GAP_DAYS_LOGGED: Final[int] = 5  # Days named in the log line of the one-time gap check

_LOGGER = get_logger(__name__)

try:
    from astral.sun import (
        azimuth as _astral_azimuth,  # pyright: ignore[reportAssignmentType, reportAttributeAccessIssue]
        elevation as _astral_elevation,  # pyright: ignore[reportAssignmentType, reportAttributeAccessIssue]
    )

    from homeassistant.helpers.sun import (
        get_astral_observer,  # pyright: ignore[reportAttributeAccessIssue]
    )

    _USE_ASTRAL_OBSERVER = True
except ImportError:  # pragma: no cover
    from homeassistant.helpers.sun import get_astral_location

    _USE_ASTRAL_OBSERVER = False
    _LOGGER.info("Using get_astral_location approach for solar geometry calculations")

    def _astral_elevation(loc: Any, ts: dt) -> float:  # pyright: ignore[reportAssignmentType]
        return loc.solar_elevation(ts)

    def _astral_azimuth(loc: Any, ts: dt) -> float:  # pyright: ignore[reportAssignmentType]
        return loc.solar_azimuth(ts)


def _file_mtime(filename: str) -> float:
    """Return the modification time of a file, or 0 when it does not exist (blocking, run it in the executor)."""
    try:
        return Path(filename).stat().st_mtime
    except FileNotFoundError:
        return 0


def compute_power_intervals(
    power_readings: list[tuple[dt, float]],
    generation_intervals: dict[dt, float],
) -> bool:
    """Compute time-weighted average power per 30-minute interval and add kWh to generation_intervals.

    Returns True if power readings were sufficient, False otherwise.
    """

    if len(power_readings) <= 1:
        return False

    for interval_start in generation_intervals:
        interval_end = interval_start + timedelta(minutes=30)
        weighted_sum = 0.0
        total_weight = 0.0

        for i, (reading_time, power_kw) in enumerate(power_readings):
            if i + 1 < len(power_readings):
                next_time = power_readings[i + 1][0]
            else:
                next_time = interval_end

            seg_start = max(reading_time, interval_start)
            seg_end = min(next_time, interval_end)

            if seg_start < seg_end:
                duration = (seg_end - seg_start).total_seconds()
                weighted_sum += power_kw * duration
                total_weight += duration

        if total_weight > 0:
            avg_power_kw = weighted_sum / total_weight
            generation_intervals[interval_start] += avg_power_kw * 0.5

    return True


def compute_energy_intervals(
    sample_time: list[dt],
    sample_generation: list[float],
    sample_generation_time: list[dt],
    sample_timedelta: list[int],
    generation_intervals: dict[dt, float],
    period_start: dt,
    period_end: dt,
) -> EnergyResult:
    """Distribute energy deltas across 30-minute intervals, filtering excessive jumps.

    Modifies generation_intervals in place. Returns an EnergyResult with diagnostic info.
    """

    # Determine generation-consistent or time-consistent increments.
    uniform_increment = False
    non_zero_samples = sorted([round(sample, 5) for sample in sample_generation if sample > 0.0003])
    if percentile(non_zero_samples, 25) == percentile(non_zero_samples, 75):
        uniform_increment = True
    else:
        non_zero_samples = sorted([sample for sample in sample_timedelta if sample > 0])
    _, upper = interquartile_bounds(non_zero_samples, factor=(1.5 if uniform_increment else 2.2))
    upper += 0.1 if uniform_increment else 1
    time_delta_samples = [sample for sample in sample_timedelta if sample > 0]
    if time_delta_samples:
        _, time_upper = interquartile_bounds(time_delta_samples, factor=2.2)
        time_upper += 1
    else:
        time_upper = 0

    ignored: dict[dt, bool] = {}
    last_interval: dt | None = None
    prev_report_time: dt | None = None

    if (
        len(sample_time) == len(sample_generation)
        and len(sample_time) == len(sample_generation_time)
        and len(sample_time) == len(sample_timedelta)
    ):
        for idx, (interval, kwh, report_time, time_delta) in enumerate(
            zip(sample_time, sample_generation, sample_generation_time, sample_timedelta, strict=True)
        ):
            is_excessive = False
            if interval != last_interval:
                last_interval = interval
                if uniform_increment:
                    if round(kwh, 4) > upper:
                        is_excessive = True
                        ignored[interval] = True
                elif time_delta > upper and kwh > 0.0003:
                    if kwh > 0.14:
                        is_excessive = True
                        ignored[interval] = True
                if is_excessive:
                    ignored[interval - timedelta(minutes=30)] = True

            if not is_excessive and idx > 0 and prev_report_time is not None:
                delta_start = prev_report_time
                delta_end = report_time
                current_interval_start = interval
                prev_interval_start = delta_start.replace(minute=delta_start.minute // 30 * 30, second=0, microsecond=0)

                if prev_report_time == period_start:
                    generation_intervals[current_interval_start] += kwh
                    prev_report_time = report_time
                    continue

                if report_time == period_end:
                    if prev_interval_start in generation_intervals:
                        generation_intervals[prev_interval_start] += kwh
                    prev_report_time = report_time
                    continue

                if time_upper and time_delta > time_upper and kwh > 0.0003:
                    generation_intervals[current_interval_start] += kwh
                elif prev_interval_start == current_interval_start:
                    generation_intervals[interval] += kwh
                else:
                    total_seconds = (delta_end - delta_start).total_seconds()
                    if total_seconds > 0:
                        intervals_crossed = []
                        temp_interval = prev_interval_start
                        while temp_interval <= current_interval_start:
                            interval_end = temp_interval + timedelta(minutes=30)
                            overlap_start = max(delta_start, temp_interval)
                            overlap_end = min(delta_end, interval_end)
                            if overlap_start < overlap_end:
                                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                                proportion = overlap_seconds / total_seconds
                                intervals_crossed.append((temp_interval, proportion))
                            temp_interval = interval_end

                        for crossed_interval, proportion in intervals_crossed:
                            if crossed_interval in generation_intervals:
                                generation_intervals[crossed_interval] += kwh * proportion
            elif not is_excessive and idx == 0:
                generation_intervals[interval] += kwh

            prev_report_time = report_time

        for interval in ignored:
            generation_intervals[interval] = 0.0

    return EnergyResult(uniform_increment=uniform_increment, upper=upper, ignored=ignored)


def _is_number(state: str) -> bool:
    """Return whether a state is a finite number, also negative or in exponent notation."""
    try:
        return math.isfinite(float(state))
    except ValueError:
        return False


def _valid_factors(factors: Any) -> bool:
    """Return whether granular dampening factors are a list of finite numbers between 0.0 and 1.0."""
    return isinstance(factors, list) and all(
        isinstance(factor, (int, float)) and not isinstance(factor, bool) and 0.0 <= factor <= 1.0 for factor in factors
    )


class Dampening:
    """Manages all dampening-related operations for Solcast forecasts."""

    def __init__(self, api: SolcastApi) -> None:
        """Initialise the dampening manager.

        Arguments:
            api: The parent SolcastApi instance.
        """
        self.api = api
        self.adaptive = DampeningAdaptive(self)
        self.auto_factors: dict[dt, float] = {}
        self.auto_factors_history: dict[int, dict[int, list[dict[str, Any]]]] = {}
        self.data_generation: dict[str, list[dict[str, Any]] | Any] = {
            LAST_UPDATED: dt.fromtimestamp(0, UTC),
            GENERATION: [],
            VERSION: GENERATION_VERSION,
        }
        self.filename_generation = api.filename_generation
        self.granular_allow_reset = True
        self.granular_serialising = False
        self.factors: dict[str, list[float]] = {}
        self.factors_mtime: float = 0
        self.target_peak_intervals: dict[int, float] | None = None  # Peaks the model compared with, see adjustment_peak()

    def allow_granular_reset(self) -> bool:
        """Allow options change to reset the granular dampening file to an empty dictionary."""
        return self.granular_allow_reset

    def get_filename(self) -> str:
        """Return the dampening configuration filename."""
        return self.api.filename_dampening

    def set_allow_granular_reset(self, enable: bool) -> None:
        """Set/clear allow reset granular dampening file to an empty dictionary by options change."""
        self.granular_allow_reset = enable

    def site_actuals(self, site: str) -> list[dict[str, Any]]:
        """Return a site's estimated actuals, none for a site that has none yet (a new site, or its first fetch failed)."""
        return self.api.data_actuals[SITE_INFO].get(site, {}).get(FORECASTS, [])

    def adjusted_interval_dt(self, interval: dt) -> int:
        """Adjust a datetime as standard time."""
        interval_tz = interval.astimezone(self.api.tz)
        return self._interval_index_from_tz_dt(interval_tz, self.api.dt_helper.dst_offset(interval_tz))

    @staticmethod
    def _interval_index_from_tz_dt(period_start_tz: dt, offset: timedelta) -> int:
        """Return the interval index (0-47) from a timezone-aware datetime and its DST offset, early intervals capped at 0."""
        return max(0, period_start_tz.hour * 2 + period_start_tz.minute // 30 - offset // timedelta(minutes=HALF_HOUR_MINUTES))

    @staticmethod
    def _tilt_incidence_gain(elevation: float, solar_azimuth: float, tilt: float, panel_azimuth: float) -> float:
        """Return a simple tilt-aware irradiance gain for one site."""
        elevation_rad = math.radians(elevation)
        tilt_rad = math.radians(tilt)
        azimuth_delta_rad = math.radians(solar_azimuth - panel_azimuth)

        gain = math.sin(elevation_rad) * math.cos(tilt_rad) + math.cos(elevation_rad) * math.sin(tilt_rad) * math.cos(azimuth_delta_rad)
        return max(gain, 0.0)

    def elevation_adjustment_ratio(self, past_ts: dt, target_ts: dt) -> float:
        """Return a geometry-normalisation ratio between past and target timestamps.

        Used to normalise historical PV generation samples from a prior day to the expected
        solar contribution on a target day, compensating for solar-geometry drift.

        For each site, the cos-incidence gain is computed at both timestamps; the site ratios
        are averaged across all sites.

        Arguments:
            past_ts: Timestamp of the past half-hour sample.
            target_ts: Timestamp representing the same wall-clock moment on the target day.

        Returns:
            (float) A clamped multiplier to apply to the past value.
        """
        observer_or_location = (
            get_astral_observer(self.api.hass) if _USE_ASTRAL_OBSERVER else get_astral_location(self.api.hass)[0]
        )  # pragma: no cover

        elev_past = _astral_elevation(observer_or_location, past_ts)
        elev_target = _astral_elevation(observer_or_location, target_ts)

        # Skip adjustment near the horizon where tiny sin values blow up the ratio
        # and where shading models break down anyway.
        if elev_past < 5.0 or elev_target < 5.0:
            return 1.0

        azimuth_past = _astral_azimuth(observer_or_location, past_ts)
        azimuth_target = _astral_azimuth(observer_or_location, target_ts)

        ratio_sum = 0.0
        count = 0

        for site in self.api.sites:
            tilt = float(site[SITE_ATTRIBUTE_TILT])
            # Solcast azimuth is N=0, W=+90, E=-90; astral gives a compass bearing (E=90, W=270).
            if (panel_azimuth := azimuth_to_compass_degrees(site[SITE_ATTRIBUTE_AZIMUTH])) is None:
                continue

            past_gain = self._tilt_incidence_gain(elev_past, azimuth_past, tilt, panel_azimuth)
            target_gain = self._tilt_incidence_gain(elev_target, azimuth_target, tilt, panel_azimuth)
            if past_gain <= 0.0 or target_gain <= 0.0:
                continue

            ratio_sum += target_gain / past_gain
            count += 1

        if count == 0:
            # No site sees the sun at both timestamps. Impossible fallback really. This used to be the non-azimuth/tilt adjusted return, included for posterity.
            return max(0.5, min(2.0, math.sin(math.radians(elev_target)) / math.sin(math.radians(elev_past))))

        # Clamp to avoid extreme swings from numerical edge-cases.
        return max(0.5, min(2.0, ratio_sum / count))

    def _target_timestamp(self, past_ts: dt, target_day: dt) -> dt:
        """Build a timestamp on the local date of target_day at the same UTC time-of-day as past_ts."""
        return past_ts + timedelta(days=(target_day.astimezone(self.api.tz).date() - past_ts.astimezone(self.api.tz).date()).days)

    async def apply_forward(self, applicable_sites: list[str] | None = None, do_past_hours: int = 0) -> None:
        """Apply dampening to forward forecasts."""
        if self.api.data_undampened[SITE_INFO]:
            _LOGGER.debug("Applying future dampening")

            # Factors of this run only, so a period that only a later site reaches gets this run's factor, not the last run's.
            auto_factors: dict[dt, float] = {}

            undampened_interval_pv50: defaultdict[dt, float] = defaultdict(float)
            for site in self.api.sites:
                for forecast in self.api.data_undampened[SITE_INFO].get(site[RESOURCE_ID], {}).get(FORECASTS, []):
                    period_start = forecast[PERIOD_START]
                    if period_start >= self.api.dt_helper.day_start_utc():
                        undampened_interval_pv50[period_start] += forecast[ESTIMATE] * 0.5

            record_adjustment = True
            for site in self.api.sites:
                site_id = site[RESOURCE_ID]
                # Load all forecasts.
                forecasts_undampened_future = [
                    forecast
                    for forecast in self.api.data_undampened[SITE_INFO].get(site_id, {}).get(FORECASTS, [])
                    if forecast[PERIOD_START]
                    >= (
                        self.api.dt_helper.day_start_utc()
                        if self.api.data[SITE_INFO].get(site_id)
                        else self.api.dt_helper.day_start_utc() - timedelta(hours=do_past_hours)
                    )
                ]
                forecasts = (
                    {forecast[PERIOD_START]: forecast for forecast in self.api.data[SITE_INFO][site_id][FORECASTS]}
                    if self.api.data[SITE_INFO].get(site_id)
                    else {}
                )
                sorted_forecasts_undampened_future = sorted(forecasts_undampened_future, key=itemgetter(PERIOD_START))
                apply_dampening_to_site = (site_id in applicable_sites) if applicable_sites else True

                await asyncio.sleep(0)  # Yield to event loop to avoid blocking

                for forecast in sorted_forecasts_undampened_future:
                    period_start = forecast[PERIOD_START]
                    if apply_dampening_to_site:
                        period_start = forecast[PERIOD_START]
                        pv = round(forecast[ESTIMATE], 4)
                        pv10 = round(forecast[ESTIMATE10], 4)
                        pv90 = round(forecast[ESTIMATE90], 4)

                        # Retrieve the dampening factor for the period, and dampen the estimates.
                        dampening_factor = self.get_factor(
                            site_id,
                            period_start.astimezone(self.api.tz),
                            undampened_interval_pv50.get(period_start, -1),
                            record_adjustment=record_adjustment,
                        )
                        if record_adjustment or period_start not in auto_factors:
                            # Also a period the first site lacks, as a site fetched at another time can reach further ahead.
                            auto_factors[period_start] = dampening_factor
                        pv_dampened = round(pv * dampening_factor, 4)
                        pv10_dampened = round(pv10 * dampening_factor, 4)
                        pv90_dampened = round(pv90 * dampening_factor, 4)

                        # Add or update the new entries.
                        self.api.forecast_entry_update(forecasts, period_start, pv_dampened, pv10_dampened, pv90_dampened)
                    else:
                        self.api.forecast_entry_update(
                            forecasts,
                            period_start,
                            round(forecast[ESTIMATE], 4),
                            round(forecast[ESTIMATE10], 4),
                            round(forecast[ESTIMATE90], 4),
                        )

                if apply_dampening_to_site:
                    record_adjustment = False

                await self.api.fetcher.sort_and_prune(
                    site_id, self.api.data, self.api.advanced_options[ADVANCED_HISTORY_MAX_DAYS], forecasts
                )

            # Periods of sites left out of this run keep their factor.
            self.auto_factors = {
                period_start: factor
                for period_start, factor in self.auto_factors.items()
                if period_start >= self.api.dt_helper.day_start_utc() and period_start not in auto_factors
            } | auto_factors

    async def apply_yesterday(self) -> None:
        """Apply dampening to yesterday's estimated actuals."""
        await self._apply_actuals_range(
            start=self.api.dt_helper.day_start_utc(future=-1),
            end=self.api.dt_helper.day_start_utc(),
        )

    async def apply_recovered_history(self, recovered_periods_by_site: dict[str, set[float]]) -> None:
        """Apply dampening to recovered historical estimated actuals."""
        if not recovered_periods_by_site:
            return

        recovered_periods = {
            dt.fromtimestamp(period_start, UTC) for periods in recovered_periods_by_site.values() for period_start in periods
        }
        undampened_interval_pv50 = self._build_actuals_interval_pv50(recovered_periods)

        for site in self.api.sites:
            periods = recovered_periods_by_site.get(site[RESOURCE_ID])
            if not periods:
                continue

            _LOGGER.debug(
                "Apply dampening to recovered historical estimated actuals for %s: %s",
                site[RESOURCE_ID],
                self._format_recovered_periods(periods),
            )

            actuals_undampened = [actual for actual in self.site_actuals(site[RESOURCE_ID]) if actual[PERIOD_START].timestamp() in periods]
            if not actuals_undampened:
                continue

            extant_actuals = (
                {actual[PERIOD_START]: actual for actual in self.api.data_actuals_dampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]}
                if self.api.data_actuals_dampened[SITE_INFO].get(site[RESOURCE_ID])
                else {}
            )

            for actual in actuals_undampened:
                period_start = actual[PERIOD_START]
                dampened = round(
                    actual[ESTIMATE]
                    * self.get_factor(
                        site[RESOURCE_ID],
                        period_start.astimezone(self.api.tz),
                        undampened_interval_pv50.get(period_start, -1.0),
                    ),
                    4,
                )
                self.api.forecast_entry_update(extant_actuals, period_start, dampened)

            await self.api.fetcher.sort_and_prune(
                site[RESOURCE_ID],
                self.api.data_actuals_dampened,
                self.api.advanced_options[ADVANCED_HISTORY_MAX_DAYS],
                extant_actuals,
            )

    def _format_recovered_periods(self, periods: set[float]) -> str:
        """Return local date spans for recovered periods."""
        days = sorted({dt.fromtimestamp(period_start, UTC).astimezone(self.api.tz).date() for period_start in periods})
        if not days:
            return ""

        spans: list[str] = []
        span_start = days[0]
        span_end = days[0]

        for day in days[1:]:
            if day == span_end + timedelta(days=1):
                span_end = day
                continue

            spans.append(self._format_date_range(span_start, span_end))
            span_start = day
            span_end = day

        spans.append(self._format_date_range(span_start, span_end))
        return ", ".join(spans)

    @staticmethod
    def _format_date_range(start: date, end: date) -> str:
        """Return a formatted date range string."""
        start_str = start.strftime(DT_DATE_ONLY_FORMAT)
        return start_str if start == end else f"{start_str} to {end.strftime(DT_DATE_ONLY_FORMAT)}"

    def _build_actuals_interval_pv50(self, applicable_periods: set[dt]) -> defaultdict[dt, float]:
        """Build combined pv50 values for estimated actual timestamps."""
        undampened_interval_pv50: defaultdict[dt, float] = defaultdict(float)

        for site in self.api.sites:
            for forecast in self.site_actuals(site[RESOURCE_ID]):
                period_start = forecast[PERIOD_START]
                if period_start in applicable_periods:
                    undampened_interval_pv50[period_start] += forecast[ESTIMATE] * 0.5

        return undampened_interval_pv50

    async def _apply_actuals_range(self, start: dt, end: dt) -> None:
        """Apply dampening to estimated actuals in a time range."""
        if start >= end:
            return

        undampened_interval_pv50 = self._build_actuals_interval_pv50(
            {
                forecast[PERIOD_START]
                for site in self.api.sites
                for forecast in self.site_actuals(site[RESOURCE_ID])
                if start <= forecast[PERIOD_START] < end
            }
        )

        for site in self.api.sites:
            _LOGGER.debug(
                "Apply dampening to previous day estimated actuals for %s from %s to %s",
                site[RESOURCE_ID],
                start.strftime(DT_DATE_FORMAT),
                end.strftime(DT_DATE_FORMAT),
            )

            actuals_undampened = [actual for actual in self.site_actuals(site[RESOURCE_ID]) if start <= actual[PERIOD_START] < end]
            if not actuals_undampened:
                continue

            extant_actuals = (
                {actual[PERIOD_START]: actual for actual in self.api.data_actuals_dampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]}
                if self.api.data_actuals_dampened[SITE_INFO].get(site[RESOURCE_ID])
                else {}
            )

            for actual in actuals_undampened:
                period_start = actual[PERIOD_START]
                undampened = actual[ESTIMATE]
                factor = self.get_factor(
                    site[RESOURCE_ID],
                    period_start.astimezone(self.api.tz),
                    undampened_interval_pv50.get(period_start, -1.0),
                )
                dampened = round(undampened * factor, 4)
                self.api.forecast_entry_update(extant_actuals, period_start, dampened)

            await self.api.fetcher.sort_and_prune(
                site[RESOURCE_ID],
                self.api.data_actuals_dampened,
                self.api.advanced_options[ADVANCED_HISTORY_MAX_DAYS],
                extant_actuals,
            )

    async def get(self, site: str | None, site_underscores: bool) -> list[dict[str, Any]]:
        """Retrieve the currently set dampening factors.

        Arguments:
            site (str): An optional site.
            site_underscores (bool): Whether to replace dashes with underscores in returned site names.

        Returns:
            (list[dict[str, Any]]): The action response for the presently set dampening factors.
        """
        if self.api.entry_options.get(SITE_DAMP):
            if not site:
                sites = [_site[RESOURCE_ID] for _site in self.api.sites]
            else:
                sites = [site]
            all_set = self.factors.get(ALL) is not None
            if site:
                available_sites = {item[RESOURCE_ID] for item in self.api.sites}
                if site != ALL and site not in available_sites:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key=EXCEPTION_NOT_A_SITE,
                    )
                if not all_set:
                    if site in self.factors:
                        return [
                            {
                                SITE: _site if not site_underscores else format_site_key(_site),
                                "damp_factor": ",".join(str(factor) for factor in self.factors[_site]),
                            }
                            for _site in sites
                            if self.factors.get(_site)
                        ]
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key=EXCEPTION_DAMP_NOT_FOR_SITE,
                        translation_placeholders={SITE: site},
                    )
                if site != ALL:
                    if site in self.factors:
                        _LOGGER.warning(
                            "There is dampening for site %s, but it is being overridden by an all sites entry, returning the 'all' entries instead",
                            site,
                        )
                    else:
                        _LOGGER.warning(
                            "There is no dampening set for site %s, but it is being overridden by an all sites entry, returning the 'all' entries instead",
                            site,
                        )
                return [
                    {
                        SITE: ALL,
                        "damp_factor": ",".join(str(factor) for factor in self.factors[ALL]),
                    }
                ]
            if all_set:
                return [
                    {
                        SITE: ALL,
                        "damp_factor": ",".join(str(factor) for factor in self.factors[ALL]),
                    }
                ]
            return [
                {
                    SITE: _site if not site_underscores else format_site_key(_site),
                    "damp_factor": ",".join(str(factor) for factor in self.factors[_site]),
                }
                for _site in sites
                if self.factors.get(_site)
            ]
        if not site or site == ALL:
            return [
                {
                    SITE: ALL,
                    "damp_factor": ",".join(str(factor) for _, factor in self.api.damp.items()),
                }
            ]
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=EXCEPTION_DAMP_USE_ALL,
            translation_placeholders={SITE: site},
        )

    def get_earliest_estimate_after_dampened(self, after: dt) -> dt | None:
        """Get the earliest contiguous dampened estimated actual datetime.

        Returns:
            dt | None: The earliest dampened estimated actual datetime, or None if no data.
        """
        return self._get_earliest_estimate_after(self.api.data_estimated_actuals_dampened, after=after, dampened=True)

    def get_earliest_estimate_after_undampened(self, after: dt) -> dt | None:
        """Get the earliest contiguous undampened estimated actual datetime.

        Returns:
            dt | None: The earliest undampened estimated actual datetime, or None if no data.
        """
        return self._get_earliest_estimate_after(self.api.data_estimated_actuals, after=after)

    def get_factor(self, site: str | None, period_start: dt, interval_pv50: float, record_adjustment: bool = False) -> float:
        """Retrieve either a traditional or granular dampening factor."""
        if site is not None:
            if self.api.entry_options.get(SITE_DAMP):
                if self.factors.get(ALL):
                    return self._get_granular_factor(ALL, period_start, interval_pv50, record_adjustment=record_adjustment)
                if self.factors.get(site):
                    return self._get_granular_factor(site, period_start)
                return 1.0
        return self.api.damp.get(f"{period_start.hour}", 1.0)

    @staticmethod
    def _bucket_interval_start(timestamp: dt, interval_minutes: int = HALF_HOUR_MINUTES) -> dt:
        """Return timestamp rounded down to the nearest interval boundary."""
        return timestamp.replace(
            minute=timestamp.minute // interval_minutes * interval_minutes,
            second=0,
            microsecond=0,
        )

    @staticmethod
    def _build_float_intervals(start: dt, interval_minutes: int, initial_value: float = 0.0, end: dt | None = None) -> dict[dt, float]:
        """Build a float map for the given interval size, from start to end (default one day)."""
        minutes = int(((end or start + timedelta(days=1)) - start).total_seconds()) // 60
        return {start + timedelta(minutes=minute): initial_value for minute in range(0, minutes, interval_minutes)}

    @staticmethod
    def _build_half_hour_bool_intervals(start: dt, end: dt | None = None) -> dict[dt, bool]:
        """Build a half-hour bool map from start to end (default one day)."""
        return dict.fromkeys(Dampening._build_float_intervals(start, HALF_HOUR_MINUTES, end=end), False)

    async def _get_entity_history(
        self,
        recorder_instance: Any,
        start: dt,
        end: dt,
        entity: str,
        *state_changes_args: Any,
    ) -> dict[str, list[State]]:
        """Fetch recorder state history for one entity in a time window."""
        return await recorder_instance.async_add_executor_job(
            state_changes_during_period,
            self.api.hass,
            start,
            end,
            entity,
            *state_changes_args,
        )

    async def _collect_generation_intervals_for_day(
        self,
        prev_start: dt,
        day_start: dt,
        day: int,
        entity_registry: er.EntityRegistry,
        recorder_instance: Any,
        recheck: bool = False,
    ) -> tuple[dict[dt, float], bool]:
        """Collect one day of PV generation intervals from configured entities, and whether a reading gap spoils the day.

        A recheck of a cached day finds a gap only in the readings: an entity missing now says nothing about that day,
        and a day without readings from any entity may be purged from the recorder.
        """
        generation_intervals = self._build_float_intervals(prev_start, HALF_HOUR_MINUTES, end=day_start)
        gap = False
        whole_day = [(prev_start, day_start)]
        read = silent = 0

        for entity in self.api.options.generation_entities:
            r_entity = entity_registry.async_get(entity)
            if r_entity is None or r_entity.disabled_by is not None:
                if not recheck:  # The recheck of each cached day would repeat it
                    _LOGGER.error(
                        "Generation entity %s is %s", entity, "not a valid entity" if r_entity is None else "disabled, please enable it"
                    )
                    gap = gap or self._daylight_gap(whole_day, prev_start, day_start)  # The others give only a part
                continue

            read += 1
            entity_history = await self._get_entity_history(recorder_instance, prev_start, day_start, entity)
            if entity_history.get(entity) and len(entity_history[entity]) > 4:
                _LOGGER.debug("Retrieved day %d PV generation data from entity: %s", -1 + day * -1, entity)
                states = entity_history[entity]
                spans = [  # Unavailable or unknown
                    (e.last_updated, states[i + 1].last_updated if i + 1 < len(states) else day_start)
                    for i, e in enumerate(states)
                    if not _is_number(e.state)
                ]

                if self._is_power_entity(entity):
                    # Power entity: compute time-weighted average kW per interval, then convert to kWh (* 0.5).
                    conversion_factor = self._get_conversion_factor(entity, entity_history[entity], is_power=True)
                    power_readings: list[tuple[dt, float]] = [
                        (e.last_updated.astimezone(UTC), float(e.state) * conversion_factor)
                        for e in entity_history[entity]
                        if _is_number(e.state)
                    ]

                    if not compute_power_intervals(power_readings, generation_intervals):
                        _LOGGER.debug("Insufficient power readings for entity: %s", entity)
                    gap = gap or self._daylight_gap(spans, prev_start, day_start)
                    continue

                # Energy entity: compute deltas and distribute across intervals.
                conversion_factor = self._get_conversion_factor(entity, entity_history[entity])
                numeric_entries = [
                    (e.last_updated.astimezone(UTC), float(e.state) * conversion_factor)
                    for e in entity_history[entity]
                    if _is_number(e.state)
                ]
                sample_time: list[dt] = [self._bucket_interval_start(ts) for ts, _ in numeric_entries]
                sample_generation: list[float] = [0.0, *diff([v for _, v in numeric_entries])]
                sample_generation_time: list[dt] = [ts for ts, _ in numeric_entries]
                sample_timedelta: list[int] = [
                    0,
                    *diff([(ts - prev_start).total_seconds() for ts, _ in numeric_entries]),
                ]

                if sample_generation_time and sample_generation_time[0] == prev_start:
                    sample_generation[0] = 0.0
                    sample_timedelta[0] = 0

                entity_intervals = self._build_float_intervals(
                    prev_start, HALF_HOUR_MINUTES, end=day_start
                )  # An ignored jump zeroes only this entity
                result: EnergyResult = compute_energy_intervals(
                    sample_time,
                    sample_generation,
                    sample_generation_time,
                    sample_timedelta,
                    entity_intervals,
                    prev_start,
                    day_start,
                )
                for interval_start, generation in entity_intervals.items():
                    generation_intervals[interval_start] += generation
                _LOGGER.debug(
                    f"%s increments detected for entity: %s, outlier upper bound: {'%.3f kWh' if result.uniform_increment else '%d seconds'}",  # noqa: G004
                    "Generation-consistent" if result.uniform_increment else "Time-consistent",
                    entity,
                    result.upper,
                )
                for interval in result.ignored:
                    _LOGGER.debug(
                        "Ignoring excessive PV generation jump at %s from entity: %s",
                        interval.astimezone(self.api.tz).strftime(DT_DATE_FORMAT),
                        entity,
                    )
                spans += [  # Readings missing before an ignored catch-up jump
                    (sample_generation_time[i - 1], sample_generation_time[i])
                    for i in range(1, len(sample_time))
                    if sample_time[i] in result.ignored
                ]
                gap = gap or self._daylight_gap(spans, prev_start, day_start)
            else:
                _LOGGER.debug(
                    "No day %d PV generation data (or barely any) from entity: %s (%s)",
                    -1 + day * -1,
                    entity,
                    entity_history.get(entity),
                )
                silent += 1
                gap = gap or (not recheck and self._daylight_gap(whole_day, prev_start, day_start))  # No usable readings all day
        if recheck and 0 < silent < read:  # Readings from some entities only
            gap = gap or self._daylight_gap(whole_day, prev_start, day_start)

        for interval_start, generation in generation_intervals.items():
            generation_intervals[interval_start] = round(generation, 3)

        return generation_intervals, gap

    def _daylight_gap(self, spans: list[tuple[dt, dt]], prev_start: dt, day_start: dt) -> bool:
        """Return whether spans without readings miss a significant share of the day's expected energy.

        Each half hour weighs as much as its peak estimated actual, so a sensor that is unavailable at night or at dusk,
        when little or nothing is generated, does not spoil a day, while an outage of an hour around noon does.
        """
        half_hour = timedelta(minutes=HALF_HOUR_MINUTES)
        expected = missing = 0.0
        for interval in self._build_half_hour_bool_intervals(prev_start, day_start):
            if (weight := self.api.peak_intervals.get(self.adjusted_interval_dt(interval), 0.0)) <= 0:
                continue
            expected += weight
            overlap = sum(
                (min(end, interval + half_hour) - max(start, interval)) / half_hour
                for start, end in spans
                if start < interval + half_hour and end > interval
            )
            missing += weight * min(overlap, 1.0)
        return missing > 0 and missing >= expected * _GENERATION_GAP_FRACTION

    async def _apply_suppression_entity_limits(
        self,
        export_limiting: dict[dt, bool],
        prev_start: dt,
        day_start: dt,
        entity_registry: er.EntityRegistry,
        recorder_instance: Any,
    ) -> None:
        """Apply suppression-entity state history to export-limiting intervals."""
        platforms = [PLATFORM_BINARY_SENSOR, PLATFORM_SENSOR, PLATFORM_SWITCH]
        find_entity = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_SUPPRESSION_ENTITY]
        entity = ""
        found = False
        for platform in platforms:
            entity = f"{platform}.{find_entity}"
            r_entity = entity_registry.async_get(entity)
            if r_entity is not None and r_entity.disabled_by is None:
                found = True
                break
        if not found:
            return

        _LOGGER.debug("Suppression entity %s exists", entity)
        entity_history = await self._get_entity_history(
            recorder_instance,
            prev_start,
            day_start,
            entity,
            True,  # No attributes
            False,  # Descending order
            None,  # Limit
            True,  # Include start time state
        )

        if not entity_history.get(entity) or len(entity_history[entity]) == 0:
            return

        entity_state: dict[dt, bool] = {}
        state = False
        for e in entity_history[entity]:
            if e.state not in _SUPPRESSION_ENTITY_STATES:
                continue

            interval = self._bucket_interval_start(e.last_updated.astimezone(UTC))
            if e.state in _SUPPRESSION_ENTITY_ON_STATES:
                state = True
                if not entity_state.get(interval):
                    entity_state[interval] = state
                    interval_plus_half_hour = interval + timedelta(minutes=HALF_HOUR_MINUTES)
                    if state and entity_state.get(interval_plus_half_hour) is not None:
                        entity_state.pop(interval_plus_half_hour)
                _LOGGER.debug(
                    "Interval %s state change %s at %s",
                    interval.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                    entity_state[interval],
                    e.last_updated.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                )
            elif state:
                state = False
                interval_plus_half_hour = interval + timedelta(minutes=HALF_HOUR_MINUTES)
                entity_state[interval_plus_half_hour] = False
                _LOGGER.debug(
                    "Interval %s state change %s at %s",
                    interval_plus_half_hour.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                    entity_state[interval_plus_half_hour],
                    e.last_updated.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                )

        state = False
        for interval in export_limiting:
            if entity_state.get(interval) is not None:
                state = entity_state[interval]
            export_limiting[interval] = state
            if state:
                _LOGGER.debug("Auto-dampen suppressed for interval %s", interval.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT))

    async def _apply_site_export_limits(
        self,
        export_limiting: dict[dt, bool],
        prev_start: dt,
        day_start: dt,
        entity_registry: er.EntityRegistry,
        recorder_instance: Any,
    ) -> None:
        """Apply site export-limit detection to half-hour intervals."""
        if self.api.options.site_export_limit <= 0 or self.api.options.site_export_entity == "":
            return

        entity = self.api.options.site_export_entity
        r_entity = entity_registry.async_get(entity)
        if r_entity is None:
            _LOGGER.error("Site export entity %s is not a valid entity", entity)
            return
        if r_entity.disabled_by is not None:
            _LOGGER.error("Site export entity %s is disabled, please enable it", entity)
            return

        export_intervals = self._build_float_intervals(prev_start, _SITE_EXPORT_INTERVAL_MINUTES, end=day_start)
        entity_history = await self._get_entity_history(recorder_instance, prev_start, day_start, entity)
        if not entity_history.get(entity) or len(entity_history[entity]) == 0:
            _LOGGER.debug("No site export history found for %s", entity)
            return

        conversion_factor = self._get_conversion_factor(entity, entity_history[entity])
        readings = [
            (e.last_updated.astimezone(UTC), float(e.state) * conversion_factor) for e in entity_history[entity] if _is_number(e.state)
        ]

        # Spread each export delta over the time since the previous reading, so a meter that reports every 15 minutes
        # does not show three 5-minute intervals of export in one.
        step = timedelta(minutes=_SITE_EXPORT_INTERVAL_MINUTES)
        for (start, previous), (end, current) in pairwise(readings):
            if end <= start:
                continue
            interval = self._bucket_interval_start(start, _SITE_EXPORT_INTERVAL_MINUTES)
            while interval < end:
                if interval in export_intervals:
                    overlap = min(end, interval + step) - max(start, interval)
                    export_intervals[interval] += (current - previous) * overlap / (end - start)
                interval += step

        for interval, export in export_intervals.items():
            export_intervals[interval] = round(export * (60 / _SITE_EXPORT_INTERVAL_MINUTES), 3)

        for interval, export in export_intervals.items():
            export_interval = self._bucket_interval_start(interval)
            if export >= self.api.options.site_export_limit:
                export_limiting[export_interval] = True

    async def get_pv_generation(self) -> None:
        """Get PV generation from external entity/entities.

        Supports two entity types:
        - Energy entities (Wh/kWh/MWh, total increasing): Computes energy deltas and distributes across intervals.
        - Power entities (W/kW/MW, instantaneous): Computes time-weighted average power per interval, then converts to kWh.

        The entities must have state history. Very large units are not supported (e.g. GWh, TWh) because of precision loss.
        """

        start_time = time.time()

        # Load the generation history.
        generation: dict[dt, dict[str, Any]] = {generated[PERIOD_START]: generated for generated in self.data_generation[GENERATION]}
        load_days: int = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS]
        days = load_days
        if generation:  # Every day missing since the last stored interval, for example after an outage, at least yesterday
            missing = (
                self.api.dt_helper.day_start_utc().astimezone(self.api.tz).date() - max(generation).astimezone(self.api.tz).date()
            ).days - 1
            days = min(load_days, max(1, missing))

        entity_registry = er.async_get(self.api.hass)
        recorder_instance = get_instance(self.api.hass)
        await self.prepare_data(only_peaks=True)  # Peak intervals tell daylight for gap detection

        for day in range(days):
            # PV generation
            # On UTC :00/:30 like the estimated actuals, also where local midnight is at :15 or :45 UTC
            day_start = self._bucket_interval_start(self.api.dt_helper.day_start_utc(future=(-1 * day)))
            prev_start = self._bucket_interval_start(self.api.dt_helper.day_start_utc(future=(-1 * day) - 1))  # 23 or 25 hours at DST
            generation_intervals, gap = await self._collect_generation_intervals_for_day(
                prev_start,
                day_start,
                day,
                entity_registry,
                recorder_instance,
            )
            if gap:
                _LOGGER.debug(
                    "PV generation on %s has a gap in daylight readings, excluding the day from accuracy and dampening",
                    prev_start.astimezone(self.api.tz).strftime(DT_DATE_ONLY_FORMAT),
                )
                for interval in generation_intervals:
                    generation.pop(interval, None)
                continue

            export_limiting = self._build_half_hour_bool_intervals(prev_start, day_start)
            await self._apply_suppression_entity_limits(
                export_limiting,
                prev_start,
                day_start,
                entity_registry,
                recorder_instance,
            )
            await self._apply_site_export_limits(
                export_limiting,
                prev_start,
                day_start,
                entity_registry,
                recorder_instance,
            )

            # Add recent generation intervals to the history.
            generation.update(
                {
                    i: {PERIOD_START: i, GENERATION: generated, EXPORT_LIMITING: export_limiting[i]}
                    for i, generated in generation_intervals.items()
                }
            )

        # Trim, sort and serialise.
        self.data_generation = {
            LAST_UPDATED: dt_util.now(UTC).replace(microsecond=0),
            VERSION: GENERATION_VERSION,
            GENERATION_ENTITIES: sorted(self.api.options.generation_entities),
            GENERATION: sorted(
                filter(
                    lambda generated: generated[PERIOD_START] >= self.api.dt_helper.day_start_utc(future=-22),
                    generation.values(),
                ),
                key=itemgetter(PERIOD_START),
            ),
        }
        await self.api.sites_cache.serialise_data(self.data_generation, self.filename_generation)
        _LOGGER.debug("Task get_pv_generation took %.3f seconds", time.time() - start_time)

    async def recheck_generation_gaps(self) -> bool:
        """Leave out the cached days that have a reading gap, once for a cache written before gaps were checked.

        Only a day the recorder still holds is checked. A day it has purged cannot be read again, so its cached
        generation is the only copy left and stays.

        Returns:
            bool: Whether a day was left out.
        """
        generation: dict[dt, dict[str, Any]] = {generated[PERIOD_START]: generated for generated in self.data_generation[GENERATION]}
        entity_registry = er.async_get(self.api.hass)
        recorder_instance = get_instance(self.api.hass)
        recorded_since = dt_util.now(UTC) - timedelta(days=recorder_instance.keep_days)
        await self.prepare_data(only_peaks=True)
        dropped: list[str] = []
        day = 0
        while generation and (day_start := self._bucket_interval_start(self.api.dt_helper.day_start_utc(future=-day))) > min(generation):
            prev_start = self._bucket_interval_start(self.api.dt_helper.day_start_utc(future=-day - 1))
            if prev_start < recorded_since:
                break  # Older days may be purged from the recorder already.
            intervals = [interval for interval in generation if prev_start <= interval < day_start]
            if intervals:
                _, gap = await self._collect_generation_intervals_for_day(
                    prev_start, day_start, day, entity_registry, recorder_instance, recheck=True
                )
                if gap:
                    dropped.append(prev_start.astimezone(self.api.tz).strftime(DT_DATE_ONLY_FORMAT))
                    for interval in intervals:
                        generation.pop(interval)
            day += 1
        if dropped:
            dropped.sort()
            _LOGGER.debug(
                "Cached PV generation of %d day(s) has a gap in daylight readings, leaving it out: %s%s",
                len(dropped),
                ", ".join(dropped[:_GAP_DAYS_LOGGED]),
                f" and {len(dropped) - _GAP_DAYS_LOGGED} more" if len(dropped) > _GAP_DAYS_LOGGED else "",
            )
        self.data_generation[GENERATION] = sorted(generation.values(), key=itemgetter(PERIOD_START))
        self.data_generation[VERSION] = GENERATION_VERSION
        await self.api.sites_cache.serialise_data(self.data_generation, self.filename_generation)
        return bool(dropped)

    async def granular_data(self) -> bool:
        """Read the current granular dampening file.

        Returns:
            bool: Granular dampening in use.
        """

        def option(enable: bool, set_allow_reset: bool = False):
            site_damp = self.api.entry_options.get(SITE_DAMP, False) if self.api.entry_options.get(SITE_DAMP) is not None else False
            if enable ^ site_damp:
                options = {**self.api.entry_options}
                options[SITE_DAMP] = enable
                self.api.entry_options[SITE_DAMP] = enable
                if set_allow_reset:
                    self.granular_allow_reset = enable
                if self.api.entry is not None:
                    self.api.hass.config_entries.async_update_entry(self.api.entry, options=options)
            return enable

        error = False
        return_value = False
        filename = self.get_filename()
        try:
            # Taken before reading, so a change while reading is seen next time, and before an options update can start.
            self.factors_mtime = await self.api.hass.async_add_executor_job(_file_mtime, filename)
            if not self.factors_mtime:
                self.factors = {}
                return option(GRANULAR_DAMPENING_OFF)
            async with aiofiles.open(filename) as file:
                content = await file.read()
                try:
                    response_json = json.loads(content)
                except json.decoder.JSONDecodeError:
                    _LOGGER.error("JSONDecodeError, dampening ignored: %s", filename)
                    error = True
                    return option(GRANULAR_DAMPENING_OFF, SET_ALLOW_RESET)
                self.factors = cast(dict[str, Any], response_json)
                if not isinstance(response_json, dict):
                    _LOGGER.error("Dampening must be a dictionary of sites in %s, dampening ignored", filename)
                    error = True
                    return option(GRANULAR_DAMPENING_OFF, SET_ALLOW_RESET)
                if content.replace("\n", "").replace("\r", "").strip() != "" and self.factors:
                    first_site_len = 0
                    # An excluded site is still known: its factors stay in the file and apply once it is included again.
                    known_sites = {ALL, *self.api.site_transfers, *(site[RESOURCE_ID] for site in (*self.api.sites_all, *self.api.sites))}
                    for site, damp_list in self.factors.items():
                        if site not in known_sites or not _valid_factors(damp_list):
                            _LOGGER.error(
                                "Dampening factors for %s must be for a known site and between 0.0 and 1.0 in %s, dampening ignored",
                                site,
                                filename,
                            )
                            self.factors = {}
                            error = True
                            break
                        if first_site_len == 0:
                            first_site_len = len(damp_list)
                        elif len(damp_list) != first_site_len:
                            _LOGGER.error(
                                "Number of dampening factors for all sites must be the same in %s, dampening ignored",
                                filename,
                            )
                            self.factors = {}
                            error = True
                        if len(damp_list) not in (24, 48):
                            _LOGGER.error(
                                "Number of dampening factors for site %s must be 24 or 48 in %s, dampening ignored",
                                site,
                                filename,
                            )
                            self.factors = {}
                            error = True
                    if error:
                        return_value = option(GRANULAR_DAMPENING_OFF, SET_ALLOW_RESET)
                    else:
                        _LOGGER.debug("Granular dampening %s", str(self.factors))
                        return_value = option(GRANULAR_DAMPENING_ON, SET_ALLOW_RESET)
            return return_value
        finally:
            if error:
                self.factors = {}

    async def load_generation_data(self) -> dict[str, Any] | None:
        """Load generation data from cache file.

        Returns:
            dict[str, Any] | None: The loaded generation data, or None if not found.
        """
        data = None
        if await self.api.hass.async_add_executor_job(Path(self.filename_generation).is_file):
            async with aiofiles.open(self.filename_generation) as data_file:
                json_data: dict[str, Any] = json.loads(await data_file.read(), cls=JSONDecoder)
                # Note that the generation data cache does not have a version number
                # Future changes to the structure, if any, will need to be handled here by checking current version by allowing for None
                _LOGGER.debug(
                    "Data cache %s exists, file type is %s",
                    self.filename_generation,
                    type(json_data),
                )
                if isinstance(json_data, dict):
                    data = json_data
                    _LOGGER.debug("Generation data loaded")
        return data

    async def migrate_undampened_history(self) -> None:
        """Migrate un-dampened forecasts if un-dampened data for a site does not exist."""
        apply_dampening: list[str] = []
        forecasts: dict[str, dict[dt, Any]] = {}
        past_days = self.api.dt_helper.day_start_utc(future=-14)
        for site in self.api.sites:
            site = site[RESOURCE_ID]
            if not self.api.data_undampened[SITE_INFO].get(site) or len(self.api.data_undampened[SITE_INFO][site].get(FORECASTS, [])) == 0:
                _LOGGER.info(
                    "Migrating un-dampened history to %s for %s",
                    self.api.filename_undampened,
                    site,
                )
                apply_dampening.append(site)
            else:
                continue
            # Load the forecast history.
            forecasts[site] = {forecast[PERIOD_START]: forecast for forecast in self.api.data[SITE_INFO][site][FORECASTS]}
            forecasts_undampened: list[dict[str, Any]] = []
            # Migrate forecast history if un-dampened data does not yet exist.
            if forecasts[site]:
                forecasts_undampened = sorted(
                    {
                        forecast[PERIOD_START]: forecast
                        for forecast in self.api.data[SITE_INFO][site][FORECASTS]
                        if forecast[PERIOD_START] >= past_days
                    }.values(),
                    key=itemgetter(PERIOD_START),
                )
                _LOGGER.debug(
                    "Migrating %d forecast entries to un-dampened forecasts for site %s",
                    len(forecasts_undampened),
                    site,
                )
            self.api.data_undampened[SITE_INFO].update({site: {FORECASTS: copy.deepcopy(forecasts_undampened)}})

        if apply_dampening:
            self.api.data_undampened[LAST_UPDATED] = dt_util.now(UTC).replace(microsecond=0)
            await self.api.sites_cache.serialise_data(self.api.data_undampened, self.api.filename_undampened)

        if apply_dampening:
            await self.apply_forward(applicable_sites=apply_dampening)
            await self.api.sites_cache.serialise_data(self.api.data, self.api.filename)

    async def calculate_error(
        self,
        generation_day: defaultdict[dt, float],
        generation: defaultdict[dt, dict[str, Any]],
        values: tuple[dict[str, Any], ...],
        percentiles: tuple[int, ...] = (50,),
        log_breakdown: bool = False,
        breakdown_label: str = "",
    ) -> tuple[bool, float, list[float], dict[str, float]]:
        """Calculate mean and percentile absolute percentage error."""
        value_day: defaultdict[dt, float] = defaultdict(float)
        error: defaultdict[dt, float] = defaultdict(float)
        last_day: dt | None = None

        for interval in values:
            if generation.get(interval[PERIOD_START]) is None:
                continue  # Only days with generation, so an excluded day is not an infinite error
            i = interval[PERIOD_START].astimezone(self.api.options.tz).replace(hour=0, minute=0, second=0, microsecond=0)
            if i != last_day:
                value_day[i] = 0.0
                last_day = i
            if not generation[interval[PERIOD_START]][EXPORT_LIMITING]:
                value_day[i] += interval[ESTIMATE] / 2  # 30 minute intervals

        for day, value in value_day.items():
            error[day] = abs(generation_day[day] - value) / generation_day[day] * 100.0 if generation_day[day] > 0 else math.inf

            if log_breakdown:
                label_prefix = f"{breakdown_label} " if breakdown_label else ""
                _LOGGER.debug(
                    "%sAPE calculation for day %s, Actual %.2f kWh, Estimate %.2f kWh, Error %.2f%s",
                    label_prefix,
                    day.strftime(DT_DATE_ONLY_FORMAT),
                    generation_day[day],
                    value,
                    error[day],
                    "%" if error[day] != math.inf else "",
                )

        non_inf_error: dict[dt, float] = {k: v for k, v in error.items() if v != math.inf}
        daily: dict[str, float] = {k.strftime(DT_DATE_ONLY_FORMAT): round(v, 2) for k, v in non_inf_error.items()}
        return (
            (
                (len(error) != len(non_inf_error)),
                mean(non_inf_error.values()),
                [percentile(sorted(non_inf_error.values()), p) for p in percentiles],
                daily,
            )
            if non_inf_error
            else (False, math.inf, [math.inf] * len(percentiles), {})
        )

    async def check_deal_breaker_automated(self) -> bool:
        """Check for deal breakers that would prevent automated dampening from running.

        Returns:
            bool: True if a deal breaker is found, False otherwise.
        """
        deal_breaker = ""
        deal_breaker_site = ""
        if not self.data_generation[GENERATION]:
            deal_breaker = "No generation yet"
            await self._reset_automated_factors()
        else:
            for site in self.api.sites:
                if self.api.data_actuals[SITE_INFO].get(site[RESOURCE_ID]) is None:
                    deal_breaker = "No estimated actuals yet"
                    deal_breaker_site = site[RESOURCE_ID]
                    break
        if deal_breaker:
            _LOGGER.info("Auto-dampening suppressed: %s%s", deal_breaker, f" for {deal_breaker_site}" if deal_breaker_site else "")
            return True
        return False

    async def _reset_automated_factors(self) -> None:
        """Reset the automated factors to 1.0 when no generation is left to model them from.

        Factors modelled from generation that is gone (other generation entities, or every day left out) would
        otherwise stay in force until generation is there again.
        """
        if not self.api.options.auto_dampen or not any(factor != 1.0 for factor in self.factors.get(ALL, [])):
            return
        _LOGGER.warning("Auto-dampening has no PV generation to model from, so its factors are reset to 1.0 until there is")
        self.factors[ALL] = [1.0] * INTERVALS_PER_DAY
        await self.serialise_granular()
        await self.granular_data()

    async def model_automated(self, force: bool = False) -> None:
        """Model the automated dampening of the forecast data.

        Look for consistently low PV generation in consistently high estimated actual intervals.
        Dampening factors are always referenced using standard time (not daylight savings time).
        """
        start_time = time.time()

        if not self.api.options.auto_dampen and not force:
            _LOGGER.debug("Automated dampening is not enabled, skipping dampening model_automated()")
            await self.prepare_data(only_peaks=True)
            self.target_peak_intervals = None
            return

        if await self.check_deal_breaker_automated():
            return

        actuals, ignored_intervals, generation, matching_intervals = await self.prepare_data()

        _LOGGER.debug("Modelling automated dampening factors")

        dampening = await self.calculate(
            matching_intervals, generation, actuals, ignored_intervals, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL]
        )

        if dampening != self.factors.get(ALL):
            self.factors[ALL] = dampening
            await self.serialise_granular()
            await self.granular_data()
        _LOGGER.debug("Task dampening model_automated took %.3f seconds", time.time() - start_time)

    async def prepare_generation_data(self, earliest_start: dt) -> tuple[defaultdict[dt, dict[str, Any]], defaultdict[dt, float]]:
        """Prepare generation data for accuracy metrics calculation.

        ignore_unmatched excludes intervals below minimum peak in
        determine_best_settings.
        """
        ignored_intervals: list[int] = []  # Intervals to ignore in standard time

        for time_string in self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS]:
            hour, minute = map(int, time_string.split(":"))
            interval = hour * 2 + minute // 30
            ignored_intervals.append(interval)

        model_intervals = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] * INTERVALS_PER_DAY
        export_limited_intervals = dict.fromkeys(range(INTERVALS_PER_DAY), False)
        if not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY]:
            for gen in self.data_generation[GENERATION][-model_intervals:]:
                if gen[EXPORT_LIMITING]:
                    export_limited_intervals[self._adjusted_interval(gen)] = True

        data_generation = copy.deepcopy(self.data_generation)
        generation_dampening: defaultdict[dt, dict[str, Any]] = defaultdict(dict[str, Any])
        generation_dampening_day: defaultdict[dt, float] = defaultdict(float)
        for record in data_generation.get(GENERATION, [])[-model_intervals:]:
            if record[PERIOD_START] < earliest_start:
                continue

            interval = self.adjusted_interval_dt(record[PERIOD_START])
            if interval in ignored_intervals or export_limited_intervals[interval]:
                record[EXPORT_LIMITING] = True
                continue

            generation_dampening[record[PERIOD_START]] = {
                GENERATION: record[GENERATION],
                EXPORT_LIMITING: record[EXPORT_LIMITING],
            }
            if not record[EXPORT_LIMITING]:
                generation_dampening_day[
                    record[PERIOD_START].astimezone(self.api.options.tz).replace(hour=0, minute=0, second=0, microsecond=0)
                ] += record[GENERATION]

        return generation_dampening, generation_dampening_day

    async def refresh_granular_data(self) -> None:
        """Load granular dampening data if the file has changed."""
        mtime = await self.api.hass.async_add_executor_job(_file_mtime, self.get_filename())
        if mtime and mtime != self.factors_mtime:
            await self.granular_data()
            _LOGGER.info("Granular dampening loaded")
            _LOGGER.debug(
                "Granular dampening file mtime %s",
                dt.fromtimestamp(mtime, self.api.tz).strftime(DT_DATE_FORMAT),
            )

    async def serialise_granular(self) -> None:
        """Serialise the site dampening file."""
        filename = self.get_filename()
        _LOGGER.debug("Writing granular dampening to %s", filename)
        payload = json.dumps(
            self.factors,
            ensure_ascii=False,
            cls=NoIndentEncoder,
            indent=2,
        )
        self.granular_serialising = True
        try:
            async with self.api.serialise_lock:
                await self.api.hass.async_add_executor_job(write_file_atomic, filename, payload)
        finally:
            self.granular_serialising = False
        self.factors_mtime = await self.api.hass.async_add_executor_job(_file_mtime, filename)
        _LOGGER.debug(
            "Granular dampening file mtime %s",
            dt.fromtimestamp(self.factors_mtime, self.api.tz).strftime(DT_DATE_FORMAT),
        )

    def _adjusted_interval(self, interval: dict[str, Any]) -> int:
        """Adjust a forecast/actual interval as standard time."""
        return self.adjusted_interval_dt(interval[PERIOD_START])

    def adjustment_peak(self, interval: int) -> float:
        """Return the peak estimated actual a forecast is compared with by delta adjustment.

        With elevation adjustment it is the peak normalised to the sun of the day the factors were modelled for, as the
        model compared generation with that peak. Otherwise, and before the model ran, it is the peak as recorded.
        """
        return (self.target_peak_intervals if self.target_peak_intervals is not None else self.api.peak_intervals)[interval]

    def apply_adjustment(self, interval_pv50, factor, interval, delta_adjustment_model) -> float:
        """Applies selected delta_adjustment_model to past dampening factor."""
        peak = self.adjustment_peak(interval)
        match delta_adjustment_model:
            case 1:
                # Adjust the factor based on how far the forecast falls short of the peak interval, squared.
                # A forecast at or above the peak keeps the factor, and the factor never exceeds 1.0.
                shortfall = max(0.0, 1.0 - (interval_pv50 / peak))
                factor = max(factor, min(1.0, factor + ((1.0 - factor) * (shortfall**2))))
            case _:
                # Adjust the factor based on forecast vs. peak interval delta-logarithmically.
                factor = max(
                    factor,
                    min(
                        1.0,
                        factor + ((1.0 - factor) * (math.log(peak) - math.log(interval_pv50))),
                    ),
                )

        return round(factor, 3)

    def _get_conversion_factor(self, entity: str, entity_history: list[State] | None = None, is_power: bool = False) -> float:
        """Get the conversion factor for an entity to convert to kWh (energy) or kW (power)."""

        if is_power:
            unit_factors = _POWER_UNIT_FACTORS
            default_unit = "kW"
        else:
            unit_factors = _ENERGY_UNIT_FACTORS
            default_unit = "kWh"

        entity_unit = None

        if entity_history:
            latest_state = entity_history[-1]
            if hasattr(latest_state, "attributes") and latest_state.attributes:
                entity_unit = latest_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if not entity_unit:
            entity_registry = er.async_get(self.api.hass)
            entity_entry = entity_registry.async_get(entity)
            if entity_entry and entity_entry.unit_of_measurement:
                entity_unit = entity_entry.unit_of_measurement

        if not entity_unit:
            _LOGGER.warning("Entity %s has no %s, assuming %s", entity, ATTR_UNIT_OF_MEASUREMENT, default_unit)
            return 1.0

        conversion_factor = unit_factors.get(entity_unit)
        if conversion_factor is None:
            _LOGGER.error("Entity %s has an unsupported %s '%s', assuming %s", entity, ATTR_UNIT_OF_MEASUREMENT, entity_unit, default_unit)
            return 1.0

        if conversion_factor != 1.0:
            _LOGGER.debug("Entity %s uses %s, applying conversion factor %s", entity, entity_unit, conversion_factor)

        return conversion_factor

    def _is_power_entity(self, entity: str) -> bool:
        """Determine whether a generation entity is a power (W/kW) entity rather than energy (Wh/kWh)."""

        entity_registry = er.async_get(self.api.hass)
        r_entity = entity_registry.async_get(entity)
        if r_entity is not None:
            dc = r_entity.device_class or r_entity.original_device_class
            if dc == SensorDeviceClass.POWER:
                return True
        return False

    async def calculate(  # noqa: C901
        self,
        matching_intervals: dict[int, list[dt]],
        generation: dict[dt, float],
        actuals: dict[dt, float],
        ignored_intervals: list[int],
        dampening_model: int,
        verbose_log: bool = True,
        target_day: dt | None = None,
    ) -> list[float]:
        """Applies selected dampening_model to passed data to calculate list of dampening factors."""

        dampening = [1.0] * INTERVALS_PER_DAY  # Initialise dampening factors
        known = [False] * INTERVALS_PER_DAY  # A factor modelled from data (1.0 included) or preserved
        unknown: list[int] = []  # Matched intervals with too few samples for a factor of their own

        apply_elevation_adjustment = bool(self.api.advanced_options.get(ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT, False))
        if apply_elevation_adjustment and target_day is None:
            target_day = self.api.dt_helper.day_start_utc()

        # For the default model, ceiling comes from self.api.peak_intervals, which is the un-normalised max of past estimated
        # actuals across MODEL_DAYS. It is normalised here (when elevation adjustment is enabled) to target_day's sun elevation.
        peak_intervals: dict[int, float] = self.api.peak_intervals
        if apply_elevation_adjustment and target_day is not None:
            normalised, peak_intervals = self._normalise_to_target_day(actuals, target_day)
            if dampening_model not in (1, 2, 3):
                # The default model divides by the normalised peak, so it also matches the days against it. Against the
                # recorded peak only the oldest days match near sunrise and sunset in spring and autumn, too few for a factor.
                matching_intervals = self._similar_to_peak(normalised, peak_intervals, matching_intervals)

        # Check the generation for each interval and determine if it is consistently lower than the peak.
        for interval, matching in matching_intervals.items():
            # Get current factor if required
            if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]:
                prior_factor = self.factors[ALL][interval] if self.factors.get(ALL) is not None else 1.0

            interval_time = self._interval_time(interval)
            if interval in ignored_intervals:
                if verbose_log:
                    _LOGGER.debug("Interval %s is intentionally ignored, skipping", interval_time)
                continue
            # Build (timestamp, gen, elevation_ratio) triplets for matching intervals
            # that have non-zero generation. The ratio normalises generation to target_day's
            # sun geometry for the default model, which compares it with the peak of other
            # days. Models 1/2/3 compare generation with the estimated actual of the same
            # interval, which already shares its sun geometry, so they use no ratio.
            sample_triplets: list[tuple[dt, float, float]] = []
            for timestamp in matching:
                raw_gen = round(generation.get(timestamp, 0.0), 3)
                if raw_gen == 0.0:
                    continue
                if apply_elevation_adjustment and target_day is not None and dampening_model not in (1, 2, 3):
                    ratio = self.elevation_adjustment_ratio(timestamp, self._target_timestamp(timestamp, target_day))
                else:
                    ratio = 1.0
                sample_triplets.append((timestamp, raw_gen, ratio))
            generation_samples: list[float] = [gen for _, gen, _ in sample_triplets]
            preserve_this_interval = False
            if matching:
                msg = ""
                log_msg = True
                if verbose_log:
                    _LOGGER.debug(
                        "Interval %s has peak estimated actual %.3f and %d matching intervals: %s",
                        interval_time,
                        peak_intervals[interval],
                        len(matching),
                        ", ".join([date.astimezone(self.api.tz).strftime(DT_DATE_MONTH_DAY) for date in matching]),
                    )
                match dampening_model:
                    case 1 | 2 | 3:
                        if len(matching) >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]:
                            actual_samples: list[float] = [actuals.get(timestamp, 0.0) for timestamp, _, _ in sample_triplets]
                            if verbose_log:
                                _LOGGER.debug(
                                    "Selected %d estimated actuals for %s: %s",
                                    len(actual_samples),
                                    interval_time,
                                    ", ".join(f"{act:.3f}" for act in actual_samples),
                                )
                                _LOGGER.debug(
                                    "Selected %d generation records for %s: %s",
                                    len(generation_samples),
                                    interval_time,
                                    generation_samples,
                                )
                            if (
                                len(generation_samples)
                                >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION]
                            ):
                                if len(actual_samples) == len(generation_samples):
                                    raw_factors: list[float] = []
                                    for act, gen in zip(actual_samples, generation_samples, strict=True):
                                        if act <= 0:
                                            raw_factors.append(1.0)
                                            continue
                                        # Cap at 1.0 since dampening cannot amplify.
                                        raw_factors.append(min(gen / act, 1.0))
                                    if verbose_log:
                                        _LOGGER.debug(
                                            "Candidate factors for %s: %s",
                                            interval_time,
                                            ", ".join(f"{fact:.3f}" for fact in raw_factors),
                                        )
                                    match dampening_model:
                                        case 1:  # max factor from matched pairs
                                            factor = max(raw_factors)
                                        case 2:  # average factor from matched pairs
                                            factor = mean(raw_factors)
                                        case 3:  # min factor from matched pairs
                                            factor = min(raw_factors)
                                    factor = round(factor, 3) if factor > 0 else 1.0
                                    msg, factor = self._insignificant_eased(factor, interval_time)
                                    dampening[interval] = factor
                                    known[interval] = True
                                msg = (
                                    f"Mismatched sample lengths for {interval_time}: {len(actual_samples)} actuals vs {len(generation_samples)} generations"
                                    if len(actual_samples) != len(generation_samples)
                                    else msg
                                )
                            else:
                                msg = f"Not enough reliable generation samples for {interval_time} to determine dampening ({len(generation_samples)})"
                                preserve_this_interval = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]
                    case _:
                        # Normalise the numerator. Historical generation samples are scaled to what they would have been on target_day
                        # given that interval's sun elevation. Pair the denominator (peak_intervals, already normalised above).
                        normalised_generation = [round(gen * ratio, 3) for _, gen, ratio in sample_triplets]
                        peak = max(normalised_generation) if normalised_generation else 0.0
                        if verbose_log:
                            _LOGGER.debug("Interval %s max generation: %.3f, %s", interval_time, peak, normalised_generation)
                        if len(matching) >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]:
                            if peak < peak_intervals[interval]:
                                if (
                                    len(generation_samples)
                                    >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION]
                                ):
                                    factor = round(peak / peak_intervals[interval], 3) if peak_intervals[interval] != 0 else 1.0
                                    msg, dampening[interval] = self._insignificant_eased(factor, interval_time)
                                    known[interval] = True
                                else:
                                    msg = f"Not enough reliable generation samples for {interval_time} to determine dampening ({len(generation_samples)})"
                                    preserve_this_interval = self.api.advanced_options[
                                        ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS
                                    ]
                            else:
                                log_msg = False
                                known[interval] = True  # Generation reached the peak: a measured 1.0

                if not preserve_this_interval:
                    msg = (
                        f"Not enough matching intervals for {interval_time} to determine dampening"
                        if len(matching) < self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]
                        else msg
                    )
                    preserve_this_interval = (
                        self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]
                        and len(matching) < self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]
                    )

                if preserve_this_interval:
                    dampening[interval] = prior_factor
                    known[interval] = True
                    msg = msg + f", preserving prior factor {prior_factor:.3f}" if prior_factor != 1.0 else msg
                elif not known[interval]:
                    unknown.append(interval)

                if log_msg and msg != "" and verbose_log:
                    _LOGGER.debug(msg)

        return self._smooth_factors(dampening, known, unknown, verbose_log)

    def _insignificant_eased(self, factor: float, interval_time: str) -> tuple[str, float]:
        """Return the log message and a modelled factor eased towards 1.0 near the insignificant threshold."""
        eased = ease_insignificant(factor, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR])
        if eased == 1.0 and factor < 1.0:
            return f"Ignoring insignificant factor for {interval_time} of {factor:.3f}", eased
        if eased != factor:
            return f"Auto-dampen factor for {interval_time} is {eased:.3f}, eased towards 1.0 from {factor:.3f}", eased
        return f"Auto-dampen factor for {interval_time} is {factor:.3f}", factor

    def _smooth_factors(self, dampening: list[float], known: list[bool], unknown: list[int], verbose_log: bool) -> list[float]:
        """Fill matched intervals that lack samples from their neighbours, then remove a 1.0 between dampened neighbours.

        An interval with too few samples would otherwise stay at 1.0 between dampened neighbours, and one sample day more
        or less flips it, so the dampened forecast jumps between half hours. It takes the straight line between the nearest
        modelled intervals on both sides; without one on both sides it stays at 1.0, so nothing is extended beyond them.
        A 1.0 between two dampened neighbours, from generation that reached the peak or an insignificant factor, takes the
        median of the three, the higher neighbour. Other factors keep their value, so shading measured in a single
        interval stays. Intervals without matches (night) or ignored ones neither change nor count as neighbours.
        """
        filled = list(known)
        modelled = [interval for interval, is_known in enumerate(known) if is_known]
        for interval in unknown:
            before = [i for i in modelled if i < interval]
            after = [i for i in modelled if i > interval]
            if before and after:
                left, right = before[-1], after[0]
                dampening[interval] = round(dampening[left] + (dampening[right] - dampening[left]) * (interval - left) / (right - left), 3)
                filled[interval] = True
                if verbose_log:
                    _LOGGER.debug("Interpolated factor for %s is %.3f", self._interval_time(interval), dampening[interval])

        smoothed = list(dampening)
        for interval in range(1, INTERVALS_PER_DAY - 1):
            if dampening[interval] == 1.0 and filled[interval - 1] and filled[interval] and filled[interval + 1]:
                smoothed[interval] = max(dampening[interval - 1], dampening[interval + 1])
                if verbose_log and smoothed[interval] != 1.0:
                    _LOGGER.debug("Smoothed factor for %s is %.3f (was 1.000)", self._interval_time(interval), smoothed[interval])
        return smoothed

    def _interval_time(self, interval: int) -> str:
        """Return the local time of a standard-time interval today, for the log."""
        offset = self.api.dt_helper.dst_offset(
            dt_util.now(self.api.tz).replace(hour=interval // 2, minute=30 * (interval % 2), second=0, microsecond=0)
        )
        local = interval + offset // timedelta(minutes=HALF_HOUR_MINUTES)
        return f"{local // 2:02}:{30 * (local % 2):02}"

    @staticmethod
    def _get_earliest_estimate_after(data: list[dict[str, Any]], after: dt, dampened: bool = False) -> dt | None:
        """Get the earliest estimated actual datetime after a specified datetime."""
        earliest = None
        if data:
            # Find all actuals with period_start >= after, then get the earliest one
            in_scope_actuals = [actual[PERIOD_START] for actual in data if actual[PERIOD_START] >= after]
            earliest = min(in_scope_actuals) if in_scope_actuals else None
            _LOGGER.debug(
                "Earliest applicable %s estimated actual datetime is %s",
                "dampened" if dampened else "undampened",
                earliest,
            )
        return earliest

    def _get_granular_factor(self, site: str, period_start: dt, interval_pv50: float = -1.0, record_adjustment: bool = False) -> float:
        """Retrieve a granular dampening factor."""
        index = period_start.hour if len(self.factors[site]) == 24 else period_start.hour * 2 + period_start.minute // 30
        factor = self.factors[site][index]
        if (
            site == ALL
            and (self.api.options.auto_dampen or self.api.advanced_options[ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT])
            and self.factors.get(ALL)
        ):
            interval = self.adjusted_interval_dt(period_start)
            # Automated factors are indexed in standard time, manual factors in local time with or without delta adjustment.
            factor = min(1.0, self.factors[ALL][interval if self.api.options.auto_dampen else index])
            if (
                not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT]
                and self.adjustment_peak(interval) > 0
                and interval_pv50 > 0
                and factor < 1.0
            ):
                interval_time = period_start.astimezone(self.api.tz).strftime(DT_DATE_FORMAT)
                factor_pre_adjustment = factor

                factor = self.apply_adjustment(
                    interval_pv50, factor, interval, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL]
                )

                if (
                    record_adjustment
                    and period_start.astimezone(self.api.tz).date() == dt_util.now(self.api.tz).date()
                    and round(factor, 3) != round(factor_pre_adjustment, 3)
                ):
                    _LOGGER.debug(
                        "%sdjusted granular dampening factor for %s, %.3f (was %.3f, peak %.3f, interval pv50 %.3f)",
                        "Ignoring insignificant a"
                        if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED] <= factor < 1.0
                        else "A",
                        interval_time,
                        factor,
                        factor_pre_adjustment,
                        self.adjustment_peak(interval),
                        interval_pv50,
                    )
                factor = ease_insignificant(
                    factor, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED], factor_pre_adjustment
                )

        return min(1.0, factor)

    async def prepare_data(
        self, only_peaks: bool = False, until: dt | None = None, target_day: dt | None = None
    ) -> tuple[OrderedDict[dt, float], list[int], dict[dt, float], dict[int, list[dt]]]:
        """Builds data required for dampening calculations, for the model days before until (default today).

        target_day is the day the factors are modelled for (default today). With elevation adjustment the peaks kept for
        delta adjustment are normalised to its sun.
        """
        actuals: OrderedDict[dt, float] = OrderedDict()
        model_days: int = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS]
        day_end = until or self.api.dt_helper.day_start_utc()

        _LOGGER.debug("Determining peak estimated actual intervals%s", " and dampening data" if not only_peaks else "")
        if (
            self.api.options.auto_dampen or self.api.advanced_options[ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT]
        ) and self.api.options.get_actuals:
            for site in self.api.sites:
                site_history = self.site_actuals(site[RESOURCE_ID])
                start, end = self.api.query.get_list_slice(
                    site_history,
                    day_end - timedelta(days=model_days),
                    day_end,
                    search_past=True,
                )
                site_actuals = {actual[PERIOD_START]: actual for actual in site_history[start:end]}
                for period_start, site_actual in site_actuals.items():
                    extant: float | None = actuals.get(period_start)
                    if extant is not None:
                        actuals[period_start] += site_actual[ESTIMATE] * 0.5
                    else:
                        actuals[period_start] = site_actual[ESTIMATE] * 0.5

            # Collect top intervals from the past MODEL_DAYS days.
            self.api.peak_intervals = dict.fromkeys(range(INTERVALS_PER_DAY), 0.0)
            for period_start, actual in actuals.items():
                interval = self.adjusted_interval_dt(period_start)
                if self.api.peak_intervals[interval] < actual:
                    self.api.peak_intervals[interval] = round(actual, 3)

        if only_peaks:
            return actuals, [], {}, {}

        ignore_intervals_cfg: list[str] = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS]
        no_limiting: bool = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY]

        ignored_intervals: list[int] = []  # Intervals to ignore in local time zone
        for time_string in ignore_intervals_cfg:
            hour, minute = map(int, time_string.split(":"))
            interval = hour * 2 + minute // 30
            ignored_intervals.append(interval)

        model_intervals = model_days * INTERVALS_PER_DAY
        recent = [gen for gen in self.data_generation[GENERATION] if until is None or gen[PERIOD_START] < until][-model_intervals:]
        export_limited_intervals = dict.fromkeys(range(INTERVALS_PER_DAY), False)
        if not no_limiting:
            for gen in recent:
                if gen[EXPORT_LIMITING]:
                    export_limited_intervals[self._adjusted_interval(gen)] = True

        generation: dict[dt, float] = {}
        for gen in recent:
            if not no_limiting:
                if not export_limited_intervals[self._adjusted_interval(gen)]:
                    generation[gen[PERIOD_START]] = gen[GENERATION]
            elif not gen[EXPORT_LIMITING]:
                generation[gen[PERIOD_START]] = gen[GENERATION]

        # Delta adjustment compares a forecast with the peak the default model divides by, with elevation adjustment the
        # peak normalised to the target day's sun.
        self.target_peak_intervals = (
            self._normalise_to_target_day(actuals, target_day or self.api.dt_helper.day_start_utc())[1]
            if self.api.advanced_options.get(ADVANCED_AUTOMATED_DAMPENING_ELEVATION_ADJUSTMENT, False)
            else dict(self.api.peak_intervals)
        )

        # Collect intervals that are close to the peak.
        matching_intervals = self._similar_to_peak(actuals, self.api.peak_intervals, range(INTERVALS_PER_DAY))
        return actuals, ignored_intervals, generation, matching_intervals

    def _similar_to_peak(self, actuals: dict[dt, float], peaks: dict[int, float], intervals: Iterable[int]) -> dict[int, list[dt]]:
        """Return per interval the periods whose estimated actual is close to the interval's peak."""
        similar_peak: float = self.api.advanced_options.get(ADVANCED_AUTOMATED_DAMPENING_SIMILAR_PEAK, DEFAULT_DAMPENING_SIMILAR_PEAK)
        matching_intervals: dict[int, list[dt]] = {interval: [] for interval in intervals}
        for period_start, actual in actuals.items():
            interval = self.adjusted_interval_dt(period_start)
            if interval in matching_intervals and actual > similar_peak * peaks[interval]:
                matching_intervals[interval].append(period_start)
        return matching_intervals

    def _normalise_to_target_day(self, actuals: dict[dt, float], target_day: dt) -> tuple[dict[dt, float], dict[int, float]]:
        """Return the estimated actuals scaled to the sun of target_day, and their peak per standard-time interval."""
        normalised: dict[dt, float] = {}
        peaks: dict[int, float] = dict.fromkeys(range(INTERVALS_PER_DAY), 0.0)
        for period_start, actual in actuals.items():
            normalised[period_start] = adjusted = actual * self.elevation_adjustment_ratio(
                period_start, self._target_timestamp(period_start, target_day)
            )
            interval = self.adjusted_interval_dt(period_start)
            if peaks[interval] < adjusted:
                peaks[interval] = round(adjusted, 3)
        return normalised, peaks
