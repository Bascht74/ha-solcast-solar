"""Test configuration for Solcast Solar integration."""

from collections.abc import Generator
import logging
import threading
from typing import Any

import freezegun
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.util import dt as dt_util

from . import aioresponses_reset

from tests.ignore_uncaught_exceptions import IGNORE_UNCAUGHT_EXCEPTIONS

# Background tasks can fire during teardown under parallel execution, producing
# an asyncio exception (InvalidStateError, CancelledError) when the entry is
# already unloading. Suppress here.
IGNORE_UNCAUGHT_EXCEPTIONS.append(
    (
        "tests.components.solcast_solar.test_integration",
        "test_integration",
    )
)

_SUPPRESS_LOGGERS = [
    "homeassistant.core",
    "homeassistant.components.recorder.core",
    "homeassistant.components.recorder.pool",
    "homeassistant.components.recorder.pool.MutexPool",
    "sqlalchemy.engine.Engine",
    "watchfiles",
    "watchfiles.main",
    "asyncio",
]


@pytest.fixture(autouse=True)
def suppress_noisy_loggers() -> Generator[None]:
    """Disable noisy loggers for the duration of each test only."""
    loggers = [logging.getLogger(name) for name in _SUPPRESS_LOGGERS]
    previous = [logger.disabled for logger in loggers]
    for logger in loggers:
        logger.disabled = True
    yield
    for logger, was_disabled in zip(loggers, previous, strict=True):
        logger.disabled = was_disabled


@pytest.fixture(autouse=True)
def reset_aioresponses() -> Generator[None]:
    """Ensure the aiohttp mock is stopped after every test."""
    yield
    aioresponses_reset()


@pytest.fixture(autouse=True)
def join_watcher_threads() -> Generator[None]:
    """Let the worker threads of the file watchers end before Home Assistant's check for lingering threads.

    watchfiles waits for file changes in AnyIO worker threads, which are told to stop when the test task ends and
    may not be gone yet when the check runs. This fixture is torn down before that check, so it waits for them.
    """
    before = set(threading.enumerate())
    yield
    for thread in set(threading.enumerate()) - before:
        if thread.name == "AnyIO worker thread":
            thread.join(timeout=10)


@pytest.fixture(autouse=True)
def frozen_time() -> Generator[FrozenDateTimeFactory]:
    """Freeze test time."""

    with freezegun.freeze_time(f"{dt_util.naive_now().date()} 12:27:27", tz_offset=-10) as freeze:
        yield freeze  # type: ignore[misc]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use a per-test config directory so xdist workers do not share files."""
    return hass_tmp_config_dir


# Slowest tests.  Placing them first in the queue ensures xdist dispatches each to a separate worker.
_SLOW_FIRST: tuple[str, ...] = (
    "test_api_failure",
    "test_remaining_actions",
    "test_adaptive_auto_dampen",
    "test_auto_dampen",
    "test_advanced_options",
    "test_scenarios",
    "test_reconfigure_api_quota",
    "test_reauth_api_key",
    "test_reconfigure_api_key1",
    "test_integration_runtime_and_dampening_flow",
)


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Move the slowest tests to the front so xdist workers start on them first."""
    if not config.getoption("--dist", default="no").startswith(("load", "worksteal")):
        return
    slow, rest = [], []
    for item in items:
        base = item.name.split("[")[0]
        (slow if base in _SLOW_FIRST else rest).append(item)
    slow.sort(key=lambda i: _SLOW_FIRST.index(i.name.split("[")[0]))
    items[:] = slow + rest
