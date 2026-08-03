"""run_passport() -- Config wiring only (source=TARGET, mode="analyze"), mocked run_analyze()
so this doesn't need real exiftool/bin binaries -- the real end-to-end pipeline is covered by
ci/windows_ci_test.py::test_passport_report_on_real_archive (needs bin/, not runnable here)."""
import photosort_win as m


def test_run_passport_points_cfg_source_at_target(monkeypatch, tmp_path):
    target = str(tmp_path / "MyArchive")
    seen = {}

    def _fake_run_analyze(cfg, mode, log=print, self_scan=False):
        seen["cfg"] = cfg
        seen["mode"] = mode
        seen["self_scan"] = self_scan
        return "STATS"

    monkeypatch.setattr(m, "run_analyze", _fake_run_analyze)
    result = m.run_passport(target, log=lambda *a, **k: None)

    assert result == "STATS"
    assert seen["mode"] == "analyze"
    # Живой репорт пользователя, 2026-08-01: паспорт сканирует собственный TARGET, не "сырой"
    # SOURCE -- run_analyze() должен знать об этом (см. self_scan докстринг в photosort_win.py).
    assert seen["self_scan"] is True
    cfg = seen["cfg"]
    assert cfg.source == m.os.path.abspath(target)
    assert cfg.target != cfg.source
    assert cfg.sample_limit == 0


def test_run_passport_placeholder_never_collides_with_a_real_target(tmp_path):
    """Config.__post_init__ requires source != target -- run_passport() uses
    _NO_TARGET_PLACEHOLDER as a stand-in cfg.target since mode="analyze" never reads it (only
    "analyze-full" does). Confirm the placeholder itself would never accidentally equal a real
    archive path passed in as `target` (the actual TARGET being checked)."""
    assert m._NO_TARGET_PLACEHOLDER != str(tmp_path)
