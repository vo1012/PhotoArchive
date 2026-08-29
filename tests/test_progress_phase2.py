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
import zipfile

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
# _list_7z() -- 7-Zip 26.02, живой боевой прогон 2026-08-29: `l -slt` для .7z-архивов
# больше не печатает "Folder = +/-", каталог виден только по "Attributes = D". Без разбора
# этого атрибута каждая папка внутри .7z считалась файловой записью -> entries завышался ->
# archive_path_traversal_suspected (extracted_count < entries) выбрасывал ВЕСЬ .7z с папками.
# ---------------------------------------------------------------------------

_SEVENZIP_2602_7Z_SLT = """
7-Zip 26.02 (x64)  Copyright (c) 1999-2026 Igor Pavlov

Listing archive: a.7z

--
Path = a.7z
Type = 7z

----------
Path = DUB
Size = 0
Modified = 2015-09-01 15:23:37
Attributes = D
CRC =

Path = DUB\\USBDriver
Size = 0
Modified = 2015-09-01 15:23:37
Attributes = D
CRC =

Path = DUB\\photo1.jpg
Size = 100
Modified = 2015-09-01 15:21:28
Attributes = A
CRC = 3B82B2D2

Path = DUB\\USBDriver\\setup.exe
Size = 200
Modified = 2015-09-01 15:21:28
Attributes = A
CRC = AABBCCDD
"""


def test_list_7z_does_not_count_directories_as_entries_with_new_7zip_output(monkeypatch):
    class _FakeRun:
        returncode = 0
        stdout = _SEVENZIP_2602_7Z_SLT.encode("utf-8")
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _FakeRun())

    info = m._list_7z("a.7z")
    assert info.ok
    assert info.entries == 2          # два файла, НЕ 4 -- две папки (Attributes = D) не в счёт
    assert info.media_count == 1      # только photo.jpg
    assert not info.path_traversal


def test_list_7z_still_reads_folder_line_for_zip(monkeypatch):
    # .zip у 7-Zip 26.02 по-прежнему печатает "Folder = +/-"; ветка Attributes ей не мешает.
    zip_slt = (
        "----------\n"
        "Path = d\nFolder = +\nSize = 0\nAttributes = D\n\n"
        "Path = d\\a.jpg\nFolder = -\nSize = 10\nAttributes = A\n\n"
        "Path = d\\b.jpg\nFolder = -\nSize = 20\nAttributes = A\n"
    )

    class _FakeRun:
        returncode = 0
        stdout = zip_slt.encode("utf-8")
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _FakeRun())

    info = m._list_7z("d.zip")
    assert info.entries == 2
    assert info.media_count == 2


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
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, n)))
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
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, n)))
    list(walker.walk())

    assert not any("node_modules" in c[1] for c in calls)


def test_object_line_cb_shows_placement_letter_only_when_enabled(tmp_path):
    """Задача пользователя, 2026-08-09 (--dry-run/[3] реальная сборка): буква "A" (альбом) --
    у обычной, узнаваемой альбомной папки; "D" (по дате) -- у dump-именованной ("downloads").
    show_placement_letter -- ТОЛЬКО --dry-run/реальная сборка (по прямой просьбе пользователя),
    не analyze -- letter остаётся "" (пустой), если флаг не передан вовсе (значение по
    умолчанию)."""
    source = tmp_path / "source"
    (source / "MyAlbum").mkdir(parents=True)
    (source / "MyAlbum" / "a.jpg").write_bytes(b"x")
    (source / "downloads").mkdir()
    (source / "downloads" / "b.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()
    cfg = _make_cfg(tmp_path)

    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, show_placement_letter=True,
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, letter)))
    list(walker.walk())
    by_path = {path: letter for _tag, path, letter in calls}
    assert by_path[str(source / "MyAlbum")] == "A"
    assert next(letter for path, letter in by_path.items() if path.endswith("downloads")) == "D"

    calls_off = []
    walker_off = m.SourceWalker(cfg, log=lambda *a, **k: None,
                                 object_line_cb=lambda tag, path, n, letter="": calls_off.append(letter))
    list(walker_off.walk())
    assert all(letter == "" for letter in calls_off)  # флаг не передан -- старое поведение


def test_handle_archive_object_line_shows_placement_letter_when_enabled(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "MyAlbum.zip", "w") as zf:
        zf.writestr("a.jpg", b"x" * 100)
    (tmp_path / "target").mkdir()
    cfg = _make_cfg(tmp_path)

    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, show_placement_letter=True,
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, letter)))
    list(walker.walk())
    archive_calls = [c for c in calls if c[0] == "archive"]
    assert len(archive_calls) == 1
    assert archive_calls[0][2] == "A"  # "MyAlbum" -- узнаваемое имя альбома, не dump


def test_object_line_cb_deferred_stray_folder_announces_at_processing_not_discovery(tmp_path):
    # Живой боевой прогон, речь пользователя 2026-08-07 ("[папка] F:\\1: найдено медиафайлов
    # 62... но в архиве альбом не создан, в ByDate ещё не легло -- он считает, что то, что
    # прошло через экран, уже обработано"): "BBB_loose" ниже -- папка без узнаваемого альбома,
    # её файлы откладываются на Фазу 3 (_deferred_stray_files) -- строка "[папка] ...: найдено
    # медиафайлов 2" не должна печататься раньше, чем Фаза 3 реально возьмётся за первый её
    # файл (принцип "начинает отрабатывать", согласован с пользователем -- НЕ "увидели при
    # обходе Фазы 1", но и не "все файлы папки уже обработаны").
    #
    # "downloads" -- узнаваемое dump-имя (find_album() отдаёт None для него, в отличие от
    # обычного альбома, см. is_dump_segment()) -- её файлы откладываются на Фазу 3. Третья
    # папка ("zzz_album2", тоже настоящий альбом) НУЖНА для теста, не для сценария
    # пользователя: без неё проверка не различала бы старое и новое поведение -- Фаза 1 в
    # любом случае не успевает объявить "downloads" РАНЬШЕ, чем выдаст единственный файл
    # ПРЕДЫДУЩЕЙ папки (тот уже выдан до того, как обход дошёл до "downloads" в стеке). С
    # третьей папкой разница видна: под СТАРЫМ кодом Фаза 1 успевает объявить "downloads"
    # (просто увидев её при обходе) ДО того, как выдаст файл ИЗ СЛЕДУЮЩЕЙ папки "zzz_album2" --
    # хотя ни один файл "downloads" ещё не тронут. Алфавитный порядок имён -- "aaa" < "downloads"
    # < "zzz" -- гарантирует именно такой порядок обхода (sorted(os.listdir())).
    source = tmp_path / "source"
    (source / "aaa_album1").mkdir(parents=True)
    (source / "aaa_album1" / "photo1.jpg").write_bytes(b"x")
    (source / "downloads").mkdir()
    (source / "downloads" / "a.jpg").write_bytes(b"x")
    (source / "downloads" / "b.jpg").write_bytes(b"x")
    (source / "zzz_album2").mkdir()
    (source / "zzz_album2" / "photo2.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    calls = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, n)))

    def loose_already_announced():
        return any(c[1].endswith("downloads") for c in calls)

    seen = []
    for item in walker.walk():
        seen.append((item.rel_path, loose_already_announced()))

    names = [n for n, _ in seen]
    assert names == ["aaa_album1/photo1.jpg", "zzz_album2/photo2.jpg",
                      "downloads/a.jpg", "downloads/b.jpg"], names

    # Ключевая проверка: в момент выдачи photo2.jpg (СЛЕДУЮЩАЯ после downloads папка,
    # обойдённая Фазой 1) "downloads" ЕЩЁ НЕ должна быть объявлена -- ни один её файл не
    # тронут, Фаза 3 ещё даже не началась.
    assert seen[0][1] is False, seen  # photo1.jpg -- до downloads в обходе, тем более рано
    assert seen[1][1] is False, seen  # photo2.jpg -- ПОСЛЕ downloads в обходе, но объявления быть не должно
    # К моменту выдачи ПЕРВОГО файла downloads -- объявление уже появилось (Фаза 3 реально
    # взялась за эту папку).
    assert seen[2][1] is True, seen
    assert seen[3][1] is True, seen  # второй файл той же папки, повторного объявления нет

    loose_calls = [c for c in calls if c[1].endswith("downloads")]
    assert len(loose_calls) == 1  # ровно один раз на папку, не на каждый файл
    assert loose_calls[0] == ("folder", str(source / "downloads"), 2)


# ---------------------------------------------------------------------------
# SourceWalker: DVD-юниты (VIDEO_TS) -- 2026-08-07, по итогам боевого прогона (домашнее видео
# на DVD не попадало в архив) целая папка VIDEO_TS теперь копируется как один неделимый юнит
# (см. секцию "DVD-VIDEO UNITS" в photosort_win.py) вместо старого поведения "обнаружен, но не
# скопирован". Эти тесты бьют по walker.walk() напрямую (не через run()) -- проверяют
# детекцию/fingerprint/yield SourceItem, не реальное копирование на диск (это отдельно, см.
# test_dvd_unit_build.py).
# ---------------------------------------------------------------------------

def test_dvd_unit_yields_items_with_forced_dest_and_records_as_copied(tmp_path):
    source = tmp_path / "source"
    disc = source / "Some_Movie_DVD5"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert len(items) == 1
    item = items[0]
    assert item.dvd_dest_path is not None
    assert item.dvd_dest_path.endswith(
        m.os.path.join("Albums", "Some_Movie_DVD5", "VIDEO_TS", "VTS_01_0.VOB"))
    assert item.dvd_sha256 == m.sha256_file(str(disc / "VIDEO_TS" / "VTS_01_0.VOB"))

    assert len(walker.dvd_units_copied) == 1
    unit = walker.dvd_units_copied[0]
    assert unit["name"] == "Some_Movie_DVD5"
    assert unit["n_files"] == 1
    assert unit["total_bytes"] == 10
    assert walker.dvd_units_skipped_duplicate == []


def test_dvd_unit_log_line_shows_placement_letter_when_enabled(tmp_path):
    """Дополнение пользователя, 2026-08-09, к A/D-буквам после [папка]/[archive]: "[DVD]"
    -- та же буква, тот же принцип (сразу после "]", один пробел перед текстом). "Some_Movie_
    DVD5" -- узнаваемое имя альбома (не dump), letter="A"; --dry-run/[3] реальная сборка
    только (show_placement_letter), не analyze -- letter="" по умолчанию."""
    source = tmp_path / "source"
    disc = source / "Some_Movie_DVD5"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append, show_placement_letter=True)
    list(walker.walk())
    assert any("[DVD]A новый DVD-диск ->" in ln for ln in lines), lines

    lines_off = []
    walker_off = m.SourceWalker(cfg, log=lines_off.append)
    disc2 = source / "Some_Movie_DVD6"
    (disc2 / "VIDEO_TS").mkdir(parents=True)
    (disc2 / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    list(walker_off.walk())
    assert any("[DVD] новый DVD-диск ->" in ln for ln in lines_off), lines_off
    assert not any("[DVD]A" in ln or "[DVD]D" in ln for ln in lines_off)


def test_dvd_unit_log_line_truncates_long_paths_instead_of_wrapping(tmp_path, monkeypatch):
    """Живой боевой прогон 2026-08-28: строка «[DVD]D новый DVD-диск -> <dest> (N файлов,
    <src>)» уходит в write_heavy_notice() с ДВУМЯ длинными путями и без обрезки под ширину --
    реальный DVD-путь рвался посреди слова. Фикс (как у _log_archive в 86f2b2f): disp_base под
    фикс-кап, dest_dir под остаток ширины, wrap только если всё равно не влезло в окно."""
    # Узкий «терминал» + маленький бюджет пути (в pytest sys.stderr.isatty() == False, поэтому
    # _console_tag_line_budget() иначе всегда вернул бы 80 -- подменяем оба).
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 60)
    monkeypatch.setattr(m, "_console_tag_line_budget", lambda tail_len, **kw: 20)

    source = tmp_path / "source"
    disc = source / "a_very_deeply_nested_folder_name" / "and_another_one_here" / "downloads" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append, show_placement_letter=True)
    list(walker.walk())

    dvd_lines = [ln for ln in lines if "новый DVD-диск ->" in ln]
    assert len(dvd_lines) == 1
    ln = dvd_lines[0].lstrip("\n")
    assert "\n" not in ln                    # одна физическая строка, не перенос
    assert "…" in ln                         # длинные пути реально обрезаны
    assert ln.startswith("  [DVD]D новый DVD-диск -> ")


def test_dvd_unit_log_line_letter_is_date_when_no_album_found(tmp_path):
    source = tmp_path / "source"
    disc = source / "downloads"  # известное dump-имя -- find_album() не найдёт альбом
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append, show_placement_letter=True)
    list(walker.walk())
    assert any("[DVD]D новый DVD-диск ->" in ln for ln in lines), lines


def test_dvd_unit_detection_is_case_insensitive(tmp_path):
    # Folder name case and extension case both vary in the wild -- neither should matter.
    source = tmp_path / "source"
    disc = source / "lowercase_disc"
    (disc / "video_ts").mkdir(parents=True)
    (disc / "video_ts" / "VTS_01_0.vob").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert len(items) == 1
    assert walker.dvd_units_copied[0]["name"] == "lowercase_disc"


def test_dvd_unit_not_detected_without_vob_ifo_bup(tmp_path):
    source = tmp_path / "source"
    disc = source / "Not_A_DVD"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "readme.txt").write_bytes(b"x")  # no .vob/.ifo/.bup -- not a real DVD
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    # readme.txt -- "other" file type, normal per-file pipeline drops it silently (see
    # _walk_dir()) -- not a DVD unit AND not yielded as a regular item either.
    assert items == []
    assert walker.dvd_units_copied == []


def test_vob_file_outside_video_ts_folder_goes_through_normal_video_pipeline(tmp_path):
    # 2026-08-07, по требованию пользователя: отдельностоящий .vob (НЕ внутри VIDEO_TS) --
    # обычный видеофайл (VIDEO_EXTS теперь включает "vob"), не DVD-юнит. Папка VIDEO_TS --
    # сигнал для DVD-обработки, не голое расширение .vob само по себе.
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "clip.vob").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert walker.dvd_units_copied == []
    assert len(items) == 1
    assert items[0].dvd_dest_path is None  # обычный item, не форсированное DVD-размещение
    assert items[0].ftype == "video"


def test_dvd_unit_detected_via_ifo_without_vob(tmp_path):
    source = tmp_path / "source"
    disc = source / "Menu_Only_DVD"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert len(items) == 1
    assert walker.dvd_units_copied[0]["name"] == "Menu_Only_DVD"


def test_dvd_unit_duplicate_is_skipped_not_reyielded(tmp_path):
    """Прямое требование пользователя, 2026-08-07: "объединение DVD-папок недопустимо" --
    юнит, чей fingerprint уже есть в реестре (переданном как dvd_unit_registry, см.
    сохранение/чтение таблицы dvd_units в _run_impl()), пропускается ЦЕЛИКОМ -- ни одного
    SourceItem не yield'ится, ничего не дописывается."""
    source = tmp_path / "source"
    disc = source / "Disc1"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    records = m._dvd_unit_file_records(str(disc / "VIDEO_TS"))
    fingerprint = m._dvd_unit_fingerprint(records)

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             dvd_unit_registry={fingerprint: r"D:\Target\Albums\Disc1\VIDEO_TS"})
    items = list(walker.walk())

    assert items == []
    assert walker.dvd_units_copied == []
    assert walker.dvd_units_skipped_duplicate == [
        {"name": "Disc1", "dest_path": r"D:\Target\Albums\Disc1\VIDEO_TS"}]


def test_dvd_unit_duplicate_log_line_truncates_long_path_instead_of_wrapping(tmp_path, monkeypatch):
    """Живой репорт 2026-08-29: «[DVD] дубль уже архивированного диска, пропущен: <path>» была
    единственной из [DVD]/[archive]/[папка]-строк без обрезки пути под ширину -- на реальном
    DVD-пути рвалась посреди слова. Тот же приём, что у «[DVD] новый DVD-диск ->» рядом."""
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 60)
    monkeypatch.setattr(m, "_console_tag_line_budget", lambda tail_len, **kw: 20)

    source = tmp_path / "source"
    disc = source / "a_very_deeply_nested_folder" / "with_a_long_name_inside" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VTS_01_0.VOB").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    records = m._dvd_unit_file_records(str(disc))
    fingerprint = m._dvd_unit_fingerprint(records)
    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append,
                             dvd_unit_registry={fingerprint: r"D:\T\Albums\x\VIDEO_TS"})
    list(walker.walk())

    dup_lines = [ln for ln in lines if "дубль уже архивированного диска" in ln]
    assert len(dup_lines) == 1
    ln = dup_lines[0].lstrip("\n")
    assert "\n" not in ln     # одна физическая строка
    assert "…" in ln          # длинный путь реально обрезан
    assert ln.startswith("  [DVD] дубль уже архивированного диска, пропущен: ")


def test_dvd_unit_file_records_calls_progress_cb_per_file(tmp_path):
    """REVIEW-HANDOFF.md Раунд 148, замечание 2: фингерпринт DVD-юнита хеширует все VOB подряд
    (гигабайты) без опроса паузы по пробелу -- progress_cb пробрасывается в _dvd_unit_file_records()
    и дальше в sha256_file(), вызывается хотя бы раз на файл."""
    vts = tmp_path / "VIDEO_TS"
    vts.mkdir()
    for n in ("VTS_01_0.VOB", "VTS_01_1.VOB", "VIDEO_TS.IFO"):
        (vts / n).write_bytes(b"x" * 32)

    calls = []
    records = m._dvd_unit_file_records(str(vts), progress_cb=lambda: calls.append(1))

    assert len(records) == 3
    assert len(calls) >= 3  # >= (по разу на файл + по чанку внутри sha256_file крупного файла)


def test_dvd_unit_name_collision_with_existing_target_content_gets_suffixed(tmp_path):
    # Реалистичный коллизионный сценарий: альбом "Video"/VIDEO_TS уже существует в TARGET (от
    # более раннего прогона/другого источника) -- НОВЫЙ (другое содержимое, другой fingerprint)
    # диск с тем же альбомным именем "Video" не должен ни слиться с уже стоящей там VIDEO_TS,
    # ни провалиться -- заводит "VIDEO_TS (2)" рядом, прямое требование пользователя
    # ("объединение DVD-папок недопустимо").
    source = tmp_path / "source"
    disc = source / "Video" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VTS_01_0.VOB").write_bytes(b"new_content")
    target = tmp_path / "target"
    existing = target / "Albums" / "Video" / "VIDEO_TS"
    existing.mkdir(parents=True)
    (existing / "VTS_01_0.VOB").write_bytes(b"old_content_from_earlier_run")

    cfg = _make_cfg(tmp_path, target=str(target))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert len(walker.dvd_units_copied) == 1
    dest = walker.dvd_units_copied[0]["dest_path"]
    assert m.os.path.basename(dest) == "VIDEO_TS (2)"
    assert m.os.path.dirname(dest) == str(target / "Albums" / "Video")


def test_dvd_unit_inside_archive_hashes_before_tmp_extract_cleanup(tmp_path):
    """Раунд 49 ревью (REVIEW-HANDOFF.md, замечание 2), тот же класс бага, что уже чинился для
    запароленных вложенных архивов (Раунды 45/47) -- VIDEO_TS найден ВНУТРИ архива, физически
    живёт под cfg.tmp_extract, удаляется cleanup_dir() до конца walk(). _handle_dvd_unit()
    хеширует файлы СИНХРОННО в момент обнаружения (см. _dvd_unit_file_records() внутри неё) --
    этот тест реально прогоняет весь walk() и проверяет, что sha256 посчитан успешно (не
    FileNotFoundError), а имя юнита взято из папки-контейнера ВНУТРИ архива ("vacation"), не
    голой "VIDEO_TS"."""
    import zipfile

    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "vacation.zip", "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)  # real media -- archive gets extracted
        zf.writestr("VIDEO_TS/VTS_01_0.VOB", b"v" * 100)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    dvd_items = [it for it in items if it.dvd_dest_path is not None]
    assert len(dvd_items) == 1
    assert dvd_items[0].dvd_sha256 == m.sha256_bytes(b"v" * 100)  # same bytes -> same digest
    assert walker.dvd_units_copied[0]["name"] == "vacation"


def test_dvd_unit_inside_nested_archive_subdir_uses_container_name(tmp_path):
    """Same bug class as above, one directory level deeper inside the archive."""
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

    assert walker.dvd_units_copied[0]["name"] == "Disk1"


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
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, n)))
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
                             object_line_cb=lambda tag, path, n, letter="": calls.append((tag, path, n)))
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

    assert any(op is not None and "Извлекаю" in op for op in ops)
    assert ops[-1] is None  # cleared again after extraction, whether it succeeded or not


def test_log_own_line_uses_heavy_notice_cb_without_leading_newline(tmp_path):
    """Живая находка пользователя, 2026-08-24 (третий заход -- фикс "\\n" устранил склейку, но
    открыл дублирование/"уезжание вверх" статус-строки, т.к. бар не переиспользовал свою
    строку): heavy_notice_cb -- прямая ссылка на бар (ProgressReporter.write_heavy_notice()),
    вызывается с СЫРЫМ текстом без добавленного "\n" -- сам bar.clear() перед печатью уже
    гарантирует чистую строку, второй перевод строки был бы лишним пустым отступом."""
    source = tmp_path / "source"
    source.mkdir()
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    notices = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             heavy_notice_cb=lambda line, wrap=True: notices.append(line))
    list(walker.walk())

    assert notices  # at least the extraction notice fired
    assert any("Распаковка" in n for n in notices)
    assert not any(n.startswith("\n") for n in notices)


def test_log_own_line_falls_back_to_log_with_leading_newline_without_cb(tmp_path):
    """Симметричный случай -- без heavy_notice_cb (analyze-режимы без two_line-бара и т.п.):
    старое поведение (self.log("\\n" + msg)) остаётся как есть, координировать не с чем, но и
    склеить с чем-то на экране тоже, безопасный фолбэк."""
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

    extraction_lines = [ln for ln in lines if "Распаковка" in ln]
    assert extraction_lines
    assert all(ln.startswith("\n") for ln in extraction_lines)


def test_handle_archive_shows_razbor_arhiva_during_content_walk(tmp_path):
    """Живая находка пользователя, 2026-08-19 (боевой прогон): "распаковка (X ГБ)" гасилась в
    None СРАЗУ после самой физической распаковки -- дальнейший обход распакованного содержимого
    (у архива с горой вложенных файлов -- самая долгая часть прогона) не показывал в поле
    операции НИЧЕГО, откатывался на статичный resting-текст, хотя "обработано объектов %" тем
    временем честно стоит на месте (архив тикает одним объектом только по завершении всего
    содержимого). Теперь между "распаковка" и финальным None должно быть "разбор архива"."""
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

    # Не точная последовательность -- "Смотрю" (_open_deferred_gap(), существующая, отдельная
    # механика) законно перемежается с "В архиве" (она же -- фолбэк _close_deferred_gap(), пока
    # обход внутри архива, см. её докстрин): важно, что "В архиве" реально появляется, и что
    # поле гасится в None РОВНО ОДИН раз -- в самом конце, а не где-то посреди обхода.
    assert m._ARCHIVE_CONTENT_TRANSIENT_OP in ops
    assert ops.count(None) == 1
    assert ops[-1] is None


def test_handle_archive_razbor_arhiva_persists_through_nested_archive(tmp_path):
    """Живая находка пользователя, 2026-08-19 (боевой прогон, архив с гигантским количеством
    вложенных файлов/вложенных архивов внутри): "разбор архива" должен держаться ВЕСЬ обход
    распакованного содержимого внешнего архива, включая обработку вложенных архивов внутри --
    не гаситься в None на завершении КАЖДОГО вложенного архива (это стёрло бы пометку внешнего,
    хотя его собственная обработка ещё не закончена). Счётчик глубины
    (SourceWalker._archive_walk_depth) должен гасить transient_op ровно один раз -- когда
    завершается самый внешний архив, не раньше."""
    source = tmp_path / "source"
    source.mkdir()
    nested_zip = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested_zip, "w") as zf:
        zf.writestr("b.jpg", b"y" * 10)
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
        tf.add(nested_zip, arcname="nested.zip")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ops = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, transient_op_cb=ops.append)
    list(walker.walk())

    # "В архиве" может законно повториться (фолбэк _close_deferred_gap(), см. её докстрин) --
    # ключевая гарантия не "ровно один раз", а что None НЕ появляется, пока обработка вложенного
    # архива идёт внутри внешнего: поле гасится в None РОВНО ОДИН раз -- только когда весь
    # внешний архив (включая вложенный) уже полностью обработан.
    assert m._ARCHIVE_CONTENT_TRANSIENT_OP in ops
    assert ops.count(None) == 1
    assert ops[-1] is None
    assert ops.index(m._ARCHIVE_CONTENT_TRANSIENT_OP) < ops.index(None)


def test_handle_archive_extracted_log_line_suppressed_when_count_matches_listing(tmp_path):
    # SESSION-HANDOFF.txt п.6 (2026-08-05, боевой прогон): симметрично с уже исправленной
    # archive_no_media (0==0 подавлен, живой репорт 2026-08-02) -- write_object_line() уже
    # напечатал предварительное число из листинга ДО распаковки; когда подтверждённое после
    # реальной распаковки число СОВПАДАЕТ с ним -- вторая строка ("распаковано, найдено
    # медиафайлов N") больше не печатается, это был бы буквальный повтор.
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

    assert not any("распаковано, найдено медиафайлов" in ln for ln in lines)
    assert not any(": archive_extracted" in ln for ln in lines)  # old "status note" style is gone
    # archives.log/n_archives_found по-прежнему видят событие -- silent тушит только консоль.
    assert any(status == "archive_extracted" for _, status, _ in walker.archive_logs)


def test_handle_archive_extracted_log_line_shown_when_count_differs_from_listing(tmp_path):
    # Листинг архива не заглядывает ВНУТРЬ вложенных архивов (info.media_count считает только
    # ARCHIVE_EXTS-независимые расширения, см. _member_name_is_strict_media()) -- но после
    # реальной распаковки обход спускается и во вложенный zip тоже, находя больше медиа, чем
    # предварительно показал write_object_line(). Расхождение -- НЕ повтор, вторая строка
    # должна печататься с новым, уточнённым числом.
    source = tmp_path / "source"
    source.mkdir()
    nested_zip = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested_zip, "w") as zf:
        zf.writestr("b.jpg", b"y" * 10)
    tar_path = source / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        p = tmp_path / "a.jpg"
        p.write_bytes(b"x" * 10)
        tf.add(p, arcname="a.jpg")
        tf.add(nested_zip, arcname="nested.zip")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append)
    list(walker.walk())

    assert any("[archive] " in ln and "album.tar: распаковано, найдено медиафайлов 2" in ln
               for ln in lines)


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
                             object_line_cb=lambda tag, path, n, letter="": object_lines.append((tag, path, n)))
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

def test_quick_media_count_estimate_counts_only_media_candidates(tmp_path):
    # 2026-08-17: readme.txt ("other" file_type) no longer counted -- see docstring, source
    # dominated by non-media files used to drag "обработано объектов %" to 100% almost instantly.
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    (source / "Album" / "b.jpg").write_bytes(b"x")
    (source / "readme.txt").write_bytes(b"x")  # not counted -- "other" file_type
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    assert m._quick_media_count_estimate(str(source), cfg) == 2


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


def test_quick_media_count_estimate_counts_video_ts_unit_as_one(tmp_path):
    # SESSION-HANDOFF.txt, 2026-08-09 (боевой прогон, вторая находка): раньше оценка спускалась
    # ВНУТРЬ VIDEO_TS и считала каждый .vob/.ifo/.bup отдельно, тогда как реальный обход
    # (_tick_object()) тикает VIDEO_TS-юнит как ОДНО целое -- X (реальные тики) никогда не
    # догонял Y (эта оценка) на источниках с DVD-рипами. Синтетика: 5 фото в обычной папке + 1
    # VIDEO_TS с 3 файлами -- тот же случай, что test_object_progress_dvd_unit_ticks_once_as_a_whole.
    source = tmp_path / "source"
    (source / "Photos").mkdir(parents=True)
    for i in range(5):
        (source / "Photos" / f"{i}.jpg").write_bytes(b"x")
    disc = source / "Video"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"i" * 20)
    (disc / "VIDEO_TS" / "VIDEO_TS.BUP").write_bytes(b"i" * 20)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    y = m._quick_media_count_estimate(str(source), cfg)
    assert y == 6  # 5 фото + 1 VIDEO_TS-юнит целиком, не +3 отдельных файла внутри него

    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == y  # X (реальные тики) == Y (оценка), это и есть цель фикса


def test_quick_media_count_estimate_skips_skip_marker_folder_entirely(tmp_path):
    # Та же находка, вторая причина расхождения: реальный обход пропускает SKIP_MARKER-папку
    # целиком (0 тиков, см. _walk_dir()), прежняя оценка о разметке SKIP_MARKER не знала вообще
    # и считала её файлы.
    source = tmp_path / "source"
    (source / "Photos").mkdir(parents=True)
    (source / "Photos" / "a.jpg").write_bytes(b"x")
    skipped = source / "Skipped"
    skipped.mkdir()
    (skipped / m.SKIP_MARKER).write_bytes(b"")
    (skipped / "hidden1.jpg").write_bytes(b"x")
    (skipped / "hidden2.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    y = m._quick_media_count_estimate(str(source), cfg)
    assert y == 1  # только Photos/a.jpg -- Skipped/* не считается вовсе

    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == y


def test_quick_media_count_estimate_skip_marker_at_root_does_not_skip_root(tmp_path):
    # _walk_dir(): SKIP_MARKER пропускает папку целиком, КРОМЕ самого корня SOURCE (ПРАВИЛО
    # ЯВНОГО УКАЗАНИЯ) -- оценка должна повторять то же исключение, не пропускать SOURCE
    # целиком только потому, что маркер лежит прямо в его корне.
    source = tmp_path / "source"
    source.mkdir()
    (source / m.SKIP_MARKER).write_bytes(b"")
    (source / "a.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    # Корень не пропускается -- но сам SKIP_PHOTOSORT.txt не media-кандидат (2026-08-17,
    # оценка теперь классифицирует по расширению) -- считается только a.jpg.
    y = m._quick_media_count_estimate(str(source), cfg)
    assert y == 1

    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == y


# ---------------------------------------------------------------------------
# SourceWalker: object_progress_cb -- "объектов X/Y" в статус-строке (живой репорт
# пользователя, 2026-08-01, заменяет [прошло/план]). ДОЛЖЕН тикать той же гранулярностью,
# что и _quick_media_count_estimate() (архив = 1, не заглядывая внутрь, media-кандидат = 1,
# non-media НЕ считается вовсе, см. 2026-08-17) -- иначе числитель никогда не догонит
# знаменатель.
# ---------------------------------------------------------------------------

def test_object_progress_ignores_non_media_files(tmp_path):
    # 2026-08-17: readme.txt больше не тикает -- источник, где немедийные файлы численно
    # доминируют (боевой прогон), раньше доводил X до Y почти сразу, задолго до реальной
    # обработки медиафайлов в остальном дереве (клэмп min(X/Y*100, 100.0) держал 100% весь
    # остаток прогона).
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "a.jpg").write_bytes(b"x")
    (source / "readme.txt").write_bytes(b"x")  # non-media -- must NOT tick
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())

    assert sum(ticks) == 1  # a.jpg only, not readme.txt
    assert sum(ticks) == m._quick_media_count_estimate(str(source), cfg)


def test_object_progress_bare_gz_not_counted_in_x_or_y(tmp_path):
    """Раунд 159 ревью: фикс 0.6.4 (_walk_dir() пропускает бэйр .gz/.bz2 как "other") разъехал
    знаменатель -- _quick_media_count_estimate() всё ещё считал их (file_type()=="archive"),
    _walk_dir() уже не тикал. Триггер -- ровно `.sync/core-*.log.gz` YandexDisk. X и Y обязаны
    совпасть."""
    source = tmp_path / "source"
    sync = source / "OLD" / ".sync"
    sync.mkdir(parents=True)
    (source / "photo.jpg").write_bytes(b"x")
    for i in range(5):
        (sync / f"core-{i}.log.gz").write_bytes(b"x")
    (sync / "journal.sql.bz2").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    assert m._quick_media_count_estimate(str(source), cfg) == 1  # только photo.jpg

    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == 1
    assert sum(ticks) == m._quick_media_count_estimate(str(source), cfg)


def test_object_progress_real_tar_gz_still_counted(tmp_path):
    # контроль: настоящий .tar.gz по-прежнему тикает как 1 объект и в X, и в Y
    import io
    import tarfile
    source = tmp_path / "source"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"x")
    with tarfile.open(source / "album.tar.gz", "w:gz") as tf:
        data = b"\xff\xd8\xff\xe0jpegish"
        ti = tarfile.TarInfo("inner.jpg")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    assert m._quick_media_count_estimate(str(source), cfg) == 2  # photo.jpg + album.tar.gz


def test_object_progress_junk_heavy_folder_does_not_move_percent_before_real_media_is_reached(tmp_path):
    """Живой боевой прогон, 2026-08-17: источник с папкой из тысяч мелких немедийных файлов
    (обходится/дисквалифицируется мгновенно, но реально требует времени на сам обход папки)
    показывал "обработано объектов 100%" задолго до конца прогона -- эта папка численно
    доминировала и в X, и в Y (см. докстрины _tick_object()/_quick_media_count_estimate()),
    хотя реальные (медленные, exif/hash) медиафайлы в других папках дерева ещё не были
    обработаны ни разу. red-before-green: до фикса Y == 2005 (2000 junk + 5 фото), а X
    (после полного обхода Junk, до первого реального фото) уже был бы 2000 -- 99.75%,
    процент вплотную к 100% раньше, чем обработан хоть один реальный файл. После фикса Junk не
    входит ни в X, ни в Y вовсе -- Y == 5, X == 0 в той же точке."""
    source = tmp_path / "source"
    junk = source / "AAA_Junk"  # walked first (alphabetically before the photo folders below)
    junk.mkdir(parents=True)
    for i in range(2000):
        (junk / f"tile{i:04d}.dat").write_bytes(b"x")
    photos1 = source / "BBB_Photos1"
    photos1.mkdir()
    for i in range(3):
        (photos1 / f"img{i:02d}.jpg").write_bytes(b"x")
    photos2 = source / "CCC_Photos2"
    photos2.mkdir()
    for i in range(2):
        (photos2 / f"img{i:02d}.jpg").write_bytes(b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    total_estimate = m._quick_media_count_estimate(str(source), cfg)
    assert total_estimate == 5  # 3 + 2 photos -- 2000 junk files no longer inflate Y

    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    items = walker.walk()
    first_item = next(items)
    # AAA_Junk (2000 files) is fully walked before BBB_Photos1's first item is ever yielded
    # (alphabetical traversal) -- if junk still ticked, sum(ticks) would already be ~2000 here.
    assert first_item.rel_path == "BBB_Photos1/img00.jpg", first_item.rel_path
    assert sum(ticks) == 0, sum(ticks)
    list(items)  # drain the rest
    assert sum(ticks) == total_estimate == 5


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


# 2026-08-07, живой боевой прогон пользователя (F:→D:, "объектов 5577/27918 | всего медиа
# 2476 -- а что в остальных?"): тик должен происходить в момент ЗАВЕРШЕНИЯ разбора объекта
# (сразу после yield/yield-from, когда вызывающий код уже полностью его обработал и вернулся
# за следующим), не в момент, когда walker впервые увидел имя при обходе -- иначе объекты,
# отложенные Фазой 1 на Фазу 2/3 (тильда-архивы/файлы без альбома), тикают ДО того, как их
# реально обработали, и счётчик убегает далеко вперёд "всего медиа". Ниже -- тесты именно на
# ПОРЯДОК/МОМЕНТ тика (не только итоговую сумму, ту уже проверяют тесты выше и она не менялась).

def test_object_progress_deferred_stray_file_ticks_after_yield_not_before(tmp_path):
    source = tmp_path / "source"
    (source / "Album").mkdir(parents=True)
    (source / "Album" / "photo1.jpg").write_bytes(b"x")
    (source / "stray.jpg").write_bytes(b"x")  # корень SOURCE -- альбом не находится, откладывается
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)

    seen = []
    for item in walker.walk():
        seen.append((item.rel_path, sum(ticks)))

    names = [n for n, _ in seen]
    assert names == ["Album/photo1.jpg", "stray.jpg"], names  # альбомный контент первым (Фаза 1)
    # В момент выдачи КАЖДОГО item тик за НЕГО САМОГО ещё не должен был сработать -- он
    # срабатывает только после того, как вызывающий код заберёт item и генератор продолжится.
    assert seen[0][1] == 0, seen  # photo1.jpg выдан -- ни один тик ещё не произошёл
    assert seen[1][1] == 1, seen  # stray.jpg выдан -- тик есть только за photo1.jpg, НЕ за себя
    assert sum(ticks) == 2  # оба тика в итоге случились


def test_object_progress_deferred_tilde_archive_ticks_once_after_its_content_not_during_phase1(tmp_path):
    source = tmp_path / "source"
    album = source / "RealAlbum"
    album.mkdir(parents=True)
    (album / "normal.jpg").write_bytes(b"x")
    with zipfile.ZipFile(album / "~backup.zip", "w") as zf:
        for name in ("p1.jpg", "p2.jpg", "p3.jpg"):
            zf.writestr(name, b"x")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)

    seen = []
    for item in walker.walk():
        seen.append((item.rel_path, sum(ticks)))

    names = [n for n, _ in seen]
    assert names[0] == "RealAlbum/normal.jpg", names  # альбомный файл первым (Фаза 1)
    archive_item_names = names[1:]
    assert len(archive_item_names) == 3  # p1/p2/p3 -- все три из ~backup.zip, отложены на Фазу 2

    assert seen[0][1] == 0, seen  # normal.jpg выдан -- тиков ещё нет
    # Пока идут файлы ИЗ архива -- тика за сам архив ещё быть не должно (архив тикает как ОДНО
    # целое, только когда его контент полностью обработан, не по файлам внутри и не заранее).
    for name, ticks_so_far in seen[1:]:
        assert ticks_so_far == 1, (name, ticks_so_far, seen)
    assert sum(ticks) == 2  # normal.jpg (1) + ~backup.zip как единое целое (1), не 4


def test_object_progress_dvd_unit_ticks_once_as_a_whole(tmp_path):
    # DVD-юниты раньше вообще не тикали "объектов" (не проходили через общий цикл по именам
    # файлов -- см. _walk_dir()) -- теперь тикают тем же принципом, что и архив: одна единица
    # целиком, после того как весь её контент обработан.
    source = tmp_path / "source"
    disc = source / "Video"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"i" * 20)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    ticks = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None, object_progress_cb=ticks.append)
    list(walker.walk())
    assert sum(ticks) == 1


# REVIEW-HANDOFF.md, Раунд 86, замечание 2 + follow-up (2026-08-10, речь пользователя) --
# ПЕРЕСМОТРЕНО 2026-08-18 (боевой прогон: источник с горой мелких media-файлов + немного
# крупных/видео рядом, "обработано объектов 100%" на 4-й минуте при полутора часах прогона).
# Промежуточная версия (тик НА ОТПРАВКУ батча, ОДНИМ вызовом на весь батч, ДО exiftool_batch())
# была задумана как "ограниченная неточность, максимум один батч вперёд" -- но video_duration_
# and_resolution() (ffprobe) для видео из этого же батча вызывается ПОЗЖЕ, поштучно, внутри
# analyze_batch() в основном цикле run_analyze() -- если батч содержал видео (особенно
# последний батч источника), тик уже засчитывал их как "готово" за секунды/минуты до того, как
# ffprobe реально их дощупал, и "обработано объектов 100%" держалось клэмпом весь этот разрыв.
# Финальная версия тикает ПОШТУЧНО, в run_analyze(), ПОСЛЕ analyze_batch() для каждого item --
# честно отражает реальное завершение (ffprobe включительно), без батч-уровня компромисса.
# Тест ниже проверяет: тик идёт по одному на файл (не пачкой на весь батч), и после
# exiftool_batch() (тегирование батча -- дешёвая часть, отдельная от per-item ffprobe) -- non-
# media файл (2026-08-17: больше НЕ тикает вовсе) не создаёт лишних событий.
def test_object_progress_ticks_once_per_item_after_analyze_batch(tmp_path, monkeypatch):
    from PIL import Image

    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for name in ("photo1.jpg", "photo2.jpg", "photo3.jpg"):
        Image.new("RGB", (800, 600), (10, 20, 30)).save(album / name, "JPEG")
    (album / "readme.txt").write_bytes(b"not media")  # non-media -- never ticks (2026-08-17)

    events = []
    real_exiftool_batch = m.exiftool_batch
    real_add_object_progress = m.ProgressReporter.add_object_progress

    def _spy_exiftool_batch(paths, **kw):
        events.append(("batch", len(paths)))
        return real_exiftool_batch(paths, **kw)

    def _spy_add_object_progress(self, n=1):
        events.append(("tick", n))
        return real_add_object_progress(self, n)

    monkeypatch.setattr(m, "exiftool_batch", _spy_exiftool_batch)
    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy_add_object_progress)

    cfg = m.Config(source=str(source), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                    workdir=str(tmp_path / "appdir"))
    stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

    assert stats.total_files == 3  # readme.txt is "other" -- never enters the pipeline at all
    # По одному тику на файл (3 отдельных вызова с n=1), не один пачкой на n=3 -- readme.txt не
    # тикает вовсе (2026-08-17, "other" не считается в X/Y).
    tick_events = [n for kind, n in events if kind == "tick"]
    assert tick_events == [1, 1, 1], events
    # Батч (exiftool_batch(), тегирование) происходит РАНЬШЕ любого тика для этого батча --
    # тики теперь следуют ЗА реальной обработкой, не опережают её.
    batch_idx = next(i for i, e in enumerate(events) if e[0] == "batch")
    first_tick_idx = next(i for i, e in enumerate(events) if e[0] == "tick")
    assert batch_idx < first_tick_idx, events


def test_object_progress_video_ticks_after_ffprobe_not_before(tmp_path, monkeypatch):
    """Живой боевой прогон, 2026-08-18 (analyze --source, много мелких файлов + немного
    крупных/видео рядом): "обработано объектов 100%" встало на 4-й минуте прогона, который
    реально шёл полтора часа -- батч-тик (см. класс выше) засчитывал видео из своего батча КАК
    ГОТОВОЕ до того, как video_duration_and_resolution() (ffprobe, самая медленная часть
    analyze-quick для видео) вообще начинал(а) их разбирать. red-before-green: до фикса тик для
    видео шёл РАНЬШЕ вызова ffprobe для этого же файла; после -- строго позже."""
    from PIL import Image

    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    Image.new("RGB", (800, 600), (10, 20, 30)).save(album / "photo.jpg", "JPEG")
    (album / "clip.mp4").write_bytes(b"fake video bytes")

    events = []
    real_add_object_progress = m.ProgressReporter.add_object_progress

    def _fake_ffprobe(path):
        events.append(("ffprobe", m.os.path.basename(path)))
        return (1.0, 640, 480, 1000)

    def _spy_add_object_progress(self, n=1):
        events.append(("tick", n))
        return real_add_object_progress(self, n)

    monkeypatch.setattr(m, "video_duration_and_resolution", _fake_ffprobe)
    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy_add_object_progress)

    cfg = m.Config(source=str(source), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                    workdir=str(tmp_path / "appdir"))
    m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    ffprobe_idx = next(i for i, e in enumerate(events) if e[0] == "ffprobe")
    # Никакой тик не должен произойти раньше ffprobe -- иначе "объектов %" уже посчитал бы
    # видео обработанным до того, как самая медленная его часть реально началась.
    ticks_before_ffprobe = [e for e in events[:ffprobe_idx] if e[0] == "tick"]
    assert ticks_before_ffprobe == [], events
    assert any(e[0] == "tick" for e in events[ffprobe_idx:]), events


# ---------------------------------------------------------------------------
# ProgressReporter(two_line=True)
# ---------------------------------------------------------------------------

def test_object_progress_analyze_archive_ticks_once_not_per_file_inside(tmp_path, monkeypatch):
    """Тик теперь идёт поштучно в run_analyze() (см. класс выше) -- файлы ИЗ РАСПАКОВАННОГО
    архива тоже проходят через тот же основной цикл/analyze_batch(), но не должны получить
    СВОЙ тик каждый: архив уже засчитан как ОДНА единица внутри SourceWalker (тот же принцип,
    что и у обычной сборки, см. defer_media_object_tick), иначе на источнике с архивами счёт
    задвоился бы (N файлов внутри архива + сам архив, вместо просто архива)."""
    source = tmp_path / "NewBatch"
    source.mkdir()
    with zipfile.ZipFile(source / "album.zip", "w") as zf:
        for name in ("p1.jpg", "p2.jpg", "p3.jpg"):
            zf.writestr(name, b"x" * 20)

    events = []
    real_add_object_progress = m.ProgressReporter.add_object_progress

    def _spy_add_object_progress(self, n=1):
        events.append(n)
        return real_add_object_progress(self, n)

    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy_add_object_progress)

    cfg = m.Config(source=str(source), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                    workdir=str(tmp_path / "appdir"))
    m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    # Ровно один тик (архив как целое, из SourceWalker) -- ни одного лишнего тика за 3 файла
    # внутри него из основного цикла run_analyze().
    assert events == [1], events


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
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed, 10 files -> 2.00с/файл
    line = bar._build_two_line_status()
    assert "2.00с/файл" in line
    bar.close()


def test_two_line_status_shows_elapsed_time_before_rate(monkeypatch):
    # 2026-08-07, речь пользователя ("зачем это выводить в статусе, я этого не просил" про
    # старый текстовый transient_op "чтение метаданных, файлов: N…" во время батч-чтения EXIF
    # -- см. photosort_win.py:_walk_with_exif_prefetch()): убран, заменён общим временем текущей
    # фазы -- показывает "не зависло" без привязки к конкретной операции, позиция -- между
    # "объектов X/Y" и скоростью "с/файл", как попросил пользователь. 2026-08-11, речь
    # пользователя: подпись "занято" перед временем убрана -- само поле ЧЧ:ММ:СС без подписи.
    bar = _two_line_bar()
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed -> tqdm.format_interval "00:20"
    line = bar._build_two_line_status()
    assert "00:20" in line
    assert "занято" not in line
    assert line.index("00:20") < line.index("с/файл")
    bar.close()


def test_console_columns_returns_real_terminal_width(monkeypatch):
    # REVIEW-HANDOFF.md, Раунд 134, придирка: все существующие тесты подменяют
    # _console_columns() целиком (лямбдой), ни один не гоняет её РЕАЛЬНОЕ тело -- эти три теста
    # мокают то, что _console_columns() читает (os.get_terminal_size()), а не саму функцию.
    fake_size = m.os.terminal_size((137, 40))
    monkeypatch.setattr(m.os, "get_terminal_size", lambda fd: fake_size)
    assert m._console_columns() == 137


def test_console_columns_falls_back_when_stdout_has_no_fileno(monkeypatch):
    # sys.__stdout__-ловушка (см. _console_columns()'s докстринг) -- windowed-сборка стартует
    # с sys.stdout=None до AllocConsole(); .fileno() на None -- AttributeError.
    class _NoFileno:
        pass

    monkeypatch.setattr(m.sys, "stdout", _NoFileno())
    assert m._console_columns(fallback=42) == 42


def test_console_columns_falls_back_on_os_error(monkeypatch):
    # stdout.fileno() существует, но реального терминала нет (файл/пайп) -- os.get_terminal_size()
    # поднимает OSError на такой fd, тот же путь, что и раньше покрывал shutil-обёртку.
    def _raise(fd):
        raise OSError("not a terminal")

    monkeypatch.setattr(m.os, "get_terminal_size", _raise)
    assert m._console_columns(fallback=55) == 55


def test_two_line_status_drops_elapsed_field_when_terminal_too_narrow(monkeypatch):
    # Речь пользователя, 2026-08-07 ("нужно не допустить переноса строки статуса"): время --
    # единственное поле, добавленное этой правкой, не часть уже проверенного пользователем на
    # практике формата -- если целиком не влезает в реальную ширину терминала, просто не
    # показывается (перенос сломал бы самообновление строки через \r), вместо того чтобы
    # переноситься на вторую строку. На широком терминале (не-tty/файл/пайп в остальных тестах
    # этого класса) это никогда не срабатывает -- см. sys.stderr.isatty() guard.
    #
    # REVIEW-HANDOFF.md, Раунд 86, замечание 1: на 80-колоночном терминале одного только поля
    # времени мало даже для короткого desc этого бара -- прогрессивное снятие полей
    # (см. _build_two_line_status()) идёт дальше, снимает и скорость. Раньше тест этого не
    # замечал, потому что не проверял итоговую длину строки вовсе -- именно это и было находкой
    # ревизора (реальная строка оставалась 120+ символов даже "после фикса").
    bar = _two_line_bar()
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed -> "00:20", однозначный маркер поля
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    line = bar._build_two_line_status()
    assert "00:20" not in line
    assert "с/файл" not in line
    assert "обработано объектов" in line  # ключевой сигнал "не зависло" не снимается никогда
    assert len(line) <= 80, (len(line), line)


def test_two_line_status_fits_80_columns_even_for_longest_op_description(monkeypatch):
    # REVIEW-HANDOFF.md, Раунд 86, замечание 1, конкретный кейс из живого репорта пользователя:
    # analyze/Паспорт архива использует самое длинное известное desc
    # (_ANALYZE_PASSPORT_PROGRESS_DESC, self._op_field_width=51 для этого бара) -- на этом
    # тексте прежний фикс (снятие только "занято") не помогал вообще: base+tail без "занято"
    # был 123 символа против 80-колоночного cmd.exe. Текст операции теперь обрезается, если
    # даже "op | всего медиа | обработано объектов %" без него не влезает -- итог должен
    # реально уместиться, не только "стать короче".
    bar = m.ProgressReporter(total=None, desc=m._ANALYZE_PASSPORT_PROGRESS_DESC, unit="файл",
                              two_line=True, total_estimate=100)
    bar.add_object_progress(42)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    line = bar._build_two_line_status()
    assert len(line) <= 80, (len(line), line)
    assert "обработано объектов" in line  # ключевой сигнал сохранён даже при обрезке текста
    assert "42.0%" in line
    bar.close()
    bar.close()


def test_two_line_status_fits_below_55_columns_by_dropping_media_count_field(monkeypatch):
    # REVIEW-HANDOFF.md, Раунд 87, замечание 1: предыдущий фикс (тест выше) обрезал только
    # текст операции -- "всего медиа NNNN"-суффикс фиксированной длины никогда не снимался,
    # так что минимальная длина строки (op="…", 1 символ) была константой ~55 символов,
    # НЕ зависящей от реальной ширины терминала: ниже 55 колонок строка снова гарантированно
    # переполнялась, неограниченно по мере сужения (тот же класс проблемы, что чинил весь
    # Раунд 86, просто сдвинутый на другой порог). Теперь "всего медиа" -- ещё одно поле,
    # снимаемое прогрессивно (после текста операции), "обработано объектов %" -- по-прежнему
    # единственное НИКОГДА не снимаемое.
    bar = m.ProgressReporter(total=None, desc=m._ANALYZE_PASSPORT_PROGRESS_DESC, unit="файл",
                              two_line=True, total_estimate=100)
    bar.add_object_progress(42)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    for columns in (40, 30):
        monkeypatch.setattr(m, "_console_columns",
                             lambda fallback=80, columns=columns: columns)
        line = bar._build_two_line_status()
        assert len(line) <= columns, (columns, len(line), line)
        assert "обработано объектов" in line
        assert "42.0%" in line
        assert "всего медиа" not in line
    bar.close()
    bar.close()


def test_two_line_status_shows_object_progress_with_total_estimate():
    # Живой репорт пользователя (2026-08-01): заменяет прежний [прошло/план] -- честный
    # счётчик X/Y (X = self._obj_count, Y = total_estimate), без экстраполяции времени. Обе
    # величины -- ОДНА гранулярность (архив = 1 объект, см. add_object_progress()).
    # 2026-08-09 (речь пользователя): X/Y-дробь заменена на процент ("обработано объектов
    # XX%") -- та же причина, что видно ниже в test_two_line_status_forces_100_percent_on_
    # completion: сырые X/Y читались как расхождение/баг, когда total_estimate (оценка) не
    # совпадал с фактом.
    bar = _two_line_bar(total_estimate=100)
    bar.add_object_progress(7)
    line = bar._build_two_line_status()
    assert "обработано объектов   7.0%" in line
    bar.close()


def test_two_line_status_op_field_width_sized_per_bar_not_global_max():
    """Речь пользователя, 2026-08-11: раньше ширина поля операции (_TWO_LINE_OP_FIELD_WIDTH,
    удалена) считалась ОДНИМ общим максимумом по ВСЕМ resting-desc программы разом (Раунд 67
    сознательно к этому и привёл -- см. архивную версию этого теста в git-истории) -- на
    практике это разбухало пустым местом для короткого resting-текста до ширины самого
    длинного из ВСЕХ, даже когда эти два режима физически не могут появиться в одном прогоне.
    Теперь self._op_field_width считается ПО КАЖДОМУ бару отдельно (max его собственного desc и
    _MAX_TRANSIENT_OP_LEN) -- короткий resting-desc больше не тащит за собой чужую ширину.

    2026-08-24: "длинный" desc здесь -- синтетическая строка, не производственная константа --
    раньше тест сравнивал против m._ANALYZE_PASSPORT_PROGRESS_DESC, но эта строка сама была
    сокращена в этом же коммите (живая просьба пользователя, "очень длинное название") --
    завязка на КОНКРЕТНУЮ production-константу делает тест хрупким к будущим сокращениям текста,
    хотя проверяется МЕХАНИЗМ (per-bar ширина), не конкретное значение этой строки."""
    short_bar = _two_line_bar()  # "Разбираю и копирую файлы" (24 символа)
    long_desc = "Э" * 60  # заведомо длиннее любого resting-desc и порога транзиентных текстов
    long_bar = m.ProgressReporter(total=None, desc=long_desc, unit="файл", two_line=True)
    short_pos = short_bar._build_two_line_status().index("| всего медиа")
    long_pos = long_bar._build_two_line_status().index("| всего медиа")
    assert short_pos < long_pos
    assert long_pos - short_pos == len(long_desc) - short_bar._op_field_width
    short_bar.close()
    long_bar.close()


class TestPhaseDescNamesDontRepeatModeFromHeader:
    """Живая просьба пользователя, 2026-08-24: название режима теперь и так есть в шапке
    параметров запуска (_log_run_start_header(), печатается ДО этих resting-текстов) -- не
    нужно повторять его тут, тратя место в поле операции статус-строки. Заодно
    _ANALYZE_PASSPORT_PROGRESS_DESC (используется и для реального self_scan="Паспорт архива",
    и для голого CLI "analyze --source" без self_scan) больше не называет режим "Паспорт
    архива" безусловно -- раньше это было неточно для второго случая (не паспорт вообще)."""

    def test_no_desc_contains_a_mode_name_or_the_word_analyze(self):
        mode_words = ["Паспорт архива", "Пробный прогон", "Сборка архива",
                      "Сканирование источника", "analyze", "passport"]
        descs = [m._DRY_RUN_PHASE_DESC, m._BUILD_PHASE_DESC,
                 m._ANALYZE_QUICK_PROGRESS_DESC, m._ANALYZE_PASSPORT_PROGRESS_DESC]
        for desc in descs:
            for word in mode_words:
                assert word.lower() not in desc.lower(), (desc, word)

    def test_analyze_descs_are_shorter_than_before_this_fix(self):
        """Регрессия по значению, не только по отсутствию слов режима -- реальная экономия
        места в поле операции, не просто перефразировка той же длины."""
        assert len(m._ANALYZE_QUICK_PROGRESS_DESC) < len("analyze — метаданные источника")
        assert len(m._ANALYZE_PASSPORT_PROGRESS_DESC) < \
            len("analyze (Паспорт архива) — метаданные + хеширование")
        assert len(m._DRY_RUN_PHASE_DESC) < len("Проверяю источник (пробный прогон)")

    def test_all_phase_descs_fit_the_length_cap(self):
        """Живая просьба пользователя, 2026-08-24 (третий заход, прямая цифра): жёсткий потолок
        длины -- 2/3 от длины "Хеширую и читаю метаданные файлов" (33 символа, та самая строка,
        которую пользователь назвал "очень длинное название") = 22 символа. Регрессия на
        будущее -- если кто-то снова удлинит один из этих текстов, тест должен покраснеть, не
        полагаться на то, что кто-то вручную заметит и посчитает символы.

        Пятый заход (SESSION-HANDOFF.txt, "ты не доделал"): транзиентные тексты (способные
        ВРЕМЕННО заменить эти же resting-тексты в том же поле, см. _MAX_TRANSIENT_OP_LEN) под
        тем же потолком -- та же регрессия на будущее, не только для resting."""
        descs = [m._DRY_RUN_PHASE_DESC, m._BUILD_PHASE_DESC,
                 m._ANALYZE_QUICK_PROGRESS_DESC, m._ANALYZE_PASSPORT_PROGRESS_DESC,
                 m._DEFERRED_CONTENT_TRANSIENT_OP, m._ARCHIVE_CONTENT_TRANSIENT_OP]
        for desc in descs:
            assert len(desc) <= m._PHASE_DESC_MAX_LEN, (desc, len(desc))
        assert m._ARCHIVE_EXTRACT_TRANSIENT_OP_MAX_LEN <= m._PHASE_DESC_MAX_LEN

    def test_all_phase_descs_and_transient_ops_have_leading_space(self):
        """Живая просьба пользователя, 2026-08-24: поле операции сливалось с левой рамкой окна
        консоли на глаз -- ведущий пробел нужен у КАЖДОГО текста, способного занять это поле,
        иначе отступ то появлялся бы, то пропадал при переключении resting/transient (хуже, чем
        не делать вообще)."""
        for text in (m._DRY_RUN_PHASE_DESC, m._BUILD_PHASE_DESC, m._ANALYZE_QUICK_PROGRESS_DESC,
                     m._ANALYZE_PASSPORT_PROGRESS_DESC, m._DEFERRED_CONTENT_TRANSIENT_OP,
                     m._ARCHIVE_CONTENT_TRANSIENT_OP):
            assert text.startswith(" "), text


def test_two_line_status_object_progress_without_total_estimate():
    # total_estimate=None (предпересчёт недоступен/не передан) -- показываем голый счётчик,
    # не притворяемся, что знаменатель (и значит процент) есть.
    bar = _two_line_bar()
    bar.add_object_progress(3)
    line = bar._build_two_line_status()
    assert "обработано объектов      3" in line
    assert "%" not in line.split("объектов")[1].split("с/файл")[0]
    bar.close()


def test_two_line_status_percent_always_has_one_decimal():
    """Речь пользователя, 2026-08-10 (follow-up к Раунду 86): раньше целые проценты до 99%
    (не создавать иллюзию точности, которой у total_estimate-оценки нет), 1 знак после запятой
    только от 99% и выше (иначе "99%" мог бы провисеть неизменным долго на большом архиве и
    читаться как зависание) -- тот же довод оказался применим и к НИЖНЕЙ границе: на большом
    total_estimate один батч-тик двигает процент заметно меньше 1%, и "0%"/"50%" рискуют
    провисеть так же, как раньше "99%". Знак после запятой у отношения не обещает точности
    самого total_estimate -- теперь всегда 1 знак, без порога."""
    bar = _two_line_bar(total_estimate=1000)
    bar.add_object_progress(500)  # 50.0%
    assert "обработано объектов  50.0%" in bar._build_two_line_status()

    bar._obj_count = 0
    bar.add_object_progress(991)  # 99.1%
    assert "обработано объектов  99.1%" in bar._build_two_line_status()
    bar.close()


def test_two_line_status_percent_truncated_not_rounded_near_100(tmp_path):
    """Живая находка пользователя, 2026-08-19 (боевой прогон, источник с одним архивом,
    вмещающим гигантское количество вложенных файлов/вложенных архивов): X/Y = 9996/10000 =
    99.96% -- f"{99.96:.1f}%" ОКРУГЛЯЕТ до буквального "100.0%" (до этого фикса), хотя реально
    не готово: архив тикает одним объектом только по завершении ВСЕГО своего содержимого, а на
    практике оставался последним, самым долгим объектом прогона (2 часа). "100.0%" держалось бы
    неотличимо от настоящего завершения весь этот остаток. Теперь верхняя граница строго 99.9,
    а усечение (не округление) не даёт значению вроде 99.96 перепрыгнуть её самостоятельно."""
    bar = _two_line_bar(total_estimate=10_000)
    bar.add_object_progress(9996)  # ровно 99.96%
    line = bar._build_two_line_status()
    assert "обработано объектов  99.9%" in line
    assert "100.0%" not in line
    bar.close()


def test_two_line_status_percent_floored_at_0_1_never_shows_literal_zero(tmp_path):
    """Речь пользователя, 2026-08-17: даже с 1 знаком после запятой X/Y*100 округляется в "0.0%"
    для любого X/Y < 0.05% -- на большом total_estimate (или пока обходится куча немедийных
    файлов, не входящих в X/Y, см. 2026-08-17 выше в докстрине объектов X/Y) это может держаться
    заметно дольше, чем один update(), и читается как зависание тем же способом, что и "0%"/
    "99%" до фикса Раунда 86. Пол 0.1% -- та же намеренная неточность, что и у самого
    "1 знак после запятой": сигнал "не зависло", не точная метрика."""
    bar = _two_line_bar(total_estimate=1_000_000)
    line = bar._build_two_line_status()  # bar._obj_count == 0 -- ни одного тика ещё не было
    assert "обработано объектов   0.1%" in line
    assert "объектов   0.0%" not in line

    bar._obj_count = 1  # 1/1_000_000 * 100 == 0.0001% -- всё ещё округлилось бы в "0.0%"
    line = bar._build_two_line_status()
    assert "обработано объектов   0.1%" in line
    bar.close()


def test_two_line_status_percent_clamped_at_99_9_when_estimate_undershoots():
    """total_estimate -- оценка (_quick_media_count_estimate()), не точный подсчёт -- реальный
    X может обогнать её мимо конца прогона (недооценка). Раньше это дало бы X/Y > 1 в дроби --
    в процентах это выглядело бы как "142%", что читается сломанным сильнее, чем сама причина.

    2026-08-19 (живой боевой прогон): верхняя граница теперь 99.9, не 100.0 -- буквальный
    "100.0%" зарезервирован ИСКЛЮЧИТЕЛЬНО за force_complete (реальное завершение, см. тест
    ниже) -- см. докстрин pct в _build_two_line_status()."""
    bar = _two_line_bar(total_estimate=100)
    bar.add_object_progress(142)
    line = bar._build_two_line_status()
    assert "обработано объектов  99.9%" in line
    assert "100.0%" not in line
    assert "142" not in line
    bar.close()


def test_two_line_status_forces_100_percent_on_successful_completion(capsys):
    """Речь пользователя, 2026-08-09 ("в конце работы всегда должно быть 100%"): реальный X/Y
    (тут 7/100 -- 7%) необязательно сойдётся к концу прогона даже при полном успехе
    (легитимные пропуски -- нет доступа к папке и т.п.) -- close() форсирует ровно "100%" на
    успешном (не прерванном) завершении, не показывает застрявший процент. Тот же принцип,
    что у стандартных прогресс-баров (apt/npm)."""
    bar = _two_line_bar(total_estimate=100)
    bar.add_object_progress(7)
    bar.update(1)  # self.count > 0 -- иначе close() в не-tty окружении вообще ничего не печатает
    capsys.readouterr()  # discard update()'s own output
    bar.close()
    captured = capsys.readouterr()
    assert "обработано объектов 100.0%" in captured.err
    assert "7.0%" not in captured.err


def test_two_line_status_does_not_force_100_percent_when_interrupted(capsys):
    """mark_interrupted() (вызывается из except KeyboardInterrupt: в run_analyze()/_run_impl())
    -- close() НЕ форсирует 100% на реально прерванном прогоне, "готово" было бы неправдой."""
    bar = _two_line_bar(total_estimate=100)
    bar.add_object_progress(7)
    bar.update(1)
    capsys.readouterr()
    bar.mark_interrupted()
    bar.close()
    captured = capsys.readouterr()
    assert "обработано объектов   7.0%" in captured.err
    assert "100%" not in captured.err


def test_two_line_status_media_count_is_processed_count():
    # "всего медиа" -- всегда self.count (реально обработанные файлы, тот же счётчик, что и
    # затем совпадает с итоговым отчётом), write_object_line() (n_found -- предварительная,
    # не подтверждённая оценка) на отображаемое число не влияет.
    bar = _two_line_bar()
    bar.write_object_line("folder", "some/folder", 50)
    bar.update(3)
    line = bar._build_two_line_status()
    assert "всего медиа        3" in line
    bar.close()


def test_two_line_status_media_count_ignores_declared_object_totals():
    """2026-08-11, речь пользователя (живой боевой прогон -- "найдено медиа" 1038 против 644 в
    report.html): раньше analyze-бар показывал декларируемую (по n_found из write_object_line(),
    ДО реальной обработки) оценку вместо self.count -- расходилась с итоговым отчётом, потому
    что часть "найденного" по имени/расширению никогда не подтверждалась реальной классификацией
    (архив без media внутри, битый файл и т.п.). Теперь ЛЮБОЙ two_line-бар (включая analyze)
    показывает только self.count -- write_object_line() с любым n_found не должен сдвигать
    отображаемое число ни на йоту, даже если update() ещё не было ни разу."""
    bar = _two_line_bar()
    bar.write_object_line("folder", "some/folder", 50)
    bar.write_object_line("archive", "some/archive.zip", 200)
    line = bar._build_two_line_status()
    assert "всего медиа        0" in line
    assert "50" not in line
    assert "200" not in line
    bar.close()


def test_add_object_progress_accumulates_across_calls():
    bar = _two_line_bar(total_estimate=10)
    bar.add_object_progress(1)
    bar.add_object_progress(1)
    bar.add_object_progress(1)
    assert bar._obj_count == 3
    bar.close()


# ---------------------------------------------------------------------------
# note_width -- SESSION-HANDOFF.txt п.13 (2026-08-05, боевой прогон): однострочный
# (не two_line) бар Фазы 1 визуально "гулял" влево-вправо -- set_description() меняло длину
# desc в зависимости от наличия note, tqdm пересчитывал позицию |###| каждый раз заново.
# ---------------------------------------------------------------------------

class _FakeTqdmBar:
    """Минимальная замена _tqdm -- перехватывает set_description()/update(), не рисует
    ничего реального (тесты не имеют настоящего терминала, is_tty=False под pytest)."""
    def __init__(self):
        self.descriptions = []

    def set_description(self, d):
        self.descriptions.append(d)

    def update(self, n):
        pass

    def set_postfix_str(self, s):
        pass

    def close(self):
        pass


def _single_line_bar_with_fake_tqdm(**overrides):
    bar = m.ProgressReporter(total=10, desc=" Просматриваю уже собранный архив", unit="файл",
                              **overrides)
    bar._bar = _FakeTqdmBar()
    return bar


def test_note_width_keeps_description_length_constant_with_and_without_note():
    bar = _single_line_bar_with_fake_tqdm(note_width=len("большое видео"))
    bar.update(1, note=None)
    bar.update(1, note="большое видео")
    bar.update(1, note=None)
    lengths = {len(d) for d in bar._bar.descriptions}
    assert len(lengths) == 1  # ни разу не поменялась длина -- |###| не сдвигается
    bar.close()


def test_note_width_none_keeps_old_variable_length_behavior():
    # note_width не передан (все остальные однострочные бары) -- поведение не меняется, длина
    # description по-прежнему растёт/падает вместе с note.
    bar = _single_line_bar_with_fake_tqdm()
    bar.update(1, note=None)
    bar.update(1, note="большое видео")
    lengths = {len(d) for d in bar._bar.descriptions}
    assert len(lengths) == 2  # старое поведение -- длина реально разная
    bar.close()


def test_note_width_pads_note_shorter_than_reserved_width():
    bar = _single_line_bar_with_fake_tqdm(note_width=20)
    bar.update(1, note="повтор")
    assert bar._bar.descriptions[0] == " Просматриваю уже собранный архив — повтор              "
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


def test_write_object_line_shows_placement_letter_when_given(capsys):
    """Задача пользователя, 2026-08-09 (--dry-run/[3] реальная сборка): "A" (альбом)/"D" (по
    дате) сразу после закрывающей "]" тега, с ровно ОДНИМ пробелом перед путём для ОБОИХ тегов
    -- "[папка]" короче "[archive]" на 2 символа, поэтому у нее на 2 паддинг-пробела больше
    ПЕРЕД буквой, не после (наивная вставка буквы в старый фиксированный хвостовой паддинг дала
    бы 1 пробел у archive и 3 у папка -- разное расстояние до пути, что и не нужно)."""
    bar = _two_line_bar()
    bar.write_object_line("archive", "Foto.zip", 5, "A")
    bar.write_object_line("folder", "Album", 3, "D")
    captured = capsys.readouterr()
    assert "  [archive]A Foto.zip: найдено медиафайлов 5" in captured.err
    assert "  [папка]D   Album: найдено медиафайлов 3" in captured.err
    bar.close()


def test_write_object_line_without_letter_keeps_old_format(capsys):
    """letter="" (по умолчанию, analyze/[4] Паспорт архива -- буква не показывается вовсе) --
    старый формат без изменений, никакой буквы/лишнего пробела."""
    bar = _two_line_bar()
    bar.write_object_line("archive", "Foto.zip", 5, "")
    bar.write_object_line("folder", "Album", 3, "")
    captured = capsys.readouterr()
    assert "  [archive] Foto.zip: найдено медиафайлов 5" in captured.err
    assert "  [папка]   Album: найдено медиафайлов 3" in captured.err
    assert "[archive]A" not in captured.err
    assert "[archive]D" not in captured.err
    bar.close()


def test_object_line_truncates_long_path_from_the_front(monkeypatch):
    bar = _two_line_bar()
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
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
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    archive_line = bar._format_object_line("archive", "Foto.zip", 5)
    folder_line = bar._format_object_line("folder", "Album", 3)
    self_log_style = "  [archive] Foto.zip: archive_extracted 5 медиафайлов"
    assert archive_line[:2] == folder_line[:2] == self_log_style[:2] == "  "
    bar.close()


def test_log_archive_budget_has_same_safety_margin_as_object_line(monkeypatch):
    # SESSION-HANDOFF.txt, 2026-08-09 (боевой прогон, третья находка): _object_line_budget()
    # ("[папка]" -- write_object_line()) резервировал фиксированный запас "+8" сверх точной
    # длины хвоста, тогда как _log_archive() ("[archive]") звал _console_tag_line_budget() с
    # ТОЧНОЙ длиной хвоста БЕЗ какого-либо запаса -- на одной и той же ширине терминала бюджет
    # [archive] оказывался на 6 символов ШИРЕ (меньше запас прочности перед краем терминала),
    # чем у [папка], хотя оба тега/отступ идентичной ширины. Margin теперь общий, внутри
    # _console_tag_line_budget() -- разница между двумя бюджетами должна объясняться ТОЛЬКО
    # точной разницей длины текста хвоста (23 символа archive_no_media vs 21 символ у
    # object_line's "универсального" резерва под неизвестное число), не отдельным
    # необъяснённым запасом в одном месте и его отсутствием в другом.
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    # Реальные вызывающие методы, не переизобретённая вручную арифметика -- иначе тест мог бы
    # незаметно перестать проверять фактическое поведение _object_line_budget()/_log_archive().
    bar = _two_line_bar()
    object_budget = bar._object_line_budget()
    archive_no_media_tail = ": найдено медиафайлов 0"
    archive_budget = m._console_tag_line_budget(len(archive_no_media_tail))
    object_line_tail_reserve = len(" найдено медиафайлов ")
    assert object_budget - archive_budget == len(archive_no_media_tail) - object_line_tail_reserve
    bar.close()


def test_log_archive_truncates_long_path_same_as_object_line(tmp_path, monkeypatch):
    # Регресс на сам перенос строки из боевого прогона, с путём НАРОЧНО подобранной длины
    # (columns=80, tail="архив без медиа" -> new budget 37, old budget без margin был бы 45):
    # 44-символьный путь укладывался бы в СТАРЫЙ бюджет [archive] (45) БЕЗ обрезки -- строка
    # печаталась бы целиком и переносилась по краю терминала -- но не укладывается в новый
    # (37, после фикса), значит теперь обрезается "…"-префиксом так же, как обрезалась бы
    # эквивалентная по длине строка [папка] (см. test_object_line_truncates_long_path_from_the_front).
    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    prefix = "F:\\" + "Отпуск 2015\\" + "Фото\\"  # placeholder-имена, не реальные с боевого прогона
    long_path = prefix + "x" * 20 + ".zip"
    assert len(long_path) == 44
    walker._log_archive(long_path, "archive_no_media")
    assert len(lines) == 1
    # SourceWalker._log_own_line() (2026-08-24, живой репорт пользователя -- "плывущая" статус-
    # строка, склейка с активным баром) прибавляет ведущий "\n" перед КАЖДЫМ таким сообщением --
    # снят здесь же, бюджет по ширине терминала считается по видимому тексту, не по "\n".
    line = lines[0].lstrip("\n")
    assert line.startswith("  [archive] …")
    assert line.endswith(": найдено медиафайлов 0")
    assert len(line) <= 80


# ---------------------------------------------------------------------------
# Живой боевой прогон, 2026-08-28 (пополнение архива на D:): часть [archive]-строк выводилась
# БЕЗ буквы A/D после "]" (только объект-строка write_object_line() её несла), и та же строка
# переносилась там, где место ещё оставалось -- путь обрезан под полную ширину терминала, а
# write_heavy_notice() переносил по порогу 2/3. 2026-08-29 (ещё два живых репорта
# "необоснованный перенос"): write_heavy_notice() теперь переносит по краю окна, не по 2/3.
# ---------------------------------------------------------------------------

def test_log_archive_renders_placement_letter_right_after_bracket(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 120)
    walker._log_archive("D:\\Архив\\www.zip", "archive_extracted", "5 медиафайлов",
                         count=5, letter="A")
    assert lines[0].lstrip("\n").startswith(
        "  [archive]A D:\\Архив\\www.zip: распаковано, найдено медиафайлов 5")
    lines.clear()
    walker._log_archive("D:\\Архив\\www.zip", "archive_no_media")  # letter="" по умолчанию
    line = lines[0].lstrip("\n")
    assert line.startswith("  [archive] D:\\Архив\\www.zip: найдено медиафайлов 0")
    assert "[archive]A" not in line and "[archive]D" not in line


def test_log_archive_fitted_line_is_not_rewrapped_by_heavy_notice(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 120)
    captured = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             heavy_notice_cb=lambda line, wrap=True: captured.append((line, wrap)))
    # ~85 символов: влезает в терминал 120 целиком -- под старым кодом (перенос по 2/3 = 80)
    # перенеслось бы на "...распаковано, найдено" \n "медиафайлов 1577".
    walker._log_archive("D:\\Архив Е\\E\\OLD\\YandexDisk\\www.zip", "archive_extracted",
                         "1577 медиафайлов", count=1577)
    assert len(captured) == 1
    line, wrap = captured[0]
    assert wrap is False
    assert "\n" not in line
    assert line == ("  [archive] D:\\Архив Е\\E\\OLD\\YandexDisk\\www.zip: "
                    "распаковано, найдено медиафайлов 1577")


def test_log_archive_long_free_form_note_wraps_at_full_width_not_two_thirds(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 120)
    captured = []
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                             heavy_notice_cb=lambda line, wrap=True: captured.append((line, wrap)))
    walker._log_archive("D:\\x\\big.7z", "archive_path_traversal_suspected",
                         "распаковано 17 файлов из 19 по листингу -- похоже, часть содержимого "
                         "вышла за пределы папки распаковки")
    line, wrap = captured[0]
    assert wrap is True  # note не влезает в окно (120) целиком -- перенос оправдан

    # write_heavy_notice() переносит по краю окна (_console_columns()), НЕ по 2/3
    # (_terminal_wrap_width()) -- 2026-08-29, два живых репорта "необоснованный перенос".
    at_full = m._wrap_console_text(line, m._console_columns())
    phys = at_full.split("\n")
    assert len(phys) == 2                       # ровно один перенос, не три коротких строки
    assert all(len(pl) <= 120 for pl in phys)
    assert len(phys[0]) > 80                    # первая строка идёт до края окна, не до 2/3

    # sanity: старое поведение (перенос по 2/3 = 80) дало бы более одной лишней строки
    at_two_thirds = m._wrap_console_text(line, m._terminal_wrap_width())
    assert len(at_two_thirds.split("\n")) > len(phys)


def test_write_heavy_notice_wraps_at_full_terminal_width(monkeypatch):
    # 2026-08-29, два живых репорта "необоснованный перенос строки": write_heavy_notice()
    # переносит однострочные статус-уведомления SourceWalker'а по КРАЮ окна (_console_columns()),
    # а не по 2/3 (_terminal_wrap_width()) -- последнее только для прозы меню в console_log().
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 120)
    seen = []
    real_wrap = m._wrap_console_text
    monkeypatch.setattr(m, "_wrap_console_text",
                         lambda text, width: (seen.append(width), real_wrap(text, width))[1])
    bar = _two_line_bar()
    try:
        bar.write_heavy_notice("  " + "x" * 200)  # заведомо длиннее окна -- перенос сработает
    finally:
        bar.close()
    assert seen == [120]  # полная ширина окна, не 80 (2/3)


def test_skipped_meta_summary_line_not_wrapped_when_it_fits_window(monkeypatch):
    # Живой репорт 2026-08-29: "в архиве пропущено служебных записей: N (...)" рвалась посреди
    # фразы ("...не файлы с" \n "данными)") при куче места справа -- write_heavy_notice() резал
    # по 2/3, а строка (~84 симв.) влезает в окно 120 целиком.
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 120)
    msg = ("  в архиве пропущено служебных записей: 256 "
           "(ссылки и устройства — не файлы с данными)")
    assert len(msg) <= 120
    assert "\n" not in m._wrap_console_text(msg, m._console_columns())
    # репро валиден: старый порог (2/3 = 80) действительно рвал бы эту строку
    assert "\n" in m._wrap_console_text(msg, m._terminal_wrap_width())


def test_handle_archive_status_lines_carry_placement_letter_when_enabled(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested_zip = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested_zip, "w") as zf:
        zf.writestr("b.jpg", b"y" * 10)
    with zipfile.ZipFile(source / "MyAlbum.zip", "w") as zf:
        zf.writestr("a.jpg", b"x" * 10)
        zf.write(nested_zip, arcname="nested.zip")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    lines = []
    walker = m.SourceWalker(cfg, log=lines.append, show_placement_letter=True)
    list(walker.walk())
    # "MyAlbum" -- узнаваемый альбом -> "A"; строка "распаковано, найдено N" (count != листинг)
    # печатается через _log_archive() и теперь несёт ту же букву, что и объект-строка.
    assert any(ln.lstrip("\n").startswith("  [archive]A ")
               and "распаковано, найдено медиафайлов 2" in ln for ln in lines), lines


# ---------------------------------------------------------------------------
# _extraction_log_name_budget() -- live bug found by the user, 2026-08-01: a long archive
# name pushed "  Распаковка <имя> (X ГБ)…" past write_heavy_notice()'s line-wrap threshold --
# the wrapped second physical line then confused the tqdm bar's clear()/refresh() bookkeeping
# (which assumes it only ever owns exactly one row), leaving a stale visual duplicate.
# 2026-08-29: the threshold is the FULL terminal width now, not 2/3 -- the budget follows.
# ---------------------------------------------------------------------------

def test_extraction_log_name_budget_returns_large_value_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: False)
    assert m._extraction_log_name_budget() >= 200  # write_heavy_notice() never wraps off-tty


def test_extraction_log_message_fits_full_terminal_width_after_truncation(monkeypatch):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    long_name = "backup-" + "very-long-folder-name-" * 4 + "2024-09-18.tar.gz"  # ~110 символов
    budget = m._extraction_log_name_budget()
    truncated = m._truncate_progress_note(long_name, maxlen=budget)
    msg = f"  Распаковка {truncated} (6.2 ГБ)…"

    # write_heavy_notice() переносит по ПОЛНОЙ ширине окна (_console_columns()), не по 2/3
    # -- бюджет обязан удержать строку в одной физической строке при 80 колонках.
    assert len(msg) <= 80
    assert "\n" not in m._wrap_console_text(msg, m._console_columns())
    # различающий хвост (расширение + дата) сохранён, не срезан вместе с началом.
    assert truncated.endswith("2024-09-18.tar.gz")


def test_extraction_log_message_would_overflow_full_width_without_truncation(monkeypatch):
    # Sanity-check репро: неусечённое длинное имя реально выходит за полную ширину 80-колоночного
    # окна -- иначе бюджет выше ничего не проверяет.
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80: 80)
    long_name = "backup-" + "very-long-folder-name-" * 4 + "2024-09-18.tar.gz"
    msg = f"  Распаковка {long_name} (6.2 ГБ)…"
    assert len(msg) > 80
    assert "\n" in m._wrap_console_text(msg, m._console_columns())


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


def test_batch_rate_hint_used_instead_of_wall_clock(monkeypatch):
    """2026-08-06, боевой прогон ("скорость всегда 0"): _pending_heavy_time-исключение верно
    для распаковки/хеширования (одноразовая пауза, не цена файла), но НЕВЕРНО для батч-чтения
    EXIF -- там время батча И ЕСТЬ реальная цена N файлов. set_batch_rate_hint(per_item, N)
    должен подставить per_item как instantaneous для N ближайших update(), не пересчитывать
    по wall-clock (тот, между мгновенными yield'ами уже готового батча, дал бы ~0)."""
    bar = _two_line_bar()
    clock = _FakeClock(bar._t0)
    monkeypatch.setattr(m.time, "time", clock)

    bar.set_batch_rate_hint(3.0, 2)  # batch of 2 files, 3.0s/файл в среднем
    # Yield'ы внутри батча идут мгновенно -- wall-clock тут дал бы ~0, если бы не хинт.
    clock.advance(0.001)
    bar.update(1)
    assert bar._ema_rate == pytest.approx(3.0)
    clock.advance(0.001)
    bar.update(1)
    assert bar._ema_rate == pytest.approx(3.0)

    # Хинт исчерпан после 2 update() -- следующий файл снова меряется по wall-clock как обычно
    # (обычный EMA-блендинг с уже накопленным 3.0, не застрявшее значение хинта).
    clock.advance(5.0)
    bar.update(1)
    assert bar._ema_rate == pytest.approx(0.3 * 5.0 + 0.7 * 3.0)
    bar.close()


def test_batch_rate_hint_discards_stale_pending_heavy_time():
    """set_transient_op("работаю")/set_transient_op(None) вокруг батча копит то же самое
    время в _pending_heavy_time, что уже учтено хинтом -- update() должен ОТБРОСИТЬ его при
    потреблении хинта, не вычесть ещё раз поверх (задвоение)."""
    bar = _two_line_bar()
    bar._pending_heavy_time = 999.0  # искусственно "протекшее" значение, как будто от set_transient_op
    bar.set_batch_rate_hint(2.0, 1)
    bar.update(1)
    assert bar._ema_rate == pytest.approx(2.0)
    assert bar._pending_heavy_time == 0.0
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

    def counting(self, force_complete=False):
        calls["n"] += 1
        return original(self, force_complete=force_complete)

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

    def counting(self, force_complete=False):
        calls["n"] += 1
        return original(self, force_complete=force_complete)

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

    def counting(self, force_complete=False):
        calls["n"] += 1
        return original(self, force_complete=force_complete)

    monkeypatch.setattr(m.ProgressReporter, "_build_two_line_status", counting)
    calls["n"] = 0
    for _ in range(200):
        clock.advance(0.01)
        bar.update(0, note=None)  # precursor call, real code passes note=None for photos
        clock.advance(0.01)
        bar.update(1, note=None)
    assert calls["n"] < 20  # ~200/15 (throttled), not 200 (one per file, bug behavior)
    bar.close()


def _narrow_terminal(monkeypatch, columns: int):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_console_columns", lambda fallback=80, columns=columns: columns)


def test_log_menu_line_wrapped_fits_on_one_line(monkeypatch):
    _narrow_terminal(monkeypatch, 80)
    lines = []
    m._log_menu_line_wrapped("    [1] Диск C:  →  C:\\__PhotoArchive__", "   ",
                              "(папка уже есть)", lines.append)
    assert lines == ["    [1] Диск C:  →  C:\\__PhotoArchive__   (папка уже есть)"]


def test_log_menu_line_wrapped_single_tail_wraps_to_second_line(monkeypatch):
    """SESSION-HANDOFF.txt, 2026-08-05 (боевой прогон п.7) -- первый уровень фикса: tail,
    не помещающийся на одну строку с head, переносится ЦЕЛИКОМ на новую строку с отступом."""
    _narrow_terminal(monkeypatch, 60)
    lines = []
    m._log_menu_line_wrapped("    [1] Диск C:  →  C:\\__PhotoArchive__", "   ",
                              "(папки пока нет — возможное место для архива)", lines.append)
    assert lines == [
        "    [1] Диск C:  →  C:\\__PhotoArchive__",
        "    (папки пока нет — возможное место для архива)",
    ]


def test_log_menu_line_wrapped_list_tail_fits_together(monkeypatch):
    _narrow_terminal(monkeypatch, 100)
    lines = []
    m._log_menu_line_wrapped("    [1] Диск C:  →  C:\\__PhotoArchive__", "   ",
                              ["(папка уже есть)", "(тот же диск, что и источник)"],
                              lines.append)
    assert lines == [
        "    [1] Диск C:  →  C:\\__PhotoArchive__   (папка уже есть)  "
        "(тот же диск, что и источник)"
    ]


def test_log_menu_line_wrapped_list_tail_splits_into_three_lines_on_narrow_terminal(monkeypatch):
    """Живой боевой прогон 2026-08-06: узкий терминал ломал даже уже перенесённый tail ЕЩЁ
    раз, потому что status+suffix считались одним неразрывным куском ("...добавилось" /
    "бы)  (тот же диск...)" -- разрыв терминалом, не нашим кодом, посреди фразы). tail-список
    из 2 независимых кусков (status, suffix) должен упаковываться жадно -- каждый кусок цел,
    третья строка появляется, только если оба куска вместе не помещаются даже на отдельной
    строке под отступом."""
    _narrow_terminal(monkeypatch, 60)
    lines = []
    m._log_menu_line_wrapped(
        "    [1] Диск C:  →  C:\\__PhotoArchive__", "   ",
        ["(уже есть — проверю, что добавилось бы)", "(тот же диск, что и источник)"],
        lines.append,
    )
    assert lines == [
        "    [1] Диск C:  →  C:\\__PhotoArchive__",
        "    (уже есть — проверю, что добавилось бы)",
        "    (тот же диск, что и источник)",
    ]
    # Ни одна строка не должна содержать оборванное слово/скобку самим терминалом -- каждая
    # строка либо head целиком, либо один целый tail-кусок с отступом.
    for line in lines:
        assert line == "    [1] Диск C:  →  C:\\__PhotoArchive__" or line.startswith("    (")


def test_log_menu_line_wrapped_single_piece_longer_than_terminal_wraps_by_words(monkeypatch):
    """REVIEW-HANDOFF.md, Раунд 69, замечание 2: жадная упаковка списка кусков не защищала от
    ОДНОГО куска, который сам по себе длиннее доступной ширины (columns - len(indent)) -- он
    уходил в log() одной строкой длиннее терминала, терминал переносил её ещё раз посреди
    фразы. Терминал в 40 колонок (тот же случай, что нашёл ревизор реальным вызовом) -- ни
    одна строка не должна превышать 40 символов."""
    _narrow_terminal(monkeypatch, 40)
    lines = []
    m._log_menu_line_wrapped(
        "    [1] Диск C:  →  C:\\__PhotoArchive__", "   ",
        ["(папки пока нет — возможное место для архива)"],
        lines.append,
    )
    assert lines == [
        "    [1] Диск C:  →  C:\\__PhotoArchive__",
        "    (папки пока нет — возможное место",
        "    для архива)",
    ]
    for line in lines:
        assert len(line) <= 40
