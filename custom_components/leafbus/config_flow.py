"""Set up a board by its address."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LeafBusClient, LeafBusConnectionError, LeafBusError
from .const import DEFAULT_HOST, DOMAIN


class LeafBusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the board's address, check it answers like a LeafBus board."""

    VERSION = 1

    async def _probe(self, host: str) -> tuple[dict[str, Any] | None, str | None]:
        client = LeafBusClient(async_get_clientsession(self.hass), host)
        try:
            info = await client.info()
        except LeafBusConnectionError:
            return None, "cannot_connect"
        except LeafBusError:
            return None, "not_leafbus"
        if not isinstance(info, dict) or "version" not in info or "panels" not in info:
            return None, "not_leafbus"
        return info, None

    @staticmethod
    def _unique_id(info: dict[str, Any], host: str) -> str:
        mac = info.get("mac")
        return mac.replace(":", "").lower() if mac else f"{info.get('hostname') or host}"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            info, error = await self._probe(host)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(self._unique_id(info, host))
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=info.get("hostname") or host, data={CONF_HOST: host}
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, DEFAULT_HOST)): str}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address of a board that is already set up."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            info, error = await self._probe(host)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(self._unique_id(info, host))
                self._abort_if_unique_id_mismatch(reason="wrong_board")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}
            ),
            errors=errors,
        )
