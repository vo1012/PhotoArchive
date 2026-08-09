"""2026-08-08, живой боевой прогон F:->D: -- stats["undated"] (summary.txt "Без надёжной
даты"/report.py "не удалось распознать дату") инкрементировался ТОЛЬКО через final_decision==
"undated" (ветка "нет альбома И нет даты", photosort_win.py:_process_record()), а
undated_media.csv пишется для ЛЮБОГО Tier D файла независимо от альбома -- живой прогон дал
прямое расхождение: CSV содержал 4 файла (все из альбома 100_PANA), а summary.txt писал
"Без надёжной даты: 0". Обе подписи обещают "не удалось определить дату" без оговорки про
альбом -- решение (обсуждено с пользователем, "как лучше для пользователя"): stats["undated"]
должен считать любой Tier D файл, включая те, что легли в альбом."""
import photosort_win as m
from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _stub_exiftool(monkeypatch, tags_by_path=None):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: (tags_by_path or {}).get(p, {}) for p in paths})


def test_tier_d_file_in_album_counted_as_undated(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    # Форсируем Tier D ("нет сигнала даты вообще") -- то же, что реально происходит для файлов
    # без EXIF, вне узнаваемого имени папки-даты, чей mtime отфильтрован как copy-artifact
    # (см. tests/test_dates.py::test_no_signal_at_all_when_mtime_is_a_copy_artifact), проще
    # и детерминированнее воспроизвести здесь прямым monkeypatch, чем собирать три файла с
    # синхронными mtime.
    monkeypatch.setattr(m, "resolve_date", lambda *a, **kw: (None, "D", "none", "no_signal", None))
    source = tmp_path / "source"
    album = source / "AlbumX"
    album.mkdir(parents=True)
    _make_jpeg(album / "a.jpg")
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = m.run(cfg, log=lambda *a, **k: None)

    # Сердце фикса: файл с альбомом, но без надёжной даты, должен учитываться в "undated" --
    # раньше учитывались только файлы БЕЗ альбома И без даты.
    assert stats.get("undated", 0) == 1
    # Маршрутизация не должна измениться -- файл по-прежнему идёт по альбомному пути
    # (final_decision остаётся "appended_new"), фикс только добавляет счётчик, не трогает
    # физическое размещение.
    assert stats.get("appended_new", 0) == 1
    assert (target / "Albums" / "AlbumX" / "a.jpg").exists()
