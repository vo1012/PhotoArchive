"""SESSION-HANDOFF.txt, 2026-08-31 (Пункт 2 разбора внешнего аудита производительности):
реальная сборка (_run_impl()) раньше звала exiftool ОТДЕЛЬНЫМ спавном на каждый медиафайл
(analyze_batch([item]) без tags_by_path=) -- ~140 мс/файл против ~5 мс/файл при батче
(REVIEW-HANDOFF.md, Раунд 4). Теперь _run_impl() оборачивает обход тем же
_walk_with_exif_prefetch(), что и run_analyze() с 2026-08-02: один спавн exiftool на до
_EXIF_PREFETCH_BATCH_SIZE обычных файлов, при этом каждый item по-прежнему обрабатывается по
одному (не read-ahead для хеша/копии). Тесты ниже: батч реально батчится, "обработано
объектов %" тикает поштучно ПОСЛЕ analyze_batch() (не на отправку батча -- см. Раунд 100),
DVD/архивы не задвоены, EXIF-теги по-прежнему доходят до решения, --sample-limit не
разгоняет батч."""
import io
import zipfile

import photosort_win as m
from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _spy_exiftool(monkeypatch, sink):
    real = m.exiftool_batch

    def _spy(paths, **kw):
        sink.append(list(paths))
        return real(list(paths), **kw)

    monkeypatch.setattr(m, "exiftool_batch", _spy)


def _spy_ticks(monkeypatch, sink):
    real = m.ProgressReporter.add_object_progress

    def _spy(self, n=1):
        sink.append(n)
        return real(self, n)

    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy)


def _run_build(tmp_path, source, dry_run=False):
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    cfg = m.Config(source=str(source), target=str(target), dry_run=dry_run, sample_limit=0,
                    workdir=str(workdir))
    stats, *_ = m.run(cfg, log=lambda *a, **k: None)
    return stats, target


# ---------------------------------------------------------------------------
# батчинг exiftool на пути сборки
# ---------------------------------------------------------------------------

def test_build_path_batches_exiftool_instead_of_one_spawn_per_file(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for i in range(6):
        _make_jpeg(album / f"photo{i}.jpg", color=(i * 30, 40, 200))

    calls = []
    _spy_exiftool(monkeypatch, calls)
    _run_build(tmp_path, source)

    # До фикса: 6 вызовов по одному пути каждый (analyze_batch([item]) на каждой итерации).
    # После: ровно один спавн на все 6 путей.
    assert len(calls) == 1, calls
    assert len(calls[0]) == 6, calls


def test_build_path_dry_run_also_batches_exiftool(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for i in range(4):
        _make_jpeg(album / f"p{i}.jpg", color=(i * 30, 40, 200))

    calls = []
    _spy_exiftool(monkeypatch, calls)
    _run_build(tmp_path, source, dry_run=True)

    assert len(calls) == 1 and len(calls[0]) == 4, calls


def test_build_path_exif_date_still_reaches_placement(tmp_path, monkeypatch):
    """Регрессия: tags_by_path из батча реально ПОТРЕБЛЯЕТСЯ analyze_batch() на пути сборки
    (не просто вычисляется и выбрасывается) -- дата из EXIF должна раскладывать файл по
    своему году, а не по mtime."""
    source = tmp_path / "NewBatch"
    source.mkdir()
    _make_jpeg(source / "shot.jpg")

    def _fake_batch(paths, **kw):
        return {p: {"DateTimeOriginal": "2011:06:15 12:00:00"} for p in paths}

    monkeypatch.setattr(m, "exiftool_batch", _fake_batch)
    _stats, target = _run_build(tmp_path, source)

    placed = list(target.rglob("shot.jpg"))
    assert placed, list(target.rglob("*"))
    assert "2011" in str(placed[0]), placed[0]


# ---------------------------------------------------------------------------
# "обработано объектов %" -- поштучно, после analyze_batch()
# ---------------------------------------------------------------------------

def test_build_path_object_progress_ticks_once_per_item_after_batch(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        _make_jpeg(album / name)
    (album / "note.txt").write_bytes(b"not media")  # "other" -- не тикает вовсе (2026-08-17)

    events = []
    real_batch = m.exiftool_batch
    real_tick = m.ProgressReporter.add_object_progress

    def _spy_batch(paths, **kw):
        events.append(("batch", len(list(paths))))
        return real_batch(list(paths), **kw)

    def _spy_tick(self, n=1):
        events.append(("tick", n))
        return real_tick(self, n)

    monkeypatch.setattr(m, "exiftool_batch", _spy_batch)
    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy_tick)
    _run_build(tmp_path, source)

    ticks = [n for kind, n in events if kind == "tick"]
    assert ticks == [1, 1, 1], events
    # Батч (дешёвое тегирование) идёт РАНЬШЕ любого тика этого батча -- тики следуют ЗА
    # реальной обработкой (analyze_batch/phash/ffprobe), не опережают её.
    batch_idx = next(i for i, e in enumerate(events) if e[0] == "batch")
    first_tick_idx = next(i for i, e in enumerate(events) if e[0] == "tick")
    assert batch_idx < first_tick_idx, events


def test_build_path_video_object_tick_after_ffprobe_not_before(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    _make_jpeg(album / "photo.jpg")
    (album / "clip.mp4").write_bytes(b"fake video bytes")

    events = []
    real_tick = m.ProgressReporter.add_object_progress

    def _fake_ffprobe(path):
        events.append(("ffprobe", m.os.path.basename(path)))
        return (1.0, 640, 480, 1000)

    def _spy_tick(self, n=1):
        events.append(("tick", n))
        return real_tick(self, n)

    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
    monkeypatch.setattr(m, "video_duration_and_resolution", _fake_ffprobe)
    monkeypatch.setattr(m, "video_phash_3frames", lambda path, dur: [])
    monkeypatch.setattr(m.ProgressReporter, "add_object_progress", _spy_tick)
    _run_build(tmp_path, source)

    ffprobe_idx = next(i for i, e in enumerate(events) if e[0] == "ffprobe")
    assert [e for e in events[:ffprobe_idx] if e[0] == "tick"] == [], events
    assert any(e[0] == "tick" for e in events[ffprobe_idx:]), events


def test_build_path_archive_contents_tick_once_as_a_unit(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    source.mkdir()
    with zipfile.ZipFile(source / "album.zip", "w") as zf:
        for name in ("p1.jpg", "p2.jpg", "p3.jpg"):
            buf = io.BytesIO()
            Image.new("RGB", (400, 300), (20, 40, 60)).save(buf, "JPEG")
            zf.writestr(name, buf.getvalue())

    ticks = []
    _spy_ticks(monkeypatch, ticks)
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
    _run_build(tmp_path, source)

    # Архив -- одна единица: ровно один тик за все 3 файла внутри (defer_media_object_tick
    # не должен позволить основному циклу тикнуть за каждый распакованный файл отдельно).
    assert ticks == [1], ticks


# ---------------------------------------------------------------------------
# DVD-юниты не задеты
# ---------------------------------------------------------------------------

def test_walk_with_exif_prefetch_yields_dvd_items_alone_without_exiftool(tmp_path, monkeypatch):
    vob = tmp_path / "VTS_01_0.VOB"
    vob.write_bytes(b"v" * 100)
    dvd_item = m.SourceItem(str(vob), "VIDEO_TS/VTS_01_0.VOB", "Disc/VIDEO_TS/VTS_01_0.VOB",
                             vob.stat().st_size, vob.stat().st_mtime, "video",
                             dvd_dest_path=r"X:\Albums\Disc\VIDEO_TS\VTS_01_0.VOB")
    jpg = tmp_path / "a.jpg"
    _make_jpeg(jpg)
    img_item = m.SourceItem(str(jpg), "a.jpg", "a.jpg", jpg.stat().st_size, jpg.stat().st_mtime,
                             "image")

    seen = []
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: seen.append(list(paths)) or {p: {} for p in paths})

    out = list(m._walk_with_exif_prefetch(
        iter([dvd_item, img_item]), str(tmp_path / "_extract"), batch_size=50))

    assert [it.read_path for it, _tags in out] == [str(vob), str(jpg)]
    dvd_pair = next(t for it, t in out if it.read_path == str(vob))
    assert dvd_pair == {}
    # exiftool звался ровно один раз и БЕЗ .VOB (только за настоящее изображение).
    assert seen == [[str(jpg)]], seen


def test_build_path_dvd_unit_not_exiftooled_and_ticks_once(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    disc = source / "Disc"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 400)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"i" * 40)
    (disc / "VIDEO_TS" / "VIDEO_TS.BUP").write_bytes(b"b" * 40)

    calls = []
    ticks = []
    _spy_exiftool(monkeypatch, calls)
    _spy_ticks(monkeypatch, ticks)
    _run_build(tmp_path, source)

    assert calls == [], calls          # ни одного спавна exiftool на .VOB/.IFO/.BUP
    assert ticks == [1], ticks         # весь VIDEO_TS -- одна единица прогресса


# ---------------------------------------------------------------------------
# --sample-limit
# ---------------------------------------------------------------------------

def test_build_path_sample_limit_bounds_exiftool_batch_size(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for i in range(10):
        _make_jpeg(album / f"photo{i}.jpg", color=(i * 20, 40, 200))

    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    calls = []
    _spy_exiftool(monkeypatch, calls)

    cfg = m.Config(source=str(source), target=str(target), sample_limit=3, workdir=str(workdir))
    m.run(cfg, log=lambda *a, **k: None)

    # Без ограничения батча прогрев набрал бы все 10 путей ДО первой проверки sample_limit
    # (она снаружи генератора). С min(_BUILD_EXIF_PREFETCH_BATCH_SIZE, sample_limit) -- ни один
    # спавн не больше лимита.
    assert calls, "exiftool должен был вызваться хотя бы раз"
    assert all(len(paths) <= 3 for paths in calls), calls


# ---------------------------------------------------------------------------
# размер build-батча (уже́ Паспорта -- зона поражения при сбое чанка exiftool)
# ---------------------------------------------------------------------------

def test_build_batch_size_is_smaller_than_passport():
    # Раунд 172 (наблюдение "вне рамок") + решение пользователя 2026-08-31: сбой одного чанка
    # exiftool роняет ВЕСЬ чанк на mtime-дату, а уже размещённые копии залипают (дедуп по SHA
    # на перезапуске не перекладывает) -- build-путь чаще идёт по медленному USB/сети, где
    # 120-c-таймаут достижимее -> у него батч вдвое меньше.
    assert m._BUILD_EXIF_PREFETCH_BATCH_SIZE == 100
    assert m._BUILD_EXIF_PREFETCH_BATCH_SIZE < m._EXIF_PREFETCH_BATCH_SIZE


def test_build_path_chunks_exiftool_at_build_batch_size(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_BUILD_EXIF_PREFETCH_BATCH_SIZE", 3)
    source = tmp_path / "NewBatch"
    album = source / "Album"
    album.mkdir(parents=True)
    for i in range(7):
        _make_jpeg(album / f"p{i}.jpg", color=(i * 30, 40, 200))

    calls = []
    _spy_exiftool(monkeypatch, calls)
    _run_build(tmp_path, source)

    # 7 файлов при батче 3 -> спавны [3, 3, 1], а не один на 7.
    assert [len(c) for c in calls] == [3, 3, 1], calls
