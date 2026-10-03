"""`make benchmark` (NFR-1, section 11.8): the timing script runs the worker's pipeline."""

from app import benchmark


def test_benchmark_reports_the_timings_of_a_synthetic_scan(capsys):
    report = benchmark.run(slices=50, size=32)
    assert report["slices"] == 50 and report["matrix"] == "32x32"
    assert report["is_demo"] is True
    assert set(report["step_seconds"]) >= {"load", "lung_mask", "inference", "total"}
    assert report["total_seconds"] > 0 and report["budget_seconds"] == 180
    assert report["within_budget"] is True

    benchmark.main(["--slices", "50", "--size", "16"])
    assert '"total_seconds"' in capsys.readouterr().out
