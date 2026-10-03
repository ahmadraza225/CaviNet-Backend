"""Job functions run by the worker."""

import uuid

from app.core.database import get_sessionmaker


def ping() -> str:
    """Trivial job used to check that the queue and worker are wired correctly."""
    return "pong"


def analyse_case(case_id: str) -> str:
    """FR-04.6 analysis job: the AI pipeline (M-05) for one queued case."""
    from app.workers import analysis

    with get_sessionmaker()() as db:
        return analysis.run(db, uuid.UUID(case_id))
