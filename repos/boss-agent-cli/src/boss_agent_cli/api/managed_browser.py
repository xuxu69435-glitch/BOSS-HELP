"""An isolated, persistent Chrome session owned by the local client."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlparse

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.browser_client import BrowserSession, HOME_URL
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError, classify_code_37


class ManagedBrowserError(Exception):
	def __init__(self, code: str, message: str) -> None:
		self.code = code
		super().__init__(message)


class OperationCancelled(Exception):
	code = "CANCELLED"


def browser_state(data_dir: Path) -> dict[str, Any]:
	path = data_dir / "managed-browser" / "state.json"
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		return {key: data[key] for key in ("status", "message", "checked_at") if key in data}
	except (OSError, ValueError, TypeError):
		return {"status": "needs_login", "message": "专用会话尚未登录", "checked_at": None}


def _start_playwright() -> Any:
	from playwright.sync_api import sync_playwright

	return sync_playwright().start()


def _private_directory(path: Path) -> None:
	path.mkdir(parents=True, exist_ok=True)
	if sys.platform == "win32":
		who = subprocess.run(
			["whoami", "/user", "/fo", "csv", "/nh"],
			stdin=subprocess.DEVNULL,
			capture_output=True,
			check=True,
			timeout=10,
			creationflags=subprocess.CREATE_NO_WINDOW,
		)
		sid = next(csv.reader(io.StringIO(who.stdout.decode("utf-8", errors="replace"))))[1]
		if not sid.startswith("S-1-"):
			raise ManagedBrowserError("PROFILE_PERMISSIONS", "无法确认专用会话目录权限")
		subprocess.run(
			["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
			stdin=subprocess.DEVNULL,
			capture_output=True,
			check=True,
			timeout=10,
			creationflags=subprocess.CREATE_NO_WINDOW,
		)
	else:
		path.chmod(0o700)


class ProfileLock:
	def __init__(self, path: Path) -> None:
		self.path = path
		self.handle: Any = None

	def acquire(self) -> None:
		self.handle = self.path.open("a+b")
		self.handle.seek(0, 2)
		if not self.handle.tell():
			self.handle.write(b"0")
			self.handle.flush()
		self.handle.seek(0)
		try:
			if sys.platform == "win32":
				import msvcrt

				msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
			else:
				import fcntl

				fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError as exc:
			self.handle.close()
			self.handle = None
			raise ManagedBrowserError("BROWSER_BUSY", "专用浏览器正在被另一项任务使用") from exc

	def close(self) -> None:
		if self.handle is not None:
			self.handle.close()
			self.handle = None


class ManagedBrowserSession(BrowserSession):
	def __enter__(self) -> ManagedBrowserSession:
		return self

	def __init__(
		self,
		data_dir: Path,
		*,
		delay: tuple[float, float] = (5, 8),
		logger: Any = None,
		headless: bool = True,
		cancel: Event | None = None,
	) -> None:
		super().__init__(cookies={}, user_agent="", delay=delay, logger=logger)
		self.directory = data_dir.resolve() / "managed-browser"
		self.data_dir = data_dir
		self.headless = headless
		self.cancel = cancel or Event()
		self._lock = ProfileLock(self.directory / "session.lock")
		self._ready = False

	def _state(self, status: str, message: str) -> None:
		data = {"status": status, "message": message, "checked_at": time.time()}
		temp = self.directory / "state.tmp"
		temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
		temp.replace(self.directory / "state.json")

	def _check_cancel(self) -> None:
		if self.cancel.is_set():
			raise OperationCancelled("任务已停止")

	def _ensure_started(self) -> None:
		if self._started:
			return
		self._check_cancel()
		profile = self.directory / "profile"
		if self.directory.resolve() != self.directory or profile.resolve() != profile:
			raise ManagedBrowserError("PROFILE_PATH", "专用会话目录不能链接到其他浏览器目录")
		_private_directory(self.directory)
		self._lock.acquire()
		try:
			self._pw = _start_playwright()
			self._context = self._pw.chromium.launch_persistent_context(
				str(self.directory / "profile"),
				channel="chrome",
				headless=self.headless,
				chromium_sandbox=True,
				accept_downloads=False,
				ignore_https_errors=False,
				locale="zh-CN",
				viewport={"width": 1280, "height": 900},
				timeout=30000,
			)
			self._context.set_default_timeout(15000)
			self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
			self._started = True
			self._page.goto(
				HOME_URL if self.headless else "https://www.zhipin.com/web/user/",
				wait_until="domcontentloaded",
				timeout=30000,
			)
		except Exception:
			self.close()
			raise

	def _page_flags(self) -> dict[str, Any]:
		return self._page.evaluate("""() => ({
			host: location.hostname, path: location.pathname,
			verification: /请完成验证|请先通过验证|环境存在异常|访问过于频繁|安全验证/.test(document.body?.innerText || '')
		})""")

	def _handle_result(self, result: dict[str, Any]) -> None:
		code = result.get("code")
		if code in {9, 36} or (code == 37 and classify_code_37(result) == "environment_risk"):
			self._state("blocked", "BOSS要求环境或账号校验，自动任务已停止")
			raise EnvironmentRiskError("BOSS要求环境或账号校验，已停止；请在专用登录窗口处理后明确恢复")
		if code == 37:
			self._state("needs_login", "专用登录会话已失效")
			raise ManagedBrowserError("MANAGED_LOGIN_REQUIRED", "专用会话已失效，请重新登录")

	def _verify(self) -> None:
		self._check_cancel()
		flags = self._page_flags()
		if flags.get("host") not in {"www.zhipin.com", "zhipin.com"}:
			raise ManagedBrowserError("BROWSER_PAGE", "专用窗口不在BOSS站点")
		if flags.get("verification"):
			self._state("blocked", "BOSS页面显示验证提示")
			raise EnvironmentRiskError("BOSS页面显示验证提示，未执行自动操作")
		if not any(cookie.get("name") == "wt2" and cookie.get("value") for cookie in self._context.cookies(HOME_URL)):
			self._state("needs_login", "专用会话尚未登录或已失效")
			raise ManagedBrowserError("MANAGED_LOGIN_REQUIRED", "请先登录专用浏览器")
		result = self._fetch("GET", endpoints.USER_INFO_URL)
		self._handle_result(result)
		if result.get("code") != 0:
			self._state("needs_login", "登录状态尚未通过验证")
			raise ManagedBrowserError("MANAGED_LOGIN_REQUIRED", "登录状态尚未通过验证，请完成登录后再检查")
		self._ready = True
		self._state("ready", "专用登录会话已验证")

	def _fetch(
		self, method: str, url: str, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None
	) -> dict[str, Any]:
		self._throttle.wait()
		if method == "GET":
			self._check_cancel()
		result = self._page.evaluate(
			"""async ({method,url,params,data}) => {
			try {
				const target = new URL(url);
				for (const [key,value] of Object.entries(params)) if (value !== null) target.searchParams.set(key,String(value));
				const options = {method,credentials:'include',signal:AbortSignal.timeout(25000),headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}};
				if(method === 'POST') {options.headers['Content-Type']='application/x-www-form-urlencoded';options.body=new URLSearchParams(data).toString();}
				const response = await fetch(target,options);
				return await response.json();
			} catch { return {code:-1}; }
		}""",
			{"method": method, "url": url, "params": params or {}, "data": data or {}},
		)
		self._throttle.mark()
		if not isinstance(result, dict) or result.get("code") == -1:
			raise ManagedBrowserError("NETWORK_UNCERTAIN", "请求未取得可确认的响应；不会自动重试发送")
		return result

	def prepare(self) -> None:
		if self._ready:
			return
		state = browser_state(self.data_dir)
		if state["status"] == "blocked":
			raise EnvironmentRiskError("专用会话仍处于校验暂停状态，请先处理验证并明确恢复")
		if state["status"] != "ready":
			raise ManagedBrowserError("MANAGED_LOGIN_REQUIRED", "请先在控制台登录专用浏览器")
		self._ensure_started()
		self._verify()

	def _check_login_page(self) -> None:
		if self._page.is_closed():
			raise ManagedBrowserError("LOGIN_WINDOW_CLOSED", "专用登录窗口已关闭")
		if urlparse(self._page.url).hostname not in {"www.zhipin.com", "zhipin.com"}:
			message = "BOSS登录页退回空白页或离开站点，本次登录已停止。请先在普通Chrome确认BOSS能正常打开；不会自动重试。"
			self._state("needs_login", message)
			raise ManagedBrowserError("LOGIN_PAGE_UNAVAILABLE", message)

	def login(self, confirmed: Event, *, timeout: float = 300) -> None:
		if self.headless:
			raise ValueError("Login requires the visible dedicated browser")
		self._ensure_started()
		try:
			self._page.wait_for_function(
				"() => location.hostname !== 'www.zhipin.com' && location.hostname !== 'zhipin.com' || !!document.body?.innerText.trim()",
				timeout=15000,
			)
		except Exception as exc:
			self._check_login_page()
			message = "BOSS登录页未能加载出内容，本次登录已停止；不会自动重试。"
			self._state("needs_login", message)
			raise ManagedBrowserError("LOGIN_PAGE_UNAVAILABLE", message) from exc
		self._check_login_page()
		self._state("waiting_login", "等待在专用窗口完成登录或验证")
		deadline = time.monotonic() + timeout
		try:
			while time.monotonic() < deadline:
				self._check_cancel()
				self._check_login_page()
				if confirmed.is_set():
					confirmed.clear()
					try:
						self._verify()
						return
					except ManagedBrowserError as exc:
						if exc.code != "MANAGED_LOGIN_REQUIRED":
							raise
						self._state("waiting_login", "尚未检测到有效登录。请先在BOSS窗口完成登录，再点击完成登录。")
				# Keep Playwright events flowing while awaiting a human action.
				self._page.wait_for_timeout(250)
			raise ManagedBrowserError("LOGIN_TIMEOUT", "登录等待已结束，尚未保存已验证状态")
		finally:
			if browser_state(self.data_dir)["status"] == "waiting_login":
				self._state("needs_login", "登录尚未完成")

	def request(
		self, method: str, url: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None
	) -> dict[str, Any]:
		if method == "GET":
			self._check_cancel()
		parsed = urlparse(url)
		if parsed.scheme != "https" or parsed.hostname != "www.zhipin.com":
			raise ValueError("Managed browser requests are restricted to BOSS")
		self.prepare()
		result = self._fetch(method, url, params=params, data=data)
		self._handle_result(result)
		return result

	def close(self) -> None:
		try:
			if self._context is not None:
				try:
					self._context.close()
				except Exception:
					pass
		finally:
			self._context = None
			try:
				if self._pw is not None:
					try:
						self._pw.stop()
					except Exception:
						pass
			finally:
				self._pw = None
				self._ready = self._started = False
				self._lock.close()
