"""sanitize_windows_component() / truncate_segment() / _looks_like_path_traversal() -- pure
string transforms, no filesystem I/O."""
import pytest

import photosort_win as m


@pytest.mark.parametrize("raw,expected", [
    ("normal name", "normal name"),
    ("", "_"),
    ("bad:name?", "bad_name_"),
    ("path/with\\seps", "path_with_seps"),
    ("trailing dot.", "trailing dot"),  # Windows drops trailing dots/spaces on a component
    ("trailing space ", "trailing space"),
    ("CON", "CON_"),
    ("con", "con_"),  # case-insensitive reserved-name check
    ("CON.jpg", "CON_.jpg"),  # reserved name is still reserved with an extension
    ("COM1", "COM1_"),
    ("NOTCON", "NOTCON"),  # not an exact reserved-name match, left alone
])
def test_sanitize_windows_component(raw, expected):
    assert m.sanitize_windows_component(raw) == expected


def test_sanitize_all_invalid_chars_replaced_one_for_one():
    # Each illegal character is replaced individually (not collapsed) -- only a name that's
    # empty to begin with (or becomes empty after stripping trailing dots/spaces) falls back
    # to the single "_" placeholder, see test_sanitize_windows_component's "" case above.
    assert m.sanitize_windows_component('<>:"/\\|?*') == "_" * 9


def test_truncate_segment_short_name_unchanged():
    assert m.truncate_segment("photo.jpg", 100) == "photo.jpg"


def test_truncate_segment_respects_char_budget():
    name = "a" * 50 + ".jpg"
    truncated = m.truncate_segment(name, 20)
    assert len(truncated) <= 20
    assert truncated.endswith(".jpg")


def test_truncate_segment_respects_utf8_byte_budget_not_just_char_count():
    # Cyrillic is 2 bytes/char in UTF-8 -- 200 Cyrillic characters is 400 bytes, well over the
    # 255-byte filesystem component limit, even though it's fewer than max_len=240 characters.
    name = "ы" * 200 + ".jpg"
    truncated = m.truncate_segment(name, 240)
    assert len(truncated.encode("utf-8")) <= 255
    assert truncated.endswith(".jpg")


def test_truncate_segment_never_splits_a_multibyte_character():
    name = "ё" * 200
    truncated = m.truncate_segment(name, 240)
    # A byte-boundary cut mid-character would fail to decode/would leave stray continuation
    # bytes -- round-tripping through encode/decode already exercises this, but assert the
    # actual content is still made only of whole 'ё' characters as an extra guard.
    assert truncated == "ё" * len(truncated)


def test_truncate_segment_sanitizes_before_truncating():
    truncated = m.truncate_segment("CON", 100)
    assert truncated == "CON_"


@pytest.mark.parametrize("member_name,expected", [
    ("photos/img.jpg", False),
    ("../../etc/passwd", True),
    ("photos/../../../etc/passwd", True),
    ("/etc/passwd", True),
    (r"C:\Windows\System32", True),
    (r"photos\img.jpg", False),
    ("..", True),
    ("photos/..", True),
    ("photos/normal..name/img.jpg", False),  # ".." only counts as a whole path segment
])
def test_looks_like_path_traversal(member_name, expected):
    assert m._looks_like_path_traversal(member_name) is expected
