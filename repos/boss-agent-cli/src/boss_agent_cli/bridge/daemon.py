"""Browser Bridge daemon — HTTP + WebSocket 服务。

接收 CLI 的 HTTP 命令，转发给 Chrome 扩展（WebSocket），返回结果。
首次浏览器命令时自动启动，空闲 4h 后自动退出。

依赖 aiohttp（可选依赖，仅 bridge extra 安装）。
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PID_FILE = Path.home() / ".boss-agent" / "bridge" / "daemon.pid"
_LOG_FILE = Path.home() / ".boss-agent" / "bridge" / "daemon.log"


def trusted_extension_origin(origin: str) -> bool:
	return re.fullmatch(r"chrome-extension://[a-p]{32}", origin) is not None


def _ensure_dirs() -> None:
	_PID_FILE.parent.mkdir(parents=True, exist_ok=True)


def is_daemon_running() -> bool:
	"""检查 daemon 是否在运行。"""
	if not _PID_FILE.exists():
		return False
	try:
		pid = int(_PID_FILE.read_text().strip())
		os.kill(pid, 0)
		return True
	except (OSError, ValueError):
		_PID_FILE.unlink(missing_ok=True)
		return False


def get_daemon_pid() -> int | None:
	"""获取 daemon PID，不运行则返回 None。"""
	if not _PID_FILE.exists():
		return None
	try:
		pid = int(_PID_FILE.read_text().strip())
		os.kill(pid, 0)
		return pid
	except (OSError, ValueError):
		_PID_FILE.unlink(missing_ok=True)
		return None


def stop_daemon() -> bool:
	"""停止 daemon 进程。"""
	pid = get_daemon_pid()
	if pid is None:
		return False
	try:
		os.kill(pid, signal.SIGTERM)
		for _ in range(20):
			try:
				os.kill(pid, 0)
				time.sleep(0.1)
			except OSError:
				break
		_PID_FILE.unlink(missing_ok=True)
		return True
	except OSError:
		_PID_FILE.unlink(missing_ok=True)
		return False


def start_daemon_background() -> int | None:
	"""在后台启动 daemon 进程，返回实际 daemon PID。跨平台兼容。"""
	from boss_agent_cli.bridge.client import BridgeClient

	client = BridgeClient(max_retries=1)
	status = client.status()
	if status and status.get("ok"):
		return int(status["pid"])

	_ensure_dirs()

	# 跨平台后台启动：用 subprocess.Popen 替代 os.fork
	kwargs: dict[str, Any] = {}
	if sys.platform == "win32":
		# Windows: DETACHED_PROCESS 标志
		kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
	else:
		# Unix: 新会话脱离终端
		kwargs["start_new_session"] = True

	with open(_LOG_FILE, "a") as log_fd:
		proc = subprocess.Popen(
			[sys.executable, "-m", "boss_agent_cli.bridge.daemon", "--serve"],
			stdout=log_fd,
			stderr=log_fd,
			stdin=subprocess.DEVNULL,
			**kwargs,
		)

	# Check the service itself: os.kill(pid, 0) is not a safe probe on Windows.
	for _ in range(20):
		time.sleep(0.25)
		status = client.status()
		if status and status.get("ok"):
			return int(status["pid"])

	# fallback: 返回 Popen 的 PID
	return proc.pid


async def _extension_hello(ws: Any, *, timeout: float = 5.0) -> str:
	from aiohttp import WSMsgType

	message = await asyncio.wait_for(ws.receive(), timeout=timeout)
	if message.type != WSMsgType.TEXT:
		raise ValueError("Extension hello required")
	data = json.loads(message.data)
	version = data.get("version") if isinstance(data, dict) else None
	if (
		not isinstance(data, dict)
		or data.get("type") != "hello"
		or not isinstance(version, str)
		or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){2,3}", version)
	):
		raise ValueError("Invalid extension hello")
	return version


async def _extension_keepalive(ws: Any, *, interval: float = 20.0) -> None:
	# Application messages keep MV3 workers active; this never touches a BOSS tab.
	while not ws.closed:
		await asyncio.sleep(interval)
		if not ws.closed:
			await ws.send_json({"type": "ping"})


async def _run_daemon() -> None:
	"""daemon 主循环：统一 aiohttp HTTP + WebSocket 服务。"""
	from aiohttp import web

	from boss_agent_cli.bridge.protocol import (
		BRIDGE_HOST,
		BRIDGE_PORT,
		DAEMON_IDLE_TIMEOUT,
		DAEMON_WS_PATH,
		DAEMON_PING_PATH,
		DAEMON_STATUS_PATH,
		DAEMON_COMMAND_PATH,
	)

	start_time = time.time()
	ext_ws: Any = None
	ext_version: str | None = None
	last_activity = time.time()
	pending_commands: dict[str, "asyncio.Future[Any]"] = {}

	# ── HTTP handlers ─────────────────────────────────────────────

	async def handle_ping(request: Any) -> Any:
		nonlocal last_activity
		last_activity = time.time()
		return web.json_response({"ok": True})

	async def handle_status(request: Any) -> Any:
		nonlocal last_activity
		last_activity = time.time()
		return web.json_response(
			{
				"ok": True,
				"extensionConnected": ext_ws is not None and not ext_ws.closed and ext_version is not None,
				"extensionVersion": ext_version,
				"pid": os.getpid(),
				"uptime": int(time.time() - start_time),
			}
		)

	async def handle_command(request: Any) -> Any:
		nonlocal last_activity
		last_activity = time.time()
		origin = request.headers.get("Origin", "")
		if origin:
			return web.json_response({"ok": False, "error": "Untrusted origin"}, status=403)
		if request.content_type != "application/json":
			return web.json_response({"ok": False, "error": "JSON content type required"}, status=415)

		if ext_ws is None or ext_ws.closed or ext_version is None:
			return web.json_response(
				{"id": "", "ok": False, "error": "Extension not connected"},
				status=503,
			)

		try:
			cmd = await request.json()
		except (ValueError, KeyError):
			return web.json_response(
				{"id": "", "ok": False, "error": "Invalid JSON"},
				status=400,
			)
		if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str) or not cmd["id"]:
			return web.json_response({"ok": False, "error": "Invalid command id"}, status=400)
		if cmd.get("workspace", "boss") != "boss":
			return web.json_response({"ok": False, "error": "Only BOSS workspace is supported"}, status=400)

		cmd_id = cmd.get("id", "")
		future = asyncio.get_event_loop().create_future()
		pending_commands[cmd_id] = future

		try:
			await ext_ws.send_json(cmd)
			result = await asyncio.wait_for(future, timeout=30.0)
			return web.json_response(result)
		except asyncio.TimeoutError:
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": "Command timed out (30s)"},
				status=504,
			)
		except Exception as e:
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": f"Send failed: {e}"},
				status=502,
			)
		finally:
			pending_commands.pop(cmd_id, None)

	# ── WebSocket handler（Chrome 扩展连接） ──────────────────────

	async def ws_handler(request: Any) -> Any:
		nonlocal ext_ws, ext_version
		if not trusted_extension_origin(request.headers.get("Origin", "")):
			return web.json_response({"ok": False, "error": "Extension origin required"}, status=403)
		if ext_ws is not None:
			return web.json_response({"ok": False, "error": "An extension is already connected"}, status=409)
		ws = web.WebSocketResponse()
		await ws.prepare(request)
		ext_ws = ws
		ext_version = None
		keepalive = None
		print("[bridge] 扩展已连接", flush=True)

		try:
			ext_version = await _extension_hello(ws)
			print(f"[bridge] 扩展版本: {ext_version}", flush=True)
			await ws.send_json({"type": "ready"})
			keepalive = asyncio.create_task(_extension_keepalive(ws))
			async for msg in ws:
				if msg.type == web.WSMsgType.TEXT:
					data = json.loads(msg.data)
					if not isinstance(data, dict):
						continue
					msg_type = data.get("type")

					if msg_type in {"hello", "pong"}:
						continue

					if msg_type == "log":
						level = data.get("level", "info")
						log_msg = data.get("msg", "")
						print(f"[bridge:ext:{level}] {log_msg}", flush=True)
						continue

					# 命令结果
					cmd_id = data.get("id")
					if cmd_id and cmd_id in pending_commands and not pending_commands[cmd_id].done():
						pending_commands[cmd_id].set_result(data)
				elif msg.type == web.WSMsgType.ERROR:
					print(f"[bridge] WS error: {ws.exception()}", flush=True)
		except (asyncio.TimeoutError, ValueError):
			print("[bridge] 扩展握手超时或协议无效，关闭未就绪连接", flush=True)
		finally:
			if keepalive is not None:
				keepalive.cancel()
				await asyncio.gather(keepalive, return_exceptions=True)
			await ws.close()
			if ext_ws is ws:
				ext_ws = None
				ext_version = None
				# 扩展断连：取消所有 pending 命令
				for cmd_id, fut in list(pending_commands.items()):
					if not fut.done():
						fut.set_result({"id": cmd_id, "ok": False, "error": "Extension disconnected"})
			print("[bridge] 扩展已断开", flush=True)

		return ws

	# ── 启动统一服务 ──────────────────────────────────────────────

	@web.middleware
	async def local_host_only(request: Any, handler: Any) -> Any:
		if request.host.split(":", 1)[0] not in {"127.0.0.1", "localhost"}:
			return web.json_response({"ok": False, "error": "Local host required"}, status=403)
		return await handler(request)

	app = web.Application(middlewares=[local_host_only])
	app.router.add_get(DAEMON_PING_PATH, handle_ping)
	app.router.add_get(DAEMON_STATUS_PATH, handle_status)
	app.router.add_post(DAEMON_COMMAND_PATH, handle_command)
	app.router.add_get(DAEMON_WS_PATH, ws_handler)

	runner = web.AppRunner(app, access_log=None)
	await runner.setup()
	site = web.TCPSite(runner, BRIDGE_HOST, BRIDGE_PORT)
	await site.start()
	print(f"[bridge] 服务启动: http://{BRIDGE_HOST}:{BRIDGE_PORT}", flush=True)
	print(f"[bridge] PID: {os.getpid()}, 空闲超时: {DAEMON_IDLE_TIMEOUT}s", flush=True)

	# 写 PID 文件（服务启动后再写，确保端口可用）
	_PID_FILE.write_text(str(os.getpid()))

	# ── 空闲超时检查 ──────────────────────────────────────────────

	try:
		while True:
			await asyncio.sleep(60)
			idle = time.time() - last_activity
			if idle > DAEMON_IDLE_TIMEOUT and ext_ws is None:
				print(f"[bridge] 空闲 {int(idle)}s 且无扩展连接，自动退出", flush=True)
				break
	except asyncio.CancelledError:
		pass
	finally:
		_PID_FILE.unlink(missing_ok=True)
		await runner.cleanup()
		print("[bridge] daemon 已停止", flush=True)


# ── CLI 入口：python -m boss_agent_cli.bridge.daemon --serve ────────

if __name__ == "__main__":
	if "--serve" in sys.argv:
		import argparse

		parser = argparse.ArgumentParser()
		parser.add_argument("--serve", action="store_true")
		parser.add_argument("--data-dir", type=Path)
		parser.add_argument("--log-to-file", action="store_true")
		args = parser.parse_args()
		if args.data_dir is not None:
			_PID_FILE = args.data_dir.resolve() / "bridge" / "daemon.pid"
			_LOG_FILE = args.data_dir.resolve() / "bridge" / "daemon.log"
		_ensure_dirs()
		if args.log_to_file:
			log_stream = _LOG_FILE.open("a", encoding="utf-8", buffering=1)
			sys.stdout = log_stream
			sys.stderr = log_stream
		try:
			asyncio.run(_run_daemon())
		except KeyboardInterrupt:
			pass
		finally:
			_PID_FILE.unlink(missing_ok=True)
