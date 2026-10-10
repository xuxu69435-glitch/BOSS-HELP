"""Explicit existing-browser requirements, without login or temporary profiles."""

import re
from typing import Any

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.bridge.client import BROWSER_FAILURE_MESSAGES, BridgeClient, BridgeRequestError


class BrowserSessionNotFound(Exception):
	code = "BROWSER_SESSION_NOT_FOUND"


class BrowserSearchPageRequired(BrowserSessionNotFound):
	code = "BROWSER_SEARCH_PAGE_REQUIRED"

	def __init__(self, path: str) -> None:
		self.diagnostic = {"source": "page_context", "request_sent": False, "page_path": path}
		page = "聊天页" if path.rstrip("/") == "/web/geek/chat" else "非职位搜索页"
		super().__init__(f"当前连接的是BOSS{page}，未发起搜索请求。请在BOSS当前标签页打开“职位”，确认网页能正常搜索后再手动复查；切换页面不代表接口已经恢复。")


class BrowserNativeCollectionError(RuntimeError):
	def __init__(self, code: str, message: str, diagnostic: dict[str, Any]) -> None:
		self.code = code
		self.diagnostic = diagnostic
		super().__init__(message)


BROWSER_CONNECT_ACTION = (
	"连接BOSS Agent Bridge扩展，已登录的BOSS首页或职位页均可；不需要一直保持前台，不会自动要求重新登录。"
)


def require_existing_boss_tab(*, for_search: bool = False) -> None:
	"""Check the selected tab before creating a batch or reserving a send attempt."""
	bridge = BridgeClient(max_retries=1)
	if not bridge.is_extension_connected():
		raise BrowserSessionNotFound("浏览器扩展未连接。" + BROWSER_CONNECT_ACTION)
	result = bridge.send_command(
		"inspect",
		workspace="boss",
	)
	if not result.ok or not isinstance(result.data, dict):
		if getattr(result, "code", "") in BROWSER_FAILURE_MESSAGES:
			raise BridgeRequestError(result)
		detail = "扩展暂时无法连接BOSS页面；这不能判定为你关闭了网页。"
		if "Multiple BOSS tabs" in str(result.error):
			detail = "发现多个BOSS页面，暂时无法确定目标；请选中要使用的BOSS页面。"
		elif "No tab with id:" in str(result.error):
			detail = "扩展记住的旧标签页编号已失效，不代表你关闭了网页；请重新加载已更新的扩展。"
		raise BrowserSessionNotFound(detail + "尚未请求招聘平台。")
	data = result.data
	if data.get("hostPermissionGranted") is False:
		raise BrowserSessionNotFound("BOSS扩展缺少持续的站点访问权限，请在Chrome扩展详情中允许访问zhipin.com后重新连接；尚未请求平台。")
	if data.get("host") not in {"www.zhipin.com", "zhipin.com"}:
		raise BrowserSessionNotFound("所选标签页不是BOSS页面，已停止。")
	if data.get("verificationVisible"):
		raise EnvironmentRiskError.from_page(data)
	path = str(data.get("path", ""))
	if re.search(r"/(?:login|logout|signin|signup|register|verify|captcha|security|auth)(?:/|$)", path, re.I):
		raise BrowserSessionNotFound("当前BOSS页面是登录或验证页面，已停止自动化请求。")
	is_home = path == "/" or re.fullmatch(r"/[a-z]{2,32}/?", path)
	if not (is_home or path.startswith(("/web/geek/", "/job_detail/"))):
		raise BrowserSessionNotFound("当前页面不是支持的BOSS首页或职位页，已停止自动化请求。")
	if for_search and path.rstrip("/") not in {"/web/geek/job", "/web/geek/jobs"}:
		raise BrowserSearchPageRequired(path)
