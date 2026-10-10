"""Regression checks for the Windows double-click entry point."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "打开求职客户端.bat"
GUI_LAUNCHER = LAUNCHER.with_suffix(".vbs")
SCRIPT = ROOT / "scripts" / "start_client.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
WSCRIPT = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/wscript.exe"
VENV = ROOT / "venvs" / "boss-agent-cli"
pytestmark = pytest.mark.skipif(not LAUNCHER.exists(), reason="Local client launcher is not installed")


def test_batch_uses_ascii_and_windows_line_endings():
	content = LAUNCHER.read_bytes()
	content.decode("ascii")
	assert b"\r\n" in content
	assert b"\n" not in content.replace(b"\r\n", b"")
	assert b'wscript.exe" //nologo "%~dpn0.vbs"' in content
	assert b"pause" not in content
	assert b"powershell.exe" not in content


def test_gui_launcher_is_hidden_and_keeps_failure_reporting():
	content = GUI_LAUNCHER.read_text(encoding="ascii")
	assert "WScript.ScriptFullName" in content
	assert "shell.Run(command, 0, True)" in content
	assert "-WindowStyle Hidden" in content and "-OpenBrowser" in content
	assert "If result <> 0 Then" in content and "MsgBox" in content
	assert "client-launcher.log" in content and "WScript.Quit 1" in content
	script = SCRIPT.read_text(encoding="utf-8")
	assert "Scripts\\pythonw.exe'" in script
	assert "Scripts\\python.exe'" not in script


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell entry point")
def test_startup_failure_is_logged_from_a_different_working_directory(tmp_path):
	root = tmp_path / "client with spaces 求职"
	(root / "scripts").mkdir(parents=True)
	script = root / "scripts" / SCRIPT.name
	shutil.copyfile(SCRIPT, script)
	# This fixture deliberately has no Python environment and cannot contact BOSS.
	result = subprocess.run(
		[str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-OpenBrowser"],
		cwd=tmp_path,
		stdin=subprocess.DEVNULL,
		capture_output=True,
		creationflags=subprocess.CREATE_NO_WINDOW,
		timeout=15,
	)
	output = result.stdout + result.stderr
	assert result.returncode == 1
	assert b"Project Python environment not found" in output
	assert b"client_token=" not in output
	log = (root / "logs" / "client-launcher.log").read_text(encoding="utf-8-sig")
	assert "Project Python environment not found" in log
	assert "client_token=" not in log


@pytest.mark.skipif(os.name != "nt" or not VENV.exists(), reason="Installed Windows GUI runtime")
def test_windowless_cold_start_and_reopen_reuse_the_real_client(tmp_path):
	from client_fixture import fixture_client

	root = tmp_path / "client with spaces 求职"
	data = root / "data" / "boss-agent"
	controller, batch_id = fixture_client(data)
	controller.close()
	settings = data / "preferences" / "settings.json"
	settings.parent.mkdir()
	shutil.copyfile(data / "preferences.json", settings)
	venv = root / "venvs" / "boss-agent-cli"
	(venv / "Scripts").mkdir(parents=True)
	shutil.copyfile(VENV / "Scripts" / "pythonw.exe", venv / "Scripts" / "pythonw.exe")
	shutil.copyfile(VENV / "pyvenv.cfg", venv / "pyvenv.cfg")
	(root / "scripts").mkdir()
	shutil.copyfile(GUI_LAUNCHER, root / GUI_LAUNCHER.name)
	shutil.copyfile(SCRIPT, root / "scripts" / "start_client_impl.ps1")
	# Only intercept opening the default browser; service startup and authentication are real.
	(root / "scripts" / SCRIPT.name).write_text(
		'''param([switch]$OpenBrowser)
function Start-Process {
    [CmdletBinding()]
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    if ($FilePath -like 'http://127.0.0.1:*') {
        Add-Content -LiteralPath (Join-Path $PSScriptRoot 'browser-opened.txt') -Value 'opened'
    } else {
        Microsoft.PowerShell.Management\\Start-Process @PSBoundParameters
    }
}
& (Join-Path $PSScriptRoot 'start_client_impl.ps1') -OpenBrowser:$OpenBrowser
''',
		encoding="ascii",
	)
	# Measure console allocation inside the actual pythonw process, before the service starts.
	(root / "sitecustomize.py").write_text(
		'''import ctypes, json, os, sys
from pathlib import Path
Path(__file__).with_name("console-probe.json").write_text(json.dumps({
    "pid": os.getpid(), "console": ctypes.windll.kernel32.GetConsoleWindow(),
    "stdout_missing": sys.stdout is None, "executable": sys.executable,
}))
''',
		encoding="ascii",
	)
	env = os.environ.copy()
	env["PYTHONPATH"] = os.pathsep.join([str(root), str(SCRIPT.parents[1] / "repos/boss-agent-cli/src"), str(VENV / "Lib/site-packages")])
	runtime_path = data / "client-control" / "runtime.json"
	info = None
	try:
		for _ in range(2):
			result = subprocess.run(
				[str(WSCRIPT), "//nologo", str(root / GUI_LAUNCHER.name)],
				cwd=tmp_path,
				env=env,
				stdin=subprocess.DEVNULL,
				capture_output=True,
				timeout=35,
			)
			assert result.returncode == 0
			assert b"client_token=" not in result.stdout + result.stderr
			current = json.loads(runtime_path.read_text(encoding="utf-8"))
			if info is not None:
				assert current == info
			info = current
		assert (root / "scripts" / "browser-opened.txt").read_text().splitlines() == ["opened", "opened"]
		probe = json.loads((root / "console-probe.json").read_text())
		assert probe["pid"] == info["pid"] and probe["console"] == 0 and probe["stdout_missing"]
		assert Path(probe["executable"]).name == "pythonw.exe"
		assert (data / "client-control" / "server.log").exists()
		with urlopen(Request(info["url"] + "/api/state", headers={"X-Client-Token": info["launch_token"]}), timeout=5) as response:
			state = json.load(response)
		assert state["operation"]["status"] == "idle"
		assert state["batches"][0]["id"] == batch_id
		assert not (data / "client-control" / "events.jsonl").exists()
	finally:
		if runtime_path.exists():
			info = json.loads(runtime_path.read_text(encoding="utf-8"))
			request = Request(
				info["url"] + "/api/shutdown", data=b"{}",
				headers={"Origin": info["url"], "X-Client-Token": info["launch_token"], "Content-Type": "application/json"},
			)
			with urlopen(request, timeout=5):
				pass
			# Wait for the fixture service to exit before pytest removes its private directory.
			import ctypes
			from ctypes import wintypes

			kernel = ctypes.WinDLL("kernel32", use_last_error=True)
			kernel.OpenProcess.restype = wintypes.HANDLE
			handle = kernel.OpenProcess(0x00100000, False, info["pid"])
			if handle:
				try:
					kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
					assert kernel.WaitForSingleObject(handle, 10000) == 0
				finally:
					kernel.CloseHandle.argtypes = [wintypes.HANDLE]
					kernel.CloseHandle(handle)
