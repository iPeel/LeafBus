"""Power on / off."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LeafBusConfigEntry
from .entity import LeafBusEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeafBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([LeafBusPowerSwitch(entry.runtime_data, "power")])


class LeafBusPowerSwitch(LeafBusEntity, SwitchEntity):
    """The panels' power, as the power button in the board's web UI: off
    fades to dark, on resumes the effect that was playing."""

    @property
    def is_on(self) -> bool:
        return bool(self.state_data.get("power"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.state_data
        return {"brightness": st.get("brightness"), "effect": st.get("effect"),
                "source": st.get("source")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_state(power=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_state(power=False)
