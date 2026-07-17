"""classify_image() -- pure size/EXIF-based media classification. The .gif branch opens the
file with PIL to check for animation, but swallows any exception (missing file, non-image
content) and falls through to the same width/height logic -- so it stays testable without a
real GIF file: a nonexistent path just exercises the "not animated" fallback path."""
import pytest

import photosort_win as m


def test_icon_or_svg_never_media():
    assert m.classify_image("icon.ico", 64, 64, camera=None, size_bytes=100) == (False, "icon_or_svg")
    assert m.classify_image("logo.svg", None, None, camera=None, size_bytes=100) == (False, "icon_or_svg")


def test_gif_nonexistent_file_falls_through_to_size_check():
    # PIL can't open a path that doesn't exist -- classify_image() catches that and proceeds
    # as if the animation check found nothing, using width/height like any other format.
    is_media, note = m.classify_image("/no/such/file.gif", 800, 600, camera=None, size_bytes=100)
    assert (is_media, note) == (True, None)


def test_missing_dimensions_is_low_confidence_photo():
    assert m.classify_image("photo.jpg", None, None, camera=None, size_bytes=100) == (
        True, "low_confidence_photo")


def test_tiny_image_not_media():
    assert m.classify_image("photo.jpg", 200, 150, camera=None, size_bytes=100) == (False, "tiny_image")


def test_small_image_without_camera_flagged_but_kept():
    assert m.classify_image("photo.jpg", 500, 400, camera=None, size_bytes=100) == (True, "small_image")


def test_small_image_with_camera_exif_not_flagged():
    # Camera EXIF is trusted evidence this is a real (if small) photo, not a thumbnail/sticker.
    assert m.classify_image("photo.jpg", 500, 400, camera="Canon EOS 80D", size_bytes=100) == (True, None)


def test_ordinary_photo_no_flag():
    assert m.classify_image("photo.jpg", 4000, 3000, camera=None, size_bytes=100) == (True, None)


@pytest.mark.parametrize("max_side,small_image_px,camera,expected_note", [
    (639, 640, None, "small_image"),
    (640, 640, None, None),  # exactly at the threshold counts as "ordinary", not "small"
    (256, 640, None, "small_image"),  # exactly at the tiny/small boundary is media, not tiny
    (255, 640, None, "tiny_image"),
])
def test_small_image_threshold_boundaries(max_side, small_image_px, camera, expected_note):
    is_media, note = m.classify_image("photo.jpg", max_side, max_side - 1, camera=camera,
                                       size_bytes=100, small_image_px=small_image_px)
    if expected_note == "tiny_image":
        assert (is_media, note) == (False, "tiny_image")
    else:
        assert (is_media, note) == (True, expected_note)
