import atexit
from typing import Any

from boss_agent_cli.api import endpoints
from boss_agent_cli.api._base_client import _BaseHttpClient
from boss_agent_cli.api.httpx_helpers import make_client_registry
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError, raise_if_environment_risk

# atexit safeguard: close any BossClient instances not explicitly closed
_OPEN_CLIENTS, _close_open_clients = make_client_registry()


atexit.register(_close_open_clients)


class AuthError(Exception):
	def __init__(self, message: str = "", *, diagnostic: dict[str, Any] | None = None) -> None:
		self.diagnostic = diagnostic or {}
		super().__init__(message)


class GreetingDeliveryUnverified(NotImplementedError):
	code = "GREETING_TEXT_UNVERIFIED"


class AccountRiskError(Exception):
	"""BOSS 直聘风控拦截（code 36）：检测到异常行为。"""

	def __init__(self, message: str = "", is_cdp: bool = False, *, diagnostic: dict[str, Any] | None = None):
		self.is_cdp = is_cdp
		self.diagnostic = diagnostic or {"source": "unknown"}
		super().__init__(message)


class BossClient(_BaseHttpClient):
	"""Hybrid API client: browser channel for high-risk ops, httpx for low-risk ops."""

	_BASE_URL = endpoints.BASE_URL
	_DEFAULT_HEADERS = endpoints.DEFAULT_HEADERS
	_REFERER_MAP = endpoints.REFERER_MAP
	_AUTH_ERROR_CLS = AuthError
	_CODE_STOKEN_EXPIRED = endpoints.CODE_STOKEN_EXPIRED
	_CODE_RATE_LIMITED = endpoints.CODE_RATE_LIMITED

	def _register(self) -> None:
		_OPEN_CLIENTS.add(self)

	def _unregister(self) -> None:
		_OPEN_CLIENTS.discard(self)

	# ── Browser request (high-risk ops) ──────────────────────────────

	def _browser_request(
		self, method: str, url: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None
	) -> dict[str, Any]:
		result = self._get_browser().request(method, url, params=params, data=data)
		meta = result.get("_bridge_meta")
		if result.get("code") == "BRIDGE_SEARCH_PAGE_REQUIRED" and isinstance(meta, dict) and meta.get("source") == "page_context" and meta.get("request_sent") is False:
			from boss_agent_cli.api.browser_source import BrowserSearchPageRequired

			raise BrowserSearchPageRequired(str(meta.get("page_path", "")))
		if result.get("code") == "BRIDGE_LOGIN_REQUIRED" and isinstance(meta, dict) and meta.get("source") == "page_login" and meta.get("request_sent") is False:
			raise AuthError("当前BOSS页面需要登录，未发起本次接口请求。", diagnostic={"source": "page_login", "request_sent": False})
		if str(result.get("code", "")).startswith("BRIDGE_NATIVE_") and isinstance(meta, dict) and meta.get("transport") == "native-page":
			from boss_agent_cli.api.browser_source import BrowserNativeCollectionError

			diagnostic = {key: meta[key] for key in ("source", "transport", "request_sent", "http_status", "endpoint") if key in meta}
			raise BrowserNativeCollectionError(str(result["code"]), str(result.get("message") or "网页采集尚未完成"), diagnostic)
		actual_endpoint = str(meta.get("endpoint") or url) if isinstance(meta, dict) and meta.get("transport") == "native-page" else url
		raise_if_environment_risk(result, endpoint=actual_endpoint)
		code = result.get("code")
		if code == endpoints.CODE_ACCOUNT_RISK:
			msg = result.get("message", "账户存在异常行为")
			browser = self._get_browser()
			is_cdp = getattr(browser, "_is_cdp", False)
			mode = "CDP" if is_cdp else ("Bridge" if getattr(browser, "_is_bridge", False) else "headless patchright")
			raise AccountRiskError(
				f"BOSS 直聘风控拦截 (code {code}): {msg}。"
				f"当前浏览器模式: {mode}。"
				f"建议：停止自动化访问并回到 BOSS 直聘官方页面手动处理。",
				is_cdp=is_cdp,
				diagnostic=EnvironmentRiskError.from_response(result, endpoint=actual_endpoint).diagnostic,
			)
		return result

	# ── Public API ───────────────────────────────────────────────────
	# High-risk: search, recommend, greet, job_card → browser channel
	# Low-risk: status, me, cities, schema, detail → httpx channel

	def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
		params: dict[str, Any] = {"query": query, "page": filters.get("page", 1)}
		if raw_params := filters.get("raw_params"):
			params.update(raw_params)
		if city := filters.get("city"):
			code = endpoints.CITY_CODES.get(city)
			if code is None:
				raise ValueError(f"未知城市: {city}")
			params["city"] = code
		if salary := filters.get("salary"):
			code = filters.get("salary_code") or endpoints.SALARY_CODES.get(salary)
			if code:
				params["salary"] = code
		if exp := filters.get("experience"):
			code = filters.get("experience_code") or endpoints.EXPERIENCE_CODES.get(exp)
			if code:
				params["experience"] = code
		if edu := filters.get("education"):
			code = filters.get("education_code") or endpoints.EDUCATION_CODES.get(edu)
			if code:
				params["degree"] = code
		if scale := filters.get("scale"):
			code = filters.get("scale_code") or endpoints.SCALE_CODES.get(scale)
			if code:
				params["scale"] = code
		if industry := filters.get("industry"):
			code = filters.get("industry_code") or endpoints.INDUSTRY_CODES.get(industry)
			if code:
				params["industry"] = code
		if stage := filters.get("stage"):
			code = filters.get("stage_code") or endpoints.STAGE_CODES.get(stage)
			if code:
				params["stage"] = code
		if job_type := filters.get("job_type"):
			code = filters.get("job_type_code") or endpoints.JOB_TYPE_CODES.get(job_type)
			if code:
				params["jobType"] = code
		return self._browser_request("GET", endpoints.SEARCH_URL, params=params)

	def recommend_jobs(self, page: int = 1) -> dict[str, Any]:
		params = {"page": page}
		return self._browser_request("GET", endpoints.RECOMMEND_URL, params=params)

	def greet(self, security_id: str, job_id: str, message: str = "") -> dict[str, Any]:
		# friend/add acknowledges contact creation, not delivery of the supplied text.
		raise GreetingDeliveryUnverified("专属招呼正文发送尚未验证，已暂停自动发送；建立会话不代表正文已发送。")

	def apply(self, security_id: str, job_id: str, lid: str = "") -> dict[str, Any]:
		"""Current minimal apply path - reuses the immediate-chat browser endpoint."""
		data = {
			"securityId": security_id,
			"jobId": job_id,
		}
		if lid:
			data["lid"] = lid
		return self._browser_request("POST", endpoints.GREET_URL, data=data)

	def job_card(self, security_id: str, lid: str = "") -> dict[str, Any]:
		"""httpx 优先 + 浏览器降级获取职位卡片信息。"""
		try:
			return self.job_card_httpx(security_id, lid)
		except (AccountRiskError, EnvironmentRiskError):
			raise
		except Exception:
			pass
		params = {"securityId": security_id, "lid": lid}
		return self._browser_request("GET", endpoints.JOB_CARD_URL, params=params)

	def job_card_httpx(self, security_id: str, lid: str = "") -> dict[str, Any]:
		"""通过 httpx 通道获取职位卡片信息（低延迟）。"""
		params = {"securityId": security_id, "lid": lid}
		return self._request("GET", endpoints.JOB_CARD_URL, params=params)

	def job_card_browser(self, security_id: str, lid: str = "", *, native_job: dict[str, Any] | None = None) -> dict[str, Any]:
		"""Read a job card through the selected browser, without an httpx attempt."""
		params: dict[str, Any] = {"securityId": security_id, "lid": lid}
		if native_job is not None and self._browser_source == "existing-browser":
			params["nativeJob"] = native_job
		return self._browser_request("GET", endpoints.JOB_CARD_URL, params=params)

	# ── Low-risk: httpx channel ──────────────────────────────────────

	def job_detail(self, job_id: str) -> dict[str, Any]:
		params = {"encryptJobId": job_id}
		return self._request("GET", endpoints.DETAIL_URL, params=params)

	def user_info(self) -> dict[str, Any]:
		return self._request("GET", endpoints.USER_INFO_URL)

	def resume_baseinfo(self) -> dict[str, Any]:
		return self._request("GET", endpoints.RESUME_BASEINFO_URL)

	def resume_expect(self) -> dict[str, Any]:
		return self._request("GET", endpoints.RESUME_EXPECT_URL)

	def deliver_list(self, page: int = 1) -> dict[str, Any]:
		params = {"page": page}
		return self._request("GET", endpoints.DELIVER_LIST_URL, params=params)

	def job_favorites(self, page: int = 1, tag: int = 4, is_active: bool = True) -> dict[str, Any]:
		"""获取职位收藏列表（geekGetJob?tag=4，与 deliver_list 同级 wapi 只读 GET）。"""
		params = {"page": page, "tag": tag, "isActive": "true" if is_active else "false"}
		return self._request("GET", endpoints.GEEK_GET_JOB_URL, params=params)

	def friend_list(self, page: int = 1) -> dict[str, Any]:
		params = {"page": page}
		return self._request("GET", endpoints.FRIEND_LIST_URL, params=params)

	def interview_data(self) -> dict[str, Any]:
		return self._request("GET", endpoints.INTERVIEW_DATA_URL)

	def job_history(self, page: int = 1) -> dict[str, Any]:
		params = {"page": page}
		return self._request("GET", endpoints.JOB_HISTORY_URL, params=params)

	def chat_history(self, gid: str, security_id: str, *, page: int = 1, count: int = 20) -> dict[str, Any]:
		"""获取与指定好友的聊天消息历史。"""
		params = {"gid": gid, "securityId": security_id, "page": page, "c": count, "src": 0}
		return self._request("GET", endpoints.CHAT_HISTORY_URL, params=params)

	def friend_label(
		self, friend_id: str, label_id: int, friend_source: int = 0, *, remove: bool = False
	) -> dict[str, Any]:
		"""添加或移除好友标签。"""
		url = endpoints.FRIEND_LABEL_DELETE_URL if remove else endpoints.FRIEND_LABEL_ADD_URL
		params = {"friendId": friend_id, "friendSource": friend_source, "labelId": label_id}
		return self._request("GET", url, params=params)

	def exchange_contact(self, security_id: str, uid: str, name: str, exchange_type: int = 1) -> dict[str, Any]:
		"""请求交换联系方式（1=手机, 2=微信）。"""
		data = {"type": exchange_type, "securityId": security_id, "uniqueId": uid, "name": name}
		return self._browser_request("POST", endpoints.EXCHANGE_REQUEST_URL, data=data)

	def resume_status(self) -> dict[str, Any]:
		"""查询简历完整度和在线状态。"""
		return self._request("GET", endpoints.RESUME_STATUS_URL)

	def geek_get_job(self, security_id: str) -> dict[str, Any]:
		"""查询与某招聘者的互动关系（是否已打招呼等）。"""
		params = {"securityId": security_id}
		return self._request("GET", endpoints.GEEK_GET_JOB_URL, params=params)
