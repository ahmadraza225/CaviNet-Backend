"""CT scan intake (M-04): read an upload, validate it, pick the series, de-identify and store.

    sources = sources_for(upload)               # a .zip, or individual files
    headers = read_headers(sources)             # FR-04.2 "files are DICOM"
    series = select_series(headers)             # FR-04.3 the series with the most slices
    validate_series(series)                     # FR-04.2 the remaining rules
    metadata = write_deidentified(series, dir)  # FR-04.4 then FR-04.5

Every rule failure raises `ScanRejected` with a plain-language reason that is shown to the
doctor and stored on the case. Reasons never quote file names or DICOM values that could
identify the patient (NFR-3).
"""

import io
import math
import secrets
import shutil
import statistics
import warnings
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO

import pydicom
from pydicom import config as pydicom_config
from pydicom.datadict import tag_for_keyword
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.uid import generate_uid

# Never raise or warn about odd values while reading (warnings could quote patient data).
pydicom_config.settings.reading_validation_mode = pydicom_config.IGNORE

MIN_SLICES = 50  # FR-04.2
MAX_SLICE_SPACING_MM = 5.0  # FR-04.2
# Slices count as axial when their normal is within about 20 degrees of the body axis.
AXIAL_MIN_COSINE = math.cos(math.radians(20))
MAX_ZIP_ENTRIES = 20_000
MAX_UNZIPPED_BYTES = 8 * 1024**3

# Files that scanners and operating systems add to exports; they are not part of the scan.
_IGNORED_NAMES = {"dicomdir", ".ds_store", "thumbs.db", "desktop.ini"}

_MODALITY_NAMES = {
    "MR": "an MRI scan",
    "PT": "a PET scan",
    "NM": "a nuclear medicine scan",
    "US": "an ultrasound",
    "CR": "an X-ray",
    "DX": "an X-ray",
    "MG": "a mammogram",
    "XA": "an angiogram",
    "SR": "a report, not images",
}


class ScanRejected(Exception):
    """The upload does not meet a rule; `reason` is shown to the doctor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- Sources ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One uploaded file: a staged file or a member of the uploaded .zip."""

    index: int
    opener: Callable[[], BinaryIO]

    def open(self) -> BinaryIO:
        return self.opener()


@dataclass
class Upload:
    """What the request handler staged: one .zip, or one or more other files."""

    kind: str  # "zip" or "dcm"
    paths: list[Path]


def _ignored(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    base = parts[-1].lower()
    return (
        name.endswith("/") or "__MACOSX" in parts or base in _IGNORED_NAMES or base.startswith("._")
    )


@contextmanager
def sources_for(upload: Upload) -> Iterator[list[Source]]:
    """The files to inspect. A .zip is read in place: members are never extracted to disk."""
    if upload.kind != "zip":
        yield [
            Source(index, (lambda path=path: path.open("rb")))
            for index, path in enumerate(upload.paths)
        ]
        return

    path = upload.paths[0]
    if not zipfile.is_zipfile(path):
        raise ScanRejected("The .zip file is damaged or is not a zip file.")
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ScanRejected("The .zip file is damaged or is not a zip file.") from None
    with archive:
        members = [info for info in archive.infolist() if not _ignored(info.filename)]
        if any(info.flag_bits & 0x1 for info in members):
            raise ScanRejected("The .zip file is password-protected. Upload an unprotected .zip.")
        if len(members) > MAX_ZIP_ENTRIES:
            raise ScanRejected(
                f"The .zip file contains more than {MAX_ZIP_ENTRIES:,} files. "
                "Upload only the CT scan."
            )
        if sum(info.file_size for info in members) > MAX_UNZIPPED_BYTES:
            raise ScanRejected("The .zip file expands to more than 8 GB. Upload only the CT scan.")
        if not members:
            raise ScanRejected("The .zip file contains no DICOM files.")
        yield [
            Source(index, (lambda info=info: archive.open(info)))
            for index, info in enumerate(members)
        ]


# --- Headers (FR-04.2: files are DICOM) ---------------------------------------------------


@dataclass
class SliceHeader:
    source: Source
    series_uid: str
    series_number: int | None
    modality: str
    is_image: bool
    rows: int | None = None
    columns: int | None = None
    orientation: tuple[float, ...] | None = None
    position: tuple[float, ...] | None = None
    thickness_mm: float | None = None
    pixel_spacing_mm: tuple[float, float] | None = None
    study_date: date | None = None
    manufacturer: str | None = None
    model: str | None = None
    kernel: str | None = None


def _floats(ds: Dataset, keyword: str, count: int) -> tuple[float, ...] | None:
    try:
        value = ds.get(keyword)
        if value is None:
            return None
        values = list(value) if isinstance(value, list | tuple | MultiValue) else [value]
        floats = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    if len(floats) != count or not all(math.isfinite(v) for v in floats):
        return None
    return floats


def _int(ds: Dataset, keyword: str) -> int | None:
    try:
        value = ds.get(keyword)
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _text(ds: Dataset, keyword: str, limit: int = 64) -> str | None:
    value = ds.get(keyword)
    if value in (None, ""):
        return None
    if isinstance(value, list | tuple | MultiValue):
        value = "\\".join(str(v) for v in value)
    text = str(value).strip()
    return text[:limit] or None


def _date(ds: Dataset, keyword: str) -> date | None:
    value = str(ds.get(keyword, "") or "").strip()
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date() if len(value) >= 8 else None
    except ValueError:
        return None


def _read(fp: BinaryIO, *, headers_only: bool) -> Dataset:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pydicom.dcmread(fp, stop_before_pixels=headers_only)


def _header(source: Source) -> SliceHeader | None:
    """The slice's header, or None if the file is not DICOM (or is damaged)."""
    try:
        with source.open() as fp:
            ds = _read(fp, headers_only=True)
    except Exception:  # noqa: BLE001 - any read failure means "not a usable DICOM file"
        return None
    if "SOPClassUID" not in ds:  # every DICOM object has one; a truncated file does not
        return None
    spacing = _floats(ds, "PixelSpacing", 2)
    return SliceHeader(
        source=source,
        series_uid=str(ds.get("SeriesInstanceUID", "") or ""),
        series_number=_int(ds, "SeriesNumber"),
        modality=str(ds.get("Modality", "") or "").strip().upper(),
        is_image="Rows" in ds and "Columns" in ds,
        rows=_int(ds, "Rows"),
        columns=_int(ds, "Columns"),
        orientation=_floats(ds, "ImageOrientationPatient", 6),
        position=_floats(ds, "ImagePositionPatient", 3),
        thickness_mm=(_floats(ds, "SliceThickness", 1) or (None,))[0],
        pixel_spacing_mm=(spacing[0], spacing[1]) if spacing else None,
        study_date=_date(ds, "StudyDate"),
        manufacturer=_text(ds, "Manufacturer"),
        model=_text(ds, "ManufacturerModelName"),
        kernel=_text(ds, "ConvolutionKernel"),
    )


def read_headers(sources: list[Source]) -> list[SliceHeader]:
    """FR-04.2: every uploaded file must be DICOM."""
    headers, not_dicom = [], 0
    for source in sources:
        header = _header(source)
        if header is None:
            not_dicom += 1
        else:
            headers.append(header)
    if not_dicom:
        noun = "file is" if not_dicom == 1 else "files are"
        raise ScanRejected(
            f"{not_dicom} of the {len(sources)} uploaded {noun} not DICOM (or damaged). "
            "Upload only the CT scan's DICOM (.dcm) files, or a .zip that contains them."
        )
    return headers


# --- Series selection (FR-04.3) -----------------------------------------------------------


@dataclass
class Series:
    slices: list[SliceHeader]
    series_found: int
    series_number: int | None
    other_sizes: list[int] = field(default_factory=list)

    @property
    def description(self) -> str:
        """Plain-language record of the choice, shown on the case timeline."""
        number = f"series {self.series_number}" if self.series_number is not None else "a series"
        if self.series_found == 1:
            return f"One image series found ({len(self.slices)} slices)."
        others = ", ".join(str(size) for size in self.other_sizes)
        return (
            f"{self.series_found} image series found; used {number} with the most slices "
            f"({len(self.slices)}; others: {others})."
        )


def select_series(headers: list[SliceHeader]) -> Series:
    """The series with the most slices, as the dataset authors did. Ties go to the lower
    series number, so the choice is repeatable."""
    groups: dict[str, list[SliceHeader]] = {}
    for header in headers:
        if header.is_image:
            groups.setdefault(header.series_uid, []).append(header)
    if not groups:
        raise ScanRejected("No DICOM images were found in the upload.")

    def rank(item: tuple[str, list[SliceHeader]]) -> tuple[int, int, str]:
        uid, slices = item
        number = slices[0].series_number
        return (-len(slices), number if number is not None else 1 << 30, uid)

    ordered = sorted(groups.items(), key=rank)
    chosen = ordered[0][1]
    return Series(
        slices=chosen,
        series_found=len(groups),
        series_number=chosen[0].series_number,
        other_sizes=[len(slices) for _, slices in ordered[1:]],
    )


# --- Validation (FR-04.2) -----------------------------------------------------------------


def _normal(orientation: tuple[float, ...]) -> tuple[float, float, float]:
    ax, ay, az, bx, by, bz = orientation
    n = (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)
    length = math.sqrt(sum(v * v for v in n)) or 1.0
    return (n[0] / length, n[1] / length, n[2] / length)


def slice_spacing_mm(series: Series) -> float:
    """Median distance between neighbouring slice positions along the slice normal."""
    normal = _normal(series.slices[0].orientation)  # type: ignore[arg-type]
    distances = sorted(
        {
            round(sum(p * n for p, n in zip(s.position, normal, strict=True)), 3)  # type: ignore[arg-type]
            for s in series.slices
        }
    )
    if len(distances) < 2:
        raise ScanRejected(
            "All slices have the same position, so the slice spacing cannot be measured."
        )
    return statistics.median(b - a for a, b in zip(distances, distances[1:], strict=False))


def validate_series(series: Series) -> float:
    """Apply the FR-04.2 rules in the order the requirement lists them. Returns the slice
    spacing in mm."""
    slices = series.slices

    modalities = {s.modality for s in slices}
    if modalities != {"CT"}:
        other = sorted(modalities - {"CT"})[0]
        if not other:
            raise ScanRejected(
                "The scan does not say what kind of scan it is (no modality). "
                "CaviNet analyses chest CT scans only."
            )
        kind = _MODALITY_NAMES.get(other, f"a {other} scan")
        raise ScanRejected(
            f"The selected series is {kind}, not a CT scan. CaviNet analyses chest CT scans only."
        )

    if any(s.orientation is None for s in slices):
        raise ScanRejected(
            "The slices have no orientation information, so CaviNet cannot check that "
            "they are axial."
        )
    if any(abs(_normal(s.orientation)[2]) < AXIAL_MIN_COSINE for s in slices):  # type: ignore[arg-type]
        raise ScanRejected(
            "The slices are not axial (cross-sections of the body). "
            "Upload the axial series of the chest CT."
        )

    if len(slices) < MIN_SLICES:
        raise ScanRejected(
            f"The scan has {len(slices)} slices; at least {MIN_SLICES} are needed. "
            "Upload the full chest CT series."
        )

    sizes = sorted({(s.rows, s.columns) for s in slices}, key=str)
    if len(sizes) > 1:
        listed = " and ".join(f"{r}×{c}" for r, c in sizes[:3])
        raise ScanRejected(
            f"The slices do not all have the same size ({listed} pixels). "
            "All slices of the scan must have the same number of rows and columns."
        )

    if any(s.position is None for s in slices):
        raise ScanRejected(
            "The slices have no position information, so the slice spacing cannot be checked."
        )
    spacing = slice_spacing_mm(series)
    if spacing > MAX_SLICE_SPACING_MM + 1e-6:
        raise ScanRejected(
            f"The slices are {spacing:.1f} mm apart; the maximum is "
            f"{MAX_SLICE_SPACING_MM:g} mm. Upload a thinner-slice reconstruction of the scan."
        )
    return spacing


# --- De-identification (FR-04.4) ----------------------------------------------------------

# Attributes that must stay present but empty (Type 2 in the CT image IOD).
EMPTIED_KEYWORDS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "ReferringPhysicianName",
    "AccessionNumber",
    "StudyID",
)
# Attributes removed wherever they appear (also inside sequences).
REMOVED_KEYWORDS = (
    # names
    "OtherPatientNames",
    "PatientBirthName",
    "PatientMotherBirthName",
    "ResponsiblePerson",
    # identifiers
    "OtherPatientIDs",
    "OtherPatientIDsSequence",
    "IssuerOfPatientID",
    "MedicalRecordLocator",
    "AdmissionID",
    "RequestedProcedureID",
    "PerformedProcedureStepID",
    "ScheduledProcedureStepID",
    "DeviceSerialNumber",
    "PatientInsurancePlanCodeSequence",
    # birth date and time
    "PatientBirthTime",
    # addresses and contact details
    "PatientAddress",
    "PatientTelephoneNumbers",
    "CountryOfResidence",
    "RegionOfResidence",
    # institution
    "InstitutionName",
    "InstitutionAddress",
    "InstitutionalDepartmentName",
    "InstitutionCodeSequence",
    "StationName",
    # physicians and staff
    "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "ReferringPhysicianIdentificationSequence",
    "PhysiciansOfRecord",
    "PhysiciansOfRecordIdentificationSequence",
    "PerformingPhysicianName",
    "PerformingPhysicianIdentificationSequence",
    "NameOfPhysiciansReadingStudy",
    "PhysiciansReadingStudyIdentificationSequence",
    "OperatorsName",
    "OperatorIdentificationSequence",
    "RequestingPhysician",
    "ScheduledPerformingPhysicianName",
    # accession numbers
    "IssuerOfAccessionNumberSequence",
    # free text that often carries identity
    "PatientComments",
    "ImageComments",
)
# UIDs replaced with new ones (one consistent mapping per case).
REPLACED_UID_KEYWORDS = (
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "FrameOfReferenceUID",
)
DEIDENTIFICATION_METHOD = "CaviNet FR-04.4: DICOM PS3.15 Basic Profile subset"


def _tags(keywords: tuple[str, ...]) -> frozenset[int]:
    tags = {tag_for_keyword(keyword) for keyword in keywords}
    if None in tags:  # pragma: no cover - guarded by a unit test
        raise RuntimeError("unknown DICOM keyword in the de-identification lists")
    return frozenset(tags)  # type: ignore[arg-type]


_REMOVED_TAGS = _tags(REMOVED_KEYWORDS)
_EMPTIED_TAGS = _tags(EMPTIED_KEYWORDS)


class Deidentifier:
    """Applies FR-04.4 to every slice of one case. UIDs map consistently within the case,
    using a random salt so the originals cannot be recomputed."""

    def __init__(self) -> None:
        self._salt = secrets.token_hex(16)
        self._uids: dict[str, str] = {}

    def _uid(self, original: str) -> str:
        if original not in self._uids:
            self._uids[original] = generate_uid(entropy_srcs=[self._salt, original])
        return self._uids[original]

    def apply(self, ds: Dataset) -> Dataset:
        def scrub(dataset: Dataset, element) -> None:
            if element.tag in _REMOVED_TAGS:
                del dataset[element.tag]
            elif element.tag in _EMPTIED_TAGS:
                element.value = ""

        ds.walk(scrub)
        ds.remove_private_tags()
        for keyword in REPLACED_UID_KEYWORDS:
            if keyword in ds:
                setattr(ds, keyword, self._uid(str(getattr(ds, keyword))))
        if "SOPInstanceUID" in ds and getattr(ds, "file_meta", None) is not None:
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = DEIDENTIFICATION_METHOD
        return ds


# --- Storage and metadata (FR-04.4, FR-04.5) ----------------------------------------------


@dataclass
class ScanMetadata:
    """FR-04.5, plus the series choice (FR-04.3)."""

    num_slices: int
    series_found: int
    series_number: int | None
    series_description: str
    slice_spacing_mm: float
    study_date: date | None
    slice_thickness_mm: float | None
    pixel_spacing_mm: tuple[float, float] | None
    rows: int | None
    columns: int | None
    manufacturer: str | None
    model: str | None
    kernel: str | None


def _sorted_slices(series: Series) -> list[SliceHeader]:
    """Head-to-feet order along the slice normal (highest position first)."""
    normal = _normal(series.slices[0].orientation)  # type: ignore[arg-type]
    return sorted(
        series.slices,
        key=lambda s: -sum(p * n for p, n in zip(s.position, normal, strict=True)),  # type: ignore[arg-type]
    )


DAMAGED_SLICE = (
    "At least one slice of the scan is damaged (its image data is incomplete). "
    "Export the scan again and upload it."
)


def _pixel_data_complete(ds: Dataset) -> bool:
    if "PixelData" not in ds:
        return False
    syntax = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    if syntax is not None and syntax.is_compressed:
        return len(ds.PixelData) > 0  # compressed frames cannot be sized without decoding
    try:
        expected = (
            int(ds.Rows)
            * int(ds.Columns)
            * int(ds.get("SamplesPerPixel", 1))
            * (int(ds.BitsAllocated) // 8)
            * int(ds.get("NumberOfFrames", 1) or 1)
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return len(ds.PixelData) >= expected


def write_deidentified(series: Series, spacing_mm: float, dicom_dir: Path) -> ScanMetadata:
    """De-identify each slice in memory, then write it as dicom_dir/00001.dcm, … in slice
    order. Only de-identified data is ever written here."""
    dicom_dir.mkdir(parents=True, exist_ok=True)
    deidentifier = Deidentifier()
    for number, header in enumerate(_sorted_slices(series), start=1):
        with header.source.open() as fp:
            data = fp.read()
        try:
            ds = _read(io.BytesIO(data), headers_only=False)
        except Exception as error:  # noqa: BLE001 - the header was fine, the rest is not
            raise ScanRejected(DAMAGED_SLICE) from error
        if not _pixel_data_complete(ds):
            raise ScanRejected(DAMAGED_SLICE)
        deidentifier.apply(ds)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds.save_as(dicom_dir / f"{number:05d}.dcm", enforce_file_format=True)

    first = series.slices[0]
    return ScanMetadata(
        num_slices=len(series.slices),
        series_found=series.series_found,
        series_number=series.series_number,
        series_description=series.description,
        slice_spacing_mm=round(spacing_mm, 3),
        study_date=first.study_date,
        slice_thickness_mm=first.thickness_mm,
        pixel_spacing_mm=first.pixel_spacing_mm,
        rows=first.rows,
        columns=first.columns,
        manufacturer=first.manufacturer,
        model=first.model,
        kernel=first.kernel,
    )


def intake(upload: Upload, dicom_dir: Path) -> ScanMetadata:
    """The whole M-04 pipeline for one upload. Raises ScanRejected with the reason."""
    with sources_for(upload) as sources:
        try:
            headers = read_headers(sources)
            series = select_series(headers)
            spacing = validate_series(series)
            return write_deidentified(series, spacing, dicom_dir)
        except (zipfile.BadZipFile, EOFError) as error:
            shutil.rmtree(dicom_dir, ignore_errors=True)
            raise ScanRejected("The .zip file is damaged and could not be read.") from error
        except ScanRejected:
            shutil.rmtree(dicom_dir, ignore_errors=True)  # never keep part of a scan
            raise
