"""CT upload and cases (M-04, M-08). Doctor role only (section 9.3)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import DbSession, DoctorUser, require_doctor
from app.core.config import get_settings
from app.models import AuditAction, Case, Patient, User
from app.schemas.cases import CaseOut, ResultOut
from app.services import audit, report, results, storage, uploads
from app.services import cases as cases_service
from app.services import patients as patients_service
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


@router.get("/cases/{case_id}/result", response_model=ResultOut)
def get_result(case_id: uuid.UUID, doctor: DoctorUser, db: DbSession) -> ResultOut:
    """FR-06.1 to FR-06.3: class, probability, confidence, band, explanation, validated
    performance, disclaimer and the DEMO banner (FR-05.6). Viewing is audited (FR-09.2)."""
    case = cases_service.get_or_404(db, case_id)
    result = results.result_for(case, db.get(Patient, case.patient_id))
    audit.record(
        db, AuditAction.RESULT_VIEWED, actor=doctor, target_type="case", target_id=str(case.id)
    )
    db.commit()
    return result


@router.get(
    "/cases/{case_id}/report",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The PDF report."}},
)
def download_report(
    case_id: uuid.UUID,
    doctor: DoctorUser,
    db: DbSession,
    tz: Annotated[
        str | None,
        Query(max_length=64, description="IANA time zone for the report date, e.g. Asia/Karachi"),
    ] = None,
) -> Response:
    """FR-07.1, FR-07.2: the diagnostic PDF report of a completed case. It is generated on
    each download and never stored (NFR-3). Downloads are audited (FR-07.3)."""
    case = cases_service.get_or_404(db, case_id)
    patient = db.get(Patient, case.patient_id)
    result = results.result_for(case, patient)
    pdf, filename = report.report_for(
        case, patient, result, doctor.full_name, report.timezone_or_utc(tz)
    )
    audit.record(
        db, AuditAction.REPORT_DOWNLOADED, actor=doctor, target_type="case", target_id=str(case.id)
    )
    db.commit()
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/cases/{case_id}/previews/{index}", response_class=FileResponse)
def get_preview(
    case_id: uuid.UUID,
    _: DoctorUser,
    db: DbSession,
    index: int = Path(ge=0, le=999),
) -> FileResponse:
    """FR-06.4: one of the 48 lung-window slices (PNG), 0 = nearest the head."""
    case = cases_service.get_or_404(db, case_id)
    return FileResponse(
        results.preview_path(case, index),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )
