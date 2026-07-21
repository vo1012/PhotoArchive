"""ROADMAP.md, analyze как "2 части": обнаружение существующих архивов (__служебные_файлы)
внутри просканированного SOURCE во время analyze/analyze-full -- SourceWalker.found_archive_roots
(побочный продукт обхода, см. photosort_win.py:SourceWalker._walk_dir), классификация
top-level/nested (photosort_win.py:classify_found_archives) и рендер части 2 report.html
(report.py:_render_found_archives)."""
import os

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


def test_classify_analyze_full_adds_target_when_it_has_an_archive(tmp_path):
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    cfg = _make_cfg(tmp_path, target=str(target))
    top, nested = m.classify_found_archives([], cfg, "analyze-full")
    assert top == [os.path.realpath(str(target))]


def test_classify_analyze_full_no_duplicate_when_target_already_found(tmp_path):
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    target_real = os.path.realpath(str(target))
    cfg = _make_cfg(tmp_path, target=str(target))
    top, nested = m.classify_found_archives([target_real], cfg, "analyze-full")
    assert top == [target_real]


def test_classify_non_full_mode_never_adds_target(tmp_path):
    target = tmp_path / "target"
    (target / "__служебные_файлы").mkdir(parents=True)
    cfg = _make_cfg(tmp_path, target=str(target))
    top, nested = m.classify_found_archives([], cfg, "analyze")
    assert top == []


# ---------------------------------------------------------------------------
# run_analyze: end-to-end -- found_archive_top_level/found_archive_nested заполняются
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
    assert stats.found_archive_nested == {}


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
