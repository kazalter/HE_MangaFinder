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
                    "tombstone": {
                        "text": {"text": "This Post was deleted by the Post author."}
                    },
                }
            }
        }
    }

    assert collector.post_status("123")["status"] == "deleted"
