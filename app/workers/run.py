"""Entry point for the worker container: python -m app.workers.run"""

import logging

from rq import Worker

from app.core.config import get_settings
from app.core.redis import get_redis


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    worker = Worker([settings.analysis_queue], connection=get_redis())
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
