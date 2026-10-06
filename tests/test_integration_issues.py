"""Tests for Solcast Solar issue registry behaviors and quota warnings."""

import asyncio
from contextlib import suppress
import copy
from types import SimpleNamespace

import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    ACTUALS_COST,
    API_LIMIT,
    API_USED,
    DOMAIN,
    GET_ACTUALS,
    ISSUE_ACTUALS_QUOTA_TODAY,
    TASK_ACTUALS_FETCH,
    USE_ACTUALS,
)
from homeassistant.components.solcast_solar.coordinator import SolcastUpdateCoordinator
from homeassistant.components.solcast_solar.fetcher import Fetcher
from homeassistant.components.solcast_solar.issues import sync_actuals_quota_risk_issue
from homeassistant.components.solcast_solar.solcastapi import SolcastApi
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import (
    DEFAULT_INPUT1,
    MOCK_OVER_LIMIT,
    async_cleanup_integration_tests,
    async_init_integration,
    session_clear,
    session_set,
)
from .test_integration import patch_solcast_api


@pytest.fixture(autouse=True)
def frozen_time() -> None:
    """Disable the global freezer fixture for this module."""


async def test_pop_task_result_handles_cancelled_task() -> None:
    """Ensure cancelled fetch tasks are popped without raising."""

    api = SimpleNamespace(tasks={})
    fetcher = Fetcher(api=api)  # pyright: ignore[reportArgumentType]

    task = asyncio.create_task(asyncio.sleep(5))
    api.tasks[TASK_ACTUALS_FETCH] = task
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    result = fetcher._pop_task_result(TASK_ACTUALS_FETCH)

    assert result is None
    assert TASK_ACTUALS_FETCH not in api.tasks


def test_pop_task_result_handles_missing_task() -> None:
    """Ensure missing fetch tasks return None without raising."""

    api = SimpleNamespace(tasks={})
    fetcher = Fetcher(api=api)  # pyright: ignore[reportArgumentType]

    assert fetcher._pop_task_result(TASK_ACTUALS_FETCH) is None


async def test_pop_task_result_handles_task_exception() -> None:
    """Ensure failed fetch tasks are popped and converted to None."""

    api = SimpleNamespace(tasks={})
    fetcher = Fetcher(api=api)  # pyright: ignore[reportArgumentType]

    async def _fail() -> None:
        raise RuntimeError("Magic smoke released")

    task = asyncio.create_task(_fail())
    api.tasks[TASK_ACTUALS_FETCH] = task
    with suppress(RuntimeError):
        await task

    result = fetcher._pop_task_result(TASK_ACTUALS_FETCH)

    assert result is None
    assert TASK_ACTUALS_FETCH not in api.tasks


async def test_pop_task_result_keeps_a_newer_task() -> None:
    """Ensure an ending fetch does not pop or read a newer fetch under the same name."""

    api = SimpleNamespace(tasks={})
    fetcher = Fetcher(api=api)  # pyright: ignore[reportArgumentType]

    # The clear data action cancels a fetch and starts a new one before the cancelled fetch ends.
    old = asyncio.create_task(asyncio.sleep(5))
    api.tasks[TASK_ACTUALS_FETCH] = old
    old.cancel()
    with suppress(asyncio.CancelledError):
        await old
    gate = asyncio.Event()

    async def _new_fetch() -> dict[str, str]:
        await gate.wait()
        return {"new": "data"}

    new = asyncio.create_task(_new_fetch())
    api.tasks[TASK_ACTUALS_FETCH] = new

    assert fetcher._pop_task_result(TASK_ACTUALS_FETCH, old) is None
    assert api.tasks[TASK_ACTUALS_FETCH] is new
    assert not new.done()

    gate.set()
    await new
    assert fetcher._pop_task_result(TASK_ACTUALS_FETCH, new) == {"new": "data"}
    assert TASK_ACTUALS_FETCH not in api.tasks


async def test_pop_task_result_cancels_a_running_task() -> None:
    """Ensure a fetch still running when its caller ends is cancelled instead of read."""

    api = SimpleNamespace(tasks={})
    fetcher = Fetcher(api=api)  # pyright: ignore[reportArgumentType]

    task = asyncio.create_task(asyncio.sleep(5))
    api.tasks[TASK_ACTUALS_FETCH] = task

    assert fetcher._pop_task_result(TASK_ACTUALS_FETCH, task) is None
    assert TASK_ACTUALS_FETCH not in api.tasks
    with suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_actuals_quota_today_issue_raised_when_quota_at_risk(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the quota risk issue is raised when typical usage plus actuals cost exceeds the inferred quota."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}]
        api_typical: dict[str, int] = {"key1": 10}
        api_limit = 9

        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, {}, {}, api_limit, get_actuals=True)
        issue = issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)
        assert issue is not None, "Issue ISSUE_ACTUALS_QUOTA_TODAY should exist"
        assert issue.is_persistent is False, "Issue should not be persistent"
        assert issue.translation_placeholders is not None, "Issue should have translation placeholders"
        assert issue.translation_placeholders[API_USED] == "10"
        assert issue.translation_placeholders[API_LIMIT] == "10"
        assert issue.translation_placeholders[ACTUALS_COST] == "1"
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_per_key_math(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the quota risk issue uses per-key site counts, not the total across all keys."""

    try:
        fake_sites = [
            {CONF_API_KEY: "key1"},
            {CONF_API_KEY: "key1"},
            {CONF_API_KEY: "key2"},
        ]
        api_typical: dict[str, int] = {"key1": 9, "key2": 9}
        api_limit = 9

        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, {}, {}, api_limit, get_actuals=True)
        issue = issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)
        assert issue is not None, "Issue should be raised: key1 typical 9 + 2 actuals = 11 > 10"
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders[API_USED] == "9"
        assert issue.translation_placeholders[API_LIMIT] == "10"
        assert issue.translation_placeholders[ACTUALS_COST] == "2", (
            "actuals_cost must be key1's site count (2), not total sites across both keys (3)"
        )

        ir.async_delete_issue(hass, DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)
        api_typical2: dict[str, int] = {"key1": 7, "key2": 10}
        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical2, {}, {}, api_limit, get_actuals=True)
        issue2 = issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)
        assert issue2 is not None, "Issue should be raised: key2 typical 10 + 1 = 11 > 10"
        assert issue2.translation_placeholders is not None
        assert issue2.translation_placeholders[API_USED] == "10"
        assert issue2.translation_placeholders[ACTUALS_COST] == "1"
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_not_raised_when_within_quota(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the quota risk issue is not raised when typical usage plus actuals cost is within the inferred quota."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}]
        api_limit = 9

        api_typical: dict[str, int] = {"key1": 8}
        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, {}, {}, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should not exist: typical 8 + 1 actuals = 9, not > inferred quota 10"
        )

        sync_actuals_quota_risk_issue(hass, fake_sites, {"key1": 10}, {}, {}, 10, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should not exist at the hobbyist maximum, where the update plan keeps the estimated actuals calls free"
        )
        sync_actuals_quota_risk_issue(hass, fake_sites, {"key1": 50}, {}, {}, 50, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should not exist at the hobbyist maximum of 50"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_persists_until_config_reduced(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that a raised quota risk issue is not auto-cleared by a transient drop in typical usage."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}, {CONF_API_KEY: "key1"}]
        api_limit = 9

        sync_actuals_quota_risk_issue(hass, fake_sites, {"key1": 9}, {}, {}, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue should be raised: typical 9 + 2 actuals = 11 > inferred quota 10"
        )

        sync_actuals_quota_risk_issue(hass, fake_sites, {"key1": 5}, {}, {}, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue should persist: configuration api_limit(9)+2 actuals=11 > inferred quota 10, regardless of the current typical being low"
        )

        sync_actuals_quota_risk_issue(hass, fake_sites, {"key1": 5}, {}, {}, 8, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should be cleared once api_limit(8)+2 actuals=10 <= inferred quota 10"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_persists_after_429(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the quota risk issue is not cleared by a 429 quota-exceeded response."""

    try:
        options = copy.deepcopy(DEFAULT_INPUT1)
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 1
        entry = await async_init_integration(hass, options)
        coordinator: SolcastUpdateCoordinator = entry.runtime_data.coordinator
        solcast: SolcastApi = patch_solcast_api(coordinator.solcast)
        caplog.clear()

        for key in solcast.api_typical:
            solcast.api_typical[key] = 50
        sync_actuals_quota_risk_issue(
            hass, solcast.sites, solcast.api_typical, solcast.api_used, solcast.api_forced, solcast.api_limit, get_actuals=True
        )
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, "Issue should exist before 429"

        session_set(MOCK_OVER_LIMIT)
        try:
            await solcast.fetcher.update_estimated_actuals()
        finally:
            session_clear(MOCK_OVER_LIMIT)

        assert "No valid data was returned for estimated_actuals" in caplog.text
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue ISSUE_ACTUALS_QUOTA_TODAY should persist after a 429: the configuration is still risky"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_cleared_when_get_actuals_disabled(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the runtime quota risk issue clears when estimated actuals fetching is disabled."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}]
        api_typical: dict[str, int] = {"key1": 10}
        api_limit = 9

        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, {}, {}, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, "Issue should exist before clearing"

        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, {}, {}, api_limit, get_actuals=False)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue ISSUE_ACTUALS_QUOTA_TODAY should be cleared when get_actuals is disabled"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_raised_by_todays_usage_exceeding_typical(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that today's running total serves as a floor when it exceeds the persisted typical."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}]
        api_typical: dict[str, int] = {"key1": 5}
        api_used: dict[str, int] = {"key1": 9}
        api_limit = 9

        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, api_used, {}, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should NOT exist: effective 9 + 1 actuals = 10, not > inferred quota 10"
        )

        api_used["key1"] = 10
        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, api_used, {}, api_limit, get_actuals=True)
        issue = issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)
        assert issue is not None, "Issue should be raised: today's 10 calls + 1 actuals = 11 > inferred quota 10"
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders[API_USED] == "10"
        assert issue.translation_placeholders[API_LIMIT] == "10"
        assert issue.translation_placeholders[ACTUALS_COST] == "1"

        api_used["key1"] = 5
        api_forced: dict[str, int] = {"key1": 5}
        sync_actuals_quota_risk_issue(hass, fake_sites, api_typical, api_used, api_forced, api_limit, get_actuals=True)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue should be raised: today's 5 tracked + 5 forced + 1 actuals = 11 > inferred quota 10"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_actuals_quota_today_issue_suppressed_when_allow_exceed_and_high_limit(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test that the quota risk issue is never raised when allow_exceed_api_limit_maximum is set and api_limit > 50."""

    try:
        fake_sites = [{CONF_API_KEY: "key1"}]
        api_limit = 4000

        api_typical: dict[str, int] = {"key1": api_limit}
        sync_actuals_quota_risk_issue(
            hass,
            fake_sites,
            api_typical,
            {"key1": api_limit},
            {},
            api_limit,
            get_actuals=True,
            allow_exceed_api_limit_maximum=True,
        )
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is None, (
            "Issue should NOT be raised when allow_exceed_api_limit_maximum=True and api_limit > 50"
        )

        sync_actuals_quota_risk_issue(
            hass,
            fake_sites,
            {"key1": 10},
            {},
            {},
            9,
            get_actuals=True,
            allow_exceed_api_limit_maximum=True,
        )
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue should be raised when allow_exceed=True but api_limit=9 (quota=9) and typical 10 + 1 > 9"
        )
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY)

        sync_actuals_quota_risk_issue(
            hass,
            fake_sites,
            {"key1": 10},
            {},
            {},
            9,
            get_actuals=True,
            allow_exceed_api_limit_maximum=False,
        )
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_ACTUALS_QUOTA_TODAY) is not None, (
            "Issue should be raised for a sub-maximum limit without allow_exceed_api_limit_maximum"
        )
    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
