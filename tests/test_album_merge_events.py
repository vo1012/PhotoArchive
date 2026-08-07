"""REVIEW-HANDOFF.md, Раунд 70 (2026-08-07) -- две находки в одном и том же коде
(`_process_record()`'s album-ветка + `build_final_summary()`), обе про `stats["album_merge_events"]`
(см. `_note_album_source()`), обе закрыты одним заходом, раз уж один и тот же код пришлось трогать:

[БЛОКЕР] `_note_album_source()` (`76344f2`) сменила форму элемента списка с пары `(album, prefix)`
на тройку `(album, prefix, is_dup)` -- `report.py` обновлён тем же коммитом, но ВТОРОЙ потребитель
того же списка, `build_final_summary()` (`photosort_win.py`), остался на распаковке двух значений --
`ValueError` на КАЖДОМ прогоне, где альбом реально собирается из ≥2 разных физических мест
источника. Ни один из существующих тестов не проводил `album_merge_events` через РЕАЛЬНЫЙ `run()`
до `build_final_summary()` -- этот файл закрывает именно этот пробел.

[ЗАМЕЧАНИЕ] Вызов `_note_album_source()` в основной append-ветке раньше шёл ДО `resolve_dest_path()`
(который считает РЕАЛЬНЫЙ `is_dup` -- собственный, независимый от `pool` механизм обнаружения дубля
по имени+хешу на диске, "identical_at_destination") -- всегда получал дефолт `is_dup=False`, даже
когда файл через несколько строк оказывался таким дублем."""
import os

import photosort_win as m
from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _stub_exiftool(monkeypatch, tags_by_path=None):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: (tags_by_path or {}).get(p, {}) for p in paths})


class TestAlbumMergeEventDoesNotCrashSummary:
    def test_real_run_with_album_merged_from_two_sources_does_not_crash(self, tmp_path, monkeypatch):
        """Раунд 70 [БЛОКЕР] -- прямое воспроизведение находки ревизора: альбом "Отпуск"
        собирается из двух разных физических мест источника (обычная папка + dump-сегмент
        "~backup", поглощаемый find_album()) -- build_final_summary() раньше падал ValueError
        на этом самом сценарии."""
        _stub_exiftool(monkeypatch)
        source = tmp_path / "source"
        (source / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "Отпуск" / "x.jpg", color=(10, 20, 30))
        (source / "~backup" / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "~backup" / "Отпуск" / "a.jpg", color=(40, 50, 60))  # different content
        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                        workdir=str(workdir))

        stats, *_ = m.run(cfg, log=lambda *a, **k: None)  # must not raise ValueError

        events = stats["album_merge_events"]
        assert len(events) == 1
        album, prefix, is_dup = events[0]  # triple, not pair -- would ValueError-unpack otherwise
        assert album == "Отпуск"
        assert is_dup is False  # genuinely new, different content -- really appended, not a dup


class TestAlbumMergeEventIsDupReflectsResolveDestPathLevelDuplicate:
    def test_note_album_source_gets_real_is_dup_not_stale_default(self, tmp_path, monkeypatch):
        """Раунд 70 [ЗАМЕЧАНИЕ] -- узкий, но реальный сценарий: index_archive() (Фаза 1)
        пропускает уже существующий в TARGET файл из-за временного OSError при чтении (тот же
        класс гонки, что и другие guard'ы в этой кодовой базе) -- pool не содержит его хеш,
        поэтому decide() не ловит дубль. Но файл физически на диске, и resolve_dest_path()
        (несколько строк ниже в той же ветке) ловит его своим независимым механизмом
        "identical_at_destination". Записанное merge-событие должно нести РЕАЛЬНЫЙ is_dup=True,
        не дефолт False, который был бы записан до фикса (вызов _note_album_source() шёл раньше
        resolve_dest_path() в коде)."""
        _stub_exiftool(monkeypatch)
        target = tmp_path / "target"
        existing = target / "Albums" / "Отпуск" / "a.jpg"
        existing.parent.mkdir(parents=True)
        _make_jpeg(existing, color=(10, 20, 30))

        # Walk order matters here: "~backup" sorts BEFORE "Отпуск" (sorted(os.listdir()), tilde
        # code point < Cyrillic) -- the "~backup" source is touched FIRST (becomes the known
        # base for the album, no merge event yet, see _note_album_source()'s "is_first_ever"),
        # "Отпуск" is touched SECOND (creates the actual merge event) -- so the duplicate file
        # has to live under "Отпуск" for its is_dup to be the one recorded in that event.
        source = tmp_path / "source"
        (source / "~backup" / "Отпуск").mkdir(parents=True)
        _make_jpeg(source / "~backup" / "Отпуск" / "x.jpg", color=(99, 88, 77))  # first source, base
        (source / "Отпуск").mkdir(parents=True)
        # Same content AND filename as the pre-existing target file -- a real duplicate, but
        # only detectable via resolve_dest_path()'s on-disk check (pool won't have it, see
        # monkeypatch below).
        _make_jpeg(source / "Отпуск" / "a.jpg", color=(10, 20, 30))

        real_sha256_file = m.sha256_file
        state = {"failed_once": False}
        existing_norm = os.path.normpath(str(existing))

        def flaky_sha256_file(path, *a, **kw):
            if not state["failed_once"] and os.path.normpath(path) == existing_norm:
                state["failed_once"] = True
                raise OSError("simulated transient read failure during Phase 1 indexing")
            return real_sha256_file(path, *a, **kw)

        monkeypatch.setattr(m, "sha256_file", flaky_sha256_file)

        workdir = tmp_path / "workdir"
        workdir.mkdir()
        cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                        workdir=str(workdir))

        stats, *_ = m.run(cfg, log=lambda *a, **k: None)

        assert state["failed_once"]  # sanity: the simulated race actually triggered
        events = stats["album_merge_events"]
        assert len(events) == 1
        album, prefix, is_dup = events[0]
        assert album == "Отпуск"
        assert is_dup is True  # resolve_dest_path()-level duplicate, not the stale False default
