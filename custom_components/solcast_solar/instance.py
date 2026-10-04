"""Naming and file rules for more than one Solcast config entry.

An entry without ``instance_name`` is the original entry: it keeps
``solcast.json``, the existing unique IDs and the device name
``Solcast PV Forecast``. A named entry gets its own cache stem, a
prefixed unique ID for the shared sensors, and its own device name.
Rooftop sensors stay on the resource ID, which is already unique.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import (
    CONFIG_DISCRETE_NAME,
    CONFIG_FOLDER_DISCRETE,
    INSTANCE_NAME,
    INTEGRATION,
    TITLE,
)

_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})  # codespell:ignore ue
# A named stem must not be a file of the original entry (solcast-actuals.json, ...),
# nor match its globs solcast-sites*.json and solcast-usage*.json.
_RESERVED_SLUGS = ("actuals", "advanced", "dampening", "generation", "undampened")
_RESERVED_PREFIXES = ("sites", "usage")


def instance_name(options: Mapping[str, Any] | None) -> str:
    """Return the trimmed instance name, or an empty string for the original entry."""

    if not options:
        return ""
    return str(options.get(INSTANCE_NAME, "") or "").strip()


def instance_slug(name: str) -> str:
    """Return a lowercase slug of ASCII letters and digits, or an empty string."""

    lowered = name.strip().lower().translate(_UMLAUT)
    slug = "".join(character for character in lowered if character.isascii() and character.isalnum())
    if slug or not any(character.isalnum() for character in lowered):
        return slug
    # Only names without any ASCII letter or digit (東屋根) are transliterated, so existing slugs stay.
    return slugify(lowered).replace("_", "")


def is_reserved_slug(slug: str) -> bool:
    """Return whether a slug would reuse or match the original entry's cache files."""

    return slug in _RESERVED_SLUGS or slug.startswith(_RESERVED_PREFIXES)


def is_named_instance(options: Mapping[str, Any] | None) -> bool:
    """Return whether this entry is a named extra instance."""

    return bool(instance_slug(instance_name(options)))


def entry_title(options: Mapping[str, Any] | None) -> str:
    """Return the config-entry title. The original entry stays ``Solcast Solar``."""

    name = instance_name(options)
    if not instance_slug(name):
        return TITLE
    return f"Solcast {name}"


def saved_title(entry: Any, options: Mapping[str, Any] | None) -> str:
    """Return the title to save with changed options; a named entry keeps the title the user may have set."""

    return str(entry.title) if is_named_instance(options) else TITLE


def device_name_for(options: Mapping[str, Any] | None) -> str:
    """Return the device name. The original entry stays ``Solcast PV Forecast``."""

    name = instance_name(options)
    if not instance_slug(name):
        return INTEGRATION
    return f"Solcast {name}"


def shared_unique_id(options: Mapping[str, Any] | None, key: str) -> str:
    """Prefix a shared-sensor unique ID. Rooftop IDs must not be passed here."""

    slug = instance_slug(instance_name(options))
    if not slug:
        return key
    return f"{slug}_{key}"


def cache_stem(options: Mapping[str, Any] | None) -> str:
    """Return ``solcast`` or ``solcast-<slug>``."""

    slug = instance_slug(instance_name(options))
    if not slug:
        return "solcast"
    return f"solcast-{slug}"


def cache_file_path(hass: HomeAssistant, options: Mapping[str, Any] | None) -> str:
    """Return the cache path in the same shape the original entry uses today."""

    stem = cache_stem(options)
    return hass.config.path(
        f"{hass.config.config_dir}/{CONFIG_DISCRETE_NAME}/{stem}.json"
        if CONFIG_FOLDER_DISCRETE
        else f"{hass.config.config_dir}/{stem}.json"
    )


def advanced_file_path(hass: HomeAssistant, options: Mapping[str, Any] | None) -> Path:
    """Return the advanced-options file that belongs to this cache stem."""

    cache = Path(cache_file_path(hass, options))
    return cache.parent / f"{cache.stem}-advanced.json"


def scoped_issue_id(issue_id: str, options: Mapping[str, Any] | None, entry_id: str) -> str:
    """Keep the original issue ID for the unnamed entry so existing repairs stay put."""

    if not is_named_instance(options) or not entry_id:
        return issue_id
    return f"{issue_id}_{entry_id}"


def repair_issue_id(issue_id: str, entry: Any) -> str:
    """Return the repair ID for this config entry."""

    if entry is None:
        return issue_id
    return scoped_issue_id(issue_id, getattr(entry, "options", None), str(getattr(entry, "entry_id", "") or ""))


def repair_placeholders(entry: Any, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Placeholders for a repair; ``instance`` adds a named entry's title to the repair title, and is empty otherwise."""

    instance = ""
    if entry is not None and is_named_instance(entry.options):
        instance = f" ({str(entry.title or '').strip() or device_name_for(entry.options)})"
    return {"instance": instance, **(extra or {})}
