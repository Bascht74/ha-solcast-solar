"""The calls kept for estimated actuals per UTC day, usage files of 4.7.0.2, and API usage of keys that fetch nothing."""

from collections.abc import Generator
import copy
import dataclasses
from datetime import UTC, datetime as dt, timedelta
from hashlib import md5
import json
from pathlib import Path
from typing import Any

import freezegun
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    API_LIMIT,
    DAILY_ACTUALS_CONSUMED,
    DAILY_LIMIT_CONSUMED,
    DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS,
    EXCLUDE_SITES,
    INTEGRATION_VERSION,
    LAST_UPDATED,
    SUCCESS,
    SUCCESS_ACTUALS,
    SUCCESS_FORCED,
    TASK_NEW_DAY_ACTUALS,
)
from homeassistant.core import HomeAssistant

from . import (
    DEFAULT_INPUT1,
    DEFAULT_INPUT2,
    KEY1,
    KEY2,
    MOCK_SESSION_CONFIG,
    async_cleanup_integration_tests,
    async_init_integration,
)

from tests.common import async_fire_time_changed

SITE2 = "2222-2222-2222-2222"
SITE3 = "3333-3333-3333-3333"


@pytest.fixture(autouse=True)
def frozen_time() -> Generator[FrozenDateTimeFactory]:
    """Freeze at 2026-10-06 00:30 UTC, 02:30 in Berlin, without a time zone offset."""

    with freezegun.freeze_time("2026-10-06 00:30:00", tz_offset=0) as freeze:
        yield freeze  # type: ignore[misc]


def _sued(**changes: Any) -> dict[str, Any]:
    """Like the user's Süd entry: key 1 with the other site excluded, a limit of five and estimated actuals."""

    return copy.deepcopy(DEFAULT_INPUT1) | {API_LIMIT: "5", "api_quota": "5", EXCLUDE_SITES: [SITE2]} | changes


async def _fire(hass: HomeAssistant, freezer: FrozenDateTimeFactory, until: dt) -> None:
    freezer.move_to(until)
    async_fire_time_changed(hass, until)
    await hass.async_block_till_done()


async def _updates(solcast: Any, freezer: FrozenDateTimeFactory, day: dt, hours: tuple[int, ...]) -> list[str]:
    outcomes = []
    for hour in hours:
        freezer.move_to(day.replace(hour=hour))
        outcomes.append((await solcast.fetcher.get_forecast_update()).outcome.name)
    return outcomes


@pytest.mark.parametrize(
    ("zone", "now", "updated_today", "due"),
    [
        pytest.param("Europe/Berlin", dt(2026, 10, 6, 6, 0, tzinfo=UTC), True, 1, id="berlin_day_local_midnight_ahead"),
        pytest.param("Europe/Berlin", dt(2026, 10, 6, 22, 30, tzinfo=UTC), True, 0, id="berlin_after_the_local_midnight_fetch"),
        pytest.param("Europe/Berlin", dt(2026, 10, 6, 0, 30, tzinfo=UTC), False, 2, id="berlin_missed_local_midnight"),
        pytest.param("Europe/London", dt(2026, 3, 29, 6, 0, tzinfo=UTC), True, 1, id="london_clocks_forward"),
        pytest.param("Europe/London", dt(2026, 10, 6, 6, 0, tzinfo=UTC), True, 1, id="london_summer"),
        pytest.param("Europe/London", dt(2026, 12, 6, 6, 0, tzinfo=UTC), True, 0, id="london_winter"),
        pytest.param("Australia/Sydney", dt(2026, 10, 6, 6, 0, tzinfo=UTC), True, 1, id="sydney_before_local_midnight"),
        pytest.param("America/New_York", dt(2026, 10, 6, 6, 0, tzinfo=UTC), True, 0, id="new_york_after_local_midnight"),
    ],
)
async def test_estimated_actuals_fetches_due_before_utc_midnight(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    frozen_time: FrozenDateTimeFactory,
    zone: str,
    now: dt,
    updated_today: bool,
    due: int,
) -> None:
    """Calls are kept for each estimated actuals fetch still due before Solcast's count starts again at UTC midnight."""

    try:
        frozen_time.move_to(now)
        entry = await async_init_integration(hass, _sued(), timezone=zone)
        solcast = entry.runtime_data.coordinator.solcast
        solcast.data_actuals[LAST_UPDATED] = now if updated_today else now - timedelta(days=1)
        assert solcast.estimated_actuals_fetches_due == due
        solcast.api_actuals[KEY1] = 1  # Fetches made today are not taken off what is still due
        assert solcast.api_actuals_reserve(KEY1) == due
        solcast.options = dataclasses.replace(solcast.options, get_actuals=False)
        assert solcast.api_actuals_reserve(KEY1) == 0
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_two_estimated_actuals_fetches_in_one_utc_day(
    recorder_mock: Recorder, hass: HomeAssistant, frozen_time: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Two estimated actuals fetches in one UTC day keep their calls, and a forced one takes from the forecast updates.

    Berlin: Home Assistant was down over local midnight and starts at 02:30 (00:30 UTC). The missed fetch and the one of
    the next local midnight (22:00 UTC) both count this UTC day.
    """

    try:
        MOCK_SESSION_CONFIG["api_limit"] = 5  # Solcast's quota for the key is the entry's limit
        entry = await async_init_integration(hass, _sued(), timezone="Europe/Berlin")
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        # As after the UTC midnight reset, with the estimated actuals of the local day not fetched yet.
        solcast.data_actuals[LAST_UPDATED] -= timedelta(days=1)
        solcast.api_used[KEY1] = solcast.api_actuals[KEY1] = MOCK_SESSION_CONFIG["api_used"][KEY1] = 0
        coordinator.tasks.pop(TASK_NEW_DAY_ACTUALS, None)
        assert solcast.api_actuals_reserve(KEY1) == 2
        assert await coordinator.updater.check_estimated_actuals_fetch()
        await _fire(hass, frozen_time, dt(2026, 10, 6, 0, 32, tzinfo=UTC))
        assert (solcast.api_used[KEY1], solcast.api_actuals_reserve(KEY1)) == (1, 1)

        # A forced fetch of estimated actuals takes a call from the forecast updates, not from the next local midnight.
        frozen_time.move_to(dt(2026, 10, 6, 5, 0, tzinfo=UTC))
        await solcast.fetcher.update_estimated_actuals()
        assert (solcast.api_used[KEY1], solcast.api_actuals_reserve(KEY1)) == (2, 1)

        assert await _updates(solcast, frozen_time, dt(2026, 10, 6, tzinfo=UTC), (6, 9, 12, 15)) == [
            "SUCCESS",
            "SUCCESS",
            "FAILED",
            "FAILED",
        ]
        assert "1 API call(s) kept for the estimated actuals due before UTC midnight" in caplog.text

        caplog.clear()
        frozen_time.move_to(dt(2026, 10, 6, 22, 0, tzinfo=UTC))  # 00:00 in Berlin
        await coordinator._update_integration_listeners()  # pyright: ignore[reportPrivateUsage]
        assert TASK_NEW_DAY_ACTUALS in coordinator.tasks
        await _fire(hass, frozen_time, dt(2026, 10, 6, 22, 16, tzinfo=UTC))
        assert "Update estimated actuals failed" not in caplog.text
        assert solcast.api_used[KEY1] == MOCK_SESSION_CONFIG["api_used"][KEY1] == 5
        assert solcast.api_actuals_reserve(KEY1) == 0
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_clocks_forward_in_london(
    recorder_mock: Recorder, hass: HomeAssistant, frozen_time: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Europe/London on 29 March 2026: local midnight of the 29th is at 00:00 UTC, of the 30th at 23:00 UTC the same day."""

    try:
        MOCK_SESSION_CONFIG["api_limit"] = 5
        frozen_time.move_to("2026-03-28 23:58:00")
        entry = await async_init_integration(hass, _sued(), timezone="Europe/London")
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        frozen_time.move_to(dt(2026, 3, 29, 0, 0, tzinfo=UTC))
        await coordinator._update_utc_midnight_usage_sensor_data()  # pyright: ignore[reportPrivateUsage]
        MOCK_SESSION_CONFIG["api_used"][KEY1] = 0
        await coordinator._update_integration_listeners()  # pyright: ignore[reportPrivateUsage]
        assert TASK_NEW_DAY_ACTUALS in coordinator.tasks
        await _fire(hass, frozen_time, dt(2026, 3, 29, 0, 16, tzinfo=UTC))
        assert (solcast.api_used[KEY1], solcast.api_actuals[KEY1]) == (1, 1)

        outcomes = await _updates(solcast, frozen_time, dt(2026, 3, 29, tzinfo=UTC), (6, 9, 12, 15, 17))
        assert outcomes == ["SUCCESS", "SUCCESS", "SUCCESS", "FAILED", "FAILED"]

        caplog.clear()
        frozen_time.move_to(dt(2026, 3, 29, 23, 0, tzinfo=UTC))
        await coordinator._update_integration_listeners()  # pyright: ignore[reportPrivateUsage]
        assert TASK_NEW_DAY_ACTUALS in coordinator.tasks
        await _fire(hass, frozen_time, dt(2026, 3, 29, 23, 16, tzinfo=UTC))
        assert "Update estimated actuals failed" not in caplog.text
        assert solcast.api_used[KEY1] == MOCK_SESSION_CONFIG["api_used"][KEY1] == 5
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


@pytest.mark.parametrize(
    ("written_by", "expected"),
    [
        pytest.param("4.7.0.2", 3, id="4.7.0.2_counted_them_already"),
        pytest.param("v4.7.0.2", 3, id="v4.7.0.2_from_the_hacs_zip"),
        pytest.param("4.7.0.1", 4, id="4.7.0.1_did_not_count_them"),
    ],
)
async def test_usage_of_4702_counted_once(recorder_mock: Recorder, hass: HomeAssistant, written_by: str, expected: int) -> None:
    """After an update from 4.7.0.2, which counted today's estimated actuals without marking it, they are not added again."""

    try:
        entry = await async_init_integration(hass, _sued())
        solcast = entry.runtime_data.coordinator.solcast
        assert solcast.integration_version == "4.7.0.5"
        assert solcast.headers["User-Agent"] == "ha-solcast-solar-integration/4.7.0.5"

        usage_file = Path(solcast.sites_cache._get_usage_cache_filename(KEY1))  # pyright: ignore[reportPrivateUsage]
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
        usage.pop(DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS)
        usage[DAILY_LIMIT_CONSUMED], usage[DAILY_ACTUALS_CONSUMED] = 3, 1
        usage_file.write_text(json.dumps(usage), encoding="utf-8")
        data_file = Path(solcast.filename)
        data = json.loads(data_file.read_text(encoding="utf-8"))
        assert data[INTEGRATION_VERSION] == "4.7.0.5"
        data[INTEGRATION_VERSION] = written_by
        data_file.write_text(json.dumps(data), encoding="utf-8")

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.coordinator.solcast.api_used[KEY1] == expected
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
        assert (usage[DAILY_LIMIT_CONSUMED], usage[DAILY_LIMIT_CONSUMED_INCLUDES_ACTUALS]) == (expected, True)
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_usage_shown_for_keys_with_fetched_sites(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """API used and the forced and estimated actuals counts leave out a key whose sites are all excluded, like the limit."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2) | {EXCLUDE_SITES: [SITE3]})
        solcast = entry.runtime_data.coordinator.solcast
        assert solcast.api_sites_per_key == {KEY1: 2}
        solcast.api_used[KEY1], solcast.api_used[KEY2] = 4, 9  # Key 2 spent its calls before its site was excluded

        def hashed(api_key: str) -> str:
            return md5(api_key[-6:].encode()).hexdigest()

        solcast.data[SUCCESS][SUCCESS_FORCED] = {hashed(KEY1): 1, hashed(KEY2): 6}
        solcast.data[SUCCESS][SUCCESS_ACTUALS] = {hashed(KEY1): 2, hashed(KEY2): 7}
        assert (solcast.api_used_count, solcast.successes_forced_24h, solcast.successes_actuals_24h) == (4, 1, 2)
        solcast.data[SUCCESS][SUCCESS_FORCED] = {hashed(KEY2): 6}
        assert solcast.successes_forced_24h == 0
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
