"""The installed AI model (section 11.6): `MODEL_PATH`, normally /models/cavinet_model.pth,
put there by `make fetch-model`.

The worker loads the ensemble once and reuses it (the cache key includes the file's
modification time, so a newly fetched model is picked up). The API only reads the bundle's
metadata (memory-mapped, without loading the weights) for the model status, the admin
model page (FR-09.4) and the DEMO banner (FR-05.6).
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DEMO_BANNER = "DEMO MODEL: NOT FOR CLINICAL USE"
NOT_INSTALLED = (
    "No AI model is installed, so the scan cannot be analysed. "
    "An administrator must run 'make fetch-model' and restart CaviNet."
)
UNREADABLE = (
    "The installed AI model file cannot be read, so the scan cannot be analysed. "
    "An administrator must run 'make fetch-model' again."
)


class ModelUnavailable(Exception):
    """No usable model; the message is safe to show to the doctor."""


def model_path() -> Path:
    return Path(get_settings().model_path)


def _file_key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=2)
def _metadata(key: tuple[str, int, int]) -> dict[str, Any]:
    from cavinet_ml.model.bundle import file_sha256, load_bundle, metadata

    path = Path(key[0])
    info = metadata(load_bundle(path, mmap=True))
    info["file_name"] = path.name
    info["file_size_bytes"] = key[2]
    info["file_sha256"] = file_sha256(path)
    return info


def model_info() -> dict[str, Any] | None:
    """The installed bundle without its weights, or None if there is no usable model."""
    key = _file_key(model_path())
    if key is None:
        return None
    try:
        return _metadata(key)
    except Exception as error:  # noqa: BLE001 - BundleError, OSError, …
        logger.error("The model file cannot be read: %s", type(error).__name__)
        return None


def model_status() -> dict[str, Any]:
    info = model_info()
    if info is None:
        return {
            "installed": False,
            "is_demo": False,
            "model_name": None,
            "model_version": None,
            "created_at": None,
            "symptom_assisted": False,
        }
    return {
        "installed": True,
        "is_demo": bool(info["is_demo"]),
        "model_name": info["model_name"],
        "model_version": info.get("model_version") or info["created_at"],
        "created_at": info["created_at"],
        "symptom_assisted": get_fusion() is not None,
    }


@lru_cache(maxsize=1)
def _analyser(key: tuple[str, int, int], lungmask_weights: str):
    from cavinet_ml.analysis import Analyser
    from cavinet_ml.inference import Ensemble
    from cavinet_ml.model.bundle import load_bundle
    from cavinet_ml.preprocessing.lungmask_segmenter import LungmaskSegmenter

    ensemble = Ensemble(load_bundle(key[0]))
    segmenter = LungmaskSegmenter(lungmask_weights)
    if not segmenter.available:
        logger.warning("lungmask weights not found; preprocessing will use the fallback crop")
    return Analyser(ensemble, segmenter)


def get_analyser():
    """The worker's analyser for the installed model (loaded once, then reused)."""
    key = _file_key(model_path())
    if key is None:
        raise ModelUnavailable(NOT_INSTALLED)
    try:
        return _analyser(key, get_settings().lungmask_weights)
    except Exception as error:  # noqa: BLE001
        logger.error("The model could not be loaded: %s", type(error).__name__)
        raise ModelUnavailable(UNREADABLE) from error


@lru_cache(maxsize=2)
def _weights_sha256(key: tuple[str, int, int]) -> str:
    from cavinet_ml.fetch import bundle_weights_sha256
    from cavinet_ml.model.bundle import load_bundle

    return bundle_weights_sha256(load_bundle(key[0], mmap=True))


@lru_cache(maxsize=2)
def _fusion(fusion_key: tuple[str, int, int], weights: str):
    from cavinet_ml.fusion import load_fusion

    fusion = load_fusion(fusion_key[0])
    return fusion if fusion.matches(weights) else None


def get_fusion():
    """M-12: the symptom-assisted prediction parameters fitted for the installed model
    (`cavinet_fusion.json` next to it), or None: then only the CT result is shown."""
    from cavinet_ml.fusion import FUSION_FILE

    key = _file_key(model_path())
    fusion_key = _file_key(model_path().with_name(FUSION_FILE))
    if key is None or fusion_key is None:
        return None
    try:
        fusion = _fusion(fusion_key, _weights_sha256(key))
    except Exception as error:  # noqa: BLE001 - FusionError, OSError, …
        logger.error(
            "The symptom-assisted prediction file cannot be used: %s", type(error).__name__
        )
        return None
    if fusion is None:
        logger.warning("The symptom-assisted prediction file is for another model; ignored")
    return fusion


def clear_caches() -> None:
    _metadata.cache_clear()
    _analyser.cache_clear()
    _weights_sha256.cache_clear()
    _fusion.cache_clear()


def configure_threads() -> None:
    """Use every CPU core for inference unless TORCH_NUM_THREADS says otherwise."""
    import torch

    threads = int(os.environ.get("TORCH_NUM_THREADS") or (os.cpu_count() or 1))
    torch.set_num_threads(max(1, threads))
