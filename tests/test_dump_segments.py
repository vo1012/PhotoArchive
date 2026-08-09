"""is_dump_segment() / find_album() -- pure path-segment classification, no filesystem I/O.

2026-08-08 (альбомный редизайн, "чем проще, тем лучше для пользователя"): алгоритм сведён к
одному правилу -- любой служебный (dump) сегмент ГДЕ УГОДНО на пути отравляет всё, что глубже,
без исключений по позиции (день-папка, юзернейм профиля, архив как "второй шанс" -- всё это
убрано). Если путь не отравлен -- он целиком зеркалится в Albums\\, и каждая папка на нём это
свой отдельный альбом (см. RULES.md)."""
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
    ("archive", False),
    ("Фото Чайка_2024", False),
    ("Яндекс_диск", False),
])
def test_is_dump_segment_known_names(name, expected):
    assert m.is_dump_segment(name) is expected


def test_force_dump_tilde_prefix():
    # 2026-07-11 (user request): a '~'-prefixed folder is ALWAYS dump, even though the plain
    # name would be a plausible real album.
    assert m.is_dump_segment("~Яндекс_диск") is True
    assert m.is_dump_segment("Яндекс_диск") is False


def test_bare_digit_folder_always_dump():
    # 2026-08-08: убрана позиционная экземпция дня-папки (было: НЕ dump, если уже внутри
    # найденного альбома) -- "день-папка всегда dump, отравляет наравне со всеми" (прямое
    # решение пользователя). Любой голый цифровой сегмент, любой длины, теперь безусловно dump.
    assert m.is_dump_segment("20240802") is True
    assert m.is_dump_segment("101") is True
    assert m.is_dump_segment("2015-08-20") is False  # separators -- not a bare digit run


def test_dump_tag_always_dump_regardless_of_prefix_or_config():
    # A folder we generated ourselves (build_bydate_dest_dir) carries DUMP_TAG -- unambiguous,
    # no user ever types this by hand, must stay dump even with an empty configured name set.
    assert m.is_dump_segment("2023-10 [PhotoArchive]", dump_names=set(), dump_prefixes=()) is True


def test_dump_prefixes_whatsapp_telegram():
    assert m.is_dump_segment("WhatsApp Images") is True
    assert m.is_dump_segment("Telegram Documents") is True


class TestFindAlbum:
    def test_no_dump_anywhere_mirrors_whole_path(self):
        album, subpath, prefix = m.find_album("Отпуск/Море/photo.jpg")
        assert (album, subpath, prefix) == ("Отпуск", ["Море"], "Отпуск")

    def test_single_segment_album(self):
        album, subpath, prefix = m.find_album("Отпуск/photo.jpg")
        assert (album, subpath, prefix) == ("Отпуск", [], "Отпуск")

    def test_file_directly_in_source_root_has_no_album(self):
        assert m.find_album("photo.jpg") == (None, None, None)

    def test_dump_top_segment_poisons_everything_below(self):
        # DCIM -- служебное имя, отравляет "Отпуск" ниже, хотя раньше (до редизайна) поиск
        # просто пропускал бы DCIM и нашёл бы "Отпуск" как альбом.
        assert m.find_album("DCIM/Отпуск/photo.jpg") == (None, None, None)

    def test_dump_deep_inside_otherwise_real_path_poisons_below(self):
        # 2026-08-03: dump/тильда-папка НИЖЕ уже "хорошего" сегмента всё равно отравляет всё
        # глубже неё, без права на реанимацию даже осмысленным именем ниже.
        assert m.find_album("RealAlbum/~synced/RealLookingSubAlbum/photo.jpg") == (None, None, None)

    def test_dump_does_not_affect_sibling_branch(self):
        # Отравление скоуплено на файлы, ЧЕЙ путь реально проходит через dump-сегмент -- сосед
        # в той же папке, но без dump-сегмента на своём пути, резолвится нормально.
        assert m.find_album("RealAlbum/normal.jpg") == ("RealAlbum", [], "RealAlbum")

    def test_bare_date_folder_now_poisons_like_any_other_dump_name(self):
        # 2026-08-08: раньше эта день-папка была исключением ("не считается сигналом
        # некуратировано") -- теперь исключений нет вовсе, она dump как и всё остальное.
        assert m.find_album("RealAlbum/20240802/RealLookingSubAlbum/photo.jpg") == (None, None, None)

    def test_users_profile_root_poisons_like_any_dump_name(self):
        # 2026-08-08: отдельная проверка юзернейма профиля убрана как избыточная -- "Users"
        # само по себе уже служебное имя и отравляет всё ниже с первого сегмента, до проверки
        # логина дело не доходит вовсе (даже частый сценарий "весь профиль/Pictures как
        # источник" теперь падает в ByDate, осознанно принято пользователем).
        assert m.find_album("Users/User1/Pictures/Отпуск/photo.jpg") == (None, None, None)

    def test_archive_own_dump_name_poisons_only_its_own_content(self):
        # Имя архива -- просто ещё один сегмент пути, проверяется той же is_dump_segment(), в
        # том же едином проходе, без отдельного кода. Тильда/dump-имя архива отравляет то, что
        # внутри НЕГО, но не соседние файлы (у них этого сегмента вовсе нет в их rel_path).
        rel_path = "RealAlbum/~backup.zip/inner/photo.jpg"
        assert m.find_album(rel_path, archive_boundary_idx=1) == (None, None, None)
        assert m.find_album("RealAlbum/sibling.jpg") == ("RealAlbum", [], "RealAlbum")

    def test_archive_normal_name_participates_like_an_ordinary_folder(self):
        # Архив с обычным (не dump) именем -- просто ещё один сегмент дерева, ничем не
        # отличается от папки: путь целиком зеркалится, включая имя архива как подпапка.
        rel_path = "Свадьба/export.zip/photos/img.jpg"
        album, subpath, prefix = m.find_album(rel_path, archive_boundary_idx=1)
        assert (album, subpath, prefix) == ("Свадьба", ["export.zip", "photos"], "Свадьба")

    def test_dump_folder_poisons_before_archive_is_ever_consulted(self):
        rel_path = "RealAlbum/~tildefolder/somearchive/photo.jpg"
        assert m.find_album(rel_path, archive_boundary_idx=2) == (None, None, None)

    def test_every_folder_in_a_clean_tree_is_its_own_album(self):
        # "Альбом -- это каждая папка в дереве" (прямая формулировка пользователя): "Мои
        # фото/Свадьба" и "Мои фото/Отпуск" физически лежат под общим "Мои фото", но каждая
        # папка на пути (включая сам "Мои фото") -- потенциально свой альбом; find_album()
        # отдаёт верхний сегмент как album/album_prefix (для build_album_dest_dir()), полный
        # путь восстанавливается как album_prefix + subpath.
        album1, subpath1, prefix1 = m.find_album("Мои фото/Свадьба/img.jpg")
        album2, subpath2, prefix2 = m.find_album("Мои фото/Отпуск/img.jpg")
        assert (album1, subpath1, prefix1) == ("Мои фото", ["Свадьба"], "Мои фото")
        assert (album2, subpath2, prefix2) == ("Мои фото", ["Отпуск"], "Мои фото")

    def test_dump_ancestor_before_real_name_poisons_the_whole_thing(self):
        # 2026-08-08: раньше два разных dump-родителя перед одинаковым именем считались двумя
        # разными альбомами (album_prefix разный) -- теперь ОБА падают в ByDate, вопрос
        # "разные ли это альбомы" не возникает вовсе, раз альбома нет.
        assert m.find_album("DCIM/Отпуск/a.jpg") == (None, None, None)
        assert m.find_album("Camera/Отпуск/b.jpg") == (None, None, None)


class TestIsTerminalBydateBranch:
    """2026-08-08 (альбомный редизайн): с уходом позиционных исключений найти "тупик" стало
    безусловным -- если СРЕДИ УЖЕ ПРОЙДЕННЫХ сегментов (включая текущую папку/архив) есть хотя
    бы один dump, результат уже окончательный (True), что бы ни нашлось глубже. Больше нет
    промежуточного "ещё не решено" состояния, отличного от простого is_dump_segment()."""

    def test_dump_name_alone_is_terminal(self):
        # 2026-08-08: раньше DCIM сам по себе НЕ был тупиком (поиск мог продолжаться глубже и
        # найти реальный альбом) -- теперь любой dump сегмент немедленно и безусловно тупик.
        assert m._is_terminal_bydate_branch(["DCIM"]) is True

    def test_real_folder_is_not_terminal(self):
        assert m._is_terminal_bydate_branch(["Отпуск"]) is False

    def test_bare_digit_date_folder_is_now_terminal(self):
        # 2026-08-08: убрана экземпция -- день-папка dump как и всё остальное.
        assert m._is_terminal_bydate_branch(["Отпуск", "20240802"]) is True

    def test_dump_folder_below_real_name_is_terminal(self):
        assert m._is_terminal_bydate_branch(["RealAlbum", "~synced"]) is True

    def test_users_profile_root_is_terminal(self):
        assert m._is_terminal_bydate_branch(["Users", "User1"]) is True

    def test_archive_own_dump_name_is_terminal(self):
        assert m._is_terminal_bydate_branch(["RealAlbum", "~namedarchive"], archive_boundary_idx=1) is True

    def test_archive_own_normal_name_is_not_terminal(self):
        assert m._is_terminal_bydate_branch(["RealAlbum", "realname"], archive_boundary_idx=1) is False

    @pytest.mark.parametrize("segments,archive_boundary_idx,expected_terminal", [
        (["DCIM"], None, True),
        (["Отпуск"], None, False),
        (["Отпуск", "20240802"], None, True),
        (["RealAlbum", "~synced"], None, True),
        (["Users", "User1"], None, True),
        (["RealAlbum", "~namedarchive"], 1, True),
        (["RealAlbum", "realname"], 1, False),
        (["RealAlbum", "~tildefolder", "somearchive"], 2, True),
    ])
    def test_agrees_with_find_album_on_a_leaf_completing_the_same_path(
            self, segments, archive_boundary_idx, expected_terminal):
        """Если _is_terminal_bydate_branch() говорит "тупик" (True) для этой ветки, то
        find_album() для ЛЮБОГО файла, довершающего этот же путь, обязана вернуть None."""
        got_terminal = m._is_terminal_bydate_branch(segments, archive_boundary_idx=archive_boundary_idx)
        assert got_terminal is expected_terminal
        if not expected_terminal:
            return
        rel_path = "/".join(segments) + "/photo.jpg"
        album, _subpath, _prefix = m.find_album(rel_path, archive_boundary_idx=archive_boundary_idx)
        assert album is None
