"""Альбомный редизайн (SESSION-HANDOFF.txt, 2026-08-08): интеграционные тесты сверх
tests/test_dump_segments.py::TestFindAlbum -- проверяют реальный физический результат полного
прогона (не только find_album() как чистую функцию), плюс поведение дедупа внутриальбомных
дублей (маркер-файл __ПРОПУЩЕННЫЕ_ДУБЛИ.txt удалён 2026-08-29, данные -- в skipped.csv/xlsx)."""
import photosort_win as m
from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _stub_exiftool(monkeypatch, tags_by_path=None):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: (tags_by_path or {}).get(p, {}) for p in paths})


def _run(tmp_path, source, dry_run=False):
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    cfg = m.Config(source=str(source), target=str(target), dry_run=dry_run, sample_limit=0,
                    workdir=str(workdir))
    stats, *_ = m.run(cfg, log=lambda *a, **k: None)
    return stats, target


def test_nested_same_named_albums_land_in_separate_physical_folders(tmp_path, monkeypatch):
    # Исходный запрос пользователя, запустивший весь редизайн: "Мои фото/Свадьба" и
    # "Мои фото/Отпуск" -- два разных альбома, не один общий "Мои фото" с подпапками.
    #
    # Раунд 77 ревью (REVIEW-HANDOFF.md, ПРИДИРКА 2): этот тест НЕ различает старое/новое
    # поведение -- физическое размещение файлов по путям Albums/Мои фото/Свадьба/... уже было
    # верным ДО редизайна (старый алгоритм "самый верхний сегмент + сохранённый subpath" клал
    # файлы туда же). Оставлен как обычная регрессия физического размещения (полезная сама по
    # себе), но название/это место НЕ следует читать как "тест, подтверждающий редизайн" --
    # реальный различитель (недосчёт вложенных альбомов в n_albums_detected/отчёте) --
    # tests/test_analyze_batch.py::TestAlbumDateGroupingStats::
    # test_n_albums_detected_counts_every_folder_in_the_tree_separately (red-before-green
    # подтверждён отдельно, см. REVIEW-HANDOFF.md Раунд 77).
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "Мои фото" / "Свадьба").mkdir(parents=True)
    (source / "Мои фото" / "Отпуск").mkdir(parents=True)
    _make_jpeg(source / "Мои фото" / "Свадьба" / "a.jpg", color=(10, 20, 30))
    _make_jpeg(source / "Мои фото" / "Отпуск" / "b.jpg", color=(200, 100, 50))

    _stats, target = _run(tmp_path, source)

    assert (target / "Albums" / "Мои фото" / "Свадьба" / "a.jpg").exists()
    assert (target / "Albums" / "Мои фото" / "Отпуск" / "b.jpg").exists()
    # Общий родитель "Мои фото" сам по себе не альбом-контейнер -- никакого файла прямо в нём.
    assert not (target / "Albums" / "Мои фото" / "a.jpg").exists()


def test_event_subfolders_also_split_into_separate_nested_albums(tmp_path, monkeypatch):
    # Осознанно принятое следствие того же правила: "Свадьба 2015/Церемония" и
    # "Свадьба 2015/Банкет" -- тоже разные вложенные альбомы, не подпапки одного "Свадьба 2015".
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "Свадьба 2015" / "Церемония").mkdir(parents=True)
    (source / "Свадьба 2015" / "Банкет").mkdir(parents=True)
    _make_jpeg(source / "Свадьба 2015" / "Церемония" / "a.jpg", color=(10, 20, 30))
    _make_jpeg(source / "Свадьба 2015" / "Банкет" / "b.jpg", color=(200, 100, 50))

    _stats, target = _run(tmp_path, source)

    assert (target / "Albums" / "Свадьба 2015" / "Церемония" / "a.jpg").exists()
    assert (target / "Albums" / "Свадьба 2015" / "Банкет" / "b.jpg").exists()


def test_dump_ancestor_before_real_name_falls_to_bydate_entirely(tmp_path, monkeypatch):
    # 2026-08-08: раньше два разных dump-родителя (DCIM/Camera) перед одинаковым именем
    # альбома физически разводились в разные ветки Albums\ -- теперь DCIM/Camera отравляют
    # всё целиком, ни один файл не попадает в Albums\ вовсе, оба уходят в ByDate\.
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "DCIM" / "Отпуск").mkdir(parents=True)
    (source / "Camera" / "Отпуск").mkdir(parents=True)
    _make_jpeg(source / "DCIM" / "Отпуск" / "a.jpg", color=(10, 20, 30))
    _make_jpeg(source / "Camera" / "Отпуск" / "b.jpg", color=(200, 100, 50))

    stats, target = _run(tmp_path, source)

    assert not (target / "Albums").exists() or not any((target / "Albums").iterdir())
    assert stats["appended_images"] == 2


def _album_files(target, *parts):
    d = target.joinpath("Albums", *parts)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_within_album_duplicate_is_skipped_not_double_copied(tmp_path, monkeypatch):
    # Маркер-файл __ПРОПУЩЕННЫЕ_ДУБЛИ.txt удалён 2026-08-29 -- проверяем само поведение:
    # байт-идентичная копия кадра под другим именем в той же ветке НЕ копируется дважды,
    # учитывается как skipped_present, инвариант "в альбоме не больше файлов, чем в источнике".
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg", color=(1, 2, 3))
    (source / "AlbumX" / "a_copy.jpg").write_bytes((source / "AlbumX" / "a.jpg").read_bytes())

    stats, target = _run(tmp_path, source)

    assert stats["skipped_present"] == 1
    assert _album_files(target, "AlbumX") == ["a.jpg"]  # только один из двух, никаких .txt-маркеров


def test_within_album_duplicate_recorded_in_skipped_csv(tmp_path, monkeypatch):
    # Данные, которые раньше дублировались в маркер-файле, полностью остаются в skipped.csv.
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg", color=(1, 2, 3))
    (source / "AlbumX" / "a_copy.jpg").write_bytes((source / "AlbumX" / "a.jpg").read_bytes())

    _stats, target = _run(tmp_path, source)

    skipped_csv = (target / "__служебные_файлы" / "logs" / "skipped.csv").read_text(encoding="utf-8")
    assert "a_copy.jpg" in skipped_csv and "a.jpg" in skipped_csv


def test_no_marker_txt_anywhere_in_archive(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumA").mkdir(parents=True)
    (source / "AlbumB").mkdir(parents=True)
    _make_jpeg(source / "AlbumA" / "a.jpg", color=(9, 9, 9))
    (source / "AlbumA" / "a_copy.jpg").write_bytes((source / "AlbumA" / "a.jpg").read_bytes())
    (source / "AlbumB" / "a_dup.jpg").write_bytes((source / "AlbumA" / "a.jpg").read_bytes())

    _stats, target = _run(tmp_path, source)

    txts = [str(p) for p in (target / "Albums").rglob("*.txt")]
    assert txts == [], txts
    assert not hasattr(m, "SKIPPED_DUP_MARKER_FILENAME")


def test_albums_merged_csv_logs_append_for_each_nested_subalbum_independently(tmp_path, monkeypatch):
    """Раунд 77 ревью (REVIEW-HANDOFF.md, [ЗАМЕЧАНИЕ] 1): st.merged_albums_seen раньше
    ключевался голым верхним сегментом (album_prefix) -- под новой моделью ("каждая папка --
    свой альбом") два РАЗНЫХ вложенных подальбома под одним и тем же верхним сегментом
    (PlaceA/Sub1, PlaceA/Sub2) делили один ключ, и проверка "уже видели этот альбом в этом
    прогоне" для второго короткозамыкала на первом -- реальная дозапись в уже существующую на
    диске Sub2 молча не логировалась в albums_merged.csv. Ключуем по dest_dir (уникален на
    физическую папку) вместо голого album_prefix."""
    _stub_exiftool(monkeypatch)
    target = tmp_path / "target"
    existing = target / "Albums" / "PlaceA" / "Sub2"
    existing.mkdir(parents=True)
    _make_jpeg(existing / "old.jpg", color=(1, 2, 3))

    source = tmp_path / "source"
    (source / "PlaceA" / "Sub1").mkdir(parents=True)
    (source / "PlaceA" / "Sub2").mkdir(parents=True)
    _make_jpeg(source / "PlaceA" / "Sub1" / "x.jpg", color=(10, 20, 30))
    _make_jpeg(source / "PlaceA" / "Sub2" / "y.jpg", color=(40, 50, 60))

    _run(tmp_path, source)

    csv_path = target / "__служебные_файлы" / "logs" / "albums_merged.csv"
    rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2  # header + exactly one append event (Sub2 -- Sub1 is brand new)
    assert "PlaceA/Sub2/y.jpg" in rows[1]
