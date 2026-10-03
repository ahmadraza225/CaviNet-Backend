"""CT upload and cases (M-04, M-08). Doctor role only (section 9.3)."""

import uuid

from fastapi import APIRouter, Depends, Request, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import DbSession, DoctorUser, require_doctor
from app.core.config import get_settings
from app.models import Case, Patient, User
from app.schemas.cases import CaseOut
from app.services import cases as cases_service
from app.services import patients as patients_service
from app.services import storage, uploads
from app.services.uploads import StagedUpload

router = APIRouter(tags=["cases"], dependencies=[Depends(require_doctor)])


def case_out(db, case: Case) -> CaseOut:
    patient = db.get(Patient, case.patient_id)
    uploader = db.get(User, case.uploaded_by_id) if case.uploaded_by_id else None
    return CaseOut.from_case(case, patient, uploader.full_name if uploader else None)


def _create_and_process(db, doctor: User, patient: Patient, staged: StagedUpload) -> CaseOut:
    case = cases_service.create_case(db, doctor, patient, staged)
    cases_service.process_upload(db, case, staged)
    return case_out(db, case)


@router.post(
    "/patients/{patient_id}/cases", response_model=CaseOut, status_code=status.HTTP_201_CREATED
)
async def upload_scan(
    patient_id: uuid.UUID, request: Request, doctor: DoctorUser, db: DbSession
) -> CaseOut:
    """FR-04.1: one .zip, or one or more .dcm files, as multipart/form-data (field "files").

    The files are streamed to a staging folder, checked (FR-04.2, FR-04.3), de-identified
    and stored (FR-04.4, FR-04.5), and the analysis is queued (FR-04.6). The response is the
    new case: status "queued", or "failed" with the reason. The staged originals are deleted
    before the response is sent.
    """
    patient = await run_in_threadpool(patients_service.get_or_404, db, patient_id)
    staging = await run_in_threadpool(storage.new_staging_dir)
    try:
        staged = await uploads.receive(request, staging, get_settings().max_upload_bytes)
        return await run_in_threadpool(_create_and_process, db, doctor, patient, staged)
    finally:
        await run_in_threadpool(storage.remove_paths, [staging])


@router.get("/cases/{case_id}", response_model=CaseOut)
def get_case(case_id: uuid.UUID, _: DoctorUser, db: DbSession) -> CaseOut:
    """The case with its status timeline (FR-08.1), scan details and result."""
    return case_out(db, cases_service.get_or_404(db, case_id))
