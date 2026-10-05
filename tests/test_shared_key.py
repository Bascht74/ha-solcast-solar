"""Two entries may share an API key when each fetches other sites, each with its own share of the daily quota."""

import copy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest

from homeassistant import config_entries
from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from homeassistant.components.solcast_solar.const import (
    AFFIRMATION_REAUTH_SUCCESSFUL,
    AFFIRMATION_RECONFIGURED,
    API_LIMIT,
    AUTO_UPDATE,
    CONFIG_ENTRY_ID,
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
from homeassistant.components.solcast_solar.enums import UpdateOutcome
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


async def test_split_one_key_between_two_entries(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Süd and West with one key: each entry fetches its own site with its own limit, counter and files."""

    try:
        sued = await async_init_integration(hass, _options("10"))

        # Step 1: Süd stops fetching West, and keeps half the quota.
        flow = SolcastSolarOptionFlowHandler(sued)
        flow.hass = hass
        result = await flow.async_step_init({**sued.options, SITE_EXPORT_ENTITY: [], EXCLUDE_SITES: [SITE2], API_LIMIT: "5"})
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()

        # Step 2: a new entry with the same key takes West, and leaves Süd's site excluded.
        caplog.clear()
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: KEY1, API_LIMIT: "5", AUTO_UPDATE: "1", INSTANCE_NAME: "West"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        west = result["result"]
        assert west.state is ConfigEntryState.LOADED
        assert west.options[EXCLUDE_SITES] == [SITE1]
        assert f"Site(s) {SITE1} fetched by {sued.title}, so excluded here" in caplog.text

        sued_api = sued.runtime_data.coordinator.solcast
        west_api = west.runtime_data.coordinator.solcast
        assert [site[RESOURCE_ID] for site in sued_api.sites] == [SITE1]
        assert [site[RESOURCE_ID] for site in west_api.sites] == [SITE2]
        assert _rooftop_entry(entity_registry, SITE1) == sued.entry_id
        assert _rooftop_entry(entity_registry, SITE2) == west.entry_id
        # Each entry plans with its own site and limit: Süd keeps one call for estimated actuals, West fetches none.
        assert sued.runtime_data.coordinator.divisions == 4
        assert west.runtime_data.coordinator.divisions == 5
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None

        # Each entry counts its own calls in its own usage file.
        sued_usage = Path(sued_api.sites_cache._get_usage_cache_filename(KEY1))  # pyright: ignore[reportPrivateUsage]
        west_usage = Path(west_api.sites_cache._get_usage_cache_filename(KEY1))  # pyright: ignore[reportPrivateUsage]
        assert (sued_usage.name, west_usage.name) == ("solcast-usage.json", "solcast-west-usage.json")
        sued_used, west_used = sued_api.api_used[KEY1], west_api.api_used[KEY1]
        west_api.data[LAST_UPDATED] -= timedelta(minutes=1)
        assert (await west_api.fetcher.get_forecast_update()).outcome == UpdateOutcome.SUCCESS
        assert west_api.api_used[KEY1] == west_used + 1
        assert sued_api.api_used[KEY1] == sued_used
        for usage, api in ((sued_usage, sued_api), (west_usage, west_api)):
            assert json.loads(usage.read_text(encoding="utf-8"))[DAILY_LIMIT_CONSUMED] == api.api_used[KEY1]

        # Limits that add up to more than ten raise one repair for the key, which names the entries and their limits.
        await _set_options(hass, west, **{API_LIMIT: "6"})
        issue = issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE)
        assert issue is not None
        assert issue.translation_key == ISSUE_SHARED_API_LIMIT
        assert issue.translation_placeholders == {
            "api_key": "******1",
            "entries": f"{sued.title} (5), {west.title} (6)",
            "total": "11",
            "quota": "10",
        }

        # An ignored repair stays ignored while the entries reload, and gets no entry name.
        ir.async_ignore_issue(hass, DOMAIN, SHARED_ISSUE, True)
        await hass.config_entries.async_reload(sued.entry_id)
        await hass.config_entries.async_reload(west.entry_id)
        await hass.async_block_till_done()
        issue = issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE)
        assert issue is not None
        assert issue.dismissed_version is not None
        assert "instance" not in (issue.translation_placeholders or {})

        # A disabled entry does not count.
        await hass.config_entries.async_set_disabled_by(west.entry_id, ConfigEntryDisabler.USER)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None
        await hass.config_entries.async_set_disabled_by(west.entry_id, None)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is not None

        # Limits that fit the quota clear it.
        await _set_options(hass, sued, **{API_LIMIT: "4"})
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None

        # Removing West clears the repair and its files, and keeps Süd's files.
        await _set_options(hass, sued, **{API_LIMIT: "5"})
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is not None
        config_dir = get_config_dir(hass.config.config_dir)

        def _files(west_files: bool) -> list[str]:
            return sorted(path.name for path in config_dir.glob("solcast*") if path.name.startswith("solcast-west") is west_files)

        sued_files = _files(False)
        assert "solcast-west-usage.json" in _files(True)
        await hass.config_entries.async_remove(west.entry_id)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None
        assert _files(True) == []
        assert _files(False) == sued_files
        assert sued.state is ConfigEntryState.LOADED
        assert (
            json.loads(sued_usage.read_text(encoding="utf-8"))[DAILY_LIMIT_CONSUMED] == sued.runtime_data.coordinator.solcast.api_used[KEY1]
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_new_key_shared_with_another_entry(recorder_mock: Recorder, hass: HomeAssistant, issue_registry: ir.IssueRegistry) -> None:
    """Every flow that sets a new key excludes the sites another entry fetches, and refuses a key that leaves no site."""

    west_options = _options("20", **{CONF_API_KEY: KEY2, INSTANCE_NAME: "West"})
    shared_key = f"{KEY2},{KEY1}"
    try:
        original = await async_init_integration(hass, _options("20"))
        west = await async_init_integration(hass, west_options, unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)

        async def _reset() -> None:
            hass.config_entries.async_update_entry(west, options={**west.options, CONF_API_KEY: KEY2, EXCLUDE_SITES: []})
            await hass.async_block_till_done()
            assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is None

        def _shared() -> None:
            assert west.options[CONF_API_KEY] == shared_key
            assert west.options[EXCLUDE_SITES] == [SITE1, SITE2]
            assert west.state is ConfigEntryState.LOADED
            assert [site[RESOURCE_ID] for site in west.runtime_data.coordinator.solcast.sites] == [SITE3]
            assert west.runtime_data.coordinator.solcast.api_sites_per_key == {KEY2: 1}
            assert issue_registry.async_get_issue(DOMAIN, SHARED_ISSUE) is not None

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": west.entry_id}, data=west.data
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: shared_key, API_LIMIT: "20", AUTO_UPDATE: "1"}
        )
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()
        _shared()
        await _reset()

        result = await west.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: shared_key})
        assert result.get("reason") == AFFIRMATION_REAUTH_SUCCESSFUL
        await hass.async_block_till_done()
        _shared()
        await _reset()

        flow = SolcastSolarOptionFlowHandler(west)
        flow.hass = hass
        result = await flow.async_step_init({**west.options, SITE_EXPORT_ENTITY: [], CONF_API_KEY: shared_key})
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()
        _shared()
        await _reset()

        await _set_options(hass, west, **{CONF_API_KEY: shared_key})
        _shared()

        # The original still fetches both sites of the key, so they count once each.
        assert [site[RESOURCE_ID] for site in original.runtime_data.coordinator.solcast.sites] == [SITE1, SITE2]

        # A new key whose every site the entry excludes leaves nothing to fetch.
        hass.config_entries.async_update_entry(
            west,
            options={
                **west.options,
                CONF_API_KEY: KEY2,
                EXCLUDE_SITES: ["4444-4444-4444-4444", "5555-5555-5555-5555", "6666-6666-6666-6666"],
            },
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": west.entry_id}, data=west.data
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "3", API_LIMIT: "20", AUTO_UPDATE: "1"})
        assert result.get("errors") == {"base": EXCEPTION_ALL_SITES_EXCLUDED}
        assert west.options[CONF_API_KEY] == KEY2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


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
        west_api.fetcher._overspent_logged[KEY1] = dt_util.utcnow().date() - timedelta(days=1)  # pyright: ignore[reportPrivateUsage]
        await _refused(2)
        assert message in caplog.text
        # At its own limit the entry has spent its share; a forced call that Solcast refuses is not blamed on the other entry.
        west_api.fetcher._overspent_logged.clear()  # pyright: ignore[reportPrivateUsage]
        await _refused(5, force=True)
        assert "Solcast reports the daily limit" not in caplog.text
    finally:
        session_clear(MOCK_OVER_LIMIT)
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
