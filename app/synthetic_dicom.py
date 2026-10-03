"""Synthetic chest-CT DICOM series for tests and demonstrations. No real patient data.

The files carry made-up identifying values (patient name, IDs, birth date, institution,
physicians, accession number and a private tag) so tests can prove de-identification
removes them (FR-04.4).

Command line (writes a demo .zip to stdout or a file):

    python -m app.synthetic_dicom > synthetic_ct.zip
    python -m app.synthetic_dicom --slices 30 --out too_short.zip
"""

import argparse
import io
import sys
import zipfile
from array import array
from collections.abc import Callable, Iterable

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
CORONAL = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0)

# Made-up identifying values written into every synthetic file.
IDENTIFYING_VALUES = {
    "PatientName": "SYNTHETIC^TESTPATIENT",
    "PatientID": "SYN-MRN-0042",
    "PatientBirthDate": "19700101",
    "PatientAddress": "1 Synthetic Street, Test City",
    "PatientTelephoneNumbers": "+92-000-0000000",
    "OtherPatientIDs": "SYN-OTHER-ID",
    "InstitutionName": "Synthetic General Hospital",
    "InstitutionAddress": "2 Hospital Road, Test City",
    "ReferringPhysicianName": "SYNTHETIC^REFERRER",
    "PerformingPhysicianName": "SYNTHETIC^PERFORMER",
    "OperatorsName": "SYNTHETIC^OPERATOR",
    "AccessionNumber": "SYN-ACC-0042",
    "StationName": "SYNTH-CT-01",
    "DeviceSerialNumber": "SYN-SERIAL-9",
}
PRIVATE_CREATOR = "SYNTHETIC CAVINET"
PRIVATE_VALUE = "SYN-PRIVATE-SECRET"

Customize = Callable[[int, Dataset], None]


def _pixels(rows: int, columns: int) -> bytes:
    """A crude chest: air outside, soft tissue body, two darker lungs (HU + 1024)."""
    values = array("h")
    for r in range(rows):
        y = (r + 0.5) / rows * 2 - 1
        for c in range(columns):
            x = (c + 0.5) / columns * 2 - 1
            hu = -1000
            if (x / 0.9) ** 2 + (y / 0.7) ** 2 <= 1:
                hu = 40
                if ((abs(x) - 0.4) / 0.25) ** 2 + (y / 0.45) ** 2 <= 1:
                    hu = -850
            values.append(hu + 1024)  # stored value; RescaleIntercept -1024 gives HU
    return values.tobytes()


class SyntheticStudy:
    """One synthetic study; `series()` adds a series of slices to it."""

    def __init__(self, seed: str = "cavinet") -> None:
        self.seed = seed
        self.study_uid = generate_uid(entropy_srcs=[seed, "study"])
        self.frame_uid = generate_uid(entropy_srcs=[seed, "frame"])
        self._pixel_cache: dict[tuple[int, int], bytes] = {}

    def series(
        self,
        slices: int = 60,
        *,
        series_number: int = 2,
        rows: int = 64,
        columns: int = 64,
        modality: str = "CT",
        spacing_mm: float = 1.25,
        thickness_mm: float | None = None,
        orientation: tuple[float, ...] = AXIAL,
        customize: Customize | None = None,
    ) -> list[bytes]:
        """Encoded DICOM files (Part 10, uncompressed), one per slice."""
        series_uid = generate_uid(entropy_srcs=[self.seed, "series", str(series_number)])
        files = []
        for index in range(slices):
            ds = self._slice(
                index,
                series_uid=series_uid,
                series_number=series_number,
                rows=rows,
                columns=columns,
                modality=modality,
                spacing_mm=spacing_mm,
                thickness_mm=spacing_mm if thickness_mm is None else thickness_mm,
                orientation=orientation,
            )
            if customize:
                customize(index, ds)
            files.append(encode(ds))
        return files

    def _slice(
        self,
        index: int,
        *,
        series_uid: str,
        series_number: int,
        rows: int,
        columns: int,
        modality: str,
        spacing_mm: float,
        thickness_mm: float,
        orientation: tuple[float, ...],
    ) -> Dataset:
        sop_uid = generate_uid(entropy_srcs=[series_uid, str(index)])
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = Dataset()
        ds.file_meta = meta
        for keyword, value in IDENTIFYING_VALUES.items():
            setattr(ds, keyword, value)
        ds.PatientSex = "F"
        ds.PatientAge = "056Y"
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = self.study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = self.frame_uid
        ds.StudyDate = "20260915"
        ds.SeriesDate = "20260915"
        ds.StudyTime = "101500"
        ds.StudyID = "SYN-STUDY-1"
        ds.Modality = modality
        ds.Manufacturer = "SYNTHETIC"
        ds.ManufacturerModelName = "CaviNet Synthetic CT"
        ds.ConvolutionKernel = "STANDARD"
        ds.SeriesNumber = series_number
        ds.InstanceNumber = index + 1
        ds.SliceThickness = f"{thickness_mm:g}"
        ds.PixelSpacing = ["0.7", "0.7"]
        ds.ImageOrientationPatient = [f"{v:g}" for v in orientation]
        # Positions advance along the slice normal.
        normal = _cross(orientation[:3], orientation[3:])
        offset = -index * spacing_mm
        origin = (-180.0, -180.0, 0.0)
        ds.ImagePositionPatient = [
            f"{o + n * offset:g}" for o, n in zip(origin, normal, strict=True)
        ]
        ds.SliceLocation = f"{offset:g}"
        ds.Rows = rows
        ds.Columns = columns
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.RescaleIntercept = "-1024"
        ds.RescaleSlope = "1"
        key = (rows, columns)
        if key not in self._pixel_cache:
            self._pixel_cache[key] = _pixels(rows, columns)
        ds.PixelData = self._pixel_cache[key]
        block = ds.private_block(0x0029, PRIVATE_CREATOR, create=True)
        block.add_new(0x10, "LO", PRIVATE_VALUE)
        return ds


def _cross(a: Iterable[float], b: Iterable[float]) -> tuple[float, float, float]:
    ax, ay, az = a
    bx, by, bz = b
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def encode(ds: Dataset) -> bytes:
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def zip_files(files: Iterable[bytes], folder: str = "SYNTHETIC_CT") -> bytes:
    """A .zip with the files in a nested folder, as PACS exports often do."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, data in enumerate(files, start=1):
            archive.writestr(f"{folder}/series/IM{index:05d}", data)
    return buffer.getvalue()


def demo_scan(slices: int = 120, size: int = 128) -> bytes:
    """A demo upload: an axial chest series plus a 3-slice localizer (the axial series has
    the most slices, so it is the one used)."""
    study = SyntheticStudy("demo")
    axial = study.series(slices, series_number=2, rows=size, columns=size, spacing_mm=2.5)
    localizer = study.series(3, series_number=1, rows=size, columns=size, orientation=CORONAL)
    return zip_files([*localizer, *axial])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write a synthetic chest CT as a .zip.")
    parser.add_argument("--slices", type=int, default=120, help="axial slices (default 120)")
    parser.add_argument("--size", type=int, default=128, help="rows and columns (default 128)")
    parser.add_argument("--out", help="output file (default: standard output)")
    args = parser.parse_args(argv)
    data = demo_scan(args.slices, args.size)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(data)
    else:
        sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()


__all__ = [
    "AXIAL",
    "CORONAL",
    "IDENTIFYING_VALUES",
    "PRIVATE_CREATOR",
    "PRIVATE_VALUE",
    "SyntheticStudy",
    "demo_scan",
    "encode",
    "zip_files",
]
