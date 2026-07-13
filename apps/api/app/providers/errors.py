class ProviderError(RuntimeError):
    """A source failed in a way that can be shown and retried safely."""


class AuthorNotFoundError(ProviderError):
    pass
