"""Rules that keep the original Solcast entry and separate a named one."""

from homeassistant.components.solcast_solar.const import INTEGRATION, TITLE
from homeassistant.components.solcast_solar.instance import (
    cache_stem,
    device_name_for,
    entry_title,
    instance_slug,
    is_reserved_slug,
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


def test_slug_fallback_and_reserved_names() -> None:
    """Names without ASCII letters are transliterated; names of the original's files are reserved."""

    assert instance_slug("Süd-&Westdach") == "suedwestdach"
    assert instance_slug("Café") == "caf"
    assert instance_slug("東屋根") == "dongwugen"
    assert instance_slug("☀️") == ""
    for reserved in ("Sites", "Usage", "Sites2", "usage-west", "Actuals", "Advanced", "Dampening", "Generation", "Undampened"):
        assert is_reserved_slug(instance_slug(reserved)), reserved
    for allowed in ("West", "Generationsdach", "Ostdach", "2026"):
        assert not is_reserved_slug(instance_slug(allowed)), allowed
