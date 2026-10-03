"""Patient request/response models with the FR-03.1 validation rules."""

import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.clock import as_utc, utcnow
from app.models import Patient, Sex

EARLIEST_BIRTH_DATE = date(1900, 1, 1)
_MR_RE = re.compile(r"^[A-Z0-9][A-Z0-9/-]*$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{5,30}$")

REQUIRED_FIELDS = ("full_name", "mr_number", "date_of_birth", "sex")


def normalize_mr_number(value: str) -> str:
    return value.strip().upper()


def _full_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Full name is required.")
    if not re.search(r"[^\W\d_]", value):
        raise ValueError("Full name must contain letters.")
    return value


def _mr_number(value: str) -> str:
    value = normalize_mr_number(value)
    if not value:
        raise ValueError("MR number is required.")
    if not _MR_RE.match(value):
        raise ValueError("MR number may contain only letters, digits, '-' and '/'.")
    return value


def _date_of_birth(value: date) -> date:
    if value > utcnow().date():
        raise ValueError("Date of birth cannot be in the future.")
    if value < EARLIEST_BIRTH_DATE:
        raise ValueError("Date of birth must be on or after 1900-01-01.")
    return value


def _phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not _PHONE_RE.match(value):
        raise ValueError("Enter a valid phone number (digits, spaces, +, - and brackets).")
    return value


def _notes(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


class PatientCreate(BaseModel):
    full_name: str = Field(max_length=120)
    mr_number: str = Field(max_length=32)
    date_of_birth: date
    sex: Sex
    phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)

    _v_name = field_validator("full_name")(_full_name)
    _v_mr = field_validator("mr_number")(_mr_number)
    _v_dob = field_validator("date_of_birth")(_date_of_birth)
    _v_phone = field_validator("phone")(_phone)
    _v_notes = field_validator("notes")(_notes)


class PatientUpdate(BaseModel):
    """Partial update: only the fields sent are changed. Required fields cannot be cleared."""

    full_name: str | None = Field(default=None, max_length=120)
    mr_number: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("full_name")
    @classmethod
    def _v_name(cls, value: str | None) -> str | None:
        return None if value is None else _full_name(value)

    @field_validator("mr_number")
    @classmethod
    def _v_mr(cls, value: str | None) -> str | None:
        return None if value is None else _mr_number(value)

    @field_validator("date_of_birth")
    @classmethod
    def _v_dob(cls, value: date | None) -> date | None:
        return None if value is None else _date_of_birth(value)

    _v_phone = field_validator("phone")(_phone)
    _v_notes = field_validator("notes")(_notes)

    @model_validator(mode="after")
    def _required_fields_not_cleared(self) -> "PatientUpdate":
        for name in REQUIRED_FIELDS:
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name.replace('_', ' ').capitalize()} cannot be empty.")
        return self

    def changes(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields_set}


def age_on(date_of_birth: date, today: date) -> int:
    before_birthday = (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
    return today.year - date_of_birth.year - int(before_birthday)


class ScanSummary(BaseModel):
    """One scan in a patient's history (FR-03.4). Filled from Phase 4."""

    id: uuid.UUID
    uploaded_at: datetime
    status: str
    result: str | None = None


class PatientOut(BaseModel):
    id: uuid.UUID
    full_name: str
    mr_number: str
    date_of_birth: date
    age: int
    sex: Sex
    phone: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_patient(cls, patient: Patient, **extra: Any) -> "PatientOut":
        return cls(
            id=patient.id,
            full_name=patient.full_name,
            mr_number=patient.mr_number,
            date_of_birth=patient.date_of_birth,
            age=age_on(patient.date_of_birth, utcnow().date()),
            sex=Sex(patient.sex),
            phone=patient.phone,
            notes=patient.notes,
            created_at=as_utc(patient.created_at),
            updated_at=as_utc(patient.updated_at),
            **extra,
        )


class PatientDetail(PatientOut):
    scans: list[ScanSummary] = []


class PatientPage(BaseModel):
    items: list[PatientOut]
    total: int
    page: int
    page_size: int
    pages: int
