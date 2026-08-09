"""Альбомный редизайн (SESSION-HANDOFF.txt, 2026-08-08): интеграционные тесты сверх
tests/test_dump_segments.py::TestFindAlbum -- проверяют реальный физический результат полного
прогона (не только find_album() как чистую функцию), плюс новый маркер-файл
__ПРОПУЩЕННЫЕ_ДУБЛИ.txt (замена снесённого __ВНИМАНИЕ_объединённая_папка.txt)."""
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


def test_skipped_dup_marker_written_for_within_album_duplicate(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg", color=(1, 2, 3))
    # Байт-идентичная копия того же кадра под другим именем -- пропущена как дубль ВНУТРИ
    # той же ветки/альбома.
    (source / "AlbumX" / "a_copy.jpg").write_bytes((source / "AlbumX" / "a.jpg").read_bytes())

    stats, target = _run(tmp_path, source)

    album_dir = target / "Albums" / "AlbumX"
    marker = album_dir / m.SKIPPED_DUP_MARKER_FILENAME
    assert stats["skipped_present"] == 1
    assert marker.exists()
    content = marker.read_text(encoding="utf-8")
    assert "a_copy.jpg" in content
    assert "a.jpg" in content


def test_skipped_dup_marker_not_written_when_no_duplicates(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg")

    _stats, target = _run(tmp_path, source)

    assert not (target / "Albums" / "AlbumX" / m.SKIPPED_DUP_MARKER_FILENAME).exists()


def test_skipped_dup_marker_not_written_during_dry_run(tmp_path, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg", color=(1, 2, 3))
    (source / "AlbumX" / "a_copy.jpg").write_bytes((source / "AlbumX" / "a.jpg").read_bytes())

    stats, target = _run(tmp_path, source, dry_run=True)

    assert stats["skipped_present"] == 1
    # dry_run -- сам маркер-файл не пишется (RULES.md, "пробный прогон ничего не пишет") --
    # resolve_dest_path() создаёт пустую папку альбома даже в dry_run (нужна для проверки
    # занятости имени), это не связано с новым маркер-файлом, поэтому не проверяем её отсутствие.
    assert not (target / "Albums" / "AlbumX" / m.SKIPPED_DUP_MARKER_FILENAME).exists()


def test_skipped_dup_marker_overwritten_not_appended_on_rerun(tmp_path, monkeypatch):
    # Новый маркер-файл, в отличие от снесённого __ВНИМАНИЕ_объединённая_папка.txt, не несёт
    # роли "памяти между прогонами" -- перезаписывается целиком на каждом прогоне.
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumX").mkdir(parents=True)
    _make_jpeg(source / "AlbumX" / "a.jpg", color=(1, 2, 3))
    (source / "AlbumX" / "a_copy.jpg").write_bytes((source / "AlbumX" / "a.jpg").read_bytes())

    stats1, target = _run(tmp_path, source)
    marker = target / "Albums" / "AlbumX" / m.SKIPPED_DUP_MARKER_FILENAME
    lines1 = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert stats1["skipped_present"] == 1
    assert len(lines1) == 1  # только a_copy.jpg -- дубль ВНУТРИ этого первого прогона

    # Второй прогон по тому же (неизменному) SOURCE -- на этот раз ОБА файла совпадают с уже
    # присутствующим в архиве содержимым, значит 2 события за ЭТОТ прогон. Если бы файл
    # дозаписывался (как старый __ВНИМАНИЕ_объединённая_папка.txt), итог был бы 1+2=3 строки.
    stats2, target = _run(tmp_path, source)
    lines2 = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert stats2["skipped_present"] == 2
    assert len(lines2) == 2  # перезаписан целиком, не задваивается между прогонами


def test_cross_album_content_duplicate_does_not_write_marker(tmp_path, monkeypatch):
    # RULES.md: глобальный SHA-256 дедуп-пул остаётся межветочным -- совпадение по контенту с
    # файлом из ДРУГОГО альбома -- не "внутриветочный" дубль, в новый маркер-файл не попадает.
    _stub_exiftool(monkeypatch)
    source = tmp_path / "source"
    (source / "AlbumA").mkdir(parents=True)
    (source / "AlbumB").mkdir(parents=True)
    _make_jpeg(source / "AlbumA" / "a.jpg", color=(9, 9, 9))
    (source / "AlbumB" / "a_dup.jpg").write_bytes((source / "AlbumA" / "a.jpg").read_bytes())

    stats, target = _run(tmp_path, source)

    assert stats["skipped_present"] == 1
    assert not (target / "Albums" / "AlbumA" / m.SKIPPED_DUP_MARKER_FILENAME).exists()
    assert not (target / "Albums" / "AlbumB" / m.SKIPPED_DUP_MARKER_FILENAME).exists()


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
