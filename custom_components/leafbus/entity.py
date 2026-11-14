"""Base entity: one device per board."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LeafBusCoordinator


class LeafBusEntity(CoordinatorEntity[LeafBusCoordinator]):
    """Shares the board's device entry and the coordinator's data."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LeafBusCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        info = coordinator.info
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_translation_key = key
        mac = info.get("mac")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name=entry.title,
            manufacturer="LeafBus",
            model=f"Nanoleaf Shapes controller, {info.get('panels', '?')} panels",
            sw_version=info.get("version"),
            configuration_url=coordinator.client.base,
        )

    @property
    def state_data(self) -> dict:
        return self.coordinator.data.state
