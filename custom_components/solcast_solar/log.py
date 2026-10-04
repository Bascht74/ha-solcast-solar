"""Solcast Solar logging helpers."""

from contextvars import ContextVar
import logging

_LOG_MESSAGE_REWRITES: dict[str, tuple[str, tuple[int, ...]]] = {
    "Finished fetching %s data in %.3f seconds (success: %s)": (
        "Finished fetching %s data (success: %s)",
        (0, 2),
    ),
}


# Name of the named entry whose task, timer or action is running; unset for the original entry.
_INSTANCE: ContextVar[str | None] = ContextVar("solcast_solar_instance", default=None)


def set_log_instance(name: str | None) -> None:
    """Prefix log lines of the running task, and of the tasks and timers it starts, with an entry name."""
    _INSTANCE.set(name or None)


class _LogFilter(logging.Filter):
    """Rewrite selected Solcast Solar log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite a configured log record and name the entry it belongs to."""
        if isinstance(record.msg, str) and (rewrite := _LOG_MESSAGE_REWRITES.get(record.msg)) is not None:
            replacement_message, argument_indexes = rewrite
            if isinstance(record.args, tuple) and all(0 <= index < len(record.args) for index in argument_indexes):
                record.msg = replacement_message
                record.args = tuple(record.args[index] for index in argument_indexes)
        if isinstance(record.msg, str) and (instance := _INSTANCE.get()) is not None:
            record.msg = f"[{instance.replace('%', '%%') if record.args else instance}] {record.msg}"
        return True


_LOG_FILTER = _LogFilter()


def get_logger(name: str) -> logging.Logger:
    """Return a logger with Solcast Solar message rewrites enabled."""
    logger = logging.getLogger(name)
    logger.addFilter(_LOG_FILTER)
    return logger
