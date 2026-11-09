"""Minimal official DeepSeek transport. Never logs credentials or error bodies."""
import json
import os
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelUnavailable(RuntimeError):
    def __init__(self, message, *, category="unavailable", http_status=None):
        super().__init__(message)
        self.category = category
        self.http_status = http_status


def safe_failure(exc):
    """Never serialize exception text/URLs: providers and proxies may echo credentials."""
    allowed = {"unavailable", "provider_http", "dns", "tls", "timeout", "network",
               "invalid_response", "configuration", "budget", "mcp_startup", "mcp_transport",
               "mcp_shutdown", "tool_unavailable", "contract_violation"}
    category = getattr(exc, "category", "unavailable")
    row = {"category": category if isinstance(category, str) and category in allowed else "unavailable"}
    status = getattr(exc, "http_status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        row["http_status"] = status
    return row


class DeepSeek:
    def __init__(self, api_key=None, model=None):
        self._key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model or os.environ.get("LOGIMATE_MODEL", "deepseek-flash")
        if not self._key:
            raise ModelUnavailable("DEEPSEEK_API_KEY is not configured", category="configuration")

    def complete(self, messages, *, tools=None, json_output=False, timeout=20):
        body = {"model": self.model, "messages": messages, "stream": False,
                "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_output:
            body["response_format"] = {"type": "json_object"}
        request = Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        start = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise ModelUnavailable(f"DeepSeek HTTP {exc.code}", category="provider_http", http_status=exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            category = ("tls" if isinstance(reason, ssl.SSLError) else "dns" if isinstance(reason, socket.gaierror)
                        else "timeout" if isinstance(reason, TimeoutError) else "network")
            raise ModelUnavailable("DeepSeek network or timeout failure", category=category) from None
        except (ValueError, UnicodeError):
            raise ModelUnavailable("Malformed DeepSeek response", category="invalid_response") from None
        try:
            choice = data["choices"][0]
            raw = choice["message"]
            message = {"role": "assistant", "content": raw.get("content")}
            if raw.get("tool_calls"):
                message["tool_calls"] = raw["tool_calls"]
            # Hidden reasoning, headers and full provider response are never persisted.
            meta = {"provider": "deepseek", "model": data.get("model", self.model), "response_id": data.get("id"),
                    "usage": data.get("usage", {}), "finish_reason": choice.get("finish_reason"),
                    "elapsed_ms": round((time.perf_counter()-start)*1000, 3)}
            return message, meta
        except (KeyError, IndexError, TypeError):
            raise ModelUnavailable("Malformed DeepSeek response", category="invalid_response") from None
