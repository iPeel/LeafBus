"""Tests for custom_components/leafbus with a real Home Assistant core.

    pip install pytest-homeassistant-custom-component     (Python 3.13+)
    pytest ha_tests -o asyncio_mode=auto
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HOST = "10.0.0.42"
BASE = f"http://{HOST}"

INFO = {"version": "2.0.0", "hostname": "leafbus", "mac": "02:00:00:4c:42:01",
        "panels": 8, "fps": 20.0, "source": "effect"}
STATE = {"power": True, "brightness": 255, "effect": "rainbow", "params": {},
         "preset": "Rainbow", "source": "effect", "panels": 8, "touch": [],
         "touches": 0, "fps": 20.0, "timer": None, "timer_set": None,
         "time": None, "colors": "ff000000" * 8}
PRESETS = [{"name": "Rainbow", "effect": "rainbow", "params": {}},
           {"name": "Warm white", "effect": "solid", "params": {}},
           {"name": "Fireplace", "effect": "fire", "params": {}}]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/leafbus."""
    return


@pytest.fixture
def board(aioclient_mock):
    """A fake board: GET answers from .state / .presets, POST /api/state
    applies the change like the firmware does and answers with the state."""

    class Board:
        state = copy.deepcopy(STATE)
        presets = copy.deepcopy(PRESETS)
        info = dict(INFO)
        posts = []

        def mock(self):
            """Every reply is built when the request comes in, so tests can
            change .state / .presets / .info at any time."""
            aioclient_mock.clear_requests()
            aioclient_mock.get(f"{BASE}/api/info", side_effect=self._get(lambda: self.info))
            aioclient_mock.get(f"{BASE}/api/state", side_effect=self._get(lambda: self.state))
            aioclient_mock.get(f"{BASE}/api/presets", side_effect=self._get(lambda: self.presets))
            aioclient_mock.post(f"{BASE}/api/state", side_effect=self._post)

        @staticmethod
        def _get(body):
            async def reply(method, url, data):
                from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
                return AiohttpClientMockResponse(method, url, json=copy.deepcopy(body()))
            return reply

        async def _post(self, method, url, data):
            from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
            self.posts.append(data)
            st = self.state
            if "preset" in data:
                st["preset"] = data["preset"]
                st["power"] = True
            if "timer" in data:
                m = data["timer"]
                st["timer"] = float(m) if m else None
                st["timer_set"] = m or None
            if "power" in data:
                st["power"] = data["power"]
                if not data["power"]:
                    st["timer"] = st["timer_set"] = None
            return AiohttpClientMockResponse(method, url, json=copy.deepcopy(st))

    b = Board()
    b.mock()
    return b
