"""run_passport() -- Config wiring only (source=TARGET, mode="analyze"), mocked run_analyze()
so this doesn't need real exiftool/bin binaries -- the real end-to-end pipeline is covered by
ci/windows_ci_test.py::test_passport_report_on_real_archive (needs bin/, not runnable here)."""
import photosort_win as m


def test_run_passport_points_cfg_source_at_target(monkeypatch, tmp_path):
    target_path = tmp_path / "MyArchive"
    target_path.mkdir()
    # 2026-08-24: run_passport() теперь жёстко требует реальный маркер архива (живая просьба
    # пользователя) -- этот тест не про сам гейт, просто отмечаем папку архивом.
    (target_path / "__служебные_файлы").mkdir()
    target = str(target_path)
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


class TestRunPassportRefusesNonArchiveTarget:
    """Живая просьба пользователя, 2026-08-24: "если паспорт пытаются сделать на что угодно,
    кроме архива, не стартовать". Раньше run_passport() было готово self-scan'ить ЛЮБУЮ папку
    (Desktop/Downloads/что угодно) -- всё просто попало бы в "не внутри альбома/даты", без
    единого предупреждения, что это вообще не архив. Гейт добавлен в саму run_passport() --
    единственную точку входа для всех трёх вызывающих (меню [4], CLI "analyze --target",
    GUI-мастер), не дублируется в каждом отдельно (GUI и так уже блокирует "Далее" тем же
    _target_has_existing_archive() через _describe_passport_target() — здесь это для двух
    остальных путей, defense-in-depth для GUI)."""

    def test_missing_directory_refused(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "run_analyze", lambda *a, **k: calls.append(1) or "STATS")
        result = m.run_passport(str(tmp_path / "does_not_exist"), log=lambda *a, **k: None)
        assert result is None
        assert calls == []  # не должно даже пытаться сканировать

    def test_plain_folder_without_archive_markers_refused(self, tmp_path, monkeypatch):
        (tmp_path / "photo.jpg").write_bytes(b"x")
        calls = []
        monkeypatch.setattr(m, "run_analyze", lambda *a, **k: calls.append(1) or "STATS")
        result = m.run_passport(str(tmp_path), log=lambda *a, **k: None)
        assert result is None
        assert calls == []

    def test_error_message_logged(self, tmp_path):
        logged = []
        m.run_passport(str(tmp_path / "not_an_archive"), log=lambda msg: logged.append(msg))
        assert any("не похож на архив" in msg for msg in logged)

    def test_folder_with_service_dir_marker_accepted(self, tmp_path, monkeypatch):
        (tmp_path / "__служебные_файлы").mkdir()
        monkeypatch.setattr(m, "run_analyze", lambda *a, **k: "STATS")
        result = m.run_passport(str(tmp_path), log=lambda *a, **k: None)
        assert result == "STATS"

    def test_folder_with_albums_and_bydate_marker_accepted(self, tmp_path, monkeypatch):
        """Более старый архив/ручное дерево без __служебные_файлы -- та же сигнатура, что уже
        использует _target_has_existing_archive() для подменю выбора диска."""
        (tmp_path / "Albums").mkdir()
        (tmp_path / "ByDate").mkdir()
        monkeypatch.setattr(m, "run_analyze", lambda *a, **k: "STATS")
        result = m.run_passport(str(tmp_path), log=lambda *a, **k: None)
        assert result == "STATS"
