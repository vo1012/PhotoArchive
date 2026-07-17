"""hamming() / _aspect_bucket() / _quality_key() / image_is_strictly_better() /
video_is_strictly_better() / Pool.find_near_dup_image() / decide() -- pure dedup/quality
logic on bare Pool()/PoolEntry()/SourceRecord() objects, no real media files needed."""
import time

import pytest

import photosort_win as m


def _entry(**kwargs):
    defaults = dict(sha256="0" * 64, ftype="image", dest_path="dest", size=1000)
    defaults.update(kwargs)
    return m.PoolEntry(**defaults)


class TestHamming:
    def test_identical_hashes_zero_distance(self):
        assert m.hamming("aa55000000000000", "aa55000000000000") == 0

    def test_none_hash_returns_sentinel_not_crash(self):
        assert m.hamming(None, "aa55000000000000") == 999

    def test_known_distance(self):
        # v_close flips exactly one bit relative to the base hash (see point-5 fix commit).
        assert m.hamming("aa55000000000000", "aa55000000800000") == 1


class TestHammingInt:
    """hamming_int()/_phash_to_int() -- the cached-int fast path Pool uses instead of
    hamming(str, str) in its hot near-dup lookup loop (second-review perf finding,
    2026-07-17: re-parsing hex on every call made the O(n) fallback scan O(n) in wall-clock
    minutes at real archive scale). Must agree with hamming() bit-for-bit -- it's the same
    XOR-popcount math, just skipping imagehash's hex_to_hash() re-parse."""

    def test_agrees_with_hamming_on_known_distance(self):
        a, b = "aa55000000000000", "aa55000000800000"
        assert m.hamming_int(m._phash_to_int(a), m._phash_to_int(b)) == m.hamming(a, b) == 1

    def test_identical_hashes_zero_distance(self):
        v = m._phash_to_int("aa55000000000000")
        assert m.hamming_int(v, v) == 0

    def test_none_input_returns_sentinel_not_crash(self):
        assert m.hamming_int(None, m._phash_to_int("aa55000000000000")) == 999
        assert m.hamming_int(m._phash_to_int("aa55000000000000"), None) == 999

    def test_phash_to_int_invalid_hex_returns_none(self):
        assert m._phash_to_int("not-hex") is None
        assert m._phash_to_int(None) is None


@pytest.mark.parametrize("aspect,expected_bucket", [
    (4 / 3, round(4 / 3 * 50)),
    (1.0, 50),
    (16 / 9, round(16 / 9 * 50)),
])
def test_aspect_bucket(aspect, expected_bucket):
    assert m._aspect_bucket(aspect) == expected_bucket


class TestQualityComparison:
    def test_larger_pixel_area_wins(self):
        small = _entry(width=800, height=600, size=100000)
        big = _entry(width=4000, height=3000, size=100000)
        assert m.image_is_strictly_better(big, small) is True
        assert m.image_is_strictly_better(small, big) is False

    def test_equal_area_falls_back_to_file_size(self):
        smaller_file = _entry(width=800, height=600, size=100000)
        bigger_file = _entry(width=800, height=600, size=200000)
        assert m.image_is_strictly_better(bigger_file, smaller_file) is True

    def test_equal_area_and_size_falls_back_to_camera_exif(self):
        no_exif = _entry(width=800, height=600, size=100000, has_camera=False)
        with_exif = _entry(width=800, height=600, size=100000, has_camera=True)
        assert m.image_is_strictly_better(with_exif, no_exif) is True
        assert m.image_is_strictly_better(no_exif, with_exif) is False

    def test_identical_on_every_criterion_is_not_strictly_better(self):
        a = _entry(width=800, height=600, size=100000, has_camera=True)
        b = _entry(width=800, height=600, size=100000, has_camera=True)
        assert m.image_is_strictly_better(a, b) is False

    def test_video_prefers_area_then_bitrate_then_size(self):
        low = m.PoolEntry(sha256="0" * 64, ftype="video", dest_path="d", size=1000,
                           width=640, height=480, bitrate=1000)
        high = m.PoolEntry(sha256="1" * 64, ftype="video", dest_path="d", size=1000,
                            width=1920, height=1080, bitrate=1000)
        assert m.video_is_strictly_better(high, low) is True


class TestFindNearDupImage:
    """Migrated from ci/windows_ci_test.py's point-5 subprocess unit tests (second-review
    plan pt.5) -- same hand-crafted phash hex strings, now running as plain pytest instead of
    a subprocess round-trip through `python -c`."""

    def test_picks_best_quality_not_nearest_by_hamming(self):
        pool = m.Pool()
        aspect = 4 / 3
        query_phash = "aa55000000000000"
        close_low_quality = _entry(sha256="a" * 64, dest_path="close", size=100000,
                                    aspect=aspect, width=800, height=600, phash="aa55000000800000")
        far_high_quality = _entry(sha256="b" * 64, dest_path="far", size=5000000,
                                   aspect=aspect, width=4000, height=3000, phash="aa5500c0c0008000")
        pool.add(close_low_quality)
        pool.add(far_high_quality)

        entry, aspect_ok, dist = pool.find_near_dup_image(aspect, query_phash)

        assert entry.dest_path == "far"
        assert aspect_ok is True
        assert dist == 5

    def test_crop_fallback_finds_entry_with_different_phash_prefix(self):
        # Before the point-5 fix, the fallback looked entries up by the QUERY phash's own
        # 4-hex prefix in an index keyed by each ENTRY's own prefix -- a genuine crop match
        # with a different prefix (common once cropping perturbs enough hash bits) was missed.
        pool = m.Pool()
        entry = _entry(sha256="c" * 64, dest_path="cropped_source", size=500000,
                        aspect=4 / 3, width=1200, height=900, phash="aa55000000000000")
        pool.add(entry)

        found, aspect_ok, dist = pool.find_near_dup_image(1.8, "6a55000000000000")

        assert found.dest_path == "cropped_source"
        assert aspect_ok is False
        assert dist == 2

    def test_no_match_outside_threshold_returns_none(self):
        pool = m.Pool()
        pool.add(_entry(sha256="d" * 64, dest_path="unrelated", size=100,
                         aspect=4 / 3, width=800, height=600, phash="0000000000000000"))
        entry, aspect_ok, dist = pool.find_near_dup_image(4 / 3, "ffffffffffffffff")
        assert (entry, aspect_ok, dist) == (None, None, None)

    def test_fallback_scan_scales_to_thousands_of_entries(self):
        # Regression guard for the second-review perf finding (2026-07-17): the fallback
        # loop used to re-parse both hex phashes via imagehash.hex_to_hash() on every
        # comparison (~48us/call measured) -- O(n) per miss, O(n^2) over a real run, since
        # most photos in a real archive are unique and never match an aspect bucket (that's
        # exactly what forces this fallback branch). This pool is deliberately small (pytest
        # needs to stay fast, not simulate a real 20k-photo archive) but the time budget is
        # tight enough that the OLD per-call hex-parsing implementation would have blown it
        # (2000 entries * ~48us =~ 96ms) while the cached-int path clears it comfortably.
        pool = m.Pool()
        for i in range(2000):
            pool.add(_entry(sha256=f"{i:064x}", dest_path=f"e{i}", size=1000,
                             aspect=1.5, width=800, height=600, phash=f"{i:016x}"))
        start = time.perf_counter()
        # aspect=0.7 guarantees a miss on every aspect bucket (pool is all aspect=1.5) --
        # forces the O(n) fallback scan, not the cheap aspect-bucket path.
        pool.find_near_dup_image(0.7, "ffffffffffffffff")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, (
            f"near-dup fallback scan took {elapsed:.3f}s for 2000 entries -- likely a "
            f"perf regression in Pool.find_near_dup_image()'s hot loop")


def _source_record(ftype, **kwargs):
    item_kwargs = dict(read_path="/src/photo.jpg", origin_display="photo.jpg",
                        rel_path="Album/photo.jpg", size=1000, mtime=0.0, ftype=ftype)
    for key in ("sibling_path",):
        if key in kwargs:
            item_kwargs[key] = kwargs.pop(key)
    item = m.SourceItem(**item_kwargs)
    rec_kwargs = dict(item=item)
    rec_kwargs.update(kwargs)
    return m.SourceRecord(**rec_kwargs)


class TestDecide:
    def test_exact_duplicate_is_skipped(self):
        pool = m.Pool()
        pool.add(_entry(sha256="e" * 64, dest_path="existing.jpg"))
        rec = _source_record("image", sha256="e" * 64, phash="aa55000000000000", aspect=4 / 3)
        decision = m.decide(pool, rec)
        assert decision.decision == "skipped_present"
        assert decision.matched_dest == "existing.jpg"

    def test_new_image_no_neighbors_is_appended_new(self):
        pool = m.Pool()
        rec = _source_record("image", sha256="f" * 64, phash="aa55000000000000", aspect=4 / 3)
        decision = m.decide(pool, rec)
        assert decision.decision == "appended_new"

    def test_better_quality_near_dup_is_appended_better(self):
        pool = m.Pool()
        pool.add(_entry(sha256="1" * 64, dest_path="old.jpg", size=100000,
                         aspect=4 / 3, width=800, height=600, phash="aa55000000000000"))
        rec = _source_record("image", sha256="2" * 64, phash="aa55000000800000", aspect=4 / 3,
                              width=4000, height=3000, camera="Canon")
        decision = m.decide(pool, rec)
        assert decision.decision == "appended_better"
        assert decision.matched_dest == "old.jpg"

    def test_worse_or_equal_quality_near_dup_is_appended_not_skipped(self):
        # p.5.7: near-dups are always appended (never skipped) -- a burst-shot sequence can
        # have a technically-lower-quality frame that still matters to the user.
        pool = m.Pool()
        pool.add(_entry(sha256="3" * 64, dest_path="old.jpg", size=5000000,
                         aspect=4 / 3, width=4000, height=3000, phash="aa55000000000000"))
        rec = _source_record("image", sha256="4" * 64, phash="aa55000000800000", aspect=4 / 3,
                              width=800, height=600)
        decision = m.decide(pool, rec)
        assert decision.decision == "appended_near_dup"
        assert "near_dup_of=old.jpg" in decision.note
        assert "_hamming=1" in decision.note

    def test_no_phash_is_appended_uncertain(self):
        pool = m.Pool()
        rec = _source_record("image", sha256="5" * 64, phash=None, aspect=None)
        decision = m.decide(pool, rec)
        assert decision.decision == "appended_uncertain"
        assert decision.note == "no_phash_available"

    def test_lone_raw_always_mirrored_even_with_mirror_raw_false(self):
        pool = m.Pool()
        rec = _source_record("raw", sha256="6" * 64, sibling_path=None)
        decision = m.decide(pool, rec, mirror_raw=False)
        assert decision.decision == "raw_mirrored"
        assert decision.note == "raw_lone_mirrored"

    def test_redundant_raw_with_jpeg_sibling_skipped_when_mirror_raw_false(self):
        pool = m.Pool()
        rec = _source_record("raw", sha256="7" * 64, sibling_path="/src/photo.jpg")
        decision = m.decide(pool, rec, mirror_raw=False)
        assert decision.decision == "raw_skipped"
        assert decision.note == "raw_skipped_has_jpeg"

    def test_disputed_record_short_circuits(self):
        pool = m.Pool()
        rec = _source_record("image", sha256="8" * 64, broken=True, media_note="corrupt_file")
        decision = m.decide(pool, rec)
        assert decision.decision == "disputed"
        assert decision.note == "corrupt_file"
