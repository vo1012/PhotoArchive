"""SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2 -- новые форматирующие функции/методы:
- _fmt_size_gb() -- размер для транзиентной операции статус-строки.
- ArchiveInfo.media_count / _member_name_is_strict_media() -- точный счётчик "найдено
  медиафайлов N" для архивов (без учёта вложенных архивов, в отличие от has_media_candidate).
- SourceWalker._walk_dir()'s folder_media_count + object_line_cb -- то же самое для папок.
- _quick_media_count_estimate() -- быстрый предпересчёт SOURCE для ETA, под теми же правилами
  исключения, что и обычный обход.
- ProgressReporter(two_line=True) -- новый формат статус-строки + write_object_line()/
  set_transient_op().

Только форматирующая логика на синтетических путях/деревьях/архивах -- не требует реального
терминала (is_tty=False по умолчанию под pytest, тот же принцип, что и у остальных тестов
этого файла)."""
import tarfile
import textwrap

import pytest

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


# ---------------------------------------------------------------------------
# _fmt_size_gb()
# ---------------------------------------------------------------------------

def test_fmt_size_gb_tiny_fraction():
    assert m._fmt_size_gb(50 * 1024**2) == "<0.1ГБ"  # 50 МБ


def test_fmt_size_gb_normal():
    assert m._fmt_size_gb(1.5 * 1024**3) == "1.5ГБ"


def test_fmt_size_gb_boundary_exactly_0_1():
    assert m._fmt_size_gb(0.1 * 1024**3) == "0.1ГБ"


# ---------------------------------------------------------------------------
# ArchiveInfo.media_count / _member_name_is_strict_media()
# ---------------------------------------------------------------------------

def test_member_name_is_strict_media_excludes_archive_exts():
    assert m._member_name_is_strict_media("photo.jpg")
    assert m._member_name_is_strict_media("clip.mp4")
    assert m._member_name_is_strict_media("raw.cr2")
    assert not m._member_name_is_strict_media("nested.zip")
    assert not m._member_name_is_strict_media("readme.txt")


def test_list_tar_media_count_excludes_nested_archive(tmp_path):
    tar_path = tmp_path / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        for name, content in [("a.jpg", b"x"), ("b.jpg", b"y"),
                               ("nested.zip", b"z"), ("notes.txt", b"w")]:
            member_path = tmp_path / name
            member_path.write_bytes(content)
            tf.add(member_path, arcname=name)

    info = m._list_tar(str(tar_path), "r:")
    assert info.ok
    assert info.entries == 4
    assert info.media_count == 2  # only a.jpg/b.jpg -- not nested.zip, not notes.txt
    assert info.has_media_candidate  # unaffected (still counts nested.zip as a candidate)


# ---------------------------------------------------------------------------
# SourceWalker: folder_media_count -> object_line_cb("folder", ...)
# ---------------------------------------------------------------------------

def test_walk_dir_reports_folder_media_count_image_raw_video_only(tmp_path):
    source = tmp_path / "source"
    album = source / "Album"
    album.mkdir(parents=True)
    (album / "a.jpg").write_bytes(b"x" * 10)
    (album / "b.cr2").write_bytes(b"x" * 10)
    (album / "c.mp4").write_bytes(b"x" * 10)
    (album / "readme.txt").write_bytes(b"x" * 10)  # not media -- must not be counted
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             object_line_cb=lambda tag, path, n: calls.append((tag, path, n)))
    list(walker.walk())

    folder_calls = [c for c in calls if c[0] == "folder" and c[1].endswith("Album")]
    assert len(folder_calls) == 1
    assert folder_calls[0][2] == 3  # a.jpg + b.cr2 + c.mp4, not readme.txt


def test_walk_dir_object_line_cb_not_called_for_excluded_dirs(tmp_path):
    source = tmp_path / "source"
    (source / "node_modules").mkdir(parents=True)
    (source / "node_modules" / "junk.jpg").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             object_line_cb=lambda tag, path, n: calls.append((tag, path, n)))
    list(walker.walk())

    assert not any("node_modules" in c[1] for c in calls)


# ---------------------------------------------------------------------------
# SourceWalker: dvd_folders -- DVD-Video (VIDEO_TS) обнаружен, но не скопирован (живой репорт
# пользователя, 2026-08-01 -- .vob не распознаётся как медиа вообще, см. SESSION-HANDOFF.txt).
# ---------------------------------------------------------------------------

def test_dvd_folder_detected_by_video_ts_with_vob(tmp_path):
    source = tmp_path / "source"
    disc = source / "Some_Movie_DVD5"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == [str(disc)]  # parent of VIDEO_TS, not VIDEO_TS itself


def test_dvd_folder_detection_is_case_insensitive(tmp_path):
    # Folder name case and extension case both vary in the wild -- neither should matter.
    source = tmp_path / "source"
    disc = source / "lowercase_disc"
    (disc / "video_ts").mkdir(parents=True)
    (disc / "video_ts" / "VTS_01_0.vob").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == [str(disc)]


def test_dvd_folder_not_detected_without_vob_ifo_bup(tmp_path):
    source = tmp_path / "source"
    disc = source / "Not_A_DVD"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "readme.txt").write_bytes(b"x")  # no .vob/.ifo/.bup -- not a real DVD
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == []


def test_vob_file_outside_video_ts_folder_not_detected(tmp_path):
    # Folder NAME is the signal, not just the presence of a .vob file anywhere -- a stray .vob
    # dropped directly in an ordinary album folder shouldn't misfire the DVD notice.
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "clip.vob").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == []


def test_dvd_folder_detected_via_ifo_without_vob(tmp_path):
    source = tmp_path / "source"
    disc = source / "Menu_Only_DVD"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == [str(disc)]


def test_dvd_folder_inside_archive_is_not_dead_tmp_extract_path(tmp_path):
    """Раунд 49 ревью (REVIEW-HANDOFF.md, замечание 2): VIDEO_TS найден ВНУТРИ архива (не
    напрямую под SOURCE) -- тот же класс бага, что уже чинился для запароленных вложенных
    архивов (Раунды 45/47). До фикса dvd_folders хранил os.path.dirname(cur_dirpath), реальный
    путь под cfg.tmp_extract -- удаляется cleanup_dir() до того, как report.html строит по нему
    ссылку. После фикса -- читаемый origin-трейл ("vacation.zip → vacation", тот же формат, что
    у skip_marker-лога/progress_cb в этой же функции), не абсолютный путь под tmp_extract."""
    import zipfile

    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "vacation.zip", "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)  # real media -- has_media_candidate=True, archive extracted
        zf.writestr("VIDEO_TS/VTS_01_0.VOB", b"v" * 100)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == ["vacation.zip → vacation"]


def test_dvd_folder_inside_nested_archive_subdir_keeps_origin_trail(tmp_path):
    """Same bug class as above, one directory level deeper inside the archive -- the fix must
    strip only the trailing VIDEO_TS component of cur_rel_prefix, keeping the rest of the trail
    (the "disk" folder), same convention as the depth==0 dirname(cur_dirpath) case."""
    import zipfile

    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "vacation.zip", "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)
        zf.writestr("Disk1/VIDEO_TS/VTS_01_0.VOB", b"v" * 100)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.dvd_folders == ["vacation.zip → vacation/Disk1"]


# ---------------------------------------------------------------------------
# SourceWalker: archive media_count -> object_line_cb("archive", ...)
# ---------------------------------------------------------------------------

def test_handle_archive_reports_media_count_before_extraction(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        for name in ("a.jpg", "b.jpg", "notes.txt"):
            p = tmp_path / name
            p.write_bytes(b"x" * 10)
            tf.add(p, arcname=name)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             object_line_cb=lambda tag, path, n: calls.append((tag, path, n)))
    list(walker.walk())

    archive_calls = [c for c in calls if c[0] == "archive"]
    assert len(archive_calls) == 1
    assert archive_calls[0][2] == 2  # a.jpg + b.jpg, not notes.txt


def test_handle_archive_object_line_shows_full_path_for_top_level_archive(tmp_path):
    # Живой репорт пользователя (2026-08-01): архив верхнего уровня показывал только имя файла
    # ("upravlenie_osvesheniem.zip"), не полный путь -- в отличие от [папка]-строк (см.
    # disp_for_object в _walk_dir()), которые в той же ситуации показывают cur_dirpath целиком.
    # Причина была в origin_prefix: для архива он уже "самоссылающийся" (включает собственное
    # имя архива), поэтому старый "if origin_prefix else basename" никогда не срабатывал.
    source = tmp_path / "source"
    (source / "Sub").mkdir(parents=True)
    tar_path = source / "Sub" / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             object_line_cb=lambda tag, path, n: calls.append((tag, path, n)))
    list(walker.walk())

    archive_calls = [c for c in calls if c[0] == "archive"]
    assert len(archive_calls) == 1
    assert archive_calls[0][1] == str(tar_path)  # full path, not bare "album.tar"


def test_handle_archive_sets_and_clears_transient_op_around_extraction(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ops = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, transient_op_cb=ops.append)
    list(walker.walk())

    assert any(op is not None and "распаковка" in op for op in ops)
    assert ops[-1] is None  # cleared again after extraction, whether it succeeded or not


def test_handle_archive_extracted_log_line_matches_object_line_style(tmp_path):
    # Живой репорт пользователя (2026-08-01): "[archive] X: archive_extracted N медиафайлов"
    # (status-стиль) vs "[archive] X найдено медиафайлов N" (object-line-стиль, тот же самый
    # архив несколькими строками выше) -- две разные на вид строки про одно и то же архивное
    # событие. Приводим archive_extracted к object-line-стилю (с "распаковано," спереди --
    # второе, подтверждённое после реальной распаковки число, не буквальный повтор).
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append)
    list(walker.walk())

    assert any("[archive] " in ln and "распаковано, найдено медиафайлов 1" in ln for ln in lines)
    assert not any(": archive_extracted" in ln for ln in lines)  # old "status note" style is gone


def test_handle_archive_no_media_is_not_printed_twice(tmp_path):
    """Живой репорт пользователя, 2026-08-02 ("зачем 2 раза и почему в одном перенос?"):
    archive_no_media (найден по листингу, БЕЗ реальной распаковки -- has_media_candidate
    False) раньше печаталась ДВАЖДЫ -- один раз через write_object_line() (object_line_cb,
    сразу после листинга), второй раз через _log_archive()'s собственный log() чуть ниже --
    тем же самым текстом "найдено медиафайлов 0". Вторая копия не обрезала путь под ширину
    терминала (в отличие от первой), из-за чего на длинных путях расходилась переносом строки
    -- реальный дубль-баг, не просто разное форматирование одного факта. Фикс: этот код-путь
    зовёт _log_archive(..., silent=True) -- archive_logs (для archives.log/n_archives_found)
    по-прежнему пишется, но log() вызывается только один раз, через object_line_cb."""
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "junk.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "readme.txt"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="readme.txt")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    object_lines = []
    walker = m.SourceWalker(cfg, log=lines.append,
                             object_line_cb=lambda tag, path, n: object_lines.append((tag, path, n)))
    list(walker.walk())

    # object_line_cb (реальный источник консольной строки в production, см. run_analyze()/
    # _run_impl()) получил событие с 0 медиафайлов -- сигнал не потерян.
    assert any(tag == "archive" and n == 0 for tag, path, n in object_lines)
    # log() (второй, ранее дублирующий путь) для ЭТОГО архива теперь молчит -- ни разу не
    # печатает "найдено медиафайлов 0" САМ, раз это уже показал object_line_cb выше.
    assert not any("найдено медиафайлов 0" in ln for ln in lines)
    assert not any(": archive_no_media" in ln for ln in lines)
    # archive_logs (файловый archives.log/n_archives_found) по-прежнему записан -- silent
    # тушит только консоль, не саму бухгалтерию.
    assert any(status == "archive_no_media" for _display, status, _note in walker.archive_logs)


def test_extraction_log_uses_fmt_size_gb_for_tiny_archive(tmp_path):
    # Живой репорт пользователя (2026-08-01): "Распаковка X (0.0 ГБ)…" для мелких архивов --
    # ручной f"{...:.1f} ГБ" вместо уже существующей _fmt_size_gb() (уже используемой в
    # статус-строке рядом), которая даёт однозначный "<0.1ГБ".
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append)
    list(walker.walk())

    assert any("Распаковка" in ln and "<0.1ГБ" in ln for ln in lines)
    assert not any("0.0 ГБ" in ln for ln in lines)


# ---------------------------------------------------------------------------
# _quick_media_count_estimate()
# ---------------------------------------------------------------------------

def test_quick_media_count_estimate_counts_all_files_under_default_rules(tmp_path):
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    (source / "Album" / "b.jpg").write_bytes(b"x")
    (source / "readme.txt").write_bytes(b"x")  # counted too -- estimate doesn't classify type
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    assert m._quick_media_count_estimate(str(source), cfg) == 3


def test_quick_media_count_estimate_excludes_default_exclude_dirs(tmp_path):
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "junk1.jpg").write_bytes(b"x")
    (source / "node_modules" / "junk2.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    assert m._quick_media_count_estimate(str(source), cfg) == 1  # only Album/a.jpg


def test_quick_media_count_estimate_reports_progress_deltas(tmp_path):
    source = tmp_path / "source"
    (source / "A").mkdir(parents=True)
    (source / "A" / "1.jpg").write_bytes(b"x")
    (source / "A" / "2.jpg").write_bytes(b"x")
    (source / "B").mkdir(parents=True)
    (source / "B" / "3.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    deltas = []
    total = m._quick_media_count_estimate(str(source), cfg, on_progress=deltas.append)
    assert total == 3
    assert sum(deltas) == 3
    assert all(d > 0 for d in deltas)  # never called with a zero/negative delta


def test_quick_media_count_estimate_never_descends_into_target(tmp_path):
    # SOURCE=C:\ whole-disk scans can legitimately contain TARGET as a subfolder -- the
    # estimate must skip it exactly like SourceWalker._walk_dir()'s self-eating protection,
    # otherwise a big existing archive would inflate "план" for its own next run.
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    target = source / "__PhotoArchive__"
    (target / "Albums" / "old").mkdir(parents=True)
    for i in range(5):
        (target / "Albums" / "old" / f"old_{i}.jpg").write_bytes(b"x")

    cfg = _make_cfg(tmp_path, source=str(source), target=str(target))
    assert m._quick_media_count_estimate(str(source), cfg) == 1  # only Album/a.jpg, not target's 5


def test_quick_media_count_estimate_single_file_source(tmp_path):
    source = tmp_path / "single.jpg"
    source.write_bytes(b"x")
    (tmp_path / "target").mkdir()
    cfg = _make_cfg(tmp_path, source=str(source))
    assert m._quick_media_count_estimate(str(source), cfg) == 1


# ---------------------------------------------------------------------------
# SourceWalker: object_progress_cb -- "объектов X/Y" в статус-строке (живой репорт
# пользователя, 2026-08-01, заменяет [прошло/план]). ДОЛЖЕН тикать той же гранулярностью,
# что и _quick_media_count_estimate() (архив = 1, не заглядывая внутрь, любой файл = 1,
# включая не-медиа) -- иначе числитель никогда не догонит знаменатель.
# ---------------------------------------------------------------------------

def test_object_progress_ticks_once_per_file_including_non_media(tmp_path):
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    (source / "readme.txt").write_bytes(b"x")  # non-media -- must still tick
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())

    assert sum(ticks) == 2  # a.jpg + readme.txt
    assert sum(ticks) == m._quick_media_count_estimate(str(source), cfg)


def test_object_progress_ticks_once_per_archive_regardless_of_media_inside(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with tarfile.open(source / "album.tar", "w") as tf:
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            p = tmp_path / name
            p.write_bytes(b"x" * 10)
            tf.add(p, arcname=name)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())

    assert sum(ticks) == 1  # the archive itself, not its 3 internal media files
    assert sum(ticks) == m._quick_media_count_estimate(str(source), cfg)


def test_object_progress_nested_archive_does_not_add_extra_tick(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    inner_path = tmp_path / "_inner.tar"
    with tarfile.open(inner_path, "w") as tf:
        p = tmp_path / "photo.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="photo.jpg")
    with tarfile.open(source / "outer.tar", "w") as tf:
        tf.add(inner_path, arcname="inner.tar")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())

    # outer.tar only -- inner.tar (found ONLY after outer.tar is extracted) doesn't add its own
    # tick: _quick_media_count_estimate() never opens outer.tar either, so it never sees inner.tar.
    assert sum(ticks) == 1
    assert sum(ticks) == m._quick_media_count_estimate(str(source), cfg)


def test_object_progress_single_file_source_ticks_once(tmp_path):
    source = tmp_path / "single.jpg"
    source.write_bytes(b"x")
    (tmp_path / "target").mkdir()
    cfg = _make_cfg(tmp_path, source=str(source))
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == 1


# ---------------------------------------------------------------------------
# ProgressReporter(two_line=True)
# ---------------------------------------------------------------------------

def _two_line_bar(**overrides):
    return m.ProgressReporter(total=None, desc="Разбираю и копирую файлы", unit="файл",
                               two_line=True, **overrides)


def test_two_line_status_resting_operation_is_desc():
    bar = _two_line_bar()
    line = bar._build_two_line_status()
    assert line.startswith("Разбираю и копирую файлы")
    assert "всего медиа" in line
    bar.close()


def test_two_line_status_transient_op_replaces_resting_operation():
    bar = _two_line_bar()
    bar.set_transient_op("распаковка (1.5ГБ)")
    line = bar._build_two_line_status()
    assert line.startswith("распаковка (1.5ГБ)")
    bar.set_transient_op(None)
    line2 = bar._build_two_line_status()
    assert line2.startswith("Разбираю и копирую файлы")
    bar.close()


def test_two_line_status_omits_free_space_when_disk_usage_path_is_none():
    bar = _two_line_bar(disk_usage_path=None)
    line = bar._build_two_line_status()
    assert "своб." not in line
    bar.close()


def test_two_line_status_includes_free_space_when_disk_usage_path_set(tmp_path):
    bar = _two_line_bar(disk_usage_path=str(tmp_path))
    line = bar._build_two_line_status()
    assert "своб." in line
    bar.close()


def test_two_line_status_rate_is_seconds_per_file_not_files_per_second(monkeypatch):
    bar = _two_line_bar()
    bar.count = 10
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed, 10 files -> 2.00s/файл
    line = bar._build_two_line_status()
    assert "2.00s/файл" in line
    bar.close()


def test_two_line_status_shows_object_progress_with_total_estimate():
    # Живой репорт пользователя (2026-08-01): заменяет прежний [прошло/план] -- честный
    # счётчик "объектов X/Y" (X = self._obj_count, Y = total_estimate), без экстраполяции
    # времени. Обе величины -- ОДНА гранулярность (архив = 1 объект, см. add_object_progress()).
    bar = _two_line_bar(total_estimate=100)
    bar.add_object_progress(7)
    line = bar._build_two_line_status()
    assert "объектов         7/100" in line
    bar.close()


def test_two_line_status_object_progress_without_total_estimate():
    # total_estimate=None (предпересчёт недоступен/не передан) -- показываем голый счётчик
    # без "/Y", не притворяемся, что знаменатель есть.
    bar = _two_line_bar()
    bar.add_object_progress(3)
    line = bar._build_two_line_status()
    assert "объектов             3" in line
    assert "/" not in line.split("объектов")[1].split("s/файл")[0]
    bar.close()


def test_add_object_progress_accumulates_across_calls():
    bar = _two_line_bar(total_estimate=10)
    bar.add_object_progress(1)
    bar.add_object_progress(1)
    bar.add_object_progress(1)
    assert bar._obj_count == 3
    bar.close()


def test_write_object_line_format_and_no_bar_write_used(capsys):
    bar = _two_line_bar()
    assert bar._bar is None  # not a tty under pytest -- exercises the plain-print branch
    bar.write_object_line("archive", "Foto.zip", 5)
    bar.write_object_line("folder", "Album", 3)
    captured = capsys.readouterr()
    assert "  [archive] Foto.zip: найдено медиафайлов 5" in captured.err
    assert "  [папка]   Album: найдено медиафайлов 3" in captured.err
    bar.close()


def test_object_line_truncates_long_path_from_the_front(monkeypatch):
    bar = _two_line_bar()
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((80, 24)))
    long_path = "C:\\" + "x" * 200 + "\\end.jpg"
    line = bar._format_object_line("folder", long_path, 1)
    assert line.startswith("  [папка]   …")
    assert line.endswith("end.jpg: найдено медиафайлов 1")
    assert len(line) < len(long_path)
    assert len(line) <= 80  # fits the 80-column terminal set up above
    bar.close()


def test_object_line_indent_matches_source_walker_log_lines(monkeypatch):
    # Живой репорт пользователя (2026-08-01): "скачет" левый край, потому что
    # write_object_line() печатал строки БЕЗ отступа, а все self.log()-строки SourceWalker
    # ("  [archive] ...", "  [skip_marker] ...", "  Распаковка ...") -- с 2-пробельным. Обе
    # группы строк чередуются в одном и том же выводе -- отступ должен совпадать буквально.
    bar = _two_line_bar()
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((80, 24)))
    archive_line = bar._format_object_line("archive", "Foto.zip", 5)
    folder_line = bar._format_object_line("folder", "Album", 3)
    self_log_style = "  [archive] Foto.zip: archive_extracted 5 медиафайлов"
    assert archive_line[:2] == folder_line[:2] == self_log_style[:2] == "  "
    bar.close()


# ---------------------------------------------------------------------------
# _extraction_log_name_budget() -- live bug found by the user, 2026-08-01: a long archive
# name pushed "  Распаковка <имя> (X ГБ)…" past console_log()'s own line-wrap threshold
# (_wrap_console_text()/_terminal_wrap_width(), 2/3 of real terminal width) -- the wrapped
# second physical line then confused the tqdm bar's clear()/refresh() bookkeeping (which
# assumes it only ever owns exactly one row), leaving a stale visual duplicate on screen.
# ---------------------------------------------------------------------------

def test_extraction_log_name_budget_returns_large_value_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: False)
    assert m._extraction_log_name_budget() >= 200  # console_log() never wraps -- no need to shrink


def test_extraction_log_message_no_longer_wraps_at_80_columns(monkeypatch):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((80, 24)))
    archive_name = "archive-2026-07-09_20-14-47.zip"  # the exact name from the live bug report
    budget = m._extraction_log_name_budget()
    truncated = m._truncate_progress_note(archive_name, maxlen=budget)
    msg = f"  Распаковка {truncated} (6.2 ГБ)…"

    # Reproduce console_log()'s own wrapping decision (_wrap_console_text()) -- before the
    # fix, this exact message wrapped into 2 physical lines at 80 columns.
    width = m._terminal_wrap_width()
    wrapped = textwrap.wrap(msg.strip(), width=width, initial_indent="  ", subsequent_indent="    ",
                             break_long_words=False, break_on_hyphens=False)
    assert len(wrapped) == 1

    # The truncated name keeps its distinguishing tail (the date), not just noise off the front.
    assert truncated.endswith("2026-07-09_20-14-47.zip")


def test_extraction_log_message_still_wraps_without_the_fix_at_80_columns(monkeypatch):
    # Sanity-check the repro itself: the ORIGINAL (untruncated) message really did wrap at a
    # plain 80-column console -- if this stops being true, the fix above is testing nothing.
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((80, 24)))
    msg = "  Распаковка archive-2026-07-09_20-14-47.zip (6.2 ГБ)…"
    width = m._terminal_wrap_width()
    wrapped = textwrap.wrap(msg.strip(), width=width, initial_indent="  ", subsequent_indent="    ",
                             break_long_words=False, break_on_hyphens=False)
    assert len(wrapped) == 2


# ---------------------------------------------------------------------------
# ProgressReporter(two_line=True) -- "план" EMA-сглаживание (живой репорт пользователя,
# 2026-08-01: кумулятивное среднее по всей истории прогона "дёргалось" то вверх, то вниз --
# один медленный файл надолго сдвигал среднее). Контролируем time.time() напрямую, чтобы
# точно управлять "прошедшим временем" между вызовами update().
# ---------------------------------------------------------------------------

class _FakeClock:
    """time.time() stand-in that only moves when explicitly told to -- update() (and the
    _build_two_line_status() it calls internally) may call time.time() more than once per
    invocation; a clock that only advances on .advance() keeps every call within one update()
    seeing the same "now", so tests don't need to know or care how many internal calls
    happen -- only how much wall-clock time passes BETWEEN update() calls."""
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def test_ema_rate_is_none_before_any_real_update():
    bar = _two_line_bar()
    assert bar._ema_rate is None
    bar.close()


def test_ema_rate_set_after_first_real_update(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    clock.advance(2.0)  # 2s to process 1 file
    bar.update(1)
    assert bar._ema_rate == pytest.approx(2.0)
    bar.close()


def test_ema_rate_smooths_a_single_slow_outlier_file(monkeypatch):
    """A slow archive/video (20s for 1 file) should move the EMA rate noticeably, but NOT
    all the way to 20s/file the way a plain per-call rate would -- it's a weighted blend
    with whatever the running EMA already was."""
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    # Three normal ~1s files establish a baseline EMA around 1s/файл.
    for _ in range(3):
        clock.advance(1.0)
        bar.update(1)
    baseline = bar._ema_rate
    assert baseline == pytest.approx(1.0, abs=0.3)

    # One outlier: 20s for a single file.
    clock.advance(20.0)
    bar.update(1)
    after_outlier = bar._ema_rate

    # EMA moved up, but nowhere near the outlier's own 20.0 -- alpha-weighted blend, not a
    # full jump (a plain cumulative average over few files would jump much closer to it).
    assert after_outlier > baseline
    assert after_outlier < 20.0 * 0.5
    bar.close()


def test_ema_rate_recovers_after_outlier_faster_than_cumulative_average(monkeypatch):
    """The whole point of the fix: after one slow file, a few normal files should pull the
    EMA rate back down close to normal quickly -- not stay dragged up for a long time the
    way a cumulative all-history average would."""
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    clock.advance(1.0)
    bar.update(1)  # establish baseline ~1s/файл
    clock.advance(20.0)
    bar.update(1)  # one slow outlier

    # Cumulative average at this point is known exactly: 2 files, 21s elapsed -> 10.5s/файл.
    cumulative_avg = 21.0 / bar.count

    # A handful of normal ~1s files afterwards.
    for _ in range(5):
        clock.advance(1.0)
        bar.update(1)

    assert bar._ema_rate < cumulative_avg  # EMA recovered noticeably faster
    assert bar._ema_rate == pytest.approx(1.0, abs=1.5)  # back close to normal
    bar.close()


def test_update_with_n_zero_does_not_affect_rate(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    clock.advance(1.0)
    bar.update(1)
    rate_after_first = bar._ema_rate

    # A note-only update (n=0, e.g. "хеширование видеофайла (X)" shown before the blocking
    # call) must not be treated as a (very fast) file and skew the rate.
    clock.advance(0.001)
    bar.update(0, note="хеширование видеофайла (1.0ГБ)")
    assert bar._ema_rate == rate_after_first
    bar.close()


# ---------------------------------------------------------------------------
# ProgressReporter(two_line=True) -- исключение времени тяжёлых операций (распаковка архива/
# хеширование видео) из EMA-расчёта (живой репорт пользователя, 2026-08-01: реальные примеры
# плана 267ч/323ч при факте в 2-3ч -- см. _close_transient_segment()/_pending_heavy_time).
# ---------------------------------------------------------------------------

def test_set_transient_op_heavy_time_excluded_from_ema_rate(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    clock.advance(1.0)
    bar.update(1)  # baseline ~1s/файл
    baseline = bar._ema_rate
    assert baseline == pytest.approx(1.0)

    bar.set_transient_op("распаковка (2.0ГБ)")
    clock.advance(300.0)  # 5 minutes blocking extraction, no ticks during it
    bar.set_transient_op(None)
    clock.advance(1.0)  # this file's own, normal cost
    bar.update(1)

    # Without the fix this would jump toward ~150s/файл (0.3*300 + 0.7*1.0) -- with it, the
    # 300s extraction is excluded entirely, leaving the rate essentially unchanged.
    assert bar._ema_rate == pytest.approx(1.0, abs=0.05)
    bar.close()


def test_video_hashing_note_heavy_time_excluded_from_ema_rate(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    clock.advance(1.0)
    bar.update(1)  # baseline ~1s/файл
    baseline = bar._ema_rate

    note = "хеширование видеофайла (2.9ГБ)"
    bar.update(0, note=note)  # pre-mark, before the blocking hash call
    clock.advance(180.0)  # 3 minutes blocking hashing
    bar.update(1, note=note)  # post-mark, SAME note -- must not reopen a new segment

    # The video's own tick contributes ~0 (its wall time is almost entirely the excluded
    # hashing), but must NOT go negative or blow up like the un-fixed 267h/323h examples.
    assert 0.0 <= bar._ema_rate < baseline

    # The critical regression check: the NEXT, perfectly ordinary file must NOT have its own
    # real processing time swallowed by a leftover open segment from the video's note staying
    # the same across pre-mark and post-mark (see _close_transient_segment()'s docstring).
    clock.advance(1.0)
    bar.update(1, note=None)
    assert bar._ema_rate > 0.1  # would be ~0 if the bug were still there
    bar.close()


# ---------------------------------------------------------------------------
# ProgressReporter(two_line=True) -- троттлинг перерисовки статус-строки (живой репорт
# пользователя, 2026-08-01: "не нужно обновлять по каждому тику, достаточно раз в 10-20").
# ---------------------------------------------------------------------------

def test_ordinary_ticks_are_throttled(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    calls = {"n": 0}
    original = m.ProgressReporter._build_two_line_status

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(m.ProgressReporter, "_build_two_line_status", counting)
    calls["n"] = 0  # discount whatever __init__/update(0) already did
    for _ in range(m._STATUS_REFRESH_EVERY_N - 1):
        clock.advance(0.01)
        bar.update(1)
    assert calls["n"] == 0  # not yet at the threshold -- no rebuild
    clock.advance(0.01)
    bar.update(1)  # this tick crosses the threshold
    assert calls["n"] == 1
    bar.close()


def test_note_or_n_zero_updates_bypass_throttle(monkeypatch):
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    calls = {"n": 0}
    original = m.ProgressReporter._build_two_line_status

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(m.ProgressReporter, "_build_two_line_status", counting)
    calls["n"] = 0
    clock.advance(0.01)
    bar.update(1)  # ordinary tick, throttled -- no rebuild yet
    assert calls["n"] == 0
    clock.advance(0.01)
    bar.update(0, note="хеширование видеофайла (1.0ГБ)")  # note!=None -- must bypass throttle
    assert calls["n"] == 1
    clock.advance(0.01)
    bar.update(1, note="хеширование видеофайла (1.0ГБ)")  # note!=None -- must bypass throttle
    assert calls["n"] == 2
    bar.close()


def test_bare_n_zero_precursor_does_not_bypass_throttle(monkeypatch):
    """Раунд 49 ревью (REVIEW-HANDOFF.md, замечание 1): _run_impl()'s основной цикл вызывает
    bar.update(0, note=note) перед КАЖДЫМ файлом, не только видео -- для обычного фото note=None.
    До фикса голый n==0 форсировал refresh безусловно, поэтому эта самая частая в реальности пара
    (update(0, note=None) + update(1, note=None) на каждой итерации) обнуляла троттлинг на каждом
    файле -- он не срабатывал вовсе. Красный до фикса: calls["n"] был бы 200, не ~14."""
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)
    calls = {"n": 0}
    original = m.ProgressReporter._build_two_line_status

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(m.ProgressReporter, "_build_two_line_status", counting)
    calls["n"] = 0
    for _ in range(200):
        clock.advance(0.01)
        bar.update(0, note=None)  # precursor call, real code passes note=None for photos
        clock.advance(0.01)
        bar.update(1, note=None)
    assert calls["n"] < 20  # ~200/15 (throttled), not 200 (one per file, bug behavior)
    bar.close()
