"""Two Solcast entries must stay on their own files, sensors and devices."""

import copy
import logging

import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import DAMP_FACTOR, DOMAIN, INSTANCE_NAME, RESOURCE_ID
from homeassistant.const import ATTR_DEVICE_ID, CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import (
    DEFAULT_INPUT1,
    KEY1,
    KEY2,
    async_cleanup_integration_tests,
    async_init_integration,
    no_error_or_exception,
)


def _device(hass: HomeAssistant, entry_id: str):
    """Return the single device created for one config entry."""

    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    assert len(device) == 1
    return device[0]


async def test_two_instances_stay_separate(
    recorder_mock: Recorder,
    hass: HomeAssistant,
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

        registry = er.async_get(hass)
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
        assert "1111-1111-1111-1111" in caplog.text
        assert "already used by Solcast entry Solcast PV Forecast" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
