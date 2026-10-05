"""Issue registry helpers for Solcast Solar."""

from collections.abc import Mapping
from datetime import datetime as dt
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    ACTUALS_COST,
    API_LIMIT,
    API_USED,
    AUTO_UPDATE,
    CONFIGURED_VALUE,
    DOMAIN,
    DT_DATE_ONLY_FORMAT,
    GET_ACTUALS,
    ISSUE_ACTUALS_API_LIMIT,
    ISSUE_ACTUALS_QUOTA_TODAY,
    ISSUE_ADVANCED_DEPRECATED,
    ISSUE_ADVANCED_PROBLEM,
    ISSUE_UNUSUAL_AZIMUTH_NORTHERN,
    ISSUE_UNUSUAL_AZIMUTH_SOUTHERN,
    LEARN_MORE_ADVANCED,
    NEW_OPTION,
    OPTION,
    PROBLEMS,
    STOPS_WORKING,
    SUGGESTED_VALUE,
)
from .enums import AutoUpdate
from .instance import is_named_instance, repair_placeholders, scoped_issue_id
from .log import get_logger
from .util import split_and_strip

_LOGGER = get_logger(__name__)


def check_unusual_azimuth(latitude: float, azimuth: float) -> tuple[bool, str, int]:
    """Classify whether an azimuth is unusual for the given latitude.

    Returns a tuple of (unusual, issue_key, proposal) where:
        unusual: True if the azimuth is unusual for the hemisphere.
        issue_key: The issue key string (northern or southern).
        proposal: The suggested corrected azimuth value.
    """
    unusual = False
    proposal = 0
    if latitude > 0:
        # Northern hemisphere: azimuth should be 90..180 or -180..-90
        issue_key = ISSUE_UNUSUAL_AZIMUTH_NORTHERN
        if azimuth > 0 and not (90 <= azimuth <= 180):
            unusual = True
            proposal = 180 - int(azimuth)
        if azimuth < 0 and not (-180 <= azimuth <= -90):
            unusual = True
            proposal = -180 - int(azimuth)
    else:
        # Southern hemisphere: azimuth should be 0..90 or -90..0
        issue_key = ISSUE_UNUSUAL_AZIMUTH_SOUTHERN
        if azimuth > 0 and not (0 <= azimuth <= 90):
            unusual = True
            proposal = 180 - int(azimuth)
        if azimuth < 0 and not (-90 <= azimuth <= 0):
            unusual = True
            proposal = -180 - int(azimuth)
    return unusual, issue_key, proposal


def _scoped(issue_id: str, entry: ConfigEntry | None) -> str:
    """Suffix repair IDs for a named entry so two entries do not share one repair."""

    if entry is None:
        return issue_id
    return scoped_issue_id(issue_id, entry.options, entry.entry_id)


def refresh_issue_placeholders(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-create this entry's repairs raised by an older version, whose title lacks the ``instance`` placeholder."""

    others = [other.entry_id for other in hass.config_entries.async_entries(DOMAIN) if other.entry_id != entry.entry_id]
    named = is_named_instance(entry.options)
    for issue in list(ir.async_get(hass).issues.values()):
        if issue.domain != DOMAIN or not issue.active or issue.translation_key is None or issue.severity is None:
            continue
        if "instance" in (issue.translation_placeholders or {}):
            continue
        if named != issue.issue_id.endswith(f"_{entry.entry_id}") or (
            not named and any(issue.issue_id.endswith(f"_{other}") for other in others)
        ):
            continue
        _LOGGER.debug("Refresh issue %s for this entry", issue.issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue.issue_id,
            breaks_in_ha_version=issue.breaks_in_ha_version,
            data=issue.data,
            is_fixable=bool(issue.is_fixable),
            is_persistent=issue.is_persistent,
            issue_domain=issue.issue_domain,
            learn_more_url=issue.learn_more_url,
            severity=issue.severity,
            translation_key=issue.translation_key,
            translation_placeholders=repair_placeholders(entry, issue.translation_placeholders),
        )


def sync_actuals_api_limit_issue(
    hass: HomeAssistant, options: Mapping[str, Any], sites: list[dict[str, Any]], entry: ConfigEntry | None = None
) -> None:
    """Raise or remove warning issue when estimated actuals consume auto-update API calls."""

    issue_registry = ir.async_get(hass)
    issue_id = _scoped(ISSUE_ACTUALS_API_LIMIT, entry)

    def _remove_issue() -> None:
        if issue_registry.async_get_issue(DOMAIN, issue_id) is not None:
            _LOGGER.debug("Remove issue for %s", issue_id)
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    try:
        auto_update = int(options.get(AUTO_UPDATE, AutoUpdate.NONE))
    except (TypeError, ValueError):
        _remove_issue()
        return

    if auto_update == AutoUpdate.NONE or not options.get(GET_ACTUALS, False):
        _remove_issue()
        return

    api_keys = split_and_strip(str(options.get(CONF_API_KEY, "")))
    api_limits = split_and_strip(str(options.get(API_LIMIT, "")))
    if not api_keys or not api_limits:
        _remove_issue()
        return

    original_limit_count = len(api_limits)
    while len(api_limits) < len(api_keys):
        api_limits.append(api_limits[-1])
    api_limits = api_limits[: len(api_keys)]

    try:
        configured_limits = [int(lim) for lim in api_limits]
    except ValueError:
        _remove_issue()
        return

    if not configured_limits or not all(limit in (10, 50) for limit in configured_limits):
        _remove_issue()
        return

    sites_per_key = dict.fromkeys(api_keys, 0)
    for site in sites:
        if (site_key := site.get(CONF_API_KEY)) in sites_per_key:
            sites_per_key[site_key] += 1

    suggested_limits = [max(lim - sites_per_key.get(key, 0), 1) for lim, key in zip(configured_limits, api_keys, strict=True)]

    if all(c <= s for c, s in zip(configured_limits, suggested_limits, strict=True)):
        _remove_issue()
        return

    if original_limit_count == 1:
        configured_value = str(configured_limits[0])
        suggested_value = str(min(suggested_limits))
    else:
        configured_value = ",".join(str(limit) for limit in configured_limits)
        suggested_value = ",".join(str(limit) for limit in suggested_limits)
    _LOGGER.debug(
        "Raise issue `%s` for configured API limits %s, suggested %s",
        ISSUE_ACTUALS_API_LIMIT,
        configured_value,
        suggested_value,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ACTUALS_API_LIMIT,
        translation_placeholders=repair_placeholders(
            entry,
            {
                CONFIGURED_VALUE: configured_value,
                SUGGESTED_VALUE: suggested_value,
            },
        ),
    )


def sync_actuals_quota_risk_issue(
    hass: HomeAssistant,
    sites: list[dict[str, Any]],
    api_typical: dict[str, int],
    api_used: dict[str, int],
    api_forced: dict[str, int],
    api_limit: int,
    get_actuals: bool,
    allow_exceed_api_limit_maximum: bool = False,
    entry: ConfigEntry | None = None,
) -> None:
    """Raise or remove warning issue when typical daily API usage may exhaust quota if actuals are fetched."""

    issue_registry = ir.async_get(hass)
    issue_id = _scoped(ISSUE_ACTUALS_QUOTA_TODAY, entry)

    def _remove_issue() -> None:
        if issue_registry.async_get_issue(DOMAIN, issue_id) is not None:
            _LOGGER.debug("Remove issue for %s", issue_id)
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    if not get_actuals or api_limit == 0:
        _remove_issue()
        return

    if allow_exceed_api_limit_maximum and api_limit > 50:
        _remove_issue()
        return

    inferred_quota = api_limit if allow_exceed_api_limit_maximum else (10 if api_limit <= 10 else 50)

    if not allow_exceed_api_limit_maximum and api_limit == inferred_quota:
        _remove_issue()
        return

    # Count sites per API key — actuals fetch uses one call per site per key.
    sites_per_key: dict[str, int] = {}
    for site in sites:
        if (key := site.get(CONF_API_KEY)) is not None:
            sites_per_key[key] = sites_per_key.get(key, 0) + 1

    def _effective_typical(key: str) -> int:
        """Return today's running total or the persisted typical, whichever is higher."""
        return max(
            api_typical.get(key, inferred_quota),
            api_used.get(key, 0) + api_forced.get(key, 0),
        )

    at_risk_items = [
        (key, count, _effective_typical(key)) for key, count in sites_per_key.items() if _effective_typical(key) + count > inferred_quota
    ]
    if at_risk_items:
        # Pick the key with the worst overage to populate the issue message.
        # Each key has its own independent quota, so describe the single key at risk.
        _, actuals_cost, effective = max(at_risk_items, key=lambda t: t[2] + t[1] - inferred_quota)
        _LOGGER.debug(
            "Raise issue `%s`: effective typical %d + %d actuals > %d inferred quota",
            ISSUE_ACTUALS_QUOTA_TODAY,
            effective,
            actuals_cost,
            inferred_quota,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ACTUALS_QUOTA_TODAY,
            translation_placeholders=repair_placeholders(
                entry,
                {
                    API_USED: str(effective),
                    API_LIMIT: str(inferred_quota),
                    ACTUALS_COST: str(actuals_cost),
                },
            ),
        )
        return

    # Only clear the issue when the configured API limit guarantees headroom for actuals.
    if all(api_limit + count <= inferred_quota for count in sites_per_key.values()):
        _remove_issue()


async def raise_or_clear_advanced_problems(problems: list[str], hass: HomeAssistant, entry: ConfigEntry | None = None):
    """Raise or clear advanced unknown option issues."""
    issue_registry = ir.async_get(hass)
    issue_id = _scoped(ISSUE_ADVANCED_PROBLEM, entry)
    if problems:
        problem_list = "".join([("\n* " + problem) for problem in sorted(problems)])
        issue = issue_registry.async_get_issue(DOMAIN, issue_id)
        if (
            issue is not None
            and issue.translation_placeholders is not None
            and issue.translation_placeholders.get(PROBLEMS) != problem_list
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            await hass.async_block_till_done()
        _LOGGER.debug("Raising advanced option problems issue for: %s", ", ".join(problems))
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            translation_key=ISSUE_ADVANCED_PROBLEM,
            translation_placeholders=repair_placeholders(entry, {PROBLEMS: problem_list}),
            severity=ir.IssueSeverity.ERROR,
            learn_more_url=LEARN_MORE_ADVANCED,
        )
        issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    else:
        issue_registry = ir.async_get(hass)
        issue = issue_registry.async_get_issue(DOMAIN, issue_id)
        if issue is not None:
            _LOGGER.debug("Removing advanced problems issue")
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def raise_or_clear_advanced_deprecated(
    deprecated_in_use: dict[str, str],
    hass: HomeAssistant,
    stops_working: dict[str, dt] | None = None,
    entry: ConfigEntry | None = None,
):
    """Raise or clear advanced deprecated option issues."""
    issue_id = _scoped(ISSUE_ADVANCED_DEPRECATED, entry)
    if deprecated_in_use:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            translation_key=ISSUE_ADVANCED_DEPRECATED,
            translation_placeholders=repair_placeholders(
                entry,
                {
                    OPTION: ", ".join(deprecated_in_use.keys()),
                    NEW_OPTION: ", ".join(deprecated_in_use.values()),
                    STOPS_WORKING: (
                        " ("
                        + ", ".join(
                            [
                                f"{option} stops working after {date.strftime(DT_DATE_ONLY_FORMAT)}"
                                for option, date in stops_working.items()
                                if option in deprecated_in_use
                            ]
                        )
                        + ")"
                    )
                    if stops_working
                    else "",
                },
            ),
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=LEARN_MORE_ADVANCED,
        )
    else:
        issue_registry = ir.async_get(hass)
        issue = issue_registry.async_get_issue(DOMAIN, issue_id)
        if issue is not None:
            _LOGGER.debug("Removing advanced deprecation issue")
            ir.async_delete_issue(hass, DOMAIN, issue_id)
