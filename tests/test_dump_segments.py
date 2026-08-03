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
        # 2026-08-03: "архив == папка" -- the archive's own (non-dump) name is now kept as an
        # ordinary subpath level, exactly like a real folder would be, regardless of how many
        # media files live inside it (see find_album()'s docstring).
        assert subpath == ["export.zip", "photos"]

    def test_archive_own_name_kept_in_subpath_even_with_single_file(self):
        # Same rule as above, worded against the original "single file vs many" question this
        # was discussed against: no file-count exception exists -- a single-photo archive gets
        # the exact same sub-folder treatment as one with many.
        rel_path = "Свадьба/export.zip/img.jpg"
        album, subpath, _prefix = m.find_album(rel_path, archive_boundary_idx=1)
        assert album == "Свадьба"
        assert subpath == ["export.zip"]

    def test_dump_folder_below_album_poisons_everything_beneath_no_recovery(self):
        # 2026-08-03 (user request): a dump/tilde FOLDER found below an already-established
        # album voids the album for EVERYTHING deeper, permanently -- even folders that look
        # like perfectly meaningful album names of their own can't "un-poison" the branch.
        rel_path = "RealAlbum/~synced/RealLookingSubAlbum/photo.jpg"
        assert m.find_album(rel_path) == (None, None, None)

    def test_poison_does_not_affect_sibling_branch(self):
        # The poison above is scoped to items whose path actually passes through the dump/
        # tilde folder -- a sibling file directly in RealAlbum (not through ~synced) resolves
        # completely normally.
        album, subpath, _prefix = m.find_album("RealAlbum/normal.jpg")
        assert album == "RealAlbum"
        assert subpath == []

    def test_bare_date_folder_below_album_does_not_poison(self):
        # Companion of the poison test above: a bare 6-8 digit day-folder is NOT a "not
        # curated" signal (same for_subpath exemption as test_bare_digit_date_subpath_kept_
        # inside_real_album), so it must not trigger the poison rule.
        album, subpath, _prefix = m.find_album("RealAlbum/20240802/RealLookingSubAlbum/photo.jpg")
        assert album == "RealAlbum"
        assert subpath == ["20240802", "RealLookingSubAlbum"]

    def test_tilde_archive_own_name_is_unconditional_bydate_even_inside_real_album(self):
        # 2026-08-03 (user request): unlike a folder, an archive's OWN tilde/dump name makes
        # its entire content independently ByDate, regardless of where the archive itself
        # physically sits -- even directly inside an otherwise-fine real album.
        rel_path = "RealAlbum/~backup/photo.jpg"
        assert m.find_album(rel_path, archive_boundary_idx=1) == (None, None, None)

    def test_tilde_archive_does_not_poison_sibling_files_in_same_album(self):
        # The independence cuts both ways: the tilde-archive's own fate doesn't leak out to
        # affect a normal sibling file living in the very same album folder.
        album, subpath, _prefix = m.find_album("RealAlbum/normal.jpg")
        assert album == "RealAlbum"
        assert subpath == []

    def test_dump_folder_poisons_before_archive_is_ever_consulted(self):
        # If the branch is ALREADY poisoned by a dump/tilde FOLDER before reaching an archive,
        # the archive doesn't get a chance to "rescue" anything via its own (perfectly normal)
        # name -- the poison is a one-way door regardless of what's found deeper.
        rel_path = "RealAlbum/~tildefolder/somearchive/photo.jpg"
        assert m.find_album(rel_path, archive_boundary_idx=2) == (None, None, None)


class TestIsTerminalBydateBranch:
    """REVIEW-HANDOFF.md, Раунд 58 (придирка): _is_terminal_bydate_branch() дублирует логику
    find_album() вручную (см. её собственный докстринг: "kept in sync manually"), но не имела
    ни одного прямого теста -- всё покрытие было косвенным, через сквозное поведение
    SourceWalker.walk(). Раунд 58 подтвердил инвариант независимым fuzz-тестом (~60000
    случайных путей, не тестами проекта) -- эти тесты не повторяют тот же fuzz, а закрепляют
    ключевые случаи из докстринга/RULES.md напрямую, чтобы будущая правка find_album() без
    синхронной правки этой копии ловилась сразу, не только на практике.

    segments -- путь ДО и ВКЛЮЧАЯ папку/архив, который сейчас проверяется (без имени файла,
    в отличие от find_album())."""

    def test_still_searching_dump_name_alone_is_not_terminal(self):
        # A dump-named folder alone doesn't end the search -- a real album might still be
        # found deeper (e.g. DCIM/Отпуск/photo.jpg), so Фаза 1 must keep descending.
        assert m.is_dump_segment("DCIM")
        assert m._is_terminal_bydate_branch(["DCIM"]) is False

    def test_real_album_folder_is_not_terminal(self):
        assert m._is_terminal_bydate_branch(["Отпуск"]) is False

    def test_bare_digit_date_folder_inside_album_is_not_terminal(self):
        # Same for_subpath exemption as find_album()'s bare-digit-date handling -- a day-
        # folder carried over from a camera/phone is not a poison signal.
        assert m._is_terminal_bydate_branch(["Отпуск", "20240802"]) is False

    def test_dump_folder_below_album_is_terminal(self):
        assert m._is_terminal_bydate_branch(["RealAlbum", "~synced"]) is True

    def test_profile_username_branch_is_not_terminal(self):
        # Mirrors find_album()'s Users/Home handling -- "Users" itself is a recognized dump
        # name (loop keeps searching), "User1" is disqualified as a profile username, but
        # neither makes the branch a definitive dead end -- a real album might still exist
        # deeper under the profile folder.
        assert m._is_terminal_bydate_branch(["Users", "User1"]) is False

    def test_archive_own_dump_name_is_terminal(self):
        assert m._is_terminal_bydate_branch(["DCIM", "~namedarchive"], archive_boundary_idx=1) is True

    def test_archive_own_normal_name_becomes_album_not_terminal(self):
        assert m._is_terminal_bydate_branch(["DCIM", "realname"], archive_boundary_idx=1) is False

    def test_archive_own_name_without_letters_is_terminal(self):
        # Dead end: neither the disk side nor the archive's own name (no letters) qualify.
        assert m._is_terminal_bydate_branch(["DCIM", "12345"], archive_boundary_idx=1) is True

    def test_archive_with_normal_name_inside_real_album_is_not_terminal(self):
        assert m._is_terminal_bydate_branch(["RealAlbum", "normalarchive"], archive_boundary_idx=1) is False

    def test_tilde_archive_inside_real_album_is_terminal(self):
        # 2026-08-03: an archive's own tilde/dump name is unconditional ByDate regardless of
        # where it physically sits -- even directly inside an otherwise-fine real album.
        assert m._is_terminal_bydate_branch(["RealAlbum", "~backup"], archive_boundary_idx=1) is True

    def test_dump_folder_poisons_before_archive_is_reached(self):
        assert m._is_terminal_bydate_branch(
            ["RealAlbum", "~tildefolder", "somearchive"], archive_boundary_idx=2) is True

    @pytest.mark.parametrize("segments,archive_boundary_idx,expected_terminal", [
        (["DCIM"], None, False),
        (["Отпуск"], None, False),
        (["Отпуск", "20240802"], None, False),
        (["RealAlbum", "~synced"], None, True),
        (["Users", "User1"], None, False),
        (["DCIM", "~namedarchive"], 1, True),
        (["DCIM", "realname"], 1, False),
        (["DCIM", "12345"], 1, True),
        (["RealAlbum", "normalarchive"], 1, False),
        (["RealAlbum", "~backup"], 1, True),
        (["RealAlbum", "~tildefolder", "somearchive"], 2, True),
    ])
    def test_agrees_with_find_album_on_a_leaf_completing_the_same_path(
            self, segments, archive_boundary_idx, expected_terminal):
        """Инвариант, который держится ТОЛЬКО в одну сторону (см. докстринг
        _is_terminal_bydate_branch()): если она говорит "тупик" (True) для этой ветки, то
        find_album() для ЛЮБОГО файла, довершающего этот же путь, обязана вернуть None -- это
        и есть всё, что "тупик" гарантирует (одно и то же ревизорское fuzz-подтверждение).
        Обратное НЕ верно и не проверяется здесь: "не тупик" (False) означает лишь "поиск ещё
        не исчерпан", а не "файл-лист прямо на этой глубине обязательно найдёт альбом" -- у
        "DCIM/photo.jpg" (пример из докстрингов обеих функций) find_album() тоже честно вернёт
        None, что не противоречит False (реальный альбом мог бы найтись ГЛУБЖЕ, просто не в
        этом конкретном, специально коротком тестовом пути)."""
        got_terminal = m._is_terminal_bydate_branch(segments, archive_boundary_idx=archive_boundary_idx)
        assert got_terminal is expected_terminal
        if not expected_terminal:
            return
        rel_path = "/".join(segments) + "/photo.jpg"
        album, _subpath, _prefix = m.find_album(rel_path, archive_boundary_idx=archive_boundary_idx)
        assert album is None
