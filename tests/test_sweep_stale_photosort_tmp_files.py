"""_sweep_stale_photosort_tmp_files() -- 2026-08-24, живая просьба пользователя ("добавь
чистку при старте"): atomic_copy()'s staging-файлы (".photosort_tmp_*") остаются на диске
только если процесс убит настолько резко, что даже её собственные except-блоки не успевают
отработать (os._exit()/крах/Task Manager/пропажа питания -- НЕ обычный Ctrl-C, см. функции
докстринг в photosort_win.py). Вызывается из Фазы 0 реального прогона (run()/_run_impl()),
безопасно относительно гонки с другим живым прогоном -- run() уже держит TargetLock на этот
TARGET на всё время своей работы."""
import os

import photosort_win as m


def test_noop_when_target_does_not_exist(tmp_path):
    target = tmp_path / "does_not_exist"
    m._sweep_stale_photosort_tmp_files(str(target), log=lambda *a, **kw: None)  # must not raise


def test_noop_when_nothing_to_sweep(tmp_path):
    album = tmp_path / "Albums" / "Отпуск"
    album.mkdir(parents=True)
    (album / "photo.jpg").write_bytes(b"data")
    logged = []
    m._sweep_stale_photosort_tmp_files(str(tmp_path), log=lambda msg: logged.append(msg))
    assert (album / "photo.jpg").exists()
    assert logged == []


def test_removes_orphaned_staging_files_anywhere_under_target(tmp_path):
    """Огрызки могут лежать в ЛЮБОЙ папке альбома/даты (dest_dir конкретного файла внутри
    atomic_copy()), не в одном централизованном месте -- функция обязана найти их везде под
    TARGET, полным обходом."""
    album1 = tmp_path / "Albums" / "Отпуск"
    album2 = tmp_path / "ByDate" / "2020" / "2020-05"
    album1.mkdir(parents=True)
    album2.mkdir(parents=True)
    orphan1 = album1 / ".photosort_tmp_abc123"
    orphan2 = album2 / ".photosort_tmp_def456"
    orphan1.write_bytes(b"partial")
    orphan2.write_bytes(b"partial")
    (album1 / "real_photo.jpg").write_bytes(b"real")
    logged = []
    m._sweep_stale_photosort_tmp_files(str(tmp_path), log=lambda msg: logged.append(msg))
    assert not orphan1.exists()
    assert not orphan2.exists()
    assert (album1 / "real_photo.jpg").exists()  # реальный файл не тронут
    assert len(logged) == 1
    assert "2" in logged[0]


def test_swallows_failure_removing_individual_file(tmp_path, monkeypatch):
    album = tmp_path / "Albums" / "Отпуск"
    album.mkdir(parents=True)
    (album / ".photosort_tmp_abc123").write_bytes(b"partial")

    real_remove = os.remove

    def _boom(path):
        if ".photosort_tmp_" in path:
            raise OSError("simulated removal failure (e.g. file in use)")
        real_remove(path)

    monkeypatch.setattr(os, "remove", _boom)
    m._sweep_stale_photosort_tmp_files(str(tmp_path), log=lambda *a, **kw: None)  # must not raise
