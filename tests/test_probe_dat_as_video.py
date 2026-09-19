"""Речь пользователя, 2026-09-19 (после реального VHS_new-бага с .mpg): ".dat" -- расширение
слишком неспецифичное, чтобы просто добавить его в VIDEO_EXTS как остальные легаси-форматы
(кэши приложений/winmail.dat/базы данных встречаются под ним чаще, чем видео с VCD-дисков).
Вместо этого -- opt-in content-sniff через Config.probe_dat_as_video (по умолчанию выключен):
classify_source_file_type() пробует ffprobe только на ".dat" и только если флаг включён, и
только тогда, когда голое расширение уже дало "other" (все остальные типы -- нулевая
дополнительная цена, как и раньше).

ffprobe/ffmpeg реально проверены исполнением (не по докстрингу) вручную на забандленных
bin/ffmpeg.exe/bin/ffprobe.exe для .vro/.dv/.ts/.asf/.divx/.rm -- демультиплексирование и
извлечение кадра (нужно для pHash) отработали на синтетических образцах для каждого формата.
Тесты здесь не спавнят реальный ffprobe (мокают m._dat_has_video_stream/m.ffprobe_json) -- то
же соглашение, что и у остального этого файла (см. test_video_cache_hit_never_calls_ffmpeg в
test_analyze_batch.py)."""
import photosort_win as m


def _walk(tmp_path, probe_dat_as_video=False):
    (tmp_path / "target").mkdir(exist_ok=True)
    cfg = m.Config(source=str(tmp_path / "source"), target=str(tmp_path / "target"),
                    probe_dat_as_video=probe_dat_as_video)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    return list(walker.walk())


class TestVideoExtsCoverage:
    def test_new_legacy_formats_present(self):
        for ext in ("mpg", "mpeg", "vro", "dv", "ts", "asf", "divx", "rm", "rmvb"):
            assert ext in m.VIDEO_EXTS, ext

    def test_dat_deliberately_not_in_plain_extension_set(self):
        # "dat" МУСТ оставаться opt-in (probe_dat_as_video), не обычным расширением -- слишком
        # неспецифично (см. докстрин VIDEO_EXTS/классификация "PROBE-DAT" в RULES.md).
        assert "dat" not in m.VIDEO_EXTS
        assert m.file_type("clip.dat") == "other"


class TestProbeDatDisabledByDefault:
    def test_dat_not_copied_when_flag_off(self, tmp_path, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("probe_dat_as_video=False must never touch ffprobe")

        monkeypatch.setattr(m, "_dat_has_video_stream", _boom)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "clip.dat").write_bytes(b"not a real container -- must not even be probed")

        items = _walk(tmp_path, probe_dat_as_video=False)
        assert items == [], [it.origin_display for it in items]


class TestProbeDatEnabled:
    def test_dat_copied_as_video_when_stream_detected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_dat_has_video_stream", lambda path: True)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "vcd_clip.dat").write_bytes(b"fake mpeg-ps bytes")

        items = _walk(tmp_path, probe_dat_as_video=True)
        assert len(items) == 1
        assert items[0].ftype == "video"
        assert items[0].origin_display.endswith("vcd_clip.dat")

    def test_dat_still_skipped_when_no_stream_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "_dat_has_video_stream", lambda path: False)

        src = tmp_path / "source"
        src.mkdir(parents=True)
        (src / "browser_cache.dat").write_bytes(b"definitely not video")

        items = _walk(tmp_path, probe_dat_as_video=True)
        assert items == [], [it.origin_display for it in items]

    def test_classify_source_file_type_unit(self, tmp_path, monkeypatch):
        cfg_on = m.Config(source=str(tmp_path / "S"), target=str(tmp_path / "T"),
                           probe_dat_as_video=True)
        cfg_off = m.Config(source=str(tmp_path / "S"), target=str(tmp_path / "T"),
                            probe_dat_as_video=False)

        monkeypatch.setattr(m, "_dat_has_video_stream", lambda path: True)
        assert m.classify_source_file_type("x.dat", cfg_on) == "video"
        assert m.classify_source_file_type("x.dat", cfg_off) == "other"
        # non-"dat" extensions never reach the probe at all, regardless of the flag
        assert m.classify_source_file_type("photo.jpg", cfg_on) == "image"


class TestDatFfprobeContentSniff:
    """_dat_has_video_stream() сама -- тонкая обёртка над ffprobe_json(), мокаем на этом уровне
    (не спавним реальный ffprobe), чтобы проверить и позитивный, и отрицательный разбор JSON."""

    def test_recognizes_valid_video_stream(self, monkeypatch):
        monkeypatch.setattr(m, "ffprobe_json", lambda path: {
            "streams": [{"codec_type": "video", "width": 320, "height": 240}]
        })
        assert m._dat_has_video_stream("clip.dat") is True

    def test_rejects_audio_only_or_no_streams(self, monkeypatch):
        monkeypatch.setattr(m, "ffprobe_json", lambda path: {
            "streams": [{"codec_type": "audio"}]
        })
        assert m._dat_has_video_stream("clip.dat") is False

        monkeypatch.setattr(m, "ffprobe_json", lambda path: {})
        assert m._dat_has_video_stream("clip.dat") is False

    def test_rejects_video_stream_without_dimensions(self, monkeypatch):
        # ffprobe couldn't determine width/height -- not a confident enough signal to trust an
        # ambiguous ".dat" as real video (same bar file_type() gets for free via extension).
        monkeypatch.setattr(m, "ffprobe_json", lambda path: {
            "streams": [{"codec_type": "video"}]
        })
        assert m._dat_has_video_stream("clip.dat") is False


class TestArchiveReindexRecognizesDatWithoutReprobing(object):
    """index_archive() (переиндексация TARGET на следующем прогоне) -- .dat, уже лежащий в
    архиве, безусловно video, БЕЗ повторного вызова ffprobe: попасть туда он мог только через
    classify_source_file_type() (проба уже пройдена один раз при копировании)."""

    def test_walk_media_files_treats_existing_dat_as_video(self, tmp_path, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("re-indexing an already-archived .dat must not re-probe it")

        monkeypatch.setattr(m, "ffprobe_json", _boom)

        root = tmp_path / "Albums" / "Отпуск"
        root.mkdir(parents=True)
        (root / "old_vcd.dat").write_bytes(b"already-confirmed video, no reason to re-probe")

        found = list(m._walk_media_files(str(tmp_path)))
        assert len(found) == 1
        path, ftype = found[0]
        assert ftype == "video"
        assert path.endswith("old_vcd.dat")
