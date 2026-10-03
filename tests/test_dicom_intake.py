"""M-04 scan intake: FR-04.2 validation, FR-04.3 series choice, FR-04.4 de-identification,
FR-04.5 metadata. All scans are synthetic (app.synthetic_dicom)."""

import io
import zipfile
from datetime import date
from pathlib import Path

import pydicom
import pytest
from pydicom.datadict import tag_for_keyword

from app.services import dicom_intake
from app.services.dicom_intake import (
    EMPTIED_KEYWORDS,
    REMOVED_KEYWORDS,
    REPLACED_UID_KEYWORDS,
    ScanRejected,
    Upload,
    intake,
)
from app.synthetic_dicom import (
    AXIAL,
    CORONAL,
    IDENTIFYING_VALUES,
    PRIVATE_CREATOR,
    PRIVATE_VALUE,
    SyntheticStudy,
    encode,
    zip_files,
)


def run_zip(tmp_path: Path, data: bytes):
    (tmp_path / "upload.zip").write_bytes(data)
    out = tmp_path / "case" / "dicom"
    return intake(Upload("zip", [tmp_path / "upload.zip"]), out), out


def run_files(tmp_path: Path, files: list[bytes]):
    paths = []
    for index, data in enumerate(files):
        path = tmp_path / f"{index:05d}.dcm"
        path.write_bytes(data)
        paths.append(path)
    out = tmp_path / "case" / "dicom"
    return intake(Upload("dcm", paths), out), out


def rejected(tmp_path: Path, data: bytes) -> str:
    with pytest.raises(ScanRejected) as caught:
        run_zip(tmp_path, data)
    assert not (tmp_path / "case" / "dicom").exists() or not any(
        (tmp_path / "case" / "dicom").iterdir()
    ), "nothing may be stored for a rejected scan"
    return caught.value.reason


def series(slices: int = 60, **kwargs) -> list[bytes]:
    return SyntheticStudy("unit").series(slices, **kwargs)


# --- Valid uploads ------------------------------------------------------------------------


def test_fr04_1_valid_zip_is_accepted_and_stored_in_slice_order(tmp_path):
    scan, out = run_zip(tmp_path, zip_files(series(60)))
    stored = sorted(out.iterdir())
    assert [p.name for p in stored[:2]] == ["00001.dcm", "00002.dcm"]
    assert len(stored) == 60 == scan.num_slices
    positions = [float(pydicom.dcmread(p).ImagePositionPatient[2]) for p in stored]
    assert positions == sorted(positions, reverse=True)  # head to feet


def test_fr04_1_valid_dcm_files_are_accepted(tmp_path):
    scan, out = run_files(tmp_path, series(55))
    assert scan.num_slices == 55
    assert len(list(out.glob("*.dcm"))) == 55


def test_fr04_1_zip_with_nested_folders_and_os_junk_is_accepted(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("EXPORT/", b"")
        for index, data in enumerate(series(50)):
            archive.writestr(f"EXPORT/STUDY/SERIES/{index}", data)
        archive.writestr("EXPORT/DICOMDIR", b"not parsed")
        archive.writestr("__MACOSX/EXPORT/._0", b"resource fork")
        archive.writestr("EXPORT/.DS_Store", b"finder")
        archive.writestr("EXPORT/Thumbs.db", b"thumbnails")
    scan, _ = run_zip(tmp_path, buffer.getvalue())
    assert scan.num_slices == 50


def test_fr04_2_exactly_50_slices_and_exactly_5_mm_are_accepted(tmp_path):
    scan, _ = run_zip(tmp_path, zip_files(series(50, spacing_mm=5.0)))
    assert (scan.num_slices, scan.slice_spacing_mm) == (50, 5.0)


def test_fr04_2_slightly_tilted_axial_slices_are_accepted(tmp_path):
    tilted = (1.0, 0.0, 0.0, 0.0, 0.9659, -0.2588)  # 15 degrees of gantry tilt
    scan, _ = run_zip(tmp_path, zip_files(series(60, orientation=tilted)))
    assert scan.num_slices == 60


# --- FR-04.2: each rule rejects bad input with the documented reason ----------------------


def test_fr04_2_non_dicom_files_are_rejected(tmp_path):
    files = [*series(60), b"%PDF-1.4 a report", b"plain text notes"]
    reason = rejected(tmp_path, zip_files(files))
    assert reason == (
        "2 of the 62 uploaded files are not DICOM (or damaged). Upload only the CT scan's "
        "DICOM (.dcm) files, or a .zip that contains them."
    )


def test_fr04_2_a_single_non_dicom_file_is_reported_in_the_singular(tmp_path):
    with pytest.raises(ScanRejected, match=r"^1 of the 1 uploaded file is not DICOM"):
        run_files(tmp_path, [b"hello"])


def test_fr04_2_dicom_truncated_in_the_header_is_rejected(tmp_path):
    files = series(60)
    files[10] = files[10][:200]
    assert "1 of the 60 uploaded file is not DICOM (or damaged)" in rejected(
        tmp_path, zip_files(files)
    )


def test_fr04_2_dicom_truncated_in_the_image_data_is_rejected(tmp_path):
    files = series(60)
    files[10] = files[10][:-500]
    assert rejected(tmp_path, zip_files(files)) == (
        "At least one slice of the scan is damaged (its image data is incomplete). "
        "Export the scan again and upload it."
    )


def test_fr04_2_non_ct_modality_is_rejected(tmp_path):
    assert rejected(tmp_path, zip_files(series(60, modality="MR"))) == (
        "The selected series is an MRI scan, not a CT scan. CaviNet analyses chest CT scans only."
    )


def test_fr04_2_unknown_modality_is_rejected_by_its_code(tmp_path):
    reason = rejected(tmp_path, zip_files(series(60, modality="OT")))
    assert reason.startswith("The selected series is a OT scan, not a CT scan.")


def test_fr04_2_missing_modality_is_rejected(tmp_path):
    files = series(60, customize=lambda i, ds: delattr(ds, "Modality"))
    assert rejected(tmp_path, zip_files(files)).startswith(
        "The scan does not say what kind of scan it is (no modality)."
    )


def test_fr04_2_non_axial_orientation_is_rejected(tmp_path):
    assert rejected(tmp_path, zip_files(series(60, orientation=CORONAL))) == (
        "The slices are not axial (cross-sections of the body). "
        "Upload the axial series of the chest CT."
    )


def test_fr04_2_missing_orientation_is_rejected(tmp_path):
    files = series(60, customize=lambda i, ds: delattr(ds, "ImageOrientationPatient"))
    assert "no orientation information" in rejected(tmp_path, zip_files(files))


def test_fr04_2_fewer_than_50_slices_is_rejected(tmp_path):
    assert rejected(tmp_path, zip_files(series(49))) == (
        "The scan has 49 slices; at least 50 are needed. Upload the full chest CT series."
    )


def test_fr04_2_inconsistent_rows_and_columns_are_rejected(tmp_path):
    study = SyntheticStudy("sizes")

    def shrink(index, ds):
        if index == 7:
            other = study.series(1, rows=32, columns=48)[0]
            small = pydicom.dcmread(io.BytesIO(other))
            ds.Rows, ds.Columns, ds.PixelData = small.Rows, small.Columns, small.PixelData

    assert rejected(tmp_path, zip_files(study.series(60, customize=shrink))) == (
        "The slices do not all have the same size (32×48 and 64×64 pixels). "
        "All slices of the scan must have the same number of rows and columns."
    )


def test_fr04_2_slice_spacing_over_5_mm_is_rejected(tmp_path):
    assert rejected(tmp_path, zip_files(series(60, spacing_mm=5.5))) == (
        "The slices are 5.5 mm apart; the maximum is 5 mm. "
        "Upload a thinner-slice reconstruction of the scan."
    )


def test_fr04_2_missing_positions_are_rejected(tmp_path):
    files = series(60, customize=lambda i, ds: delattr(ds, "ImagePositionPatient"))
    assert rejected(tmp_path, zip_files(files)) == (
        "The slices have no position information, so the slice spacing cannot be checked."
    )


def test_fr04_2_identical_positions_are_rejected(tmp_path):
    def same(index, ds):
        ds.ImagePositionPatient = ["-180", "-180", "0"]

    assert "All slices have the same position" in rejected(
        tmp_path, zip_files(series(60, customize=same))
    )


def test_fr04_2_rules_are_checked_in_the_order_listed(tmp_path):
    """An MR series that is also too short is reported as the wrong modality first."""
    assert "not a CT scan" in rejected(tmp_path, zip_files(series(10, modality="MR")))


def test_upload_without_images_is_rejected(tmp_path):
    def no_image(index, ds):
        for keyword in ("Rows", "Columns", "PixelData"):
            delattr(ds, keyword)

    assert rejected(tmp_path, zip_files(series(3, customize=no_image))) == (
        "No DICOM images were found in the upload."
    )


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"PK\x03\x04 this is not really a zip", "The .zip file is damaged or is not a zip file."),
        (b"just text", "The .zip file is damaged or is not a zip file."),
    ],
)
def test_damaged_zip_is_rejected(tmp_path, data, reason):
    assert rejected(tmp_path, data) == reason


def test_zip_with_only_ignored_files_is_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__MACOSX/._x", b"x")
        archive.writestr("folder/", b"")
    assert rejected(tmp_path, buffer.getvalue()) == "The .zip file contains no DICOM files."


def test_password_protected_zip_is_rejected(tmp_path):
    data = bytearray(zip_files(series(50)))
    # Set the "encrypted" flag on the first entry (local header and central directory).
    data[6] |= 0x1
    central = data.find(b"PK\x01\x02")
    data[central + 8] |= 0x1
    assert rejected(tmp_path, bytes(data)) == (
        "The .zip file is password-protected. Upload an unprotected .zip."
    )


def test_zip_bombs_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(dicom_intake, "MAX_ZIP_ENTRIES", 10)
    assert "more than 10 files" in rejected(tmp_path, zip_files(series(50)))
    monkeypatch.setattr(dicom_intake, "MAX_ZIP_ENTRIES", 20_000)
    monkeypatch.setattr(dicom_intake, "MAX_UNZIPPED_BYTES", 1000)
    assert "expands to more than 8 GB" in rejected(tmp_path, zip_files(series(50)))


def test_corrupt_zip_member_is_rejected_cleanly(tmp_path):
    data = bytearray(zip_files(series(50)))
    central = data.find(b"PK\x01\x02")
    data[central - 40 : central - 20] = b"\x00" * 20  # damage the last member's data
    reason = rejected(tmp_path, bytes(data))
    assert "damaged" in reason


# --- FR-04.3: several series ---------------------------------------------------------------


def test_fr04_3_the_series_with_the_most_slices_is_used(tmp_path):
    study = SyntheticStudy("multi")
    files = [
        *study.series(55, series_number=3),
        *study.series(80, series_number=4, spacing_mm=1.0),
        *study.series(2, series_number=1, orientation=CORONAL),  # localizer
    ]
    scan, out = run_zip(tmp_path, zip_files(files))
    assert (scan.num_slices, scan.series_number, scan.series_found) == (80, 4, 3)
    assert scan.slice_spacing_mm == 1.0
    assert scan.series_description == (
        "3 image series found; used series 4 with the most slices (80; others: 55, 2)."
    )
    assert len(list(out.glob("*.dcm"))) == 80  # only the chosen series is kept
    numbers = {pydicom.dcmread(p).SeriesNumber for p in out.iterdir()}
    assert numbers == {4}


def test_fr04_3_the_largest_series_is_used_even_if_it_fails_a_rule(tmp_path):
    """The rule picks first and validates second, exactly as written."""
    study = SyntheticStudy("multi")
    files = [*study.series(60, series_number=2), *study.series(70, series_number=5, modality="MR")]
    assert "an MRI scan" in rejected(tmp_path, zip_files(files))


def test_fr04_3_ties_go_to_the_lower_series_number(tmp_path):
    study = SyntheticStudy("tie")
    files = [*study.series(60, series_number=7), *study.series(60, series_number=3)]
    scan, _ = run_zip(tmp_path, zip_files(files))
    assert scan.series_number == 3


def test_fr04_3_single_series_description(tmp_path):
    scan, _ = run_zip(tmp_path, zip_files(series(50)))
    assert scan.series_description == "One image series found (50 slices)."


# --- FR-04.4: de-identification ------------------------------------------------------------


def test_fr04_4_deidentification_keyword_lists_are_valid_dicom():
    for keyword in (*REMOVED_KEYWORDS, *EMPTIED_KEYWORDS, *REPLACED_UID_KEYWORDS):
        assert tag_for_keyword(keyword) is not None, keyword


def test_fr04_4_stored_files_contain_none_of_the_removed_tags(tmp_path):
    def nested(index, ds):
        # Identifiers can also hide inside sequences.
        item = pydicom.Dataset()
        item.AccessionNumber = "SYN-NESTED-ACC"
        item.PatientName = "SYNTHETIC^NESTED"
        item.InstitutionName = "Nested Hospital"
        ds.RequestAttributesSequence = [item]
        ds.PatientComments = "SYNTHETIC comments naming the patient"

    originals = SyntheticStudy("deid").series(50, customize=nested)
    original = pydicom.dcmread(io.BytesIO(originals[0]))
    _, out = run_zip(tmp_path, zip_files(originals))

    secrets = [
        *IDENTIFYING_VALUES.values(),
        PRIVATE_VALUE,
        PRIVATE_CREATOR,
        "SYN-NESTED-ACC",
        "SYNTHETIC^NESTED",
        "Nested Hospital",
        "SYNTHETIC comments",
        "SYN-STUDY-1",
        original.StudyInstanceUID,
        original.SeriesInstanceUID,
        original.FrameOfReferenceUID,
    ]
    for path in out.iterdir():
        raw = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in raw, (path.name, secret)
        ds = pydicom.dcmread(path)
        for keyword in REMOVED_KEYWORDS:
            assert keyword not in ds
            for item in ds.get("RequestAttributesSequence", []):
                assert keyword not in item
        for keyword in EMPTIED_KEYWORDS:
            assert ds.get(keyword) in ("", None), keyword
        assert not any(element.tag.is_private for element in ds)
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.DeidentificationMethod.startswith("CaviNet FR-04.4")
        assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID


def test_fr04_4_scan_content_needed_for_analysis_is_kept(tmp_path):
    originals = series(50)
    _, out = run_zip(tmp_path, zip_files(originals))
    first = pydicom.dcmread(sorted(out.iterdir())[0])
    source = pydicom.dcmread(io.BytesIO(originals[0]))
    assert first.PixelData == source.PixelData
    assert first.Modality == "CT"
    assert first.PatientSex == "F" and first.StudyDate == "20260915"
    assert list(first.ImageOrientationPatient) == list(AXIAL)
    assert first.RescaleIntercept == -1024


def test_fr04_4_new_uids_are_consistent_within_a_case_and_differ_between_cases(tmp_path):
    files = zip_files(series(50))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, first_out = run_zip(tmp_path / "a", files)
    _, second_out = run_zip(tmp_path / "b", files)
    first = [pydicom.dcmread(p) for p in sorted(first_out.iterdir())]
    second = pydicom.dcmread(sorted(second_out.iterdir())[0])
    assert len({ds.SeriesInstanceUID for ds in first}) == 1
    assert len({ds.StudyInstanceUID for ds in first}) == 1
    assert len({ds.SOPInstanceUID for ds in first}) == 50
    assert second.SeriesInstanceUID != first[0].SeriesInstanceUID


def test_fr04_4_dcm_uploads_are_deidentified_too(tmp_path):
    _, out = run_files(tmp_path, series(50))
    for path in out.iterdir():
        assert b"SYNTHETIC^TESTPATIENT" not in path.read_bytes()


# --- FR-04.5: metadata ---------------------------------------------------------------------


def test_fr04_5_scan_metadata_is_extracted(tmp_path):
    scan, _ = run_zip(tmp_path, zip_files(series(64, spacing_mm=1.25, thickness_mm=1.0)))
    assert scan.study_date == date(2026, 9, 15)
    assert scan.num_slices == 64
    assert scan.slice_thickness_mm == 1.0
    assert scan.slice_spacing_mm == 1.25
    assert scan.pixel_spacing_mm == (0.7, 0.7)
    assert (scan.rows, scan.columns) == (64, 64)
    assert scan.manufacturer == "SYNTHETIC"
    assert scan.model == "CaviNet Synthetic CT"
    assert scan.kernel == "STANDARD"


def test_fr04_5_missing_or_odd_metadata_is_left_empty(tmp_path):
    def odd(index, ds):
        ds.StudyDate = "2026"
        ds.ConvolutionKernel = ["FC13", "AIDR"]
        del ds.Manufacturer
        del ds.SliceThickness

    scan, _ = run_zip(tmp_path, zip_files(series(50, customize=odd)))
    assert scan.study_date is None
    assert scan.kernel == "FC13\\AIDR"
    assert scan.manufacturer is None
    assert scan.slice_thickness_mm is None


def test_synthetic_files_are_valid_part_10_dicom():
    data = encode(pydicom.dcmread(io.BytesIO(series(1)[0])))
    assert data[128:132] == b"DICM"


def test_demo_scan_command_writes_a_valid_two_series_zip(tmp_path, capsysbinary):
    from app import synthetic_dicom

    out = tmp_path / "demo.zip"
    synthetic_dicom.main(["--slices", "60", "--size", "32", "--out", str(out)])
    scan, _ = run_zip(tmp_path, out.read_bytes())
    assert (scan.num_slices, scan.series_found, scan.rows) == (60, 2, 32)

    synthetic_dicom.main(["--slices", "50", "--size", "16"])
    assert zipfile.is_zipfile(io.BytesIO(capsysbinary.readouterr().out))
