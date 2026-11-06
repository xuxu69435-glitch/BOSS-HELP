"""Independent, draft-only greetings from pasted JDs or public BOSS job pages."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from boss_agent_cli.delivery import generate_draft
from boss_agent_cli.output import redact_sensitive_text


def _text(body: dict[str, Any], key: str, maximum: int) -> str:
	value = body.get(key, "")
	if not isinstance(value, str) or len(value) > maximum or "\0" in value:
		raise ValueError(f"{key}必须是最多{maximum}字的文本")
	return value.strip()


def greeting_input(body: dict[str, Any]) -> dict[str, str]:
	job = {key: _text(body, key, limit) for key, limit in (("url", 2048), ("description", 12000), ("title", 160), ("company", 160))}
	if job["url"]:
		url = urlsplit(job["url"])
		if (
			url.scheme != "https" or url.hostname not in {"www.zhipin.com", "zhipin.com"}
			or url.username or url.password or url.port not in {None, 443}
			or not re.fullmatch(r"/job_detail/[A-Za-z0-9_-]+\.html", url.path)
		):
			raise ValueError("请粘贴BOSS完整岗位链接（https://www.zhipin.com/job_detail/…html），或直接粘贴岗位描述")
		# Tracking parameters and fragments are neither needed nor sent to the model.
		job["url"] = urlunsplit(("https", "www.zhipin.com", url.path, "", ""))
	if not job["url"] and not job["description"]:
		raise ValueError("请提供岗位链接或岗位描述")
	if job["description"] and len(job["description"]) < 20:
		raise ValueError("岗位描述至少20字，请包含岗位职责或要求")
	return job


class _JobPage(HTMLParser):
	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.stack: list[tuple[str, set[str], str]] = []
		self.parts: dict[str, list[str]] = {key: [] for key in ("description", "title", "company", "salary", "ld", "plain")}
		self.ld_blocks: list[str] = []

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		attrs_map = dict(attrs)
		classes = set((attrs_map.get("class") or "").split())
		kind = ""
		if tag == "script" and attrs_map.get("type") == "application/ld+json":
			kind = "ld"
		elif "job-sec-text" in classes:
			kind = "description"
		elif tag == "h1":
			kind = "title"
		elif "company-name" in classes:
			kind = "company"
		elif "salary" in classes:
			kind = "salary"
		if tag in {"br", "p", "div", "li"}:
			self.handle_data("\n")
		if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
			self.stack.append((tag, classes, kind))

	def handle_endtag(self, tag: str) -> None:
		self.handle_data("\n" if tag in {"p", "div", "li"} else "")
		for index in range(len(self.stack) - 1, -1, -1):
			if self.stack[index][0] == tag:
				if self.stack[index][2] == "ld":
					self.ld_blocks.append("".join(self.parts["ld"]))
					self.parts["ld"].clear()
				del self.stack[index:]
				break

	def handle_data(self, data: str) -> None:
		if any(tag == "script" and kind == "ld" for tag, _, kind in self.stack):
			self.parts["ld"].append(data)
			return
		if any(tag in {"script", "style", "noscript", "template"} for tag, _, _ in self.stack):
			return
		self.parts["plain"].append(data)
		for key in {kind for _, _, kind in self.stack if kind}:
			self.parts[key].append(data)

	def text(self, key: str) -> str:
		return "\n".join(line.strip() for line in "".join(self.parts[key]).splitlines() if line.strip())


def parse_job_page(html: str) -> dict[str, str]:
	page = _JobPage()
	page.feed(html)
	job = {key: page.text(key) for key in ("title", "company", "description", "salary")}
	# JSON-LD is optional. Never treat navigation text or a login wall as a JD.
	for script in page.ld_blocks:
		try:
			value = json.loads(script)
		except ValueError:
			continue
		entries = value if isinstance(value, list) else [value]
		for entry in entries:
			if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
				continue
			if isinstance(entry.get("description"), str):
				text = _JobPage()
				text.feed(entry["description"])
				job["description"] = text.text("plain")
			if isinstance(entry.get("title"), str):
				job["title"] = entry["title"]
			company = entry.get("hiringOrganization")
			if isinstance(company, dict) and isinstance(company.get("name"), str):
				job["company"] = company["name"]
	if len(job["description"]) < 20 or len(job["description"]) > 12000:
		raise ValueError("未读到完整岗位描述，请复制页面中的岗位职责与要求后粘贴生成")
	return {key: value[:160] if key != "description" else value for key, value in job.items()}


def read_public_job(url: str) -> dict[str, str]:
	try:
		# No logged-in browser, bridge, cookies, navigation, redirects, or retry queue.
		with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
			with client.stream("GET", url) as response:
				if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").lower():
					raise ValueError("岗位链接暂时无法读取，请复制页面中的岗位描述后粘贴生成")
				chunks, size, started = [], 0, time.monotonic()
				for chunk in response.iter_bytes():
					size += len(chunk)
					if size > 2_000_000 or time.monotonic() - started > 15:
						raise ValueError("岗位页面过大或读取超时，请直接粘贴岗位描述")
					chunks.append(chunk)
				return parse_job_page(b"".join(chunks).decode("utf-8", errors="replace"))
	except httpx.HTTPError as exc:
		raise ValueError("岗位链接读取失败，请复制页面中的岗位描述后粘贴生成") from exc


class GreetingAssistant:
	def __init__(self, context: Callable[[], tuple[Any, str, dict[str, str]]]) -> None:
		self._context = context
		self._mutex = threading.RLock()
		self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boss-solo-greeting")
		self._state: dict[str, Any] = {"status": "idle"}

	def state(self) -> dict[str, Any]:
		with self._mutex:
			return copy.deepcopy(self._state)

	def start(self, body: dict[str, Any]) -> dict[str, Any]:
		job = greeting_input(body)
		with self._mutex:
			if self._state["status"] == "running":
				raise ValueError("单独招呼正在生成，请等待这次完成")
			self._state = {"status": "running", "id": uuid.uuid4().hex, "input": job, "started_at": time.time()}
			self._executor.submit(self._run, job)
			return self.state()

	def _run(self, job: dict[str, str]) -> None:
		try:
			service, resume, profile = self._context()
			if not job["description"]:
				page = read_public_job(job["url"])
				job = {**page, **{key: value for key, value in job.items() if value}}
			result = generate_draft(service, job, resume, timeout=45)
			outcome = {"status": "complete", "job": job, "result": result, "profile": profile}
		except Exception as exc:
			outcome = {"status": "error", "error": redact_sensitive_text(str(exc))[:400] or "生成失败，请重试"}
		with self._mutex:
			self._state.update(**outcome, finished_at=time.time())

	def close(self) -> None:
		self._executor.shutdown(wait=True)
