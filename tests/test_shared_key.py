"""Two entries may share an API key when each fetches other sites, each with its own share of the daily quota."""

import copy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from custom_components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from custom_components.solcast_solar.const import (
    AFFIRMATION_REAUTH_SUCCESSFUL,
    AFFIRMATION_RECONFIGURED,
    API_LIMIT,
    AUTO_UPDATE,
    CONFIG_ENTRY_ID,
    CONFIG_VERSION,
    DAILY_LIMIT_CONSUMED,
    DOMAIN,
    EXCEPTION_ALL_SITES_EXCLUDED,
    EXCLUDE_SITES,
    FORECASTS,
    INSTANCE_NAME,
    ISSUE_SHARED_API_LIMIT,
    LAST_UPDATED,
    RESOURCE_ID,
    SERVICE_SET_OPTIONS,
    SITE_EXPORT_ENTITY,
)
from custom_components.solcast_solar.enums import UpdateOutcome
import pytest

from homeassistant import config_entries
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from . import (
    DEFAULT_INPUT1,
    KEY1,
    KEY2,
    MOCK_OVER_LIMIT,
    async_cleanup_integration_tests,
    async_init_integration,
    get_config_dir,
    session_clear,
    session_set,
)

from tests.common import MockConfigEntry

SITE1 = "1111-1111-1111-1111"
SITE2 = "2222-2222-2222-2222"
SITE3 = "3333-3333-3333-3333"
SHARED_ISSUE = f"{ISSUE_SHARED_API_LIMIT}_{sha256(KEY1.encode()).hexdigest()[:12]}"


def _options(limit: str, **changes: Any) -> dict[str, Any]:
    """Options of an entry with key 1 and the given API limit."""

    return copy.deepcopy(DEFAULT_INPUT1) | {API_LIMIT: limit, "api_quota": limit} | changes


def _rooftop_entry(entity_registry: er.EntityRegistry, site: str) -> str | None:
    """Return the config entry that owns the sensor of a site."""

    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, f"solcast_solcast_api_{site}")
    entity = entity_registry.async_get(entity_id) if entity_id is not None else None
    return entity.config_entry_id if entity is not None else None


async def _set_options(hass: HomeAssistant, entry: MockConfigEntry | config_entries.ConfigEntry, **data: Any) -> None:
    """Call the set_options action for one entry and wait for its reload."""

    await hass.services.async_call(DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: entry.entry_id, **data}, blocking=True)
    await hass.async_block_till_done()


async def test_refusal_caused_by_another_entry_is_logged(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 429 below this entry's limit while another entry uses the key is logged once per UTC day; the counter still goes to the limit."""

    message = (
        "Solcast reports the daily limit of API key ******1 reached at 2 of this entry's 5 calls; "
        "Solcast PV Forecast uses the same API key and spent more than its share"
    )
    try:
        await async_init_integration(hass, _options("5", **{EXCLUDE_SITES: [SITE2]}))
        west = await async_init_integration(
            hass,
            _options("5", **{EXCLUDE_SITES: [SITE1], INSTANCE_NAME: "West"}),
            unique_id="solcast_west",
            title="Solcast West",
            orphan_hard_limit=False,
        )
        west_api = west.runtime_data.coordinator.solcast
        session_set(MOCK_OVER_LIMIT)

        async def _refused(used: int, force: bool = False) -> None:
            caplog.clear()
            west_api.api_used[KEY1] = used
            assert await west_api.fetcher.fetch_data(hours=24, path=FORECASTS, site=SITE2, api_key=KEY1, force=force) is None
            assert west_api.api_used[KEY1] == 5
            assert "API allowed polling limit has been exceeded, API counter set to 5/5" in caplog.text

        await _refused(2)
        assert message in caplog.text
        await _refused(2)
        assert "Solcast reports the daily limit" not in caplog.text

        # Logged once a UTC day, also when the entry was reloaded in between.
        assert await hass.config_entries.async_reload(west.entry_id)
        await hass.async_block_till_done()
        west_api = west.runtime_data.coordinator.solcast
        await _refused(2)
        assert "Solcast reports the daily limit" not in caplog.text
        logged = hass.data[f"{DOMAIN}_overspent_logged"]
        logged[(west.entry_id, KEY1)] = dt_util.utcnow().date() - timedelta(days=1)
        await _refused(2)
        assert message in caplog.text

        # At its own limit the entry has spent its share; a forced call that Solcast refuses is not blamed on the other entry.
        logged.clear()
        await _refused(5, force=True)
        assert "Solcast reports the daily limit" not in caplog.text
    finally:
        session_clear(MOCK_OVER_LIMIT)
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_shared_limit_counts_entries_that_fetch_with_the_key(
    recorder_mock: Recorder, hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """An entry counts towards a key's quota while it may fetch with it; disabling one that failed to load is seen at once."""

    try:
        sued = await async_init_integration(hass, _options("6", **{EXCLUDE_SITES: [SITE2]}))
        # West counts the other site, but every site it would fetch is excluded, so its set-up fails.
        west_options = _options("6", **{INSTANCE_NAME: "West", EXCLUDE_SITES: [SITE1, SITE2]})
        west = MockConfigEntry(domain=DOMAIN, title="Solcast West", data=west_options, options=west_options, version=CONFIG_VERSION)
        west.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(west.entry_id)
        await hass.async_block_till_done()
        assert west.state is ConfigEntryState.SETUP_ERROR

        # An entry that is not loaded may fetch with every key it has, so its limits count.
        issue = issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE)
        assert issue is not None
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["total"] == "12"

        # Disabling it does not unload anything, yet the repair follows at once.
        await hass.config_entries.async_set_disabled_by(west.entry_id, ConfigEntryDisabler.USER)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None
        assert sued.state is ConfigEntryState.LOADED
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_options_of_an_unloaded_entry_check_its_sites(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Including a site again in an entry that is not loaded is refused when another entry fetches it."""

    try:
        sued = await async_init_integration(hass, _options("5", **{EXCLUDE_SITES: [SITE2]}))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: KEY1, API_LIMIT: "5", AUTO_UPDATE: "1", INSTANCE_NAME: "West"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(sued.entry_id)
        await hass.async_block_till_done()
        assert sued.state is ConfigEntryState.NOT_LOADED

        flow = SolcastSolarOptionFlowHandler(sued)
        flow.hass = hass
        result = await flow.async_step_init({**sued.options, SITE_EXPORT_ENTITY: [], EXCLUDE_SITES: []})
        assert result.get("errors") == {"base": "rooftop_in_use"}
        assert result.get("description_placeholders", {}).get("rooftops") == SITE2
        assert sued.options[EXCLUDE_SITES] == [SITE2]
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
