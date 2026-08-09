"""analyze_batch() -- exact-dup phash short-circuit (review round 4 finding,
REVIEW-HANDOFF.md): decide() always checks pool.find_exact(sha256) before ever reading
rec.phash/rec.aspect for image/video (see TestDecide in test_pool_dedup.py), so a file that's
already an exact duplicate of something in the pool shouldn't pay for the expensive phash
decode (image_phash_and_size: full PIL decode + DCT; video_phash_3frames: three ffmpeg spawns).
exiftool_batch()/ffprobe-backed helpers are stubbed out here -- this suite is about the
phash short-circuit, not about exercising the real bundled binaries."""
import hashlib
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _item(path, ftype="image", **kwargs):
    st = path.stat()
    kwargs.setdefault("read_path", str(path))
    kwargs.setdefault("origin_display", path.name)
    kwargs.setdefault("rel_path", path.name)
    kwargs.setdefault("size", st.st_size)
    kwargs.setdefault("mtime", st.st_mtime)
    kwargs.setdefault("ftype", ftype)
    return m.SourceItem(**kwargs)


@pytest.fixture(autouse=True)
def _no_exiftool(monkeypatch):
    # analyze_batch always calls exiftool_batch() first, regardless of dup status -- not
    # part of this finding, stub it out so tests don't need the real bundled binary.
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})


class TestImageExactDupSkipsPhash:
    def test_no_pool_computes_phash_as_before(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        recs = m.analyze_batch([_item(img)])

        assert len(calls) == 1
        assert recs[0].phash is not None

    def test_pool_miss_still_computes_phash(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256="f" * 64, ftype="image", dest_path="other.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)

        assert len(calls) == 1
        assert recs[0].phash is not None

    def test_exact_dup_in_pool_skips_phash(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        sha = hashlib.sha256(img.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="image", dest_path="existing.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)
        rec = recs[0]

        assert calls == []  # the expensive decode was never called
        assert rec.phash is None
        assert rec.sha256 == sha
        # is_media/classify_image must still see real width/height from the cheap
        # image_size_only() path -- decide() gates on rec.is_media BEFORE the ftype branches
        # that check for an exact dup, so this can't be starved by skipping phash.
        assert rec.is_media is True
        assert (rec.width, rec.height) == (800, 600)

    def test_exact_dup_decide_result_unaffected_by_skipped_phash(self, tmp_path):
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        sha = hashlib.sha256(img.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="image", dest_path="existing.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)
        decision = m.decide(pool, recs[0])

        assert decision.decision == "skipped_present"
        assert decision.matched_dest == "existing.jpg"

    def test_skip_hash_with_pool_given_never_marks_exact_dup(self, tmp_path, monkeypatch):
        # skip_hash=True (analyze-quick) never computes sha256 -- exact_dup must stay False
        # even if a pool happens to be passed, not crash on rec.sha256 being None.
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256="0" * 64, ftype="image", dest_path="other.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool, skip_hash=True)

        assert calls == []  # skip_hash's own cheap path, not the exact-dup one
        assert recs[0].sha256 is None
        assert recs[0].phash is None
        assert (recs[0].width, recs[0].height) == (800, 600)


class TestVideoExactDupSkipsPhash:
    def _video_item(self, tmp_path, ftype="video"):
        vid = tmp_path / "a.mp4"
        vid.write_bytes(b"not a real video, duration/phash are stubbed below")
        return vid, _item(vid, ftype=ftype)

    def test_pool_miss_still_computes_phash(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda p: (2.0, 640, 480, 1000))
        monkeypatch.setattr(m, "video_phash_3frames",
                             lambda p, d: (calls.append(p), ["a" * 16, "b" * 16, "c" * 16])[1])

        vid, item = self._video_item(tmp_path)
        recs = m.analyze_batch([item], pool=m.Pool())

        assert len(calls) == 1
        assert recs[0].phash == "a" * 16 + "|" + "b" * 16 + "|" + "c" * 16

    def test_exact_dup_skips_phash(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda p: (2.0, 640, 480, 1000))
        monkeypatch.setattr(m, "video_phash_3frames",
                             lambda p, d: (calls.append(p), ["a" * 16, "b" * 16, "c" * 16])[1])

        vid, item = self._video_item(tmp_path)
        sha = hashlib.sha256(vid.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="video", dest_path="existing.mp4", size=1))

        recs = m.analyze_batch([item], pool=pool)
        rec = recs[0]

        assert calls == []
        assert rec.phash is None
        assert rec.is_media is True
        assert rec.duration == 2.0

        decision = m.decide(pool, rec)
        assert decision.decision == "skipped_present"
        assert decision.matched_dest == "existing.mp4"


class TestArchiveHashCacheReuse:
    """Задача 7 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): analyze_batch(cache=...)
    -- то же (path,size,mtime)-валидное попадание в archive_cache, что уже применяет
    index_archive() к обычной сборке, теперь доступно и вызывающей стороне run_analyze()
    (mode="analyze", т.е. "Паспорт архива") -- не пересчитывать sha256/phash/duration с нуля
    для файла, чьи хеш уже известен и валиден."""

    def test_image_cache_hit_skips_sha256_and_phash(self, tmp_path, monkeypatch):
        sha_calls, phash_calls = [], []
        monkeypatch.setattr(m, "sha256_file_with_retry",
                             lambda p, *a, **kw: (sha_calls.append(p), "real-sha")[1])
        real_phash = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size",
                             lambda p: (phash_calls.append(p), real_phash(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (it.size, it.mtime, "cached-sha", "cached-phash", None, 111, 222, None)}

        recs = m.analyze_batch([it], cache=cache)
        rec = recs[0]

        assert sha_calls == []  # sha256_file_with_retry never invoked -- cache supplied it
        assert phash_calls == []  # image_phash_and_size never invoked either
        assert rec.sha256 == "cached-sha"
        assert rec.phash == "cached-phash"
        assert (rec.width, rec.height) == (111, 222)

    def test_image_cache_miss_size_mismatch_recomputes(self, tmp_path, monkeypatch):
        sha_calls = []
        monkeypatch.setattr(m, "sha256_file_with_retry",
                             lambda p, *a, **kw: (sha_calls.append(p), "real-sha")[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        # size deliberately wrong -- cache entry must be treated as stale, not trusted.
        cache = {str(img): (it.size + 1, it.mtime, "stale-sha", "stale-phash", None, 1, 1, None)}

        recs = m.analyze_batch([it], cache=cache)

        assert sha_calls == [str(img)]  # real hashing DID run -- cache was correctly rejected
        assert recs[0].sha256 == "real-sha"

    def test_image_cache_miss_mtime_mismatch_recomputes(self, tmp_path, monkeypatch):
        sha_calls = []
        monkeypatch.setattr(m, "sha256_file_with_retry",
                             lambda p, *a, **kw: (sha_calls.append(p), "real-sha")[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (it.size, it.mtime + 5.0, "stale-sha", "stale-phash", None, 1, 1, None)}

        recs = m.analyze_batch([it], cache=cache)

        assert sha_calls == [str(img)]
        assert recs[0].sha256 == "real-sha"

    def test_image_cache_hit_still_correct_with_exact_dup_pool(self, tmp_path):
        """cache_hit -- phash приходит бесплатно из кэша, поэтому НЕ нулится даже при
        exact_dup (в отличие от свежепосчитанного пути, см. докстринг analyze_batch()) --
        decide() всё равно не читает rec.phash в этом случае, значение просто не мешает."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (it.size, it.mtime, "cached-sha", "cached-phash", None, 800, 600, None)}
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256="cached-sha", ftype="image", dest_path="existing.jpg", size=1))

        recs = m.analyze_batch([it], pool=pool, cache=cache)
        rec = recs[0]

        assert rec.phash == "cached-phash"
        decision = m.decide(pool, rec)
        assert decision.decision == "skipped_present"

    def test_video_cache_hit_skips_duration_and_phash(self, tmp_path, monkeypatch):
        duration_calls, phash_calls = [], []
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda p: (duration_calls.append(p), (2.0, 640, 480, 1000))[1])
        monkeypatch.setattr(m, "video_phash_3frames",
                             lambda p, d: (phash_calls.append(p), ["x" * 16] * 3)[1])
        monkeypatch.setattr(m, "sha256_file_with_retry", lambda p, *a, **kw: "real-sha")

        vid = tmp_path / "a.mp4"
        vid.write_bytes(b"not a real video, duration/phash are stubbed")
        it = _item(vid, ftype="video")
        cache = {str(vid): (it.size, it.mtime, "cached-sha", "cached-phash", 9.5, 320, 240, 500)}

        recs = m.analyze_batch([it], cache=cache)
        rec = recs[0]

        assert duration_calls == []
        assert phash_calls == []
        assert (rec.duration, rec.width, rec.height, rec.bitrate) == (9.5, 320, 240, 500)
        assert rec.phash == "cached-phash"

    def test_raw_cache_hit_reads_width_height_from_cache_too(self, tmp_path, monkeypatch):
        """Речь пользователя, 2026-08-02: раньше RAW-ветка ИГНОРИРОВАЛА cache_hit для width/
        height, всегда читая их из tags (exiftool) -- на exif_cache_hit=True (кэш пуст в tags,
        см. _tag_prefetch_pairs()/_exif_cache_ready()) width/height ошибочно стали бы None,
        хотя _seed_archive_cache() и раньше писала их для RAW не хуже, чем для image/video
        (тот же общий индекс cached[5]/cached[6]). Исправлено -- RAW теперь читает
        width/height из кэша на cache_hit, тем же паттерном, что image/video."""
        sha_calls = []
        monkeypatch.setattr(m, "sha256_file_with_retry",
                             lambda p, *a, **kw: (sha_calls.append(p), "real-sha")[1])
        raw = tmp_path / "a.cr2"
        raw.write_bytes(b"fake raw bytes")
        it = _item(raw, ftype="raw")
        cache = {str(raw): (it.size, it.mtime, "cached-sha", None, None, 999, 999, None)}

        recs = m.analyze_batch([it], cache=cache)
        rec = recs[0]

        assert sha_calls == []
        assert rec.sha256 == "cached-sha"
        assert (rec.width, rec.height) == (999, 999)  # теперь тоже из кэша, не из tags

    def test_raw_cache_miss_still_reads_width_height_from_tags(self, tmp_path, monkeypatch):
        """Дополняет тест выше: без cache_hit RAW по-прежнему берёт width/height из EXIF-тегов
        (реального кэш-попадания нет -- нечего переиспользовать)."""
        raw = tmp_path / "a.cr2"
        raw.write_bytes(b"fake raw bytes")
        it = _item(raw, ftype="raw")

        recs = m.analyze_batch(
            [it], cache=None,
            tags_by_path={str(raw): {"ImageWidth": 640, "ImageHeight": 480}})
        rec = recs[0]

        assert (rec.width, rec.height) == (640, 480)

    def test_no_cache_argument_behaves_exactly_as_before(self, tmp_path, monkeypatch):
        sha_calls = []
        monkeypatch.setattr(m, "sha256_file_with_retry",
                             lambda p, *a, **kw: (sha_calls.append(p), "real-sha")[1])
        img = tmp_path / "a.jpg"
        _make_jpeg(img)

        recs = m.analyze_batch([_item(img)])  # cache=None default

        assert sha_calls == [str(img)]
        assert recs[0].sha256 == "real-sha"

    def test_run_analyze_self_scan_false_does_not_pollute_archive_cache(self, tmp_path):
        """REVIEW-HANDOFF.md, Раунд 52 [ЗАМЕЧАНИЕ]: archive_hash_cache (эта же задача 7) был
        задуман только для self_scan=True (Паспорт архива, cfg.source указывает на уже
        собранный TARGET) -- гейт в run_analyze() проверял только mode=="analyze", не
        self_scan, поэтому обычный документированный CLI-режим `analyze`
        (run_analyze_for_source(), self_scan=False по умолчанию -- read-only предпросмотр
        произвольного SOURCE, TARGET не читается и не пишется, см. README.md) тоже читал и
        писал archive_cache -- с ключами вида "путь SOURCE-файла", которого в архиве никогда
        не было и не будет. archive_cache используется реальной сборкой (index_archive()) как
        источник валидных путей АРХИВА -- чужой ключ там просто мусор в общем на всю машину
        work.db, не потеря данных, но и не то, что документация обещает про analyze."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        _make_jpeg(source / "photo1.jpg")

        cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))
        m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # self_scan=False (default)

        conn = m.connect(cfg.index_db)
        try:
            rows = conn.execute("SELECT path FROM archive_cache").fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_suppress_logs_dry_run_does_not_write_archive_cache_into_target(self, tmp_path):
        """REVIEW-HANDOFF.md, Раунд 54, замечание 1: archive_cache (речь пользователя 2026-08-02,
        задача 2) переехал ВНУТРЬ архива (archive_cache_db_path(cfg.target)) -- до этого писать
        в него во время "Пробного прогона" (suppress_logs=True) было безобидно (файл был в
        WORKDIR, не в TARGET). index_archive() (Фаза 1) вызывается БЕЗУСЛОВНО, включая при
        suppress_logs=True, и открывала это соединение не глядя на suppress_logs -- на уже
        существующем архиве (обычный сценарий "раз в год добавляю фото") это создавало/писало
        настоящий файл ВНУТРИ TARGET, нарушая задокументированную гарантию "suppress_logs
        никогда не пишет в TARGET" (run()'s docstring, _bare_launch_run_dryrun()). Фикс --
        index_archive() открывает archive_cache только когда `not cfg.suppress_logs`."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        # Уже существующий архив -- ровно то состояние TARGET, с которым столкнётся
        # пользователь при повторном визите (не первый прогон программы), см. находку.
        (target / "__служебные_файлы").mkdir(parents=True)
        albums = target / "Albums"
        albums.mkdir()
        _make_jpeg(albums / "existing.jpg")
        _make_jpeg(source / "newphoto.jpg")

        cache_path = target / "__служебные_файлы" / "archive_cache.db"
        assert not cache_path.exists()

        result = m.run_for_source(str(source), str(target), dry_run=True, sample_limit=0,
                                   log=lambda *a, **k: None, suppress_logs=True, shared_pool=None)

        assert not result.failed
        assert not cache_path.exists()  # ключевая проверка -- TARGET остаётся нетронутым

    def test_dry_run_does_not_create_any_directory_on_fresh_target(self, tmp_path):
        """Живая находка пользователя (2026-08-09): "после dry-run осталась структура архива
        (не удалена)" -- [2] Пробный прогон (suppress_logs=True) реально создавал
        Albums/ByDate/RAW/_Unsorted-ветки на диске (пришлось чистить руками перед реальной
        сборкой), хотя ни один файл не копировался. Причина: resolve_dest_path() (проверка
        занятости имени, вызывается ДО решения "дубль или нет", независимо от dry_run) звала
        _makedirs_iterative(dest_dir) безусловно -- реальное копирование (place_file()) и так
        уже гейтится cfg.dry_run, но этот makedirs стоял РАНЬШЕ той проверки. TARGET здесь --
        свежая, ещё не существующая на диске папка (не просто "пустая", а полностью
        отсутствующая) -- если после прогона на диске появилась ХОТЬ ОДНА подпапка, дефект
        воспроизведён."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        _make_jpeg(source / "undated.jpg")  # без EXIF-даты -- маршрут в ByDate, тот же путь,
                                             # что и в живой находке пользователя
        target = tmp_path / "MyArchive"
        assert not target.exists()

        result = m.run_for_source(str(source), str(target), dry_run=True, sample_limit=0,
                                   log=lambda *a, **k: None, suppress_logs=True, shared_pool=None)

        assert not result.failed
        assert not target.exists()  # TARGET целиком отсутствует -- ничего не создано вовсе


class TestExifCacheReuse:
    """Речь пользователя, 2026-08-02 ("почему Фаза 1 быстрая, а паспорт медленный -- разве не
    один алгоритм?"): archive_cache теперь хранит и EXIF-производные поля (дата/источник
    даты/камера/GPS), не только sha256/pHash -- раньше exiftool звался БЕЗУСЛОВНО на каждый
    файл в run_analyze(), даже при полном попадании по хешу, потому что этих полей в кэше не
    было вовсе. cached[8] -- exif_cached (1/0/None), cached[9:14] -- exif_dt (ISO-строка)/
    exif_dt_source/camera/gps_lat/gps_lon (см. SCHEMA)."""

    def test_exif_cache_hit_populates_fields_without_tags(self, tmp_path):
        """tags_by_path пуст для этого файла (как будто exiftool вообще не звался, см.
        _tag_prefetch_pairs()/_exif_cache_ready()) -- analyze_batch() обязан взять
        дату/камеру/GPS из archive_cache, не оставить их None."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (
            it.size, it.mtime, "cached-sha", "cached-phash", None, 800, 600, None,
            1, "2022-04-04T10:00:00", "DateTimeOriginal", "Canon EOS 80D", 55.75, 37.62,
        )}

        recs = m.analyze_batch([it], cache=cache, tags_by_path={})  # exiftool skipped upstream
        rec = recs[0]

        assert rec.exif_dt == m.datetime(2022, 4, 4, 10, 0, 0)
        assert rec.exif_dt_source == "DateTimeOriginal"
        assert rec.camera == "Canon EOS 80D"
        assert (rec.gps_lat, rec.gps_lon) == (55.75, 37.62)

    def test_old_short_cache_row_without_exif_columns_falls_back_to_tags(self, tmp_path):
        """Обратная совместимость: archive_cache.db, смигрировавший ДО этой правки, имеет
        строки без exif-колонок вовсе -- 8-элементный кортеж (индексы 0-7), как раньше.
        len(cached) > 8 должно быть False -- никакого IndexError, честный откат на tags."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (it.size, it.mtime, "cached-sha", "cached-phash", None, 800, 600, None)}

        recs = m.analyze_batch(
            [it], cache=cache,
            tags_by_path={str(img): {"DateTimeOriginal": "2022:04:04 10:00:00",
                                      "Make": "Canon", "Model": "EOS 80D"}})
        rec = recs[0]

        assert rec.exif_dt == m.datetime(2022, 4, 4, 10, 0, 0)
        assert rec.camera == "Canon EOS 80D"

    def test_exif_cache_hit_but_no_exif_at_all_stays_none_not_missing(self, tmp_path):
        """Кэшированный ответ "у этого файла нет EXIF" (все exif-поля NULL, exif_cached=1) --
        легитимный кэш-хит, не то же самое, что "не проверяли". rec.exif_dt должен остаться
        None (а не упасть/переключиться обратно на попытку вызвать exiftool)."""
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        it = _item(img)
        cache = {str(img): (
            it.size, it.mtime, "cached-sha", "cached-phash", None, 800, 600, None,
            1, None, None, None, None, None,
        )}

        recs = m.analyze_batch([it], cache=cache, tags_by_path={})
        rec = recs[0]

        assert rec.exif_dt is None
        assert rec.camera is None

    def test_run_analyze_second_pass_never_calls_exiftool_when_fully_cached(self, tmp_path):
        """Интеграционный тест сквозь _walk_with_exif_prefetch()/_exif_cache_ready(): первый
        паспорт на свежесобранном архиве закономерно зовёт exiftool (кэш ещё пуст на exif),
        второй -- на НЕИЗМЕНИВШЕМСЯ архиве -- не должен звать exiftool вообще, ни разу.
        Раньше (до этой правки) второй прогон всё равно звал exiftool безусловно на каждый
        файл -- ровно находка пользователя ("почему паспорт всегда медленный")."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        _make_jpeg(source / "photo1.jpg", color=(10, 20, 30))
        _make_jpeg(source / "photo2.jpg", color=(200, 40, 60))

        cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                        workdir=str(workdir))
        m.ensure_target_layout(cfg)
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        call_sizes = []
        real_exiftool_batch = m.exiftool_batch

        def _spy(paths, **kw):
            call_sizes.append(len(paths))
            return real_exiftool_batch(paths, **kw)

        m.exiftool_batch = _spy
        try:
            cfg2 = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                             workdir=str(workdir))
            stats = m.run_analyze(cfg2, "analyze", log=lambda *a, **k: None, self_scan=True)
        finally:
            m.exiftool_batch = real_exiftool_batch

        assert stats.total_files == 2
        assert not call_sizes, f"expected exiftool_batch() never called on the warm pass, got {call_sizes}"


class TestVideoCacheReuse:
    """Речь пользователя, 2026-08-03 ("сделать ffmpeg?" -> кэш video-полей по аналогии с EXIF
    выше): в отличие от exif_dt/camera/gps (которых в archive_cache не было ДО задачи 2026-08-02
    выше), duration/phash/width/height/bitrate для video были частью SCHEMA archive_cache и
    писались _seed_archive_cache()/analyze_batch()'s cache_hit-ветками с самого начала этого
    кэша (задолго до EXIF-расширения) -- video_duration_and_resolution()/video_phash_3frames()
    уже не звались вовсе на попадании в кэш, никакого отдельного фикса не требовалось. Тесты
    ниже закрывают именно ЭТУ пустоту в покрытии (существовавшую и до, и после EXIF-задачи) --
    не регрессия, найденная сейчас, а нулевое явное подтверждение уже работающего поведения."""

    def test_video_cache_hit_never_calls_ffmpeg(self, tmp_path, monkeypatch):
        vid = tmp_path / "a.mp4"
        vid.write_bytes(b"not a real video -- cache hit must never touch it")
        it = _item(vid, ftype="video")

        def _boom(*a, **kw):
            raise AssertionError("video_duration_and_resolution() should not be called on a cache hit")

        def _boom2(*a, **kw):
            raise AssertionError("video_phash_3frames() should not be called on a cache hit")

        monkeypatch.setattr(m, "video_duration_and_resolution", _boom)
        monkeypatch.setattr(m, "video_phash_3frames", _boom2)

        cache = {str(vid): (
            it.size, it.mtime, "cached-sha", "aaaa|bbbb|cccc", 12.5, 1920, 1080, 4000,
        )}
        recs = m.analyze_batch([it], cache=cache, tags_by_path={})
        rec = recs[0]

        assert rec.duration == 12.5
        assert (rec.width, rec.height, rec.bitrate) == (1920, 1080, 4000)
        assert rec.phash == "aaaa|bbbb|cccc"
        assert rec.is_media is True

    def test_run_analyze_reuses_video_fields_seeded_by_the_build_itself(self, tmp_path, monkeypatch):
        """Сборка (_run_impl()) сама сеет archive_cache через _seed_archive_cache() сразу после
        place_file() -- Паспорт на том же архиве СРАЗУ (без второго прохода) не должен звать
        video_duration_and_resolution()/video_phash_3frames() вовсе, в отличие от EXIF (где
        именно ЭТО раньше требовало отдельного тёплого прохода, см. тест выше) -- разница
        подтверждает, что video-кэш никогда не имел этого конкретного пробела."""
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        (source / "video1.mp4").write_bytes(b"fake video bytes")

        duration_calls, phash_calls = [], []
        real_duration = m.video_duration_and_resolution
        real_phash = m.video_phash_3frames

        def _spy_duration(p):
            duration_calls.append(p)
            return (5.0, 1280, 720, 2000)

        def _spy_phash(p, d):
            phash_calls.append(p)
            return ["a" * 16, "b" * 16, "c" * 16]

        m.video_duration_and_resolution = _spy_duration
        m.video_phash_3frames = _spy_phash
        try:
            cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                            workdir=str(workdir))
            m.ensure_target_layout(cfg)
            m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)
            assert len(duration_calls) == 1  # build itself computed it once, as expected

            cfg2 = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                             workdir=str(workdir))
            duration_calls.clear()
            phash_calls.clear()
            stats = m.run_analyze(cfg2, "analyze", log=lambda *a, **k: None, self_scan=True)
        finally:
            m.video_duration_and_resolution = real_duration
            m.video_phash_3frames = real_phash

        assert stats.total_files == 1
        assert not duration_calls, f"expected no ffprobe call at all, got {duration_calls}"
        assert not phash_calls, f"expected no ffmpeg call at all, got {phash_calls}"


class TestAnalyzeExifPrefetchRespectsSampleLimit:
    """REVIEW-HANDOFF.md, Раунд 54, замечание 2 + Раунд 55, придирка: _walk_with_exif_prefetch()
    (батчинг exiftool, 2026-08-02) копил батч ДО _ANALYZE_EXIF_PREFETCH_BATCH_SIZE=200 файлов
    ПЕРЕД тем, как отдать первый элемент вызывающему циклу run_analyze() -- проверка
    cfg.sample_limit стоит СНАРУЖИ генератора и физически не могла сработать раньше.
    "--sample-limit N" (дешёвый тест на малой выборке, в т.ч. на медленном сетевом источнике)
    реально тратил exiftool на до 200 файлов вместо N, молча. Первый фикс (батч <= sample_limit)
    оставлял остаточный ×2 (обычный `for`+`break` вызывает next() на один item больше лимита,
    генератору приходилось набирать ещё один полный батч ради него) -- итоговый фикс:
    itertools.islice() снаружи, не ручной break -- ровно N item, ровно один батч."""

    def test_exiftool_batch_size_bounded_by_sample_limit(self, tmp_path, monkeypatch):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        for i in range(10):
            _make_jpeg(source / f"photo{i}.jpg", color=(i * 20, 40, 200))

        call_sizes = []
        real_exiftool_batch = m.exiftool_batch

        def _spy(paths, **kw):
            call_sizes.append(len(paths))
            return real_exiftool_batch(paths, **kw)

        monkeypatch.setattr(m, "exiftool_batch", _spy)  # overrides the autouse stub above

        cfg = m.Config(source=str(source), target=str(target), sample_limit=3, workdir=str(workdir))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.total_files == 3  # --sample-limit's own documented contract, unaffected
        # До первого фикса: один вызов на весь прогретый батч (10, весь SOURCE -- меньше чем
        # _ANALYZE_EXIF_PREFETCH_BATCH_SIZE=200 в этом тесте, но принцип тот же: батч не
        # ограничивался sample_limit вообще). После первого, но до итогового фикса: [3, 3] --
        # остаточный ×2 (см. докстринг класса). После итогового фикса -- ровно один батч ровно
        # нужного размера, ни одного лишнего файла/спавна.
        assert call_sizes == [cfg.sample_limit], call_sizes


class TestExifPrefetchRateHint:
    """2026-08-06, боевой прогон пользователя ("статус-строка надолго замирает, работает
    очень медленно"): пока идёт сбор батча + сам вызов exiftool_batch() на весь батч сразу --
    ни один update() не происходит. Изначальный фикс (transient_op_cb, текстовая строка
    "чтение метаданных, файлов: N…") убран 2026-08-07 по прямой просьбе пользователя --
    видимость активности теперь решает общее "занято" время в статус-строке
    (ProgressReporter._build_two_line_status(), см. tests/test_progress_phase2.py), не
    привязанное к конкретной операции.

    Следующая находка того же дня ("скорость всегда 0"): без учёта времени батча в EMA
    (в отличие от распаковки, это время И ЕСТЬ цена N файлов) -- rate_hint_cb получает
    (секунд_на_файл, N), только для батчей больше 1 файла."""

    def _items(self, tmp_path, n):
        paths = []
        for i in range(n):
            p = tmp_path / f"a{i}.jpg"
            _make_jpeg(p)
            paths.append(p)
        return [_item(p) for p in paths]

    def test_walk_with_exif_prefetch_yields_all_items(self, tmp_path):
        # Без каких-либо callback'ов -- обычный вызывающий код (analyze без two_line-бара)
        # не должен падать.
        items = self._items(tmp_path, 3)
        result = list(m._walk_with_exif_prefetch(
            iter(items), str(tmp_path / "_extract"), batch_size=2))
        assert len(result) == 3

    def test_rate_hint_called_only_for_multi_file_batches(self, tmp_path):
        items = self._items(tmp_path, 3)
        calls = []
        list(m._walk_with_exif_prefetch(
            iter(items), str(tmp_path / "_extract"), batch_size=2,
            rate_hint_cb=lambda per_item, n: calls.append(n)))
        # batch_size=2 -> первый батч из 2 файлов вызывает хинт (с n=2), хвостовой батч из
        # 1 файла -- нет (одиночный файл меряется честно как есть).
        assert calls == [2]

    def test_rate_hint_reflects_real_elapsed_time(self, tmp_path, monkeypatch):
        items = self._items(tmp_path, 2)
        real_exiftool_batch = m.exiftool_batch

        def _slow_exiftool_batch(paths, **kw):
            fake_clock["now"] += 4.0  # имитирует 4 секунды на весь батч из 2 файлов
            return real_exiftool_batch(paths, **kw)

        fake_clock = {"now": 1000.0}
        monkeypatch.setattr(m, "exiftool_batch", _slow_exiftool_batch)
        monkeypatch.setattr(m.time, "time", lambda: fake_clock["now"])

        calls = []
        list(m._walk_with_exif_prefetch(
            iter(items), str(tmp_path / "_extract"), batch_size=2,
            rate_hint_cb=lambda per_item, n: calls.append((per_item, n))))
        assert calls == [(2.0, 2)]  # 4.0s / 2 файла = 2.0s/файл

    def test_none_rate_hint_callback_does_not_crash(self, tmp_path):
        items = self._items(tmp_path, 3)
        result = list(m._walk_with_exif_prefetch(
            iter(items), str(tmp_path / "_extract"), batch_size=2, rate_hint_cb=None))
        assert len(result) == 3


class TestAnalyzeShowsObjectEta:
    """Речь пользователя, 2026-08-02 ("подумай, как сделать информативной интерактив при
    построении паспорта"): run_analyze() (значит и "Паспорт архива", и CLI analyze/
    analyze-full) раньше показывал только голый растущий счётчик и скорость -- ту же ETA-
    machinery, что уже была у Фазы 2 реальной сборки (_quick_media_count_estimate() +
    object_progress_cb + ProgressReporter(two_line=True)), run_analyze() не использовал вовсе.
    Проверяем, что она реально подключена -- не только что код не падает."""

    def test_main_bar_gets_two_line_and_total_estimate(self, tmp_path, monkeypatch):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        for i in range(5):
            _make_jpeg(source / f"photo{i}.jpg", color=(i * 20, 40, 200))

        constructed = []
        real_pr = m.ProgressReporter

        class _SpyProgressReporter(real_pr):
            def __init__(self, *a, **kw):
                constructed.append(kw)
                super().__init__(*a, **kw)

        monkeypatch.setattr(m, "ProgressReporter", _SpyProgressReporter)

        cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))
        m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        # Два ProgressReporter за прогон: предпересчёт ("Оцениваю объём работы", total_estimate
        # ещё не при делах) и основной бар (must be two_line=True с реальным total_estimate).
        assert len(constructed) == 2, constructed
        estimate_kw, main_kw = constructed
        assert estimate_kw.get("desc") == "Оцениваю объём работы"
        assert main_kw.get("two_line") is True
        assert main_kw.get("total_estimate") == 5  # 5 файлов реально лежат в source

    def test_source_walker_gets_object_progress_and_line_callbacks(self, tmp_path, monkeypatch):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        _make_jpeg(source / "a.jpg")

        seen_kwargs = {}
        real_walker = m.SourceWalker

        class _SpySourceWalker(real_walker):
            def __init__(self, *a, **kw):
                seen_kwargs.update(kw)
                super().__init__(*a, **kw)

        monkeypatch.setattr(m, "SourceWalker", _SpySourceWalker)

        cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))
        m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        # До этой правки run_analyze() передавал только progress_cb -- object_line_cb/
        # transient_op_cb/object_progress_cb не были подключены вовсе (архив внутри SOURCE не
        # получал ни живой "распаковываю..." строки, ни счётчика "объектов X/Y").
        assert seen_kwargs.get("object_line_cb") is not None
        assert seen_kwargs.get("transient_op_cb") is not None
        assert seen_kwargs.get("object_progress_cb") is not None


class TestAlbumDateGroupingStats:
    """SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата в analyze-отчёте):
    n_albums_detected -- фикс точности (album_prefix, не голое имя альбома), плюс новые
    n_media_in_albums/n_media_by_date/bydate_media_by_folder."""

    def _cfg(self, tmp_path):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        return source, m.Config(source=str(source), target=str(target), sample_limit=0,
                                 workdir=str(workdir))

    def test_n_albums_detected_counts_every_folder_in_the_tree_separately(self, tmp_path):
        """2026-08-08 (альбомный редизайн, "альбом -- это каждая папка в дереве"): "Мои
        фото/Свадьба" и "Мои фото/Отпуск" -- общий родитель "Мои фото" считается один раз,
        а сами подпапки -- отдельно, итого 3 разных альбома для 2 файлов, не 1 (общий
        контейнер) и не 2 (только подпапки без родителя)."""
        source, cfg = self._cfg(tmp_path)
        (source / "Мои фото" / "Свадьба").mkdir(parents=True)
        (source / "Мои фото" / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "Мои фото" / "Свадьба" / "a.jpg")
        _make_jpeg(source / "Мои фото" / "Отпуск" / "b.jpg")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_albums_detected == 3

    def test_dump_ancestor_before_real_name_counts_as_no_album_at_all(self, tmp_path):
        """2026-08-08: dump-родитель (DCIM) перед реальным именем теперь отравляет весь путь
        целиком -- ни один уровень не считается альбомом, всё падает в ByDate."""
        source, cfg = self._cfg(tmp_path)
        (source / "DCIM" / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "DCIM" / "Отпуск" / "a.jpg")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_albums_detected == 0

    def test_n_media_in_albums_and_n_media_by_date_split_correctly(self, tmp_path):
        """YY (n_media_in_albums)/QQ (n_media_by_date) -- фильтр по item.ftype media, тот же
        цикл, что и n_albums_detected."""
        source, cfg = self._cfg(tmp_path)
        (source / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "Отпуск" / "a.jpg")
        _make_jpeg(source / "Отпуск" / "b.jpg")
        _make_jpeg(source / "c.jpg")  # без альбома -- файл прямо в корне SOURCE

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_media_in_albums == 2
        assert stats.n_media_by_date == 1
        assert len(stats.bydate_media_by_folder) == 1

    def test_bydate_media_by_folder_counts_distinct_folders_not_files(self, tmp_path):
        """ZZ (число обычных папок) -- len() счётчика по РАЗНЫМ папкам, не суммарный файл-
        счёт (это QQ, отдельно). DCIM/Camera -- известные технические имена (dump), файлы
        прямо внутри них (без вложенного альбома глубже) не находят альбом вовсе."""
        source, cfg = self._cfg(tmp_path)
        (source / "DCIM").mkdir()
        (source / "Camera").mkdir()
        _make_jpeg(source / "DCIM" / "a.jpg")
        _make_jpeg(source / "DCIM" / "b.jpg")
        _make_jpeg(source / "Camera" / "c.jpg")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert len(stats.bydate_media_by_folder) == 2
        assert stats.n_media_by_date == 3

    def test_self_scan_recognizes_real_album_content_not_as_dump_item(self, tmp_path, monkeypatch):
        """Живая находка (2026-08-08, ci/windows_ci_test.py::test_passport_report_on_real_archive
        на реальных bin/): "Albums" -- защищённое dump-имя (самозащита от каскадного
        самопоедания) -- на self_scan (Паспорт архива/analyze на уже собранном архиве)
        item.rel_path буквально начинается с "Albums/", и под безусловным отравлением (после
        альбомного редизайна) это топило ЛЮБОЙ правильно разложенный файл архива -- находился
        не альбом, а "файл добавлен мимо программы" (n_dump_items). Регрессия не поймана
        существующими self_scan-тестами (ни один не строил реальный альбом и не self-scan'ил
        его), поймана только сквозным CI-прогоном на реальных bin/."""
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        source = tmp_path / "source"
        target = tmp_path / "target"
        workdir = tmp_path / "workdir"
        source.mkdir()
        target.mkdir()
        workdir.mkdir()
        (source / "Отпуск").mkdir()
        _make_jpeg(source / "Отпуск" / "a.jpg")

        cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))
        m.ensure_target_layout(cfg)
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)
        assert (target / "Albums" / "Отпуск" / "a.jpg").exists()  # precondition

        cfg2 = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                         workdir=str(workdir))
        stats = m.run_analyze(cfg2, "analyze", log=lambda *a, **k: None, self_scan=True)

        assert stats.n_dump_items == 0
        assert stats.n_media_in_albums == 1


class TestDisputedAndUnreadablePaths:
    """SESSION-HANDOFF.txt, задачи 4/6 (2026-08-09, боевой прогон): analyze-уровень раньше
    сливал "содержимое не распознано" (disputed) и "физически не удалось прочитать"
    (unreadable) в один общий n_broken_or_zero, без единого пути к файлу. Теперь -- два
    раздельных списка реальных абсолютных путей."""
    def _cfg(self, tmp_path):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        return source, m.Config(source=str(source), target=str(target), sample_limit=0,
                                 workdir=str(workdir))

    def _expected_abs_path(self, source, *parts):
        """REVIEW-HANDOFF.md, Раунд 80 [ЗАМЕЧАНИЕ]: _analyze_source_abs_path() всегда склеивает
        "\\" (программа -- только для Windows, см. её докстринг/CLAUDE.md), НЕЗАВИСИМО от
        разделителя, который на POSIX-раннере дал бы str(source / "a" / "b") (pathlib берёт
        posixpath, "/"). Прямое сравнение с str(pathlib.Path(...)) поэтому platform-зависимо и
        падает на не-Windows CI -- собираем ожидаемое значение той же ручной склейкой, что и
        сама функция, не через pathlib."""
        return str(source).rstrip("\\/") + "\\" + "\\".join(parts)

    def test_zero_byte_file_recorded_as_disputed_not_unreadable(self, tmp_path):
        """Пустой файл -- содержимое прочитано (0 байт), просто не медиа -- "не удалось
        распознать" (disputed_paths), не "не прочитано" (unreadable_paths). Путь -- реальный
        абсолютный (SOURCE + относительный origin_display, см. _analyze_source_abs_path())."""
        source, cfg = self._cfg(tmp_path)
        (source / "Album").mkdir()
        (source / "Album" / "broken.jpg").write_bytes(b"")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.disputed_paths == [self._expected_abs_path(source, "Album", "broken.jpg")]
        assert stats.unreadable_paths == []

    def test_corrupt_content_recorded_as_disputed_not_unreadable(self, tmp_path):
        """Ненулевой файл, который физически ЧИТАЕТСЯ, но не РАСПОЗНАЁТСЯ как изображение
        (rec.broken, не rec.read_error) -- та же категория "не удалось распознать", что и
        пустой файл выше, не "не прочитано"."""
        source, cfg = self._cfg(tmp_path)
        (source / "garbage.jpg").write_bytes(b"not a real jpeg" * 10)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.disputed_paths == [self._expected_abs_path(source, "garbage.jpg")]
        assert stats.unreadable_paths == []

    def test_read_error_recorded_as_unreadable_not_disputed(self, tmp_path, monkeypatch):
        """rec.read_error -- физический I/O-сбой при чтении, отдельная категория от
        disputed выше ("не прочитано", не "не удалось распознать"). Нужен mode="analyze" (не
        "analyze-quick") -- rec.read_error проверяется только внутри `if not skip_hash`, см.
        SESSION-HANDOFF.txt (шестая находка)."""
        source, cfg = self._cfg(tmp_path)
        _make_jpeg(source / "a.jpg")

        def _raise(*a, **kw):
            raise m.ReadError("locked by another process")
        monkeypatch.setattr(m, "sha256_file_with_retry", _raise)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.unreadable_paths == [self._expected_abs_path(source, "a.jpg")]
        assert stats.disputed_paths == []

    def test_disputed_and_unreadable_paths_use_backslash_separators(self, tmp_path):
        """_analyze_source_abs_path() нормализует item.origin_display ("/" -- POSIX-style) в
        "\\\\" при склейке с cfg.source -- иначе _win_dirname()/_win_basename() (рендер отчёта)
        расщепляли бы смешанный путь неверно (см. докстринг функции). Проверяем именно СУФФИКС
        (часть, произведённую из origin_display), не весь путь целиком -- cfg.source сам по
        себе на POSIX-раннере (см. _expected_abs_path()) законно содержит "/" (tmp_path -- это
        реальный путь текущей ОС, не собственно то, что тестирует эта функция)."""
        source, cfg = self._cfg(tmp_path)
        (source / "Sub").mkdir()
        (source / "Sub" / "broken.jpg").write_bytes(b"")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.disputed_paths == [self._expected_abs_path(source, "Sub", "broken.jpg")]
        assert stats.disputed_paths[0].endswith("Sub\\broken.jpg")
        assert "Sub/broken.jpg" not in stats.disputed_paths[0]

    def test_video_ts_disputed_path_not_doubled_with_source(self):
        """Живая находка (report.html, боевой прогон analyze, 2026-08-09): VIDEO_TS/DVD-юнит
        ВНЕ архива строит item.origin_display из disp_base=cur_dirpath (см.
        SourceWalker._handle_dvd_unit()) -- уже АБСОЛЮТНЫЙ путь, в отличие от обычных файлов
        (SOURCE-относительный, единственный случай, который _analyze_source_abs_path()
        предполагала раньше). Слепое приклеивание cfg.source поверх уже-абсолютного пути давало
        "C:\\C:\\Users\\..." в реальном отчёте -- нерабочая file://-ссылка.

        Юнит-тест ПРЯМО на _analyze_source_abs_path(), не через полный run_analyze(): сам баг
        воспроизводится только когда origin_display уже выглядит как Windows-абсолютный путь
        (диск/UNC, см. _is_windows_abs_path()) -- у tmp_path-фикстуры на POSIX-раннере
        (публичный репозиторий гоняет tests/ на ubuntu-latest в CI) не бывает вида "C:\\...",
        сквозной прогон через реальный SourceWalker не воспроизвёл бы задвоение на этой
        платформе вовсе -- не то же самое, что "фикс не нужен на POSIX" (программа только для
        Windows, см. CLAUDE.md/докстринг _is_windows_abs_path())."""
        cfg = SimpleNamespace(source="C:\\Users\\test\\NewBatch")
        item = SimpleNamespace(origin_display="C:\\Users\\test\\Desktop\\VIDEO_TS/VIDEO_TS.BUP")

        result = m._analyze_source_abs_path(cfg, item)

        assert result == "C:\\Users\\test\\Desktop\\VIDEO_TS\\VIDEO_TS.BUP"
        assert result.count("C:") == 1

    def test_video_ts_files_not_flagged_broken_in_analyze(self, tmp_path, monkeypatch):
        """Живая находка (отчёт пользователя, боевой прогон, 2026-08-09): `analyze` показывал
        VIDEO_TS.BUP/.IFO как «6 файлов не удалось распознать» на КАЖДОМ отсканированном
        DVD-рипе -- SourceWalker._handle_dvd_unit() ставит ftype="video" безусловно для ВСЕХ
        файлов юнита (включая .IFO/.BUP -- служебные файлы навигации/бэкапа DVD-структуры, не
        сами по себе проигрываемое видео), а run_analyze() гоняет каждый item с ftype="video"
        через analyze_batch()'s video_duration_and_resolution() (ffprobe) -- гарантированно
        проваливается на .IFO/.BUP, это не видеопоток. Реальная сборка не задета (dvd_dest_path
        items не доходят до analyze_batch() там, см. её докстринг) -- баг был специфичен для
        analyze-режима, не по данным пользователя ("не медиа файлы" -- фактически верное
        наблюдение, .IFO/.BUP действительно не медиаконтент сам по себе).

        video_duration_and_resolution() монкипатчена на явный отказ -- тест не зависит от
        реального ffprobe/валидности содержимого файла, проверяет только, что DVD-юнит-item
        вообще не доходит до этого вызова (см. item.dvd_dest_path is not None: continue)."""
        source, cfg = self._cfg(tmp_path)
        video_ts = source / "VIDEO_TS"
        video_ts.mkdir()
        (video_ts / "VTS_01_0.VOB").write_bytes(b"x" * 100)
        (video_ts / "VIDEO_TS.IFO").write_bytes(b"not a real video stream" * 5)
        (video_ts / "VIDEO_TS.BUP").write_bytes(b"not a real video stream" * 5)
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda path: (_ for _ in ()).throw(
                                 AssertionError(f"should never be called for a DVD-unit item: {path}")))

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.disputed_paths == []
        assert stats.unreadable_paths == []
        assert stats.n_broken_or_zero == 0
        assert stats.total_files == 3
        assert stats.n_videos == 3

    def test_video_ts_files_still_counted_in_years_structure_and_tiers(self, tmp_path):
        """Живая находка пользователя, 2026-08-09 ("2013 не попал в структуру"): первая версия
        фикса выше (безусловный `continue` для DVD-item) заодно вышибала DVD-содержимое из
        ВСЕГО блока классификации -- dates_by_year ("Медиафайлы по годам"), tree_folder_counts
        ("Структура архива"), n_dump_items/n_media_by_date -- не только broken-проверку.
        total_files/n_videos (см. соседний тест выше) продолжали считать эти файлы -- разные
        карточки одного отчёта показывали бы разные числа. Дата файлов выставлена явно
        (os.utime) на 2013 -- тот же год, что в живой находке.

        Разброс по тирам (некоторые файлы попадают в C "оценочно по соседним", не D) --
        штатное поведение resolve_date() для группы файлов без EXIF с одинаковым mtime в одной
        папке (та же неопределённость была бы и для любых НЕ-DVD файлов в такой ситуации) --
        не проверяется здесь точным числом, важно только что данные ЕСТЬ (не потеряны целиком,
        как было до фикса) и согласованы между n_dump_items/n_media_by_date/tree_folder_counts."""
        source, cfg = self._cfg(tmp_path)
        video_ts = source / "VIDEO_TS"
        video_ts.mkdir()
        files = [video_ts / "VTS_01_0.VOB", video_ts / "VIDEO_TS.IFO", video_ts / "VIDEO_TS.BUP"]
        for f in files:
            f.write_bytes(b"x" * 100)
        old_ts = datetime(2013, 9, 15, 12, 0, 0).timestamp()
        for f in files:
            os.utime(f, (old_ts, old_ts))

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.disputed_paths == []  # соседний тест выше -- не регресс Задачи D
        assert stats.dates_by_year[2013] >= 1  # раньше было 0 -- 2013 пропадал целиком
        assert stats.n_dump_items == 3  # не в альбоме -- VIDEO_TS не имя альбома, всегда ByDate
        assert stats.n_media_by_date == 3
        assert any(k.startswith("ByDate/2013") for k in stats.tree_folder_counts)
        # Согласованность между карточками отчёта -- ровно то, о чём спросил пользователь:
        # сумма по структуре не должна расходиться с общим числом файлов юнита.
        assert sum(stats.tree_folder_counts.values()) == stats.total_files == 3


class TestDateTierBydateCounters:
    """Задача 5 (SESSION-HANDOFF.txt, 2026-08-09): n_tier_b_bydate/n_tier_c_bydate/
    n_tier_d_bydate -- album-исключающие счётчики для объединённого чек-листа report.html,
    та же семантика "тир X и не в альбоме", что n_tier_cd_bydate, просто тоньше на тир."""
    def _cfg(self, tmp_path):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        return source, m.Config(source=str(source), target=str(target), sample_limit=0,
                                 workdir=str(workdir))

    def test_tier_b_counts_only_outside_albums(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        monkeypatch.setattr(m, "resolve_date",
                             lambda *a, **kw: (None, "B", "medium", "filename_pattern", None))
        source, cfg = self._cfg(tmp_path)
        (source / "Album").mkdir()
        _make_jpeg(source / "Album" / "in_album.jpg")
        _make_jpeg(source / "loose.jpg")

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_tier_b_bydate == 1  # только loose.jpg -- in_album.jpg исключён
        assert stats.tier_counts["B"] == 2  # сырой tier_counts по-прежнему считает ОБА файла

    def test_tier_c_and_d_also_exclude_album_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        source, cfg = self._cfg(tmp_path)
        (source / "Album").mkdir()
        _make_jpeg(source / "Album" / "c_in_album.jpg")
        _make_jpeg(source / "c_loose.jpg")
        _make_jpeg(source / "Album" / "d_in_album.jpg")
        _make_jpeg(source / "d_loose.jpg")

        calls = {}

        def _fake_resolve_date(ctx, rel_path, *a, **kw):
            tier = "C" if "c_" in rel_path else "D"
            calls[rel_path] = tier
            return (None, tier, "low", "no_signal", None)
        monkeypatch.setattr(m, "resolve_date", _fake_resolve_date)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

        assert stats.n_tier_c_bydate == 1  # только c_loose.jpg
        assert stats.n_tier_d_bydate == 1  # только d_loose.jpg
        assert stats.n_tier_cd_bydate == 2  # C+D вместе, не в альбоме -- уже существующий счётчик


class TestCheckSignatureFlag:
    """Задача 11 (SESSION-HANDOFF.txt, 2026-08-09): sniff_signature() -- заметные накладные
    расходы на медленном/сетевом диске (отдельный open() на КАЖДЫЙ файл). Новый флаг
    cfg.check_signature (по умолчанию False) отключает проверку в обычном анализе, self-scan
    ("Паспорт архива") проверяет ВСЕГДА, независимо от флага."""
    def _cfg(self, tmp_path, **overrides):
        source = tmp_path / "NewBatch"
        source.mkdir()
        target = tmp_path / "MyArchive"
        target.mkdir()
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        return source, m.Config(source=str(source), target=str(target), sample_limit=0,
                                 workdir=str(workdir), **overrides)

    def _make_signature_mismatch_file(self, source):
        # ZIP magic bytes (PK\x03\x04) под именем .jpg -- sniff_signature() вернёт "archive",
        # _coarse_kind("image")=="image" -- несоответствие. Не обязано быть валидным
        # изображением дальше по циклу (sniff_signature() -- самая первая проверка, до
        # analyze_batch()/декодирования, см. run_analyze()).
        (source / "fake.jpg").write_bytes(b"PK\x03\x04" + b"\x00" * 60)

    def test_disabled_by_default_no_check_in_normal_analyze(self, tmp_path):
        source, cfg = self._cfg(tmp_path)
        self._make_signature_mismatch_file(source)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # self_scan=False

        assert cfg.check_signature is False
        assert stats.n_signature_mismatch == 0

    def test_enabled_via_flag_checks_in_normal_analyze(self, tmp_path):
        source, cfg = self._cfg(tmp_path, check_signature=True)
        self._make_signature_mismatch_file(source)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)  # self_scan=False

        assert stats.n_signature_mismatch == 1

    def test_self_scan_always_checks_regardless_of_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        source, cfg = self._cfg(tmp_path)  # check_signature=False (по умолчанию)
        self._make_signature_mismatch_file(source)

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=True)

        assert cfg.check_signature is False
        assert stats.n_signature_mismatch == 1  # self_scan игнорирует флаг, проверяет всегда
