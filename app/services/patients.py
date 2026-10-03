"""Patient management (M-03, FR-03.1 to FR-03.4). Doctor role only (section 9.3).

Audit entries name the patient by id only; names and MR numbers stay in the patients
table (NFR-3).
"""

import enum
import logging
import uuid
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditAction, Patient, User
from app.schemas.patients import PatientCreate, PatientUpdate, normalize_mr_number
from app.services import audit, storage
from app.services.errors import Conflict, InvalidInput, NotFound

logger = logging.getLogger(__name__)

PAGE_SIZE = 20  # FR-03.3


class PatientSort(enum.StrEnum):
    FULL_NAME = "full_name"
    MR_NUMBER = "mr_number"
    DATE_OF_BIRTH = "date_of_birth"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class SortOrder(enum.StrEnum):
    ASC = "asc"
    DESC = "desc"


_SORT_COLUMNS = {
    PatientSort.FULL_NAME: func.lower(Patient.full_name),
    PatientSort.MR_NUMBER: Patient.mr_number,
    PatientSort.DATE_OF_BIRTH: Patient.date_of_birth,
    PatientSort.CREATED_AT: Patient.created_at,
    PatientSort.UPDATED_AT: Patient.updated_at,
}


def _mr_taken() -> Conflict:
    return Conflict("A patient with this MR number already exists.", code="mr_number_taken")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_or_404(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise NotFound("Patient not found.")
    return patient


def count_patients(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Patient)) or 0


def list_patients(
    db: Session,
    *,
    query: str | None = None,
    sort: PatientSort = PatientSort.CREATED_AT,
    order: SortOrder = SortOrder.DESC,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> tuple[list[Patient], int]:
    """FR-03.3: search by name or MR number (case-insensitive, part of the text is
    enough), sort by one column, and page."""
    conditions = []
    text = " ".join((query or "").split())
    if text:
        pattern = f"%{_escape_like(text)}%"
        conditions.append(
            or_(
                Patient.full_name.ilike(pattern, escape="\\"),
                Patient.mr_number.ilike(pattern, escape="\\"),
            )
        )
    total = db.scalar(select(func.count()).select_from(Patient).where(*conditions)) or 0
    column = _SORT_COLUMNS[sort]
    ordering = column.asc() if order == SortOrder.ASC else column.desc()
    items = db.scalars(
        select(Patient)
        .where(*conditions)
        .order_by(ordering, Patient.id)  # id keeps pages stable when values tie
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(items), total


def _mr_number_in_use(db: Session, mr_number: str, *, exclude: uuid.UUID | None = None) -> bool:
    statement = select(Patient.id).where(Patient.mr_number == mr_number)
    if exclude is not None:
        statement = statement.where(Patient.id != exclude)
    return db.scalar(statement.limit(1)) is not None


def _commit(db: Session) -> None:
    """Commit; a unique-index violation means another request took the MR number first."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _mr_taken() from None


def create_patient(db: Session, actor: User, data: PatientCreate) -> Patient:
    if _mr_number_in_use(db, data.mr_number):
        raise _mr_taken()
    patient = Patient(
        full_name=data.full_name,
        mr_number=data.mr_number,
        date_of_birth=data.date_of_birth,
        sex=data.sex.value,
        phone=data.phone,
        notes=data.notes,
        created_by_id=actor.id,
    )
    db.add(patient)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _mr_taken() from None
    audit.record(
        db,
        AuditAction.PATIENT_CREATED,
        actor=actor,
        target_type="patient",
        target_id=str(patient.id),
    )
    _commit(db)
    return patient


def update_patient(db: Session, actor: User, patient: Patient, data: PatientUpdate) -> Patient:
    """Apply the fields that were sent. The audit entry lists which fields changed, not
    their values."""
    changed: dict[str, object] = {}
    for name, value in data.changes().items():
        if isinstance(value, enum.Enum):
            value = value.value
        if getattr(patient, name) != value:
            changed[name] = value
    if not changed:
        return patient
    if "mr_number" in changed and _mr_number_in_use(
        db, str(changed["mr_number"]), exclude=patient.id
    ):
        raise _mr_taken()
    for name, value in changed.items():
        setattr(patient, name, value)
    audit.record(
        db,
        AuditAction.PATIENT_UPDATED,
        actor=actor,
        target_type="patient",
        target_id=str(patient.id),
        details={"fields": sorted(changed)},
    )
    _commit(db)
    return patient


def patient_file_paths(patient: Patient) -> list[Path]:
    """Everything on disk that belongs to the patient (see app.services.storage)."""
    return [storage.patient_dir(patient.id)]


def delete_patient(db: Session, actor: User, patient: Patient, *, confirm: str | None) -> None:
    """FR-03.2: permanent delete. The caller must send the patient's MR number as `confirm`.

    The database rows go first (scans, results and reports from Phase 4 follow through
    ON DELETE CASCADE); the files are removed once that is committed.
    """
    if normalize_mr_number(confirm or "") != patient.mr_number:
        raise InvalidInput(
            "Type the patient's MR number to confirm the deletion.",
            code="confirmation_required",
        )
    paths = patient_file_paths(patient)
    audit.record(
        db,
        AuditAction.PATIENT_DELETED,
        actor=actor,
        target_type="patient",
        target_id=str(patient.id),
    )
    db.delete(patient)
    db.commit()
    failed = storage.remove_paths(paths)
    if failed:
        logger.error("A deleted patient's files could not all be removed: %d left", len(failed))
