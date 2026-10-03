"""Case responses (M-04, M-08)."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.core.clock import as_utc
from app.models import Case, Patient


class CasePatient(BaseModel):
    id: uuid.UUID
    full_name: str
    mr_number: str


class TimelineEntry(BaseModel):
    status: str
    at: datetime
    message: str | None


class ScanDetails(BaseModel):
    """FR-04.5 metadata and the FR-04.3 series choice."""

    study_date: date | None
    num_slices: int | None
    slice_thickness_mm: float | None
    slice_spacing_mm: float | None
    pixel_spacing_mm: tuple[float, float] | None
    rows: int | None
    columns: int | None
    manufacturer: str | None
    model: str | None
    kernel: str | None
    series_found: int | None
    series_number: int | None


class UploadDetails(BaseModel):
    kind: str
    files: int
    bytes: int


class CaseResult(BaseModel):
    """Summary on the case page; the full result is GET /api/cases/{id}/result."""

    label: str
    is_stub: bool
    is_demo: bool
    analyser: str | None
    note: str | None
    probability_tb_pct: float | None = None
    confidence_pct: float | None = None
    band: str | None = None


class ValidatedPerformance(BaseModel):
    """FR-06.3: the model's measured performance, from its model card."""

    auc: float | None
    sensitivity: float | None
    specificity: float | None
    cases: int | None
    dataset: str | None


class ResultModel(BaseModel):
    name: str
    version: str
    trained_at: str | None
    folds: int | None
    is_demo: bool


class ResultOut(BaseModel):
    """FR-06.1 to FR-06.4 and FR-05.4/05.6 for one completed case."""

    case_id: uuid.UUID
    patient: CasePatient
    predicted_class: str
    probability_tb: float
    probability_tb_pct: float
    confidence_pct: float
    band: str
    inconclusive: bool
    explanation: str
    disclaimer: str
    is_demo: bool
    demo_banner: str | None
    model: ResultModel
    validated_performance: ValidatedPerformance
    warnings: list[str]
    processing_seconds: float | None
    step_seconds: dict[str, float]
    lung_volume_ml: float | None
    preview_count: int
    completed_at: datetime | None


class CaseOut(BaseModel):
    id: uuid.UUID
    patient: CasePatient
    status: str
    uploaded_at: datetime
    uploaded_by: str | None
    completed_at: datetime | None
    failure_reason: str | None
    timeline: list[TimelineEntry]
    upload: UploadDetails
    scan: ScanDetails | None
    result: CaseResult | None

    @classmethod
    def from_case(cls, case: Case, patient: Patient, uploaded_by: str | None) -> "CaseOut":
        scan = None
        if case.num_slices is not None:
            spacing = None
            if case.pixel_spacing_row_mm is not None and case.pixel_spacing_col_mm is not None:
                spacing = (case.pixel_spacing_row_mm, case.pixel_spacing_col_mm)
            scan = ScanDetails(
                study_date=case.study_date,
                num_slices=case.num_slices,
                slice_thickness_mm=case.slice_thickness_mm,
                slice_spacing_mm=case.slice_spacing_mm,
                pixel_spacing_mm=spacing,
                rows=case.rows,
                columns=case.columns,
                manufacturer=case.manufacturer,
                model=case.manufacturer_model,
                kernel=case.convolution_kernel,
                series_found=case.series_found,
                series_number=case.series_number,
            )
        result = None
        if case.result_label:
            details = case.result_details or {}
            result = CaseResult(
                label=case.result_label,
                is_stub=case.result_is_stub,
                is_demo=case.model_is_demo,
                analyser=case.analyser,
                note=details.get("note"),
                probability_tb_pct=details.get("probability_tb_pct"),
                confidence_pct=details.get("confidence_pct"),
                band=case.confidence_band,
            )
        return cls(
            id=case.id,
            patient=CasePatient(
                id=patient.id, full_name=patient.full_name, mr_number=patient.mr_number
            ),
            status=case.status,
            uploaded_at=as_utc(case.created_at),
            uploaded_by=uploaded_by,
            completed_at=as_utc(case.completed_at),
            failure_reason=case.failure_reason,
            timeline=[
                TimelineEntry(
                    status=event.status, at=as_utc(event.created_at), message=event.message
                )
                for event in case.events
            ],
            upload=UploadDetails(
                kind=case.upload_kind, files=case.upload_files, bytes=case.upload_bytes
            ),
            scan=scan,
            result=result,
        )
