"""Starting the local bridge must not probe Windows PIDs with kill()."""

from unittest.mock import Mock

from boss_agent_cli.bridge import daemon


def test_reuses_healthy_daemon_without_pid_probes(monkeypatch):
	monkeypatch.setattr("boss_agent_cli.bridge.client.BridgeClient.status", Mock(return_value={"ok": True, "pid": 123}))
	probe = Mock(side_effect=AssertionError("No signal-based process probe"))
	spawn = Mock(side_effect=AssertionError("No duplicate daemon"))
	monkeypatch.setattr(daemon.os, "kill", probe)
	monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
	assert daemon.start_daemon_background() == 123
	probe.assert_not_called()
	spawn.assert_not_called()


def test_starts_hidden_daemon_and_waits_for_health(tmp_path, monkeypatch):
	monkeypatch.setattr(
		"boss_agent_cli.bridge.client.BridgeClient.status", Mock(side_effect=[None, {"ok": True, "pid": 456}])
	)
	monkeypatch.setattr(daemon, "_PID_FILE", tmp_path / "daemon.pid")
	monkeypatch.setattr(daemon, "_LOG_FILE", tmp_path / "daemon.log")
	monkeypatch.setattr(daemon.time, "sleep", lambda _: None)
	monkeypatch.setattr(daemon.os, "kill", Mock(side_effect=AssertionError("No signal-based process probe")))
	spawn = Mock(return_value=Mock(pid=456))
	monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
	assert daemon.start_daemon_background() == 456
	assert spawn.call_args.args[0][1:] == ["-m", "boss_agent_cli.bridge.daemon", "--serve"]
	assert spawn.call_args.kwargs["stdin"] == daemon.subprocess.DEVNULL
	if daemon.sys.platform == "win32":
		assert spawn.call_args.kwargs["creationflags"] & daemon.subprocess.DETACHED_PROCESS
