"""Versioned local profile editing. Saving never calls a model or a job platform."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from boss_agent_cli.resume.models import dict_to_resume, resume_to_text


def _text(value: Any, maximum: int, label: str) -> str:
	if not isinstance(value, str) or len(value) > maximum or "\0" in value:
		raise ValueError(f"{label}格式无效或内容过长")
	return value


def validate_profile(data: Any, name: str) -> dict[str, Any]:
	if not isinstance(data, dict) or data.get("name") != name:
		raise ValueError("资料名称不匹配，请重新加载当前资料")
	if len(json.dumps(data, ensure_ascii=False).encode()) > 60000:
		raise ValueError("资料过大，请精简到60KB以内")
	allowed = {"name", "title", "center_title", "personal_info", "job_intention", "modules", "avatar", "created_at", "updated_at"}
	if set(data) - allowed:
		raise ValueError("资料包含不支持的字段")
	result = copy.deepcopy(data)
	if not _text(result.get("title"), 160, "姓名或资料标题").strip():
		raise ValueError("请填写姓名或资料标题")
	if type(result.get("center_title", False)) is not bool:
		raise ValueError("标题布局无效")
	for key in ("avatar", "created_at", "updated_at"):
		_text(result.get(key, ""), 2048, "资料元信息")
	for section, maximum in (("personal_info", 40), ("job_intention", 30)):
		value = result.get(section)
		if section == "job_intention" and value is None:
			continue
		if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > maximum:
			raise ValueError("个人信息或求职安排格式无效")
		if section == "personal_info":
			if set(value) - {"items", "layout"}:
				raise ValueError("个人信息包含不支持的字段")
			_text(value.get("layout", "inline"), 40, "信息布局")
		else:
			if set(value) - {"title", "icon", "items", "show_background"}:
				raise ValueError("求职安排包含不支持的字段")
			_text(value.get("title", "求职安排"), 100, "求职安排标题")
			_text(value.get("icon", ""), 100, "图标")
			if type(value.get("show_background", True)) is not bool:
				raise ValueError("求职安排布局无效")
		for item in value["items"]:
			if not isinstance(item, dict) or set(item) - {"label", "value", "icon", "link"}:
				raise ValueError("个人信息条目无效")
			if not _text(item.get("label"), 80, "字段名称").strip():
				raise ValueError("个人信息的字段名称不能为空")
			_text(item.get("value"), 3000, "字段内容")
			if item["label"] == "每周可实习天数" and item["value"].isdecimal() and not 0 <= int(item["value"]) <= 7:
				raise ValueError("每周可实习天数必须为0至7天")
			_text(item.get("icon", ""), 100, "图标")
			link = _text(item.get("link", ""), 2048, "链接")
			if link:
				url = urlparse(link)
				if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
					raise ValueError("链接仅支持不含账号密码的HTTP或HTTPS地址")
	modules = result.get("modules")
	if not isinstance(modules, list) or len(modules) > 40:
		raise ValueError("资料模块数量无效")
	ids = set()
	for module in modules:
		if not isinstance(module, dict) or set(module) - {"id", "title", "icon", "rows"}:
			raise ValueError("资料模块格式无效")
		mid = _text(module.get("id"), 100, "模块标识")
		if not mid or mid in ids:
			raise ValueError("资料模块标识重复或为空")
		ids.add(mid)
		if not _text(module.get("title"), 160, "模块标题").strip():
			raise ValueError("模块标题不能为空")
		_text(module.get("icon", ""), 100, "图标")
		rows = module.get("rows")
		if not isinstance(rows, list) or len(rows) > 60:
			raise ValueError("模块条目数量无效")
		for row in rows:
			if not isinstance(row, dict) or set(row) - {"type", "content", "tags"} or row.get("type") not in {"richtext", "tags"}:
				raise ValueError("资料条目仅支持文本和技能标签")
			lines = row.get("tags" if row["type"] == "tags" else "content")
			if not isinstance(lines, list) or len(lines) > 200:
				raise ValueError("资料条目内容无效")
			for line in lines:
				_text(line, 6000, "资料正文")
	return result


def profile_model_text(data: dict[str, Any]) -> str:
	value = copy.deepcopy(data)
	# The storage name is a stable identifier, not the editable person's name.
	value["name"] = ""
	for section in ("personal_info", "job_intention"):
		if isinstance(value.get(section), dict):
			value[section]["items"] = [
				item for item in value[section].get("items", [])
				if not re.search(r"手机|电话|邮箱|邮件|微信|住址|联系地址|身份证", str(item.get("label", "")))
			]
			for item in value[section]["items"]:
				if item.get("link") and item["link"] not in item.get("value", ""):
					item["value"] += " " + item["link"]
	text = resume_to_text(dict_to_resume(value))
	text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", text)
	return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)


class ProfileStore:
	def __init__(self, data_dir: Path) -> None:
		path = data_dir / "client-control" / "profiles.db"
		path.parent.mkdir(parents=True, exist_ok=True)
		self.db = sqlite3.connect(path, timeout=10)
		self.db.row_factory = sqlite3.Row
		self.db.execute("PRAGMA journal_mode=WAL")
		self.db.execute("CREATE TABLE IF NOT EXISTS profile_versions (id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL)")

	def __enter__(self) -> ProfileStore:
		return self

	def __exit__(self, *args: Any) -> None:
		self.db.close()

	@staticmethod
	def base_version(base: dict[str, Any]) -> str:
		return "base-" + hashlib.sha256(json.dumps(base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

	def current(self, name: str, base: dict[str, Any]) -> dict[str, Any]:
		row = self.db.execute("SELECT * FROM profile_versions WHERE name=? ORDER BY rowid DESC LIMIT 1", (name,)).fetchone()
		return {"version": row["id"], "created_at": row["created_at"], "data": json.loads(row["payload"])} if row else {"version": self.base_version(base), "created_at": None, "data": copy.deepcopy(base)}

	def save(self, name: str, data: Any, *, expected_version: str, base: dict[str, Any]) -> dict[str, Any]:
		payload = validate_profile(data, name)
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			current = self.current(name, base)
			if current["version"] != expected_version:
				raise ValueError("资料已有更新，请重新加载后再保存；本次没有覆盖任何内容")
			version, created_at = uuid.uuid4().hex, time.time()
			self.db.execute("INSERT INTO profile_versions VALUES (?, ?, ?, ?)", (version, name, json.dumps(payload, ensure_ascii=False), created_at))
		return {"version": version, "created_at": created_at, "data": payload}

	def snapshot(self, name: str, base: dict[str, Any]) -> str:
		current = self.current(name, base)
		if current["version"].startswith("base-"):
			return self.save(name, base, expected_version=current["version"], base=base)["version"]
		return current["version"]

	def get(self, name: str, version: str) -> dict[str, Any]:
		row = self.db.execute("SELECT payload FROM profile_versions WHERE id=? AND name=?", (version, name)).fetchone()
		if row is None:
			raise ValueError("该批次使用的个人资料版本不存在，未改用其他资料")
		return json.loads(row["payload"])
