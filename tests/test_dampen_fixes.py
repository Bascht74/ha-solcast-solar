import datetime
from datetime import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from custom_components.solcast_solar.dampen import Dampening


def test_target_timestamp_uses_local_date() -> None:
    """In a zone ahead of UTC the target is on the local date of target_day, not one day earlier."""
    dampening = Dampening.__new__(Dampening)
    dampening.api = SimpleNamespace(tz=ZoneInfo("Australia/Sydney"))  # pyright: ignore[reportAttributeAccessIssue]
    past_ts = dt(2025, 1, 11, 2, 0, tzinfo=datetime.UTC)  # 13:00 on 11 January in Sydney
    target_day = dt(2025, 1, 25, tzinfo=ZoneInfo("Australia/Sydney"))
    assert dampening._target_timestamp(past_ts, target_day) == dt(2025, 1, 25, 2, 0, tzinfo=datetime.UTC)  # pyright: ignore[reportPrivateUsage]
