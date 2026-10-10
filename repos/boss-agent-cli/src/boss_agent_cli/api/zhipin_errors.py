"""Source-aware pauses: local guards never impersonate remote API responses."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from boss_agent_cli.output import redact_sensitive_text


ENVIRONMENT_RISK_ACTION = (
	"停止自动化访问并保留进度；仅在官方页面确有验证提示时由本人处理，之后由用户明确恢复。不要自动刷新、重试或切换通道。"
)


class EnvironmentRiskError(Exception):
	code = "ENVIRONMENT_RISK"

	def __init__(self, message: str = "自动化访问已暂停", *, diagnostic: dict[str, Any] | None = None) -> None:
		self.diagnostic = diagnostic or {"source": "unknown"}
		super().__init__(message)

	@classmethod
	def from_page(cls, data: dict[str, Any]) -> EnvironmentRiskError:
		meta = data.get("_bridge_meta") or {}
		sent = isinstance(meta, dict) and meta.get("transport") == "native-page" and meta.get("request_sent") is True
		verification = data.get("verification") or (meta.get("verification") if isinstance(meta, dict) else {}) or {}
		evidence = _page_evidence(verification)
		source = "page_verification" if evidence else "local_guard"
		message = (
			"网页采集过程中出现可见验证组件，自动化已暂停；本次读取已发起，结果未确认。"
			if evidence and sent else "本地检查检测到可见验证组件，自动化已暂停；未发起本次平台请求。"
			if evidence else "本地页面检查触发暂停，但没有具体组件证据；不能据此认定BOSS要求验证或账号异常。"
		)
		diagnostic = {"source": source, "request_sent": sent, "page_verification": True if evidence else None, "page_evidence": evidence}
		if sent:
			diagnostic["transport"] = "native-page"
		return cls(message, diagnostic=diagnostic)

	@classmethod
	def from_response(
		cls, response: dict[str, Any], *, endpoint: str = "", transport_source: str = "", http_status: int | None = None
	) -> EnvironmentRiskError:
		meta = response.get("_bridge_meta")
		meta = meta if isinstance(meta, dict) and transport_source != "api_response" else {}
		if meta.get("source") == "page_verification" and (meta.get("request_sent") is False or meta.get("transport") == "native-page"):
			return cls.from_page(response)
		source = "api_response" if transport_source == "api_response" or meta.get("source") == "api_response" else "unknown"
		code = response.get("code")
		code = code if type(code) is int else None
		path = urlparse(endpoint or str(meta.get("endpoint") or "")).path
		verification = meta.get("verification")
		page_seen = verification.get("detected") if isinstance(verification, dict) else None
		diagnostic = {
			"source": source,
			"response_code": code,
			"response_message": _safe_message(response_message(response)),
			"endpoint": path if path.startswith("/wapi/") else "",
			"http_status": http_status if type(http_status) is int else meta.get("http_status") if type(meta.get("http_status")) is int else None,
			"request_sent": True if source == "api_response" else None,
			"page_verification": page_seen if type(page_seen) is bool else None,
			"page_evidence": _page_evidence(verification),
		}
		page_path = urlparse(str(meta.get("page_path") or "")).path
		if page_path in {"/web/geek/job", "/web/geek/jobs", "/web/geek/chat"}:
			diagnostic["page_path"] = page_path
		if meta.get("transport") == "native-page":
			diagnostic["transport"] = "native-page"
		message = (
			f"BOSS接口返回错误（code {code}），自动化已暂停；网页不一定出现相同提示，不能仅凭错误码判断账号状态。"
			if source == "api_response" else f"自动化读取返回code {code}，但来源证据未记录；已暂停，不能据此认定账号异常。"
		)
		return cls(message, diagnostic=diagnostic)


def _safe_message(value: str) -> str:
	value = re.sub(r"https?://[^\s<>]+", "[URL REDACTED]", value)
	value = re.sub(r'''(?i)(["']?(?:__zp_stoken__|securityId|encryptUid|api[_-]?key|authorization|cookie|token|stoken|session[_-]?id)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r"\1[REDACTED]", value)
	value = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}\b", "[REDACTED]", value)
	return redact_sensitive_text(value)[:300]


def _page_evidence(value: Any) -> list[dict[str, str]]:
	if not isinstance(value, dict) or value.get("detected") is not True or not isinstance(value.get("evidence"), list):
		return []
	allowed = {"verification_path", "visible_verification_prompt", "visible_verification_frame"}
	return [
		{"kind": str(item["kind"]), "cue": "验证页面路径" if item["kind"] == "verification_path" else _safe_message(str(item.get("cue") or ""))[:80]}
		for item in value["evidence"][:3] if isinstance(item, dict) and isinstance(item.get("kind"), str) and item["kind"] in allowed
	]


def response_message(response: dict[str, Any]) -> str:
	return next((value for key in ("message", "msg", "error", "zpData") if isinstance(value := response.get(key), str) and value), "")


def classify_code_37(response: dict[str, Any]) -> Literal["token_expired", "environment_risk"]:
	message = response_message(response).lower()
	if any(marker in message for marker in ("环境存在异常", "环境异常", "访问环境异常", "请求环境异常")):
		return "environment_risk"
	if any(
		marker in message
		for marker in (
			"__zp_stoken__",
			"stoken",
			"token",
			"登录态过期",
			"登录状态过期",
			"登录状态已失效",
			"登录已过期",
			"凭证过期",
			"认证过期",
		)
	):
		return "token_expired"
	return "environment_risk"


def raise_if_environment_risk(response: Any, *, endpoint: str = "", transport_source: str = "", http_status: int | None = None) -> None:
	meta = response.get("_bridge_meta") if isinstance(response, dict) else None
	if (
		isinstance(response, dict) and response.get("code") == "BRIDGE_PAGE_VERIFICATION"
		and transport_source != "api_response" and isinstance(meta, dict)
		and meta.get("source") == "page_verification" and (meta.get("request_sent") is False or meta.get("transport") == "native-page")
	):
		raise EnvironmentRiskError.from_page(response)
	if isinstance(response, dict) and response.get("code") == 37 and classify_code_37(response) == "environment_risk":
		raise EnvironmentRiskError.from_response(response, endpoint=endpoint, transport_source=transport_source, http_status=http_status)
