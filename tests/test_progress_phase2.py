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
                             object_line_cb=lambda tag, path, n: calls.append((tag, path, n)))

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
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed, 10 files -> 2.00с/файл
    line = bar._build_two_line_status()
    assert "2.00с/файл" in line
    bar.close()


def test_two_line_status_shows_elapsed_time_before_rate(monkeypatch):
    # 2026-08-07, речь пользователя ("зачем это выводить в статусе, я этого не просил" про
    # старый текстовый transient_op "чтение метаданных, файлов: N…" во время батч-чтения EXIF
    # -- см. photosort_win.py:_walk_with_exif_prefetch()): убран, заменён общим "занято"
    # временем текущей фазы -- показывает "не зависло" без привязки к конкретной операции,
    # позиция -- между "объектов X/Y" и скоростью "с/файл", как попросил пользователь.
    bar = _two_line_bar()
    monkeypatch.setattr(bar, "_t0", bar._t0 - 20)  # 20s elapsed -> tqdm.format_interval "00:20"
    line = bar._build_two_line_status()
    assert "00:20" in line
    assert line.index("занято") < line.index("с/файл")
    bar.close()


def test_two_line_status_drops_elapsed_field_when_terminal_too_narrow(monkeypatch):
    # Речь пользователя, 2026-08-07 ("нужно не допустить переноса строки статуса"): "занято" --
    # единственное поле, добавленное этой правкой, не часть уже проверенного пользователем на
    # практике формата -- если целиком не влезает в реальную ширину терминала, просто не
    # показывается (перенос сломал бы самообновление строки через \r), вместо того чтобы
    # переноситься на вторую строку. На широком терминале (не-tty/файл/пайп в остальных тестах
    # этого класса) это никогда не срабатывает -- см. sys.stderr.isatty() guard.
    bar = _two_line_bar()
    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((80, 24)))
    line = bar._build_two_line_status()
    assert "занято" not in line
    assert "с/файл" in line  # остальная строка по-прежнему показывается целиком
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


def test_two_line_status_op_field_width_covers_analyze_passport_desc():
    """REVIEW-HANDOFF.md, Раунд 67, замечание 2: _TWO_LINE_OP_FIELD_WIDTH раньше считался
    только по паре run_for_source() (34/24 символа) -- desc run_analyze() ("Паспорт архива",
    51 символ) был длиннее, колонка "| всего медиа" на этом пути уезжала вправо относительно
    [2]/[3]. Проверяем, что позиция одинакова для короткого (run_for_source) и длинного
    (run_analyze/Паспорт) desc -- колонки больше не расходятся."""
    short_bar = _two_line_bar()  # "Разбираю и копирую файлы"
    long_bar = m.ProgressReporter(total=None, desc=m._ANALYZE_PASSPORT_PROGRESS_DESC,
                                   unit="файл", two_line=True)
    short_pos = short_bar._build_two_line_status().index("| всего медиа")
    long_pos = long_bar._build_two_line_status().index("| всего медиа")
    assert short_pos == long_pos
    short_bar.close()
    long_bar.close()


def test_two_line_status_object_progress_without_total_estimate():
    # total_estimate=None (предпересчёт недоступен/не передан) -- показываем голый счётчик
    # без "/Y", не притворяемся, что знаменатель есть.
    bar = _two_line_bar()
    bar.add_object_progress(3)
    line = bar._build_two_line_status()
    assert "объектов             3" in line
    assert "/" not in line.split("объектов")[1].split("с/файл")[0]
    bar.close()


def test_two_line_status_media_count_defaults_to_processed_count():
    # media_count_from_objects=False (дефолт, Фаза 2/реальная сборка) -- "всего медиа" по-прежнему
    # растёт по факту update(), write_object_line() ни на что не влияет.
    bar = _two_line_bar()
    bar.write_object_line("folder", "some/folder", 50)
    bar.update(3)
    line = bar._build_two_line_status()
    assert "всего медиа        3" in line
    bar.close()


def test_two_line_status_media_count_from_objects_ignores_batching_lag():
    # SESSION-HANDOFF.txt п.1: run_analyze()'s бар (media_count_from_objects=True) -- "всего
    # медиа" растёт по write_object_line()'s n_found ПРИ ВХОДЕ в объект, не дожидаясь update()
    # -- живой баг был именно в задержке между "объект найден" и "экзифтул батч протегирован".
    bar = _two_line_bar(media_count_from_objects=True)
    bar.write_object_line("folder", "some/folder", 50)
    line = bar._build_two_line_status()
    assert "всего медиа       50" in line
    # update() (реальная обработка файлов) не задваивает счётчик отображения -- он по-прежнему
    # читает self._media_declared, не self.count.
    bar.update(3)
    line2 = bar._build_two_line_status()
    assert "всего медиа       50" in line2
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
    bar = m.ProgressReporter(total=10, desc="Просматриваю уже собранный архив", unit="файл",
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
    assert bar._bar.descriptions[0] == "Просматриваю уже собранный архив — повтор              "
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


def _narrow_terminal(monkeypatch, columns: int):
    monkeypatch.setattr(m.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                         lambda fallback=(80, 24): m.os.terminal_size((columns, 24)))


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
