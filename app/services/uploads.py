"""Receive an upload by streaming it straight to a staging folder (FR-04.1, NFR-1).

The request body (multipart/form-data) is parsed chunk by chunk and each file part is
written to DATA_DIR/staging/<id>/ as it arrives, so a 1.5 GB upload never sits in memory or
in a second temporary copy. File names are only used for their extension; they are never
stored or logged (they can contain patient names).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from app.services.dicom_intake import Upload
from app.services.errors import InvalidInput, PayloadTooLarge, UnsupportedMediaType

# Allowance for multipart boundaries and headers on top of the file bytes.
FORM_OVERHEAD_BYTES = 16 * 1024 * 1024


def _size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def too_large(max_bytes: int) -> PayloadTooLarge:
    limit = _size(max_bytes)
    return PayloadTooLarge(
        f"The upload is larger than the {limit} limit. Upload only the chest CT series, "
        "or compress it as a .zip."
    )


@dataclass
class _Part:
    headers: dict[bytes, bytes] = field(default_factory=dict)
    field_name: bytes = b""
    field_value: bytes = b""


@dataclass
class StagedUpload:
    upload: Upload
    files: int
    total_bytes: int


class _Receiver:
    def __init__(self, staging: Path, max_bytes: int) -> None:
        self.staging = staging
        self.max_bytes = max_bytes
        self.total = 0
        self.zips: list[Path] = []
        self.others: list[Path] = []
        self._part = _Part()
        self._file: BinaryIO | None = None

    # python-multipart callbacks
    def on_part_begin(self) -> None:
        self._part = _Part()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._part.field_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._part.field_value += data[start:end]

    def on_header_end(self) -> None:
        self._part.headers[self._part.field_name.lower()] = self._part.field_value
        self._part.field_name = self._part.field_value = b""

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._part.headers.get(b"content-disposition", b""))
        filename = params.get(b"filename")
        if filename is None:
            return  # an ordinary form field: ignored
        is_zip = filename.decode("utf-8", "replace").lower().endswith(".zip")
        count = len(self.zips) + len(self.others) + 1
        path = self.staging / f"{count:05d}{'.zip' if is_zip else '.dcm'}"
        (self.zips if is_zip else self.others).append(path)
        self._file = path.open("wb")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._file is None:
            return
        self.total += end - start
        if self.total > self.max_bytes:
            raise too_large(self.max_bytes)
        self._file.write(data[start:end])

    def on_part_end(self) -> None:
        self.close()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


async def receive(request: Request, staging: Path, max_bytes: int) -> StagedUpload:
    """Stream the files of a multipart upload into `staging`. Checks the request shape:
    one .zip, or one or more other (DICOM) files, within the size limit."""
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UnsupportedMediaType("Send the scan as a file upload (multipart/form-data).")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + FORM_OVERHEAD_BYTES:
        raise too_large(max_bytes)

    receiver = _Receiver(staging, max_bytes)
    callbacks = {
        name: getattr(receiver, name)
        for name in (
            "on_part_begin",
            "on_header_field",
            "on_header_value",
            "on_header_end",
            "on_headers_finished",
            "on_part_data",
            "on_part_end",
        )
    }
    parser = MultipartParser(boundary, callbacks)
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError:
        raise InvalidInput(
            "The upload was incomplete or damaged. Please try again.", code="bad_upload"
        ) from None
    finally:
        receiver.close()

    if not receiver.zips and not receiver.others:
        raise InvalidInput(
            "Choose a .zip file or the scan's .dcm files to upload.", code="no_files"
        )
    if receiver.zips and receiver.others:
        raise InvalidInput(
            "Upload either one .zip file or the scan's .dcm files, not both.",
            code="mixed_upload",
        )
    if len(receiver.zips) > 1:
        raise InvalidInput("Upload one .zip file at a time.", code="several_zips")
    paths = receiver.zips or receiver.others
    kind = "zip" if receiver.zips else "dcm"
    return StagedUpload(Upload(kind, paths), files=len(paths), total_bytes=receiver.total)
