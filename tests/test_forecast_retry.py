"""Test forecasts update retry mechanism."""

import asyncio
import copy
from datetime import UTC, datetime as dt, timedelta
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock
from zoneinfo import ZoneInfo

from aiohttp import ClientConnectorDNSError, ClientResponseError, RequestInfo
from aiohttp.client_reqrep import ConnectionKey
from freezegun.api import FrozenDateTimeFactory
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from yarl import URL

from homeassistant.components.recorder import Recorder
from homeassistant.components.solcast_solar.const import (
    ADVANCED_DNS_TIMEOUT_RETRIES,
    ADVANCED_LOG_UPDATE_FAILURE_ONLY,
    ADVANCED_TRIGGER_ON_API_AVAILABLE,
    ADVANCED_TRIGGER_ON_API_UNAVAILABLE,
    API_KEY,
    DAILY_LIMIT_CONSUMED,
    DOMAIN,
    FAILURE,
    FORECASTS,
    ISSUE_API_UNAVAILABLE,
    LAST_7D,
    LAST_14D,
    LAST_24H,
    LAST_UPDATED,
    RESET,
    RESOURCE_ID,
    SERVICE_FORCE_UPDATE_FORECASTS,
    TASK_FORECASTS_FETCH,
    TASK_FORECASTS_FETCH_IMMEDIATE,
    TASK_NEW_DAY_ACTUALS,
)
from homeassistant.components.solcast_solar.enums import UpdateOutcome, UpdateResult
import homeassistant.components.solcast_solar.fetcher as fetcher_module
from homeassistant.components.solcast_solar.fetcher import Fetcher
from homeassistant.components.solcast_solar.instance import repair_placeholders
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from . import (
    DEFAULT_INPUT1,
    KEY1,
    MOCK_BUSY,
    async_cleanup_integration_tests,
    async_init_integration,
    session_clear,
    session_set,
    write_advanced_options,
)


class AsyncMockDoNothing(mock.MagicMock):
    """Do nothing. Used to replace asyncio sleep."""

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        """Do nothing."""
        return super().__call__(*args, **kwargs)


@pytest.fixture(autouse=True)
def frozen_time() -> None:
    """Override autouse fixture for this module.

    Using other mock times.
    """
    return


_LOGGER = logging.getLogger(__name__)


def _dns_connector_error(message: str) -> ClientConnectorDNSError:
    """Build a DNS connector error with a concrete connection key."""

    return ClientConnectorDNSError(
        ConnectionKey(
            host="api.solcast.com.au",
            port=443,
            is_ssl=True,
            ssl=True,
            proxy=None,
            proxy_auth=None,
            proxy_headers_hash=None,
            server_hostname=None,
        ),
        OSError(message),
    )


def test_dns_error_message_falls_back_to_class_name() -> None:
    """The DNS error helper should tolerate connector errors without an os_error."""

    class DummyConnectorDNSError:
        """Synthetic DNS error without an os_error attribute."""

        os_error = None

    assert Fetcher._dns_error_message(cast(ClientConnectorDNSError, DummyConnectorDNSError())) == "DummyConnectorDNSError"


def _occurs_in_log(caplog: pytest.LogCaptureFixture, text: str, occurrences: int) -> None:
    occurs = 0
    for entry in caplog.messages:
        if text in entry:
            occurs += 1
    assert occurrences == occurs, f"Expected {text!r} to occur {occurrences} times in the log, found {occurs}"


def _log_level_for(caplog: pytest.LogCaptureFixture, text: str) -> int:
    """Return the level of the first caplog record whose message contains text."""
    for record in caplog.records:
        if text in record.getMessage():
            return record.levelno
    raise AssertionError(f"No log record found containing: {text!r}")


async def _wait_for_log(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
    text: str,
    timeout: float = 10,
) -> None:
    """Wait for a log message while advancing frozen time."""

    last_record = 0
    async with asyncio.timeout(timeout):
        while True:
            records = caplog.records
            if any(text in r.getMessage() for r in records[last_record:]):
                return
            last_record = len(records)
            freezer.tick(0.1)
            await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_forecast_retry(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test retry mechanism."""

    try:
        freezer.move_to("2025-01-11 00:00:00")  # A pending update will be queued for 00:00:09 UTC

        write_advanced_options(
            hass.config.config_dir,
            {
                ADVANCED_TRIGGER_ON_API_UNAVAILABLE: "Automation unavailable",
                ADVANCED_TRIGGER_ON_API_AVAILABLE: "Automation available",
            },
        )

        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast

        # A second check in the same five-minute window must not queue the pending update twice.
        created = caplog.text.count("Create task pending_update_009")
        await coordinator._updater.check_forecast_fetch()  # pyright: ignore[reportPrivateUsage]
        assert caplog.text.count("Create task pending_update_009") == created == 1

        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": [
                    {
                        "id": "automation_available",
                        "alias": "Automation available",
                        "trigger": {"platform": "event", "event_type": "test_event"},
                        "action": {"service": "persistent_notification.create"},
                    },
                    {
                        "id": "automation_unavailable",
                        "alias": "Automation unavailable",
                        "trigger": {"platform": "event", "event_type": "test_event"},
                        "action": {"service": "persistent_notification.create"},
                    },
                ]
            },
        ), "Automation component setup failed"
        await hass.async_block_till_done()

        session_set(MOCK_BUSY)
        caplog.clear()

        solcast.data[LAST_UPDATED] -= timedelta(minutes=20)
        with mock.patch("homeassistant.components.solcast_solar.fetcher.Fetcher._sleep", new_callable=AsyncMockDoNothing):
            await _wait_for_log(hass, caplog, freezer, "Raise issue for api_unavailable")

        assert "API was tried 10 times, but all attempts failed" in caplog.text
        _occurs_in_log(caplog, "Call status 429/Try again later", 10)
        assert "Forecast has not been updated: 429/Try again later after 10 attempts, next auto update at" in caplog.text
        assert "Completed task pending_update_009" in caplog.text
        assert "Raise issue for api_unavailable" in caplog.text
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_API_UNAVAILABLE) is not None, "Issue ISSUE_API_UNAVAILABLE should exist"
        await solcast.tasks_cancel()
        await coordinator.tasks_cancel()

        session_clear(MOCK_BUSY)
        caplog.clear()
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_UPDATE_FORECASTS, {}, blocking=True)
        await _wait_for_log(hass, caplog, freezer, "Remove issue for api_unavailable", timeout=30)
        assert "Remove issue for api_unavailable" in caplog.text
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_API_UNAVAILABLE) is None, "Issue ISSUE_API_UNAVAILABLE should be removed"
        await solcast.tasks_cancel()
        await coordinator.tasks_cancel()

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_api_available_trigger_needs_cleared_issue(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The API-available automation runs only when a successful call clears the api_unavailable repair."""

    try:
        write_advanced_options(hass.config.config_dir, {ADVANCED_TRIGGER_ON_API_AVAILABLE: "Automation available"})
        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        fetcher = entry.runtime_data.coordinator.solcast.fetcher
        with mock.patch("homeassistant.components.solcast_solar.fetcher.async_trigger_automation_by_name") as trigger:
            assert await fetcher.fetch_data(hours=24, path=FORECASTS, site="1111-1111-1111-1111", api_key=KEY1, force=True)
            trigger.assert_not_called()

            ir.async_create_issue(
                hass,
                DOMAIN,
                ISSUE_API_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_API_UNAVAILABLE,
                translation_placeholders=repair_placeholders(entry),
            )
            assert await fetcher.fetch_data(hours=24, path=FORECASTS, site="1111-1111-1111-1111", api_key=KEY1, force=True)
            trigger.assert_called_once_with(hass, "Automation available")
            assert issue_registry.async_get_issue(DOMAIN, ISSUE_API_UNAVAILABLE) is None

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_log_update_failure_only_enabled(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test retry mechanism with log_update_failure_only enabled.

    Retry messages must be at DEBUG level, and the only forecast-update
    warning must be the final summary containing the failure reason.
    """

    try:
        freezer.move_to("2025-01-11 00:00:00")

        write_advanced_options(
            hass.config.config_dir,
            {
                ADVANCED_TRIGGER_ON_API_UNAVAILABLE: "Automation unavailable",
                ADVANCED_TRIGGER_ON_API_AVAILABLE: "Automation available",
                ADVANCED_LOG_UPDATE_FAILURE_ONLY: True,
            },
        )

        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast

        session_set(MOCK_BUSY)
        caplog.clear()
        caplog.set_level(logging.DEBUG)

        solcast.data[LAST_UPDATED] -= timedelta(minutes=20)
        with mock.patch("homeassistant.components.solcast_solar.fetcher.Fetcher._sleep", new_callable=AsyncMockDoNothing):
            await _wait_for_log(hass, caplog, freezer, "Raise issue for api_unavailable")

        # Retry-related messages must be logged at DEBUG (not WARNING).
        assert _log_level_for(caplog, "Call status 429/Try again later, pausing") == logging.DEBUG
        # API usage status must not be logged as ERROR when enabled.
        assert _log_level_for(caplog, "Call status 429/Try again later, API used is") == logging.DEBUG
        # Retry exhaustion should not produce an extra log line when enabled.
        assert "API was tried 10 times, but all attempts failed" not in caplog.text
        # The overall forecast-not-updated summary stays WARNING and carries the reason.
        assert (
            _log_level_for(
                caplog,
                "Forecast has not been updated: 429/Try again later after 10 attempts, next auto update at",
            )
            == logging.WARNING
        )

        await solcast.tasks_cancel()
        await coordinator.tasks_cancel()

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_forecast_abort_does_not_build_actuals(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Ensure aborted forecast updates do not rebuild estimated actual data."""

    try:
        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator

        with (
            mock.patch.object(
                coordinator.solcast.fetcher,
                "get_forecast_update",
                new=mock.AsyncMock(return_value=UpdateResult(UpdateOutcome.ABORTED, "Forecast update aborted")),
            ),
            mock.patch.object(
                coordinator.solcast,
                "build_actual_data",
                new=mock.AsyncMock(return_value=True),
            ) as build_actual_data,
        ):
            await coordinator._updater.forecast_update(completion="Completed task update")

        build_actual_data.assert_not_awaited()

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_forecast_update_aborts_while_a_fetch_runs(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A forecast update aborts while another fetch of this entry is still running."""

    try:
        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        solcast = entry.runtime_data.coordinator.solcast
        solcast.data[LAST_UPDATED] -= timedelta(minutes=20)

        solcast.tasks[TASK_FORECASTS_FETCH] = mock.Mock()  # A fetch that is still running.
        result = await solcast.fetcher.get_forecast_update()
        solcast.tasks.pop(TASK_FORECASTS_FETCH)

        assert result.outcome is UpdateOutcome.ABORTED
        assert "A fetch task is already running, so aborting forecast update" in caplog.text

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_dns_timeout_retries_then_succeeds(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retry DNS resolution timeouts immediately before failing the fetch."""

    try:
        write_advanced_options(hass.config.config_dir, {ADVANCED_DNS_TIMEOUT_RETRIES: 2})

        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        site_id = solcast.sites[0][RESOURCE_ID]
        api_key = solcast.sites[0][API_KEY]

        dns_timeout = _dns_connector_error("Timeout while contacting DNS servers")
        response = mock.MagicMock(status=200, url=f"https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts")
        response.text = mock.AsyncMock(return_value=json.dumps({FORECASTS: []}))

        original_session = solcast.aiohttp_session
        mock_session = mock.MagicMock()
        mock_session.get = mock.AsyncMock(side_effect=[dns_timeout, dns_timeout, response])
        solcast.aiohttp_session = mock_session
        caplog.set_level(logging.DEBUG)

        try:
            result = await solcast.fetcher.fetch_data(hours=48, path=FORECASTS, site=site_id, api_key=api_key, force=True)
        finally:
            solcast.aiohttp_session = original_session

        assert result == {FORECASTS: []}
        assert mock_session.get.await_count == 3
        assert "DNS resolution timeout fetching path forecasts for site" in caplog.text
        _occurs_in_log(caplog, "retry 1/2", 1)
        _occurs_in_log(caplog, "retry 2/2", 1)

        await solcast.tasks_cancel()
        await coordinator.tasks_cancel()

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dns_timeout_retries", "expected_reason", "expected_calls", "expected_retry_logs"),
    [
        pytest.param(2, "DNS resolution timeout after 2 retries", 3, 2, id="retries-exhausted"),
        pytest.param(0, "DNS resolution timeout", 1, 0, id="zero-retries"),
    ],
)
async def test_dns_timeout_retries_exhausted_terminal_failure(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    dns_timeout_retries: int,
    expected_reason: str,
    expected_calls: int,
    expected_retry_logs: int,
) -> None:
    """Exhausted DNS retries return a clear failure reason and do not enter backoff."""

    try:
        write_advanced_options(hass.config.config_dir, {ADVANCED_DNS_TIMEOUT_RETRIES: dns_timeout_retries})

        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast
        site_id = solcast.sites[0][RESOURCE_ID]
        api_key = solcast.sites[0][API_KEY]

        dns_timeout = _dns_connector_error("Timeout while contacting DNS servers")

        original_session = solcast.aiohttp_session
        mock_session = mock.MagicMock()
        mock_session.get = mock.AsyncMock(side_effect=[dns_timeout] * expected_calls)
        solcast.aiohttp_session = mock_session
        caplog.set_level(logging.DEBUG)

        sleep_mock = mock.AsyncMock()
        with mock.patch.object(solcast.fetcher, "_sleep", sleep_mock):
            try:
                result = await solcast.fetcher.fetch_data(hours=48, path=FORECASTS, site=site_id, api_key=api_key, force=True)
            finally:
                solcast.aiohttp_session = original_session

        assert result == expected_reason
        assert mock_session.get.await_count == expected_calls
        sleep_mock.assert_not_awaited()
        _occurs_in_log(caplog, "DNS resolution timeout fetching path forecasts for site", expected_retry_logs)
        assert "pausing" not in caplog.text

        await solcast.tasks_cancel()
        await coordinator.tasks_cancel()

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_force_update_unload_cancels_update_task(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Ensure unload cancels an in-progress immediate forecast update task."""

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_forecast_update(*_args: Any, **_kwargs: Any) -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator

        with mock.patch.object(coordinator._updater, "forecast_update", side_effect=blocked_forecast_update):
            await hass.services.async_call(DOMAIN, SERVICE_FORCE_UPDATE_FORECASTS, {}, blocking=False)

            async with asyncio.timeout(10):
                while not entered.is_set() or TASK_FORECASTS_FETCH_IMMEDIATE not in coordinator.tasks:
                    await hass.async_block_till_done()

            assert await hass.config_entries.async_unload(entry.entry_id), "Config entry unload failed"
            await hass.async_block_till_done()

            assert TASK_FORECASTS_FETCH_IMMEDIATE not in coordinator.tasks
            assert cancelled.is_set(), "Expected in-progress immediate task to be cancelled on unload"

    finally:
        await async_cleanup_integration_tests(hass)


@pytest.mark.asyncio
async def test_retry_recovery_then_schedule_deferred_actuals(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    issue_registry: ir.IssueRegistry,
) -> None:
    """After retry failure and recovery, schedule estimated actuals when midnight window was missed."""

    try:
        freezer.move_to("2025-01-11 00:00:00")

        write_advanced_options(
            hass.config.config_dir,
            {
                ADVANCED_TRIGGER_ON_API_UNAVAILABLE: "Automation unavailable",
                ADVANCED_TRIGGER_ON_API_AVAILABLE: "Automation available",
            },
        )

        entry = await async_init_integration(hass, DEFAULT_INPUT1)
        coordinator = entry.runtime_data.coordinator
        solcast = coordinator.solcast

        session_set(MOCK_BUSY)
        caplog.clear()
        solcast.data[LAST_UPDATED] -= timedelta(minutes=20)

        with mock.patch("homeassistant.components.solcast_solar.fetcher.Fetcher._sleep", new_callable=AsyncMockDoNothing):
            await _wait_for_log(hass, caplog, freezer, "Raise issue for api_unavailable")

        assert issue_registry.async_get_issue(DOMAIN, ISSUE_API_UNAVAILABLE) is not None

        session_clear(MOCK_BUSY)
        caplog.clear()
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_UPDATE_FORECASTS, {}, blocking=True)
        await _wait_for_log(hass, caplog, freezer, "Remove issue for api_unavailable", timeout=30)
        assert issue_registry.async_get_issue(DOMAIN, ISSUE_API_UNAVAILABLE) is None

        freezer.move_to("2025-01-11 00:30:00")

        # Emulate no estimated-actuals update yet today so scheduling path is exercised.
        tz = ZoneInfo("Australia/Brisbane")
        solcast.data_actuals[LAST_UPDATED] = dt(2025, 1, 10, 12, 0, 0, tzinfo=tz).astimezone(solcast.tz)

        caplog.clear()
        scheduled = await coordinator._updater.check_estimated_actuals_fetch()
        assert scheduled is True
        assert TASK_NEW_DAY_ACTUALS in coordinator.tasks
        assert "Estimated actuals update window was missed, scheduling at" in caplog.text

        await coordinator.tasks_cancel_specific(TASK_NEW_DAY_ACTUALS)

    finally:
        await async_cleanup_integration_tests(hass)


async def test_fetch_timeout_and_client_error_are_failures(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test the time limit on all attempts fails the update like other failures, and a client error does not log the API key."""

    api_key = "SECRETKEY1234567890"

    class _ShortTimeout:
        """The fetcher's asyncio, with the fifteen minute limit cut short."""

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        @staticmethod
        def timeout(_delay: float) -> asyncio.Timeout:
            return asyncio.timeout(0.05)

    async def hang(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(60)

    async def client_error(url: str, params: dict[str, Any], **_kwargs: Any) -> None:
        request = RequestInfo(URL(url).with_query(params), "GET", CIMultiDictProxy(CIMultiDict()))
        raise ClientResponseError(request, (), status=502, message="Bad Gateway")

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        solcast = entry.runtime_data.coordinator.solcast
        monkeypatch.setattr(fetcher_module, "asyncio", _ShortTimeout())
        monkeypatch.setattr(solcast, "aiohttp_session", SimpleNamespace(get=hang))
        solcast.data[LAST_UPDATED] = dt_util.now(UTC) - timedelta(hours=1)
        failures = solcast.data[FAILURE][LAST_24H]

        result = await solcast.fetcher.get_forecast_update(force=True)
        assert result.outcome == UpdateOutcome.FAILED
        assert solcast.data[FAILURE][LAST_24H] == failures + 1

        caplog.clear()
        monkeypatch.setattr(solcast, "aiohttp_session", SimpleNamespace(get=client_error))
        solcast.api_used[api_key] = 0
        solcast.api_limits[api_key] = 10
        assert await solcast.fetcher.fetch_data(hours=1, path=FORECASTS, site="1111-1111-1111-1111", api_key=api_key, force=True) is None
        assert "Client error: 502" in caplog.text
        assert api_key not in caplog.text

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_gateway_errors_are_failures_and_cancel_is_raised(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """A retried 502, 503 or 504 counts as a failure like a 429, and cancelling a fetch cancels its caller too."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        solcast = entry.runtime_data.coordinator.solcast
        site_id = solcast.sites[0][RESOURCE_ID]
        api_key = solcast.sites[0][API_KEY]
        failures = solcast.data[FAILURE][LAST_24H]

        responses = [mock.MagicMock(status=status, url="https://api.solcast.com.au") for status in (502, 503, 504, 200)]
        responses[-1].text = mock.AsyncMock(return_value=json.dumps({FORECASTS: []}))
        original_session = solcast.aiohttp_session
        solcast.aiohttp_session = SimpleNamespace(get=mock.AsyncMock(side_effect=responses))
        try:
            with mock.patch.object(solcast.fetcher, "_sleep", mock.AsyncMock()):
                result = await solcast.fetcher.fetch_data(hours=48, path=FORECASTS, site=site_id, api_key=api_key, force=True)
            assert result == {FORECASTS: []}
            assert solcast.data[FAILURE][LAST_24H] == failures + 3

            solcast.aiohttp_session = SimpleNamespace(get=mock.AsyncMock(side_effect=asyncio.CancelledError))
            with pytest.raises(asyncio.CancelledError):
                await solcast.fetcher.fetch_data(hours=48, path=FORECASTS, site=site_id, api_key=api_key, force=True)
        finally:
            solcast.aiohttp_session = original_session

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"


async def test_restart_over_utc_midnight_resets_usage_and_rotates_failures(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Days of daily resets missed while Home Assistant was stopped are caught up at the next start."""

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT1))
        solcast = entry.runtime_data.coordinator.solcast
        solcast.data[FAILURE][LAST_24H] = 3
        solcast.data[FAILURE][LAST_7D] = [3, 1, 0, 0, 0, 0, 0]
        solcast.data[FAILURE][LAST_14D] = [3, 1] + [0] * 12
        await solcast.sites_cache.serialise_data(solcast.data, solcast.filename)

        usage_file = Path(solcast.sites_cache._get_usage_cache_filename(KEY1))
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
        usage[DAILY_LIMIT_CONSUMED] = 7
        usage[RESET] = (solcast.dt_helper.utc_previous_midnight() - timedelta(days=2)).isoformat()
        usage_file.write_text(json.dumps(usage), encoding="utf-8")

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        solcast = entry.runtime_data.coordinator.solcast

        assert solcast.data[FAILURE][LAST_24H] == 0
        assert solcast.data[FAILURE][LAST_7D] == [0, 0, 3, 1, 0, 0, 0]
        assert solcast.data[FAILURE][LAST_14D][:4] == [0, 0, 3, 1]
        assert len(solcast.data[FAILURE][LAST_14D]) == 14
        assert solcast.api_typical[KEY1] == 7
        assert solcast.sites_cache.missed_midnight_resets == 0

    finally:
        assert await async_cleanup_integration_tests(hass), "Integration test cleanup failed"
