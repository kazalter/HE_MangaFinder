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

Use only the supplied JSON. All prose in summary, highlights.text, and uncertainties MUST be
natural Simplified Chinese. Translate ordinary Japanese words and phrases instead of mixing them
into Chinese. Use translation_hints when present. Preserve creator names, circle names, work
titles, store names, booth numbers, and event codes exactly; on first mention, explain an
unfamiliar Japanese event nickname as `中文解释（原文）`. Do not translate inside an exact work
title. If a proper noun is ambiguous, keep its original spelling and state the uncertainty rather
than guessing.

Distinguish fact, author plan, and inference. Pure reposts are absent. For a quote post, the
quoted_text belongs to another account: describe it as the creator quoting/commenting on that
content, never as the creator's own work and never call it a repost. Give low-value quotes and
chatter low priority. Every highlight must cite one or more supplied integer post_ids. Never
invent an ID, title, date, event, translation, or causal relationship. Return JSON only.
"""

TRANSLATION_HINTS: tuple[tuple[str, str], ...] = (
    ("トレカケースアクキー", "卡套亚克力挂件"),
    ("トレカケース", "卡套"),
    ("アクリルキーホルダー", "亚克力挂件"),
    ("アクキー", "亚克力挂件"),
    ("アクリルスタンド", "亚克力立牌"),
    ("アクスタ", "亚克力立牌"),
    ("委託申請", "委托销售申请"),
    ("予約開始", "开放预订"),
    ("通販", "网售"),
    ("お品書き", "商品目录"),
    ("夏コミ", "夏季 Comic Market（夏コミ）"),
    ("冬コミ", "冬季 Comic Market（冬コミ）"),
    ("コミティア", "COMITIA（コミティア）"),
    ("サンプル", "样品"),
    ("スペース", "展位"),
    ("頒布", "现场销售"),
    ("表紙", "封面"),
    ("原稿", "创作稿件"),
    ("入稿", "交稿付印"),
    ("脱稿", "完成稿件"),
    ("新刊", "新作同人志"),
)
DIGEST_LOCALIZATION_VERSION = "zh-cn-2026-07-15.2"


def translation_hints(posts: list[SocialPost]) -> list[dict[str, str]]:
    corpus = "\n".join(
        part
        for post in posts
        for part in (
            post.text,
            post.ocr_text or "",
            str(post.raw_metadata.get("quoted_text") or ""),
        )
        if part
    )
    return [
        {"source": source, "preferred_zh": translated}
        for source, translated in TRANSLATION_HINTS
        if source in corpus
    ]


def localize_known_terms(text: str) -> str:
    localized = text
    for source, translated in TRANSLATION_HINTS:
        if source in localized and translated not in localized:
            localized = localized.replace(source, translated)
    # Pure reposts never reach a digest. Any such wording therefore refers to a quote post and
    # must retain the distinction between the creator's commentary and the quoted account.
    localized = localized.replace("转发", "引用").replace("转推", "引用")
    return localized


def localize_verdict(verdict: ActivityDigestVerdict) -> ActivityDigestVerdict:
    verdict.summary = localize_known_terms(verdict.summary)
    for item in verdict.highlights:
        item.text = localize_known_terms(item.text)
    verdict.uncertainties = [localize_known_terms(item) for item in verdict.uncertainties]
    return verdict


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
            "translation_hints": translation_hints(posts),
        }
        content_hash = sha256(
            json.dumps(
                {
                    "evidence": evidence,
                    "prompt_version": self.settings.social_agent_prompt_version,
                    "localization_version": DIGEST_LOCALIZATION_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
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

        verdict = localize_verdict(verdict)
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
                "text": localize_known_terms(assessment.headline),
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
