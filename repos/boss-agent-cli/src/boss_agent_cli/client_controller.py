"""Serialized local work. Budget waits may continue; platform verification never auto-resumes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from pathlib import Path
from typing import Any

import click
import httpx

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.browser_source import BrowserSessionNotFound, require_existing_boss_tab
from boss_agent_cli.api.client import AccountRiskError, AuthError, BossClient
from boss_agent_cli.api.managed_browser import OperationCancelled
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.auth.manager import AuthManager
from boss_agent_cli.bridge.client import MIN_EXTENSION_VERSION, BridgeClient, BridgeRequestError, is_supported_extension_version
from boss_agent_cli.bridge.daemon import start_daemon_background
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.client_profiles import ProfileStore, profile_model_text
from boss_agent_cli.chat_delivery import VerifiedChatPlatform
from boss_agent_cli.commands.ai_cmd import _create_ai_service
from boss_agent_cli.delivery import (
	CLIENT_COLLECTION_POLICY as COLLECTION_POLICY,
	CLIENT_SEARCH_PAGE_WINDOW,
	DeliveryError,
	checked_data,
	client_collection_policy,
	collect_batch,
	draft_batch,
	public_batch,
	seed_batch_from_local,
	send_batch,
	write_preview,
)
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.delivery_retry import retry_batch
from boss_agent_cli.output import Logger, redact_sensitive_text
from boss_agent_cli.platforms.zhipin import BossPlatform
from boss_agent_cli.resume.models import resume_to_dict
from boss_agent_cli.resume.store import ResumeStore
from boss_agent_cli.role_filters import ROLE_NAMES, ROLE_SEARCH_QUERIES

SEND_PAUSED_REASON = (
	"专属招呼正文发送尚未可靠验证，自动发送已暂停。建立会话不等于正文发送成功；已有尝试请先核实，不要重复发送。"
)
RISK_COOLDOWN_SECONDS = 900


def _terms(value: Any, label: str) -> list[str]:
	if not isinstance(value, list) or len(value) > 30:
		raise ValueError(f"{label}最多30项")
	if any(not isinstance(item, str) or not item.strip() or len(item) > 80 or any(char in item for char in "\r\n\0") for item in value):
		raise ValueError(f"{label}必须是1至80字的单行文本")
	return list(dict.fromkeys(item.strip() for item in value))


def pause_message(diagnostic: dict[str, Any]) -> str:
	source = diagnostic.get("source")
	if source == "api_response":
		code = diagnostic.get("response_code")
		message = f"接口访问返回错误{f'（code {code}）' if code is not None else ''}，自动化已暂停；不能仅凭错误码判断账号状态。"
		if diagnostic.get("page_verification") is False:
			message += "请求前未检测到可见验证组件。"
		if diagnostic.get("endpoint") == "/wapi/zpgeek/search/joblist.json":
			message += "登录有效不等于搜索接口已经恢复。"
	elif source == "page_verification":
		message = "本地检查检测到可见验证组件，自动化已暂停；请先处理该页面提示。"
	elif source == "local_guard":
		message = "本地页面检查触发暂停，但缺少具体组件证据；不能认定BOSS要求验证码或账号异常。"
	else:
		message = "自动化保持暂停：历史记录未区分接口返回与本地检测，不能据此认定账号异常。"
	return message + "本机等待结束后可手动检查连接；不会自动恢复检索或发送。"


class ChromeConnectionError(Exception):
	def __init__(self, code: str, message: str, *, status: str = "needs_connection") -> None:
		self.code = code
		self.status = status
		super().__init__(message)


def search_plan(params: dict[str, Any]) -> list[dict[str, Any]]:
	cities = params["rules"]["city"]
	queries = list(
		dict.fromkeys(
			re.sub(r"\s*(?:校招|实习)$", "", query).strip() for query in [params["query"], *params["extra_queries"]]
		)
	)
	if "target_roles" in params["rules"]:
		queries = [ROLE_SEARCH_QUERIES[role] for role in params["rules"]["target_roles"]]
	queries = list(dict.fromkeys([*queries, *params["rules"].get("custom_role_keywords", [])]))
	searches = []
	# Official search/job/condition.json verified 2026-10-01: 102 graduates, 108 students.
	# A full-time jobType alone selects social hiring, not the campus channel.
	for offset in range(len(cities)):
		for index, query in enumerate(queries):
			city = cities[(offset + index) % len(cities)]
			searches.extend(
				[
					{"query": query, "city": city, "kind": "campus", "filters": {"experience": "应届生", "experience_code": "102"}},
					{"query": query, "city": city, "kind": "internship", "filters": {"experience": "在校生", "experience_code": "108"}},
				]
			)
	return searches


class ClientController(Logger):
	def __init__(self, data_dir: Path, settings_path: Path) -> None:
		super().__init__("info")
		self.data_dir = data_dir.resolve()
		self.settings_path = settings_path.resolve()
		self._mutex = threading.RLock()
		self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boss-client")
		self.cancel = threading.Event()
		self.operation: dict[str, Any] = {"status": "idle", "kind": "", "logs": [], "batch_id": None}
		self._log_path = self.data_dir / "client-control" / "events.jsonl"
		self._connection_path = self._log_path.with_name("browser-state.json")
		self._browser_state: dict[str, Any] = {
			"status": "needs_connection",
			"message": "请保留已登录的BOSS首页或职位页，然后连接Chrome。",
			"checked_at": None,
			"mode": "existing-browser",
		}
		try:
			previous = json.loads(self._connection_path.read_text(encoding="utf-8"))
			for field in ("blocked_until", "collection_resume_at"):
				if type(previous.get(field)) in {int, float}:
					self._browser_state[field] = previous[field]
			if previous.get("status") == "blocked":
				diagnostic = previous.get("pause_diagnostic")
				diagnostic = diagnostic if isinstance(diagnostic, dict) else {"source": "unknown"}
				self._browser_state.update(
					status="blocked", message=pause_message(diagnostic), pause_diagnostic=diagnostic,
					search_recovery_required=previous.get("search_recovery_required") is True,
					search_recovery_batch_id=previous.get("search_recovery_batch_id"),
					manual_recheck_used=previous.get("manual_recheck_used") is True,
					last_search_check=previous.get("last_search_check"),
				)
		except (OSError, ValueError, AttributeError):
			pass

	def _set_browser_state(self, status: str, message: str, **fields: Any) -> None:
		with self._mutex:
			self._browser_state.update(status=status, message=message, checked_at=time.time(), **fields)
			self._connection_path.parent.mkdir(parents=True, exist_ok=True)
			temp = self._connection_path.with_suffix(".tmp")
			temp.write_text(json.dumps(self._browser_state, ensure_ascii=False), encoding="utf-8")
			temp.replace(self._connection_path)

	def _pause_for_risk(self, exc: Exception) -> None:
		diagnostic = getattr(exc, "diagnostic", None) or {"source": "unknown"}
		previous = self._browser_state
		manual_used = previous.get("manual_recheck_used") is True if previous["status"] == "blocked" else False
		search_pending = self._search_recovery_required()
		search_batch_id = previous.get("search_recovery_batch_id")
		if diagnostic.get("endpoint") == "/wapi/zpgeek/search/joblist.json":
			search_pending = True
			search_batch_id = self.operation.get("batch_id") or search_batch_id
		until = previous.get("blocked_until", 0)
		if diagnostic.get("source") == "existing_pause":
			diagnostic = previous.get("pause_diagnostic") or {"source": "unknown"}
		elif previous["status"] != "blocked" or until <= time.time():
			until = time.time() + RISK_COOLDOWN_SECONDS
		self._set_browser_state(
			"blocked", pause_message(diagnostic), blocked_until=until, pause_diagnostic=copy.deepcopy(diagnostic),
			search_recovery_required=search_pending, search_recovery_batch_id=search_batch_id,
			manual_recheck_used=manual_used,
			last_search_check=previous.get("last_search_check") if previous["status"] == "blocked" else None,
		)

	def _check_cancel(self) -> None:
		if self.cancel.is_set():
			raise OperationCancelled("任务已停止")

	def _check_chrome(self, client: BossClient, *, reuse_login: bool = False, for_search: bool = False) -> None:
		self._check_cancel()
		bridge = BridgeClient(max_retries=1)
		status = bridge.status()
		if status is None:
			self.info("正在启动本机扩展连接服务")
			start_daemon_background()
		deadline = time.monotonic() + 5
		while not (status and status.get("extensionConnected")) and time.monotonic() < deadline:
			self.cancel.wait(0.25)
			self._check_cancel()
			status = bridge.status()
		if not status or not status.get("extensionConnected"):
			raise ChromeConnectionError(
				"CHROME_CONNECTION_REQUIRED", "Chrome扩展尚未连接。请保持Chrome运行，并启用BOSS Agent Bridge扩展。"
			)
		version = str(status.get("extensionVersion") or "")
		if not is_supported_extension_version(version):
			raise ChromeConnectionError(
				"EXTENSION_UPDATE_REQUIRED",
				f"BOSS扩展需要更新。请在chrome://extensions点击BOSS Agent Bridge的刷新按钮，确认版本为{MIN_EXTENSION_VERSION}或更高。",
			)
		self._check_cancel()
		if for_search or self._search_recovery_required():
			require_existing_boss_tab(for_search=True)
		else:
			require_existing_boss_tab()
		self._check_cancel()
		if (
			reuse_login
			and self._browser_state["status"] == "ready"
			and 0 <= time.time() - (self._browser_state.get("checked_at") or 0) < 300
		):
			self.info("复用5分钟内的登录检查；当前BOSS页面已检查")
			return
		# Use only the selected Chrome tab; never import cookies or use HTTP auth fallback.
		result = client._browser_request("GET", endpoints.USER_INFO_URL)
		if result.get("code") in {9, 36}:
			raise EnvironmentRiskError.from_response(result, endpoint=endpoints.USER_INFO_URL)
		if result.get("code") != 0:
			raise ChromeConnectionError(
				"CHROME_LOGIN_REQUIRED",
				"未能确认BOSS登录状态。请在原来的Chrome页面完成登录，再重新连接。",
				status="needs_login",
			)
		self._check_cancel()
		if self._search_recovery_required():
			self.info("登录检查通过，但不能代表搜索恢复；只复查上次失败的搜索页一次，不读取JD或发送")
			self._check_search_recovery(client)
			self._check_cancel()
		self._set_browser_state(
			"ready", "Chrome已连接，登录检查成功；不代表所有接口均已通过检查。", extension_version=version, blocked_until=0, pause_diagnostic=None, search_recovery_required=False, search_recovery_batch_id=None, manual_recheck_used=False
		)

	def _search_recovery_required(self) -> bool:
		diagnostic = self._browser_state.get("pause_diagnostic") or {}
		return self._browser_state.get("search_recovery_required") is True or (
			self._browser_state["status"] == "blocked" and diagnostic.get("endpoint") == "/wapi/zpgeek/search/joblist.json"
		)

	def _check_search_recovery(self, client: BossClient) -> None:
		"""An explicit recovery action verifies the failed endpoint, never a new search scope."""
		self._check_request_pause("check")
		with DeliveryStore(self.data_dir) as store, CacheStore(self.data_dir / "cache/boss_agent.db") as cache:
			batch_id = self._browser_state.get("search_recovery_batch_id")
			batches = [store.get(batch_id)] if batch_id else [store.get(row["id"]) for row in store.list_batches()]
			batch = next((b for b in batches if b["stop_reason"] == "environment_risk" and b["stats"].get("last_error", {}).get("diagnostic", {}).get("endpoint") == "/wapi/zpgeek/search/joblist.json"), None)
			checkpoint = batch.get("checkpoint") if batch else None
			queue = (checkpoint or {}).get("queue") or []
			if batch is None or not queue or len(queue[0]) != 5 or (checkpoint or {}).get("version") not in {1, 2}:
				raise ValueError("登录有效，但缺少失败搜索的断点，未解除搜索暂停；不会用其他接口或搜索词代替验证")
			query, city, page, _, filters = queue[0]
			if not isinstance(query, str) or not query.strip() or (city is not None and not isinstance(city, str)) or type(page) is not int or page < 1 or not isinstance(filters, dict):
				raise ValueError("失败搜索的断点格式无效，未发送复查请求")
			policy = client_collection_policy(batch["params"].get("collection_policy"))
			self.operation["batch_id"] = batch["id"]
			self.operation["phase"] = "checking_search"
			while True:
				self._check_cancel()
				reservation = cache.reserve_discovery_request(
					limit=policy["request_limit"], window_seconds=policy["window_seconds"], interval=policy["interval_seconds"],
				)
				if reservation["allowed"]:
					break
				if self.operation.get("phase") != "waiting_budget":
					self.info("复查搜索前等待本机读取预算或间隔恢复，等待期间不访问BOSS")
				self.operation.update(phase="waiting_budget", wait_until=reservation["resume_at"])
				self.cancel.wait(min(0.5, max(0.01, reservation["resume_at"] - time.time())))
			self.operation.update(phase="checking_search", wait_until=None)
			stats = batch["stats"]
			stats["network_requests"] = stats.get("network_requests", 0) + 1
			stats["search_recovery_checks"] = stats.get("search_recovery_checks", 0) + 1
			stats["last_search_recovery"] = {"at": time.time(), "status": "unconfirmed"}
			store.progress(batch["id"], stats, batch["stop_reason"])
			try:
				platform = BossPlatform(client)
				data = checked_data(platform, platform.search_jobs(query, city=city, page=page, **filters))
				rows = data.get("jobList")
				if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or (rows and not any(row.get("encryptJobId") for row in rows)):
					raise DeliveryError("NETWORK_ERROR", "搜索复查没有返回有效列表，仍保持暂停")
				key = {"cache_schema": "delivery-search-v1", "platform": batch["params"].get("platform", "zhipin"), "query": query, "city": city, "page": page, **({"filters": filters} if filters else {})}
				cache.put_search(key, json.dumps(data, ensure_ascii=False))
				stats["last_search_recovery"]["status"] = "verified"
				self.info("上次失败的搜索页已返回有效列表并缓存；可以继续本批，尚未续采或发送")
			except EnvironmentRiskError as exc:
				stats["last_search_recovery"]["status"] = "blocked"
				stats["last_error"] = {"code": exc.code, "message": str(exc), "diagnostic": exc.diagnostic}
				self.info("搜索接口再次拒绝访问，登录有效也不会标记为搜索已恢复；请勿连续尝试")
				raise
			finally:
				stats["last_search_recovery"]["finished_at"] = time.time()
				cache.put_crawl_budget("delivery-discovery-completed", time.time())
				store.progress(batch["id"], stats, batch["stop_reason"])

	def _check_request_pause(self, kind: str) -> None:
		if kind in {"connect", "check", "check_now"} and self.operation.get("status") == "running" and self.operation.get("kind") == "check_now" and self.operation.get("manual_probe_authorized") is True:
			return
		if kind in {"connect", "check", "prepare", "resume", "send"}:
			remaining = ceil(max(0, self._browser_state.get("blocked_until", 0) - time.time()))
			if remaining:
				raise ValueError(
					f"本机保护性冷却还剩{remaining}秒，不是BOSS官方处罚倒计时；不会发起请求。结束后可手动检查连接，仅在页面确有验证提示时处理。"
				)

	def manual_recheck_state(self) -> dict[str, Any]:
		browser = self._browser_state
		diagnostic = browser.get("pause_diagnostic") or {}
		supported = browser["status"] == "blocked" and self._search_recovery_required() and diagnostic.get("source") == "api_response" and diagnostic.get("endpoint") == "/wapi/zpgeek/search/joblist.json" and diagnostic.get("response_code") == 37 and diagnostic.get("page_verification") is False
		remaining = ceil(max(0, browser.get("blocked_until", 0) - time.time()))
		used = browser.get("manual_recheck_used") is True
		revision = hashlib.sha256(json.dumps([browser.get("blocked_until"), diagnostic, browser.get("search_recovery_batch_id"), used], sort_keys=True).encode()).hexdigest()
		return {"supported": supported, "available": supported and remaining > 0 and not used and self.operation["status"] != "running", "used": used, "remaining_seconds": remaining, "revision": revision}

	def _wait_collection_start(self) -> None:
		with CacheStore(self.data_dir / "cache/boss_agent.db") as cache:
			budget = cache.discovery_budget(
				limit=COLLECTION_POLICY["request_limit"], window_seconds=COLLECTION_POLICY["window_seconds"]
			)
		until = max(budget["resume_at"], self._browser_state.get("collection_resume_at", 0))
		if until > time.time():
			self.info("任务已接收，等待本机采集预算恢复后继续；等待期间不访问BOSS")
			self.operation.update(phase="waiting_budget", wait_until=until)
			while time.time() < until:
				self._check_cancel()
				self.cancel.wait(min(0.5, until - time.time()))
		self._check_cancel()
		self.operation.update(phase="connecting", wait_until=None)

	def info(self, message: str) -> None:
		with self._mutex:
			self.operation["logs"] = [
				*self.operation.get("logs", []),
				{"at": time.time(), "message": redact_sensitive_text(message)},
			][-100:]

	def debug(self, message: str) -> None:
		pass

	def _event(self, kind: str, message: str, **fields: Any) -> None:
		self._log_path.parent.mkdir(parents=True, exist_ok=True)
		with self._log_path.open("a", encoding="utf-8") as handle:
			handle.write(
				json.dumps({"at": time.time(), "kind": kind, "message": message, **fields}, ensure_ascii=False) + "\n"
			)

	def settings(self) -> dict[str, Any]:
		params = json.loads(self.settings_path.read_text(encoding="utf-8"))
		params["completion_first"] = True
		return params

	@staticmethod
	def _settings_revision(params: dict[str, Any]) -> str:
		return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

	def _base_profile(self, name: str) -> dict[str, Any]:
		resume = ResumeStore(self.data_dir / "resumes").get(name)
		if resume is not None:
			return resume_to_dict(resume)
		return {"name": name, "title": name, "center_title": False, "personal_info": {"items": [], "layout": "inline"}, "job_intention": None, "modules": [], "avatar": "", "created_at": "", "updated_at": ""}

	def profile(self) -> dict[str, Any]:
		with self._mutex, ProfileStore(self.data_dir) as store:
			name = self.settings()["resume"]
			value = store.current(name, self._base_profile(name))
			return {**value, "model_text": profile_model_text(value["data"])}

	def save_profile(self, body: dict[str, Any]) -> dict[str, Any]:
		with self._mutex, ProfileStore(self.data_dir) as store:
			self._assert_idle()
			name = self.settings()["resume"]
			expected = body.get("expected_version")
			if not isinstance(expected, str):
				raise ValueError("缺少资料版本，请重新加载后保存")
			value = store.save(name, body.get("data"), expected_version=expected, base=self._base_profile(name))
			self._event("profile", "已保存本机个人资料新版本", version=value["version"])
			return {**value, "model_text": profile_model_text(value["data"])}

	def _snapshot_profile(self, params: dict[str, Any]) -> None:
		with ProfileStore(self.data_dir) as store:
			params["profile_version"] = store.snapshot(params["resume"], self._base_profile(params["resume"]))

	def _assert_idle(self) -> None:
		if self.operation["status"] == "running":
			raise ValueError("已有任务正在运行，请等待完成或停止当前任务")

	def _assert_current_batch(self, batch_id: str) -> None:
		if any(batch["id"] == batch_id and batch["source"] for batch in self.state()["batches"]):
			raise ValueError("这是追溯用的源批次，请操作已复核的新批次")

	def state(self) -> dict[str, Any]:
		with self._mutex, DeliveryStore(self.data_dir) as store:
			batches = [store.get(row["id"]) for row in store.list_batches()]
			sources = {value for batch in batches for value in batch["params"].get("source_batches", [])}
			sources.update(batch["params"].get("draft_source_batch_id") for batch in batches)
			visible = []
			for batch in batches:
				if not batch["items"] and batch["id"] != self.operation.get("batch_id"):
					continue
				public = public_batch(batch)
				visible.append(
					{
						"id": batch["id"],
						"created_at": batch["created_at"],
						"counts": public["counts"],
						"total": len(batch["items"]),
						"source": batch["id"] in sources,
						"stop_reason": batch["stop_reason"],
					}
				)
			model = "未配置"
			try:
				model = json.loads((self.data_dir / "ai/config.json").read_text(encoding="utf-8")).get(
					"ai_model", model
				)
			except (OSError, ValueError):
				pass
			settings = self.settings()
			return {
				"collection": {
					**COLLECTION_POLICY,
					"page_window": CLIENT_SEARCH_PAGE_WINDOW,
					"auto_continue": True,
					"remaining_seconds": ceil(max(0, self._browser_state.get("collection_resume_at", 0) - time.time())),
				},
				"delivery": {
					"enabled": VerifiedChatPlatform.verified_greeting_delivery,
					"message": "发送前请保留BOSS消息页。仅在服务器聊天记录匹配原文后标记已发送。"
					if VerifiedChatPlatform.verified_greeting_delivery
					else SEND_PAUSED_REASON,
				},
				"operation": copy.deepcopy(self.operation),
				"browser": copy.deepcopy(self._browser_state),
				"settings": settings,
				"settings_revision": self._settings_revision(settings),
				"batches": visible,
				"model": model,
				"roles": ROLE_NAMES,
				"cities": list(endpoints.CITY_CODES),
				"manual_recheck": self.manual_recheck_state(),
			}

	def batch(self, batch_id: str) -> dict[str, Any]:
		with DeliveryStore(self.data_dir) as store:
			return public_batch(store.get(batch_id))

	def events(self) -> list[dict[str, Any]]:
		entries: list[dict[str, Any]] = []
		for path in (self.data_dir / "client" / "events.jsonl", self._log_path):
			if path.exists():
				entries.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[-200:])
		return sorted(entries, key=lambda entry: entry["at"])[-200:]

	def save_settings(self, body: dict[str, Any]) -> None:
		with self._mutex:
			self._assert_idle()
			params = self.settings()
			if "expected_revision" in body and body["expected_revision"] != self._settings_revision(params):
				raise ValueError("筛选条件已有更新，请重新加载后再保存")
			limits = {
				"campus_count": (0, 50),
				"internship_count": (0, 50),
				"min_salary": (1, 200),
				"min_daily_salary": (1, 5000),
				"max_pages": (1, 200),
				"max_details": (1, 300),
				"max_seconds": (30, 3600),
			}
			for key, (low, high) in limits.items():
				if key in {"max_pages", "max_details", "max_seconds"} and key not in body:
					continue
				if type(body.get(key)) is not int or not low <= body[key] <= high:
					raise ValueError(f"数值无效：{key}")
			total = body["campus_count"] + body["internship_count"]
			if not 1 <= total <= 50:
				raise ValueError("总岗位数量必须为1至50个")
			cities, roles = body.get("cities"), body.get("roles")
			if (
				not isinstance(cities, list)
				or not cities
				or len(cities) > len(endpoints.CITY_CODES)
				or any(not isinstance(city, str) or city not in endpoints.CITY_CODES for city in cities)
			):
				raise ValueError("请选择有效城市")
			custom = _terms(body.get("custom_role_keywords", params["rules"].get("custom_role_keywords", [])), "自定义方向")
			if not isinstance(roles, list) or (not roles and not custom) or any(not isinstance(role, str) or role not in ROLE_NAMES for role in roles):
				raise ValueError("请选择有效岗位方向")
			year = body.get("graduation_year", params["rules"].get("graduation_year"))
			bachelor = body.get("bachelor_eligible", params["rules"].get("bachelor_eligible", True))
			if type(year) is not int or not 2000 <= year <= 2100 or type(bachelor) is not bool:
				raise ValueError("毕业届别或本科资格条件无效")
			if body.get("history_dedupe") not in {"job", "company"}:
				raise ValueError("请选择历史去重范围")
			params.update(count=total, **{key: body[key] for key in ("max_pages", "max_details", "max_seconds") if key in body})
			params["rules"].pop("max_internship_ratio", None)
			params["rules"].update(
				city=list(dict.fromkeys(cities)),
				target_roles=list(dict.fromkeys(roles)),
				custom_role_keywords=custom, graduation_year=year, bachelor_eligible=bachelor,
				min_salary=body["min_salary"],
				min_daily_salary=body["min_daily_salary"],
				history_dedupe=body["history_dedupe"],
				recruitment_quotas={"campus": body["campus_count"], "internship": body["internship_count"]},
			)
			for key, label in (("require", "必含关键词"), ("exclude", "排除关键词"), ("exclude_company", "排除公司")):
				if key in body:
					params["rules"][key] = _terms(body[key], label)
			if not search_plan(params):
				raise ValueError("所选方向在当前城市没有搜索范围，请调整城市或方向")
			for key in ("allow_unspecified_cohort", "allow_explicit_graduate_roles"):
				if key in body:
					if type(body[key]) is not bool:
						raise ValueError("候选范围开关必须为布尔值")
					params["rules"][key] = body[key]
			temp = self.settings_path.with_suffix(".tmp")
			temp.write_text(json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
			temp.replace(self.settings_path)
			self._event("settings", "已更新筛选条件")

	def review(self, body: dict[str, Any]) -> None:
		with self._mutex, DeliveryStore(self.data_dir) as store:
			self._assert_idle()
			approve, reject = set(body.get("approve", [])), set(body.get("reject", []))
			self._assert_current_batch(body["batch_id"])
			if not approve | reject or any(type(number) is not int or number < 1 for number in approve | reject):
				raise ValueError("请选择有效岗位编号")
			expected = {int(key): value for key, value in body.get("expected_updates", {}).items()}
			store.review(body["batch_id"], approve=approve, reject=reject, expected_updates=expected)
			write_preview(store, body["batch_id"])
			self._event("review", f"批准{len(approve)}个，拒绝{len(reject)}个", batch_id=body["batch_id"])

	def edit(self, body: dict[str, Any]) -> None:
		with self._mutex, DeliveryStore(self.data_dir) as store:
			self._assert_idle()
			self._assert_current_batch(body["batch_id"])
			store.edit_greeting(body["batch_id"], body["number"], body["greeting"], body["expected_update"])
			write_preview(store, body["batch_id"])
			self._event("edit", "招呼已修改，需重新审批", batch_id=body["batch_id"], number=body["number"])

	def start(self, kind: str, body: dict[str, Any]) -> None:
		with self._mutex:
			self._assert_idle()
			if kind not in {"connect", "check", "check_now", "prepare", "resume", "draft", "send"}:
				raise ValueError("未知操作")
			manual = kind == "check_now"
			if manual:
				guard = self.manual_recheck_state()
				if body.get("confirm_once") is not True or body.get("expected_pause") != guard["revision"] or not guard["available"]:
					raise ValueError("无法跳过本机等待：暂停状态已变化、本轮已复查，或当前不支持提前复查")
				self._set_browser_state("blocked", self._browser_state["message"], manual_recheck_used=True)
				self._event("check_now", "用户确认跳过本机等待，仅复查一次；不解除平台限制")
			if kind == "prepare" and "fresh_only" in body and type(body["fresh_only"]) is not bool:
				raise ValueError("fresh_only必须为布尔值")
			if kind == "prepare":
				params = self.settings()
				if not search_plan(params) or type(params["rules"].get("graduation_year")) is not int:
					raise ValueError("请先保存目标城市、岗位方向和毕业届别，再生成批次")
			if kind != "prepare":
				self._check_request_pause(kind)
			if kind in {"draft", "send", "resume"}:
				self._assert_current_batch(body["batch_id"])
				batch = self.batch(body["batch_id"])
				if kind == "resume":
					if not batch["collection_resume"]["available"]:
						raise ValueError("本批数量已齐或搜索范围已用尽，无可继续的采集任务")
					if self._browser_state["status"] != "ready":
						raise ValueError("请先手动恢复Chrome连接，再继续本批；不会自动解除平台暂停")
				if kind == "send":
					approved = [
						item for item in batch["items"] if item["status"] in {"approved", "not_sent", "uncertain"}
					]
					if not approved:
						raise ValueError("没有已批准的岗位")
					if not 1 <= len(approved) <= 50:
						raise ValueError("发送清单必须为1至50个已批准岗位")
					numbers = body.get("approved_numbers")
					if (
						not isinstance(numbers, list)
						or any(type(number) is not int for number in numbers)
						or numbers != [item["number"] for item in approved]
					):
						raise ValueError("发送清单已变化，请刷新后重新确认")
					if "limit" in body and (type(body["limit"]) is not int or body["limit"] != len(approved)):
						raise ValueError("发送数量与已批准清单不一致，请刷新页面")
					if "expected_updates" in body and (
						not isinstance(body["expected_updates"], dict)
						or any(
							body["expected_updates"].get(str(item["number"])) != item["updated_at"] for item in approved
						)
					):
						raise ValueError("批准内容已变化，请重新打开确认清单")
					if not VerifiedChatPlatform.verified_greeting_delivery:
						raise ValueError(SEND_PAUSED_REASON)
					body = {**body, "limit": len(approved), "allowed_numbers": {item["number"] for item in approved}}
					uncertain = {item["number"] for item in approved if item["status"] == "uncertain"}
					if uncertain:
						if body.get("verify_before_retry") is not True:
							raise ValueError("清单含待核实记录，请确认先核查历史、仅对确认缺失的原文补发")
						body.update(retry_numbers=uncertain, retry_authorization_id=uuid.uuid4().hex)
			self.cancel.clear()
			self.operation = {
				"status": "running",
				"kind": kind,
				"logs": [],
				"started_at": time.time(),
				"batch_id": body.get("batch_id"),
				"error": None,
				"manual_probe_authorized": manual,
			}
			self._executor.submit(self._run, kind, body)

	def stop(self) -> None:
		self.cancel.set()
		self.info("已请求停止，等待当前请求完成记录")

	def _service_and_resume(self, params: dict[str, Any] | None = None) -> tuple[Any, str]:
		params = params if params is not None else self.settings()
		ctx = click.Context(click.Command("client"), obj={"data_dir": self.data_dir, "logger": self})
		service = _create_ai_service(ctx)
		if service is None:
			raise ValueError("请先配置模型服务和本地简历")
		if params.get("profile_version"):
			with ProfileStore(self.data_dir) as store:
				data = store.get(params["resume"], params["profile_version"])
		else:
			resume = ResumeStore(self.data_dir / "resumes").get(params["resume"])
			if resume is None:
				raise ValueError("本批次的原始简历不存在，未改用新资料")
			data = resume_to_dict(resume)
		return service, profile_model_text(data)

	def _run(self, kind: str, body: dict[str, Any]) -> None:
		search_check = kind in {"connect", "check", "check_now"} and self._search_recovery_required()
		if search_check:
			self.operation.update(search_check=True, phase="checking_login")
			self.operation.setdefault("started_at", time.time())
		try:
			if kind == "draft":
				self.info("仅用已保存JD生成DeepSeek文案，不访问BOSS")
				with DeliveryStore(self.data_dir) as store:
					service, resume = self._service_and_resume(store.get(body["batch_id"])["params"])
					try:
						draft_batch(store, body["batch_id"], service, resume, self, should_stop=self.cancel.is_set, workers=3)
						missing = sum(item["status"] == "draft" for item in store.get(body["batch_id"])["items"])
						if missing:
							self.operation["shortfall_message"] = f"仍有{missing}条文案未就绪，可再次补齐；已成功或已审批的文案未修改。"
					finally:
						write_preview(store, body["batch_id"])
			else:
				self._network_operation(kind, body)
			with self._mutex:
				self.operation["status"] = (
					"stopped"
					if self.cancel.is_set()
					else ("partial" if self.operation.get("shortfall_message") else "complete")
				)
			self.info("任务已停止" if self.cancel.is_set() else (self.operation.get("shortfall_message") or "操作完成"))
		except Exception as exc:
			if (
				isinstance(exc, (EnvironmentRiskError, AccountRiskError))
				or getattr(exc, "code", None) == "RATE_LIMITED"
			):
				self._pause_for_risk(exc)
			elif kind in {"connect", "check", "check_now"} and self._search_recovery_required():
				self._set_browser_state(
					"blocked", pause_message(self._browser_state["pause_diagnostic"]) + f"本次检查未完成：{redact_sensitive_text(str(exc))[:200]}"
				)
			elif isinstance(exc, AuthError):
				self._set_browser_state("needs_login", str(exc))
			elif isinstance(exc, ChromeConnectionError):
				self._set_browser_state(exc.status, str(exc))
			elif isinstance(exc, BrowserSessionNotFound):
				self._set_browser_state("needs_connection", str(exc))
			elif isinstance(exc, BridgeRequestError):
				self._set_browser_state("needs_connection", str(exc))
			with self._mutex:
				self.operation["status"] = (
					"stopped"
					if isinstance(exc, OperationCancelled)
					or (self.cancel.is_set() and getattr(exc, "code", None) == "CANCELLED")
					else "error"
				)
				self.operation["error"] = {
					"code": getattr(exc, "code", type(exc).__name__),
					"message": redact_sensitive_text(str(exc))[:500],
				}
				diagnostic = getattr(exc, "diagnostic", None)
				if diagnostic:
					self.operation["error"]["diagnostic"] = copy.deepcopy(diagnostic)
			self.info(self.operation["error"]["message"])
		finally:
			with self._mutex:
				self.operation["finished_at"] = time.time()
				if search_check:
					self.operation.update(phase="finished", wait_until=None)
					result = {key: copy.deepcopy(self.operation.get(key)) for key in ("kind", "status", "started_at", "finished_at", "batch_id", "error")}
					self._set_browser_state(self._browser_state["status"], self._browser_state["message"], last_search_check=result)
				self._event(
					kind,
					self.operation["status"],
					batch_id=self.operation.get("batch_id"),
					error=self.operation.get("error"),
				)

	def _assert_boss_ready(self, kind: str) -> None:
		if self._browser_state["status"] == "blocked":
			raise EnvironmentRiskError("保持上次自动化暂停；本次只处理已保存资料，未发起平台请求。", diagnostic={"source": "existing_pause"})
		self._check_request_pause(kind)
		if self._browser_state["status"] != "ready":
			raise ChromeConnectionError("CHROME_CONNECTION_REQUIRED", "请先连接Chrome，确认已登录的BOSS页面可用。")

	def _prepare_operation(self, *, fresh_only: bool = False, batch_id: str | None = None) -> None:
		started = time.monotonic()
		resuming = batch_id is not None
		with DeliveryStore(self.data_dir) as store, CacheStore(self.data_dir / "cache/boss_agent.db") as cache:
			if batch_id is not None:
				batch = store.get(batch_id)
				params = batch["params"]
				if not public_batch(batch)["collection_resume"]["available"]:
					raise ValueError("本批无可继续的采集任务")
				self._assert_boss_ready("resume")
				if not batch.get("checkpoint"):
					self.info("旧批次没有搜索断点：按原条件重新扫描并跳过已入选岗位，仍写入同一批次")
			else:
				params = self.settings()
				params.update(
					searches=search_plan(params), browser_source="existing-browser",
					collection_policy=dict(COLLECTION_POLICY), continue_after_cooldown=True,
					prioritize_explicit_evidence=True, completion_first=True, fresh_only=fresh_only,
				)
				if not params["searches"]:
					raise ValueError("当前筛选条件没有可执行的搜索方向")
				self._snapshot_profile(params)
				batch_id = store.create(params)
			initial_stats = store.get(batch_id)["stats"]
			with self._mutex:
				self.operation.update(batch_id=batch_id, phase="local_candidates", completion_first=True)
			problem: Exception | None = None
			try:
				self.info("完成优先：持续补齐岗位与文案；本机预算等待后续采，平台校验仍停止")
				count = len(store.get(batch_id)["items"])
				if resuming:
					self.info("继续本批：保留原筛选条件、审批及发送记录，只追加原目标中缺少的岗位，不自动发送")
				elif fresh_only:
					self.info("本次仅查找历史批次未入选的岗位，不承接旧批次；仍使用完整JD缓存并遵守读取预算")
				else:
					self.info("先复核近期完整JD和未完成、未审批、未联系的批次，不访问BOSS")
					count = seed_batch_from_local(store, batch_id, cache, self, should_stop=self.cancel.is_set)
				if count < params["count"]:
					try:
						self._assert_boss_ready("prepare")
						self._wait_collection_start()
						with BossClient(
							AuthManager(self.data_dir, logger=self), browser_source="existing-browser", delay=(5, 8)
						) as client:
							self._check_chrome(client, reuse_login=True, for_search=True)
							self.operation["phase"] = "collecting"
							collect_batch(
								store, batch_id, BossPlatform(client), cache, self, should_stop=self.cancel.is_set,
							)
					except (
						EnvironmentRiskError,
						AccountRiskError,
						AuthError,
						ChromeConnectionError,
						BrowserSessionNotFound,
						BridgeRequestError,
						DeliveryError,
						httpx.TransportError,
					) as exc:
						problem = exc
						if isinstance(exc, (EnvironmentRiskError, AccountRiskError)):
							self._pause_for_risk(exc)
						self.info("平台读取已停止；仅为已保留岗位整理本地预览，不再请求BOSS")
				self._check_cancel()
				batch = store.get(batch_id)
				batch["stats"]["screening_seconds"] = round(initial_stats.get("screening_seconds", 0) + time.monotonic() - started, 2)
				store.progress(batch_id, batch["stats"], batch["stop_reason"])
				if batch["items"]:
					self.operation["phase"] = "drafting_saved"
					service, resume = self._service_and_resume(params)
					draft_batch(store, batch_id, service, resume, self, should_stop=self.cancel.is_set, workers=3)
				batch = store.get(batch_id)
				quotas = params["rules"].get("recruitment_quotas") or {}
				quota_met = all(
					sum(item["payload"].get("recruitment_kind") == kind for item in batch["items"]) == quota
					for kind, quota in quotas.items()
				)
				if len(batch["items"]) != params["count"] or not quota_met:
					stop = {
						"results_exhausted": "当前搜索范围暂无更多可用候选",
						"results_incomplete": "部分平台列表缺少有效岗位标识，无法确认完整结果",
						"page_limit": "达到总搜索页数上限",
						"detail_limit": "达到总JD读取上限",
						"time_limit": "达到总运行时限",
						"local_candidates": "需要恢复BOSS后继续读取",
						"environment_risk": "自动化访问已暂停，详见暂停依据",
					}.get(batch["stop_reason"], batch["stop_reason"])
					self.operation["shortfall_message"] = (
						f"未完成：本次找到{len(batch['items'])}/{params['count']}个候选，数量或分类配额尚未满足；{stop}。已保留结果，未发送。"
					)
				elif any(
					item["status"] != "rejected" and (item["status"] == "draft" or not all(item["payload"].get(key) for key in ("description", "summary", "greeting")))
					for item in batch["items"]
				):
					self.operation["shortfall_message"] = f"未完成：已筛选{len(batch['items'])}个岗位，部分预览未就绪；资料已保存，未发送。"
				if problem:
					raise problem
			finally:
				batch = store.get(batch_id)
				batch["stats"]["preview_seconds"] = round(initial_stats.get("preview_seconds", 0) + time.monotonic() - started, 2)
				store.progress(batch_id, batch["stats"], batch["stop_reason"])
				budget = cache.discovery_budget(
					limit=COLLECTION_POLICY["request_limit"], window_seconds=COLLECTION_POLICY["window_seconds"]
				)
				resume_at = max(budget["resume_at"], store.get(batch_id)["stats"].get("resume_at", 0))
				if resume_at:
					self._set_browser_state(
						self._browser_state["status"], self._browser_state["message"], collection_resume_at=resume_at
					)
				write_preview(store, batch_id)

	def _network_operation(self, kind: str, body: dict[str, Any]) -> None:
		if kind == "check_now" and not (self.operation.get("status") == "running" and self.operation.get("kind") == "check_now" and self.operation.get("manual_probe_authorized") is True):
			raise ValueError("本次提前复查未经确认")
		if kind in {"prepare", "resume"}:
			self._prepare_operation(fresh_only=body.get("fresh_only") is True, batch_id=body["batch_id"] if kind == "resume" else None)
			return
		self._check_request_pause(kind)
		if kind not in {"connect", "check", "check_now"}:
			self._assert_boss_ready(kind)
		self.info("正在检查现有Chrome与BOSS登录状态")
		with BossClient(
			AuthManager(self.data_dir, logger=self), browser_source="existing-browser", delay=(5, 8)
		) as client:
			self._check_chrome(client)
			if kind in {"connect", "check", "check_now"}:
				return
			self._check_cancel()
			rechecked = 0
			if body.get("retry_numbers"):
				results = retry_batch(
					self.data_dir,
					body["batch_id"],
					body["retry_authorization_id"],
					body["retry_numbers"],
					body["retry_numbers"],
					set(),
					mode="send",
					should_stop=self.cancel.is_set,
					logger=self,
				)
				rechecked = sum(bool(result.get("verified")) for result in results)
			self._check_cancel()
			with DeliveryStore(self.data_dir) as store, CacheStore(self.data_dir / "cache/boss_agent.db") as cache:
				try:
					sent = send_batch(
						store,
						body["batch_id"],
						VerifiedChatPlatform(client),
						cache,
						limit=body["limit"],
						interval=10,
						should_stop=self.cancel.is_set,
						allowed_numbers=body["allowed_numbers"],
					)
					self.info(f"本次服务器消息记录核实{sent + rechecked}条招呼正文")
				finally:
					write_preview(store, body["batch_id"])

	def close(self) -> None:
		self.cancel.set()
		self._executor.shutdown(wait=True)
