"""Job functions run by the worker."""


def ping() -> str:
    """Trivial job used to check that the queue and worker are wired correctly."""
    return "pong"
