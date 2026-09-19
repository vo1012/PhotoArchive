"""Речь пользователя, 2026-09-19 (тот же заход, что и probe_dat_as_video для видео -- см.
test_probe_dat_as_video.py -- "проанализируй аналогично и фото форматы файлов"): голое ".raw"
(без бренда -- в отличие от orf/rw2/raf/... в RAW_EXTS, добавленных безусловно) -- расширение
слишком неспецифичное (посторонние сырые дампы данных), а у RAW-ветки, в отличие от обычных
изображений, вообще нет своей проверки "похоже на медиа" (всегда безусловно "камера сняла
это"). Единственный сигнал, которым можно отличить реальный RAW от мусора -- осмысленные
EXIF-теги камеры (Make/Model), см. _raw_ext_has_camera_exif()/Config.probe_raw_as_photo.

Реально проверено исполнением (не по докстрингу) вручную на забандленном bin/exiftool.exe:
JPEG с прописанными Make/Model -> True, случайный бинарный мусор -> False (тот же subprocess-
вызов, что использует и код). Тесты здесь мокают m._raw_ext_has_camera_exif()/m.EXIFTOOL_BIN-
based helper напрямую -- то же соглашение, что и в test_probe_dat_as_video.py."""
import os

import photosort_win as m


def _walk(tmp_path, probe_raw_as_photo=False):
    (tmp_path / "target").mkdir(exist_ok=True)
    cfg = m.Config(source=str(tmp_path / "source"), target=str(tmp_path / "target"),
                    probe_raw_as_photo=probe_raw_as_photo)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    return list(walker.walk())


class TestExtensionCoverage:
    def test_new_image_formats_present(self):
        for ext in ("avif", "jfif"):
            assert ext in m.IMAGE_EXTS, ext

    def test_new_brand_specific_raw_formats_present(self):
        for ext in ("orf", "rw2", "raf", "pef", "srw", "3fr", "nrw", "crw", "mrw", "x3f"):
            assert ext in m.RAW_EXTS, ext

    def test_bare_raw_deliberately_not_in_plain_extension_set(self):
        # "raw" (без бренда) МУСТ оставаться opt-in (probe_raw_as_photo), не обычным
        # расширением -- слишком неспецифично, и у RAW-ветки нет проверки "похоже на медиа".
        assert "raw" not in m.RAW_EXTS
        assert m.file_type("dump.raw") == "other"


class TestProbeRawDisabledByDefault:
    def test_bare_raw_not_copied_when_flag_off(self, tmp_path, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("probe_raw_as_photo=False must never touch exiftool")

        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", _boom)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "dump.raw").write_bytes(b"not a real raw container -- must not even be probed")

        items = _walk(tmp_path, probe_raw_as_photo=False)
        assert items == [], [it.origin_display for it in items]


class TestProbeRawEnabled:
    def test_bare_raw_copied_as_raw_when_camera_exif_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", lambda path: True)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "IMG_0001.raw").write_bytes(b"fake camera raw bytes")

        items = _walk(tmp_path, probe_raw_as_photo=True)
        assert len(items) == 1
        assert items[0].ftype == "raw"
        assert items[0].origin_display.endswith("IMG_0001.raw")

    def test_bare_raw_still_skipped_when_no_camera_exif_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", lambda path: False)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "some_dump.raw").write_bytes(b"definitely not a photo")

        items = _walk(tmp_path, probe_raw_as_photo=True)
        assert items == [], [it.origin_display for it in items]

    def test_classify_source_file_type_unit(self, tmp_path, monkeypatch):
        cfg_on = m.Config(source=str(tmp_path / "S"), target=str(tmp_path / "T"),
                           probe_raw_as_photo=True)
        cfg_off = m.Config(source=str(tmp_path / "S"), target=str(tmp_path / "T"),
                            probe_raw_as_photo=False)

        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", lambda path: True)
        assert m.classify_source_file_type("x.raw", cfg_on) == "raw"
        assert m.classify_source_file_type("x.raw", cfg_off) == "other"
        # brand-specific RAW never reaches the probe -- already recognized by extension alone
        assert m.classify_source_file_type("photo.orf", cfg_off) == "raw"


class TestProbeRawSiblingPairing:
    """Живая находка при реализации (не заранее известный баг): sibling_by_base -- индекс
    "у какого файла в этой папке есть RAW/JPEG-партнёр с тем же именем" -- строился ДО фикса
    голым file_type(), который для bare ".raw" всегда даёт "other", даже когда
    probe_raw_as_photo включён и файл реально подтверждён как RAW. Из-за этого JPEG с тем же
    базовым именем получал sibling_path=None (ложный "JPEG без RAW" в stats.n_jpeg_without_raw)
    -- сама RAW-запись при этом копировалась верно (её СОБСТВЕННЫЙ sibling_path на JPEG строился
    из другого, непострадавшего направления индекса). Фикс: sibling_by_base строится через
    classify_source_file_type(), тем же кэшем, которым потом пользуется и реальный per-file
    диспетчер (без двойного/тройного subprocess-вызова на файл)."""

    def test_jpeg_finds_bare_raw_sibling_when_probe_confirms_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", lambda path: True)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "IMG_0001.jpg").write_bytes(b"fake jpeg bytes")
        (src / "IMG_0001.raw").write_bytes(b"fake camera raw bytes")

        items = {os.path.splitext(it.origin_display)[1]: it
                 for it in _walk(tmp_path, probe_raw_as_photo=True)}
        assert set(items) == {".jpg", ".raw"}
        assert items[".jpg"].ftype == "image"
        assert items[".raw"].ftype == "raw"
        assert items[".jpg"].sibling_path == items[".raw"].read_path
        assert items[".raw"].sibling_path == items[".jpg"].read_path

    def test_raw_probe_called_exactly_once_per_file_despite_two_scan_passes(self, tmp_path, monkeypatch):
        # _walk_dir() scans `files` twice (sibling/preview pass, then real dispatch) -- the
        # classification cache must make classify_source_file_type()'s subprocess call happen
        # ONCE per .raw, not two or three times (see _defer_raw_with_sibling() too).
        calls = []

        def _counted(path):
            calls.append(path)
            return True

        monkeypatch.setattr(m, "_raw_ext_has_camera_exif", _counted)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "solo.raw").write_bytes(b"fake camera raw bytes")

        items = _walk(tmp_path, probe_raw_as_photo=True)
        assert len(items) == 1 and items[0].ftype == "raw"
        assert len(calls) == 1, calls


class TestRawExtCameraExifContentSniff:
    def test_recognizes_make_or_model(self, monkeypatch):
        import subprocess

        def _fake_run(*a, **kw):
            class R:
                stdout = b'[{"Make": "Canon", "Model": "EOS 5D"}]'
            return R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert m._raw_ext_has_camera_exif("clip.raw") is True

    def test_rejects_missing_make_and_model(self, monkeypatch):
        import subprocess

        def _fake_run(*a, **kw):
            class R:
                stdout = b'[{}]'
            return R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert m._raw_ext_has_camera_exif("clip.raw") is False

    def test_rejects_on_empty_or_broken_output(self, monkeypatch):
        import subprocess

        def _fake_run_empty(*a, **kw):
            class R:
                stdout = b'[]'
            return R()

        monkeypatch.setattr(subprocess, "run", _fake_run_empty)
        assert m._raw_ext_has_camera_exif("clip.raw") is False

        def _fake_run_boom(*a, **kw):
            raise OSError("exiftool not found")

        monkeypatch.setattr(subprocess, "run", _fake_run_boom)
        assert m._raw_ext_has_camera_exif("clip.raw") is False


class TestRawProbeRealExiftoolNonAsciiPath:
    """Боевой прогон 2026-09-19: exiftool.exe на Windows получает argv в ANSI -- с
    `-charset filename=utf8` кириллический путь в argv не находится ("Invalid filename
    encoding"), проба молча возвращала False для КАЖДОГО .raw в русскоязычной папке. Моки
    subprocess.run это не ловят -- нужен настоящий exiftool (как и в остальных путях обхода,
    путь передаётся через -@ argfile)."""

    def test_camera_exif_found_in_cyrillic_directory(self, tmp_path):
        import shutil
        import subprocess

        import pytest
        if not (os.path.isfile(m.EXIFTOOL_BIN) or shutil.which(m.EXIFTOOL_BIN)):
            pytest.skip("exiftool недоступен")
        from PIL import Image
        d = tmp_path / "Отпуск"
        d.mkdir()
        jpg = d / "cam.jpg"
        Image.new("RGB", (64, 48), (10, 20, 30)).save(jpg, "JPEG")
        try:
            r = subprocess.run(
                [m.EXIFTOOL_BIN, "-q", "-overwrite_original", "-Make=Canon", "-Model=Test", str(jpg)],
                capture_output=True, timeout=60)
        except OSError:
            pytest.skip("exiftool не запускается")
        if r.returncode != 0:
            pytest.skip("exiftool не смог записать теги")
        raw = d / "cam.raw"
        os.replace(jpg, raw)
        assert m._raw_ext_has_camera_exif(str(raw)) is True


class TestArchiveReindexRecognizesBareRawWithoutReprobing:
    """index_archive() (переиндексация TARGET на следующем прогоне) -- голый .raw, уже лежащий
    в архиве, безусловно raw, БЕЗ повторного вызова exiftool: попасть туда он мог только через
    classify_source_file_type() (проба уже пройдена один раз при копировании)."""

    def test_walk_media_files_treats_existing_bare_raw_as_raw(self, tmp_path, monkeypatch):
        import subprocess

        def _boom(*a, **kw):
            raise AssertionError("re-indexing an already-archived .raw must not re-probe it")

        monkeypatch.setattr(subprocess, "run", _boom)

        root = tmp_path / "Albums" / "Отпуск"
        root.mkdir(parents=True)
        (root / "old_camera.raw").write_bytes(b"already-confirmed raw, no reason to re-probe")

        found = list(m._walk_media_files(str(tmp_path)))
        assert len(found) == 1
        path, ftype = found[0]
        assert ftype == "raw"
        assert path.endswith("old_camera.raw")
