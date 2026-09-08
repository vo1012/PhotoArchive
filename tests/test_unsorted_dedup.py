"""Находка A (SESSION-HANDOFF.txt, аудит 2026-09-03; в работу по прямой команде пользователя
2026-09-07): дедуп _Unsorted был слабее пула сборки.

`resolve_dest_path()` сверяет занятость имени только в ОДНОЙ зеркальной подпапке, а _Unsorted
в основной пул дедупа не входит вовсе -> один и тот же битый/иконочный файл, встреченный в
источнике по двум путям, давал две копии; а файл, чей контент уже нормально лежит в
Albums/ByDate/RAW, всё равно копировался в _Unsorted (медиафайл при флуктуации broken-
классификации оказывался И в разделе, И в _Unsorted -- отсюда ложные «точные дубли» в
Паспорте, находка B).

Фикс: перед укладкой в _Unsorted -- `_content_already_kept()` (основной пул + отдельный
ленивый индекс `_disputed_sha_index()` уже лежащего в _Unsorted). Обычная укладка в
ByDate/Albums этот индекс НЕ смотрит -- _Unsorted карантин, реальному архиву не мешает.

Все тесты red-before-green: до фикса _Unsorted получал вторую копию.
"""
import os
import shutil

import pytest
from PIL import Image

import photosort_win as m


@pytest.fixture(autouse=True)
def _not_a_noisy_zone(monkeypatch):
    # pytest's tmp_path лежит под сегментом "tmp" -> classify_zone() == "noisy", а в шумной
    # зоне `not rec.is_media` уходит в rejected_noise.csv, НЕ в _Unsorted (эти тесты не про
    # зоны доверия -- фиксируем "normal").
    monkeypatch.setattr(m, "classify_zone", lambda _p: "normal")


def _make_jpeg(path, size=(900, 700), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _make_tiny_png(path, color=(1, 2, 3)):
    # max side < 256 -> classify_image() -> (False, "tiny_image") -> ветка `not rec.is_media`
    Image.new("RGB", (64, 64), color).save(path, "PNG")


def _make_broken_jpeg(path):
    # расширение .jpg, но содержимое не разбирается -> rec.broken=True
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 4)


def _cfg(tmp_path, **over):
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


def _unsorted_files(cfg, suffix):
    return [os.path.join(r, f)
            for r, _d, fs in os.walk(cfg.dispute)
            for f in fs if f.lower().endswith(suffix)]


def _archive_media(cfg, suffix):
    """Файлы под разделами архива (Albums/ByDate/RAW), НЕ под _Unsorted и не служебные."""
    out = []
    for root in (cfg.albums_root, cfg.bydate_root, cfg.raw_root):
        for r, _d, fs in os.walk(root):
            out += [os.path.join(r, f) for f in fs if f.lower().endswith(suffix)]
    return out


class TestDedupWithinUnsorted:
    def test_tiny_image_duplicated_across_source_subpaths_lands_once(self, tmp_path):
        """A1: байт-идентичная иконка в двух подпапках источника -> раньше две копии
        (_Unsorted/x.png и _Unsorted/sub/x.png -- разные dest_dir, resolve_dest_path() второй
        не видел первую). Теперь -- одна, вторая уходит в skipped.csv как identical_in_unsorted."""
        source, cfg = _cfg(tmp_path)
        _make_tiny_png(source / "x.png")
        (source / "sub").mkdir()
        shutil.copyfile(source / "x.png", source / "sub" / "x.png")

        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        assert len(_unsorted_files(cfg, ".png")) == 1

        with open(os.path.join(cfg.logs, "skipped.csv"), encoding="utf-8") as f:
            skipped_text = f.read()
        assert "identical_in_unsorted" in skipped_text

    def test_non_identical_tiny_images_both_kept(self, tmp_path):
        """Граница: РАЗНЫЕ иконки (разный контент) -- обе остаются в _Unsorted, дедуп не
        ложно-срабатывает по одному лишь имени."""
        source, cfg = _cfg(tmp_path)
        _make_tiny_png(source / "x.png", color=(1, 2, 3))
        (source / "sub").mkdir()
        _make_tiny_png(source / "sub" / "x.png", color=(200, 100, 50))

        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        assert len(_unsorted_files(cfg, ".png")) == 2


class TestDoNotQuarantineAlreadyArchived:
    def test_processing_error_twin_of_an_archived_photo_is_not_copied_to_unsorted(self, tmp_path):
        """A2: два байт-идентичных настоящих фото. Первое проходит нормально -> ByDate + pool.
        Второе роняет _process_record() (симуляция бага разбора / флуктуации) -> путь
        _dispute_processing_error(). Раньше: копия в _Unsorted (контент в архиве ДВАЖДЫ).
        Теперь: _content_already_kept() видит pool-совпадение -> не копируем, skipped.csv:
        identical_in_archive."""
        source, cfg = _cfg(tmp_path)
        _make_jpeg(source / "a.jpg", color=(40, 50, 60))
        shutil.copyfile(source / "a.jpg", source / "a_copy.jpg")  # a.jpg сортируется раньше

        real = m._process_record

        def _flaky(rec, st, log=print):
            if rec.item.rel_path == "a_copy.jpg":
                raise RuntimeError("simulated decide() bug on the twin")
            return real(rec, st, log=log)

        m._process_record = _flaky
        try:
            m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)
        finally:
            m._process_record = real

        assert len(_archive_media(cfg, ".jpg")) == 1          # ровно один в разделе архива
        assert _unsorted_files(cfg, ".jpg") == []             # НЕ продублирован в _Unsorted

        with open(os.path.join(cfg.logs, "skipped.csv"), encoding="utf-8") as f:
            assert "identical_in_archive" in f.read()

    def test_good_copy_is_still_archived_when_its_broken_twin_sits_in_unsorted(self, tmp_path):
        """Граница по модели пользователя: _Unsorted -- невидимый карантин, он НЕ блокирует
        нормальную укладку хорошей копии в ByDate. Битая копия обрабатывается ПЕРВОЙ (уходит
        в _Unsorted), хорошая -- второй: она обязана попасть в раздел архива, а не быть
        пропущенной как «дубль спорного»."""
        source, cfg = _cfg(tmp_path)
        _make_jpeg(source / "z_good.jpg", color=(40, 50, 60))              # хорошая
        shutil.copyfile(source / "z_good.jpg", source / "a_broken.jpg")    # a_ обрабатывается ПЕРВОЙ

        real = m._process_record

        def _flaky(rec, st, log=print):
            if rec.item.rel_path == "a_broken.jpg":
                raise RuntimeError("simulated flaky decode on the first-seen twin")
            return real(rec, st, log=log)

        m._process_record = _flaky
        try:
            m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)
        finally:
            m._process_record = real

        assert len(_archive_media(cfg, ".jpg")) == 1     # хорошая копия реально в архиве
        assert len(_unsorted_files(cfg, ".jpg")) == 1    # битая -- в карантине, обе живы


class TestEmptyDisputeDirCleanup:
    """Живая находка пользователя (2026-09-07): в _Unsorted встречается глубокая вложенность,
    заканчивающаяся ПУСТОЙ папкой. atomic_copy() создаёт всю цепочку папок ДО копирования,
    поэтому неудавшееся размещение спорного файла оставляет пустой каркас. Чистится в конце
    реальной сборки (_prune_empty_dispute_dirs), os.rmdir не трогает ничего с содержимым."""

    def test_empty_mirror_chain_from_failed_placement_is_pruned(self, tmp_path, monkeypatch):
        source, cfg = _cfg(tmp_path)
        deep = source / "deep" / "nested" / "path"
        deep.mkdir(parents=True)
        _make_jpeg(deep / "ok.jpg", color=(1, 2, 3))          # -> ByDate, нормально
        _make_tiny_png(deep / "icon.png")                     # -> _Unsorted, размещение упадёт

        real_atomic = m.atomic_copy

        def _atomic(src, dst, *a, **kw):
            m._makedirs_iterative(m.winlong(os.path.dirname(dst)))  # как настоящая atomic_copy
            if dst.lower().endswith(".png"):
                raise OSError("simulated placement failure after mkdir")
            return real_atomic(src, dst, *a, **kw)

        monkeypatch.setattr(m, "atomic_copy", _atomic)
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        # каркас _Unsorted/deep/nested/path/ создан atomic_copy, файл не лёг -> подчищен
        assert not os.path.isdir(os.path.join(cfg.dispute, "deep"))
        assert os.path.isdir(cfg.dispute)  # сам _Unsorted/ на месте

    def test_user_added_content_and_populated_dirs_survive(self, tmp_path):
        source, cfg = _cfg(tmp_path)
        _make_jpeg(source / "photo.jpg")
        # пользователь вручную положил в _Unsorted нормальный файл при разборе
        keep_dir = os.path.join(cfg.dispute, "мои находки")
        os.makedirs(keep_dir)
        with open(os.path.join(keep_dir, "keep.jpg"), "wb") as f:
            f.write(b"user rescued this")
        # + исторический пустой каркас (старая версия / прошлый сбой)
        os.makedirs(os.path.join(cfg.dispute, "старый каркас", "вложенный"))

        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        assert os.path.isfile(os.path.join(keep_dir, "keep.jpg"))          # ручной файл цел
        assert not os.path.isdir(os.path.join(cfg.dispute, "старый каркас"))  # пустой убран


class TestPassportUnsortedAlreadyArchived:
    """п.3 (2026-09-07): в Паспорте (self_scan) файл в _Unsorted, байт-идентичный копии в
    реальном разделе, -- НЕ тревога «дубль внутри архива» (n_exact_dupes/exact_dup_edges), а
    мягкая подсказка на уборку (n_unsorted_already_archived). _Unsorted -- карантин отбраковки
    вне дедуп-базы."""

    def test_unsorted_copy_of_archived_file_routes_to_hint_not_exact_dup(self, tmp_path):
        target = tmp_path / "MyArchive"
        (target / "__служебные_файлы").mkdir(parents=True)
        album = target / "Albums" / "Отпуск"
        album.mkdir(parents=True)
        _make_jpeg(album / "beach.jpg", color=(20, 40, 60))
        us = target / "_Unsorted" / "с телефона"
        us.mkdir(parents=True)
        shutil.copyfile(album / "beach.jpg", us / "beach.jpg")  # Albums сортируется раньше _Unsorted
        workdir = tmp_path / "appdir"
        workdir.mkdir()

        cfg = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                       workdir=str(workdir))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=True)

        assert stats.n_unsorted_already_archived == 1
        assert stats.n_exact_dupes == 0
        assert stats.exact_dup_edges == []

    def test_regular_archive_internal_dup_still_flagged_as_exact(self, tmp_path):
        """Граница: настоящий дубль ВНУТРИ реальных разделов (не _Unsorted) по-прежнему
        n_exact_dupes -- п.3 не глушит настоящую находку целостности."""
        target = tmp_path / "MyArchive"
        (target / "__служебные_файлы").mkdir(parents=True)
        (target / "Albums" / "A").mkdir(parents=True)
        (target / "Albums" / "B").mkdir(parents=True)
        _make_jpeg(target / "Albums" / "A" / "x.jpg", color=(20, 40, 60))
        shutil.copyfile(target / "Albums" / "A" / "x.jpg", target / "Albums" / "B" / "x.jpg")
        workdir = tmp_path / "appdir"
        workdir.mkdir()

        cfg = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                       workdir=str(workdir))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=True)

        assert stats.n_exact_dupes == 1
        assert stats.n_unsorted_already_archived == 0

    def test_integrity_card_renders_cleanup_hint_not_dup_alarm(self):
        import report
        stats = m.AnalyzeStats(mode="analyze")
        stats.n_unsorted_already_archived = 3
        html = report._render_passport_integrity(stats)
        assert "можно удалить" in html
        assert "Дублей внутри архива нет" in html  # тревожная секция остаётся чистой

    def test_integrity_card_has_no_hint_line_when_nothing_to_clean(self):
        import report
        html = report._render_passport_integrity(m.AnalyzeStats(mode="analyze"))
        assert "можно удалить" not in html

    def test_counts_files_not_distinct_shas(self, tmp_path):
        """Раунд 216 придирка 1: три БАЙТ-ИДЕНТИЧНЫЕ копии одного архивного фото в _Unsorted --
        «удалить можно 3», не 1 (раньше считались различные sha -> 1)."""
        target = tmp_path / "MyArchive"
        (target / "__служебные_файлы").mkdir(parents=True)
        album = target / "Albums" / "Отпуск"
        album.mkdir(parents=True)
        _make_jpeg(album / "beach.jpg", color=(20, 40, 60))
        us = target / "_Unsorted" / "с телефона"
        us.mkdir(parents=True)
        for i in range(3):
            shutil.copyfile(album / "beach.jpg", us / f"beach_{i}.jpg")
        workdir = tmp_path / "appdir"
        workdir.mkdir()

        cfg = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                       workdir=str(workdir))
        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=True)

        assert stats.n_unsorted_already_archived == 3
        assert stats.n_exact_dupes == 0


class TestUnsortedDedupPridirkiRound216:
    def test_zero_byte_files_are_not_deduped_stay_visible(self, tmp_path):
        """Раунд 216 придирка 3: 0-байтные файлы НЕ дедупятся между собой -- каждый виден в
        _Unsorted под своим путём (видимость «у вас N обрезанных до нуля файлов» важнее, данных
        в них всё равно нет)."""
        source, cfg = _cfg(tmp_path)
        (source / "a").mkdir()
        (source / "b").mkdir()
        (source / "a" / "broken.jpg").write_bytes(b"")
        (source / "b" / "broken.jpg").write_bytes(b"")  # тот же (пустой) контент, другой путь

        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        # оба 0-байтных файла -- в _Unsorted, ни один не «схлопнут» как identical
        assert len(_unsorted_files(cfg, ".jpg")) == 2
        skipped_path = os.path.join(cfg.logs, "skipped.csv")
        skipped = open(skipped_path, encoding="utf-8").read() if os.path.isfile(skipped_path) else ""
        assert "identical_in_unsorted" not in skipped

    def test_skipped_csv_matched_column_is_full_path_for_both_branches(self, tmp_path):
        """Раунд 216 придирка 2: колонка "matched" в skipped.csv -- полный путь и в ветке
        archive, и в ветке unsorted (раньше unsorted логировал relpath внутри _Unsorted)."""
        source, cfg = _cfg(tmp_path)
        # два байт-идентичных иконочных файла в разных подпапках -> вторая -> identical_in_unsorted
        _make_tiny_png(source / "x.png")
        (source / "sub").mkdir()
        shutil.copyfile(source / "x.png", source / "sub" / "x.png")

        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        with open(os.path.join(cfg.logs, "skipped.csv"), encoding="utf-8") as f:
            rows = [ln for ln in f.read().splitlines() if "identical_in_unsorted" in ln]
        assert rows, "ожидалась строка identical_in_unsorted"
        # "matched" содержит абсолютный путь к файлу в _Unsorted, не голый relpath
        assert str(cfg.dispute) in rows[0] or os.path.normcase(str(cfg.dispute)) in os.path.normcase(rows[0])


def _passport_cfg(tmp_path):
    target = tmp_path / "MyArchive"
    (target / "__служебные_файлы").mkdir(parents=True)
    workdir = tmp_path / "appdir"
    workdir.mkdir()
    cfg = m.Config(source=str(target), target=m._NO_TARGET_PLACEHOLDER, sample_limit=0,
                   workdir=str(workdir))
    return target, cfg


class TestPassportUnsortedFullExclusion:
    """Полный п.3 (2026-09-08, прямая команда пользователя): _Unsorted в Паспорте (self_scan)
    исключён из КАЖДОГО счётчика, описывающего архив (не только из дедуп-базы, как мини-п.3).
    Единственный след — n_unsorted_files + отдельная строка в «Архив сейчас».

    Red-before-green: до фикса декодируемый файл _Unsorted шёл в n_images/dates_by_year/
    tier_counts/format_counts/n_images_available, а битый — в n_broken_or_zero."""

    def _run(self, cfg):
        return m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=True)

    def test_decodable_unsorted_photo_out_of_every_archive_counter(self, tmp_path):
        target, cfg = _passport_cfg(tmp_path)
        album = target / "Albums" / "Отпуск"
        album.mkdir(parents=True)
        _make_jpeg(album / "beach.jpg", color=(20, 40, 60))
        us = target / "_Unsorted" / "с телефона"
        us.mkdir(parents=True)
        _make_jpeg(us / "phone_shot.jpg", color=(90, 10, 10))  # ДРУГОЙ снимок, не копия архивного
        os.utime(album / "beach.jpg", (1_500_000_000, 1_500_000_000))   # ~2017
        os.utime(us / "phone_shot.jpg", (900_000_000, 900_000_000))     # ~1998 -- отдельный год

        stats = self._run(cfg)

        assert stats.n_unsorted_files == 1
        assert stats.n_unsorted_bytes > 0
        # ничего от _Unsorted-файла в архив-описывающих счётчиках
        assert stats.total_files == 1
        assert stats.n_images == 1
        assert stats.n_images_available == 1
        assert sum(stats.format_counts_image.values()) == 1
        assert sum(stats.tier_counts.values()) == 1
        assert sum(stats.tier_counts_no_raw.values()) == 1
        assert 1998 not in stats.dates_by_year          # год _Unsorted-снимка не просочился
        assert stats.dates_by_year.get(2017, 0) == 1
        assert stats.oldest_date is not None and stats.oldest_date.year == 2017
        assert sum(stats.tree_folder_counts.values()) == 1
        assert stats.n_broken_or_zero == 0
        assert stats.disputed_paths == []

    def test_broken_and_zero_unsorted_files_not_archive_broken(self, tmp_path):
        target, cfg = _passport_cfg(tmp_path)
        bydate = target / "ByDate" / "2019-05"
        bydate.mkdir(parents=True)
        _make_jpeg(bydate / "ok.jpg", color=(20, 40, 60))
        (bydate / "corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"junk" * 8)  # реальный битый В архиве
        us = target / "_Unsorted" / "хлам"
        us.mkdir(parents=True)
        _make_broken_jpeg(us / "broken.jpg")
        (us / "empty.jpg").write_bytes(b"")
        _make_tiny_png(us / "icon.png")

        stats = self._run(cfg)

        assert stats.n_unsorted_files == 3          # broken + empty + icon
        assert stats.n_broken_or_zero == 1          # ТОЛЬКО corrupt.jpg из ByDate
        assert stats.total_files == 2               # ok.jpg + corrupt.jpg
        # единственный «повреждён» архива -- реальный corrupt.jpg в ByDate, ни одного из _Unsorted
        assert len(stats.disputed_paths) == 1 and "corrupt.jpg" in stats.disputed_paths[0]
        assert stats.unreadable_paths == []

    def test_already_archived_hint_survives_full_exclusion(self, tmp_path):
        target, cfg = _passport_cfg(tmp_path)
        album = target / "Albums" / "Отпуск"
        album.mkdir(parents=True)
        _make_jpeg(album / "beach.jpg", color=(20, 40, 60))
        us = target / "_Unsorted" / "с телефона"
        us.mkdir(parents=True)
        shutil.copyfile(album / "beach.jpg", us / "beach.jpg")  # байт-копия архивного

        stats = self._run(cfg)

        assert stats.n_unsorted_files == 1
        assert stats.n_unsorted_already_archived == 1   # подсказка «можно удалить» цела
        assert stats.n_exact_dupes == 0                 # не тревога «дубль внутри архива»
        assert stats.n_images == 1                      # не 2 -- _Unsorted-копия не в счёте

    def test_regular_analyze_not_selfscan_still_counts_unsorted(self, tmp_path):
        """Граница: обычный analyze (self_scan=False) НЕ различает _Unsorted -- «показывает,
        что на диске». n_unsorted_files остаётся 0, файлы считаются как обычно."""
        src = tmp_path / "somefolder"
        (src / "_Unsorted").mkdir(parents=True)
        _make_jpeg(src / "_Unsorted" / "a.jpg", color=(1, 2, 3))
        _make_jpeg(src / "b.jpg", color=(4, 5, 6))
        workdir = tmp_path / "appdir"
        workdir.mkdir()
        cfg = m.Config(source=str(src), target=str(tmp_path / "T"), sample_limit=0,
                       workdir=str(workdir))

        stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None, self_scan=False)

        assert stats.n_unsorted_files == 0
        assert stats.n_images == 2

    def test_summary_line_renders_and_hides(self):
        import report
        stats = m.AnalyzeStats(mode="analyze")
        stats.total_files = 10
        assert "_Unsorted" not in report._render_passport_summary(stats)

        stats.n_unsorted_files = 4
        stats.n_unsorted_bytes = 2_000_000
        stats.n_images = 8
        stats.n_videos = 2
        html = report._render_passport_summary(stats)
        assert "Отдельно, в папке" in html and "_Unsorted</code>" in html
        assert "4 файла" in html and "2 МБ" in html
        assert "карантин отбраковки" in html
        # 220-1: недатированные/погранично-неуверенные фото НЕ в _Unsorted -- не упоминаем их
        assert "без уверенной привязки" not in html

    def test_summary_line_reads_when_media_breakdown_empty(self):
        """220-2: архив, где всё медиа ещё в _Unsorted (n_images/raw/videos == 0 -> разбивка
        по типу медиа не выводится). Строка _Unsorted не должна начинаться с висящего «них»."""
        import report
        stats = m.AnalyzeStats(mode="analyze")
        stats.total_files = 0
        stats.n_unsorted_files = 12
        stats.n_unsorted_bytes = 5_000_000
        html = report._render_passport_summary(stats)
        assert "Отдельно, в папке" in html
        assert "Кроме них" not in html  # антецедента нет -- не используем это слово
