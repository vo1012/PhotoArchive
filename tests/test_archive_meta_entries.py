"""Боевой прогон 2026-08-28: tar-ветка extract_archive() печатала СЫРОЙ текст исключения
tarfile (по-английски, "'opt/bin/cat' is a link to an absolute path") отдельной строкой на
КАЖДЫЙ пропущенный член -- backup-архив прошивки роутера дал 178 таких строк подряд, криво
завёрнутых _wrap_console_text(). Правки A/C/D/E:

- A: символьные/жёсткие ссылки, устройства и FIFO вообще не пытаемся распаковывать -- считаем.
- C: одна сводная строка на архив вместо строки на член.
- D: реальные сбои распаковки -- по-русски, с кэпом (_EXTRACT_FAILURE_SAMPLE_CAP), без
  английского хвоста исключения.
- E: и сводка, и сбои попадают в archives.log (walker.archive_logs -> archive_event()).
"""
import errno
import tarfile

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def _add_symlink(tf, name, target):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.SYMTYPE
    ti.linkname = target
    tf.addfile(ti)


def _add_hardlink(tf, name, target):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.LNKTYPE
    ti.linkname = target
    tf.addfile(ti)


def _add_file(tf, name, content=b"x" * 20):
    import io
    ti = tarfile.TarInfo(name)
    ti.size = len(content)
    tf.addfile(ti, io.BytesIO(content))


# ---------------------------------------------------------------------------
# extract_archive(): A + ExtractOutcome
# ---------------------------------------------------------------------------

def test_extract_archive_skips_link_and_device_members_without_extracting(tmp_path):
    tar_path = tmp_path / "router-backup.tar"
    with tarfile.open(tar_path, "w") as tf:
        _add_file(tf, "opt/bin/busybox")
        _add_symlink(tf, "opt/bin/cat", "/opt/bin/busybox")      # absolute-path symlink
        _add_symlink(tf, "opt/bin/ls", "busybox")                # relative symlink
        _add_hardlink(tf, "opt/bin/sh", "opt/bin/busybox")       # hardlink
        _add_file(tf, "Album/photo.jpg")

    dest = tmp_path / "out"
    outcome = m.extract_archive(str(tar_path), "tar", str(dest), log=lambda *a, **k: None)

    assert outcome.ok
    assert bool(outcome) is True
    assert outcome.skipped_meta == 3           # cat + ls + sh
    assert outcome.failure_total == 0
    assert (dest / "Album" / "photo.jpg").is_file()
    assert (dest / "opt" / "bin" / "busybox").is_file()
    # ни одна ссылка не материализована на диске
    assert not (dest / "opt" / "bin" / "cat").exists()
    assert not (dest / "opt" / "bin" / "ls").exists()
    assert not (dest / "opt" / "bin" / "sh").exists()


def test_extract_archive_clean_tar_reports_zero_skips(tmp_path):
    tar_path = tmp_path / "clean.tar"
    with tarfile.open(tar_path, "w") as tf:
        _add_file(tf, "a.jpg")
        _add_file(tf, "b.jpg")

    outcome = m.extract_archive(str(tar_path), "tar", str(tmp_path / "out"),
                                 log=lambda *a, **k: None)
    assert outcome.ok
    assert outcome.skipped_meta == 0
    assert outcome.failures == []
    assert outcome.failure_total == 0


def test_extract_outcome_is_falsy_on_failure():
    assert not m.ExtractOutcome(ok=False)
    assert m.ExtractOutcome(ok=True)


# ---------------------------------------------------------------------------
# _short_extract_error(): D -- русские причины, без английского хвоста
# ---------------------------------------------------------------------------

def test_short_extract_error_oserror_mapping():
    assert m._short_extract_error(OSError(errno.ENAMETOOLONG, "x")) == "слишком длинный путь"
    assert m._short_extract_error(OSError(errno.ENOSPC, "x")) == "нет места на диске"
    assert m._short_extract_error(OSError(errno.EACCES, "x")) == "отказано в доступе"
    assert m._short_extract_error(OSError(errno.EIO, "x")) == "ошибка записи файла"


def test_short_extract_error_tarfile_exception_names():
    class AbsolutePathError(Exception):
        pass

    class LinkOutsideDestinationError(Exception):
        pass

    class SpecialFileError(Exception):
        pass

    assert m._short_extract_error(AbsolutePathError()) == "абсолютный путь внутри архива"
    assert m._short_extract_error(LinkOutsideDestinationError()) == "ссылка за пределы архива"
    assert m._short_extract_error(SpecialFileError()) == "специальный файл (устройство/сокет)"
    assert m._short_extract_error(ValueError("boom")) == "не удалось распаковать"


# ---------------------------------------------------------------------------
# End-to-end: C + E -- одна строка в консоль, запись в archive_logs
# ---------------------------------------------------------------------------

def test_walker_archive_with_many_abs_symlinks_logs_one_summary_line(tmp_path):
    tar_path = tmp_path / "backup-2024.tar"
    with tarfile.open(tar_path, "w") as tf:
        _add_file(tf, "Album/photo.jpg")
        for tool in ("cat", "chmod", "mount", "ssh", "echo", "df", "expr", "seq", "tee", "nc"):
            _add_symlink(tf, f"opt/bin/{tool}", "/opt/bin/busybox")
    (tmp_path / "target").mkdir()

    lines = []
    cfg = _make_cfg(tmp_path, source=str(tar_path))
    walker = m.SourceWalker(cfg, log=lambda msg="", *a, **k: lines.append(str(msg)))
    items = list(walker.walk())

    assert [it.origin_display for it in items] == ["Album/photo.jpg"]

    joined = "\n".join(lines)
    # никакого сырого английского текста исключения и никаких построчных пропусков
    assert "link to an absolute path" not in joined
    assert "пропущен файл в архиве" not in joined
    # ровно одна человеческая сводная строка
    summary = [ln for ln in lines if "служебных записей" in ln]
    assert len(summary) == 1
    assert "10" in summary[0]

    # E: попало в archive_notes -> archives.log (отдельно от archive_logs, чтобы не
    # путать счётчики n_archives_found/archives_seen).
    tags = {tag for _disp, tag, _text in walker.archive_notes}
    assert "meta_entries_skipped" in tags
    text = next(t for _d, tag, t in walker.archive_notes if tag == "meta_entries_skipped")
    assert "10" in text
    # архив по-прежнему учтён РОВНО один раз в archive_logs
    assert sum(1 for _d, s, _n in walker.archive_logs if s.startswith("archive_")) == 1
