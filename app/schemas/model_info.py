"""Model status (for the DEMO banner) and the admin model page (FR-09.4)."""

from typing import Any

from pydantic import BaseModel


class ModelStatus(BaseModel):
    installed: bool
    is_demo: bool
    model_name: str | None
    model_version: str | None
    created_at: str | None
    demo_banner: str | None


class ModelInfo(BaseModel):
    """Everything in the installed bundle except the weights."""

    model_name: str
    model_version: str | None
    created_at: str  # training date
    is_demo: bool
    format_version: int
    git_commit: str | None
    initialisation: str | None
    architecture: dict[str, Any]
    folds: int
    parameters_per_fold: int
    temperature: float
    decision_threshold: float
    confidence_bands: dict[str, float]
    preprocessing: dict[str, Any]
    label_map: dict[str, str]
    metrics: dict[str, Any]
    data_manifest_sha256: str | None
    file_name: str
    file_size_bytes: int
    file_sha256: str
    demo_banner: str | None
