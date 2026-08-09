"""2026-08-08, живой боевой прогон F:->D: -- ветка raw_mirrored (_process_record(), одинокий/
новый RAW без JPEG-партнёра) реально дописывает файл в альбом наравне с skipped_present/
image-video-веткой. Альбомный редизайн той же даты убрал stats["source_album_seen"]/
"source_album_appended" и связанную секцию report.py "Уже было в архиве" целиком (см. RULES.md)
-- этот тест теперь проверяет сам физический результат (файл реально лежит в альбоме), не
устаревшую статистику по имени."""
import os

import photosort_win as m

RAW_BYTES = b"raw" * 100


def _stub_exiftool(monkeypatch, tags_by_path=None):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: (tags_by_path or {}).get(p, {}) for p in paths})


def test_raw_mirrored_album_file_appended_despite_sibling_duplicate(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    album = source / "AlbumX"
    album.mkdir(parents=True)
    # Одинокий (без JPEG-партнёра) RAW -- decide() всегда отдаёт "raw_mirrored" для него.
    (album / "a.cr2").write_bytes(RAW_BYTES)
    # Байт-идентичная копия того же кадра -- decide() для RAW тоже проверяет
    # pool.find_exact(sha256) первым делом (см. decide()), находит запись, оставленную
    # первым файлом -> "skipped_present".
    (album / "a_copy.cr2").write_bytes(RAW_BYTES)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = m.run(cfg, log=lambda *a, **k: None)

    assert stats["raw_mirrored"] == 1  # реально дописанный уникальный RAW
    assert stats["skipped_present"] == 1  # байт-идентичная копия -- дубль, не копируется повторно
    assert (target / "RAW" / "Albums" / "AlbumX" / "a.cr2").read_bytes() == RAW_BYTES
    assert not (target / "RAW" / "Albums" / "AlbumX" / "a_copy.cr2").exists()


def test_raw_mirrored_identical_at_destination_is_logged_not_silent(tmp_path, monkeypatch):
    """Пакет A п.6 (SESSION-HANDOFF.txt) -- та же ветка raw_mirrored: когда resolve_dest_path()
    (независимый от pool механизм "identical_at_destination", тот же класс гонки, что и Раунд 70
    ЗАМЕЧАНИЕ у image/video-ветки, см. test_album_merge_events.py) находит физически уже
    существующий на диске идентичный RAW-файл, код молча `return False` -- ни run_logs.skipped(),
    ни инкремент stats. Дедуп при этом происходит верно (файл не дублируется физически), но
    полностью невидим в skipped.csv/summary.txt -- узкий, но реальный сценарий (index_archive()
    временно не смог прочитать файл на Фазе 1)."""
    _stub_exiftool(monkeypatch)
    target = tmp_path / "target"
    existing = target / "RAW" / "Albums" / "AlbumX" / "a.cr2"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"raw" * 100)

    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    (source / "AlbumX" / "a.cr2").write_bytes(b"raw" * 100)  # same name AND content as target's

    real_sha256_file = m.sha256_file
    state = {"failed_once": False}
    existing_norm = os.path.normpath(str(existing))

    def flaky_sha256_file(path, *a, **kw):
        if not state["failed_once"] and os.path.normpath(path) == existing_norm:
            state["failed_once"] = True
            raise OSError("simulated transient read failure during Phase 1 indexing")
        return real_sha256_file(path, *a, **kw)

    monkeypatch.setattr(m, "sha256_file", flaky_sha256_file)

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = m.run(cfg, log=lambda *a, **k: None)

    assert state["failed_once"]  # sanity: the simulated race actually triggered
    assert stats["raw_mirrored"] == 0  # not a fresh append -- caught as a duplicate
    assert stats["skipped_present"] == 1  # must be visible in stats, not silently dropped
    assert stats["bytes_saved_by_dedup"] == len(b"raw" * 100)
