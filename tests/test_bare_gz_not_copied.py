"""Живой боевой прогон 2026-08-29: одиночные `.gz`/`.bz2` файлы (не `.tar.gz`) -- YandexDisk
`.sync/core-*.log.gz`, locale-файлы `UTF-8.gz` -- КОПИРОВАЛИСЬ в `Albums/` как `unknown_type`.

Причина: `file_type()` возвращает `"archive"` для любого расширения из `ARCHIVE_EXTS`
(включая `gz`/`bz2`), а `detect_archive_format()` для бэйр `.gz`/`.bz2` возвращает `None`
(это одиночный сжатый файл, не многофайловый архив -- распаковать нечем). В цикле обхода
`_walk_dir()` такой файл проскакивал проверку `if t == "other"` и получал
`SourceItem(ftype="archive")`, который дальше по конвейеру копировался.

Фикс: после того как `detect_archive_format()` вернул `None`, файл с `file_type == "archive"`
(а это на этой строке МОЖЕТ быть только бэйр `.gz`/`.bz2`) пропускается так же, как `"other"`.
"""
import bz2
import gzip
import io
import tarfile

from PIL import Image

import photosort_win as m


def _jpeg_bytes(color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buf, "JPEG")
    return buf.getvalue()


def _walk(tmp_path):
    (tmp_path / "target").mkdir(exist_ok=True)
    cfg = m.Config(source=str(tmp_path / "source"), target=str(tmp_path / "target"))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    return list(walker.walk())


def test_classification_asymmetry_is_the_bug(tmp_path):
    # documents the exact mismatch the fix targets
    assert m.file_type("core-1.log.gz") == "archive"
    assert m.detect_archive_format("core-1.log.gz") is None
    assert m.file_type("locale/UTF-8.gz") == "archive"
    assert m.detect_archive_format("x.bz2") is None


def test_bare_gz_is_not_copied(tmp_path):
    src = tmp_path / "source" / "OLD" / "YandexDisk" / ".sync"
    src.mkdir(parents=True)
    (src / "core-1.log.gz").write_bytes(gzip.compress(b"2016-03-30 sync log line\n" * 50))
    (src / "core-2.log.gz").write_bytes(gzip.compress(b"more log\n" * 50))

    items = _walk(tmp_path)
    assert items == [], [it.origin_display for it in items]


def test_bare_bz2_is_not_copied(tmp_path):
    src = tmp_path / "source"
    src.mkdir(parents=True)
    (src / "dump.sql.bz2").write_bytes(bz2.compress(b"INSERT INTO t VALUES (1);\n" * 20))

    items = _walk(tmp_path)
    assert items == [], [it.origin_display for it in items]


def test_real_tar_gz_with_photo_still_extracted(tmp_path):
    # control: the fix must not break genuine multi-file .tar.gz -- detect_archive_format()
    # returns "tar.gz" for it, so it goes through _handle_archive() and never reaches the
    # skip branch.
    src = tmp_path / "source"
    src.mkdir(parents=True)
    jpeg = _jpeg_bytes()
    tgz_path = src / "album.tar.gz"
    with tarfile.open(tgz_path, "w:gz") as tf:
        ti = tarfile.TarInfo("Vacation/beach.jpg")
        ti.size = len(jpeg)
        tf.addfile(ti, io.BytesIO(jpeg))

    items = _walk(tmp_path)
    assert len(items) == 1 and items[0].ftype == "image"
    assert "beach.jpg" in items[0].origin_display and "album.tar.gz" in items[0].origin_display


def test_real_photo_next_to_bare_gz_still_copied(tmp_path):
    src = tmp_path / "source"
    src.mkdir(parents=True)
    (src / "photo.jpg").write_bytes(_jpeg_bytes())
    (src / "notes.txt.gz").write_bytes(gzip.compress(b"junk\n" * 100))

    items = _walk(tmp_path)
    assert [it.origin_display for it in items] == ["photo.jpg"]
