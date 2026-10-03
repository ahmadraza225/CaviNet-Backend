"""Keep patient identifiers out of the logs (NFR-3).

Patient search terms and delete confirmations travel in query strings
(`/api/patients?q=<name>`), and uvicorn's access log writes the full URL. This filter
drops the query string from access-log lines; nginx does the same with its own log format
(frontend/nginx.conf).
"""

import logging

ACCESS_LOGGER = "uvicorn.access"


class StripQueryString(logging.Filter):
    """uvicorn access records carry (client, method, path with query, http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def install_log_filters() -> None:
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(existing, StripQueryString) for existing in logger.filters):
        logger.addFilter(StripQueryString())
