import httpx


class ProviderError(RuntimeError):
    """A source failed in a way that can be shown and retried safely."""


class AuthorNotFoundError(ProviderError):
    pass


class AuthorIdentityUnconfirmedError(AuthorNotFoundError):
    def __init__(self, original: str, candidate: str) -> None:
        self.candidate = candidate
        super().__init__(f'来源推测“{candidate}”可能对应“{original}”，请核对并加入作者别名后重试')


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Describe transport failures without leaking request URLs or credentials."""
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.ProxyError):
        return "代理连接失败"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败（请检查网络、DNS 或代理）"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        explanation = {
            403: "访问被拒绝或需要验证",
            429: "请求过于频繁",
            503: "来源暂时不可用或需要验证",
        }.get(status)
        return f"HTTP {status}" + (f"（{explanation}）" if explanation else "")
    return f"网络请求失败（{type(exc).__name__}）"


def classify_provider_error(message: str | None) -> str:
    """Classify recorded source errors conservatively; old free-text errors remain supported."""
    value = (message or "").lower()
    if "超时" in value or "timeout" in value or "timed out" in value:
        return "timeout"
    if "代理" in value or "proxy" in value:
        return "proxy"
    if "403" in value or "cloudflare" in value or "访问被拒绝" in value:
        return "forbidden"
    if "429" in value or "过于频繁" in value:
        return "rate_limit"
    if "结构" in value or "解析" in value or "数据缺失" in value or "缺少作品数据" in value:
        return "parse"
    if "候选域名" in value or "dns" in value or "连接失败" in value or "connect" in value:
        return "connection"
    if any(f"http {code}" in value for code in (500, 502, 503, 504)):
        return "unavailable"
    return "other"
