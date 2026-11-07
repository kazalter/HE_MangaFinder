from app.graphql import WebGraphQLCollector


def tweet_result(**legacy_overrides):
    legacy = {
        "created_at": "Mon Jul 13 12:24:32 +0000 2026",
        "conversation_id_str": "100",
        "full_text": "夏コミ新刊です",
        **legacy_overrides,
    }
    return {
        "__typename": "Tweet",
        "rest_id": "100",
        "legacy": legacy,
        "core": {"user_results": {"result": {"core": {"screen_name": "creator"}}}},
    }


def test_parse_retweet_is_explicitly_classified() -> None:
    parsed = WebGraphQLCollector._parse_tweet(
        tweet_result(full_text="RT @other: 新刊です"), "creator"
    )

    assert parsed is not None
    assert parsed["post_type"] == "retweet"
    assert parsed["raw"]["collector"] == "x_web_graphql"


def test_parse_quote_keeps_quoted_text_separate_from_author_text() -> None:
    result = tweet_result(full_text="これ！よろしく！", is_quote_status=True)
    result["quoted_status_result"] = {
        "result": {
            "__typename": "Tweet",
            "rest_id": "90",
            "legacy": {"full_text": "花店の設営写真"},
        }
    }

    parsed = WebGraphQLCollector._parse_tweet(result, "creator")

    assert parsed is not None
    assert parsed["post_type"] == "quote"
    assert parsed["text"] == "これ！よろしく！"
    assert parsed["quoted_post_id"] == "90"
    assert parsed["raw"]["quoted_text"] == "花店の設営写真"


def test_post_status_only_calls_explicit_tombstone_deleted() -> None:
    collector = WebGraphQLCollector.__new__(WebGraphQLCollector)
    collector._request = lambda *args, **kwargs: {
        "data": {
            "tweetResult": {
                "result": {
                    "__typename": "TweetTombstone",
                    "tombstone": {"text": {"text": "This Post was deleted by the Post author."}},
                }
            }
        }
    }

    assert collector.post_status("123")["status"] == "deleted"


def test_batched_timeline_resumes_at_page_boundary_without_losing_entries() -> None:
    collector = WebGraphQLCollector.__new__(WebGraphQLCollector)
    collector.lookup_user = lambda handle: {"id": "u", "handle": handle}
    seen = []

    def item(pid):
        result = tweet_result()
        result["rest_id"] = str(pid)
        return {"content": {"itemContent": {"tweet_results": {"result": result}}}}

    def request(operation, variables, features, toggles):
        seen.append(variables.get("cursor"))
        if variables.get("cursor") is None:
            entries = [item(5), item(4), item(3)]
            entries.append(
                {
                    "content": {
                        "entryType": "TimelineTimelineCursor",
                        "cursorType": "Bottom",
                        "value": "next",
                    }
                }
            )
        else:
            entries = [item(2), item(1)]
        return {
            "data": {
                "user": {
                    "result": {"timeline": {"timeline": {"instructions": [{"entries": entries}]}}}
                }
            }
        }

    collector._request = request
    first = collector.posts_batch("creator", "1", 2)
    assert [post["id"] for post in first["posts"]] == ["5", "4", "3"]
    assert first["complete"] is False and first["next_cursor"] == "graphql:next"
    second = collector.posts_batch("creator", "1", 2, "next")
    assert [post["id"] for post in second["posts"]] == ["2"]
    assert second["complete"] is True
    assert seen == [None, "next"]


def test_incomplete_timeline_does_not_claim_catchup():
    import pytest

    from app.graphql import GraphQLTransientError

    collector = WebGraphQLCollector.__new__(WebGraphQLCollector)
    collector.lookup_user = lambda handle: {"id": "u", "handle": handle}
    collector._request = lambda *args, **kwargs: {
        "data": {"user": {"result": {"timeline": {"timeline": {"instructions": []}}}}}
    }
    with pytest.raises(GraphQLTransientError, match="无法确认完整性"):
        collector.posts_batch("creator", "old-watermark", 2)
