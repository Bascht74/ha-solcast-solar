"""Set-up with automated dampening while a fetched site has no estimated actuals yet, and the tasks around it."""

import copy
from datetime import timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    AUTO_DAMPEN,
    CONFIG_VERSION,
    DOMAIN,
    EXCLUDE_SITES,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    INSTANCE_NAME,
    LAST_UPDATED,
    SITE_INFO,
    TASK_NEW_DAY_GENERATION,
    USE_ACTUALS,
    VERSION,
)
from homeassistant.components.solcast_solar.dampen import Dampening
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from . import (
    DEFAULT_INPUT1,
    MOCK_OVER_LIMIT,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    reload_integration,
    session_clear,
    session_set,
)

from tests.common import MockConfigEntry

SITE1 = "1111-1111-1111-1111"
SITE2 = "2222-2222-2222-2222"
GENERATION1 = "sensor.solar_export_sensor_1111_1111_1111_1111"
GENERATION2 = "sensor.solar_export_sensor_2222_2222_2222_2222"


def _options(**changes: Any) -> dict[str, Any]:
    """Key 1 with its two sites, estimated actuals and automated dampening from the first generation sensor."""

    return (
        copy.deepcopy(DEFAULT_INPUT1) | {GET_ACTUALS: True, USE_ACTUALS: 1, AUTO_DAMPEN: True, GENERATION_ENTITIES: [GENERATION1]} | changes
    )


def _no_setup_error(caplog: pytest.LogCaptureFixture, entry: ConfigEntry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    assert "Error setting up entry" not in caplog.text
    assert "KeyError" not in caplog.text
    assert "Unable to get sensor value" not in caplog.text


async def _unlink(entry: ConfigEntry, *kinds: str) -> None:
    solcast = entry.runtime_data.coordinator.solcast
    files = {
        "actuals": solcast.filename_actuals,
        "actuals_dampened": solcast.filename_actuals_dampened,
        "generation": solcast.filename_generation,
    }
    for kind in kinds:
        Path(files[kind]).unlink(missing_ok=True)


def _loads_with_generation(caplog: pytest.LogCaptureFixture, entry: ConfigEntry) -> None:
    """The entry loaded and read its generation from the recorder, although a site lacks estimated actuals."""

    _no_setup_error(caplog, entry)
    assert entry.runtime_data.coordinator.solcast.dampening.data_generation[GENERATION]


async def test_actuals_and_auto_dampening_switched_on_together(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Estimated actuals and automated dampening switched on in one save: generation is read before the first actuals."""

    try:
        entry = await async_init_integration(
            hass,
            _options(**{GET_ACTUALS: False, USE_ACTUALS: 0, AUTO_DAMPEN: False, GENERATION_ENTITIES: []}),
            extra_sensors=ExtraSensors.YES,
        )
        await _unlink(entry, "actuals", "actuals_dampened", "generation")
        caplog.clear()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, GET_ACTUALS: True, USE_ACTUALS: 1, AUTO_DAMPEN: True, GENERATION_ENTITIES: [GENERATION1]}
        )
        await hass.async_block_till_done()
        _loads_with_generation(caplog, entry)
        assert "Auto-dampening suppressed: No estimated actuals yet" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_site_included_while_generation_entities_change(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A site included again in the save that changes the generation entities: generation is read before it is fetched."""

    try:
        entry = await async_init_integration(hass, _options(**{EXCLUDE_SITES: [SITE2]}), extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)
        assert SITE2 not in entry.runtime_data.coordinator.solcast.data_actuals[SITE_INFO]
        caplog.clear()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, EXCLUDE_SITES: [], GENERATION_ENTITIES: [GENERATION1, GENERATION2]}
        )
        await hass.async_block_till_done()
        _loads_with_generation(caplog, entry)
        # The included site is fetched with its past week while loading, so its estimated actuals are there to model.
        assert SITE2 in entry.runtime_data.coordinator.solcast.data_actuals[SITE_INFO]
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_and_generation_files_missing(recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """A start without the estimated actuals and generation files reads generation before the actuals are fetched again."""

    try:
        entry = await async_init_integration(hass, _options(), extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)
        await _unlink(entry, "actuals", "actuals_dampened", "generation")
        caplog.clear()
        await reload_integration(hass, entry)
        _loads_with_generation(caplog, entry)
        assert "Auto-dampening suppressed: No estimated actuals yet" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_restart_before_the_first_estimated_actuals(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A new entry whose first fetch Solcast refused restarts after its first forecast and before its first estimated actuals.

    While nothing is fetched yet its power sensors are unavailable and log no error, and the restart loads.
    """

    try:
        await async_init_integration(hass, _options(**{EXCLUDE_SITES: [SITE2]}), extra_sensors=ExtraSensors.YES)
        west_options = _options(**{INSTANCE_NAME: "West", EXCLUDE_SITES: [SITE1], GENERATION_ENTITIES: [GENERATION2]})
        west = MockConfigEntry(domain=DOMAIN, title="Solcast West", data=west_options, options=west_options, version=CONFIG_VERSION)
        west.add_to_hass(hass)

        session_set(MOCK_OVER_LIMIT)
        caplog.clear()
        assert await hass.config_entries.async_setup(west.entry_id)
        await hass.async_block_till_done()
        solcast = west.runtime_data.coordinator.solcast
        assert "There is no solcast.json to load" in caplog.text
        assert not Path(solcast.filename).exists()
        await west.runtime_data.coordinator.update_integration_listeners()
        power_now = hass.states.get("sensor.solcast_west_power_now")
        assert power_now is not None
        assert power_now.state == "unavailable"
        _no_setup_error(caplog, west)

        # After UTC midnight the fetch succeeds. Its first forecast is saved by a scheduled update, which does not
        # fetch estimated actuals, so the files are as if that update ran and the estimated actuals are still to come.
        session_clear(MOCK_OVER_LIMIT)
        await solcast.sites_cache.reset_usage_cache()
        await reload_integration(hass, west)
        await _unlink(west, "actuals", "actuals_dampened", "generation")
        assert Path(west.runtime_data.coordinator.solcast.filename).exists()

        # Restarted before its first estimated actuals, it loads, and reads its generation for automated dampening.
        caplog.clear()
        await reload_integration(hass, west)
        _loads_with_generation(caplog, west)
    finally:
        session_clear(MOCK_OVER_LIMIT)
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_gap_check_while_a_site_has_no_estimated_actuals(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The one-time check of a generation cache from before gaps were checked runs while a site lacks estimated actuals."""

    try:
        entry = await async_init_integration(hass, _options(), extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)
        solcast = entry.runtime_data.coordinator.solcast
        generation_file = Path(solcast.filename_generation)
        cached = json.loads(generation_file.read_text(encoding="utf-8"))
        assert cached[GENERATION]
        cached.pop(VERSION)
        generation_file.write_text(json.dumps(cached), encoding="utf-8")
        actuals_file = Path(solcast.filename_actuals)
        actuals = json.loads(actuals_file.read_text(encoding="utf-8"))
        actuals[SITE_INFO].pop(SITE2)
        actuals_file.write_text(json.dumps(actuals), encoding="utf-8")

        caplog.clear()
        await reload_integration(hass, entry)
        _no_setup_error(caplog, entry)
        assert json.loads(generation_file.read_text(encoding="utf-8"))[VERSION] == 2
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


@pytest.mark.parametrize(
    ("task", "logged"),
    [
        ("get_pv_generation", "Loading generation failed, continuing without it"),
        ("recheck_generation_gaps", "Check of cached generation failed, continuing without it"),
    ],
)
async def test_generation_failure_does_not_stop_the_setup(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, task: str, logged: str
) -> None:
    """Reading generation while loading is logged when it fails, and the entry loads without it."""

    try:
        entry = await async_init_integration(hass, _options(), extra_sensors=ExtraSensors.YES)
        await reload_integration(hass, entry)
        solcast = entry.runtime_data.coordinator.solcast
        generation_file = Path(solcast.filename_generation)
        if task == "get_pv_generation":
            generation_file.unlink()
        else:
            cached = json.loads(generation_file.read_text(encoding="utf-8"))
            cached.pop(VERSION)
            generation_file.write_text(json.dumps(cached), encoding="utf-8")

        async def fail(_self: Dampening) -> None:
            raise RuntimeError("recorder gone")

        monkeypatch.setattr(Dampening, task, fail)
        caplog.clear()
        await reload_integration(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert logged in caplog.text
        assert "RuntimeError: recorder gone" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_failed_generation_update_is_scheduled_again(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed daily generation update is logged and leaves no task behind, so the next day schedules it again."""

    try:
        entry = await async_init_integration(hass, _options(), extra_sensors=ExtraSensors.YES)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast

        async def fail(_self: Dampening) -> None:
            raise RuntimeError("recorder gone")

        monkeypatch.setattr(Dampening, "get_pv_generation", fail)
        solcast.data_actuals[LAST_UPDATED] -= timedelta(days=1)  # Today's estimated actuals and generation are still due
        coordinator.tasks.pop(TASK_NEW_DAY_GENERATION, None)
        assert await coordinator.updater.check_generation_fetch()
        scheduled = coordinator.tasks[TASK_NEW_DAY_GENERATION]

        caplog.clear()
        await coordinator.updater._generation()  # pyright: ignore[reportPrivateUsage]
        assert "Update generation data failed" in caplog.text
        assert TASK_NEW_DAY_GENERATION not in coordinator.tasks
        scheduled()  # The fired timer, as the job leaves it

        assert await coordinator.updater.check_generation_fetch()
        assert coordinator.tasks[TASK_NEW_DAY_GENERATION] is not scheduled
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
