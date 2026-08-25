"""OpenAI-compatible chat-completions client used by the dwell backend."""

from __future__ import annotations

import json
import ipaddress
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _urlopen(req: urllib.request.Request, timeout: float):
    """Keep loopback model gateways out of ambient HTTP proxy settings."""
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    direct = host.lower() == "localhost"
    if not direct:
        try:
            direct = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            req, timeout=timeout
        )
    return urllib.request.urlopen(req, timeout=timeout)


@dataclass
class ProviderConfig:
    base: str
    token: str
    model: str
    effort: str = "high"

    @property
    def endpoint(self) -> str:
        base = self.base.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"


@dataclass
class EmbeddingConfig:
    base: str
    token: str
    model: str

    @property
    def endpoint(self) -> str:
        base = self.base.strip().rstrip("/")
        if base.endswith("/embeddings"):
            return base
        if base.endswith("/v1"):
            return base + "/embeddings"
        return base + "/v1/embeddings"


class EmbeddingProvider:
    """Small OpenAI-compatible embeddings client with batch support."""

    def __init__(self, config: EmbeddingConfig, timeout: float = 60.0):
        self.config = config
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.config.base or not self.config.token or not self.config.model:
            raise ProviderError("embedding API 还没配置完整")
        payload = {"model": self.config.model, "input": [str(text) for text in texts]}
        req = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.token,
                "User-Agent": "dwell-history-search/1.0",
            },
            method="POST",
        )
        try:
            with _urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", "replace")
            raise ProviderError(f"embedding 接口返回 HTTP {exc.code}", exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"embedding 接口连不上：{exc.reason}") from exc
        try:
            data = json.loads(raw)
            ordered = sorted(data["data"], key=lambda item: int(item.get("index") or 0))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "embedding 接口返回了无法识别的内容",
                detail=raw[:8192].decode("utf-8", "replace"),
            ) from exc
        if len(vectors) != len(texts) or not vectors or not vectors[0]:
            raise ProviderError("embedding 接口返回的向量数量或维度不对")
        dims = len(vectors[0])
        if any(len(vector) != dims for vector in vectors):
            raise ProviderError("embedding 接口返回了不同维度的向量")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ProviderError("embedding 接口返回了非有限数值")
        return vectors


class OpenAIProvider:
    def __init__(self, config: ProviderConfig, timeout: float = 180.0):
        self.config = config
        self.timeout = timeout

    def _request(self, payload: dict[str, Any]):
        if not self.config.base or not self.config.token or not self.config.model:
            raise ProviderError("API 还没配置完整")
        body = dict(payload)
        body["model"] = self.config.model
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.config.endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.token,
                "User-Agent": "dwell-backend/1.0",
            },
            method="POST",
        )
        try:
            return _urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", "replace")
            raise ProviderError(f"模型接口返回 HTTP {exc.code}", exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"模型接口连不上：{exc.reason}") from exc

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        with self._request(payload) as response:
            raw = response.read()
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型接口返回了无法识别的内容", detail=raw[:8192].decode("utf-8", "replace")) from exc

    def stream(self, messages: list[dict[str, Any]]) -> Iterator[dict[str, str]]:
        payload: dict[str, Any] = {"messages": messages, "stream": True}
        with self._request(payload) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                raw = response.read()
                try:
                    msg = json.loads(raw)["choices"][0]["message"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise ProviderError("模型接口没有返回可识别的流", detail=raw[:8192].decode("utf-8", "replace")) from exc
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                content = msg.get("content") or ""
                if reasoning:
                    yield {"reasoning": str(reasoning)}
                if content:
                    yield {"content": str(content)}
                return

            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta") or {}
                except (ValueError, IndexError, TypeError):
                    continue
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                content = delta.get("content")
                if reasoning:
                    yield {"reasoning": str(reasoning)}
                if content:
                    yield {"content": str(content)}
