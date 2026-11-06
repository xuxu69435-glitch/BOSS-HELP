"""Screenshot decoding stays bounded and local, including failed OCR attempts."""

import base64
import json
import os
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from boss_agent_cli.screenshot_ocr import MAX_IMAGE_BYTES, recognize_screenshot, screenshot_bytes


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4wAAAABJRU5ErkJggg==")


def image(data=PNG):
	return {"image": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}


@pytest.mark.parametrize("body", [
	{}, {"image": 123}, {"image": "https://example.test/private.png"},
	{"image": "data:image/svg+xml;base64,PHN2Zz4="}, {"image": "data:image/png;base64,%%%"},
	image(b"not a PNG"), image(PNG[:16] + struct.pack(">II", 0, 1) + PNG[24:]),
	image(PNG[:16] + struct.pack(">II", 10001, 1) + PNG[24:]),
	image(PNG[:16] + struct.pack(">II", 5000, 5000) + PNG[24:]),
	image(PNG + b"x" * MAX_IMAGE_BYTES),
])
def test_invalid_images_never_start_ocr(body):
	with patch("boss_agent_cli.screenshot_ocr.subprocess.run") as run:
		with pytest.raises(ValueError):
			recognize_screenshot(body)
		run.assert_not_called()


def test_valid_screenshot_decodes_in_memory():
	assert screenshot_bytes(image()) == PNG


def test_non_windows_has_actionable_error():
	with patch("boss_agent_cli.screenshot_ocr.os.name", "posix"):
		with pytest.raises(ValueError, match="Windows"):
			recognize_screenshot(image())


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR subprocess")
def test_windows_ocr_keeps_image_on_stdin_and_cleans_chinese_spacing(monkeypatch):
	run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"text": "  岗 位 职 责  \n\nPython and Agent 应 用 开 发"})))
	monkeypatch.setattr("boss_agent_cli.screenshot_ocr.subprocess.run", run)
	assert recognize_screenshot(image()) == {"text": "岗位职责\nPython and Agent 应用开发"}
	args, kwargs = run.call_args
	assert args[0][0].lower().endswith("windowspowershell\\v1.0\\powershell.exe")
	assert args[0][1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
	assert base64.b64decode(kwargs["input"]) == PNG
	assert kwargs["timeout"] == 35 and kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
	assert not any("iVBOR" in argument for argument in args[0])


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR subprocess")
@pytest.mark.parametrize("result,message", [
	(SimpleNamespace(returncode=0, stdout='{"error":"language"}'), "中文OCR语言包"),
	(SimpleNamespace(returncode=0, stdout='{"error":"dimensions"}'), "分辨率"),
	(SimpleNamespace(returncode=0, stdout='{"error":"failed"}'), "识别失败"),
	(SimpleNamespace(returncode=1, stdout="private details"), "识别失败"),
	(SimpleNamespace(returncode=0, stdout="invalid json"), "识别失败"),
	(SimpleNamespace(returncode=0, stdout='{"text":123}'), "识别失败"),
	(SimpleNamespace(returncode=0, stdout='{"text":"  "}'), "未识别到文字"),
	(SimpleNamespace(returncode=0, stdout=json.dumps({"text": "x" * 12001})), "12000"),
	(subprocess.TimeoutExpired("private command", 35), "超时"),
])
def test_ocr_failure_is_actionable_and_next_screenshot_can_retry(monkeypatch, result, message):
	run = Mock()
	if isinstance(result, Exception):
		run.side_effect = result
	else:
		run.return_value = result
	monkeypatch.setattr("boss_agent_cli.screenshot_ocr.subprocess.run", run)
	with pytest.raises(ValueError, match=message) as error:
		recognize_screenshot(image())
	assert "private" not in str(error.value)
	run.side_effect = None
	run.return_value = SimpleNamespace(returncode=0, stdout='{"text":"Python岗位职责"}')
	assert recognize_screenshot(image()) == {"text": "Python岗位职责"}
