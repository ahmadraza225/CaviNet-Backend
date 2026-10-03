"""Entry point for the worker container: python -m app.workers.run

A SimpleWorker runs jobs in this process, so the AI model and the lungmask network are
loaded once and reused for every scan (forking a fresh process per job would reload them).
"""

import logging

from rq import SimpleWorker

from app.core.config import get_settings
from app.core.database import get_sessionmaker
from app.core.redis import get_redis
from app.services import model_store
from app.services.cases import enqueue_analysis
from app.workers.analysis import recover_interrupted
from cavinet_ml import hide_library_notices

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    hide_library_notices()
    settings = get_settings()
    model_store.configure_threads()
    with get_sessionmaker()() as db:
        resumed = recover_interrupted(db, enqueue_analysis)
    if resumed:
        logger.warning("Found %d interrupted analyses at start-up", resumed)
    status = model_store.model_status()
    if not status["installed"]:
        logger.warning("No AI model is installed; run 'make fetch-model'")
    worker = SimpleWorker([settings.analysis_queue], connection=get_redis())
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
