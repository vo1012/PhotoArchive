"""analyze_batch() -- exact-dup phash short-circuit (review round 4 finding,
REVIEW-HANDOFF.md): decide() always checks pool.find_exact(sha256) before ever reading
rec.phash/rec.aspect for image/video (see TestDecide in test_pool_dedup.py), so a file that's
already an exact duplicate of something in the pool shouldn't pay for the expensive phash
decode (image_phash_and_size: full PIL decode + DCT; video_phash_3frames: three ffmpeg spawns).
exiftool_batch()/ffprobe-backed helpers are stubbed out here -- this suite is about the
phash short-circuit, not about exercising the real bundled binaries."""
import hashlib

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _item(path, ftype="image", **kwargs):
    st = path.stat()
    kwargs.setdefault("read_path", str(path))
    kwargs.setdefault("origin_display", path.name)
    kwargs.setdefault("rel_path", path.name)
    kwargs.setdefault("size", st.st_size)
    kwargs.setdefault("mtime", st.st_mtime)
    kwargs.setdefault("ftype", ftype)
    return m.SourceItem(**kwargs)


@pytest.fixture(autouse=True)
def _no_exiftool(monkeypatch):
    # analyze_batch always calls exiftool_batch() first, regardless of dup status -- not
    # part of this finding, stub it out so tests don't need the real bundled binary.
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})


class TestImageExactDupSkipsPhash:
    def test_no_pool_computes_phash_as_before(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        recs = m.analyze_batch([_item(img)])

        assert len(calls) == 1
        assert recs[0].phash is not None

    def test_pool_miss_still_computes_phash(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256="f" * 64, ftype="image", dest_path="other.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)

        assert len(calls) == 1
        assert recs[0].phash is not None

    def test_exact_dup_in_pool_skips_phash(self, tmp_path, monkeypatch):
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        sha = hashlib.sha256(img.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="image", dest_path="existing.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)
        rec = recs[0]

        assert calls == []  # the expensive decode was never called
        assert rec.phash is None
        assert rec.sha256 == sha
        # is_media/classify_image must still see real width/height from the cheap
        # image_size_only() path -- decide() gates on rec.is_media BEFORE the ftype branches
        # that check for an exact dup, so this can't be starved by skipping phash.
        assert rec.is_media is True
        assert (rec.width, rec.height) == (800, 600)

    def test_exact_dup_decide_result_unaffected_by_skipped_phash(self, tmp_path):
        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        sha = hashlib.sha256(img.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="image", dest_path="existing.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool)
        decision = m.decide(pool, recs[0])

        assert decision.decision == "skipped_present"
        assert decision.matched_dest == "existing.jpg"

    def test_skip_hash_with_pool_given_never_marks_exact_dup(self, tmp_path, monkeypatch):
        # skip_hash=True (analyze-quick) never computes sha256 -- exact_dup must stay False
        # even if a pool happens to be passed, not crash on rec.sha256 being None.
        calls = []
        real = m.image_phash_and_size
        monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

        img = tmp_path / "a.jpg"
        _make_jpeg(img)
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256="0" * 64, ftype="image", dest_path="other.jpg", size=1))

        recs = m.analyze_batch([_item(img)], pool=pool, skip_hash=True)

        assert calls == []  # skip_hash's own cheap path, not the exact-dup one
        assert recs[0].sha256 is None
        assert recs[0].phash is None
        assert (recs[0].width, recs[0].height) == (800, 600)


class TestVideoExactDupSkipsPhash:
    def _video_item(self, tmp_path, ftype="video"):
        vid = tmp_path / "a.mp4"
        vid.write_bytes(b"not a real video, duration/phash are stubbed below")
        return vid, _item(vid, ftype=ftype)

    def test_pool_miss_still_computes_phash(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda p: (2.0, 640, 480, 1000))
        monkeypatch.setattr(m, "video_phash_3frames",
                             lambda p, d: (calls.append(p), ["a" * 16, "b" * 16, "c" * 16])[1])

        vid, item = self._video_item(tmp_path)
        recs = m.analyze_batch([item], pool=m.Pool())

        assert len(calls) == 1
        assert recs[0].phash == "a" * 16 + "|" + "b" * 16 + "|" + "c" * 16

    def test_exact_dup_skips_phash(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "video_duration_and_resolution",
                             lambda p: (2.0, 640, 480, 1000))
        monkeypatch.setattr(m, "video_phash_3frames",
                             lambda p, d: (calls.append(p), ["a" * 16, "b" * 16, "c" * 16])[1])

        vid, item = self._video_item(tmp_path)
        sha = hashlib.sha256(vid.read_bytes()).hexdigest()
        pool = m.Pool()
        pool.add(m.PoolEntry(sha256=sha, ftype="video", dest_path="existing.mp4", size=1))

        recs = m.analyze_batch([item], pool=pool)
        rec = recs[0]

        assert calls == []
        assert rec.phash is None
        assert rec.is_media is True
        assert rec.duration == 2.0

        decision = m.decide(pool, rec)
        assert decision.decision == "skipped_present"
        assert decision.matched_dest == "existing.mp4"
