"""Repairs for the Solcast Solar integration."""

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    AFFIRMATION_RECONFIGURED,
    AFFIRMATION_UNCHANGED,
    AUTO_UPDATE,
    DOMAIN,
    ENTRY_ID,
    EXCEPTION_ENTRY_NOT_FOUND,
    ISSUE_RECORDS_MISSING_FIXABLE,
    LEARN_MORE,
)
from .log import get_logger
from .validators import Required, Schema

_LOGGER = get_logger(__name__)

AUTO_UPDATE_OPTIONS: list[SelectOptionDict] = [
    SelectOptionDict(label="sunrise_sunset", value="1"),
    SelectOptionDict(label="all_day", value="2"),
]


class SolcastRepair(RepairsFlow):
    """Handler for an issue fixing flow."""

    entry: ConfigEntry | None

    def __init__(self, *, entry: ConfigEntry | None) -> None:
        """Create flow."""

        self.entry = entry
        super().__init__()

    @callback
    def _async_get_placeholders(self) -> dict[str, str]:
        issue_registry = ir.async_get(self.hass)
        placeholders: dict[str, Any] = {"instance": ""}
        if issue := issue_registry.issues.get((DOMAIN, self.issue_id)):
            if issue.learn_more_url:
                placeholders[LEARN_MORE] = issue.learn_more_url
            placeholders["instance"] = (issue.translation_placeholders or {}).get("instance", "")  # Names a named entry

        return placeholders


class RecordsMissingRepairFlow(SolcastRepair):
    """Handler to enable auto-update."""

    # TODO: Remove the pylint suppression after minimum hacs.json HA version is raised to 2026.6+.
    # When that happens, the return type hint needs to be RepairsFlowResult
    # pylint: disable=home-assistant-return-type
    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the init."""

        return await self.async_step_offer_auto()

    # The return type hint will need to be RepairsFlowResult
    async def async_step_offer_auto(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the offer to enable auto-update."""

        if self.entry is None:
            return self.async_abort(reason=EXCEPTION_ENTRY_NOT_FOUND)

        if user_input is not None:
            opts = {AUTO_UPDATE: int(user_input[AUTO_UPDATE])}
            if opts[AUTO_UPDATE] == int(self.entry.options[AUTO_UPDATE]):
                return self.async_abort(reason=AFFIRMATION_UNCHANGED)
            new_options: dict[str, Any] = {**self.entry.options, **opts}
            self.hass.config_entries.async_update_entry(self.entry, options=new_options)
            return self.async_abort(reason=AFFIRMATION_RECONFIGURED)

        placeholders = self._async_get_placeholders()
        return self.async_show_form(
            step_id="offer_auto",
            data_schema=Schema(
                {
                    Required(AUTO_UPDATE, default="1"): SelectSelector(
                        SelectSelectorConfig(options=AUTO_UPDATE_OPTIONS, mode=SelectSelectorMode.DROPDOWN, translation_key="auto_update")
                    ),
                }
            ),
            description_placeholders=placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any],
) -> RepairsFlow:
    """Create flow."""

    # A named entry's issue ID carries its entry ID, so match the kind of issue, not the ID.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if issue is not None and issue.translation_key == ISSUE_RECORDS_MISSING_FIXABLE:
        entry = hass.config_entries.async_get_entry(data[ENTRY_ID]) if data.get(ENTRY_ID) and data[ENTRY_ID] != "" else None
        return RecordsMissingRepairFlow(entry=entry)

    return ConfirmRepairFlow()
