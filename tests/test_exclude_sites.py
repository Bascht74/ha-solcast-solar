"""An excluded site is not fetched, and the update plan keeps one call per fetched site for estimated actuals."""

import copy
from datetime import timedelta
import json
from pathlib import Path

from custom_components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from custom_components.solcast_solar.const import (
    CONFIG_ENTRY_ID,
    DOMAIN,
    ESTIMATE,
    EXCEPTION_ALL_SITES_EXCLUDED,
    EXCLUDE_SITES,
    FORECASTS,
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
from custom_components.solcast_solar.enums import UpdateOutcome
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from . import (
    DEFAULT_INPUT1,
    KEY1,
    async_cleanup_integration_tests,
    async_init_integration,
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
        assert coordinator.divisions == 10  # 20 / 2 sites
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
        assert coordinator.divisions == 20  # 20 / 1 site
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

        # Excluding every site is refused by the options flow.
        result = await flow.async_step_init({**entry.options, SITE_EXPORT_ENTITY: [], EXCLUDE_SITES: [SITE1, SITE2]})
        assert result.get("errors") == {"base": EXCEPTION_ALL_SITES_EXCLUDED}
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
        assert coordinator.divisions == 10
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
