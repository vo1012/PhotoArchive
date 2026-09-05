"""Персистентный кэш метаданных ИСТОЧНИКА (2026-09-04, прямая просьба пользователя:
"10-часовая сборка прервана на 9:50, повторный запуск снова гонит весь анализ").

Зеркалит принцип archive_cache (кэш стороны АРХИВА) для SOURCE-файлов: при возобновлении
оборванного прогона уже разобранные файлы источника не перехешируются/не перечитываются
exiftool'ом. См. photosort_win.py: SCHEMA source_meta_cache, _seed_source_meta_cache(),
_load_source_meta_cache(), _prune_source_meta_cache(), _process_record()'s сев,
_run_impl()'s загрузка + _walk_with_exif_prefetch(cache=).

red-before-green: до правки _run_impl() звал _walk_with_exif_prefetch(cache=None) и
source_meta_cache-таблицы не существовало -- второй прогон пересчитывал всё.
"""
import os
import time
import zipfile

import pytest
from PIL import Image

import photosort_win as m


def _run(cfg):
    return m.run(cfg, log=lambda *a, **k: None)


def test_source_meta_cache_is_a_recognized_yaml_key(tmp_path):
    """load_yaml_config() не должен ругаться на ключ как на незнакомый и обязан пробросить
    его в Config."""
    assert "source_meta_cache" in m.CONFIG_YAML_FIELDS
    assert m.Config(source=str(tmp_path / "s"), target=str(tmp_path / "t")).source_meta_cache is True
    p = tmp_path / "cfg.yaml"
    p.write_text("source_meta_cache: false\n", encoding="utf-8")
    warnings = []
    overrides = m.load_yaml_config(str(p), log=lambda msg: warnings.append(msg))
    assert overrides["source_meta_cache"] is False
    assert not any("source_meta_cache" in w for w in warnings)


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")


def _cfg(tmp_path, source, **kw):
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    return m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir), **kw)


def _cache_rows(target):
    conn = m._open_archive_cache_conn(str(target))
    assert conn is not None
    try:
        return list(conn.execute("SELECT read_path, root, size, mtime, seeded_at, sha256, phash "
                                  "FROM source_meta_cache"))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Round-trip: второй прогон не пересчитывает неизменные файлы
# --------------------------------------------------------------------------- #

def test_second_run_reuses_cache_and_skips_rehash(tmp_path, monkeypatch):
    source = tmp_path / "source" / "Отпуск"
    for i in range(3):
        _make_jpeg(source / f"p{i}.jpg", color=(i * 40, i * 20, 10))
    cfg = _cfg(tmp_path, tmp_path / "source")

    stats1, *_ = _run(cfg)
    assert stats1["appended_images"] == 3

    rows = _cache_rows(cfg.target)
    assert len(rows) == 3
    assert all(r[1] == cfg.source for r in rows)      # root == cfg.source
    assert all(r[5] for r in rows)                     # sha256 populated
    assert all(r[4] and r[4] > 0 for r in rows)        # seeded_at populated

    # Второй прогон -- те же файлы: файл не читается ради хеша, exiftool не зовётся.
    calls = {"read": 0, "sha_file": 0, "exif": 0}
    real_read = m.read_file_bytes_with_retry
    monkeypatch.setattr(m, "read_file_bytes_with_retry",
                         lambda *a, **k: (calls.__setitem__("read", calls["read"] + 1)
                                          or real_read(*a, **k)))
    monkeypatch.setattr(m, "sha256_file_with_retry",
                         lambda *a, **k: calls.__setitem__("sha_file", calls["sha_file"] + 1) or "x")
    real_exif = m.exiftool_batch
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **k: (calls.__setitem__("exif", calls["exif"] + 1)
                                             or real_exif(paths, **k)))

    stats2, *_ = _run(cfg)

    assert stats2["skipped_present"] == 3      # все распознаны как дубли
    assert stats2["appended_images"] == 0
    assert calls["read"] == 0                  # файл не перечитан ради sha256
    assert calls["sha_file"] == 0
    assert calls["exif"] == 0                  # media-файлы исключены из exiftool-батча


def test_changed_mtime_invalidates_that_files_cache_entry(tmp_path, monkeypatch):
    source = tmp_path / "source" / "Свадьба"
    _make_jpeg(source / "keep.jpg", color=(1, 2, 3))
    _make_jpeg(source / "touch.jpg", color=(9, 8, 7))
    cfg = _cfg(tmp_path, tmp_path / "source")
    _run(cfg)

    # touch.jpg: тот же размер (перезапись тем же кадром), но новый mtime -> кэш недействителен.
    _make_jpeg(source / "touch.jpg", color=(9, 8, 7))
    os.utime(source / "touch.jpg", (time.time() + 1000, time.time() + 1000))

    read = []
    real_read = m.read_file_bytes_with_retry
    monkeypatch.setattr(m, "read_file_bytes_with_retry",
                         lambda path, *a, **k: (read.append(path) or real_read(path, *a, **k)))

    _run(cfg)

    joined = " ".join(str(s) for s in read)
    assert "touch.jpg" in joined      # mtime сменился -> перечитан ради sha256
    assert "keep.jpg" not in joined   # без изменений -> взят из кэша


# --------------------------------------------------------------------------- #
#  Опт-аут
# --------------------------------------------------------------------------- #

def test_disabled_flag_writes_no_rows_and_rehashes(tmp_path, monkeypatch):
    source = tmp_path / "source" / "A"
    _make_jpeg(source / "x.jpg")
    cfg = _cfg(tmp_path, tmp_path / "source", source_meta_cache=False)

    _run(cfg)
    assert _cache_rows(cfg.target) == []

    read = []
    real_read = m.read_file_bytes_with_retry
    monkeypatch.setattr(m, "read_file_bytes_with_retry",
                         lambda path, *a, **k: read.append(path) or real_read(path, *a, **k))
    _run(cfg)
    assert any("x.jpg" in str(s) for s in read)   # без кэша файл перечитывается ради sha256


def test_source_meta_cache_works_when_archive_hash_cache_off(tmp_path):
    """Разные флаги -- source_meta_cache=True при archive_hash_cache=False: SOURCE-кэш
    наполняется, а archive_cache (кэш стороны архива) остаётся пуст."""
    source = tmp_path / "source" / "A"
    _make_jpeg(source / "x.jpg")
    cfg = _cfg(tmp_path, tmp_path / "source", archive_hash_cache=False, source_meta_cache=True)
    _run(cfg)

    conn = m._open_archive_cache_conn(str(cfg.target))
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_meta_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM archive_cache").fetchone()[0] == 0
    finally:
        conn.close()


def test_suppress_logs_dry_run_writes_no_source_meta_cache_into_target(tmp_path):
    """Та же гарантия, что и test_suppress_logs_dry_run_does_not_write_archive_cache_into_
    target: «Пробный прогон» (suppress_logs=True) не создаёт archive_cache.db в TARGET
    вообще -- source_meta_cache живёт в том же файле, значит и её не пишет."""
    source = tmp_path / "NewBatch"
    source.mkdir()
    target = tmp_path / "MyArchive"
    (target / "__служебные_файлы").mkdir(parents=True)
    _make_jpeg(source / "newphoto.jpg")
    cache_path = target / "__служебные_файлы" / "archive_cache.db"

    result = m.run_for_source(str(source), str(target), dry_run=True, sample_limit=0,
                               log=lambda *a, **k: None, suppress_logs=True, shared_pool=None)
    assert not result.failed
    assert not cache_path.exists()


def test_cli_dry_run_leaves_source_meta_cache_empty(tmp_path):
    """CLI --dry-run (suppress_logs=False) пишет настоящие CSV в TARGET и Фаза 1 создаёт
    archive_cache.db, НО st.cache_conn=None при dry_run -> source_meta_cache не наполняется."""
    source = tmp_path / "source" / "A"
    _make_jpeg(source / "x.jpg")
    cfg = _cfg(tmp_path, tmp_path / "source")
    cfg.dry_run = True
    _run(cfg)
    assert _cache_rows(cfg.target) == []


# --------------------------------------------------------------------------- #
#  Файлы внутри распакованного архива не кэшируются (их read_path временный)
# --------------------------------------------------------------------------- #

def test_files_from_inside_archive_are_not_cached(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    loose = src / "loose.jpg"
    _make_jpeg(loose)
    inner = tmp_path / "inner.jpg"
    _make_jpeg(inner, color=(200, 100, 50))
    with zipfile.ZipFile(src / "bundle.zip", "w") as z:
        z.write(inner, "bundle/inner.jpg")

    cfg = _cfg(tmp_path, src)
    _run(cfg)

    rows = _cache_rows(cfg.target)
    paths = [r[0] for r in rows]
    assert any(p.endswith("loose.jpg") for p in paths)
    assert not any("inner.jpg" in p for p in paths)
    assert not any(cfg.tmp_extract in p for p in paths)


# --------------------------------------------------------------------------- #
#  Чистка по возрасту
# --------------------------------------------------------------------------- #

def test_source_meta_cache_has_root_index(tmp_path):
    """REVIEW-HANDOFF.md Раунд 201 (201-1): _load_source_meta_cache() фильтрует по root --
    индекс делает это не full-scan'ом всей таблицы на каждый источник при `--source all`."""
    target = tmp_path / "t"
    (target / "__служебные_файлы").mkdir(parents=True)
    conn = m._open_archive_cache_conn(str(target))
    try:
        idx = {r[1] for r in conn.execute("PRAGMA index_list('source_meta_cache')")}
    finally:
        conn.close()
    assert "ix_source_meta_cache_root" in idx


def test_prune_removes_stale_rows_keeps_fresh(tmp_path):
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    conn = m._open_archive_cache_conn(str(target))
    now = time.time()
    old = now - (m._SOURCE_META_CACHE_TTL_DAYS + 1) * 86400
    for name, ts in (("old", old), ("fresh", now), ("null_ts", None)):
        conn.execute(
            "INSERT INTO source_meta_cache(read_path,root,size,mtime,seeded_at,sha256) "
            "VALUES (?,?,?,?,?,?)", (name, "r", 1, 1.0, ts, "s"))
    conn.commit()

    m._prune_source_meta_cache(conn, log=lambda *a, **k: None)

    kept = {r[0] for r in conn.execute("SELECT read_path FROM source_meta_cache")}
    conn.close()
    assert kept == {"fresh"}


# --------------------------------------------------------------------------- #
#  Жёсткий выход / крах: теряется только хвост с последнего коммита, не весь прогон
# --------------------------------------------------------------------------- #

def test_hard_exit_mid_run_still_persists_already_seeded_rows(tmp_path, monkeypatch):
    """Крестик окна во время работы поднимает m._HardExit (BaseException) -- она минует
    st.cache_conn.commit() в конце _run_impl(). finally основного цикла Фазы 2 докоммичивает
    накопленное. red-before-green: без коммита в finally незакоммиченная транзакция
    откатывается при раскрутке кадра -> _cache_rows() пуст."""
    src = tmp_path / "source" / "A"
    for i in range(4):
        _make_jpeg(src / f"p{i}.jpg", color=(i * 30, i * 10, 5))
    cfg = _cfg(tmp_path, tmp_path / "source")

    calls = {"n": 0}
    real = m._process_record

    def flaky(rec, st, log=print):
        calls["n"] += 1
        result = real(rec, st, log=log)   # сев source_meta_cache уже отработал внутри (в начале)
        if calls["n"] == 2:
            raise m._HardExit()
        return result

    monkeypatch.setattr(m, "_process_record", flaky)

    with pytest.raises(m._HardExit):
        _run(cfg)

    rows = _cache_rows(cfg.target)
    assert len(rows) >= 1   # файлы, обработанные до _HardExit, докоммичены в finally


def test_maybe_commit_run_cache_commits_only_after_interval(tmp_path):
    target = tmp_path / "t"
    (target / "__служебные_файлы").mkdir(parents=True)
    conn = m._open_archive_cache_conn(str(target))
    conn.execute("INSERT INTO source_meta_cache(read_path,root,size,mtime,seeded_at,sha256) "
                 "VALUES ('p','r',1,1.0,?,'s')", (time.time(),))

    class _ST:
        cache_conn = conn
        cache_last_commit = m.time.monotonic()
        cache_commit_warned = False
    st = _ST()

    def _committed_count():
        other = m._open_archive_cache_conn(str(target))
        try:
            return other.execute("SELECT COUNT(*) FROM source_meta_cache").fetchone()[0]
        finally:
            other.close()

    m._maybe_commit_run_cache(st, log=lambda *a, **k: None)
    assert _committed_count() == 0            # интервал не прошёл -> ничего не сброшено

    st.cache_last_commit -= m._CACHE_COMMIT_INTERVAL_SEC + 1
    m._maybe_commit_run_cache(st, log=lambda *a, **k: None)
    assert _committed_count() == 1            # интервал прошёл -> докоммичено
    conn.close()


# --------------------------------------------------------------------------- #
#  Формат строки: индексы кортежа совпадают с тем, что понимает _analyze_one_item()
# --------------------------------------------------------------------------- #

def test_loaded_tuple_shape_is_consumable_by_analyze_batch(tmp_path, monkeypatch):
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    img = tmp_path / "a.jpg"
    _make_jpeg(img)
    st = img.stat()

    conn = m._open_archive_cache_conn(str(target))

    class _Rec:
        sha256 = None
        phash = None
        duration = None
        width = None
        height = None
        bitrate = None
        exif_dt = None
        exif_dt_source = None
        camera = None
        gps_lat = None
        gps_lon = None
    rec = _Rec()
    rec.sha256 = "seed-sha"
    rec.phash = "seed-phash"
    rec.width, rec.height = 320, 240

    class _Item:
        pass
    it = _Item()
    it.read_path = str(img)
    it.size = st.st_size
    it.mtime = st.st_mtime

    m._seed_source_meta_cache(conn, "root", it, rec)
    conn.commit()
    cache = m._load_source_meta_cache(conn, "root")
    conn.close()

    # analyze_batch(cache=) должна распознать (size,mtime)-совпадение и взять sha/phash из кэша.
    real_item = m.SourceItem(read_path=str(img), origin_display="a.jpg", rel_path="a.jpg",
                              size=st.st_size, mtime=st.st_mtime, ftype="image")
    monkeypatch.setattr(m, "sha256_file_with_retry",
                         lambda *a, **k: pytest.fail("sha256 recomputed despite cache hit"))
    monkeypatch.setattr(m, "sha256_bytes",
                         lambda *a, **k: pytest.fail("sha256 recomputed despite cache hit"))
    recs = m.analyze_batch([real_item], cache=cache, tags_by_path={str(img): {}})
    assert recs[0].sha256 == "seed-sha"
    assert recs[0].phash == "seed-phash"
