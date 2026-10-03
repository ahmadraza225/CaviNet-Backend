"""Job functions run by the worker."""

import uuid

from app.core.database import get_sessionmaker


def ping() -> str:
    """Trivial job used to check that the queue and worker are wired correctly."""
    return "pong"


def analyse_case(case_id: str) -> str:
    """FR-04.6 analysis job. Phase 4 runs the stub analyser; Phase 5 the AI pipeline."""
    from app.workers import stub_analyser

    with get_sessionmaker()() as db:
        return stub_analyser.run(db, uuid.UUID(case_id))
