"""Задача 0/B (SESSION-HANDOFF.txt, "проактивные советы для [2] Пробный прогон"):
stats["album_profiles"] -- структурный профиль каждого альбома (сколько файлов, разброс по
годам/камерам/дата-подпапкам), собираемый в _process_record() безусловно для любого альбома,
и мультиисточниковое слияние через _sum_stats() (числа сложить, множества объединить -- не
плоский {альбом: число}, как остальные dict-поля stats). report.py's эвристика (какой профиль
считать "похожим на облачную синхронизацию") протестирована отдельно в tests/test_report.py --
здесь только сама агрегация/слияние, не пороги/рендер."""
import photosort_win as m
import report as r
from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _exif(dt=None, make=None, model=None):
    tags = {}
    if dt:
        tags["DateTimeOriginal"] = dt
    if make:
        tags["Make"] = make
    if model:
        tags["Model"] = model
    return tags


def _stub_exiftool(monkeypatch, tags_by_path):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: tags_by_path.get(p, {}) for p in paths})


class TestAlbumProfilesSingleSource:
    def test_profile_fields_from_one_album(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        album = source / "Отпуск"
        (album / "2020-05-01").mkdir(parents=True)
        a = album / "a.jpg"
        b = album / "b.jpg"
        c = album / "2020-05-01" / "c.jpg"
        # Разные пиксели -- разный SHA-256 -- без этого b/c дедуплицировались бы как точные
        # повторы a.jpg (skipped_present), не попадая в album_profiles вовсе (аггрегация
        # только в ветке "реально дописан", см. _process_record()).
        _make_jpeg(a, color=(10, 20, 30))
        _make_jpeg(b, color=(40, 50, 60))
        _make_jpeg(c, color=(70, 80, 90))

        _stub_exiftool(monkeypatch, {
            str(a): _exif(dt="2015:06:01 10:00:00", make="Canon", model="EOS R5"),
            str(b): _exif(dt="2020:07:02 10:00:00", make="Nikon", model="D850"),
            str(c): _exif(dt="2020:07:03 10:00:00", make="Canon", model="EOS R5"),
        })

        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        cfg = m.Config(source=str(source), target=str(target), dry_run=True, sample_limit=0,
                        workdir=str(workdir), suppress_logs=True)
        stats, *_ = m.run(cfg, log=lambda *a, **k: None)

        profiles = stats["album_profiles"]
        assert len(profiles) == 1
        profile = next(iter(profiles.values()))
        assert profile["name"] == "Отпуск"
        assert profile["n"] == 3
        assert profile["years"] == {2015, 2020}
        assert profile["cameras"] == {"Canon EOS R5", "Nikon D850"}
        assert profile["date_subdirs"] == {"2020-05-01"}

    def test_no_album_profiles_key_when_source_has_no_albums(self, tmp_path, monkeypatch):
        """Файлы прямо в корне SOURCE -- альбома нет вовсе, album_profiles либо отсутствует,
        либо пуст, не падает."""
        source = tmp_path / "source"
        source.mkdir()
        _make_jpeg(source / "a.jpg")
        _stub_exiftool(monkeypatch, {})

        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        cfg = m.Config(source=str(source), target=str(target), dry_run=True, sample_limit=0,
                        workdir=str(workdir), suppress_logs=True)
        stats, *_ = m.run(cfg, log=lambda *a, **k: None)

        assert not stats.get("album_profiles")


class TestAlbumProfilesEndToEndIntoReport:
    def test_real_pipeline_output_feeds_report_advisory(self, tmp_path, monkeypatch):
        """Закрывает петлю между двумя половинами, протестированными по отдельности: реальная
        сборка (photosort_win.py) -> реальный report.html (report.py). 35 файлов (>=
        REC_MIN_FILES), 6 разных лет + 4 разные камеры (>=2 из 4 структурных признаков) + имя
        "Google Photos" (совпадает с CLOUDLIKE_ALBUM_HINTS) -- флаг должен сработать по-
        настоящему, не только на синтетических run_stats, собранных вручную."""
        source = tmp_path / "source"
        album = source / "Google Photos"
        album.mkdir(parents=True)
        tags_by_path = {}
        for i in range(35):
            p = album / f"p{i}.jpg"
            _make_jpeg(p, color=(i % 255, (i * 3) % 255, (i * 7) % 255))
            year = 2015 + (i % 6)
            tags_by_path[str(p)] = _exif(dt=f"{year}:06:01 10:00:00",
                                          make=f"Make{i % 4}", model=f"Model{i % 4}")
        _stub_exiftool(monkeypatch, tags_by_path)

        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        cfg = m.Config(source=str(source), target=str(target), dry_run=True, sample_limit=0,
                        workdir=str(workdir), suppress_logs=True)
        stats, *_ = m.run(cfg, log=lambda *a, **k: None)

        out_path = tmp_path / "report.html"
        r.generate_report({}, str(out_path), level="workdir", run_stats=stats)
        html_out = out_path.read_text(encoding="utf-8")
        assert "похож на папку облачной синхронизации" in html_out
        assert "~Google Photos" in html_out


class TestSumStatsMergesAlbumProfiles:
    def test_two_sources_same_album_merges_counts_and_sets(self, tmp_path, monkeypatch):
        """Обязательный тест на 2+ источниках (SESSION-HANDOFF.txt, Задача 0): один и тот же
        альбом (по album_prefix) встречается в двух разных SOURCE-прогонах -- _sum_stats()
        должен сложить n и объединить years/cameras/date_subdirs, не перезаписать/уронить."""
        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        source1 = tmp_path / "source1"
        album1 = source1 / "Отпуск"
        album1.mkdir(parents=True)
        a1 = album1 / "a.jpg"
        _make_jpeg(a1)
        _stub_exiftool(monkeypatch, {str(a1): _exif(dt="2015:06:01 10:00:00", make="Canon", model="EOS R5")})
        cfg1 = m.Config(source=str(source1), target=str(target), dry_run=True, sample_limit=0,
                         workdir=str(workdir), suppress_logs=True)
        stats1, *_ = m.run(cfg1, log=lambda *a, **k: None)

        source2 = tmp_path / "source2"
        album2 = source2 / "Отпуск"
        album2.mkdir(parents=True)
        b2 = album2 / "b.jpg"
        _make_jpeg(b2)
        _stub_exiftool(monkeypatch, {str(b2): _exif(dt="2020:07:02 10:00:00", make="Nikon", model="D850")})
        cfg2 = m.Config(source=str(source2), target=str(target), dry_run=True, sample_limit=0,
                         workdir=str(workdir), suppress_logs=True)
        stats2, *_ = m.run(cfg2, log=lambda *a, **k: None)

        merged = m._sum_stats([stats1, stats2])
        profiles = merged["album_profiles"]
        assert len(profiles) == 1
        profile = next(iter(profiles.values()))
        assert profile["name"] == "Отпуск"
        assert profile["n"] == 2
        assert profile["years"] == {2015, 2020}
        assert profile["cameras"] == {"Canon EOS R5", "Nikon D850"}

    def test_two_sources_different_albums_kept_separate(self, tmp_path, monkeypatch):
        target = tmp_path / "target"
        target.mkdir()
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        source1 = tmp_path / "source1"
        album1 = source1 / "Отпуск"
        album1.mkdir(parents=True)
        a1 = album1 / "a.jpg"
        _make_jpeg(a1)
        _stub_exiftool(monkeypatch, {str(a1): _exif(dt="2015:06:01 10:00:00")})
        cfg1 = m.Config(source=str(source1), target=str(target), dry_run=True, sample_limit=0,
                         workdir=str(workdir), suppress_logs=True)
        stats1, *_ = m.run(cfg1, log=lambda *a, **k: None)

        source2 = tmp_path / "source2"
        album2 = source2 / "Свадьба"
        album2.mkdir(parents=True)
        b2 = album2 / "b.jpg"
        _make_jpeg(b2)
        _stub_exiftool(monkeypatch, {str(b2): _exif(dt="2020:07:02 10:00:00")})
        cfg2 = m.Config(source=str(source2), target=str(target), dry_run=True, sample_limit=0,
                         workdir=str(workdir), suppress_logs=True)
        stats2, *_ = m.run(cfg2, log=lambda *a, **k: None)

        merged = m._sum_stats([stats1, stats2])
        names = {p["name"] for p in merged["album_profiles"].values()}
        assert names == {"Отпуск", "Свадьба"}
        assert all(p["n"] == 1 for p in merged["album_profiles"].values())
