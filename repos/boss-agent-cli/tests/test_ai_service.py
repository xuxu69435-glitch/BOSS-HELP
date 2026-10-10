"""Tests for AI service client."""

from unittest.mock import patch
import asyncio

import httpx
import pytest

from boss_agent_cli.ai.service import AIService, AIServiceError


def _make_service(**kwargs) -> AIService:
	defaults = {
		"base_url": "https://api.example.com/v1",
		"api_key": "sk-test-key",
		"model": "gpt-4",
		"temperature": 0.7,
		"max_tokens": 4096,
	}
	defaults.update(kwargs)
	return AIService(**defaults)


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
	"""Create a mock httpx.Response."""
	if json_data is None:
		json_data = {
			"choices": [{"message": {"content": "Hello, world!"}}]
		}
	return httpx.Response(
		status_code=status_code,
		json=json_data,
		request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
	)


# ── successful call ──────────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_success(mock_post):
	"""Successful chat call returns the assistant's reply."""
	mock_post.return_value = _mock_response(200, {"choices": [{"message": {"content": "Test reply"}}]})
	service = _make_service()
	result = service.chat([{"role": "user", "content": "Hi"}])
	assert result == "Test reply"


# ── request validation ───────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_request_headers(mock_post):
	"""Request includes Authorization header and Content-Type."""
	mock_post.return_value = _mock_response()
	service = _make_service(api_key="sk-my-key")
	service.chat([{"role": "user", "content": "Hi"}])

	call_kwargs = mock_post.call_args
	headers = call_kwargs.kwargs["headers"]
	assert headers["Authorization"] == "Bearer sk-my-key"
	assert headers["Content-Type"] == "application/json"


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_request_body(mock_post):
	"""Request body includes model, messages, temperature, max_tokens."""
	mock_post.return_value = _mock_response()
	service = _make_service(model="gpt-4o", temperature=0.5, max_tokens=2048)
	service.chat([{"role": "user", "content": "Hello"}])

	call_kwargs = mock_post.call_args
	payload = call_kwargs.kwargs["json"]
	assert payload["model"] == "gpt-4o"
	assert payload["messages"] == [{"role": "user", "content": "Hello"}]
	assert payload["temperature"] == 0.5
	assert payload["max_tokens"] == 2048
	assert "response_format" not in payload


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_json_output_mode(mock_post):
	mock_post.return_value = _mock_response()
	service = _make_service()
	service.chat([{"role": "user", "content": "Return JSON"}], response_format={"type": "json_object"})
	assert mock_post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}


@patch("boss_agent_cli.ai.service.httpx.post")
def test_deepseek_json_drafts_disable_reasoning_without_changing_the_model(mock_post):
	mock_post.return_value = _mock_response()
	service = _make_service(base_url="https://api.deepseek.com/v1", model="deepseek-flash")
	service.chat([{"role": "user", "content": "JSON draft"}], response_format={"type": "json_object"})
	payload = mock_post.call_args.kwargs["json"]
	assert payload["thinking"] == {"type": "disabled"}
	assert payload["model"] == "deepseek-flash"
	service.chat([{"role": "user", "content": "Ordinary conversation"}])
	assert "thinking" not in mock_post.call_args.kwargs["json"]


@pytest.mark.parametrize("base_url,model", [("https://api.example.com", "deepseek-flash"), ("https://api.deepseek.com", "deepseek-reasoner")])
@patch("boss_agent_cli.ai.service.httpx.post")
def test_other_providers_and_reasoner_keep_their_options(mock_post, base_url, model):
	mock_post.return_value = _mock_response()
	_make_service(base_url=base_url, model=model).chat([], response_format={"type": "json_object"})
	assert "thinking" not in mock_post.call_args.kwargs["json"]


@patch("boss_agent_cli.ai.service.httpx.post")
def test_truncated_output_is_a_clear_error_not_a_usable_draft(mock_post):
	mock_post.return_value = _mock_response(200, {"choices": [{"finish_reason": "length", "message": {"content": '{"summary":'}}]})
	with pytest.raises(AIServiceError, match="截断"):
		_make_service().chat([])


def test_draft_timeout_is_an_overall_deadline_and_cancels_the_request(monkeypatch):
	cancelled = []
	class SlowClient:
		def __init__(self, **kwargs):
			pass
		async def __aenter__(self):
			return self
		async def __aexit__(self, *args):
			pass
		async def post(self, *args, **kwargs):
			try:
				await asyncio.sleep(10)
			finally:
				cancelled.append(True)

	monkeypatch.setattr("boss_agent_cli.ai.service.httpx.AsyncClient", SlowClient)
	with pytest.raises(AIServiceError, match="时限"):
		_make_service().chat([], timeout=0.01)
	assert cancelled == [True]


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_url_construction(mock_post):
	"""URL is base_url + /chat/completions."""
	mock_post.return_value = _mock_response()
	service = _make_service(base_url="https://api.example.com/v1")
	service.chat([{"role": "user", "content": "Hi"}])

	call_args = mock_post.call_args
	assert call_args.args[0] == "https://api.example.com/v1/chat/completions"


# ── HTTP errors ──────────────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_http_401(mock_post):
	"""HTTP 401 raises AIServiceError with status_code."""
	mock_post.return_value = _mock_response(401, {"error": {"message": "Unauthorized"}})
	service = _make_service()
	with pytest.raises(AIServiceError) as exc_info:
		service.chat([{"role": "user", "content": "Hi"}])
	assert exc_info.value.status_code == 401


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_http_500(mock_post):
	"""HTTP 500 raises AIServiceError with status_code."""
	mock_post.return_value = _mock_response(500, {"error": {"message": "Internal Server Error"}})
	service = _make_service()
	with pytest.raises(AIServiceError) as exc_info:
		service.chat([{"role": "user", "content": "Hi"}])
	assert exc_info.value.status_code == 500


# ── network error ────────────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_network_error(mock_post):
	"""Network error raises AIServiceError without status_code."""
	mock_post.side_effect = httpx.ConnectError("Connection refused")
	service = _make_service()
	with pytest.raises(AIServiceError) as exc_info:
		service.chat([{"role": "user", "content": "Hi"}])
	assert exc_info.value.status_code is None
	assert "网络请求失败" in str(exc_info.value)


# ── response format error ────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_malformed_response(mock_post):
	"""Malformed response raises AIServiceError."""
	mock_post.return_value = _mock_response(200, {"unexpected": "format"})
	service = _make_service()
	with pytest.raises(AIServiceError) as exc_info:
		service.chat([{"role": "user", "content": "Hi"}])
	assert "响应格式异常" in str(exc_info.value)


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_empty_choices(mock_post):
	"""Empty choices array raises AIServiceError."""
	mock_post.return_value = _mock_response(200, {"choices": []})
	service = _make_service()
	with pytest.raises(AIServiceError) as exc_info:
		service.chat([{"role": "user", "content": "Hi"}])
	assert "响应格式异常" in str(exc_info.value)


# ── parameter overrides ──────────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_temperature_override(mock_post):
	"""Per-call temperature override works."""
	mock_post.return_value = _mock_response()
	service = _make_service(temperature=0.7)
	service.chat([{"role": "user", "content": "Hi"}], temperature=0.2)

	payload = mock_post.call_args.kwargs["json"]
	assert payload["temperature"] == 0.2


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_max_tokens_override(mock_post):
	"""Per-call max_tokens override works."""
	mock_post.return_value = _mock_response()
	service = _make_service(max_tokens=4096)
	service.chat([{"role": "user", "content": "Hi"}], max_tokens=1024)

	payload = mock_post.call_args.kwargs["json"]
	assert payload["max_tokens"] == 1024


# ── base_url normalization ───────────────────────────────────


@patch("boss_agent_cli.ai.service.httpx.post")
def test_chat_base_url_trailing_slash(mock_post):
	"""Trailing slash in base_url is handled."""
	mock_post.return_value = _mock_response()
	service = _make_service(base_url="https://api.example.com/v1/")
	service.chat([{"role": "user", "content": "Hi"}])

	url = mock_post.call_args.args[0]
	assert url == "https://api.example.com/v1/chat/completions"
	assert "//" not in url.replace("https://", "")
