"""Power switch, preset select and sleep timer select against a fake board."""
from datetime import timedelta

import aiohttp
import pytest

from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.leafbus.const import DOMAIN

from .conftest import BASE, HOST

POWER = "switch.leafbus_power"
PRESET = "select.leafbus_preset"
TIMER = "select.leafbus_sleep_timer"


async def setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="0200004c4201",
                            data={CONF_HOST: HOST}, title="leafbus")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_and_device(hass, board):
    entry = await setup(hass)
    assert hass.states.get(POWER).state == STATE_ON
    preset = hass.states.get(PRESET)
    assert preset.state == "Rainbow"
    assert preset.attributes["options"] == ["Rainbow", "Warm white", "Fireplace"]
    timer = hass.states.get(TIMER)
    assert timer.state == "off"
    assert timer.attributes["options"] == ["off", "15", "30", "60", "120", "240", "480"]

    dev = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.unique_id), entry.entry_id)
    assert dev.manufacturer == "LeafBus"
    assert dev.sw_version == "2.0.0"
    assert (dr.CONNECTION_NETWORK_MAC, "02:00:00:4c:42:01") in dev.connections


async def test_power_switch(hass, board):
    await setup(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": POWER}, blocking=True)
    assert board.posts[-1] == {"power": False}
    assert hass.states.get(POWER).state == STATE_OFF       # from the reply, no poll needed
    await hass.services.async_call("switch", "turn_on", {"entity_id": POWER}, blocking=True)
    assert board.posts[-1] == {"power": True}
    assert hass.states.get(POWER).state == STATE_ON


async def test_sleep_timer_turns_on_and_sets_timer(hass, board):
    await setup(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": POWER}, blocking=True)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": TIMER, "option": "30"}, blocking=True)
    assert board.posts[-1] == {"power": True, "timer": 30}
    assert hass.states.get(POWER).state == STATE_ON
    assert hass.states.get(TIMER).state == "30"

    # the board counts down; the select keeps showing what was chosen
    board.state["timer"] = 12.5
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert hass.states.get(TIMER).state == "30"
    assert hass.states.get(TIMER).attributes["minutes_remaining"] == 12.5

    await hass.services.async_call("select", "select_option",
                                   {"entity_id": TIMER, "option": "off"}, blocking=True)
    assert board.posts[-1] == {"timer": 0}
    assert hass.states.get(TIMER).state == "off"
    assert hass.states.get(POWER).state == STATE_ON         # cancelling leaves the light on


async def test_timer_set_elsewhere(hass, board):
    """A timer set in the web UI to a length not in the list, or by older
    firmware without timer_set, shows the shortest choice covering it."""
    board.state.update(timer=50.0, timer_set=None)
    await setup(hass)
    assert hass.states.get(TIMER).state == "60"


async def test_preset_select(hass, board):
    await setup(hass)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": PRESET, "option": "Fireplace"}, blocking=True)
    assert board.posts[-1] == {"preset": "Fireplace"}
    assert hass.states.get(PRESET).state == "Fireplace"


async def test_custom_effect_shows_no_preset(hass, board):
    board.state.update(preset=None, effect="plasma")
    await setup(hass)
    assert hass.states.get(PRESET).state == "unknown"


async def test_new_preset_on_board_is_picked_up(hass, board):
    await setup(hass)
    board.presets.append({"name": "Party", "effect": "mosaic", "params": {}})
    board.state["preset"] = "Party"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert hass.states.get(PRESET).state == "Party"
    assert "Party" in hass.states.get(PRESET).attributes["options"]


async def test_board_offline(hass, board, aioclient_mock):
    await setup(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/api/state", exc=aiohttp.ClientConnectionError())
    aioclient_mock.post(f"{BASE}/api/state", exc=aiohttp.ClientConnectionError())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    for eid in (POWER, PRESET, TIMER):
        assert hass.states.get(eid).state == STATE_UNAVAILABLE
    board.mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    assert hass.states.get(POWER).state == STATE_ON


async def test_setup_retries_while_board_is_away(hass, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/info", exc=aiohttp.ClientConnectionError())
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", data={CONF_HOST: HOST})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state.name == "SETUP_RETRY"


async def test_unload(hass, board):
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state.name == "NOT_LOADED"
