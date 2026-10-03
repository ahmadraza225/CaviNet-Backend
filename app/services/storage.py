"""Files under DATA_DIR. Every file that belongs to a patient (scans, results, reports, from
Phase 4 on) lives under `DATA_DIR/patients/<patient id>/`, so deleting a patient removes one
directory (FR-03.2, NFR-3). Paths use the patient's random id, never their name or MR number.
"""

import logging
import shutil
import uuid
from collections.abc import Iterable
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def data_root() -> Path:
    return Path(get_settings().data_dir).resolve()


def patient_dir(patient_id: uuid.UUID) -> Path:
    return data_root() / "patients" / str(patient_id)


def remove_paths(paths: Iterable[Path]) -> list[Path]:
    """Delete files and directories inside DATA_DIR. Missing paths are fine. Returns the
    paths that could not be removed (refused or failed); failures are logged, not raised,
    because the database change they belong to is already committed."""
    root = data_root()
    failed: list[Path] = []
    for path in paths:
        resolved = Path(path).resolve()
        if root not in resolved.parents:
            logger.error("Refusing to delete %s: it is not inside DATA_DIR", resolved)
            failed.append(resolved)
            continue
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not delete %s", resolved)
            failed.append(resolved)
    return failed
