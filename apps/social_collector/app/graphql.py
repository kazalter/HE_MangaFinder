"""Authenticated X web GraphQL collector.

This provider is intentionally isolated in the social-collector container. It reads a
Playwright storage-state file, but never returns or logs Cookie values.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

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


class WebGraphQLCollector:
    def __init__(self, state_path: Path, proxy_url: str, user_agent: str) -> None:
        self.state_path = state_path
        self.proxy_url = proxy_url
        self.user_agent = user_agent
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

    @property
    def available(self) -> bool:
        try:
            names = {item["name"] for item in self._cookies()}
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return {"auth_token", "ct0"}.issubset(names)

    def _cookies(self) -> list[dict[str, Any]]:
        body = json.loads(self.state_path.read_text(encoding="utf-8"))
        return [item for item in body.get("cookies", []) if item.get("name")]

    def _cookie_header(self) -> str:
        return "; ".join(f"{item['name']}={item.get('value', '')}" for item in self._cookies())

    def _headers(self) -> dict[str, str]:
        cookie = self._cookie_header()
        csrf = next(
            (item.get("value", "") for item in self._cookies() if item.get("name") == "ct0"),
            "",
        )
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

    def _get(self, url: str, *, authenticated: bool = True) -> requests.Response:
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        errors: list[str] = []
        for profile in ("chrome124", "chrome101", "firefox135", "safari184"):
            try:
                return requests.get(
                    url,
                    headers=self._headers() if authenticated else {"User-Agent": self.user_agent},
                    proxies=proxies,
                    impersonate=profile,
                    timeout=45,
                    allow_redirects=True,
                )
            except RequestException as exc:
                errors.append(f"{profile}: {exc}")
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

    def _request(
        self,
        operation: str,
        variables: dict[str, Any],
        features: dict[str, bool],
        toggles: dict[str, bool],
        *,
        refreshed: bool = False,
    ) -> dict[str, Any]:
        if not self.available:
            raise GraphQLUnavailable("X GraphQL 会话缺少 auth_token 或 ct0")
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
            self._discover_query_ids()
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
        return str(
            text
            or result.get("reason")
            or result.get("message")
            or ""
        ).strip()

    def lookup_user(self, handle: str) -> dict[str, Any] | None:
        body = self._request(
            "UserByScreenName",
            {"screen_name": handle.removeprefix("@"), "withGrokTranslatedBio": False},
            USER_FEATURES,
            {"withPayments": False, "withAuxiliaryUserLabels": False},
        )
        result = ((body.get("data") or {}).get("user") or {}).get("result") or {}
        if result.get("__typename") != "User":
            return None
        core = result.get("core") or {}
        legacy = result.get("legacy") or {}
        avatar = (result.get("avatar") or {}).get("image_url") or legacy.get(
            "profile_image_url_https"
        )
        return {
            "id": str(result.get("rest_id") or ""),
            "handle": core.get("screen_name") or legacy.get("screen_name") or handle,
            "display_name": core.get("name") or legacy.get("name") or handle,
            "description": legacy.get("description") or "",
            "avatar_url": avatar,
        }

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

    def posts(self, handle: str, since_id: str | None, limit: int) -> list[dict[str, Any]]:
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
            timeline = (result.get("timeline") or {}).get("timeline") or {}
            next_cursor: str | None = None
            for instruction in timeline.get("instructions") or []:
                for entry in instruction.get("entries") or []:
                    content = entry.get("content") or {}
                    if "Cursor" in (content.get("entryType") or content.get("__typename") or ""):
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
