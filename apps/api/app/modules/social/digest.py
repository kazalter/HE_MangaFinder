import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import as_utc, utc_timestamp
from app.db.models import AuthorDigest, SocialPost
from app.modules.social.activity import assess_activity
from app.modules.social.repository import SocialRepository
from app.modules.social.schemas import ActivityDigestVerdict

DIGEST_SYSTEM_PROMPT = """You summarize recent public posts by a tracked comic creator into a
concise Simplified Chinese briefing.

Use only the supplied JSON. All prose in summary, highlights.text, and uncertainties MUST be
natural, fluent Simplified Chinese (简体中文) suitable for a daily QQ briefing. Translate ordinary
Japanese sentences, grammar, particles, hashtags, and phrases completely into Chinese—never copy raw
Japanese sentences or mix Japanese particles/verb endings (の/は/を/が/で/に/ます/ました/です)
into Chinese prose. Use translation_hints when present.

Proper nouns & annotations:
- Preserve creator names, circle names, exact work titles (wrap in 『...』 when clear), store names,
  booth numbers, and event codes (e.g. C108) exactly. Do not translate inside an exact work title.
- On first mention, explain an unfamiliar Japanese event nickname as `中文解释（原文）`.
- If a proper noun is ambiguous, keep its original spelling and state the uncertainty rather than
  guessing.

Content & grounding rules:
- Distinguish fact, author plan, and inference. Pure reposts are absent.
- For a quote post, the quoted_text belongs to another account: describe it as the creator
  quoting/commenting on that content, never as the creator's own work and never call it a repost.
- Prioritize recent releases, event participation, sales/preorders, schedule notices, and concrete
  creation progress; do not omit the latest posts in the period. Give chatter low priority.
- If digest_scope is "daily_brief", summarize all meaningful updates from the supplied period
  concisely within max_highlights items.
- Every highlight must cite one or more supplied integer post_ids. Never invent an ID, title, date,
  event, translation, or causal relationship.
- Return valid JSON only with keys: summary, highlights (list of {text, category, importance,
  factuality, post_ids}), uncertainties.
"""

REWRITE_CHINESE_SYSTEM_PROMPT = """You polish a comic creator digest JSON so that all prose is
natural Simplified Chinese.

Rewrite any remaining untranslated Japanese prose, particles, or phrases in summary,
highlights[].text, and uncertainties into fluent Simplified Chinese using the supplied source posts
for accuracy. Keep all integer post_ids, category, importance, factuality, exact work titles,
creator/circle names, store/booth/event codes, and brief `中文（原文）` annotations intact.
Return valid JSON only matching the same schema.
"""

TRANSLATION_HINTS: tuple[tuple[str, str], ...] = (
    ("トレカケースアクキー", "卡套亚克力挂件"),
    ("トレカケース", "卡套"),
    ("アクリルキーホルダー", "亚克力挂件"),
    ("アクキー", "亚克力挂件"),
    ("アクリルスタンド", "亚克力立牌"),
    ("アクスタ", "亚克力立牌"),
    ("タペストリー", "挂画"),
    ("委託申請", "委托销售申请"),
    ("予約開始", "开放预订"),
    ("販売開始", "开始发售"),
    ("一般販売", "一般发售"),
    ("配信開始", "开始配信/上线"),
    ("試し読み", "试阅"),
    ("本文見本", "正文样张"),
    ("描き下ろし", "全新绘制"),
    ("メロンブックス", "Melonbooks（蜜瓜书店）"),
    ("とらのあな", "虎之穴（とらのあな）"),
    ("ファンボックス", "FANBOX"),
    ("通販", "网售"),
    ("予約", "预订"),
    ("販売", "发售"),
    ("お品書き", "商品目录"),
    ("夏コミ", "夏季 Comic Market（夏コミ）"),
    ("冬コミ", "冬季 Comic Market（冬コミ）"),
    ("コミティア", "COMITIA（コミティア）"),
    ("コミケ", "Comic Market（コミケ）"),
    ("サンプル", "样品"),
    ("スペース", "展位"),
    ("頒布", "现场销售"),
    ("表紙", "封面"),
    ("原稿", "创作稿件"),
    ("入稿", "交稿付印"),
    ("脱稿", "完成稿件"),
    ("新刊", "新作同人志"),
    ("既刊", "旧刊"),
    ("再販", "再贩"),
    ("再録", "再录"),
    ("重版", "加印"),
    ("完売", "售罄"),
    ("在庫", "库存"),
    ("進捗", "创作进度"),
    ("作業中", "制作中"),
    ("制作中", "制作中"),
    ("線画", "线稿"),
    ("彩色", "上色"),
    ("イラスト", "插画"),
    ("落書き", "涂鸦"),
    ("らくがき", "涂鸦"),
    ("色紙", "签名板/色纸"),
    ("グッズ", "周边商品"),
    ("電子版", "电子版"),
    ("コラボ", "联动合作"),
    ("寄稿", "供稿"),
    ("お知らせ", "通知公告"),
    ("欠席", "缺席"),
    ("中止", "取消"),
)
DIGEST_LOCALIZATION_VERSION = "zh-cn-2026-11-25.1"

CATEGORY_LABELS_ZH: dict[str, str] = {
    "schedule_notice": "日程变动",
    "release": "新刊/新作",
    "event": "参展动态",
    "sales": "通贩/销售",
    "creation_progress": "创作进度",
    "collaboration": "合作/供稿",
    "artwork": "插画/涂鸦",
    "personal": "日常动态",
    "other": "公开动态",
}

_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
_HAN_RE = re.compile(r"[\u4E00-\u9FFF]")
_ALLOWED_JP_SPAN_RE = re.compile(
    r"『[^』]*』|「[^」]*」|《[^》]*》|【[^】]*】|（[^）]*）|\([^)]*\)|https?://\S+|@[A-Za-z0-9_]+"
)
_JP_GRAMMAR_RE = re.compile(
    r"(ました|でした|ています|ており|ます。|です。|ので|だけど|ですよ|ますね)"
)
_URL_RE = re.compile(r"https?://\S+")


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


def needs_chinese_rewrite(text: str, *, ignore_terms: tuple[str, ...] = ()) -> bool:
    """Detect untranslated Japanese prose outside work titles and parenthetical glosses."""
    stripped = text
    for term in ignore_terms:
        if term:
            stripped = stripped.replace(term, "")
    stripped = _ALLOWED_JP_SPAN_RE.sub("", stripped).strip()
    if not stripped:
        return False
    if _JP_GRAMMAR_RE.search(stripped):
        return True
    kana_count = len(_KANA_RE.findall(stripped))
    if kana_count < 3:
        return False
    han_count = len(_HAN_RE.findall(stripped))
    return kana_count >= 6 or (kana_count / max(1, kana_count + han_count)) >= 0.25


def verdict_needs_chinese_rewrite(
    verdict: ActivityDigestVerdict, *, author_name: str = ""
) -> bool:
    ignore = (author_name,) if author_name else ()
    if needs_chinese_rewrite(verdict.summary, ignore_terms=ignore):
        return True
    if any(needs_chinese_rewrite(item.text, ignore_terms=ignore) for item in verdict.highlights):
        return True
    return any(needs_chinese_rewrite(item, ignore_terms=ignore) for item in verdict.uncertainties)


def format_fallback_highlight(
    category: str,
    raw_text: str,
    *,
    has_media: bool = False,
    max_excerpt: int = 72,
) -> str:
    """Format a rule-based fallback line with a Chinese category label and a short excerpt."""
    label = CATEGORY_LABELS_ZH.get(category, "公开动态")
    cleaned = _URL_RE.sub("", raw_text)
    compact = " ".join(cleaned.split())
    if not compact or compact in {"媒体动态", "无文字动态"}:
        return f"【{label}】发布了图片/媒体动态" if has_media else f"【{label}】发布了新动态"
    localized = localize_known_terms(compact)
    excerpt = localized[:max_excerpt].rstrip()
    if len(localized) > max_excerpt:
        excerpt = f"{excerpt}…"
    return f"【{label}】{excerpt}"


def _strip_json_fences(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


class ActivityDigestReviewer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def _call_model(
        self, messages: list[dict[str, str]], *, schema: dict[str, Any]
    ) -> ActivityDigestVerdict:
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
            "messages": messages,
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
            return ActivityDigestVerdict.model_validate(json.loads(_strip_json_fences(content)))
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise RuntimeError(f"动态摘要 Agent 返回无效 JSON：{exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"动态摘要 Agent 返回 HTTP {exc.response.status_code}：{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接动态摘要 Agent：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def _rewrite_to_chinese(
        self,
        verdict: ActivityDigestVerdict,
        evidence: dict[str, Any],
        *,
        schema: dict[str, Any],
    ) -> ActivityDigestVerdict:
        messages = [
            {"role": "system", "content": REWRITE_CHINESE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Rewrite all remaining Japanese prose into Simplified Chinese.\n"
                    f"Schema: {json.dumps(schema, ensure_ascii=False)}\n"
                    f"<draft_verdict>\n{verdict.model_dump_json()}\n</draft_verdict>\n"
                    "<untrusted_posts>\n"
                    f"{json.dumps(evidence, ensure_ascii=False)}\n"
                    "</untrusted_posts>"
                ),
            },
        ]
        return await self._call_model(messages, schema=schema)

    async def review(self, evidence: dict[str, Any]) -> ActivityDigestVerdict:
        schema = ActivityDigestVerdict.model_json_schema()
        messages = [
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
        ]
        verdict = await self._call_model(messages, schema=schema)
        author_name = str(evidence.get("author") or "")
        if verdict_needs_chinese_rewrite(verdict, author_name=author_name):
            try:
                rewritten = await self._rewrite_to_chinese(verdict, evidence, schema=schema)
                if rewritten.highlights:
                    return rewritten
            except Exception:
                # Keep the grounded first-pass verdict if the Chinese polish pass fails.
                pass
        return verdict


class DigestService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = SocialRepository(session)

    async def refresh(
        self,
        author_id: int,
        days: int = 7,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> AuthorDigest | None:
        today = as_utc(now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = today + timedelta(days=1)
        period_start = period_end - timedelta(days=days)
        posts = self.repository.recent_posts_for_author(author_id, period_start, period_end)
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
                    "model": self.settings.agent_model
                    if self.settings.social_agent_configured
                    else None,
                    "provider": self.settings.agent_provider
                    if self.settings.social_agent_configured
                    else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        digest = self.repository.digest_for_period(author_id, "rolling_7d", period_end)
        if digest and digest.content_hash == content_hash and not force:
            needs_model_retry = (
                self.settings.social_agent_configured and digest.generated_by != "agent"
            )
            retry_due = (
                digest.error is None
                or not digest.updated_at
                or as_utc(now or datetime.now(UTC)) - as_utc(digest.updated_at)
                >= timedelta(minutes=15)
            )
            if not needs_model_retry or not retry_due:
                return digest

        generated_by = "rules"
        model = None
        error = None
        verdict = self._fallback(posts)
        if self.settings.social_agent_configured:
            try:
                reviewed = await ActivityDigestReviewer(self.settings).review(evidence)
                reviewed = self._ground(reviewed, {post.id for post in posts})
                if not reviewed.highlights:
                    raise ValueError("模型摘要没有可核对的原帖证据")
                verdict = reviewed
                generated_by = "agent"
                model = self.settings.agent_model
            except Exception as exc:
                error = str(exc)[:2000]

        verdict = localize_verdict(verdict)
        evidence_ids = sorted({post_id for item in verdict.highlights for post_id in item.post_ids})
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
            # Advance the retry clock even if consecutive failures have identical text.
            "updated_at": as_utc(now or datetime.now(UTC)),
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

    async def summarize_period(
        self,
        author_id: int,
        period_start: datetime,
        period_end: datetime,
        *,
        max_items: int = 3,
        min_importance: str = "normal",
    ) -> ActivityDigestVerdict | None:
        """Generate a period-scoped LLM digest when rolling_7d misses daily items."""
        if not self.settings.social_agent_configured:
            return None
        start_utc = as_utc(period_start)
        end_utc = as_utc(period_end)
        posts = [
            post
            for post in self.repository.recent_posts_for_author(author_id, start_utc, end_utc)
            if as_utc(post.posted_at) > start_utc
        ]
        if not posts:
            return None
        author = self.repository.author(author_id)
        evidence = {
            "author": author.name if author else "",
            "digest_scope": "daily_brief",
            "max_highlights": max_items,
            "min_importance": min_importance,
            "period_start": start_utc.isoformat(),
            "period_end": end_utc.isoformat(),
            "posts": [self._post_snapshot(post) for post in posts],
            "translation_hints": translation_hints(posts),
        }
        try:
            reviewed = await ActivityDigestReviewer(self.settings).review(evidence)
            reviewed = self._ground(reviewed, {post.id for post in posts})
            if not reviewed.highlights:
                return None
            return localize_verdict(reviewed)
        except Exception:
            return None

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
    def _ground(verdict: ActivityDigestVerdict, allowed_ids: set[int]) -> ActivityDigestVerdict:
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
                ranked.append(
                    (rank[assessment.importance], utc_timestamp(post.posted_at), post, assessment)
                )
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        highlights = [
            {
                "text": format_fallback_highlight(
                    assessment.category,
                    assessment.headline,
                    has_media=bool(post.media),
                ),
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
