import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType

from boss_agent_cli.bridge.daemon import _extension_hello, _extension_keepalive


@pytest.mark.parametrize(
	"value",
	[
		{},
		{"type": "hello"},
		{"type": "hello", "version": None},
		{"type": "log", "version": "1.1.0"},
		{"type": "hello", "version": "unknown"},
		[],
	],
)
def test_invalid_hello_cannot_claim_connection(value):
	ws = Mock(receive=AsyncMock(return_value=SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(value))))
	with pytest.raises(ValueError):
		asyncio.run(_extension_hello(ws))


def test_hello_returns_the_actual_extension_version():
	ws = Mock(
		receive=AsyncMock(return_value=SimpleNamespace(type=WSMsgType.TEXT, data='{"type":"hello","version":"1.1.0"}'))
	)
	assert asyncio.run(_extension_hello(ws)) == "1.1.0"


def test_missing_hello_times_out():
	async def delayed_receive():
		await asyncio.sleep(1)

	ws = Mock(receive=delayed_receive)
	with pytest.raises(asyncio.TimeoutError):
		asyncio.run(_extension_hello(ws, timeout=0.001))


def test_heartbeat_contains_no_page_or_platform_operation():
	ws = Mock(closed=False)

	async def sent(_):
		ws.closed = True

	ws.send_json = AsyncMock(side_effect=sent)
	asyncio.run(_extension_keepalive(ws, interval=0))
	ws.send_json.assert_awaited_once_with({"type": "ping"})
