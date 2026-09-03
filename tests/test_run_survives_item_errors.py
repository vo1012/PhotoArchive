"""«Ни одна текущая ошибка не должна останавливать общий прогон» -- прямое требование
пользователя (2026-09-01, после боевых крашей `'int'.strip()` в camera_from_tags() и
`OSError [Errno 22]` в датах). Последние рубежи:
  * analyze_batch()      -- пофайловый try/except -> broken-запись, обход продолжается;
  * _run_impl()          -- пофайловый try/except вокруг _process_record() + внешний
                            рубеж на сбой самого обхода;
  * run_analyze()        -- то же (пофайловый + внешний).
Все тесты red-before-green: до правки соответствующее исключение пролетало наружу и
роняло весь прогон (main() -> crash.log -> выход)."""
import os

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _item(path, ftype="image", **kw):
    st = path.stat()
    kw.setdefault("read_path", str(path))
    kw.setdefault("origin_display", path.name)
    kw.setdefault("rel_path", path.name)
    kw.setdefault("size", st.st_size)
    kw.setdefault("mtime", st.st_mtime)
    kw.setdefault("ftype", ftype)
    return m.SourceItem(**kw)


class TestAnalyzeBatchSurvivesItemError:
    def test_unexpected_error_in_one_item_becomes_broken_record_not_a_crash(
            self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})

        def _boom(tags):
            raise RuntimeError("boom in camera_from_tags")

        monkeypatch.setattr(m, "camera_from_tags", _boom)
        recs = m.analyze_batch([_item(img)], tags_by_path={str(img): {"Make": "X"}})

        assert len(recs) == 1
        assert recs[0].broken is True
        assert recs[0].is_media is False
        assert recs[0].media_note == "processing_error"
        assert "RuntimeError" in (recs[0].read_error_msg or "")

    def test_except_fallback_record_carries_is_hidden(self, tmp_path, monkeypatch):
        """183-4: пофайловый except делает свежий SourceRecord -- is_hidden должен быть
        выставлен, как в первой строке _analyze_one_item()."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        monkeypatch.setattr(m, "is_hidden_path", lambda p: True)
        monkeypatch.setattr(m, "camera_from_tags",
                             lambda tags: (_ for _ in ()).throw(RuntimeError("boom")))
        recs = m.analyze_batch([_item(img)], tags_by_path={str(img): {"Make": "X"}})
        assert recs[0].broken is True
        assert recs[0].is_hidden is True

    def test_one_bad_item_does_not_lose_the_good_ones_in_the_same_batch(
            self, tmp_path, monkeypatch):
        good1, bad, good2 = tmp_path / "g1.jpg", tmp_path / "bad.jpg", tmp_path / "g2.jpg"
        _make_jpeg(good1)
        _make_jpeg(good2)
        _make_jpeg(bad, size=(321, 123))  # отличимый размер (read-once даёт байты, не путь)
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        real = m.image_phash_and_size

        def _flaky(src):
            if m.image_size_only(src) == (321, 123):
                raise ValueError("decode exploded")
            return real(src)

        monkeypatch.setattr(m, "image_phash_and_size", _flaky)
        recs = m.analyze_batch([_item(good1), _item(bad), _item(good2)])

        assert len(recs) == 3
        assert [r.broken for r in recs] == [False, True, False]
        assert recs[0].is_media and recs[2].is_media

    def test_keyboardinterrupt_from_item_still_propagates(self, tmp_path, monkeypatch):
        """Отмена (KeyboardInterrupt/_HardExit -- не Exception) НЕ должна глохнуть в
        пофайловом except."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        monkeypatch.setattr(m, "image_phash_and_size",
                             lambda p: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            m.analyze_batch([_item(img)])


class TestRunAnalyzeSurvivesItemError:
    def _cfg(self, tmp_path):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        return source, m.Config(source=str(source), target=str(target), sample_limit=0,
                                 workdir=str(workdir))

    def test_error_in_resolve_date_for_one_file_does_not_kill_the_analysis(
            self, tmp_path, monkeypatch):
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg")
        _make_jpeg(source / "b.jpg")
        real = m.resolve_date
        seen = []

        def _flaky(date_ctx, rel_path, *a, **kw):
            seen.append(rel_path)
            if len(seen) == 1:
                raise RuntimeError("resolve_date exploded")
            return real(date_ctx, rel_path, *a, **kw)

        monkeypatch.setattr(m, "resolve_date", _flaky)
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # must not raise

        assert stats is not None
        assert stats.n_objects_total >= 1
        assert len(seen) == 2  # обход дошёл до второго файла, не оборвался на первом
        # 183-1: проблемный файл ВИДЕН в отчёте, не в никуда.
        # 184-3: бакет disputed (файл прочитан, разбор упал -- rec.broken-класс), НЕ unreadable
        assert stats.n_processing_errors == 1
        assert len(stats.disputed_paths) == 1
        assert len(stats.disputed_records) == 1
        assert len(stats.unreadable_paths) == 0
        # одна ошибка среди валидных файлов -> НЕ систематический баг, исход обычный
        assert stats.walk_aborted is False

    def test_error_on_every_file_flags_walk_aborted_systematic_bug(self, tmp_path, monkeypatch):
        """183-2: пофайловый рубеж сработал на КАЖДОМ файле -> walk_aborted (систематический
        баг не должен выглядеть как успешный прогон). 184-2: файлов должно быть не меньше пола
        _MIN_SYSTEMATIC_ERR_ITERS -- «одна кривая фотка» систематическим багом не считается."""
        source, cfg = self._cfg(tmp_path)
        n = 6  # заведомо >= _MIN_SYSTEMATIC_ERR_ITERS (5)
        assert n >= m._MIN_SYSTEMATIC_ERR_ITERS
        for i in range(n):
            _make_jpeg(source / f"f{i}.jpg")
        monkeypatch.setattr(m, "resolve_date",
                             lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom on all")))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # must not raise

        assert stats.n_processing_errors == n
        assert stats.walk_aborted is True

    def test_error_on_every_file_in_a_tiny_folder_is_not_a_systematic_bug(
            self, tmp_path, monkeypatch):
        """184-2: папка из 1-2 файлов, у всех кривые метаданные -> обычный отчёт с этими
        файлами в «Спорных», НЕ экран «ошибка в самой программе». Red-before-green: до фикса
        n_processing_errors(2) == _iter_count(2) -> walk_aborted=True."""
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg")
        _make_jpeg(source / "b.jpg")
        monkeypatch.setattr(m, "resolve_date",
                             lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom on all")))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_processing_errors == 2
        assert stats.walk_aborted is False  # ниже пола -> не систематический баг
        assert len(stats.disputed_paths) == 2

    def test_user_interrupt_with_all_items_errored_is_not_flagged_as_systematic_bug(
            self, tmp_path, monkeypatch):
        """184-2: пользователь нажал «Прервать» (KeyboardInterrupt из обхода) на прогоне, где
        каждый зашедший файл дал пофайловую ошибку -- это «Работа прервана», не «баг в
        программе». Эвристика n_processing_errors == _iter_count не должна перебивать
        stats.interrupted. Red-before-green: до фикса walk_aborted=True (нет гейта)."""
        source, cfg = self._cfg(tmp_path)
        n_files = 7  # заведомо выше пола систематического бага
        for i in range(n_files):
            _make_jpeg(source / f"f{i}.jpg")
        real_walk = m._walk_with_exif_prefetch

        def _walk_then_cancel(*a, **kw):
            n = 0
            for pair in real_walk(*a, **kw):
                yield pair
                n += 1
                if n >= n_files:
                    raise KeyboardInterrupt

        monkeypatch.setattr(m, "_walk_with_exif_prefetch", _walk_then_cancel)
        monkeypatch.setattr(m, "resolve_date",
                             lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom on all")))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.interrupted is True
        assert stats.n_processing_errors == n_files  # ошибка на каждом зашедшем
        assert stats.walk_aborted is False  # 184-2: гейт not stats.interrupted

    def test_bare_launch_view_raises_aborted_run_report_on_walk_aborted(
            self, tmp_path, monkeypatch):
        source = tmp_path / "NewBatch"
        source.mkdir()
        for i in range(m._MIN_SYSTEMATIC_ERR_ITERS):  # 184-2: выше пола систематического бага
            _make_jpeg(source / f"f{i}.jpg")
        (tmp_path / "appdir").mkdir()
        monkeypatch.setattr(m, "WORKDIR", str(tmp_path / "appdir"))
        monkeypatch.setattr(m, "resolve_date",
                             lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(m._AbortedRunReport) as ei:
            m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
        # частичный отчёт всё равно есть
        assert getattr(ei.value, "report_path", None)
        assert isinstance(ei.value, m._InterruptedRunReport)  # подкласс -- старые except ловят

    def test_crash_in_the_walk_itself_finalizes_a_partial_report(self, tmp_path, monkeypatch):
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg")
        real_walk = m._walk_with_exif_prefetch

        def _exploding_walk(*a, **kw):
            n = 0
            for pair in real_walk(*a, **kw):
                yield pair
                n += 1
                if n >= 1:
                    raise RuntimeError("walker exploded mid-iteration")

        monkeypatch.setattr(m, "_walk_with_exif_prefetch", _exploding_walk)
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # must not raise

        assert stats is not None
        # 183-2: сбой обхода -> walk_aborted (не interrupted -- не пользователь нажал «Прервать»)
        assert stats.walk_aborted is True
        assert stats.interrupted is False


class TestRunImplSurvivesItemError:
    def _cfg(self, tmp_path, **over):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        kw = dict(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                  workdir=str(workdir))
        kw.update(over)
        return source, m.Config(**kw)

    def test_error_placing_one_file_does_not_abort_the_build(self, tmp_path, monkeypatch):
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg", color=(1, 2, 3))
        _make_jpeg(source / "b.jpg", color=(90, 80, 70))
        real = m._process_record
        calls = []

        def _flaky(rec, st, log=print):
            calls.append(rec)
            if len(calls) == 1:
                raise RuntimeError("placement exploded")
            return real(rec, st, log=log)

        monkeypatch.setattr(m, "_process_record", _flaky)
        result = m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)  # must not raise

        assert len(calls) == 2  # дошёл до второго файла
        # второй файл реально разложен в архив
        placed = [f for _r, _d, fs in os.walk(cfg.target) for f in fs if f.lower().endswith(".jpg")]
        assert placed
        assert result[6] is False  # walk_aborted -- 1 ошибка из 2 файлов, не систематический баг

    def test_systematic_decide_bug_flags_walk_aborted_not_a_silent_success(
            self, tmp_path, monkeypatch):
        """190-1 (Раунд 190 ревью, по прямой команде пользователя): 185-1(б) гармонизировал
        БАКЕТ (build-путь кладёт сбой разбора в disputed/_Unsorted, не unreadable), но не
        ИСХОД -- у _run_impl() не было счётчика систематического сбоя, какой уже есть у
        run_analyze() (183-2/184-2). Баг в decide()/resolve_date()/find_album() на КАЖДОМ
        файле при реальной сборке (≥ _MIN_SYSTEMATIC_ERR_ITERS) должен флагать walk_aborted
        -> _AbortedRunReport -> экран «Работа завершилась не полностью», а не тихо выглядеть
        успехом с архивом, целиком состоящим из свалки в _Unsorted."""
        source, cfg = self._cfg(tmp_path)
        n = 6
        assert n >= m._MIN_SYSTEMATIC_ERR_ITERS
        for i in range(n):
            _make_jpeg(source / f"f{i}.jpg", color=(i, i, i))

        calls = []

        def _always_boom(rec, st, log=print):
            calls.append(rec)
            raise RuntimeError("decide() exploded on every file")

        monkeypatch.setattr(m, "_process_record", _always_boom)
        result = m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)  # must not raise

        assert len(calls) == n
        assert result[6] is True  # walk_aborted
        # безопасность 185-1(б) не пострадала -- все файлы физически в _Unsorted, не потеряны
        unsorted_jpgs = [f for _r, _d, fs in os.walk(cfg.dispute) for f in fs
                          if f.lower().endswith(".jpg")]
        assert len(unsorted_jpgs) == n

    def test_a_few_decide_errors_among_many_good_files_is_not_systematic(
            self, tmp_path, monkeypatch):
        """184-2's пол (_MIN_SYSTEMATIC_ERR_ITERS) для build-пути: маленькая папка, где ВСЕ
        файлы дают сбой, но их меньше пола, -- не систематический баг программы, обычный
        исход (тот же принцип, что test_error_on_every_file_in_a_tiny_folder_is_not_a_
        systematic_bug у run_analyze)."""
        source, cfg = self._cfg(tmp_path)
        n = 3
        assert n < m._MIN_SYSTEMATIC_ERR_ITERS
        for i in range(n):
            _make_jpeg(source / f"f{i}.jpg", color=(i, i, i))

        monkeypatch.setattr(
            m, "_process_record",
            lambda rec, st, log=print: (_ for _ in ()).throw(RuntimeError("boom")))
        result = m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)  # must not raise

        assert result[6] is False  # walk_aborted -- ниже пола, не систематический баг

    def test_bare_launch_build_raises_aborted_run_report_on_systematic_decide_bug(
            self, tmp_path, monkeypatch):
        """190-1: ось «подключено ли реально» -- walk_aborted из _run_impl() обязан реально
        долетать через RunResult до _bare_launch_run_build() и поднимать _AbortedRunReport
        (та же развязка, что уже проверена для _bare_launch_run_view()/run_analyze() выше),
        не просто выставляться и игнорироваться вызывающим кодом."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        n = m._MIN_SYSTEMATIC_ERR_ITERS
        for i in range(n):
            _make_jpeg(source / f"f{i}.jpg", color=(i, i, i))
        monkeypatch.setattr(
            m, "_process_record",
            lambda rec, st, log=print: (_ for _ in ()).throw(RuntimeError("boom on all")))

        with pytest.raises(m._AbortedRunReport) as ei:
            m._bare_launch_run_build([str(source)], str(target),
                                      input_fn=lambda *a, **k: "да", log=lambda *a, **k: None)
        assert getattr(ei.value, "report_path", None)  # частичный отчёт всё равно есть
        # все файлы физически попали в _Unsorted, не потеряны
        unsorted_jpgs = [f for _r, _d, fs in os.walk(os.path.join(str(target), "_Unsorted"))
                          for f in fs if f.lower().endswith(".jpg")]
        assert len(unsorted_jpgs) == n

    def test_error_placing_one_file_is_disputed_into_unsorted_not_unreadable(
            self, tmp_path, monkeypatch):
        """185-1(б), ответ на REVIEW-HANDOFF.md (по прямой команде пользователя): исключение,
        долетевшее до внешнего except в _run_impl() (баг в decide()/resolve_date()/find_album()
        -- симулируется тем же приёмом, что и test_error_placing_one_file_does_not_abort_the_
        build выше), по конструкции происходит СТРОГО ДО place_file() для этого файла -- теперь
        безопасно разложить в _Unsorted как "спорный", тем же путём, что уже делает run_analyze
        для того же класса сбоя (184-3), а не молчаливо считать «не прочитано» для файла,
        который на самом деле был прочитан. Red-before-green: до 185-1(б) уходил в
        unreadable.csv, _Unsorted не получал файл вовсе."""
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg", color=(1, 2, 3))
        _make_jpeg(source / "b.jpg", color=(90, 80, 70))
        real = m._process_record
        calls = []

        def _flaky(rec, st, log=print):
            calls.append(rec)
            if len(calls) == 1:
                raise RuntimeError("decide() exploded")
            return real(rec, st, log=log)

        monkeypatch.setattr(m, "_process_record", _flaky)
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)  # must not raise

        disputes_csv = os.path.join(cfg.logs, "disputes.csv")
        unreadable_csv = os.path.join(cfg.logs, "unreadable.csv")
        with open(disputes_csv, encoding="utf-8") as f:
            disputes_text = f.read()
        with open(unreadable_csv, encoding="utf-8") as f:
            unreadable_text = f.read()
        assert "a.jpg" in disputes_text
        assert "processing_error" in disputes_text
        assert "a.jpg" not in unreadable_text
        unsorted_jpgs = [f for _r, _d, fs in os.walk(cfg.dispute) for f in fs
                          if f.lower().endswith(".jpg")]
        assert unsorted_jpgs  # физически лежит в _Unsorted, не потерян

    def test_bookkeeping_failure_after_placement_does_not_duplicate_the_file(
            self, tmp_path, monkeypatch):
        """185-1(б): place_file() уже отработал успешно, сбой в бухгалтерии ПОСЛЕ него
        (pool.add()) НЕ должен пытаться разместить файл повторно -- см.
        _log_post_placement_bookkeeping_failure(). Файл обязан остаться на диске РОВНО один
        раз (в архиве, не задвоен в _Unsorted)."""
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg", color=(1, 2, 3))

        def _boom(*a, **kw):
            raise RuntimeError("pool bookkeeping exploded")

        monkeypatch.setattr(m, "PoolEntry", _boom)
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)  # must not raise

        all_jpgs = [os.path.join(r, f) for r, _d, fs in os.walk(cfg.target)
                    for f in fs if f.lower().endswith(".jpg")]
        assert len(all_jpgs) == 1  # ровно один физический файл -- не задвоен
        assert not any(p.startswith(cfg.dispute + os.sep) for p in all_jpgs)  # не ушёл в _Unsorted
        actions_log = os.path.join(cfg.logs, "actions.log")
        with open(actions_log, encoding="utf-8") as f:
            actions_text = f.read()
        assert "[bookkeeping_incomplete]" in actions_text  # новая ветка реально сработала

    def test_seed_archive_cache_failure_is_not_counted_as_write_failed(
            self, tmp_path, monkeypatch):
        """191-1 (Раунд 191 ревью, по прямой команде пользователя): 190-3 перенёс
        _seed_archive_cache() из «размещенческого» try (рядом с place_file()) в
        «бухгалтерский» (рядом с pool.add()) -- но без отдельного теста ничего не покраснело
        бы, если рефакторинг молча вернёт его назад и восстановит старый mislabel. Сбой
        сидинга кэша (БД занята/полна и т.п.) НЕ означает "не удалось записать файл" --
        place_file() к этому моменту уже отработал успешно."""
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg", color=(1, 2, 3))

        monkeypatch.setattr(
            m, "_seed_archive_cache",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("cache seed exploded")))
        stats, *_rest = m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        assert stats.get("write_failed", 0) == 0  # НЕ засчитан как сбой записи
        all_jpgs = [os.path.join(r, f) for r, _d, fs in os.walk(cfg.target)
                    for f in fs if f.lower().endswith(".jpg")]
        assert len(all_jpgs) == 1  # файл на диске, корректно размещён, не задвоен
        actions_log = os.path.join(cfg.logs, "actions.log")
        with open(actions_log, encoding="utf-8") as f:
            actions_text = f.read()
        assert "[bookkeeping_incomplete]" in actions_text  # сбой виден в логе, не проглочен
