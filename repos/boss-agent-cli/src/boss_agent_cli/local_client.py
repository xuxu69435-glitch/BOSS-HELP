"""Loopback-only job review client. No external actions run on page load."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.api.managed_browser import _private_directory


ASSETS = Path(__file__).with_name("client_assets")
FILES = {
	"/": ("index.html", "text/html; charset=utf-8"),
	"/assets/client.js": ("client.js", "text/javascript; charset=utf-8"),
	"/assets/profile.js": ("profile.js", "text/javascript; charset=utf-8"),
	"/assets/client.css": ("client.css", "text/css; charset=utf-8"),
	"/assets/lucide.min.js": ("lucide.min.js", "text/javascript; charset=utf-8"),
}


class ClientServer(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = False

	def __init__(self, address: tuple[str, int], controller: ClientController) -> None:
		if address[0] != "127.0.0.1":
			raise ValueError("The client may only bind to loopback")
		self.controller = controller
		self.token = secrets.token_urlsafe(32)
		super().__init__(address, ClientHandler)
		self.origin = f"http://127.0.0.1:{self.server_port}"
		self.data_id = hashlib.sha256(str(controller.data_dir).casefold().encode()).hexdigest()[:16]


class ClientHandler(BaseHTTPRequestHandler):
	server: ClientServer

	def setup(self) -> None:
		super().setup()
		self.connection.settimeout(10)

	def log_message(self, format: str, *args: Any) -> None:
		pass

	def _write(self, status: int, value: Any, content_type: str = "application/json; charset=utf-8") -> None:
		body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("X-Frame-Options", "DENY")
		self.send_header("Referrer-Policy", "no-referrer")
		self.send_header(
			"Content-Security-Policy",
			"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
		)
		try:
			self.end_headers()
			self.wfile.write(body)
		except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
			pass

	def _guard(self, *, write: bool = False) -> bool:
		if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
			self._write(403, {"error": "无效的本机地址"})
			return False
		origin = self.headers.get("Origin")
		supplied = self.headers.get("X-Client-Token", "")
		authorized = supplied.isascii() and secrets.compare_digest(supplied, self.server.token)
		if (origin is not None and origin != self.server.origin) or self.headers.get("Sec-Fetch-Site") == "cross-site":
			self._write(403, {"error": "禁止跨站访问"})
			return False
		if write and (
			origin != self.server.origin
			or not authorized
			or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
		):
			self._write(403, {"error": "操作来源校验失败"})
			return False
		if urlparse(self.path).path.startswith("/api/") and not authorized:
			self._write(403, {"error": "客户端授权缺失或已失效，请通过本机启动入口重新打开"})
			return False
		return True

	def do_GET(self) -> None:
		if not self._guard():
			return
		url = urlparse(self.path)
		try:
			if url.path == "/health":
				challenge = parse_qs(url.query).get("challenge", [""])[0]
				if len(challenge) > 128:
					raise ValueError("Invalid challenge")
				proof = (
					hmac.new(self.server.token.encode(), challenge.encode(), hashlib.sha256).hexdigest()
					if challenge
					else None
				)
				self._write(
					200,
					{
						"app": "boss-local-client",
						"data_id": self.server.data_id,
						"pid": os.getpid(),
						"version": "0.1.0",
						"proof": proof,
					},
				)
			elif url.path == "/favicon.ico":
				self._write(204, b"", "image/x-icon")
			elif url.path == "/api/state":
				self._write(200, self.server.controller.state())
			elif url.path == "/api/batch":
				self._write(200, self.server.controller.batch(parse_qs(url.query).get("id", [""])[0]))
			elif url.path == "/api/events":
				self._write(200, self.server.controller.events())
			elif url.path == "/api/profile":
				self._write(200, self.server.controller.profile())
			elif url.path in FILES:
				filename, mime = FILES[url.path]
				data = (ASSETS / filename).read_bytes()
				self._write(200, data, mime)
			else:
				self._write(404, {"error": "页面不存在"})
		except (ValueError, KeyError):
			self._write(400, {"error": "批次不存在或参数无效"})
		except Exception:
			self._write(500, {"error": "读取失败，请查看本机日志"})

	def do_POST(self) -> None:
		try:
			length = int(self.headers.get("Content-Length", "0"))
			if not 0 < length <= 65536:
				raise ValueError("请求内容过大或为空")
			# Drain a bounded body before rejecting it, avoiding Windows TCP resets.
			self.connection.settimeout(5)
			raw = self.rfile.read(length)
			if not self._guard(write=True):
				return
			body = json.loads(raw)
			if not isinstance(body, dict):
				raise ValueError("参数必须为对象")
			path = urlparse(self.path).path
			controller = self.server.controller
			if path == "/api/settings":
				controller.save_settings(body)
			elif path == "/api/profile":
				self._write(200, controller.save_profile(body))
				return
			elif path == "/api/review":
				controller.review(body)
			elif path == "/api/edit":
				controller.edit(body)
			elif path == "/api/stop":
				controller.stop()
			elif path == "/api/shutdown":
				controller.stop()
				self._write(200, {"ok": True})
				threading.Thread(target=self.server.shutdown, daemon=True).start()
				return
			elif path in {"/api/connect", "/api/login", "/api/check", "/api/check_now", "/api/prepare", "/api/resume", "/api/draft", "/api/send"}:
				controller.start("connect" if path == "/api/login" else path.removeprefix("/api/"), body)
			else:
				self._write(404, {"error": "操作不存在"})
				return
			self._write(200, {"ok": True})
		except (ValueError, KeyError, TypeError) as exc:
			self._write(400, {"error": str(exc)[:300]})
		except Exception:
			self._write(500, {"error": "操作失败，请查看本机日志"})


def main() -> None:
	parser = argparse.ArgumentParser(description="Local BOSS review client")
	parser.add_argument("--data-dir", required=True, type=Path)
	parser.add_argument("--settings", required=True, type=Path)
	parser.add_argument("--port", type=int, default=19827)
	args = parser.parse_args()
	log_dir = args.data_dir / "client-control"
	_private_directory(log_dir)
	sys.stdout = sys.stderr = (log_dir / "server.log").open("a", encoding="utf-8", buffering=1)
	controller = ClientController(args.data_dir, args.settings)
	server = None
	for port in range(args.port, args.port + 20):
		try:
			server = ClientServer(("127.0.0.1", port), controller)
			break
		except OSError:
			continue
	if server is None:
		raise RuntimeError("No available local port")
	runtime = log_dir / "runtime.json"
	runtime.parent.mkdir(parents=True, exist_ok=True)
	temp = runtime.with_suffix(".tmp")
	temp.write_text(
		json.dumps({"url": server.origin, "data_id": server.data_id, "pid": os.getpid(), "launch_token": server.token}),
		encoding="utf-8",
	)
	temp.replace(runtime)
	try:
		server.serve_forever()
	finally:
		server.server_close()
		controller.close()


if __name__ == "__main__":
	try:
		main()
	except Exception as exc:
		if "--data-dir" in sys.argv:
			fallback = Path(sys.argv[sys.argv.index("--data-dir") + 1]) / "client-startup-error.log"
			fallback.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
		raise
