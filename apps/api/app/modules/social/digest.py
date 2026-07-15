import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AuthorDigest, SocialPost
from app.modules.social.activity import assess_activity
from app.modules.social.repository import SocialRepository
from app.modules.social.schemas import ActivityDigestVerdict

DIGEST_SYSTEM_PROMPT = """You summarize recent public posts by a tracked comic creator.

Use only the supplied JSON. Write concise Simplified Chinese, preserve Japanese titles and event
codes, and distinguish fact, author plan, and inference. Pure reposts are absent. A quoted post is
not automatically the tracked creator's own work. Every highlight must cite one or more supplied
integer post_ids. Never invent an ID, title, date, event, or causal relationship. Personal chatter
is low priority unless it explains an absence or schedule change. Return JSON only.
"""


class ActivityDigestReviewer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def review(self, evidence: dict[str, Any]) -> ActivityDigestVerdict:
        schema = ActivityDigestVerdict.model_json_schema()
        response_format: dict[str, Any]
        if self.settings.agent_provider == "deepseek":
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "author_activity_digest",
                    "strict": True,
                    "schema": schema,
                },
            }
        payload: dict[str, Any] = {
            "model": self.settings.agent_model,
            "temperature": 0,
            "max_tokens": 2200,
            "messages": [
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Return every required schema field.\n"
                        f"Schema: {json.dumps(schema, ensure_ascii=False)}\n"
                        "<untrusted_posts>\n"
                        f"{json.dumps(evidence, ensure_ascii=False)}\n"
                        "</untrusted_posts>"
                    ),
                },
            ],
            "response_format": response_format,
        }
        if self.settings.agent_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json"}
        if self.settings.agent_api_key:
            headers["Authorization"] = f"Bearer {self.settings.agent_api_key}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.settings.agent_timeout_seconds, follow_redirects=True
        )
        try:
            response = await client.post(
                f"{self.settings.agent_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            return ActivityDigestVerdict.model_validate(json.loads(content))
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise RuntimeError(f"动态摘要 Agent 返回无效 JSON：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"动态摘要 Agent 返回 HTTP {exc.response.status_code}："
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接动态摘要 Agent：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()


class DigestService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = SocialRepository(session)

    async def refresh(self, author_id: int, days: int = 7) -> AuthorDigest | None:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = today + timedelta(days=1)
        period_start = period_end - timedelta(days=days)
        posts = self.repository.recent_posts_for_author(author_id, period_start)
        if not posts:
            return None
        author = self.repository.author(author_id)
        evidence = {
            "author": author.name if author else "",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "posts": [self._post_snapshot(post) for post in posts],
        }
        content_hash = sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        digest = self.repository.digest_for_period(author_id, "rolling_7d", period_end)
        if digest and digest.content_hash == content_hash:
            return digest

        generated_by = "rules"
        model = None
        error = None
        verdict = self._fallback(posts)
        if self.settings.social_agent_configured:
            try:
                verdict = await ActivityDigestReviewer(self.settings).review(evidence)
                verdict = self._ground(verdict, {post.id for post in posts})
                generated_by = "agent"
                model = self.settings.agent_model
            except Exception as exc:
                error = str(exc)[:2000]

        evidence_ids = sorted(
            {post_id for item in verdict.highlights for post_id in item.post_ids}
        )
        values = {
            "summary": verdict.summary,
            "highlights": [item.model_dump(mode="json") for item in verdict.highlights],
            "uncertainties": verdict.uncertainties,
            "evidence_post_ids": evidence_ids,
            "content_hash": content_hash,
            "generated_by": generated_by,
            "model": model,
            "prompt_version": self.settings.social_agent_prompt_version,
            "error": error,
        }
        if digest is None:
            digest = AuthorDigest(
                author_id=author_id,
                period_type="rolling_7d",
                period_start=period_start,
                period_end=period_end,
                **values,
            )
            self.session.add(digest)
        else:
            digest.period_start = period_start
            for key, value in values.items():
                setattr(digest, key, value)
        self.session.flush()
        return digest

    @staticmethod
    def _post_snapshot(post: SocialPost) -> dict[str, Any]:
        assessment = assess_activity(post)
        return {
            "post_id": post.id,
            "platform_post_id": post.platform_post_id,
            "type": post.post_type,
            "text": post.text,
            "ocr_text": post.ocr_text or "",
            "posted_at": post.posted_at.isoformat(),
            "links": post.links,
            "media_count": len(post.media),
            "quoted_text": post.raw_metadata.get("quoted_text"),
            "rule_category": assessment.category if assessment else "ignored",
        }

    @staticmethod
    def _ground(
        verdict: ActivityDigestVerdict, allowed_ids: set[int]
    ) -> ActivityDigestVerdict:
        highlights = []
        for item in verdict.highlights:
            item.post_ids = list(dict.fromkeys(i for i in item.post_ids if i in allowed_ids))
            if item.post_ids:
                highlights.append(item)
        verdict.highlights = highlights
        return verdict

    @staticmethod
    def _fallback(posts: list[SocialPost]) -> ActivityDigestVerdict:
        ranked = []
        rank = {"critical": 3, "high": 2, "normal": 1, "low": 0}
        for post in posts:
            assessment = assess_activity(post)
            if assessment:
                ranked.append((rank[assessment.importance], post.posted_at, post, assessment))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        highlights = [
            {
                "text": assessment.headline,
                "category": assessment.category,
                "importance": assessment.importance,
                "factuality": "fact",
                "post_ids": [post.id],
            }
            for _, _, post, assessment in ranked[:6]
        ]
        summary = "；".join(item["text"] for item in highlights[:3]) or "最近有公开动态"
        return ActivityDigestVerdict.model_validate(
            {"summary": summary, "highlights": highlights, "uncertainties": []}
        )
