"""Двухфазный обход SourceWalker (2026-08-03, речь пользователя): "если при пополнении архива
первой встречается папка, которая разберётся как ByDate, а тот же файл дальше встретится в
альбоме -- что произойдёт?" Раньше исход зависел только от алфавитного порядка обхода SOURCE
(_walk_dir()'s sorted(os.listdir())) -- дамп-папка, стоящая раньше альбома по алфавиту,
"забирала" файл себе в пул дедупа первой, и настоящий альбом ловил skipped_present. Теперь Фаза 1
(SourceWalker.walk()) обходит только альбомные ветки (см. _is_terminal_bydate_branch()), Фаза 2 --
отложенные тильда/dump-архивы, Фаза 3 -- отравленные/безальбомные ветки -- см. find_album()'s
докстринг про "отравление ветки" и "архив == папка" (тот же запрос пользователя, тот же коммит).

zipfile напрямую (без bin/7z.exe) -- .zip читается встроенным zipfile-бэкендом
(detect_archive_format()/list_archive()), тот же паттерн, что и в остальных тестах архивов
этого репозитория."""
import shutil
import tempfile
import zipfile

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


@pytest.fixture(autouse=True)
def _no_exiftool(monkeypatch):
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})


def _run(tmp_path, build_source):
    source = tmp_path / "source"
    source.mkdir()
    build_source(source)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))
    m.run(cfg, log=lambda *a, **k: None)
    albums_root = target / "Albums"
    bydate_root = target / "ByDate"
    album_files = sorted(str(p.relative_to(target)) for p in albums_root.rglob("*.jpg")) \
        if albums_root.exists() else []
    bydate_files = sorted(str(p.relative_to(target)) for p in bydate_root.rglob("*.jpg")) \
        if bydate_root.exists() else []
    return album_files, bydate_files


class TestAlbumWinsDedupRace:
    def test_album_wins_against_alphabetically_earlier_dump_folder(self, tmp_path):
        # "DCIM" (dump, default name) sorts before "ZZZ_RealAlbum" alphabetically -- under the
        # old single-pass, alphabetically-ordered walk, DCIM would be processed FIRST and claim
        # the pool slot, and the real album's copy would be skipped as a duplicate instead.
        def build(source):
            dump_dir = source / "DCIM"
            dump_dir.mkdir()
            album_dir = source / "ZZZ_RealAlbum"
            album_dir.mkdir()
            _make_jpeg(dump_dir / "photo.jpg")
            shutil.copyfile(dump_dir / "photo.jpg", album_dir / "photo_copy.jpg")

        album_files, bydate_files = _run(tmp_path, build)
        assert len(album_files) == 1, f"expected the album to keep the one physical copy, got {album_files}"
        assert bydate_files == [], f"expected the dump-folder duplicate to be skipped, not copied, got {bydate_files}"

    def test_dump_wins_when_no_album_exists_anywhere(self, tmp_path):
        # Control: with no real album in SOURCE at all, the file (wherever first found) still
        # lands in ByDate as before -- the fix doesn't invent an album out of nothing.
        def build(source):
            dump_dir = source / "DCIM"
            dump_dir.mkdir()
            _make_jpeg(dump_dir / "photo.jpg")

        album_files, bydate_files = _run(tmp_path, build)
        assert album_files == []
        assert len(bydate_files) == 1


class TestPoisonedBranchEndToEnd:
    def test_tilde_folder_nested_inside_real_album_goes_to_bydate(self, tmp_path):
        def build(source):
            d = source / "RealAlbum" / "~synced"
            d.mkdir(parents=True)
            _make_jpeg(d / "photo.jpg")

        album_files, bydate_files = _run(tmp_path, build)
        assert album_files == [], f"expected nothing under Albums, got {album_files}"
        assert len(bydate_files) == 1

    def test_sibling_file_in_same_album_is_unaffected_by_poison(self, tmp_path):
        def build(source):
            album = source / "RealAlbum"
            (album / "~synced").mkdir(parents=True)
            # distinct colors -- must NOT be byte-identical, otherwise "junk" is correctly
            # recognized as an exact duplicate of "normal" and legitimately skipped rather than
            # copied a second time, which would defeat the point of this specific test (placement
            # in isolation from dedup, not the dedup mechanism itself -- that's the class above).
            _make_jpeg(album / "~synced" / "junk.jpg", color=(200, 50, 90))
            _make_jpeg(album / "normal.jpg", color=(10, 20, 30))

        album_files, bydate_files = _run(tmp_path, build)
        assert len(album_files) == 1 and "normal.jpg" in album_files[0]
        assert len(bydate_files) == 1 and "junk.jpg" in bydate_files[0]


class TestArchiveIndependenceEndToEnd:
    def _zip_with_one_photo(self, zpath):
        with zipfile.ZipFile(zpath, "w") as zf:
            tmp_dir = tempfile.mkdtemp()
            img = f"{tmp_dir}/inner.jpg"
            _make_jpeg(img)
            zf.write(img, "inner.jpg")

    def test_tilde_named_archive_inside_real_album_goes_to_bydate(self, tmp_path):
        def build(source):
            album = source / "RealAlbum"
            album.mkdir()
            self._zip_with_one_photo(album / "~backup.zip")

        album_files, bydate_files = _run(tmp_path, build)
        assert album_files == [], f"expected nothing under Albums, got {album_files}"
        assert len(bydate_files) == 1

    def test_normal_named_archive_kept_as_subfolder_even_with_single_file(self, tmp_path):
        # 2026-08-03: "архив == папка" -- no file-count exception, even a single-photo archive
        # keeps its own name as a subfolder level under the album (see find_album()).
        def build(source):
            album = source / "RealAlbum"
            album.mkdir()
            self._zip_with_one_photo(album / "vacation.zip")

        album_files, bydate_files = _run(tmp_path, build)
        assert bydate_files == []
        assert len(album_files) == 1
        assert "RealAlbum/vacation/" in album_files[0].replace("\\", "/")


class TestWalkerPhaseOrdering:
    """Более дешёвая (без реального Pool/decide()) проверка самого порядка yield -- напрямую
    через SourceWalker.walk(), без полного _run_impl()."""

    def _cfg(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (tmp_path / "target").mkdir()
        return m.Config(source=str(source), target=str(tmp_path / "target"))

    def test_album_items_come_before_deferred_tilde_archive_items(self, tmp_path):
        cfg = self._cfg(tmp_path)
        source = tmp_path / "source"
        album = source / "AAA_RealAlbum"  # sorts BEFORE the tilde archive alphabetically anyway --
        album.mkdir()                     # the point is it must win regardless of alphabetical order
        _make_jpeg(album / "normal.jpg")
        self._zip_with_one_photo(album / "~backup.zip")

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        kinds = ["album" if "backup" not in it.rel_path else "tilde_archive" for it in items]
        assert kinds == ["album", "tilde_archive"], kinds

    def _zip_with_one_photo(self, zpath):
        with zipfile.ZipFile(zpath, "w") as zf:
            tmp_dir = tempfile.mkdtemp()
            img = f"{tmp_dir}/inner.jpg"
            _make_jpeg(img)
            zf.write(img, "inner.jpg")

    def test_album_items_come_before_poisoned_branch_items(self, tmp_path):
        cfg = self._cfg(tmp_path)
        source = tmp_path / "source"
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "normal.jpg")
        (album / "~synced").mkdir()
        _make_jpeg(album / "~synced" / "junk.jpg")

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        kinds = ["album" if "junk" not in it.rel_path else "poisoned"
                 for it in items]
        assert kinds == ["album", "poisoned"], kinds

    def test_no_files_are_lost_across_phases(self, tmp_path):
        # Regression guard for the walk()-returns-early bug this same change introduced and
        # fixed (single-archive-file SOURCE bypassing phase 2/3 draining entirely).
        cfg = self._cfg(tmp_path)
        source = tmp_path / "source"
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "normal.jpg")
        self._zip_with_one_photo(album / "~backup.zip")
        (album / "~synced").mkdir()
        _make_jpeg(album / "~synced" / "junk.jpg")

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert len(items) == 3, [it.rel_path for it in items]


class TestArchiveExtractDirNotCleanedBeforeDrain:
    """REVIEW-HANDOFF.md, Раунд 58 [БЛОКЕР]: архив с ОБЫЧНЫМ именем (распаковывается сразу в
    Фазе 1) содержащий dump-подветку внутри -- _handle_archive()'s `finally: cleanup_dir()`
    чистил temp-распакованную папку сразу же, как только её собственный _walk_dir()
    заканчивался, но "закончился" больше не значит "всё физически посещено" -- отложенная
    dump-ветка (Фаза 3) ссылается на путь ВНУТРИ уже удалённой папки. Живой репорт: файл из
    dump-подветки архива пропадал ПОЛНОСТЬЮ -- не в Albums/ByDate, ни в одном CSV-логе."""

    def _zip_with_two_entries(self, zpath, dump_name, album_name):
        with zipfile.ZipFile(zpath, "w") as zf:
            dump_img = tempfile.mktemp(suffix=".jpg")
            _make_jpeg(dump_img, color=(10, 20, 30))
            zf.write(dump_img, f"DCIM/{dump_name}")
            album_img = tempfile.mktemp(suffix=".jpg")
            _make_jpeg(album_img, color=(200, 50, 90))
            zf.write(album_img, f"RealAlbum/{album_name}")

    def test_dump_branch_inside_normally_named_archive_survives(self, tmp_path):
        def build(source):
            self._zip_with_two_entries(source / "myarchive.zip", "dump.jpg", "album.jpg")

        album_files, bydate_files = _run(tmp_path, build)
        assert len(album_files) == 1 and "album.jpg" in album_files[0]
        assert len(bydate_files) == 1 and "dump.jpg" in bydate_files[0], \
            f"dump-branch file lost, only got: {bydate_files}"

    def test_archive_entirely_dump_content_survives(self, tmp_path):
        # Reviewer's "worse" case: the WHOLE archive is dump content (typical phone/camera
        # export, no album at all) -- used to vanish completely, summary.txt claiming 0 files.
        def build(source):
            zpath = source / "myarchive.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                for name in ("a.jpg", "b.jpg"):
                    img = tempfile.mktemp(suffix=".jpg")
                    _make_jpeg(img, color=(10, 20, 30) if name == "a.jpg" else (200, 50, 90))
                    zf.write(img, f"DCIM/{name}")

        album_files, bydate_files = _run(tmp_path, build)
        assert album_files == []
        assert len(bydate_files) == 2, bydate_files

    def test_tilde_named_inner_archive_inside_normal_outer_archive_survives(self, tmp_path):
        # Same lifetime bug, different deferred list (_deferred_tilde_archives instead of
        # _deferred_bydate_roots): the inner tilde-archive FILE itself lives inside the outer
        # archive's extract_dir.
        def build(source):
            inner_zip = tempfile.mktemp(suffix=".zip")
            inner_img = tempfile.mktemp(suffix=".jpg")
            _make_jpeg(inner_img, color=(200, 50, 90))
            with zipfile.ZipFile(inner_zip, "w") as zf:
                zf.write(inner_img, "inner.jpg")

            outer_img = tempfile.mktemp(suffix=".jpg")
            _make_jpeg(outer_img, color=(10, 20, 30))
            with zipfile.ZipFile(source / "myarchive.zip", "w") as zf:
                zf.write(outer_img, "RealAlbum/photo1.jpg")
                zf.write(inner_zip, "RealAlbum/~backup.zip")

        album_files, bydate_files = _run(tmp_path, build)
        assert len(album_files) == 1 and "photo1.jpg" in album_files[0]
        assert len(bydate_files) == 1 and "inner.jpg" in bydate_files[0], \
            f"tilde-archive content lost, only got: {bydate_files}"

    def test_walker_level_no_pending_cleanup_dirs_left_unprocessed(self, tmp_path):
        """Более прямая проверка на уровне SourceWalker: после полного walk() список
        self._pending_cleanup_dirs существует и непуст (подтверждает, что механизм отложенной
        очистки реально сработал для этого архива, не просто "случайно ничего не потерялось"),
        и все перечисленные там пути к этому моменту уже физически удалены (сам walk()
        вычищает их в конце _drain_deferred_phases())."""
        source = tmp_path / "source"
        source.mkdir()
        self._zip_with_two_entries(source / "myarchive.zip", "dump.jpg", "album.jpg")
        target = tmp_path / "target"
        target.mkdir()
        cfg = m.Config(source=str(source), target=str(target))
        walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
        items = list(walker.walk())
        assert len(items) == 2, [it.rel_path for it in items]
        assert len(walker._pending_cleanup_dirs) == 1
        assert not __import__("os").path.exists(walker._pending_cleanup_dirs[0])


class TestDeferredRootReportsTransientOp:
    """REVIEW-HANDOFF.md, Раунд 58 [ЗАМЕЧАНИЕ]: переход в отложенное поддерево Фазы 3 не
    подавал никакого сигнала в progress-бар -- время до первого yield'а из такого поддерева
    (os.listdir() + сниффинг типа каждого файла, см. sibling_by_base в _walk_dir()) целиком
    приписывалось "одному файлу" ближайшим EMA-тиком (тот же класс искажения плана, что уже
    решён для распаковки архива через set_transient_op()/_pending_heavy_time, см.
    ProgressReporter). Фикс открывает сегмент безусловно на входе в КАЖДУЮ папку Фазы 1 (см.
    _open_deferred_gap()'s докстринг в __init__()) -- для обычной папки-альбома открытие и
    закрытие идут практически подряд (первый же файл обычно и есть первый yield), для
    dump-ветки сегмент реально остаётся открытым. Тесты ниже проверяют именно этот инвариант
    (корректно чередующиеся open/close, ничего не остаётся висеть открытым), не точное число
    вызовов -- то, сколько именно папок обошёл конкретный SOURCE, деталь реализации."""

    def _assert_calls_are_balanced_open_close_pairs(self, calls):
        open_now = False
        for call in calls:
            if open_now:
                assert call is None, f"expected a close (None) after an open, got: {calls}"
            else:
                assert call is not None, f"expected an open (str) before a close, got: {calls}"
            open_now = call is not None
        assert not open_now, f"transient op left open at the end of the walk: {calls}"

    def test_transient_op_balanced_with_deferred_content_present(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        album = source / "AlbumReal"
        album.mkdir()
        _make_jpeg(album / "normal.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        _make_jpeg(dump / "dump.jpg", color=(200, 50, 90))

        (tmp_path / "target").mkdir()
        cfg = m.Config(source=str(source), target=str(tmp_path / "target"))
        calls = []
        walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                                 transient_op_cb=calls.append)
        items = list(walker.walk())
        assert len(items) == 2
        assert calls  # the mechanism did fire at least once
        self._assert_calls_are_balanced_open_close_pairs(calls)

    def test_transient_op_balanced_with_nothing_deferred(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        album = source / "RealAlbum"
        album.mkdir()
        _make_jpeg(album / "normal.jpg")

        (tmp_path / "target").mkdir()
        cfg = m.Config(source=str(source), target=str(tmp_path / "target"))
        calls = []
        walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                                 transient_op_cb=calls.append)
        items = list(walker.walk())
        assert len(items) == 1
        # Every folder visit (even a plain album with no dump content at all) opens the gap
        # defensively before its own os.listdir() -- see the fix's docstring -- and closes it
        # again at the very next yield, so SOME calls are expected here too; what matters is
        # that they stay correctly balanced and closed by the end.
        self._assert_calls_are_balanced_open_close_pairs(calls)

    def test_ema_rate_not_skewed_across_many_deferred_files(self, tmp_path, monkeypatch):
        """Сквозная проверка через настоящий ProgressReporter (тот же класс, что рисует
        статус-строку в проде): реальное время, которое _walk_dir() тратит на классификацию
        КАЖДОГО файла плоской dump-папки (здесь смоделировано монки-патчем os.stat -- он
        вызывается ровно один раз на файл в основном цикле files, см. _walk_dir()), не должно
        попасть в instantaneous ближайшего update(), пока хотя бы один файл уже отложен (гейт
        открывается на ПЕРВОМ отложенном файле -- см. _open_deferred_gap()'s докстринг: cтоимость
        именно ЭТОГО первого файла остаётся неисключённой, это осознанный, маленький и
        ограниченный остаток, не путать с оставшимися N-1). На реальной dump-папке с тысячами
        файлов доминирует именно эта, N-1-часть -- ровно она и проверяется здесь."""
        source = tmp_path / "source"
        source.mkdir()
        # "AlbumReal" sorts before "DCIM" -- gets visited and yielded first, so the deferral
        # gap opens strictly AFTER our baseline tick, not folded into the same next() call.
        album = source / "AlbumReal"
        album.mkdir()
        _make_jpeg(album / "normal.jpg")
        dump = source / "DCIM"
        dump.mkdir()
        n_dump_files = 20
        for i in range(n_dump_files):
            _make_jpeg(dump / f"dump{i}.jpg", color=(200, 50, 90))

        (tmp_path / "target").mkdir()
        cfg = m.Config(source=str(source), target=str(tmp_path / "target"))
        bar = m.ProgressReporter(total=None, desc="Разбираю и копирую файлы", unit="файл",
                                  two_line=True)

        real_time = m.time.time
        fake_now = [real_time()]
        monkeypatch.setattr(m.time, "time", lambda: fake_now[0])

        real_stat = m.os.stat
        stat_calls_on_dump = []

        def _slow_stat(path, *a, **kw):
            if "DCIM" in str(path):
                stat_calls_on_dump.append(path)
                fake_now[0] += 30.0  # each dump file "costs" 30s of simulated classification
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(m.os, "stat", _slow_stat)

        walker = m.SourceWalker(cfg, log=lambda *a, **k: None,
                                 transient_op_cb=bar.set_transient_op)
        it = walker.walk()

        first_item = next(it)  # AlbumReal/normal.jpg
        fake_now[0] += 0.01
        bar.update(1)
        baseline = bar._ema_rate

        remaining = [next(it) for _ in range(n_dump_files)]
        fake_now[0] += 0.01
        bar.update(1)

        assert stat_calls_on_dump  # sanity: the hook actually fired on DCIM's content
        assert {first_item.rel_path, *(r.rel_path for r in remaining)} == {
            "AlbumReal/normal.jpg", *(f"DCIM/dump{i}.jpg" for i in range(n_dump_files))}
        # Without the fix this would jump toward hundreds of s/файл (nearly all of the
        # simulated per-file cost across 20 dump files misattributed to a single tick) --
        # with it, only a small, bounded residual (roughly one file's worth, unavoidably
        # incurred before the gap is even known to be needed -- see docstring) leaks through.
        assert bar._ema_rate < baseline + 100.0
        bar.close()
