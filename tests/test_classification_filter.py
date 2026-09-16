"""Переключатель "альбом"/"по дате"/"всё подряд" (Config.classification_filter, 2026-09-15,
прямая просьба пользователя, только GUI -- экран 2 режима "Создание архива", см.
gui_menu.py/render_paths_screen()). "all" (дефолт) -- поведение SourceWalker не меняется вообще
(см. TestClassificationFilterDefaultAll ниже и весь остальной набор тестов репозитория,
непосредственно не тронутый этой правкой). "albums_only"/"bydate_only" -- три точки внутри
SourceWalker._walk_dir() (папка/архив отравлены сегментом-именем -> _is_terminal_bydate_branch();
файл-лист -> find_album()), уже существующие для двухфазного обхода (см.
test_two_phase_album_priority.py), решают дополнительно, yield'ить найденное в ЭТОТ прогон или
отбросить -- ДО чтения байт файла (классификация чисто по сегментам пути), поэтому
albums_only/bydate_only не читает лишнего, только один противоположный по классификации набор.

Мотивирующий сценарий пользователя (SESSION-HANDOFF.txt, 2026-09-15) -- TestCrossSourceDedupPriming
ниже: несколько пересекающихся источников, прогнанных albums_only-затем-bydate_only ПО ВСЕМ
источникам, ловят межисточниковый дубль (дамп-копия источника A против альбомной копии
источника B) даже когда A обработан раньше B по времени -- Фаза 1 каждого прогона переиндексирует
уже собранный TARGET (см. RULES.md, "БАЗА ДЕДУПА")."""
import os
import tempfile
import zipfile

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(str(path), "JPEG")


@pytest.fixture(autouse=True)
def _no_exiftool(monkeypatch):
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})


def _cfg(tmp_path, classification_filter="all", workdir=None):
    source = tmp_path / "source"
    if not source.exists():
        source.mkdir()
    target = tmp_path / "target"
    if not target.exists():
        target.mkdir()
    kwargs = {}
    if workdir is not None:
        kwargs["workdir"] = str(workdir)
    return (m.Config(source=str(source), target=str(target),
                      classification_filter=classification_filter, **kwargs),
            source, target)


def _zip_with_one_photo(zpath, color=(200, 50, 90)):
    with zipfile.ZipFile(zpath, "w") as zf:
        img = tempfile.mktemp(suffix=".jpg")
        _make_jpeg(img, color=color)
        zf.write(img, "inner.jpg")


class TestClassificationFilterDefaultAll:
    def test_all_mode_yields_everything_unfiltered(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "all")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg", color=(200, 50, 90))

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert len(items) == 2, [it.rel_path for it in items]
        assert walker.classification_filtered_subtrees == 0
        assert walker.classification_filtered_files == {"image": 0, "raw": 0, "video": 0}


class TestAlbumsOnlyFilter:
    def test_album_file_kept_dump_file_dropped(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "albums_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg", color=(200, 50, 90))

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert [it.rel_path for it in items] == ["RealAlbum/photo.jpg"]
        # DCIM is a whole poisoned subtree, dropped as one unit -- not per-file.
        assert walker.classification_filtered_subtrees == 1
        assert walker.classification_filtered_files == {"image": 0, "raw": 0, "video": 0}

    def test_stray_file_without_any_album_ancestor_is_counted_per_file(self, tmp_path):
        # A dump-poisoned FILE (not a whole subdirectory) still goes through the leaf-level
        # find_album() check in _walk_dir() (self._deferred_stray_files path) -- counted exactly,
        # unlike a whole subtree.
        cfg, source, _target = _cfg(tmp_path, "albums_only")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "loose.jpg")

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert items == []
        # "DCIM" itself is the poisoned subdirectory entry point -> counted as a subtree, not
        # per-file (the file inside is never individually visited/classified).
        assert walker.classification_filtered_subtrees == 1

    def test_poisoned_subtree_never_listed(self, tmp_path, monkeypatch):
        """The whole point of the optimization: a dump-poisoned subtree must not even be
        listdir()'d in albums_only mode -- proven by making its listing blow up if touched."""
        cfg, source, _target = _cfg(tmp_path, "albums_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg")

        real_listdir = os.listdir
        guarded_path = os.path.normcase(m.winlong(str(dump)))

        def _guarded_listdir(path):
            if os.path.normcase(str(path)) == guarded_path:
                raise AssertionError("albums_only must not descend into a poisoned subtree")
            return real_listdir(path)

        monkeypatch.setattr(m.os, "listdir", _guarded_listdir)
        items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
        assert [it.rel_path for it in items] == ["RealAlbum/photo.jpg"]

    def test_tilde_archive_never_extracted(self, tmp_path, monkeypatch):
        cfg, source, _target = _cfg(tmp_path, "albums_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")
        _zip_with_one_photo(album / "~backup.zip")

        def _boom(*a, **k):
            raise AssertionError("albums_only must not open/extract a dump-named archive")

        monkeypatch.setattr(m, "list_archive", _boom)
        items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
        assert [it.rel_path for it in items] == ["RealAlbum/photo.jpg"]


class TestBydateOnlyFilter:
    def test_dump_file_kept_album_file_dropped(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "bydate_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg", color=(200, 50, 90))

        items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
        assert [it.rel_path for it in items] == ["DCIM/dump.jpg"]

    def test_tilde_archive_inside_clean_album_still_found(self, tmp_path):
        """Самый хитрый корректностный случай: bydate_only обязан всё равно спуститься в
        абсолютно чистую альбомную папку, чтобы найти вложенный тильда-архив -- его СОБСТВЕННОЕ
        имя отравляет только его же содержимое, независимо от папки-контейнера (см.
        find_album()'s докстринг "архив == папка")."""
        cfg, source, _target = _cfg(tmp_path, "bydate_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo1.jpg", color=(10, 20, 30))
        _zip_with_one_photo(album / "~backup.zip", color=(200, 50, 90))

        items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
        rels = [it.rel_path for it in items]
        assert len(rels) == 1, rels
        assert "photo1" not in rels[0]

    def test_poisoned_branch_below_album_still_found(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "bydate_only")
        album = source / "RealAlbum"
        (album / "~synced").mkdir(parents=True)
        _make_jpeg(album / "~synced" / "junk.jpg", color=(200, 50, 90))
        _make_jpeg(album / "normal.jpg", color=(10, 20, 30))

        items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
        rels = [it.rel_path for it in items]
        assert len(rels) == 1 and "junk" in rels[0], rels

    def test_normal_file_counted_per_file_when_dropped(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "bydate_only")
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg")

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert items == []
        assert walker.classification_filtered_files == {"image": 1, "raw": 0, "video": 0}
        assert walker.classification_filtered_subtrees == 0


class TestClassificationFilterDvdUnit:
    def test_albums_only_keeps_album_dvd_drops_bydate_dvd(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "albums_only")
        album_disc = source / "RealAlbum" / "VIDEO_TS"
        album_disc.mkdir(parents=True)
        (album_disc / "VTS_01_0.VOB").write_bytes(b"v" * 500)
        dump_disc = source / "DCIM" / "VIDEO_TS"
        dump_disc.mkdir(parents=True)
        (dump_disc / "VTS_01_0.VOB").write_bytes(b"w" * 500)

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert len(items) == 1, [it.rel_path for it in items]
        assert "RealAlbum" in items[0].rel_path
        assert walker.classification_filtered_subtrees == 1

    def test_bydate_only_keeps_bydate_dvd_drops_album_dvd(self, tmp_path):
        cfg, source, _target = _cfg(tmp_path, "bydate_only")
        album_disc = source / "RealAlbum" / "VIDEO_TS"
        album_disc.mkdir(parents=True)
        (album_disc / "VTS_01_0.VOB").write_bytes(b"v" * 500)
        dump_disc = source / "DCIM" / "VIDEO_TS"
        dump_disc.mkdir(parents=True)
        (dump_disc / "VTS_01_0.VOB").write_bytes(b"w" * 500)

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert len(items) == 1, [it.rel_path for it in items]
        assert "DCIM" in items[0].rel_path


class TestClassificationFilterIgnoredForBareFileSource:
    def test_bare_media_file_source_processed_regardless_of_filter(self, tmp_path):
        img = tmp_path / "photo.jpg"
        _make_jpeg(img)
        target = tmp_path / "target"
        target.mkdir()
        for filt in ("all", "albums_only", "bydate_only"):
            cfg = m.Config(source=str(img), target=str(target), classification_filter=filt)
            items = list(m.SourceWalker(cfg, log=lambda *a, **k: None).walk())
            assert len(items) == 1, (filt, items)


class TestTwoPassWorkflowEndToEnd:
    def test_albums_only_then_bydate_only_together_archive_everything_once(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "photo.jpg", color=(10, 20, 30))
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg", color=(200, 50, 90))

        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        cfg1 = m.Config(source=str(source), target=str(target), workdir=str(workdir),
                         classification_filter="albums_only")
        m.run(cfg1, log=lambda *a, **k: None)
        album_files = list((target / "Albums").rglob("*.jpg"))
        bydate_files = list((target / "ByDate").rglob("*.jpg")) if (target / "ByDate").exists() else []
        assert len(album_files) == 1, album_files
        assert bydate_files == [], "первый прогон (albums_only) не должен трогать дамп-файл"

        cfg2 = m.Config(source=str(source), target=str(target), workdir=str(workdir),
                         classification_filter="bydate_only")
        m.run(cfg2, log=lambda *a, **k: None)
        bydate_files = list((target / "ByDate").rglob("*.jpg"))
        assert len(bydate_files) == 1, bydate_files

        all_jpgs = list((target / "Albums").rglob("*.jpg")) + list((target / "ByDate").rglob("*.jpg"))
        assert len(all_jpgs) == 2, "оба реальных файла должны оказаться в архиве, ровно по разу"


class TestCrossSourceDedupPriming:
    """Мотивирующий сценарий пользователя (обсуждение 2026-09-15): несколько ПЕРЕСЕКАЮЩИХСЯ
    источников -- дамп-копия кадра в источнике A и альбомная копия ТОГО ЖЕ кадра в источнике B.
    Прогон albums_only на ОБОИХ источниках, затем bydate_only на ОБОИХ -- альбомная копия должна
    выиграть дедуп-гонку, даже если A физически обработан РАНЬШЕ B."""

    def test_album_copy_from_later_source_wins_against_earlier_dump_copy(self, tmp_path):
        source_a = tmp_path / "source_a"
        source_a.mkdir()
        dump_a = source_a / "DCIM"
        dump_a.mkdir()
        _make_jpeg(dump_a / "dump_copy.jpg", color=(10, 20, 30))

        source_b = tmp_path / "source_b"
        source_b.mkdir()
        album_b = source_b / "RealAlbum"
        album_b.mkdir()
        # Byte-identical to source_a's dump copy -- see test_nested_albums_and_dedup.py's
        # convention (write_bytes from an already-rendered JPEG, not a second independent
        # _make_jpeg() call, which is not guaranteed to be byte-for-byte reproducible).
        (album_b / "album_copy.jpg").write_bytes((dump_a / "dump_copy.jpg").read_bytes())

        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        # Pass 1: albums_only on both sources (A first, chronologically) -- primes the pool with
        # source B's album copy before source A's dump content is ever considered.
        for source in (source_a, source_b):
            cfg = m.Config(source=str(source), target=str(target), workdir=str(workdir),
                            classification_filter="albums_only")
            m.run(cfg, log=lambda *a, **k: None)
        # Pass 2: bydate_only on both -- source A's dump copy must now be recognized as an
        # already-archived duplicate (of source B's album copy from pass 1), not appended again.
        for source in (source_a, source_b):
            cfg = m.Config(source=str(source), target=str(target), workdir=str(workdir),
                            classification_filter="bydate_only")
            m.run(cfg, log=lambda *a, **k: None)

        album_files = list((target / "Albums").rglob("*.jpg"))
        bydate_files = list((target / "ByDate").rglob("*.jpg")) if (target / "ByDate").exists() else []
        assert len(album_files) == 1, album_files
        assert bydate_files == [], \
            f"дамп-копия должна была распознаться как дубль уже заархивированного альбома, а не появиться отдельно: {bydate_files}"
