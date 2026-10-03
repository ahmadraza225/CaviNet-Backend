"""Files under DATA_DIR (NFR-3).

    DATA_DIR/cases/<case id>/dicom/00001.dcm …   de-identified slices of one upload (FR-04.4)
    DATA_DIR/staging/<random id>/                 an upload while it is being received and
                                                  checked; deleted as soon as it is processed

Paths use random ids, never names or MR numbers. Deleting a patient removes the folder of
each of their cases (FR-03.2).
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


def case_dir(case_id: uuid.UUID) -> Path:
    return data_root() / "cases" / str(case_id)


def case_dicom_dir(case_id: uuid.UUID) -> Path:
    return case_dir(case_id) / "dicom"


def staging_root() -> Path:
    return data_root() / "staging"


def new_staging_dir() -> Path:
    """A fresh folder for one incoming upload."""
    path = staging_root() / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


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
