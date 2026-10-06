"""An excluded site is not fetched, and the update plan keeps one call per fetched site for estimated actuals."""

import copy
from datetime import timedelta
import json
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from homeassistant.components.solcast_solar.const import (
    API_LIMIT,
    API_USED_TOTAL_COMBINED,
    CONFIG_ENTRY_ID,
    DAILY_ACTUALS_CONSUMED,
    DAILY_LIMIT_CONSUMED,
    DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS,
    DOMAIN,
    ENTITY_API_COUNTER,
    ESTIMATE,
    EXCEPTION_ALL_SITES_EXCLUDED,
    EXCLUDE_SITES,
    FORECASTS,
    GET_ACTUALS,
    ISSUE_ACTUALS_QUOTA_TODAY,
    ISSUE_UNUSUAL_AZIMUTH_NORTHERN,
    LAST_UPDATED,
    RESOURCE_ID,
    SERVICE_DIAGNOSTIC,
    SERVICE_SET_OPTIONS,
    SITE_ATTRIBUTE_AZIMUTH,
    SITE_ATTRIBUTE_LATITUDE,
    SITE_DAMP,
    SITE_EXPORT_ENTITY,
    SITE_INFO,
    SITES,
)
from homeassistant.components.solcast_solar.enums import UpdateOutcome
from homeassistant.components.solcast_solar.issues import sync_actuals_quota_risk_issue
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from . import (
    DEFAULT_INPUT1,
    KEY1,
    KEY2,
    MOCK_SESSION_CONFIG,
    async_cleanup_integration_tests,
    async_init_integration,
    session_reset_usage,
)
from .simulator import API_KEY_SITES

SITE1 = "1111-1111-1111-1111"
SITE2 = "2222-2222-2222-2222"


def _rooftop_sensor(entity_registry: er.EntityRegistry, site: str) -> str | None:
    return entity_registry.async_get_entity_id("sensor", DOMAIN, f"solcast_solcast_api_{site}")


def _cached_sites(path: str) -> set[str]:
    return set(json.loads(Path(path).read_text(encoding="utf-8"))[SITE_INFO])


async def test_excluded_site_is_not_fetched(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An excluded site has no data, sensor or API call, frees its calls for more updates, and is fetched again once included."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        assert coordinator.divisions == 9  # (20 - 2 for estimated actuals) / 2 sites
        assert _rooftop_sensor(entity_registry, SITE1) is not None

        # Dampened estimated actuals that differ from the undampened ones must survive a site change.
        dampened = copy.deepcopy(solcast.data_actuals)
        for actual in dampened[SITE_INFO][SITE2][FORECASTS]:
            actual[ESTIMATE] = round(actual[ESTIMATE] / 2, 4)
        assert await solcast.sites_cache.serialise_data(dampened, solcast.filename_actuals_dampened)

        caplog.clear()
        hass.config_entries.async_update_entry(entry, options={**entry.options, EXCLUDE_SITES: [SITE1]})
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        assert "Options updated, action: The integration will reload" in caplog.text
        assert f"Site {SITE1} is excluded, so it is not fetched" in caplog.text
        assert f"Site {SITE1} is excluded, removing its saved data" in caplog.text
        assert "is no longer configured" not in caplog.text
        assert "Unmanaged entity" not in caplog.text

        # Fetched, counted and shown: the other site only. Offered for exclusion: both.
        assert [site[RESOURCE_ID] for site in solcast.sites] == [SITE2]
        assert [site[RESOURCE_ID] for site in solcast.sites_all] == [SITE1, SITE2]
        assert solcast.api_sites_per_key == {KEY1: 1}
        assert coordinator.divisions == 19  # (20 - 1) / 1 site
        for data in (solcast.data, solcast.data_undampened, solcast.data_actuals, solcast.data_actuals_dampened):
            assert set(data[SITE_INFO]) == {SITE2}
        for path in (solcast.filename, solcast.filename_undampened, solcast.filename_actuals, solcast.filename_actuals_dampened):
            assert _cached_sites(path) == {SITE2}
        kept = json.loads(Path(solcast.filename_actuals_dampened).read_text(encoding="utf-8"))[SITE_INFO][SITE2][FORECASTS]
        assert kept[0][ESTIMATE] == round(solcast.data_actuals[SITE_INFO][SITE2][FORECASTS][0][ESTIMATE] / 2, 4)
        assert _rooftop_sensor(entity_registry, SITE1) is None
        assert _rooftop_sensor(entity_registry, SITE2) is not None
        assert solcast.query.get_total_energy_forecast_day(0) == pytest.approx(solcast.query.get_rooftop_site_total_today(SITE2), abs=0.01)

        caplog.clear()
        solcast.data[LAST_UPDATED] -= timedelta(minutes=1)
        assert (await solcast.fetcher.get_forecast_update(force=True)).outcome == UpdateOutcome.SUCCESS
        await solcast.fetcher.update_estimated_actuals()
        assert f"Getting forecast update for site {SITE2}" in caplog.text
        assert f"Getting estimated actuals update for site {SITE2}" in caplog.text
        assert SITE1 not in caplog.text

        flow = SolcastSolarOptionFlowHandler(entry)
        flow.hass = hass
        result = await flow.async_step_init()
        selector = next(value for key, value in result["data_schema"].schema.items() if key == EXCLUDE_SITES)  # type: ignore[union-attr]
        assert [option["value"] for option in selector.config["options"]] == [SITE1, SITE2]

        # Excluding every site is refused by the options flow and the set_options action.
        result = await flow.async_step_init({**entry.options, SITE_EXPORT_ENTITY: [], EXCLUDE_SITES: [SITE1, SITE2]})
        assert result.get("errors") == {"base": EXCEPTION_ALL_SITES_EXCLUDED}
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: entry.entry_id, EXCLUDE_SITES: f"{SITE1},{SITE2}"}, blocking=True
            )
        assert raised.value.translation_key == EXCEPTION_ALL_SITES_EXCLUDED
        assert entry.options[EXCLUDE_SITES] == [SITE1]

        # The excluded site is known, so the health check does not report it.
        result = await hass.services.async_call(DOMAIN, SERVICE_DIAGNOSTIC, {}, blocking=True, return_response=True)
        assert result is not None
        assert result["data"]["excluded_sites"] == {"configured": [SITE1], "unknown_sites": [], "all_valid": True}  # type: ignore[index, call-overload]

        # Included again, the site is fetched at once, with its past week.
        caplog.clear()
        hass.config_entries.async_update_entry(entry, options={**entry.options, EXCLUDE_SITES: []})
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        assert "New site(s) have been added, so getting forecast data for them" in caplog.text
        assert f"Polling API for site {SITE1}" in caplog.text
        for data in (solcast.data, solcast.data_undampened, solcast.data_actuals):
            assert set(data[SITE_INFO]) == {SITE1, SITE2}
        assert _rooftop_sensor(entity_registry, SITE1) is not None
        assert coordinator.divisions == 9
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_all_sites_excluded(recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """An entry that excludes every site does not load."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1) | {EXCLUDE_SITES: [SITE1, SITE2]})
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert "Every site of this entry is excluded. Keep at least one site" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_excluded_site_raises_no_azimuth_repair(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unusual azimuth of an excluded site is not reported, as the site is not used."""

    first_site = API_KEY_SITES[KEY1][SITES][0]
    latitude, azimuth = first_site[SITE_ATTRIBUTE_LATITUDE], first_site[SITE_ATTRIBUTE_AZIMUTH]
    first_site[SITE_ATTRIBUTE_LATITUDE], first_site[SITE_ATTRIBUTE_AZIMUTH] = 37.8136, 50  # Unusual in the northern hemisphere
    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1) | {EXCLUDE_SITES: [SITE1]})
        assert entry.state is ConfigEntryState.LOADED
        assert "Unusual azimuth" not in caplog.text
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_UNUSUAL_AZIMUTH_NORTHERN) is None
    finally:
        first_site[SITE_ATTRIBUTE_LATITUDE], first_site[SITE_ATTRIBUTE_AZIMUTH] = latitude, azimuth
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_granular_factors_of_an_excluded_site_are_kept(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Granular dampening factors for an excluded site do not switch off the factors of the fetched site."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1) | {EXCLUDE_SITES: [SITE2]})
        solcast = entry.runtime_data.coordinator.solcast
        factors = {SITE1: [0.5] * 24, SITE2: [0.7] * 24}
        Path(solcast.dampening.get_filename()).write_text(json.dumps(factors), encoding="utf-8")

        assert await solcast.dampening.granular_data()
        await hass.async_block_till_done()
        assert entry.options[SITE_DAMP] is True
        assert solcast.dampening.factors == factors
        noon = solcast.dt_helper.day_start_utc() + timedelta(hours=12)
        assert solcast.dampening.get_factor(SITE1, noon, 1.0) == 0.5
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


@pytest.mark.parametrize(
    ("options", "updates", "limit"),
    [
        ({API_LIMIT: "10"}, 4, 10),  # Two sites: 4 x 2 forecast calls + 2 estimated actuals calls
        ({API_LIMIT: "10", GET_ACTUALS: False}, 5, 10),  # Without estimated actuals nothing is kept
        ({API_LIMIT: "9", CONF_API_KEY: KEY2}, 8, 9),  # One site: 8 forecast calls + 1 estimated actuals call
        ({API_LIMIT: "10", EXCLUDE_SITES: [SITE2]}, 9, 10),  # An excluded site frees its calls
        ({API_LIMIT: "10,9", CONF_API_KEY: f"{KEY1},{KEY2}"}, 4, 9),  # Every key fits: (10 - 2) / 2 and (9 - 1) / 1
        # A key without fetched sites does not count, neither for the plan nor for the API limit shown
        ({API_LIMIT: "5,9", CONF_API_KEY: f"{KEY1},{KEY2}", EXCLUDE_SITES: [SITE1, SITE2]}, 8, 9),
    ],
)
async def test_plan_keeps_calls_for_estimated_actuals(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    options: dict[str, Any],
    updates: int,
    limit: int,
) -> None:
    """Auto-update plans the forecast updates that leave one call per fetched site a day for estimated actuals."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1) | options)
        coordinator = entry.runtime_data.coordinator
        assert coordinator.divisions == updates
        assert coordinator.solcast.api_limit == limit
        state = hass.states.get("sensor.solcast_pv_forecast_api_limit")
        assert state is not None
        assert state.state == str(limit)
        assert coordinator.updater.get_auto_update_details()["auto_update_divisions"] == updates
        assert ("Auto update keeps one API call per site a day for estimated actuals" in caplog.text) is options.get(GET_ACTUALS, True)
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_plan_without_updates(recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """An API limit that leaves no forecast update after the estimated actuals schedules none."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        coordinator = entry.runtime_data.coordinator
        coordinator.solcast.api_limits[KEY1] = 3
        caplog.clear()
        coordinator.updater.update_setup()
        assert coordinator.divisions == 0
        assert "The API limit leaves no automated forecast update, so none is scheduled" in caplog.text
        assert coordinator.updater.get_auto_update_details()["next_auto_update"] is None
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_estimated_actuals_count_in_tracked_usage(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """A day of auto-updates and the estimated actuals fetch uses exactly the limit, counted as Solcast counts it."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1) | {API_LIMIT: "10"})
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        await solcast.sites_cache.reset_usage_cache()
        session_reset_usage()
        assert coordinator.divisions == 4
        assert solcast.api_actuals_reserve(KEY1) == 2

        for _ in range(coordinator.divisions):
            solcast.data[LAST_UPDATED] -= timedelta(minutes=1)
            assert (await solcast.fetcher.get_forecast_update()).outcome == UpdateOutcome.SUCCESS
        assert solcast.api_used[KEY1] == 8

        # A further tracked update would take a call kept for estimated actuals, so it is refused here, not by Solcast.
        caplog.clear()
        solcast.data[LAST_UPDATED] -= timedelta(minutes=1)
        assert (await solcast.fetcher.get_forecast_update()).outcome == UpdateOutcome.FAILED
        assert f"API polling limit exhausted, not getting forecast for site {SITE1}, API used is 8/10" in caplog.text
        assert "2 API call(s) kept for the estimated actuals due before UTC midnight" in caplog.text

        # Just after local midnight, at 14:05 UTC in Brisbane, the fetch of the new local day is the last of this UTC day.
        # The entry's timers are stopped, so the fetch below is the only one.
        await coordinator.tasks_cancel()
        frozen_time.tick(solcast.dt_helper.day_start_utc(future=1) + timedelta(minutes=5) - dt_util.utcnow())
        assert not solcast.estimated_actuals_updated_today
        assert solcast.api_actuals_reserve(KEY1) == 2

        # The estimated actuals fetch is never refused, and counts in the same daily total as Solcast's.
        caplog.clear()
        await solcast.fetcher.update_estimated_actuals()
        assert "Update estimated actuals failed" not in caplog.text
        assert solcast.api_used[KEY1] == solcast.api_limits[KEY1] == MOCK_SESSION_CONFIG["api_used"][KEY1] == 10
        assert solcast.api_actuals[KEY1] == 2
        assert solcast.api_actuals_reserve(KEY1) == 0
        usage = json.loads(Path(solcast.sites_cache._get_usage_cache_filename(KEY1)).read_text(encoding="utf-8"))  # pyright: ignore[reportPrivateUsage]
        assert (usage[DAILY_LIMIT_CONSUMED], usage[DAILY_ACTUALS_CONSUMED]) == (10, 2)
        await coordinator.update_integration_listeners()
        state = hass.states.get("sensor.solcast_pv_forecast_api_used")
        assert state is not None
        assert state.state == "10"
        attributes = coordinator.get_sensor_extra_attributes(ENTITY_API_COUNTER)
        assert attributes is not None
        assert attributes[API_USED_TOTAL_COMBINED] == 10

        # A forced update still goes out, and the estimated actuals calls stay out of the typical daily usage.
        solcast.data[LAST_UPDATED] -= timedelta(minutes=1)
        assert (await solcast.fetcher.get_forecast_update(force=True)).outcome == UpdateOutcome.SUCCESS
        await solcast.sites_cache.reset_api_usage(force=True)
        assert solcast.api_typical[KEY1] == solcast.api_typical_forecast_updates[KEY1] == 10  # 8 tracked + 2 forced
        assert (solcast.api_used[KEY1], solcast.api_actuals[KEY1]) == (0, 0)

        # The quota risk repair does not count today's estimated actuals twice.
        sites = [{CONF_API_KEY: "key1"}]
        sync_actuals_quota_risk_issue(hass, sites, {"key1": 5}, {"key1": 10}, {}, 9, get_actuals=True, api_actuals={"key1": 1})
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None
        sync_actuals_quota_risk_issue(hass, sites, {"key1": 5}, {"key1": 11}, {}, 9, get_actuals=True, api_actuals={"key1": 1})
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


@pytest.mark.parametrize(("consumed", "expected"), [(4, 6), (19, 20)])
async def test_usage_written_before_actuals_counted(recorder_mock: Recorder, hass: HomeAssistant, consumed: int, expected: int) -> None:
    """A usage file from before the estimated actuals calls counted in the tracked usage adds today's calls once, up to the limit."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        solcast = entry.runtime_data.coordinator.solcast
        usage_file = Path(solcast.sites_cache._get_usage_cache_filename(KEY1))  # pyright: ignore[reportPrivateUsage]
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
        assert usage.pop(DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS) is True
        usage[DAILY_LIMIT_CONSUMED], usage[DAILY_ACTUALS_CONSUMED] = consumed, 2
        usage_file.write_text(json.dumps(usage), encoding="utf-8")

        for _ in range(2):  # Counted once
            await solcast.sites_cache._sites_usage()  # pyright: ignore[reportPrivateUsage]
            assert solcast.api_used[KEY1] == expected
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
        assert (usage[DAILY_LIMIT_CONSUMED], usage[DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS]) == (expected, True)
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
