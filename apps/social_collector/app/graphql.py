"""Authenticated X web GraphQL collector.

This provider is intentionally isolated in the social-collector container. It reads a
Playwright storage-state file, but never returns or logs Cookie values.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.observability import record_metric

_ORIG_REQUESTS_GET = requests.get

DEFAULT_BATCH_BUDGET_SECONDS = float(
    os.getenv("SOCIAL_COLLECTOR_BATCH_BUDGET_SECONDS", "65.0")
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("SOCIAL_COLLECTOR_GRAPHQL_REQUEST_TIMEOUT_SECONDS", "25.0")
)
USER_CACHE_TTL_SECONDS = float(os.getenv("SOCIAL_COLLECTOR_USER_CACHE_TTL_SECONDS", "900.0"))
NEGATIVE_USER_CACHE_TTL_SECONDS = float(
    os.getenv("SOCIAL_COLLECTOR_NEGATIVE_USER_CACHE_TTL_SECONDS", "30.0")
)
USER_CACHE_MAX_SIZE = max(16, int(os.getenv("SOCIAL_COLLECTOR_USER_CACHE_MAX_SIZE", "512")))

WEB_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
GRAPHQL_BASE = "https://x.com/i/api/graphql"
MAIN_JS_RE = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/main\.[A-Za-z0-9_-]+\.js"
)

USER_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

TIMELINE_FEATURES = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

TIMELINE_TOGGLES = {
    "withPayments": False,
    "withAuxiliaryUserLabels": False,
    "withArticleRichContentState": True,
    "withArticlePlainText": False,
    "withArticleSummaryText": True,
    "withArticleVoiceOver": True,
    "withGrokAnalyze": False,
    "withDisallowedReplyControls": False,
}


class GraphQLUnavailable(RuntimeError):
    pass


class GraphQLTransientError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CredentialSnapshot:
    generation: int
    mtime_ns: int
    size: int
    cookie_header: str
    csrf: str
    names: frozenset[str]


@dataclass(frozen=True)
class _UserCacheEntry:
    user: dict[str, Any] | None
    expires_at: float
    generation: int


class WebGraphQLCollector:
    def __init__(
        self,
        state_path: Path,
        proxy_url: str,
        user_agent: str,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.state_path = state_path
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.clock: Callable[[], float] = clock or time.monotonic
        self.query_ids = {
            "UserByScreenName": os.getenv(
                "SOCIAL_COLLECTOR_USER_LOOKUP_QUERY_ID", "2qvSHpkWTMS9i0zJAwDNiA"
            ),
            "UserTweets": os.getenv(
                "SOCIAL_COLLECTOR_USER_TWEETS_QUERY_ID", "6r5OLCC_wFH4CpRyXKuAmQ"
            ),
            "TweetResultByRestId": os.getenv(
                "SOCIAL_COLLECTOR_TWEET_RESULT_QUERY_ID", "D_jNhjWZeRZT5NURzfJZSQ"
            ),
        }
        self._ensure_state()

    def _ensure_state(self) -> None:
        if not hasattr(self, "clock"):
            self.clock = time.monotonic
        if not hasattr(self, "_lock"):
            self._lock = threading.RLock()
        if not hasattr(self, "_query_refresh_lock"):
            self._query_refresh_lock = threading.Lock()
        if not hasattr(self, "_local"):
            self._local = threading.local()
        if not hasattr(self, "_credential_generation"):
            self._credential_generation = 0
        if not hasattr(self, "_cached_creds"):
            self._cached_creds: _CredentialSnapshot | None = None
        if not hasattr(self, "_user_cache"):
            self._user_cache: OrderedDict[str, _UserCacheEntry] = OrderedDict()
        if not hasattr(self, "_query_id_version"):
            self._query_id_version = 0

    def invalidate_session(self) -> None:
        self._ensure_state()
        with self._lock:
            self._credential_generation += 1
            self._cached_creds = None
            self._user_cache.clear()

    def invalidate_user(self, handle: str) -> None:
        self._ensure_state()
        norm = handle.strip().removeprefix("@").casefold()
        with self._lock:
            self._user_cache.pop(norm, None)

    def _load_credentials(self) -> _CredentialSnapshot | None:
        self._ensure_state()
        state_path = getattr(self, "state_path", None)
        if state_path is None:
            return None
        try:
            stat = state_path.stat()
        except OSError:
            with self._lock:
                if self._cached_creds is not None:
                    self._credential_generation += 1
                    self._cached_creds = None
                    self._user_cache.clear()
            return None
        with self._lock:
            gen = self._credential_generation
            cached = self._cached_creds
            if (
                cached is not None
                and cached.generation == gen
                and cached.mtime_ns == stat.st_mtime_ns
                and cached.size == stat.st_size
            ):
                return cached
        try:
            body = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = [item for item in body.get("cookies", []) if item.get("name")]
            cookie_header = "; ".join(
                f"{item['name']}={item.get('value', '')}" for item in cookies
            )
            csrf = next(
                (str(item.get("value", "")) for item in cookies if item.get("name") == "ct0"),
                "",
            )
            names = frozenset(str(item["name"]) for item in cookies)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        with self._lock:
            if self._credential_generation != gen:
                # Re-run if session was invalidated while reading.
                return self._load_credentials()
            if self._cached_creds is not None and (
                self._cached_creds.mtime_ns != stat.st_mtime_ns
                or self._cached_creds.size != stat.st_size
            ):
                self._credential_generation += 1
                self._user_cache.clear()
                gen = self._credential_generation
            snap = _CredentialSnapshot(
                generation=gen,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                cookie_header=cookie_header,
                csrf=csrf,
                names=names,
            )
            self._cached_creds = snap
            return snap

    @property
    def available(self) -> bool:
        snap = self._load_credentials()
        if snap is None:
            return False
        return {"auth_token", "ct0"}.issubset(snap.names)

    def _cookies(self) -> list[dict[str, Any]]:
        body = json.loads(self.state_path.read_text(encoding="utf-8"))
        return [item for item in body.get("cookies", []) if item.get("name")]

    def _cookie_header(self) -> str:
        snap = self._load_credentials()
        if snap is not None:
            return snap.cookie_header
        return "; ".join(f"{item['name']}={item.get('value', '')}" for item in self._cookies())

    def _headers_from_snapshot(self, snap: _CredentialSnapshot) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "ja,en;q=0.8",
            "Authorization": f"Bearer {WEB_BEARER_TOKEN}",
            "Cookie": snap.cookie_header,
            "x-csrf-token": snap.csrf,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "ja",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
        }

    def _headers(self) -> dict[str, str]:
        snap = self._load_credentials()
        if snap is not None:
            return self._headers_from_snapshot(snap)
        cookies = self._cookies()
        cookie = "; ".join(f"{item['name']}={item.get('value', '')}" for item in cookies)
        csrf = next((item.get("value", "") for item in cookies if item.get("name") == "ct0"), "")
        return {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "ja,en;q=0.8",
            "Authorization": f"Bearer {WEB_BEARER_TOKEN}",
            "Cookie": cookie,
            "x-csrf-token": csrf,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "ja",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
        }

    @contextmanager
    def _execution_scope(
        self,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
        use_session: bool = False,
    ) -> Iterator[None]:
        self._ensure_state()
        prev_deadline = getattr(self._local, "deadline", None)
        prev_cancel = getattr(self._local, "cancel_event", None)
        prev_session = getattr(self._local, "session", None)
        prev_session_gen = getattr(self._local, "session_gen", None)
        created_session = None
        if deadline is not None:
            self._local.deadline = (
                min(prev_deadline, deadline) if prev_deadline is not None else deadline
            )
        if cancel_event is not None:
            self._local.cancel_event = cancel_event
        if use_session and prev_session is None and requests.get is _ORIG_REQUESTS_GET:
            created_session = requests.Session()
            record_metric("graphql_pool_opens")
            self._local.session = created_session
            self._local.session_gen = self._credential_generation
        try:
            yield
        finally:
            self._local.deadline = prev_deadline
            self._local.cancel_event = prev_cancel
            self._local.session = prev_session
            self._local.session_gen = prev_session_gen
            if created_session is not None:
                try:
                    created_session.close()
                finally:
                    record_metric("graphql_pool_closes")

    def _remaining_budget(self) -> float | None:
        self._ensure_state()
        cancel_event = getattr(self._local, "cancel_event", None)
        if cancel_event is not None and cancel_event.is_set():
            raise GraphQLTransientError("X GraphQL 请求已取消")
        deadline = getattr(self._local, "deadline", None)
        if deadline is None:
            return None
        remaining = deadline - self.clock()
        if remaining <= 0.05:
            raise GraphQLTransientError("X GraphQL 采集超出时间预算，保留扫描进度")
        return remaining

    def _get(self, url: str, *, authenticated: bool = True) -> requests.Response:
        self._ensure_state()
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        errors: list[str] = []
        session = getattr(self._local, "session", None)
        use_session = session is not None and requests.get is _ORIG_REQUESTS_GET
        for profile in ("chrome124", "chrome101", "firefox135", "safari184"):
            remaining = self._remaining_budget()
            req_timeout = (
                min(DEFAULT_REQUEST_TIMEOUT_SECONDS, max(0.2, remaining))
                if remaining is not None
                else DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
            if authenticated:
                snap = self._load_credentials()
                if snap is None or not {"auth_token", "ct0"}.issubset(snap.names):
                    raise GraphQLUnavailable("X GraphQL 会话缺少 auth_token 或 ct0")
                headers = self._headers_from_snapshot(snap)
                active_gen = snap.generation
            else:
                headers = {"User-Agent": self.user_agent}
                active_gen = self._credential_generation
            try:
                record_metric("graphql_calls")
                if use_session:
                    if getattr(self._local, "session_gen", None) != active_gen:
                        session.cookies.clear()
                        self._local.session_gen = active_gen
                    elif getattr(session, "cookies", None) is not None:
                        session.cookies.clear()
                    response = session.get(
                        url,
                        headers=headers,
                        proxies=proxies,
                        impersonate=profile,
                        timeout=req_timeout,
                        allow_redirects=True,
                    )
                else:
                    response = requests.get(
                        url,
                        headers=headers,
                        proxies=proxies,
                        impersonate=profile,
                        timeout=req_timeout,
                        allow_redirects=True,
                    )
                content = getattr(response, "content", b"")
                if isinstance(content, (bytes, bytearray)):
                    record_metric("bytes_transferred", len(content))
                if getattr(response, "status_code", 0) == 429:
                    record_metric("rate_limited_429")
                cancel_event = getattr(self._local, "cancel_event", None)
                if cancel_event is not None and cancel_event.is_set():
                    raise GraphQLTransientError("X GraphQL 请求已取消")
                return response
            except RequestException as exc:
                record_metric("retries")
                errors.append(f"{profile}: {exc}")
                self._remaining_budget()
        raise GraphQLTransientError("X GraphQL 网络连接失败：" + " | ".join(errors))

    def _discover_query_ids(self) -> None:
        home = self._get("https://x.com/", authenticated=False)
        match = MAIN_JS_RE.search(home.text)
        if not match:
            raise GraphQLTransientError("无法从 X 首页定位前端资源")
        javascript = self._get(match.group(0), authenticated=False).text
        for operation in tuple(self.query_ids):
            found = re.search(rf'queryId:"([^"]+)",operationName:"{operation}"', javascript)
            if found:
                self.query_ids[operation] = found.group(1)

    def _refresh_query_ids(self, seen_version: int) -> None:
        self._ensure_state()
        with self._query_refresh_lock:
            if self._query_id_version != seen_version:
                return
            self._discover_query_ids()
            self._query_id_version += 1

    def _request(
        self,
        operation: str,
        variables: dict[str, Any],
        features: dict[str, bool],
        toggles: dict[str, bool],
        *,
        refreshed: bool = False,
    ) -> dict[str, Any]:
        self._ensure_state()
        if not self.available:
            raise GraphQLUnavailable("X GraphQL 会话缺少 auth_token 或 ct0")
        seen_version = self._query_id_version
        params = urlencode(
            {
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": json.dumps(features, separators=(",", ":")),
                "fieldToggles": json.dumps(toggles, separators=(",", ":")),
            }
        )
        url = f"{GRAPHQL_BASE}/{self.query_ids[operation]}/{operation}?{params}"
        response = self._get(url)
        if response.status_code in {400, 404} and not refreshed:
            self._refresh_query_ids(seen_version)
            return self._request(operation, variables, features, toggles, refreshed=True)
        if response.status_code in {401, 403}:
            raise GraphQLUnavailable(f"X GraphQL 登录会话失效或受限（HTTP {response.status_code}）")
        if response.status_code == 429 or response.status_code >= 500:
            raise GraphQLTransientError(f"X GraphQL 暂时不可用（HTTP {response.status_code}）")
        if not 200 <= response.status_code < 300:
            raise GraphQLTransientError(f"X GraphQL 返回 HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise GraphQLTransientError("X GraphQL 返回了无效 JSON") from exc
        if body.get("errors") and not body.get("data"):
            message = body["errors"][0].get("message", "GraphQL error")
            raise GraphQLTransientError(f"X GraphQL 错误：{message}")
        return body

    def post_status(self, post_id: str) -> dict[str, str | None]:
        body = self._request(
            "TweetResultByRestId",
            {
                "tweetId": str(post_id),
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": True,
            },
            TIMELINE_FEATURES,
            TIMELINE_TOGGLES,
        )
        result = ((body.get("data") or {}).get("tweetResult") or {}).get("result") or {}
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet") or {}
        kind = str(result.get("__typename") or "")
        if kind == "Tweet" or result.get("legacy"):
            return {"status": "available", "reason": None}
        reason = self._unavailable_reason(result)
        folded = reason.casefold()
        deleted_words = (
            "deleted by the post author",
            "post was deleted",
            "tweet was deleted",
            "削除されました",
            "已被删除",
            "已刪除",
        )
        if kind == "TweetTombstone" and any(word in folded for word in deleted_words):
            return {"status": "deleted", "reason": reason or "X 明确返回已删除"}
        return {
            "status": "unavailable",
            "reason": reason or kind or "X 没有返回可访问的帖子",
        }

    @staticmethod
    def _unavailable_reason(result: dict[str, Any]) -> str:
        tombstone = result.get("tombstone") or {}
        text = tombstone.get("text") or {}
        if isinstance(text, dict):
            text = text.get("text") or ""
        return str(text or result.get("reason") or result.get("message") or "").strip()

    def _cache_user_entry(self, norm_handle: str, entry: _UserCacheEntry) -> None:
        with self._lock:
            self._user_cache[norm_handle] = entry
            self._user_cache.move_to_end(norm_handle)
            while len(self._user_cache) > USER_CACHE_MAX_SIZE:
                self._user_cache.popitem(last=False)

    def lookup_user(self, handle: str) -> dict[str, Any] | None:
        self._ensure_state()
        clean = handle.strip().removeprefix("@")
        norm = clean.casefold()
        # Check on-disk file generation before reading user cache so external
        # storage-state updates invalidate stale cached accounts too.
        snap = self._load_credentials()
        gen = snap.generation if snap is not None else self._credential_generation
        now = self.clock()
        with self._lock:
            cached = self._user_cache.get(norm)
            if cached is not None:
                if cached.generation == gen and cached.expires_at > now:
                    self._user_cache.move_to_end(norm)
                    record_metric("identity_cache_hits")
                    return dict(cached.user) if cached.user is not None else None
                self._user_cache.pop(norm, None)

        record_metric("identity_lookups")
        body = self._request(
            "UserByScreenName",
            {"screen_name": clean, "withGrokTranslatedBio": False},
            USER_FEATURES,
            {"withPayments": False, "withAuxiliaryUserLabels": False},
        )
        result = ((body.get("data") or {}).get("user") or {}).get("result") or {}
        if result.get("__typename") != "User":
            self._cache_user_entry(
                norm,
                _UserCacheEntry(
                    user=None,
                    expires_at=self.clock() + NEGATIVE_USER_CACHE_TTL_SECONDS,
                    generation=gen,
                ),
            )
            return None
        core = result.get("core") or {}
        legacy = result.get("legacy") or {}
        avatar = (result.get("avatar") or {}).get("image_url") or legacy.get(
            "profile_image_url_https"
        )
        user_dict = {
            "id": str(result.get("rest_id") or ""),
            "handle": core.get("screen_name") or legacy.get("screen_name") or clean,
            "display_name": core.get("name") or legacy.get("name") or clean,
            "description": legacy.get("description") or "",
            "avatar_url": avatar,
        }
        if not user_dict["id"] or user_dict["handle"].casefold() != norm:
            with self._lock:
                self._user_cache.pop(norm, None)
            return user_dict
        self._cache_user_entry(
            norm,
            _UserCacheEntry(
                user=dict(user_dict),
                expires_at=self.clock() + USER_CACHE_TTL_SECONDS,
                generation=gen,
            ),
        )
        return user_dict

    def suggestions(self, query: str) -> list[dict[str, Any]]:
        clean = query.strip().removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean):
            return []
        user = self.lookup_user(clean)
        if not user:
            return []
        return [
            {
                "handle": user["handle"],
                "display_name": user["display_name"],
                "profile_url": f"https://x.com/{user['handle']}",
                "avatar_url": user["avatar_url"],
                "score": 0.98,
                "evidence": ["X GraphQL 精确账号查询命中"],
            }
        ]

    def posts(
        self,
        handle: str,
        since_id: str | None,
        limit: int,
        *,
        budget_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_state()
        budget = DEFAULT_BATCH_BUDGET_SECONDS if budget_seconds is None else budget_seconds
        deadline = (self.clock() + budget) if budget and budget > 0 else None
        with self._execution_scope(
            deadline=deadline,
            cancel_event=cancel_event,
            use_session=True,
        ):
            user = self.lookup_user(handle)
            if not user or not user["id"]:
                raise GraphQLUnavailable(f"X 账号 @{handle} 不存在或不可访问")
            found: dict[str, dict[str, Any]] = {}
            cursor: str | None = None
            max_pages = max(1, min(12, math.ceil(limit / 20) + 2))
            reached_since = False
            for _ in range(max_pages):
                variables: dict[str, Any] = {
                    "userId": user["id"],
                    "count": min(100, max(20, limit)),
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withVoice": True,
                }
                if cursor:
                    variables["cursor"] = cursor
                body = self._request("UserTweets", variables, TIMELINE_FEATURES, TIMELINE_TOGGLES)
                result = ((body.get("data") or {}).get("user") or {}).get("result") or {}
                if result.get("__typename") in {"UserUnavailable", "UserSuspended"}:
                    self.invalidate_user(handle)
                timeline = (result.get("timeline") or {}).get("timeline") or {}
                next_cursor: str | None = None
                for instruction in timeline.get("instructions") or []:
                    for entry in instruction.get("entries") or []:
                        content = entry.get("content") or {}
                        kind = content.get("entryType") or content.get("__typename") or ""
                        if "Cursor" in kind:
                            if content.get("cursorType") == "Bottom":
                                next_cursor = content.get("value")
                            continue
                        for item in self._entry_items(content):
                            tweet = (item.get("tweet_results") or {}).get("result") or {}
                            parsed = self._parse_tweet(tweet, user["handle"])
                            if not parsed:
                                continue
                            if since_id and parsed["id"] == since_id:
                                reached_since = True
                                break
                            found[parsed["id"]] = parsed
                            if len(found) >= limit:
                                reached_since = True
                                break
                        if reached_since:
                            break
                    if reached_since:
                        break
                if reached_since or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            return sorted(found.values(), key=lambda item: item["posted_at"], reverse=True)

    def posts_batch(
        self,
        handle: str,
        since_id: str | None,
        limit: int,
        cursor: str | None = None,
        stop_before: str | None = None,
        *,
        budget_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Return complete pages and a durable continuation; never split a page."""
        self._ensure_state()
        budget = DEFAULT_BATCH_BUDGET_SECONDS if budget_seconds is None else budget_seconds
        deadline = (self.clock() + budget) if budget and budget > 0 else None
        with self._execution_scope(
            deadline=deadline,
            cancel_event=cancel_event,
            use_session=True,
        ):
            user = self.lookup_user(handle)
            if not user or not user["id"]:
                raise GraphQLUnavailable(f"X 账号 @{handle} 不存在或不可访问")
            found: dict[str, dict[str, Any]] = {}
            initial_cursor = cursor
            current = cursor
            posts: list[dict[str, Any]] = []
            pages_completed = 0
            max_pages = max(1, min(12, math.ceil(limit / 20) + 2))
            for page_idx in range(max_pages):
                if cancel_event is not None and cancel_event.is_set():
                    raise GraphQLTransientError("X GraphQL 采集已取消")
                if deadline is not None and (deadline - self.clock()) <= 0.15:
                    if (
                        page_idx > 0
                        and pages_completed >= 1
                        and current is not None
                        and current != initial_cursor
                    ):
                        return {
                            "posts": posts,
                            "complete": False,
                            "next_cursor": "graphql:" + current,
                        }
                    raise GraphQLTransientError("X GraphQL 采集超出单批时间预算，保留扫描进度")
                variables: dict[str, Any] = {
                    "userId": user["id"],
                    "count": min(100, max(20, limit)),
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withVoice": True,
                }
                if current:
                    variables["cursor"] = current
                record_metric("timeline_pages")
                body = self._request("UserTweets", variables, TIMELINE_FEATURES, TIMELINE_TOGGLES)
                result = ((body.get("data") or {}).get("user") or {}).get("result") or {}
                if result.get("__typename") in {"UserUnavailable", "UserSuspended"}:
                    self.invalidate_user(handle)
                timeline = (result.get("timeline") or {}).get("timeline") or {}
                instructions = timeline.get("instructions")
                if not isinstance(instructions, list):
                    raise GraphQLTransientError("X 时间线结构不完整，未推进扫描位置")
                next_cursor = None
                reached_since = False
                page_times: list[datetime] = []
                for instruction in instructions:
                    for entry in instruction.get("entries") or []:
                        content = entry.get("content") or {}
                        kind = content.get("entryType") or content.get("__typename") or ""
                        if "Cursor" in kind:
                            if content.get("cursorType") == "Bottom":
                                next_cursor = content.get("value")
                            continue
                        for item in self._entry_items(content):
                            tweet = (item.get("tweet_results") or {}).get("result") or {}
                            parsed = self._parse_tweet(tweet, user["handle"])
                            if not parsed:
                                continue
                            if since_id and parsed["id"] == since_id:
                                reached_since = True
                                break
                            page_times.append(datetime.fromisoformat(parsed["posted_at"]))
                            found[parsed["id"]] = parsed
                        if reached_since:
                            break
                    if reached_since:
                        break
                posts = sorted(found.values(), key=lambda item: item["posted_at"], reverse=True)
                old_page = bool(stop_before and page_times) and all(
                    value < datetime.fromisoformat(stop_before) for value in page_times
                )
                if reached_since or old_page:
                    return {"posts": posts, "complete": True, "next_cursor": None}
                if not next_cursor:
                    if since_id:
                        raise GraphQLTransientError("X 时间线未到达上次扫描位置，无法确认完整性")
                    return {"posts": posts, "complete": True, "next_cursor": None}
                if next_cursor == current:
                    raise GraphQLTransientError("X 时间线分页位置没有推进，未推进扫描位置")
                current = next_cursor
                pages_completed += 1
                if len(found) >= limit:
                    return {
                        "posts": posts,
                        "complete": False,
                        "next_cursor": "graphql:" + current,
                    }
            return {"posts": posts, "complete": False, "next_cursor": "graphql:" + current}

    @staticmethod
    def _entry_items(content: dict[str, Any]) -> list[dict[str, Any]]:
        direct = content.get("itemContent")
        if direct:
            return [direct]
        return [
            ((item.get("item") or {}).get("itemContent") or {})
            for item in content.get("items") or []
            if ((item.get("item") or {}).get("itemContent") or {})
        ]

    @staticmethod
    def _parse_tweet(result: dict[str, Any], tracked_handle: str) -> dict[str, Any] | None:
        if result.get("__typename") in {"TweetUnavailable", "TweetTombstone"}:
            return None
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet") or {}
        legacy = result.get("legacy") or {}
        post_id = str(result.get("rest_id") or legacy.get("id_str") or "")
        if not post_id or not legacy.get("created_at"):
            return None
        core_user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
        user_core = core_user.get("core") or {}
        user_legacy = core_user.get("legacy") or {}
        author = user_core.get("screen_name") or user_legacy.get("screen_name") or tracked_handle
        text = legacy.get("full_text") or ""
        post_type = "original"
        if text.startswith("RT @") or legacy.get("retweeted_status_result"):
            post_type = "retweet"
        elif legacy.get("in_reply_to_status_id_str"):
            post_type = "reply"
        elif legacy.get("is_quote_status") or result.get("quoted_status_result"):
            post_type = "quote"
        media: list[dict[str, str]] = []
        for item in (legacy.get("extended_entities") or {}).get("media") or []:
            kind = item.get("type") or "image"
            url = item.get("media_url_https") or item.get("media_url")
            if kind == "photo" and url:
                url = f"{url}?name=orig"
            elif kind in {"video", "animated_gif"}:
                variants = [
                    value
                    for value in (item.get("video_info") or {}).get("variants") or []
                    if value.get("content_type") == "video/mp4" and value.get("url")
                ]
                variants.sort(key=lambda value: int(value.get("bitrate") or 0), reverse=True)
                url = variants[0]["url"] if variants else None
            if url:
                media.append({"type": "image" if kind == "photo" else kind, "url": url})
        links = [
            value
            for item in (legacy.get("entities") or {}).get("urls") or []
            if (value := item.get("expanded_url")) and value.startswith("http")
        ]
        quoted = (result.get("quoted_status_result") or {}).get("result") or {}
        if quoted.get("__typename") == "TweetWithVisibilityResults":
            quoted = quoted.get("tweet") or {}
        quoted_text = (quoted.get("legacy") or {}).get("full_text")
        posted_at = parsedate_to_datetime(legacy["created_at"]).astimezone(UTC).isoformat()
        return {
            "id": post_id,
            "text": text,
            "url": f"https://x.com/{author}/status/{post_id}",
            "post_type": post_type,
            "posted_at": posted_at,
            "conversation_id": str(legacy.get("conversation_id_str") or post_id),
            "replied_to_post_id": legacy.get("in_reply_to_status_id_str"),
            "quoted_post_id": str(quoted.get("rest_id")) if quoted.get("rest_id") else None,
            "media": media,
            "links": links,
            "raw": {
                "author_handle": author,
                "quoted_text": quoted_text,
                "collector": "x_web_graphql",
            },
        }
