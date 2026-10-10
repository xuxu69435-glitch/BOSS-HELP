from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from boss_agent_cli.output import Logger
from boss_agent_cli.wizard.prompts import (
	MenuOption,
	WizardCancelled,
	WizardControl,
	_main_menu_options,
	collect_wizard_input,
)


class _ScriptedMenu:
	def __init__(self, answers: list[str]) -> None:
		self.answers = iter(answers)
		self.menus: list[tuple[str, list[MenuOption]]] = []

	def select(self, title: str, options: list[MenuOption], **kwargs: object) -> str:
		self.menus.append((title, list(options)))
		return next(self.answers)

	def text(self, label: str, **kwargs: object) -> str:
		raise AssertionError(f"unexpected text prompt: {label}")


def _ctx(tmp_path: Path) -> SimpleNamespace:
	return SimpleNamespace(
		obj={
			"data_dir": tmp_path,
			"logger": Logger(),
			"platform": "zhipin",
			"role": "candidate",
			"cdp_url": "http://127.0.0.1:9222",
		}
	)


def test_main_menu_exposes_platform_relogin_without_task_selection() -> None:
	options = _main_menu_options([], platform="zhipin")
	relogin = next(item for item in options if item.value == "relogin")
	assert relogin.label == "重新登录 BOSS 直聘"
	assert "更新当前登录状态" in relogin.description

	menu = _ScriptedMenu(["relogin"])
	selection = collect_wizard_input(
		default_role="candidate",
		default_platform="zhipin",
		menu=menu,
	)
	assert selection == WizardControl(action="relogin")
	assert [title for title, _options in menu.menus] == ["请选择要进行的操作"]


def test_login_interactively_reuses_safe_login_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli.wizard import preflight

	auth = MagicMock()
	auth_manager = MagicMock(return_value=auth)
	monkeypatch.setattr(preflight, "AuthManager", auth_manager)
	monkeypatch.setattr(preflight, "_browser_kernel_status", lambda: ("ok", ""))
	monkeypatch.setattr("boss_agent_cli.wizard.renderer.busy_status", lambda _message: nullcontext())
	monkeypatch.setattr(preflight.display.console, "print", lambda *args, **kwargs: None)

	ctx = _ctx(tmp_path)
	assert preflight.login_interactively(ctx) is True
	auth_manager.assert_called_once_with(tmp_path, logger=ctx.obj["logger"], platform="zhipin")
	auth.login.assert_called_once_with(timeout=180, cdp_url="http://127.0.0.1:9222")
	auth.logout.assert_not_called()


def test_login_interactively_keeps_old_session_when_login_fails(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from boss_agent_cli.wizard import preflight

	auth = MagicMock()
	auth.login.side_effect = TimeoutError("login timeout")
	monkeypatch.setattr(preflight, "AuthManager", MagicMock(return_value=auth))
	monkeypatch.setattr(preflight, "_browser_kernel_status", lambda: ("ok", ""))
	monkeypatch.setattr("boss_agent_cli.wizard.renderer.busy_status", lambda _message: nullcontext())
	rendered: list[Exception] = []
	monkeypatch.setattr(preflight, "_render_login_failure", lambda _ctx, exc: rendered.append(exc))

	assert preflight.login_interactively(_ctx(tmp_path)) is False
	assert len(rendered) == 1
	auth.logout.assert_not_called()


@pytest.mark.parametrize(
	("login_succeeds", "expected_local_only"),
	[(True, [True, False]), (False, [True, True])],
)
def test_relogin_control_only_unlocks_online_goals_after_success(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	login_succeeds: bool,
	expected_local_only: list[bool],
) -> None:
	from boss_agent_cli.commands import wizard as wizard_command
	from boss_agent_cli.wizard import preflight

	store = MagicMock()
	store.list_recent.return_value = []
	local_only_values: list[bool] = []

	def collect(**kwargs: object) -> WizardControl:
		local_only_values.append(bool(kwargs["local_only"]))
		if len(local_only_values) == 1:
			return WizardControl(action="relogin")
		raise WizardCancelled

	monkeypatch.setattr(preflight, "ensure_login", lambda _ctx: preflight.GATE_LOCAL_ONLY)
	login = MagicMock(return_value=login_succeeds)
	monkeypatch.setattr(preflight, "login_interactively", login)
	monkeypatch.setattr(wizard_command, "collect_wizard_input", collect)
	monkeypatch.setattr(wizard_command, "clear_wizard_screen", lambda: None)
	monkeypatch.setattr(wizard_command, "ask_continue_session", lambda **kwargs: True)
	monkeypatch.setattr(wizard_command, "render_cancelled", lambda: None)

	ctx = _ctx(tmp_path)
	wizard_command._run_interactive_session(
		ctx,
		store,
		timeout_seconds=None,
		max_retries=0,
	)

	login.assert_called_once_with(ctx)
	assert local_only_values == expected_local_only
