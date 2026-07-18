"""is_dump_segment() / find_album() -- pure path-segment classification, no filesystem I/O.

Scenarios below are migrated from the equivalent subprocess unit checks in
ci/windows_ci_test.py (test_desktop_is_dump_segment, test_camera_roll_and_new_folder_are_dump_segments,
test_force_dump_tilde_prefix, test_bare_digit_date_folder_kept_inside_album_but_not_as_album_name)
-- those tests keep their end-to-end (real pipeline run) portions, only the pure-logic unit
checks were duplicated here where they run in seconds without a subprocess."""
import pytest

import photosort_win as m


@pytest.mark.parametrize("name,expected", [
    # 2026-07-11 finding: Desktop/Camera Roll/Новая папка are recognized dump segments.
    ("Desktop", True),
    ("Рабочий стол", True),
    ("Camera Roll", True),
    ("Новая папка", True),
    ("Новая папка (2)", True),
    ("New Folder", True),
    ("New Folder (3)", True),
    # deliberately NOT blanket-whitelisted -- see find_album()'s archive_boundary_idx handling
    # for how a real archive named "archive" is actually resolved.
    ("archive", False),
    ("Фото Чайка_2024", False),
    ("Яндекс_диск", False),
])
def test_is_dump_segment_known_names(name, expected):
    assert m.is_dump_segment(name) is expected


def test_force_dump_tilde_prefix():
    # 2026-07-11 (user request): a '~'-prefixed folder is ALWAYS dump, in either role, even
    # though the plain name would be a plausible real album.
    assert m.is_dump_segment("~Яндекс_диск") is True
    assert m.is_dump_segment("~Яндекс_диск", for_subpath=True) is True
    assert m.is_dump_segment("Яндекс_диск") is False


def test_bare_digit_date_folder_two_role_split():
    # 2026-07-11 finding: a bare 6-8 digit folder never NAMES an album, but survives as a
    # subpath once already inside a real album; a short digit run never gets that exemption;
    # a date WITH separators was never dump in either role.
    assert m.is_dump_segment("20240802") is True
    assert m.is_dump_segment("20240802", for_subpath=True) is False
    assert m.is_dump_segment("101", for_subpath=True) is True
    assert m.is_dump_segment("2015-08-20") is False


def test_dump_tag_always_dump_regardless_of_prefix_or_config():
    # A folder we generated ourselves (build_bydate_dest_dir) carries DUMP_TAG -- unambiguous,
    # no user ever types this by hand, must stay dump even with an empty configured name set.
    assert m.is_dump_segment("2023-10 [PhotoArchive]", dump_names=set(), dump_prefixes=()) is True


def test_dump_prefixes_whatsapp_telegram():
    assert m.is_dump_segment("WhatsApp Images") is True
    assert m.is_dump_segment("Telegram Documents") is True


class TestFindAlbum:
    def test_no_album_all_segments_dump_or_no_letters(self):
        assert m.find_album("DCIM/100ABCDE/IMG_0001.jpg") == (None, None, None)

    def test_first_meaningful_segment_is_the_album(self):
        album, subpath, prefix = m.find_album("Отпуск/2015-08-20/photo.jpg")
        assert album == "Отпуск"
        assert subpath == ["2015-08-20"]
        # album_prefix is the path from SOURCE's root up to AND INCLUDING the album segment
        # itself (used only for the merged-album marker note), not the full subpath too.
        assert prefix == "Отпуск"

    def test_bare_digit_date_subpath_kept_inside_real_album(self):
        # Companion of test_bare_digit_date_folder_two_role_split above, exercised through the
        # full find_album() path-walk rather than is_dump_segment() directly.
        album, subpath, _prefix = m.find_album("Отпуск/20240802/photo.jpg")
        assert album == "Отпуск"
        assert subpath == ["20240802"]

    def test_profile_username_segment_is_dump(self):
        # A Windows/Unix profile username sitting directly under Users/Home is not a
        # meaningful album -- loose photos underneath it fall through to ByDate instead.
        assert m.find_album("Users/User1/Pictures/photo.jpg") == (None, None, None)

    def test_archive_own_name_becomes_album_when_disk_side_has_none(self):
        # 2026-07-11 finding: an archive's OWN filename anchors the album when nothing
        # meaningful exists on the disk-side path leading to it (archive_boundary_idx == the
        # archive's own segment, "DCIM" being a dump name disqualifies the disk side). The
        # generic internal folder name "archive" (found inside many real zip exports) is never
        # trusted to NAME the album, but still survives as a subpath level underneath it.
        rel_path = "DCIM/Свадьба.zip/archive/photo.jpg"
        album, subpath, prefix = m.find_album(rel_path, archive_boundary_idx=1)
        assert album == "Свадьба.zip"
        assert subpath == ["archive"]
        assert prefix == "DCIM/Свадьба.zip"

    def test_real_album_outside_archive_wins_over_archive_name(self):
        rel_path = "Свадьба/export.zip/photos/img.jpg"
        album, subpath, _prefix = m.find_album(rel_path, archive_boundary_idx=1)
        assert album == "Свадьба"
        # the archive's own filename segment is not re-shown as a redundant subpath level
        assert subpath == ["photos"]
