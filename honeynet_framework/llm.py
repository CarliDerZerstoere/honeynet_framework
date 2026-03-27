"""
LLM interface for the Honeynet Framework.

Supports multiple backends: Ollama, OpenAI, Anthropic.
"""

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from typing import Callable, Optional

import httpx

from .exceptions import (
    LLMConnectionError,
    LLMResponseError,
    ModelNotFoundError,
)
from .utils import safe_deep_get

logger = logging.getLogger(__name__)


def _api_error_message(response: httpx.Response) -> str:
    """Extract error message from API JSON body (OpenAI/Anthropic style)."""
    try:
        body = response.json()
        err = body.get("error") or body
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
        if isinstance(err, dict) and err.get("error"):
            return str(err["error"])
    except Exception:
        pass
    return response.text or response.reason_phrase or f"HTTP {response.status_code}"


@dataclass
class LLMConfig:
    """Configuration for LLM backend."""
    provider: str = "ollama"  # ollama, openai, anthropic
    model: str = "qwen2.5-coder:32b"
    base_url: str = "http://localhost:11434"
    api_key: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 8192
    timeout: float = 1200.0  # 20 minutes (model load + first response for large models)
    # Ollama only: keep_alive in seconds; -1 = keep loaded indefinitely, 0 = unload after response
    keep_alive: Optional[float] = -1


@dataclass
class TokenUsage:
    """Token counts reported by the LLM backend for a single generate() call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate a response from the LLM.

        Returns:
            A ``(content, token_usage)`` tuple.  ``token_usage`` carries the
            prompt/completion token counts reported by the backend; callers
            accumulate these to enforce budgets or log cost.
        """
        ...

    async def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tool_rounds: int = 10,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate with tool use support.

        Parameters
        ----------
        max_tokens :
            Override the default ``config.max_tokens`` for this call.
            Useful for dynamically sizing the output budget based on
            expected response length (e.g. large WorldModel YAML).

        The LLM may call tools zero or more times mid-generation before producing
        its final text response.  ``tool_executor(tool_name, tool_input)`` is
        called for each tool invocation and must return a JSON-serialisable string.

        Tools are defined in Anthropic input_schema format::

            {
                "name": "validate_docker_image",
                "description": "...",
                "input_schema": {"type": "object", "properties": {"image_ref": {...}}}
            }

        Default implementation: falls back to plain ``generate()`` (no tool use).
        Override in providers that support tool use natively.
        """
        logger.debug(
            "Provider %s does not override generate_with_tools — using plain generate()",
            type(self).__name__,
        )
        return await self.generate(prompt, system_message=system_message, temperature=temperature)


class OllamaProvider(LLMProvider):
    """Ollama LLM provider."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._ollama_api_mode: Optional[str] = None
        # Use explicit timeout for both connect and read operations
        # Large models (72b) can take a long time to load and respond
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=30.0,  # 30 seconds to connect
                read=config.timeout,  # Full timeout for reading response
                write=30.0,  # 30 seconds to write request
                pool=30.0,  # 30 seconds to acquire connection from pool
            ),
        )

    async def _post_ollama_with_fallback(
        self,
        *,
        chat_payload: dict,
        generate_payload: dict,
    ) -> tuple[str, httpx.Response]:
        """Post to Ollama, falling back from /api/chat to /api/generate if needed."""
        modes: list[str] = []
        if self._ollama_api_mode in {"chat", "generate"}:
            modes.append(self._ollama_api_mode)
        for fallback_mode in ("chat", "generate"):
            if fallback_mode not in modes:
                modes.append(fallback_mode)

        last_404_detail = ""
        for mode in modes:
            path = "/api/chat" if mode == "chat" else "/api/generate"
            payload = chat_payload if mode == "chat" else generate_payload
            try:
                response = await self.client.post(path, json=payload)
            except httpx.ReadTimeout as e:
                raise TimeoutError(
                    f"LLM request timed out (read timeout={self.config.timeout}s). "
                    "Ollama may still be loading the model or the prompt is large. "
                    "Increase timeout (e.g. OrchestratorConfig.llm_config.timeout or --llm-timeout 1800) or use a smaller model."
                ) from e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise LLMConnectionError("ollama", self.config.base_url, e) from e
            except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
                raise TimeoutError(
                    f"LLM request timed out ({type(e).__name__}). "
                    "The Ollama server may be overloaded."
                ) from e

            if response.status_code == 404:
                try:
                    last_404_detail = response.text.strip()
                    # Ollama returns 404 with {"error":"model 'X' not found"} when the model is missing
                    body = response.json()
                    err = body.get("error", "")
                    if err and ("not found" in str(err).lower() or "model" in str(err).lower()):
                        raise ValueError(
                            f"Ollama model {self.config.model!r} is not installed. "
                            f"Pull it with: ollama pull {self.config.model}"
                        ) from None
                except json.JSONDecodeError:
                    last_404_detail = ""
                except ValueError:
                    raise
                except Exception:
                    last_404_detail = ""
                logger.debug("Ollama endpoint %s returned 404; trying fallback endpoint if available.", path)
                continue

            if response.status_code == 400:
                try:
                    body = response.json()
                    err = safe_deep_get(body, ["error", "message"]) or body.get("error") or str(body)
                except Exception:
                    err = response.text or response.reason_phrase
                raise ValueError(
                    f"Ollama returned 400 Bad Request for {path}. "
                    f"Model '{self.config.model}' may not exist or the request may be invalid. "
                    f"Try: ollama pull {self.config.model} - Response: {err}"
                ) from None

            response.raise_for_status()
            self._ollama_api_mode = mode
            return mode, response

        detail = f" {last_404_detail}" if last_404_detail else ""
        raise ValueError(
            "Ollama API error (404): neither /api/chat nor /api/generate is available at "
            f"{self.config.base_url}.{detail}"
        )

    async def generate(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate a response using Ollama API."""
        num_predict = max_tokens or self.config.max_tokens

        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        chat_payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "num_predict": num_predict,
            },
        }
        generate_payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "num_predict": num_predict,
            },
        }
        if system_message:
            generate_payload["system"] = system_message
        if self.config.keep_alive is not None:
            chat_payload["keep_alive"] = self.config.keep_alive
            generate_payload["keep_alive"] = self.config.keep_alive

        logger.debug(f"Ollama request: model={self.config.model}")
        mode, response = await self._post_ollama_with_fallback(
            chat_payload=chat_payload,
            generate_payload=generate_payload,
        )

        try:
            result = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response from Ollama API: {e}") from e

        if result is None:
            raise ValueError("Ollama API returned null response")

        content = safe_deep_get(result, ["message", "content"], "") if mode == "chat" else result.get("response", "")
        usage = TokenUsage(
            prompt_tokens=result.get("prompt_eval_count", 0),
            completion_tokens=result.get("eval_count", 0),
        )

        logger.debug(f"Ollama response length: {len(content)} tokens={usage.total_tokens}")
        return content, usage

    async def is_model_loaded(self) -> bool:
        """Return True if the configured model is already loaded in Ollama (GET /api/ps)."""
        try:
            r = await self.client.get("/api/ps")
            r.raise_for_status()
            data = r.json()
            models = data.get("models") or []
            return any(
                m.get("name") == self.config.model or m.get("model") == self.config.model
                for m in models
            )
        except Exception as e:
            logger.debug(f"Could not check loaded models: {e}")
            return False

    async def preload_model(
        self,
        progress_callback: Optional[Callable[[str, float, Optional[float]], None]] = None,
    ) -> float:
        """
        Load the model into memory and keep it loaded (keep_alive=-1).
        If already loaded, returns 0. Otherwise runs a minimal chat and returns load time in seconds.
        progress_callback(message, elapsed_seconds, progress_pct) with progress_pct None for indeterminate.
        """
        def report(msg: str, elapsed: float, pct: Optional[float] = None) -> None:
            if progress_callback:
                progress_callback(msg, elapsed, pct)

        if await self.is_model_loaded():
            report("Model already loaded.", 0.0, 1.0)
            return 0.0

        report("Loading model...", 0.0, None)
        start = time.monotonic()
        elapsed_task: Optional[asyncio.Task] = None

        async def report_elapsed() -> None:
            while True:
                await asyncio.sleep(5.0)
                e = time.monotonic() - start
                report(f"Loading model... ({e:.0f}s)", e, None)

        try:
            elapsed_task = asyncio.create_task(report_elapsed())
            chat_payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "keep_alive": -1,
                "options": {"num_predict": 1},
            }
            generate_payload = {
                "model": self.config.model,
                "prompt": "Hi",
                "stream": False,
                "keep_alive": -1,
                "options": {"num_predict": 1},
            }
            try:
                _, response = await self._post_ollama_with_fallback(
                    chat_payload=chat_payload,
                    generate_payload=generate_payload,
                )
            except ValueError as exc:
                logger.warning(
                    "Ollama preload failed; skipping preload and leaving first request to load the model: %s",
                    exc,
                )
                return 0.0
            response.raise_for_status()
        finally:
            if elapsed_task is not None:
                elapsed_task.cancel()
                try:
                    await elapsed_task
                except asyncio.CancelledError:
                    pass
        total = time.monotonic() - start
        report("Model loaded.", total, 1.0)
        logger.info(f"Ollama model {self.config.model} loaded in {total:.1f}s (keep_alive=-1)")
        return total

    async def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tool_rounds: int = 10,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate with tool use via Ollama /api/chat.

        Falls back to plain ``generate()`` if the model does not support tool use
        (Ollama returns 400 for unsupported models).
        """
        effective_max_tokens = max_tokens or self.config.max_tokens
        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
        messages: list[dict] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        total_usage = TokenUsage()

        for round_num in range(max_tool_rounds + 1):
            chat_payload = {
                "model": self.config.model,
                "messages": messages,
                "stream": False,
                "tools": ollama_tools,
                "options": {
                    "temperature": temperature if temperature is not None else self.config.temperature,
                    "num_predict": effective_max_tokens,
                },
            }
            if self.config.keep_alive is not None:
                chat_payload["keep_alive"] = self.config.keep_alive

            try:
                response = await self.client.post("/api/chat", json=chat_payload)
            except httpx.ReadTimeout as e:
                raise TimeoutError(
                    f"LLM request timed out (read timeout={self.config.timeout}s)."
                ) from e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise LLMConnectionError("ollama", self.config.base_url, e) from e
            except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
                raise TimeoutError(
                    f"LLM request timed out ({type(e).__name__})."
                ) from e

            # Model doesn't support tool use — fall back to plain generate()
            if response.status_code in (400, 405, 422, 501) and round_num == 0:
                logger.info(
                    "Ollama model %r does not support tool use — falling back to plain generate()",
                    self.config.model,
                )
                # Use the larger of the dynamic budget or the configured max to
                # avoid truncation.  Pass max_tokens as a parameter instead of
                # using instance state (_generate_max_tokens_override) to be
                # safe under concurrent async calls.
                effective_max = max(max_tokens or 0, self.config.max_tokens)
                return await self.generate(
                    prompt, system_message=system_message,
                    temperature=temperature, max_tokens=effective_max,
                )

            # Handle error responses on subsequent rounds (round_num > 0) gracefully.
            # If the model fails mid-conversation (e.g. multi-round tool use not
            # supported), return the last successful content instead of crashing.
            if not response.is_success:
                if round_num > 0 and response.status_code in (400, 405, 422, 501):
                    logger.warning(
                        "Ollama tool-use round %d returned HTTP %d — "
                        "returning last successful content instead of failing",
                        round_num, response.status_code,
                    )
                    # Return whatever content we accumulated so far
                    last_content = ""
                    if messages:
                        for msg_entry in reversed(messages):
                            if msg_entry.get("role") == "assistant" and msg_entry.get("content"):
                                last_content = msg_entry["content"]
                                break
                    return last_content, total_usage
                msg = f"Ollama API error on tool-use round {round_num} (HTTP {response.status_code})"
                try:
                    body = response.json()
                    msg += f": {body.get('error', response.text[:200])}"
                except Exception:
                    msg += f": {response.text[:200]}"
                raise ValueError(msg)
            try:
                result = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(
                    f"Ollama returned malformed JSON (HTTP {response.status_code}): {e}"
                ) from e
            total_usage = total_usage + TokenUsage(
                prompt_tokens=result.get("prompt_eval_count", 0),
                completion_tokens=result.get("eval_count", 0),
            )

            message = result.get("message", {})
            tool_calls = message.get("tool_calls")

            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": message.get("content", ""),
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    if not fn_name:
                        logger.warning("Ollama tool call missing function name — skipping: %s", tc)
                        # Inject synthetic error result so the conversation stays consistent
                        messages.append({"role": "tool", "content": "error: tool call had no function name"})
                        continue
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    result_str = await tool_executor(fn_name, args)
                    logger.debug("Tool %s(%r) → %s", fn.get("name"), args, result_str[:120])
                    messages.append({"role": "tool", "content": result_str})
                continue

            final_content = message.get("content", "")
            if not final_content and not tool_calls:
                logger.warning("Ollama returned empty content with no tool calls on round %d", round_num)
            return final_content, total_usage

        raise ValueError(
            f"Ollama tool use loop exceeded {max_tool_rounds} rounds without completing"
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible LLM provider."""

    # Available OpenAI models
    MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-4",
        "gpt-3.5-turbo",
        "o1",
        "o1-mini",
        "o1-preview",
    ]

    def __init__(self, config: LLMConfig):
        if not config.api_key:
            raise ValueError(
                "OpenAI provider requires an API key. "
                "Set api_key in LLMConfig or use /apikey command in interactive mode."
            )
        self.config = config
        # Ensure the base URL ends with /v1 for OpenAI-compatible APIs.
        # Ollama's compat endpoint is /v1/chat/completions, not /chat/completions.
        raw_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        if not raw_url.endswith("/v1"):
            raw_url = raw_url + "/v1"
        self.client = httpx.AsyncClient(
            base_url=raw_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=httpx.Timeout(
                connect=30.0,
                read=config.timeout,
                write=30.0,
                pool=30.0,
            ),
        )

    async def generate(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate a response using OpenAI API."""
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        try:
            response = await self.client.post("/chat/completions", json=payload)
        except httpx.ReadTimeout as e:
            raise TimeoutError(
                f"LLM request timed out (read timeout={self.config.timeout}s). "
                "Increase OrchestratorConfig.llm_config.timeout or --llm-timeout."
            ) from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise LLMConnectionError("openai", self.config.base_url or "", e) from e
        except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
            raise TimeoutError(
                f"LLM request timed out ({type(e).__name__}). The API server may be overloaded."
            ) from e
        if not response.is_success:
            msg = _api_error_message(response)
            raise ValueError(
                f"OpenAI API error ({response.status_code}): {msg}. "
                "For OpenAI provider use a valid model (e.g. gpt-4o, gpt-4o-mini, gpt-4-turbo). "
                "If you switched from Ollama, change the model in Settings to an OpenAI model."
            ) from None

        try:
            result = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response from OpenAI API: {e}") from e

        choices = result.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            raise ValueError(f"OpenAI API returned empty or invalid choices: {result}")

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not content:
            raise ValueError(f"OpenAI API returned empty content: {result}")

        raw_usage = result.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
        )

        return content, usage

    async def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tool_rounds: int = 10,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate with tool use via OpenAI chat completions API."""
        effective_max_tokens = max_tokens or self.config.max_tokens
        # OpenAI uses {"type": "function", "function": {...}} format
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
        messages: list[dict] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        total_usage = TokenUsage()

        for round_num in range(max_tool_rounds + 1):
            payload = {
                "model": self.config.model,
                "messages": messages,
                "tools": openai_tools,
                "temperature": temperature if temperature is not None else self.config.temperature,
                "max_tokens": effective_max_tokens,
            }
            try:
                response = await self.client.post("/chat/completions", json=payload)
            except httpx.ReadTimeout as e:
                raise TimeoutError(f"LLM request timed out ({self.config.timeout}s).") from e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise LLMConnectionError("openai", self.config.base_url or "", e) from e
            except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
                raise TimeoutError(
                    f"LLM request timed out ({type(e).__name__})."
                ) from e
            if not response.is_success:
                raise ValueError(
                    f"OpenAI API error ({response.status_code}): {_api_error_message(response)}"
                )

            try:
                result = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(
                    f"OpenAI returned malformed JSON (HTTP {response.status_code}): {e}"
                ) from e
            raw_usage = result.get("usage", {})
            total_usage = total_usage + TokenUsage(
                prompt_tokens=raw_usage.get("prompt_tokens", 0),
                completion_tokens=raw_usage.get("completion_tokens", 0),
            )

            choices = result.get("choices") or []
            if not choices:
                raise ValueError(
                    f"OpenAI returned empty choices list: {str(result)[:200]}"
                )
            choice = choices[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason")

            if finish_reason == "tool_calls":
                messages.append(message)
                for tc in message.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name", "")
                    if not fn_name:
                        logger.warning("OpenAI tool call missing function name — skipping: %s", tc)
                        # Inject synthetic error result to keep tool_call_id matched
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": "error: tool call had no function name",
                        })
                        continue
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    result_str = await tool_executor(fn_name, args)
                    logger.debug("Tool %s(%r) → %s", fn_name, args, result_str[:120])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_str,
                    })
                continue

            final_content = message.get("content", "")
            if not final_content:
                logger.warning("OpenAI returned empty content (finish_reason=%s) on round %d", finish_reason, round_num)
            return final_content, total_usage

        raise ValueError(
            f"OpenAI tool use loop exceeded {max_tool_rounds} rounds without completing"
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM provider."""

    def __init__(self, config: LLMConfig):
        if not config.api_key:
            raise ValueError(
                "Anthropic provider requires an API key. "
                "Set api_key in LLMConfig or use /apikey command in interactive mode."
            )
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url or "https://api.anthropic.com",
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=30.0,
                read=config.timeout,
                write=30.0,
                pool=30.0,
            ),
        )

    async def generate(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate a response using Anthropic API."""
        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_message:
            payload["system"] = system_message

        try:
            response = await self.client.post("/v1/messages", json=payload)
        except httpx.ReadTimeout as e:
            raise TimeoutError(
                f"LLM request timed out (read timeout={self.config.timeout}s). "
                "Increase OrchestratorConfig.llm_config.timeout or --llm-timeout."
            ) from e
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise LLMConnectionError("anthropic", self.config.base_url or "", e) from e
        except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
            raise TimeoutError(
                f"LLM request timed out ({type(e).__name__}). The API server may be overloaded."
            ) from e
        if not response.is_success:
            msg = _api_error_message(response)
            raise ValueError(
                f"Anthropic API error ({response.status_code}): {msg}. "
                "Check model name (e.g. claude-3-5-sonnet-20241022) and API key."
            ) from None

        try:
            result = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response from Anthropic API: {e}") from e

        content = result.get("content")
        if not content or not isinstance(content, list) or len(content) == 0:
            raise ValueError(f"Anthropic API returned empty or invalid content: {result}")

        # Extract text from the first text-type content block; Anthropic may
        # return thinking blocks before the actual text when extended thinking
        # is enabled.
        text = ""
        for block in content:
            if block.get("type") == "text" and block.get("text"):
                text = block["text"]
                break
        if not text:
            # Fallback: try the first block regardless of type
            text = content[0].get("text", "")
        if not text:
            raise ValueError(f"Anthropic API returned empty text: {result}")

        raw_usage = result.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=raw_usage.get("input_tokens", 0),
            completion_tokens=raw_usage.get("output_tokens", 0),
        )

        return text, usage

    async def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tool_rounds: int = 10,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, TokenUsage]:
        """Generate with tool use via Anthropic Messages API."""
        effective_max_tokens = max_tokens or self.config.max_tokens
        messages: list[dict] = [{"role": "user", "content": prompt}]
        total_usage = TokenUsage()

        for _round in range(max_tool_rounds + 1):
            payload: dict = {
                "model": self.config.model,
                "max_tokens": effective_max_tokens,
                "temperature": temperature if temperature is not None else self.config.temperature,
                "messages": messages,
                "tools": tools,
            }
            if system_message:
                payload["system"] = system_message

            try:
                response = await self.client.post("/v1/messages", json=payload)
            except httpx.ReadTimeout as e:
                raise TimeoutError(
                    f"LLM request timed out (read timeout={self.config.timeout}s)."
                ) from e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise LLMConnectionError("anthropic", self.config.base_url or "", e) from e
            except (httpx.WriteTimeout, httpx.PoolTimeout) as e:
                raise TimeoutError(
                    f"LLM request timed out ({type(e).__name__})."
                ) from e
            if not response.is_success:
                msg = _api_error_message(response)
                raise ValueError(
                    f"Anthropic API error ({response.status_code}): {msg}. "
                    "Check model name and API key."
                ) from None

            try:
                result = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(
                    f"Anthropic returned malformed JSON (HTTP {response.status_code}): {e}"
                ) from e
            raw_usage = result.get("usage", {})
            total_usage = total_usage + TokenUsage(
                prompt_tokens=raw_usage.get("input_tokens", 0),
                completion_tokens=raw_usage.get("output_tokens", 0),
            )

            stop_reason = result.get("stop_reason")
            content: list[dict] = result.get("content") or []
            if not isinstance(content, list):
                content = []

            if stop_reason == "tool_use":
                tool_uses = [b for b in content if b.get("type") == "tool_use"]
                messages.append({"role": "assistant", "content": content})
                tool_results = []
                for tu in tool_uses:
                    tu_name = tu.get("name", "")
                    tu_id = tu.get("id", "")
                    if not tu_name:
                        logger.warning("Anthropic tool_use block missing name — skipping: %s", tu)
                        continue
                    result_str = await tool_executor(tu_name, tu.get("input", {}))
                    logger.debug("Tool %s(%r) → %s", tu_name, tu.get("input"), result_str[:120])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "content": result_str,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            if not text:
                logger.warning("Anthropic returned empty text (stop_reason=%s) on round %d", stop_reason, _round)
            return text, total_usage

        raise ValueError(
            f"Anthropic tool use loop exceeded {max_tool_rounds} rounds without end_turn"
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


def create_llm_provider(config: LLMConfig) -> LLMProvider:
    """Factory function to create an LLM provider."""
    providers = {
        "ollama": OllamaProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }

    provider_class = providers.get(config.provider.lower())
    if not provider_class:
        raise ValueError(f"Unknown LLM provider: {config.provider}")

    # Avoid sending invalid Ollama model (e.g. corrupted config with "/provider" or empty)
    if config.provider.lower() == "ollama":
        model = (config.model or "").strip()
        if not model or model.startswith("/"):
            logger.warning(
                "Invalid Ollama model %r; using default qwen2.5-coder:32b. Fix config or use --llm-model.",
                config.model,
            )
            config = replace(config, model="qwen2.5-coder:32b")

    return provider_class(config)


def extract_json_from_response(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown code blocks."""
    text = text.strip()

    # Try ```json ... ``` first
    json_match = re.search(r'```json\s*\n?([\s\S]*?)\n?```', text, re.IGNORECASE)
    if json_match:
        text = json_match.group(1).strip()
    else:
        # Try ``` ... ```
        code_match = re.search(r'```\s*\n?([\s\S]*?)\n?```', text)
        if code_match:
            text = code_match.group(1).strip()

    # Find outermost JSON object by tracking brace depth (skipping string literals)
    first_brace = -1
    depth = 0
    in_str = False
    brace_matched = False
    for i, ch in enumerate(text):
        if in_str:
            if ch == '"':
                # Count consecutive backslashes before this quote.
                # An even number means the quote is NOT escaped (\\\" → escaped-bs + real-quote).
                num_bs = 0
                j = i - 1
                while j >= 0 and text[j] == '\\':
                    num_bs += 1
                    j -= 1
                if num_bs % 2 == 0:
                    in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == '{':
            if depth == 0:
                first_brace = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and first_brace >= 0:
                text = text[first_brace:i + 1]
                brace_matched = True
                break

    # If braces never balanced (truncated JSON), at least strip the non-JSON
    # preamble so downstream repair heuristics operate on the JSON fragment.
    if not brace_matched and first_brace >= 0:
        text = text[first_brace:]

    # Clean up common issues
    # Remove trailing commas before ] or }
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # Remove single-line comments (not in strings)
    lines = []
    for line in text.split('\n'):
        # Simple heuristic: remove // comments not inside strings
        if '//' in line:
            # Check if // is inside a string
            in_string = False
            result = []
            i = 0
            while i < len(line):
                if line[i] == '"' and not in_string:
                    in_string = True
                    result.append(line[i])
                elif line[i] == '"' and in_string:
                    # Count consecutive backslashes before this quote
                    num_bs = 0
                    j = i - 1
                    while j >= 0 and line[j] == '\\':
                        num_bs += 1
                        j -= 1
                    if num_bs % 2 == 0:
                        in_string = False
                    result.append(line[i])
                elif line[i:i+2] == '//' and not in_string:
                    break
                else:
                    result.append(line[i])
                i += 1
            line = ''.join(result)
        lines.append(line)
    text = '\n'.join(lines)

    # Try parsing first BEFORE applying any regex repairs — valid JSON should
    # never be mutated by repair heuristics.
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error: {e}")

    # Apply repairs only on parse failure.
    # Fix unquoted keys (string-aware: skip content inside quoted strings)
    text = _fix_unquoted_keys(text)
    # Fix missing comma after ] or } followed by property name
    text = re.sub(r'(\}|\])(\s*\n\s*)"([a-zA-Z_])', r'\1,\2"\3', text)
    # Fix missing comma after ] or } followed by { or [
    text = re.sub(r'(\}|\])(\s*\n\s*)(\{|\[)', r'\1,\2\3', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e2:
        logger.warning(f"JSON parse after initial repair failed: {e2}")
        repaired = _repair_json(text, e2)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as e3:
            logger.error(f"JSON repair failed: {e3}")
            raise ValueError(f"Failed to parse JSON even after repair: {e3}")


def _fix_unquoted_keys(text: str) -> str:
    """Fix unquoted JSON keys while preserving string contents.

    Walks character-by-character to track whether we are inside a quoted
    string, only applying the key-quoting regex to segments outside strings.
    """
    segments: list[str] = []
    i = 0
    in_string = False
    seg_start = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            # Count consecutive backslashes before this quote
            num_bs = 0
            j = i - 1
            while j >= 0 and text[j] == '\\':
                num_bs += 1
                j -= 1
            is_escaped = num_bs % 2 == 1
        else:
            is_escaped = False
        if ch == '"' and not is_escaped:
            if in_string:
                # End of string — append the string segment verbatim
                segments.append(text[seg_start:i + 1])
                seg_start = i + 1
            else:
                # Start of string — fix keys in the segment before this string
                before = text[seg_start:i]
                before = re.sub(
                    r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
                    r'\1"\2":',
                    before,
                )
                segments.append(before)
                seg_start = i
            in_string = not in_string
        i += 1
    # Handle trailing segment
    tail = text[seg_start:]
    if not in_string:
        tail = re.sub(
            r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
            r'\1"\2":',
            tail,
        )
    segments.append(tail)
    return "".join(segments)


def _repair_json(text: str, error: json.JSONDecodeError) -> str:
    """
    Attempt to repair common JSON errors from LLM outputs.

    Common issues:
    - Missing commas between elements
    - Trailing commas before closing brackets
    - Unclosed strings
    - Unquoted keys
    - Invalid escape sequences
    """
    logger.info(f"Attempting JSON repair at position {error.pos}")

    # Strategy 1: Fix unquoted keys (string-aware)
    text = _fix_unquoted_keys(text)

    # Strategy 2: Remove trailing commas before ] or }
    text = re.sub(r',(\s*[\]}])', r'\1', text)

    # Strategy 3: Add missing commas between elements
    # Pattern: "value" followed by whitespace/newline then "key" or { or [
    text = re.sub(r'("|\d|true|false|null|\]|\})(\s*\n\s*)("|\{|\[)', r'\1,\2\3', text)

    # Strategy 4: Fix missing comma after } or ] followed by " (property name) or { or [
    # This handles cases like: [{"name": "public"}] "containers" -> needs comma
    text = re.sub(r'(\}|\])(\s+)"([a-zA-Z_])', r'\1,\2"\3', text)
    text = re.sub(r'(\}|\])(\s+)(\{|\[)', r'\1,\2\3', text)

    # Strategy 5: Try to find and fix the specific error location
    if "Expecting ',' delimiter" in str(error):
        # Insert comma at error position
        pos = error.pos
        if pos > 0 and pos < len(text):
            # Look backward to find a good insertion point
            insert_pos = pos
            while insert_pos > 0 and text[insert_pos - 1] in ' \t\n':
                insert_pos -= 1
            if insert_pos > 0:
                text = text[:insert_pos] + ',' + text[insert_pos:]
                logger.info(f"Inserted missing comma at position {insert_pos}")

    # Strategy 6: Fix unclosed strings by finding unbalanced quotes
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError as e2:
        if "Unterminated string" in str(e2) or "Invalid control character" in str(e2):
            # Try to close the string at the error position
            pos = e2.pos
            if pos < len(text):
                # Find the start of the unclosed string
                start = text.rfind('"', 0, pos)
                if start != -1:
                    # Insert closing quote before problematic character
                    text = text[:pos] + '"' + text[pos:]
                    logger.info(f"Inserted closing quote at position {pos}")

    # Strategy 7: Truncate at last valid point if JSON is incomplete
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        # Try to find the last complete object/array
        bracket_count = 0
        brace_count = 0
        last_valid = 0
        in_string = False

        for i, c in enumerate(text):
            if c == '"':
                # Count consecutive backslashes to determine if quote is escaped
                num_bs = 0
                j = i - 1
                while j >= 0 and text[j] == '\\':
                    num_bs += 1
                    j -= 1
                if num_bs % 2 == 0:
                    in_string = not in_string
            elif not in_string:
                if c == '{':
                    brace_count += 1
                elif c == '}':
                    brace_count -= 1
                    if brace_count == 0 and bracket_count == 0:
                        last_valid = i + 1
                elif c == '[':
                    bracket_count += 1
                elif c == ']':
                    bracket_count -= 1
                    if brace_count == 0 and bracket_count == 0:
                        last_valid = i + 1

        if last_valid > 0 and last_valid < len(text):
            truncated = text[:last_valid]
            try:
                json.loads(truncated)
                logger.info(f"Truncated JSON at position {last_valid}")
                return truncated
            except json.JSONDecodeError:
                pass

    return text


def extract_terraform_from_response(text: str) -> str:
    """Extract Terraform HCL from LLM response."""
    # Try to find HCL in code blocks
    hcl_match = re.search(r'```(?:hcl|terraform)\s*\n([\s\S]*?)\n```', text, re.IGNORECASE)
    if hcl_match:
        return hcl_match.group(1).strip()

    # Try generic code block
    code_match = re.search(r'```\s*\n([\s\S]*?)\n```', text)
    if code_match:
        return code_match.group(1).strip()

    # If no code blocks, check if it looks like terraform
    if 'terraform {' in text or 'provider "docker"' in text or 'resource "docker_' in text:
        return text.strip()

    return text.strip()
