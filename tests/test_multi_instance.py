"""Two Solcast entries must stay on their own files, sensors and devices."""

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant import config_entries
from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar import state
from homeassistant.components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from homeassistant.components.solcast_solar.const import (
    AFFIRMATION_RECONFIGURED,
    AFFIRMATION_UNCHANGED,
    API_LIMIT,
    AUTO_DAMPEN,
    AUTO_UPDATE,
    CONFIG_ENTRY_ID,
    CUSTOM_HOURS,
    DAMP_FACTOR,
    DOMAIN,
    ENTRY_ID,
    EXCEPTION_API_ERROR,
    EXCEPTION_INSTANCE_REQUIRED,
    EXCEPTION_INSTANCE_UNKNOWN,
    EXCEPTION_ROOFTOP_IN_USE,
    EXCEPTION_SET_OPTIONS_EMPTY,
    EXCLUDE_SITES,
    GENERATION_ENTITIES,
    INSTANCE_NAME,
    INSTANCE_SLUG,
    ISSUE_CORRUPT_FILE,
    ISSUE_RECORDS_MISSING,
    ISSUE_RECORDS_MISSING_FIXABLE,
    ISSUE_UNUSUAL_AZIMUTH_NORTHERN,
    RESOURCE_ID,
    SERVICE_GET_OPTIONS,
    SERVICE_SET_OPTIONS,
    SITE_DAMP,
    SITE_EXPORT_ENTITY,
    SITE_INFO,
)
from homeassistant.components.solcast_solar.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.components.solcast_solar.energy import async_get_solar_forecast
from homeassistant.components.solcast_solar.instance import (
    repair_issue_id,
    repair_placeholders,
)
from homeassistant.components.solcast_solar.repairs import (
    RecordsMissingRepairFlow,
    async_create_fix_flow,
)
from homeassistant.components.solcast_solar.updater import Updater
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_LABEL_ID,
    CONF_API_KEY,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    label_registry as lr,
    service,
)
from homeassistant.util import dt as dt_util

from . import (
    DEFAULT_INPUT1,
    KEY1,
    KEY2,
    MOCK_BUSY,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    get_config_dir,
    no_error_or_exception,
    reload_integration,
    session_clear,
    session_set,
    wait_for_it,
)

from tests.common import MockConfigEntry


def _device(hass: HomeAssistant, entry_id: str):
    """Return the single device created for one config entry."""

    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    assert len(device) == 1
    return device[0]


async def test_two_instances_stay_separate(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A named entry does not share the original entry's files, sensors, sites or service calls."""

    legacy_options = copy.deepcopy(DEFAULT_INPUT1)
    west_options = copy.deepcopy(DEFAULT_INPUT1)
    west_options[CONF_API_KEY] = KEY2
    west_options[INSTANCE_NAME] = "West"

    try:
        legacy = await async_init_integration(hass, legacy_options)
        west = await async_init_integration(
            hass,
            west_options,
            unique_id="solcast_west",
            title="Solcast West",
            orphan_hard_limit=False,
        )
        no_error_or_exception(caplog)
        # The slug of an entry from before it was stored is stored once, unchanged; the original entry gets none.
        assert west.options[INSTANCE_SLUG] == "west"
        assert INSTANCE_SLUG not in legacy.options

        legacy_api = legacy.runtime_data.coordinator.solcast
        west_api = west.runtime_data.coordinator.solcast
        assert legacy_api is not west_api
        assert legacy_api.filename.endswith("solcast.json")
        assert legacy_api.filename.endswith("solcast-west.json") is False
        assert west_api.filename.endswith("solcast-west.json")
        assert legacy_api.sites_cache._get_usage_cache_filename("key").endswith("solcast-usage.json")
        assert west_api.sites_cache._get_usage_cache_filename("key").endswith("solcast-west-usage.json")
        assert legacy_api.sites_cache._get_sites_cache_filename("key").endswith("solcast-sites.json")
        assert west_api.sites_cache._get_sites_cache_filename("key").endswith("solcast-west-sites.json")
        assert legacy_api.filename_dampening != west_api.filename_dampening
        assert {site[RESOURCE_ID] for site in legacy_api.sites} == {"1111-1111-1111-1111", "2222-2222-2222-2222"}
        assert {site[RESOURCE_ID] for site in west_api.sites} == {"3333-3333-3333-3333"}

        registry = entity_registry
        legacy_ids = {entity.unique_id for entity in er.async_entries_for_config_entry(registry, legacy.entry_id)}
        west_ids = {entity.unique_id for entity in er.async_entries_for_config_entry(registry, west.entry_id)}
        assert legacy_ids.isdisjoint(west_ids)
        assert "total_kwh_forecast_today" in legacy_ids
        assert "west_total_kwh_forecast_today" in west_ids
        assert "solcast_solcast_api_1111-1111-1111-1111" in legacy_ids
        assert "solcast_solcast_api_3333-3333-3333-3333" in west_ids

        legacy_device = _device(hass, legacy.entry_id)
        west_device = _device(hass, west.entry_id)
        assert legacy_device.name == "Solcast PV Forecast"
        assert west_device.name == "Solcast West"
        assert legacy_device.id != west_device.id
        assert (DOMAIN, legacy.entry_id) in legacy_device.identifiers
        assert (DOMAIN, west.entry_id) in west_device.identifiers

        untouched = await hass.services.async_call(DOMAIN, "get_options", {}, blocking=True, return_response=True)
        assert untouched["data"][CONF_API_KEY] == KEY1
        targeted = await hass.services.async_call(
            DOMAIN,
            "get_options",
            {ATTR_DEVICE_ID: west_device.id},
            blocking=True,
            return_response=True,
        )
        assert targeted["data"][CONF_API_KEY] == KEY2

        await hass.services.async_call(DOMAIN, "set_dampening", {DAMP_FACTOR: ",".join(["0.4"] * 24)}, blocking=True)
        await hass.async_block_till_done()
        assert legacy.options["damp00"] == 0.4
        assert west.options["damp00"] == 1.0
        assert legacy_api.damp["0"] == 0.4
        assert west_api.damp["0"] == 1.0

        await hass.services.async_call(
            DOMAIN,
            "set_dampening",
            {DAMP_FACTOR: ",".join(["0.2"] * 24), ATTR_DEVICE_ID: west_device.id},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert west.options["damp00"] == 0.2
        assert legacy.options["damp00"] == 0.4
        assert legacy_api.damp["0"] == 0.4
        assert west_api.damp["0"] == 0.2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_shared_rooftop_is_logged(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same Solcast rooftop in a second entry is logged and does not raise."""

    ost_options = copy.deepcopy(DEFAULT_INPUT1)
    ost_options[CONF_API_KEY] = "1a"
    ost_options[INSTANCE_NAME] = "Ost"

    try:
        await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await async_init_integration(
                hass,
                ost_options,
                unique_id="solcast_ost",
                title="Solcast Ost",
                orphan_hard_limit=False,
            )
        assert "Rooftop 1111-1111-1111-1111 is also counted by Solcast entry Solcast PV Forecast" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_named_entry_through_the_flow(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """A further entry needs a usable, free name that cannot reach the original entry's files."""

    user_input = {CONF_API_KEY: KEY2, API_LIMIT: "10", AUTO_UPDATE: "1"}
    try:
        legacy = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        for name, error in (
            ("", "instance_name_required"),
            ("☀️", "instance_name_invalid"),
            ("Sites", "instance_name_reserved"),
            ("Usage 2", "instance_name_reserved"),
            ("Undampened", "instance_name_reserved"),
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input | {INSTANCE_NAME: name})
            assert result.get("errors") == {INSTANCE_NAME: error}, name

        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input | {INSTANCE_NAME: "東屋根"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Solcast 東屋根"
        await hass.async_block_till_done()
        named = result["result"]
        assert named.state is ConfigEntryState.LOADED
        assert named.options[INSTANCE_SLUG] == "dongwugen"
        assert named.unique_id == "dongwugen"
        assert named.runtime_data.coordinator.solcast.filename.endswith("solcast-dongwugen.json")

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input | {CONF_API_KEY: "3", INSTANCE_NAME: "東屋根 "}
        )
        assert result.get("errors") == {INSTANCE_NAME: "instance_name_duplicate"}

        # Reloading the original runs its orphan clean-up; the named entry's files must survive it.
        config_dir = Path(legacy.runtime_data.coordinator.solcast.config_dir)
        named_files = sorted(path.name for path in config_dir.glob("solcast-dongwugen*.json"))
        assert "solcast-dongwugen-sites.json" in named_files
        assert "solcast-dongwugen-usage.json" in named_files
        await hass.config_entries.async_reload(legacy.entry_id)
        await hass.async_block_till_done()
        assert sorted(path.name for path in config_dir.glob("solcast-dongwugen*.json")) == named_files
        assert (config_dir / "solcast-sites.json").is_file()
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


def _west_options() -> dict[str, Any]:
    """Options of a named entry with its own key."""

    options = copy.deepcopy(DEFAULT_INPUT1)
    options[CONF_API_KEY] = KEY2
    options[INSTANCE_NAME] = "West"
    return options


async def test_crash_state_and_logs_per_entry(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A named entry keeps its crash record in its own store, and its log lines carry its name."""

    try:
        legacy = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        assert "[West] Writing sites cache for ******2" in caplog.text
        assert "[West] Writing sites cache for ******1" not in caplog.text
        assert any(record.getMessage() == "Writing sites cache for ******1" for record in caplog.records)
        assert f"solcast_solar.state.{west.entry_id}" in hass_storage
        assert "solcast_solar.state" in hass_storage

        # West crashed with a fatal error: it stays down, the original entry still starts.
        west_store = await state.async_get(hass, west.entry_id)
        west_store.state.presumed_dead = True
        west_store.state.crash_time = dt_util.utcnow()
        west_store.state.exception_class = ConfigEntryError
        await west_store.async_save()
        await hass.config_entries.async_reload(west.entry_id)
        await hass.config_entries.async_reload(legacy.entry_id)
        await hass.async_block_till_done()
        assert west.state is ConfigEntryState.SETUP_ERROR
        assert legacy.state is ConfigEntryState.LOADED
        assert (await state.async_get(hass, legacy.entry_id)).state.presumed_dead is False
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


@pytest.mark.parametrize("original_name", [pytest.param("", id="original_unnamed"), pytest.param("Süd-&Westdach", id="original_named")])
async def test_accuracy_follows_each_entry_sensor(
    recorder_mock: Recorder, hass: HomeAssistant, entity_registry: er.EntityRegistry, original_name: str
) -> None:
    """Accuracy is calculated for an entry only when its own accuracy sensor is enabled."""

    original_options = copy.deepcopy(DEFAULT_INPUT1) | ({INSTANCE_NAME: original_name} if original_name else {})
    try:
        original = await async_init_integration(hass, original_options)
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        registry = entity_registry
        prefix = "suedwestdach_" if original_name else ""
        original_accuracy = registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}accuracy")
        west_accuracy = registry.async_get_entity_id("sensor", DOMAIN, "west_accuracy")
        assert original_accuracy is not None
        assert west_accuracy is not None
        registry.async_update_entity(west_accuracy, disabled_by=None)

        calculated: list[str] = []

        async def _record(updater: Updater) -> None:
            calculated.append(updater._coordinator.entry.entry_id)  # pyright: ignore[reportPrivateUsage]

        with patch.object(Updater, "calculate_accuracy_metrics", _record):
            for entry in (original, west):
                await entry.runtime_data.coordinator.updater.update_estimated_actuals_history()
            assert calculated == [west.entry_id]

            registry.async_update_entity(original_accuracy, disabled_by=None)
            calculated.clear()
            await original.runtime_data.coordinator.updater.update_estimated_actuals_history()
            assert calculated == [original.entry_id]
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_named_original_runs_automated_dampening(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An entry that carries a name models automated dampening and keeps the factors in its own file."""

    options = copy.deepcopy(DEFAULT_INPUT1)
    options[INSTANCE_NAME] = "Süd-&Westdach"
    options[AUTO_DAMPEN] = True
    options[GENERATION_ENTITIES] = [
        "sensor.solar_export_sensor_1111_1111_1111_1111",
        "sensor.solar_export_sensor_2222_2222_2222_2222",
    ]
    try:
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES_WATT_HOUR)
        caplog.clear()
        await reload_integration(hass, entry)
        await wait_for_it(hass, caplog, freezer, "Task dampening model_automated took")

        config_dir = get_config_dir(hass.config.config_dir)
        assert (config_dir / "solcast-suedwestdach-dampening.json").is_file()
        assert (config_dir / "solcast-suedwestdach-generation.json").is_file()
        assert not (config_dir / "solcast-dampening.json").exists()
        assert entry.options[SITE_DAMP] is True
        assert len(entry.runtime_data.coordinator.solcast.dampening.factors["all"]) == 48
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_repairs_stay_with_their_entry(recorder_mock: Recorder, hass: HomeAssistant, issue_registry: ir.IssueRegistry) -> None:
    """A named entry clears only its own repairs, and its fixable repair keeps the fix flow."""

    try:
        legacy = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        azimuth = {"site": "x", "proposal": "1", "extant": "2", "latitude": "3", "learn_more": ""}
        for entry in (legacy, west):
            for issue, extra in ((ISSUE_RECORDS_MISSING, {}), (ISSUE_UNUSUAL_AZIMUTH_NORTHERN, azimuth)):
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    repair_issue_id(issue, entry),
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=issue,
                    translation_placeholders=repair_placeholders(entry, extra),
                )

        west_api = west.runtime_data.coordinator.solcast
        await west_api.check_data_records()
        await west_api.sites_cache.cleanup_issues(any_unusual=False)
        for issue in (ISSUE_RECORDS_MISSING, ISSUE_UNUSUAL_AZIMUTH_NORTHERN):
            assert issue_registry.async_get_issue(DOMAIN, issue) is not None, issue
            assert issue_registry.async_get_issue(DOMAIN, f"{issue}_{west.entry_id}") is None, issue

        fixable = repair_issue_id(ISSUE_RECORDS_MISSING_FIXABLE, west)
        ir.async_create_issue(
            hass,
            DOMAIN,
            fixable,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RECORDS_MISSING_FIXABLE,
            translation_placeholders=repair_placeholders(west),
            data={ENTRY_ID: west.entry_id},
        )
        flow = await async_create_fix_flow(hass, fixable, {ENTRY_ID: west.entry_id})
        assert isinstance(flow, RecordsMissingRepairFlow)
        flow.hass = hass
        flow.issue_id = fixable
        result = await flow.async_step_init()
        assert result["step_id"] == "offer_auto"
        result = await flow.async_step_offer_auto({AUTO_UPDATE: "2"})
        assert result["type"] is FlowResultType.ABORT
        assert west.options[AUTO_UPDATE] == 2
        assert legacy.options[AUTO_UPDATE] != 2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def _api_key_for(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Call get_options with the given entry selection and return the API key of the entry that answered."""

    response = await hass.services.async_call(DOMAIN, SERVICE_GET_OPTIONS, data, blocking=True, return_response=True)
    assert response is not None
    return response["data"][CONF_API_KEY]


async def test_action_routing(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """Actions reach the entry named by config_entry_id or by any target kind, and never guess between named entries."""

    ost_options = _west_options() | {CONF_API_KEY: KEY1, INSTANCE_NAME: "Ost"}
    try:
        ost = await async_init_integration(hass, ost_options, unique_id="solcast_ost", title="Solcast Ost", orphan_hard_limit=False)
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        west_device = _device(hass, west.entry_id)
        ost_device = _device(hass, ost.entry_id)
        roof = area_registry.async_create("Roof")
        label = label_registry.async_create("PV")
        device_registry.async_update_device(west_device.id, area_id=roof.id)
        device_registry.async_update_device(ost_device.id, labels={label.label_id})
        west_sensor = entity_registry.async_get_entity_id("sensor", DOMAIN, "west_total_kwh_forecast_today")

        assert await _api_key_for(hass, {CONFIG_ENTRY_ID: west.entry_id}) == KEY2
        assert await _api_key_for(hass, {CONFIG_ENTRY_ID: ost.entry_id}) == KEY1
        assert await _api_key_for(hass, {ATTR_AREA_ID: roof.id}) == KEY2
        assert await _api_key_for(hass, {ATTR_LABEL_ID: label.label_id}) == KEY1
        assert await _api_key_for(hass, {ATTR_ENTITY_ID: [west_sensor]}) == KEY2
        assert await _api_key_for(hass, {ATTR_DEVICE_ID: ost_device.id}) == KEY1

        for data, error in (
            ({}, EXCEPTION_INSTANCE_REQUIRED),
            ({ATTR_DEVICE_ID: [west_device.id, ost_device.id]}, EXCEPTION_INSTANCE_REQUIRED),
            ({ATTR_AREA_ID: area_registry.async_create("Garage").id}, EXCEPTION_INSTANCE_UNKNOWN),
            ({CONFIG_ENTRY_ID: "not_an_entry"}, EXCEPTION_INSTANCE_UNKNOWN),
        ):
            with pytest.raises(ServiceValidationError) as raised:
                await hass.services.async_call(DOMAIN, SERVICE_GET_OPTIONS, data, blocking=True, return_response=True)
            assert raised.value.translation_key == error, data

        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: west.entry_id}, blocking=True)
        assert raised.value.translation_key == EXCEPTION_SET_OPTIONS_EMPTY

        # With one entry left, an action without field or target goes to it.
        assert await hass.config_entries.async_unload(ost.entry_id)
        assert await _api_key_for(hass, {}) == KEY2

        # Home Assistant before 2026.1 takes hass as the first argument of the target helper.
        async def _old_helper(hass: HomeAssistant, call: Any, expand_group: bool = True) -> set[str]:
            return {west.entry_id}

        with patch.object(service, "async_extract_config_entry_ids", _old_helper):
            assert await _api_key_for(hass, {ATTR_AREA_ID: roof.id}) == KEY2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_rooftop_counted_once(
    recorder_mock: Recorder, hass: HomeAssistant, entity_registry: er.EntityRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Each rooftop is counted by one entry only, also against an entry that is not loaded; entries may share an API key."""

    user_input = {CONF_API_KEY: KEY1, API_LIMIT: "10", AUTO_UPDATE: "1", INSTANCE_NAME: "Zwei"}
    try:
        original = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
        assert result.get("errors") == {"base": EXCEPTION_ROOFTOP_IN_USE}
        assert result.get("description_placeholders") == {
            "rooftops": "1111-1111-1111-1111, 2222-2222-2222-2222",
            "entries": original.title,
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input | {CONF_API_KEY: "1a"})
        assert result.get("errors") == {"base": EXCEPTION_ROOFTOP_IN_USE}

        # An entry that shares the key loads, and counts the rooftop the original excludes.
        hass.config_entries.async_update_entry(original, options={**original.options, EXCLUDE_SITES: ["2222-2222-2222-2222"]})
        await hass.async_block_till_done()
        zwei_options = copy.deepcopy(DEFAULT_INPUT1) | {INSTANCE_NAME: "Zwei", EXCLUDE_SITES: ["1111-1111-1111-1111"]}
        zwei = await async_init_integration(hass, zwei_options, unique_id="solcast_zwei", title="Solcast Zwei", orphan_hard_limit=False)
        assert zwei.state is ConfigEntryState.LOADED

        # After a restart each shared rooftop's sensor belongs to the entry that counts it.
        caplog.clear()
        await hass.config_entries.async_reload(original.entry_id)
        await hass.config_entries.async_reload(zwei.entry_id)
        await hass.async_block_till_done()
        assert "does not generate unique IDs" not in caplog.text
        assert "is also counted by" not in caplog.text
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, "solcast_solcast_api_2222-2222-2222-2222") in {
            entity.entity_id for entity in er.async_entries_for_config_entry(entity_registry, zwei.entry_id)
        }

        # Counting the first rooftop in both entries is refused; unchanged settings still save.
        flow = SolcastSolarOptionFlowHandler(zwei)
        flow.hass = hass
        form = {**zwei.options, SITE_EXPORT_ENTITY: []}
        result = await flow.async_step_init({**form, EXCLUDE_SITES: []})
        assert result.get("errors") == {"base": EXCEPTION_ROOFTOP_IN_USE}
        result = await flow.async_step_init(form)
        assert result.get("reason") == AFFIRMATION_UNCHANGED

        # A new key whose rooftops other entries count is refused by every flow.
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        for api_key, error in ((KEY1, EXCEPTION_ROOFTOP_IN_USE), ("1a", EXCEPTION_ROOFTOP_IN_USE)):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": west.entry_id}, data=west.data
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_API_KEY: api_key, API_LIMIT: "10", AUTO_UPDATE: "1"}
            )
            assert result.get("errors") == {"base": error}, api_key
            result = await west.start_reauth_flow(hass)
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: api_key})
            assert result.get("errors") == {"base": error}, api_key
            flow = SolcastSolarOptionFlowHandler(west)
            flow.hass = hass
            result = await flow.async_step_init({**west.options, SITE_EXPORT_ENTITY: [], CONF_API_KEY: api_key})
            assert result.get("errors") == {"base": error}, api_key
        assert west.options[CONF_API_KEY] == KEY2

        # The set_options action refuses the same, and a key Solcast refuses or cannot check now.
        for entry, change, error in (
            (zwei, {EXCLUDE_SITES: "2222-2222-2222-2222"}, EXCEPTION_ROOFTOP_IN_USE),
            (west, {CONF_API_KEY: "1a"}, EXCEPTION_ROOFTOP_IN_USE),
            (west, {CONF_API_KEY: KEY1}, EXCEPTION_ROOFTOP_IN_USE),
            (west, {CONF_API_KEY: "unknown"}, EXCEPTION_API_ERROR),
        ):
            with pytest.raises(ServiceValidationError) as raised:
                await hass.services.async_call(DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: entry.entry_id, **change}, blocking=True)
            assert raised.value.translation_key == error, change
        assert raised.value.translation_placeholders is not None
        assert "403" in raised.value.translation_placeholders["error_detail"]
        session_set(MOCK_BUSY)
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: west.entry_id, CONF_API_KEY: "3"}, blocking=True)
        assert raised.value.translation_key == EXCEPTION_API_ERROR
        session_clear(MOCK_BUSY)
        assert zwei.options[EXCLUDE_SITES] == ["1111-1111-1111-1111"]
        assert west.options[CONF_API_KEY] == KEY2

        # An entry that is not loaded still counts the rooftops of its sensors.
        await hass.config_entries.async_unload(original.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(DOMAIN, SERVICE_SET_OPTIONS, {CONFIG_ENTRY_ID: west.entry_id, CONF_API_KEY: "1a"}, blocking=True)
        assert raised.value.translation_key == EXCEPTION_ROOFTOP_IN_USE
        assert raised.value.translation_placeholders == {"rooftops": "1111-1111-1111-1111", "entries": original.title}
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_entries_together_and_apart(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Entries set up at the same time stay apart; unloading or reloading one leaves the others intact."""

    original_options = copy.deepcopy(DEFAULT_INPUT1) | {EXCLUDE_SITES: ["2222-2222-2222-2222"]}
    zwei_options = copy.deepcopy(DEFAULT_INPUT1) | {INSTANCE_NAME: "Zwei", EXCLUDE_SITES: ["1111-1111-1111-1111"]}
    try:
        original = await async_init_integration(hass, original_options)
        zwei = MockConfigEntry(domain=DOMAIN, title="Solcast Zwei", data=zwei_options, options=zwei_options)
        west = MockConfigEntry(domain=DOMAIN, title="Solcast West", data=_west_options(), options=_west_options())
        zwei.add_to_hass(hass)
        west.add_to_hass(hass)
        await asyncio.gather(hass.config_entries.async_setup(zwei.entry_id), hass.config_entries.async_setup(west.entry_id))
        await hass.async_block_till_done()
        assert [entry.state for entry in (original, zwei, west)] == [ConfigEntryState.LOADED] * 3

        # The original and Zwei share one key but count different rooftops, so their Energy forecasts differ.
        assert await async_get_solar_forecast(hass, original.entry_id) != await async_get_solar_forecast(hass, zwei.entry_id)
        diagnostics = await async_get_config_entry_diagnostics(hass, west)
        assert set(diagnostics["data"][SITE_INFO]) == {"3333-3333-3333-3333"}
        assert CONF_API_KEY not in diagnostics["health_check"]

        config_dir = Path(original.runtime_data.coordinator.solcast.config_dir)

        # A zero-length cache file is removed by the entry it belongs to only.
        empty = config_dir / "solcast-west-generation.json"
        empty.write_text("", encoding="utf-8")
        await hass.config_entries.async_reload(original.entry_id)
        await hass.async_block_till_done()
        assert empty.exists()

        def _others() -> dict[str, int]:
            return {
                path.name: path.stat().st_mtime_ns for path in config_dir.glob("solcast*.json") if not path.name.startswith("solcast-west")
            }

        before, original_store = _others(), copy.deepcopy(hass_storage["solcast_solar.state"])
        await hass.config_entries.async_reload(west.entry_id)
        assert await hass.config_entries.async_unload(zwei.entry_id)
        await hass.async_block_till_done()
        assert west.state is ConfigEntryState.LOADED
        assert original.state is ConfigEntryState.LOADED
        assert not empty.exists()
        assert _others() == before
        assert hass_storage["solcast_solar.state"] == original_store
        assert await _api_key_for(hass, {}) == KEY1
        assert await _api_key_for(hass, {CONFIG_ENTRY_ID: west.entry_id}) == KEY2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_removing_a_named_entry_cleans_only_its_own(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    issue_registry: ir.IssueRegistry,
) -> None:
    """Deleting a named entry removes its files, store and repairs; the original entry keeps everything."""

    try:
        original = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        for entry in (original, west):
            ir.async_create_issue(
                hass,
                DOMAIN,
                repair_issue_id(ISSUE_RECORDS_MISSING, entry),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_RECORDS_MISSING,
                translation_placeholders=repair_placeholders(entry),
            )

        # A title the user set survives an options change.
        hass.config_entries.async_update_entry(west, title="Mein Westdach")
        flow = SolcastSolarOptionFlowHandler(west)
        flow.hass = hass
        result = await flow.async_step_init({**west.options, SITE_EXPORT_ENTITY: [], CUSTOM_HOURS: 3})
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()
        assert west.title == "Mein Westdach"

        config_dir = get_config_dir(hass.config.config_dir)

        def _files(prefix: str) -> list[str]:
            return sorted(path.name for path in config_dir.glob("solcast*") if path.name.startswith(prefix))

        original_files = [name for name in _files("solcast") if not name.startswith("solcast-west")]
        assert _files("solcast-west")
        assert f"solcast_solar.state.{west.entry_id}" in hass_storage

        await hass.config_entries.async_remove(west.entry_id)
        await hass.async_block_till_done()
        assert _files("solcast-west") == []
        assert _files("solcast") == original_files
        assert f"solcast_solar.state.{west.entry_id}" not in hass_storage
        assert "solcast_solar.state" in hass_storage
        assert issue_registry.async_get_issue(DOMAIN, f"{ISSUE_RECORDS_MISSING}_{west.entry_id}") is None
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_RECORDS_MISSING) is not None

        # An entry named before reserved names were refused shares the original's file names: nothing is deleted.
        sites = MockConfigEntry(domain=DOMAIN, title="Solcast Sites", options={**DEFAULT_INPUT1, INSTANCE_NAME: "Sites"})
        sites.add_to_hass(hass)
        await hass.config_entries.async_remove(sites.entry_id)
        assert _files("solcast") == original_files
        assert original.state is ConfigEntryState.LOADED
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_removing_an_unloaded_named_entry_deletes_its_repairs(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A repair raised for a named entry while it is not loaded is deleted with the entry, and the original's stays."""

    try:
        original = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        assert await hass.config_entries.async_unload(west.entry_id)
        await hass.async_block_till_done()
        for entry in (original, west):
            ir.async_create_issue(
                hass,
                DOMAIN,
                repair_issue_id(ISSUE_RECORDS_MISSING, entry),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_RECORDS_MISSING,
                translation_placeholders=repair_placeholders(entry),
            )

        await hass.config_entries.async_remove(west.entry_id)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, repair_issue_id(ISSUE_RECORDS_MISSING, west)) is None
        assert issue_registry.async_get_issue(DOMAIN, repair_issue_id(ISSUE_RECORDS_MISSING, original)) is not None

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_flows_for_one_name_at_a_time(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """A second flow for a name that an open flow holds aborts, so two entries cannot get one name."""

    user_input = {API_LIMIT: "10", AUTO_UPDATE: "1", INSTANCE_NAME: "Nord"}
    try:
        await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        first = await hass.config_entries.flow.async_configure(first["flow_id"], user_input | {CONF_API_KEY: "unknown"})
        assert first.get("errors") == {"base": "api_error"}
        second = await hass.config_entries.flow.async_configure(second["flow_id"], user_input | {CONF_API_KEY: KEY2})
        assert second["type"] is FlowResultType.ABORT
        assert second["reason"] == "already_in_progress"
        first = await hass.config_entries.flow.async_configure(first["flow_id"], user_input | {CONF_API_KEY: KEY2})
        assert first["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_old_repairs_get_the_entry_name(recorder_mock: Recorder, hass: HomeAssistant, issue_registry: ir.IssueRegistry) -> None:
    """A repair raised before titles named the entry is raised again at setup, by its own entry only."""

    def old_issue(domain: str, issue_id: str, files: str) -> None:
        # Straight into the registry, as an older version left it; the test harness refuses the missing placeholder.
        issue_registry.issues[(domain, issue_id)] = ir.IssueEntry(
            active=True,
            breaks_in_ha_version=None,
            created=dt_util.utcnow(),
            data=None,
            dismissed_version=None,
            domain=domain,
            is_fixable=False,
            is_persistent=True,
            issue_domain=None,
            issue_id=issue_id,
            learn_more_url=None,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CORRUPT_FILE,
            translation_placeholders={"files": files},
        )

    try:
        legacy = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        west = await async_init_integration(hass, _west_options(), unique_id="solcast_west", title="Solcast West", orphan_hard_limit=False)
        west_issue = f"{ISSUE_CORRUPT_FILE}_{west.entry_id}"
        old_issue(DOMAIN, ISSUE_CORRUPT_FILE, "solcast.json")
        old_issue(DOMAIN, west_issue, "solcast-west.json")
        old_issue("other", ISSUE_CORRUPT_FILE, "other.json")

        await hass.config_entries.async_reload(legacy.entry_id)
        await hass.async_block_till_done()
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_CORRUPT_FILE).translation_placeholders == {
            "instance": "",
            "files": "solcast.json",
        }
        assert issue_registry.async_get_issue(DOMAIN, west_issue).translation_placeholders == {"files": "solcast-west.json"}
        assert issue_registry.async_get_issue("other", ISSUE_CORRUPT_FILE).translation_placeholders == {"files": "other.json"}

        await hass.config_entries.async_reload(west.entry_id)
        await hass.async_block_till_done()
        issue = issue_registry.async_get_issue(DOMAIN, west_issue)
        assert issue.translation_placeholders == {"instance": " (Solcast West)", "files": "solcast-west.json"}
        assert issue.is_persistent
        assert issue.severity is ir.IssueSeverity.ERROR
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_options_keep_title_and_exclusions(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Saving options keeps a title the user set, and a form for an entry that is not loaded offers only its exclusions."""

    options = copy.deepcopy(DEFAULT_INPUT1) | {EXCLUDE_SITES: ["2222-2222-2222-2222"]}
    try:
        entry = await async_init_integration(hass, options, title="Mein Solcast")
        flow = SolcastSolarOptionFlowHandler(entry)
        flow.hass = hass
        result = await flow.async_step_init({**entry.options, SITE_EXPORT_ENTITY: [], CUSTOM_HOURS: 3})
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()
        assert entry.title == "Mein Solcast"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        flow = SolcastSolarOptionFlowHandler(entry)
        flow.hass = hass
        result = await flow.async_step_init()
        selector = next(value for key, value in result["data_schema"].schema.items() if key == EXCLUDE_SITES)  # type: ignore[union-attr]
        assert [option["value"] for option in selector.config["options"]] == ["2222-2222-2222-2222"]
        result = await flow.async_step_init({**entry.options, SITE_EXPORT_ENTITY: [], EXCLUDE_SITES: [""]})
        assert result.get("reason") == AFFIRMATION_RECONFIGURED
        await hass.async_block_till_done()
        assert entry.options[EXCLUDE_SITES] == []
        assert entry.title == "Mein Solcast"
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
