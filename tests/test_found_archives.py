"""ROADMAP.md, analyze как "2 части": обнаружение существующих архивов (__служебные_файлы)
внутри просканированного SOURCE во время analyze/analyze-full -- SourceWalker.found_archive_roots
(побочный продукт обхода, см. photosort_win.py:SourceWalker._walk_dir), классификация
top-level/nested (photosort_win.py:classify_found_archives) и _render_found_archives()
(report.py). REVIEW-HANDOFF.md, Раунд 44: с SESSION-HANDOFF.txt пункта I (a41117c) единственный
прод-вызов generate_report_from_analyze_stats() (photosort_win.py:_finalize_analyze_report())
больше не передаёт found_archives= -- "часть 2" report.html в проде больше не рендерится,
_render_found_archives() вызывается только этим тестовым файлом напрямую (сама инфраструктура
сознательно оставлена нетронутой, см. SESSION-HANDOFF.txt, «I» -- полное вычищение отложено)."""
import os
from collections import Counter

import photosort_win as m
import report as r


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


# ---------------------------------------------------------------------------
# SourceWalker: обнаружение во время реального обхода
# ---------------------------------------------------------------------------

def test_source_walker_finds_nested_archive_and_stops_descending(tmp_path):
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    umbrella = archive_root / "__служебные_файлы"
    umbrella.mkdir(parents=True)
    (umbrella / "logs").mkdir()
    # Если бы обход спускался внутрь __служебные_файлы, эта "приманка" всплыла бы где-то в
    # результатах -- она не должна быть даже прочитана.
    (umbrella / "logs" / "sentinel_do_not_read.txt").write_text("x", encoding="utf-8")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.found_archive_roots == [os.path.realpath(str(archive_root))]


def test_source_walker_ignores_own_target(tmp_path):
    # TARGET совпадает с местом, где программа сама создаёт __служебные_файлы -- self-eating
    # protection не даёт обходу вообще зайти внутрь TARGET, поэтому TARGET никогда не
    # попадает в found_archive_roots через сам walk (см. classify_found_archives() -- для
    # TARGET нужна отдельная проверка, не walk).
    source = tmp_path / "source"
    target = source / "already_archived"
    (target / "__служебные_файлы").mkdir(parents=True)
    source.mkdir(exist_ok=True)

    cfg = _make_cfg(tmp_path, source=str(source), target=str(target))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.found_archive_roots == []


def test_source_walker_finds_archive_by_albums_bydate_fallback_when_umbrella_missing(tmp_path):
    # REVIEW-HANDOFF.md, Раунд 24: если __служебные_файлы переименовали/удалили, а сама
    # структура (Albums+ByDate) цела -- архив всё равно должен опознаваться, тем же fallback,
    # что уже есть у _target_has_existing_archive()/warn_if_target_nested_in_archive().
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    (archive_root / "Albums").mkdir(parents=True)
    (archive_root / "ByDate").mkdir(parents=True)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.found_archive_roots == [os.path.realpath(str(archive_root))]


def test_source_walker_albums_only_without_bydate_is_not_a_false_positive(tmp_path):
    source = tmp_path / "source"
    (source / "just_an_album_folder" / "Albums").mkdir(parents=True)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.found_archive_roots == []


def test_source_walker_marker_and_fallback_both_firing_deduplicates_downstream(tmp_path):
    # Полный архив (и __служебные_файлы, и Albums+ByDate целы) -- находится ОБОИМИ
    # механизмами на разных итерациях обхода; classify_found_archives() должен схлопнуть это
    # в один top-level путь, а не задвоить.
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    (archive_root / "Albums").mkdir(parents=True)
    (archive_root / "ByDate").mkdir(parents=True)
    (archive_root / "__служебные_файлы").mkdir(parents=True)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert set(walker.found_archive_roots) == {os.path.realpath(str(archive_root))}
    top, nested = m.classify_found_archives(walker.found_archive_roots, cfg, "analyze")
    assert top == [os.path.realpath(str(archive_root))]
    assert nested == {}


# ---------------------------------------------------------------------------
# classify_found_archives: чистая логика над путями (без walk)
# ---------------------------------------------------------------------------

def test_classify_no_roots(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert m.classify_found_archives([], cfg, "analyze") == ([], {})


def test_classify_independent_roots_are_both_top_level(tmp_path):
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    cfg = _make_cfg(tmp_path)
    top, nested = m.classify_found_archives([a, b], cfg, "analyze")
    assert sorted(top) == sorted([a, b])
    assert nested == {}


def test_classify_nested_inside_albums_is_excluded_and_escalated(tmp_path):
    parent = str(tmp_path / "parent")
    child = str(tmp_path / "parent" / "Albums" / "Свадьба")
    cfg = _make_cfg(tmp_path)
    top, nested = m.classify_found_archives([parent, child], cfg, "analyze")
    assert top == [parent]
    assert nested == {parent: [child]}


def test_classify_nested_outside_organized_structure_excluded_not_escalated(tmp_path):
    parent = str(tmp_path / "parent")
    child = str(tmp_path / "parent" / "random_folder" / "sub")
    cfg = _make_cfg(tmp_path)
    top, nested = m.classify_found_archives([parent, child], cfg, "analyze")
    assert top == [parent]
    assert nested == {}


def test_classify_never_adds_target(tmp_path):
    # 2026-08-04: analyze-full (единственный режим, добавлявший TARGET в найденные архивы,
    # см. классификацию/исключение в classify_found_archives()) удалён целиком -- TARGET
    # никогда не добавляется, независимо от mode.
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    cfg = _make_cfg(tmp_path, target=str(target))
    top, nested = m.classify_found_archives([], cfg, "analyze")
    assert top == []


# ---------------------------------------------------------------------------
# run_analyze: end-to-end -- found_archive_top_level заполняется
# ---------------------------------------------------------------------------

def test_run_analyze_populates_found_archives_on_stats(tmp_path):
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    (archive_root / "__служебные_файлы").mkdir(parents=True)
    (source / "plain_folder").mkdir()
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    stats = m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert stats.found_archive_top_level == [os.path.realpath(str(archive_root))]


# ---------------------------------------------------------------------------
# report.py: рендер части 2
# ---------------------------------------------------------------------------

def test_render_found_archives_empty_top_level_renders_nothing():
    assert r._render_found_archives([], {}) == ""


def test_render_found_archives_single_archive_no_logs(tmp_path):
    root = tmp_path / "archive1"
    (root / "__служебные_файлы" / "logs").mkdir(parents=True)
    html_out = r._render_found_archives([str(root)], {})
    assert "На этом диске найден архив PhotoArchive" in html_out
    assert str(root) in html_out
    assert "НЕДОСТОВЕРНЫ" not in html_out  # без вложенности -- обычная оговорка, не жёсткая


def test_render_found_archives_plural_heading_for_multiple():
    html_out = r._render_found_archives(["/a", "/b"], {})
    assert "найдено 2 архива" in html_out


def test_render_found_archives_nested_escalates_caveat_and_adds_checklist_item():
    parent = "/parent"
    child = "/parent/Albums/Свадьба"
    html_out = r._render_found_archives([parent], {parent: [child]})
    assert "НЕДОСТОВЕРНЫ" in html_out
    assert "постороннюю структуру" in html_out.lower() or "посторонних структур" in html_out.lower()
    assert child in html_out


# ---------------------------------------------------------------------------
# SourceWalker: найденный архив внутри SOURCE -- бухгалтерия (found_archive_roots), но БЕЗ
# исключения содержимого (SESSION-HANDOFF.txt п.7, 2026-08-05, боевой прогон): раньше
# analyze/analyze-quick/analyze-full проактивно пропускали содержимое найденного архива через
# cfg.include_found_archives_in_analyze/SourceWalker(exclude_found_archives=...) -- расходилось
# с тем, что реально делает сборка ([3]/CLI archive/--dry-run всегда обходят SOURCE целиком).
# Обе настройки убраны -- analyze теперь тоже всегда обходит и учитывает ВСЁ содержимое SOURCE.
# ---------------------------------------------------------------------------

def _rel_paths(items):
    return sorted(item.rel_path for item in items)


def test_source_walker_walks_into_found_archive_content(tmp_path):
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    (archive_root / "__служебные_файлы").mkdir(parents=True)
    (archive_root / "Albums").mkdir()
    (archive_root / "Albums" / "x.jpg").write_bytes(b"x")
    (source / "fresh.jpg").write_bytes(b"z")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert _rel_paths(items) == ["fresh.jpg", "old_photos/Albums/x.jpg"]
    # found_archive_roots -- бухгалтерия по-прежнему заполняется (питает "уже есть собранный
    # архив" в рекомендациях, см. report.py:_render_analyze_recommendations()), несмотря на то
    # что содержимое теперь реально обходится, не исключается.
    assert os.path.realpath(str(archive_root)) in walker.found_archive_roots


def test_source_walker_self_scan_root_with_own_archive_structure_walks_normally(tmp_path):
    """Критично для [4] Паспорт архива (run_passport(): cfg.source=TARGET) -- TARGET по
    конструкции сам содержит __служебные_файлы/Albums/ByDate прямо в корне -- is_root должен
    быть железной защитой (это НЕ "найденный архив внутри SOURCE", это сам SOURCE)."""
    source = tmp_path / "source"  # играет роль TARGET для run_passport()
    (source / "__служебные_файлы").mkdir(parents=True)
    (source / "Albums").mkdir()
    (source / "Albums" / "x.jpg").write_bytes(b"x")
    (source / "ByDate").mkdir()
    (source / "ByDate" / "y.jpg").write_bytes(b"y")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert _rel_paths(items) == ["Albums/x.jpg", "ByDate/y.jpg"]


def test_run_analyze_counts_found_archive_content_like_everything_else(tmp_path):
    source = tmp_path / "source"
    archive_root = source / "old_photos"
    (archive_root / "__служебные_файлы").mkdir(parents=True)
    (archive_root / "Albums").mkdir()
    (archive_root / "Albums" / "x.jpg").write_bytes(b"x")
    (source / "fresh.jpg").write_bytes(b"z")
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    stats = m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert stats.total_files == 2  # оба файла посчитаны -- никакого исключения больше нет
    assert stats.found_archive_top_level == [os.path.realpath(str(archive_root))]


# ---------------------------------------------------------------------------
# report.py: рекомендация про найденный архив (пункт I / п.7)
# ---------------------------------------------------------------------------

def test_analyze_recommendations_names_found_archive_path_and_suggests_passport():
    model = {"found_archive_paths": ["/src/old_photos"]}
    html_out = r._render_analyze_recommendations(model)
    assert "На источнике уже есть собранный архив" in html_out
    assert "/src/old_photos" in html_out
    assert "дублирования не будет" in html_out
    assert "Паспорт архива" in html_out


def test_analyze_recommendations_plural_heading_for_multiple_found_archives():
    model = {"found_archive_paths": ["/a", "/b"]}
    html_out = r._render_analyze_recommendations(model)
    assert "в 2 местах" in html_out


def test_analyze_recommendations_says_nothing_when_no_archive_found():
    model = {"found_archive_paths": []}
    html_out = r._render_analyze_recommendations(model)
    assert "собранный архив" not in html_out


def test_generate_report_from_analyze_stats_no_longer_renders_found_archive_block(tmp_path):
    """2026-07-31, пункт I: analyze больше не строит "Часть 2" -- found_archives-параметр
    generate_report_from_analyze_stats() не передаётся вовсе из _finalize_analyze_report()
    (photosort_win.py), поведение по умолчанию (found_archives=None) уже "не рендерить"."""
    stats = _FakeAnalyzeStatsForFoundArchivesReport()
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path), level="analyze")
    html_out = out_path.read_text(encoding="utf-8")
    assert "На этом диске найден архив" not in html_out


class _FakeAnalyzeStatsForFoundArchivesReport:
    """Минимальная замена AnalyzeStats для одного прицельного теста выше -- см. _FakeAnalyzeStats
    в tests/test_report.py для более полной версии, не импортируется отсюда, чтобы не тянуть
    межмодульную тестовую зависимость ради одного поля."""
    def __init__(self):
        self.n_images = 1
        self.n_raw = 0
        self.n_videos = 0
        self.oldest_date = None
        self.oldest_display = None
        self.n_near_dupes = 0
        self.predicted_unique_count = 1
        self.n_exact_dupes = 0
        self.n_broken_or_zero = 0
        self.disputed_paths = []
        self.unreadable_paths = []
        self.total_bytes = 100
        self.predicted_unique_bytes = 100
        self.dates_by_year = Counter()
        self.dates_by_year_month = Counter()
        self.tier_counts = Counter()
        self.near_dup_edges = []
        self.n_archives_found = 0
        self.n_archives_with_media = 0
        self.found_archive_top_level = ["/some/found/archive"]
        self.bytes_by_kind = Counter()
        self.n_objects_total = 0
        self.files_by_location = Counter()
        self.bytes_by_location = Counter()
        self.mode = "analyze-quick"
        self.cities = Counter()
        self.cameras = Counter()
        self.encrypted_archive_paths = []
        self.n_albums_detected = 0
        self.n_media_in_albums = 0
        self.bydate_media_by_folder = Counter()
        self.n_media_by_date = 0
        self.n_tier_b_bydate = 0
        self.n_tier_c_bydate = 0
        self.n_tier_d_bydate = 0
