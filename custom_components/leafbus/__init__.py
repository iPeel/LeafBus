"""LeafBus: Nanoleaf Shapes panels driven by a LeafBus ESP32 board.

Talks to the board's JSON API over the local network (see firmware/web.py
in the LeafBus repository). Entities: power switch, preset select, sleep
timer select.
"""
from __future__ import annotations

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LeafBusClient, LeafBusError
from .coordinator import LeafBusConfigEntry, LeafBusCoordinator

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: LeafBusConfigEntry) -> bool:
    """Connect to the board and set up its entities."""
    client = LeafBusClient(async_get_clientsession(hass), entry.data[CONF_HOST])
    try:
        info = await client.info()
    except LeafBusError as err:
        raise ConfigEntryNotReady(f"LeafBus board at {client.host}: {err}") from err
    coordinator = LeafBusCoordinator(hass, entry, client, info)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LeafBusConfigEntry) -> bool:
    """Remove the entities."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
