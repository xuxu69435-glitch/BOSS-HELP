"""Read job screenshots locally with Windows OCR, without a model or image files."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_REQUEST_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 512
_recognition_lock = threading.Lock()


def screenshot_bytes(body: dict[str, Any]) -> bytes:
	value = body.get("image")
	prefix = "data:image/png;base64,"
	if not isinstance(value, str) or not value.startswith(prefix):
		raise ValueError("请上传或粘贴有效的截图")
	encoded = value[len(prefix):]
	if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
		raise ValueError("截图不能超过8MB，请裁剪后重试")
	try:
		data = base64.b64decode(encoded, validate=True)
	except (ValueError, binascii.Error) as exc:
		raise ValueError("截图数据无效，请重新上传或粘贴") from exc
	if len(data) > MAX_IMAGE_BYTES:
		raise ValueError("截图不能超过8MB，请裁剪后重试")
	if len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
		raise ValueError("截图格式无效，请重新上传或粘贴")
	width, height = struct.unpack(">II", data[16:24])
	if not width or not height or max(width, height) > 10000 or width * height > 16_000_000:
		raise ValueError("截图分辨率过大或无效，请裁剪或分成多张截图")
	return data


def recognize_screenshot(body: dict[str, Any]) -> dict[str, str]:
	data = screenshot_bytes(body)
	if os.name != "nt":
		raise ValueError("截图识别需要在Windows客户端运行，可先粘贴岗位文字")
	if not _recognition_lock.acquire(blocking=False):
		raise ValueError("截图正在识别，请等待完成后重试")
	try:
		script = Path(__file__).with_name("screenshot_ocr.ps1").read_text(encoding="utf-8")
		powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
		try:
			result = subprocess.run(
				[str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode("ascii")],
				input=base64.b64encode(data).decode("ascii"), capture_output=True, encoding="utf-8",
				timeout=35, creationflags=subprocess.CREATE_NO_WINDOW,
			)
		except subprocess.TimeoutExpired as exc:
			raise ValueError("截图识别超时，请裁剪为较小的截图后重试") from exc
		except OSError as exc:
			raise ValueError("本机截图识别暂不可用，请先粘贴岗位文字") from exc
		try:
			value = json.loads(result.stdout) if result.returncode == 0 else {}
			text = value.get("text", "")
			if not isinstance(text, str):
				raise ValueError("Invalid OCR response")
		except (ValueError, AttributeError) as exc:
			raise ValueError("截图识别失败，请重新上传清晰的截图") from exc
		errors = {
			"language": "本机未安装中文OCR语言包，请在Windows语言设置中添加中文及文字识别功能后重试",
			"dimensions": "截图分辨率超过本机识别范围，请裁剪或分成多张截图",
		}
		if value.get("error") or result.returncode != 0:
			raise ValueError(errors.get(value.get("error"), "截图识别失败，请重新上传清晰的截图"))
		text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
		text = re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", text)
		if not text:
			raise ValueError("未识别到文字，请上传包含岗位职责和要求的清晰截图")
		if len(text) > 12000:
			raise ValueError("截图文字超过12000字，请裁剪为岗位相关内容")
		return {"text": text}
	finally:
		_recognition_lock.release()
