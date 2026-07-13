import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.agent_review.errors import (
    AgentNotConfiguredError,
    AgentOutputError,
    AgentTransportError,
)
from app.modules.agent_review.prompts import SYSTEM_PROMPT, user_prompt
from app.modules.agent_review.schemas import AgentVerdict, CandidateEvidence


@dataclass(frozen=True)
class ReviewResponse:
    verdict: AgentVerdict
    raw_output: dict[str, Any]


class AggregationReviewer(Protocol):
    async def review(self, evidence: CandidateEvidence) -> ReviewResponse: ...


class OpenAICompatibleReviewer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.agent_configured:
            raise AgentNotConfiguredError("Agent 未启用或尚未配置模型")
        self.settings = settings
        self._client = client

    async def review(self, evidence: CandidateEvidence) -> ReviewResponse:
        verdict_schema = AgentVerdict.model_json_schema()
        response_format: dict[str, Any]
        if self.settings.agent_provider == "deepseek":
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "manga_aggregation_verdict",
                    "strict": True,
                    "schema": verdict_schema,
                },
            }
        payload = {
            "model": self.settings.agent_model,
            "temperature": self.settings.agent_temperature,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_prompt(
                        evidence.model_dump_json(),
                        json.dumps(verdict_schema, ensure_ascii=False),
                    ),
                },
            ],
            "response_format": response_format,
        }
        if self.settings.agent_provider == "deepseek":
            # V4 defaults to thinking mode; classification should be fast and deterministic.
            payload["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json"}
        if self.settings.agent_api_key:
            headers["Authorization"] = f"Bearer {self.settings.agent_api_key}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.agent_timeout_seconds),
            follow_redirects=True,
        )
        try:
            response = await client.post(
                f"{self.settings.agent_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if not isinstance(content, str):
                raise AgentOutputError("模型响应缺少文本内容")
            decoded = json.loads(content)
            verdict = AgentVerdict.model_validate(decoded)
            return ReviewResponse(verdict=verdict, raw_output=decoded)
        except AgentOutputError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise AgentOutputError(f"模型没有返回符合 Schema 的 JSON：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise AgentTransportError(
                f"模型接口返回 HTTP {exc.response.status_code}：{detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentTransportError(f"无法连接模型接口：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()


def build_reviewer(settings: Settings) -> AggregationReviewer:
    if settings.agent_provider not in {"openai_compatible", "deepseek"}:
        raise AgentNotConfiguredError(
            f"暂不支持 Agent provider：{settings.agent_provider}"
        )
    return OpenAICompatibleReviewer(settings)
