"""Model status for every signed-in user: is a model installed, and is it the demo model?
The web app shows the DEMO banner (FR-05.6) on every screen while it is."""

from fastapi import APIRouter, Depends

from app.api.deps import require_any_role
from app.schemas.model_info import ModelStatus
from app.services import model_store

router = APIRouter(prefix="/model", tags=["model"], dependencies=[Depends(require_any_role)])


@router.get("/status", response_model=ModelStatus)
def get_model_status() -> ModelStatus:
    status = model_store.model_status()
    banner = model_store.DEMO_BANNER if status["is_demo"] else None
    return ModelStatus(**status, demo_banner=banner)
