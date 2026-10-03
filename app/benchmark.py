"""`make benchmark`: time the full analysis of a synthetic scan on this computer's CPU
(NFR-1 / section 11.8: at most 3 minutes per scan on a 4-core laptop).

    python -m app.benchmark --slices 300

It generates a synthetic chest CT (no patient data), then runs exactly what the worker runs:
DICOM loading, lung segmentation, preprocessing, previews and the installed model's
ensemble. Nothing is stored in the database.
"""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from app.services import model_store
from app.synthetic_dicom import SyntheticStudy
from cavinet_ml import hide_library_notices

BUDGET_SECONDS = 180


def run(slices: int, size: int) -> dict:
    model_store.configure_threads()
    with tempfile.TemporaryDirectory() as folder:
        dicom = Path(folder) / "dicom"
        dicom.mkdir()
        for index, data in enumerate(
            SyntheticStudy("benchmark").series(slices, rows=size, columns=size, spacing_mm=1.0)
        ):
            (dicom / f"{index:05d}.dcm").write_bytes(data)
        started = time.perf_counter()
        analyser = model_store.get_analyser()
        loaded = time.perf_counter() - started
        prepared = analyser.prepare(dicom, Path(folder) / "previews")
        result = analyser.infer(prepared)
        total = time.perf_counter() - started
    return {
        "slices": slices,
        "matrix": f"{size}x{size}",
        "cpu_threads": int(os.environ.get("TORCH_NUM_THREADS") or (os.cpu_count() or 1)),
        "model": result.model["name"],
        "is_demo": result.model["is_demo"],
        "model_load_seconds": round(loaded, 1),
        "step_seconds": result.seconds,
        "total_seconds": round(total, 1),
        "budget_seconds": BUDGET_SECONDS,
        "within_budget": total <= BUDGET_SECONDS,
        "lung_volume_ml": result.lung_volume_ml,
        "warnings": result.warnings,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slices", type=int, default=300)
    parser.add_argument("--size", type=int, default=512)
    args = parser.parse_args(argv)
    hide_library_notices()
    print(json.dumps(run(args.slices, args.size), indent=2))


if __name__ == "__main__":
    main()
