"""Rules that keep the original Solcast entry and separate a named one."""

from custom_components.solcast_solar.const import INTEGRATION, TITLE
from custom_components.solcast_solar.instance import (
    cache_stem,
    device_name_for,
    entry_title,
    instance_slug,
    scoped_issue_id,
    shared_unique_id,
)


def test_original_entry_keeps_ids_and_files() -> None:
    """An entry without a name is the one already running."""

    options: dict[str, str] = {}

    assert instance_slug("") == ""
    assert cache_stem(options) == "solcast"
    assert shared_unique_id(options, "total_kwh_forecast_today") == "total_kwh_forecast_today"
    assert shared_unique_id(options, "hard_limit") == "hard_limit"
    assert device_name_for(options) == INTEGRATION
    assert entry_title(options) == TITLE
    assert scoped_issue_id("actuals_api_limit", options, "abc") == "actuals_api_limit"


def test_named_entry_is_separate() -> None:
    """West gets its own file stem, unique IDs and device name."""

    options = {"instance_name": "West"}

    assert instance_slug("West") == "west"
    assert instance_slug("Süd") == "sued"
    assert instance_slug("!!!") == ""
    assert cache_stem(options) == "solcast-west"
    assert shared_unique_id(options, "total_kwh_forecast_today") == "west_total_kwh_forecast_today"
    assert shared_unique_id(options, "hard_limit") == "west_hard_limit"
    assert device_name_for(options) == "Solcast West"
    assert entry_title(options) == "Solcast West"
    assert scoped_issue_id("actuals_api_limit", options, "abc") == "actuals_api_limit_abc"
