"""Adding a board through the UI."""
import aiohttp

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leafbus.const import DOMAIN

from .conftest import BASE, HOST


async def test_add_board(hass, board):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({})[CONF_HOST] == "leafbus.local"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "leafbus"
    assert result["data"] == {CONF_HOST: HOST}
    assert result["result"].unique_id == "0200004c4201"


async def test_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/info", exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_HOST: HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_not_a_leafbus_board(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/info", json={"hello": "world"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_HOST: HOST})
    assert result["errors"] == {"base": "not_leafbus"}


async def test_same_board_new_address_updates_entry(hass, board, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="0200004c4201",
                            data={CONF_HOST: "10.0.0.7"}, title="leafbus")
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_HOST: HOST})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST
