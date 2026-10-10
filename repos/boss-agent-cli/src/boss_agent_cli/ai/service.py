"""AI service client for OpenAI-compatible APIs.

Provides a simple interface for chat completions with error handling.
"""

import asyncio
from typing import Any, cast
from urllib.parse import urlparse

import httpx


class AIServiceError(Exception):
	"""Raised when an AI service call fails."""

	def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
		super().__init__(message)
		self.status_code = status_code
		self.retryable = retryable


class AIService:
	"""Client for OpenAI-compatible chat completion APIs."""

	def __init__(
		self,
		base_url: str,
		api_key: str,
		model: str,
		temperature: float = 0.7,
		max_tokens: int = 4096,
	):
		# Normalize base_url: strip trailing slash
		self.base_url = base_url.rstrip("/")
		self.api_key = api_key
		self.model = model
		self.temperature = temperature
		self.max_tokens = max_tokens

	def chat(
		self,
		messages: list[dict[str, Any]],
		*,
		temperature: float | None = None,
		max_tokens: int | None = None,
		response_format: dict[str, str] | None = None,
		timeout: float | None = None,
	) -> str:
		"""Send a chat completion request and return the assistant's reply text.

		Args:
			messages: List of message dicts with 'role' and 'content' keys.
			temperature: Override default temperature for this call.
			max_tokens: Override default max_tokens for this call.
			response_format: Optional provider-supported structured output mode.

		Returns:
			The assistant's reply text.

		Raises:
			AIServiceError: On HTTP errors, network errors, or unexpected response format.
		"""
		url = f"{self.base_url}/chat/completions"
		headers = {
			"Authorization": f"Bearer {self.api_key}",
			"Content-Type": "application/json",
		}
		payload = {
			"model": self.model,
			"messages": messages,
			"temperature": temperature if temperature is not None else self.temperature,
			"max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
		}
		if response_format is not None:
			payload["response_format"] = response_format
			if (
				response_format.get("type") == "json_object"
				and urlparse(self.base_url).hostname == "api.deepseek.com"
				and self.model in {"deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"}
			):
				# Short structured drafts must not spend their entire output budget on reasoning.
				payload["thinking"] = {"type": "disabled"}

		try:
			if timeout is None:
				response = httpx.post(url, json=payload, headers=headers, timeout=60)
			else:
				async def bounded_post() -> httpx.Response:
					async with httpx.AsyncClient(timeout=timeout) as client:
						return await asyncio.wait_for(client.post(url, json=payload, headers=headers), timeout=timeout)

				response = asyncio.run(bounded_post())
			response.raise_for_status()
		except asyncio.TimeoutError as exc:
			raise AIServiceError("模型请求达到本次文案时限，已保留岗位资料", retryable=True) from exc
		except httpx.HTTPStatusError as exc:
			raise AIServiceError(
				f"API 请求失败: HTTP {exc.response.status_code}",
				status_code=exc.response.status_code,
				retryable=exc.response.status_code in {500, 502, 503, 504},
			) from exc
		except httpx.RequestError as exc:
			raise AIServiceError(f"网络请求失败: {exc}", retryable=isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError))) from exc

		try:
			data = response.json()
			if data["choices"][0].get("finish_reason") == "length":
				raise AIServiceError("模型输出长度达到上限，内容被截断，未作为有效文案保存")
			return cast("str", data["choices"][0]["message"]["content"])
		except (KeyError, IndexError, TypeError) as exc:
			raise AIServiceError(f"响应格式异常: {exc}") from exc
