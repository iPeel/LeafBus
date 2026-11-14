"""Against a real board, opt-in:

    LEAFBUS_LIVE=192.168.1.42 pytest ha_tests/test_live.py -o asyncio_mode=auto

Adds the board through the config flow, uses every entity, checks the effect
on the board with direct requests, and puts back what was playing before.
"""
import asyncio
import os

import pytest
import pytest_socket

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.leafbus.api import LeafBusClient
from custom_components.leafbus.const import DOMAIN

LIVE = os.environ.get("LEAFBUS_LIVE")
pytestmark = pytest.mark.skipif(not LIVE, reason="set LEAFBUS_LIVE=<board address>")


async def wait_for(check, timeout=8):
    for _ in range(int(timeout * 10)):
        if await check():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_live_board(hass, socket_enabled):
    pytest_socket.socket_allow_hosts(["127.0.0.1", LIVE.split(":")[0]])
    board = LeafBusClient(async_get_clientsession(hass), LIVE)
    before = await board.state()
    presets = await board.presets()
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}, data={CONF_HOST: LIVE})
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        entry = result["result"]
        await hass.async_block_till_done()
        info = await board.info()
        if info.get("mac"):
            assert entry.unique_id == info["mac"].replace(":", "")

        power = hass.states.get("switch.leafbus_power")
        preset = hass.states.get("select.leafbus_preset")
        timer = hass.states.get("select.leafbus_sleep_timer")
        assert power and preset and timer
        assert preset.attributes["options"] == presets

        # off
        await hass.services.async_call("switch", "turn_off",
                                       {"entity_id": "switch.leafbus_power"}, blocking=True)
        assert hass.states.get("switch.leafbus_power").state == STATE_OFF
        assert (await board.state())["power"] is False

        # sleep timer: turns on and sets the timer
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.leafbus_sleep_timer", "option": "15"},
                                       blocking=True)
        st = await board.state()
        assert st["power"] is True and 14 < st["timer"] <= 15
        assert st.get("timer_set") in (15, None)       # None: firmware before timer_set
        assert hass.states.get("switch.leafbus_power").state == STATE_ON
        assert hass.states.get("select.leafbus_sleep_timer").state == "15"

        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.leafbus_sleep_timer", "option": "off"},
                                       blocking=True)
        assert (await board.state())["timer"] is None
        assert hass.states.get("select.leafbus_sleep_timer").state == "off"

        # preset
        target = next(p for p in presets if p != before.get("preset"))
        await hass.services.async_call("select", "select_option",
                                       {"entity_id": "select.leafbus_preset", "option": target},
                                       blocking=True)
        assert (await board.state())["preset"] == target
        assert hass.states.get("select.leafbus_preset").state == target

        # a change made on the board shows up with the next poll
        other = next(p for p in presets if p != target)
        await board.set_state(preset=other)
        async def shows_other():
            return hass.states.get("select.leafbus_preset").state == other
        assert await wait_for(shows_other, timeout=12)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        restore = {"brightness": before["brightness"], "timer": 0}
        if before.get("preset"):
            restore["preset"] = before["preset"]
        else:
            restore.update(effect=before["effect"], params=before["params"])
        restore["power"] = before["power"]
        await board.set_state(**restore)
