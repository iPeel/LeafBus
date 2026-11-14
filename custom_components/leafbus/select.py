"""Preset and sleep timer selects."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import TIMER_OPTIONS
from .coordinator import LeafBusConfigEntry
from .entity import LeafBusEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeafBusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        LeafBusPresetSelect(coordinator, "preset"),
        LeafBusTimerSelect(coordinator, "sleep_timer"),
    ])


class LeafBusPresetSelect(LeafBusEntity, SelectEntity):
    """The presets saved on the board. Choosing one plays it and turns the
    panels on. Shows no option while an effect plays that is not a saved
    preset (changed in the web UI, or a network stream)."""

    @property
    def options(self) -> list[str]:
        return self.coordinator.data.presets

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data.presets)

    @property
    def current_option(self) -> str | None:
        preset = self.state_data.get("preset")
        return preset if preset in self.coordinator.data.presets else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_state(preset=option)


class LeafBusTimerSelect(LeafBusEntity, SelectEntity):
    """Sleep timer. Choosing a time turns the panels on and switches them
    off after that long; 'off' cancels the timer and leaves the power as it
    is."""

    _attr_options = list(TIMER_OPTIONS)

    @property
    def current_option(self) -> str:
        st = self.state_data
        remaining = st.get("timer")
        if remaining is None:
            return "off"
        set_to = st.get("timer_set")
        for key, minutes in TIMER_OPTIONS.items():
            if minutes and minutes == set_to:
                return key
        # set to a value this list does not have (or older firmware without
        # timer_set): the shortest choice that covers what is left
        for key, minutes in TIMER_OPTIONS.items():
            if minutes and minutes >= remaining:
                return key
        return list(TIMER_OPTIONS)[-1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"minutes_remaining": self.state_data.get("timer")}

    async def async_select_option(self, option: str) -> None:
        minutes = TIMER_OPTIONS[option]
        if minutes:
            await self.coordinator.async_set_state(power=True, timer=minutes)
        else:
            await self.coordinator.async_set_state(timer=0)
