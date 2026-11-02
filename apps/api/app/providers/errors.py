import httpx


class ProviderError(RuntimeError):
    """A source failed in a way that can be shown and retried safely."""


class AuthorNotFoundError(ProviderError):
    pass


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
