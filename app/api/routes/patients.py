"""Patient management (M-03, FR-03.1 to FR-03.4). Doctor role only (section 9.3)."""

import math
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import DbSession, DoctorUser, require_doctor
from app.core.clock import as_utc
from app.schemas.patients import (
    PatientCreate,
    PatientDetail,
    PatientOut,
    PatientPage,
    PatientUpdate,
    ScanSummary,
)
from app.services import cases as cases_service
from app.services import patients as patients_service
from app.services.patients import PAGE_SIZE, PatientSort, SortOrder

router = APIRouter(prefix="/patients", tags=["patients"], dependencies=[Depends(require_doctor)])


@router.get("", response_model=PatientPage)
def list_patients(
    _: DoctorUser,
    db: DbSession,
    q: Annotated[str | None, Query(max_length=120)] = None,
    sort: PatientSort = PatientSort.CREATED_AT,
    order: SortOrder = SortOrder.DESC,
    page: Annotated[int, Query(ge=1)] = 1,
) -> PatientPage:
    items, total = patients_service.list_patients(db, query=q, sort=sort, order=order, page=page)
    return PatientPage(
        items=[PatientOut.from_patient(patient) for patient in items],
        total=total,
        page=page,
        page_size=PAGE_SIZE,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
    )


@router.post("", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
def create_patient(body: PatientCreate, doctor: DoctorUser, db: DbSession) -> PatientOut:
    return PatientOut.from_patient(patients_service.create_patient(db, doctor, body))


@router.get("/{patient_id}", response_model=PatientDetail)
def get_patient(patient_id: uuid.UUID, _: DoctorUser, db: DbSession) -> PatientDetail:
    patient = patients_service.get_or_404(db, patient_id)
    scans = [
        ScanSummary(
            id=case.id,
            uploaded_at=as_utc(case.created_at),
            status=case.status,
            result=case.result_label,
            result_is_demo=case.model_is_demo,
        )
        for case in cases_service.cases_for_patient(db, patient)
    ]
    return PatientDetail.from_patient(patient, scans=scans)


@router.patch("/{patient_id}", response_model=PatientOut)
def update_patient(
    patient_id: uuid.UUID, body: PatientUpdate, doctor: DoctorUser, db: DbSession
) -> PatientOut:
    patient = patients_service.get_or_404(db, patient_id)
    return PatientOut.from_patient(patients_service.update_patient(db, doctor, patient, body))


@router.delete("/{patient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_patient(
    patient_id: uuid.UUID,
    doctor: DoctorUser,
    db: DbSession,
    confirm: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    """FR-03.2: permanent. `confirm` must be the patient's MR number."""
    patient = patients_service.get_or_404(db, patient_id)
    patients_service.delete_patient(db, doctor, patient, confirm=confirm)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
