from app.seed import main


def test_seed_is_a_safe_no_op_in_phase_1(capsys):
    assert main() == 0
    assert "Phase 2" in capsys.readouterr().out
