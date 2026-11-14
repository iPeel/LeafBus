"""Polls the board and shares its state with the entities."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LeafBusClient, LeafBusError
from .const import DOMAIN, PRESETS_EVERY, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class LeafBusData:
    """What the entities read."""

    state: dict[str, Any]
    presets: list[str] = field(default_factory=list)


type LeafBusConfigEntry = ConfigEntry[LeafBusCoordinator]


class LeafBusCoordinator(DataUpdateCoordinator[LeafBusData]):
    """One poll of /api/state every SCAN_INTERVAL, the preset list now and
    then (or as soon as the board plays a preset we do not know)."""

    config_entry: LeafBusConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LeafBusConfigEntry,
        client: LeafBusClient,
        info: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.info = info
        self._polls = 0

    async def _async_update_data(self) -> LeafBusData:
        try:
            state = await self.client.state()
            presets = self.data.presets if self.data else []
            known = state.get("preset") in presets or not state.get("preset")
            if not presets or not known or self._polls % PRESETS_EVERY == 0:
                presets = await self.client.presets()
        except LeafBusError as err:
            raise UpdateFailed(str(err)) from err
        self._polls += 1
        return LeafBusData(state=state, presets=presets)

    async def async_set_state(self, **changes: Any) -> None:
        """Send a change; the board answers with its new state, which the
        entities show at once instead of waiting for the next poll."""
        try:
            state = await self.client.set_state(**changes)
        except LeafBusError as err:
            raise HomeAssistantError(f"LeafBus {self.client.host}: {err}") from err
        self.async_set_updated_data(replace(self.data, state=state))
