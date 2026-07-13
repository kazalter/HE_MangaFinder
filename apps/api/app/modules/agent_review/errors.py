class AgentReviewError(RuntimeError):
    """Base error for a review that must not mutate aggregation state."""


class AgentNotConfiguredError(AgentReviewError):
    pass


class AgentTransportError(AgentReviewError):
    pass


class AgentOutputError(AgentReviewError):
    pass
