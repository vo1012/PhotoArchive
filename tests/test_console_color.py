"""Подсветка строк "ОШИБКА" красным в консоли (REVIEW-HANDOFF.md Раунд 15, "цвет консоли").
Реальную покраску (SetConsoleTextAttribute) можно проверить только на настоящем
Windows-терминале -- эти тесты кросс-платформенные и покрывают то, что можно:
контекст-менеджер не падает и не ломает вывод, когда хендла консоли нет (pytest-стдаут --
не tty, на Linux CI и ctypes.windll вовсе не существует), и что признак строки-ошибки
("ОШИБКА" после отступа) срабатывает/не срабатывает там, где нужно.

Белый фон при голом запуске (_console_bare_launch_colors(), тот же Раунд 15) был реализован и
откачен в этой же сессии -- живая проверка показала, что видимое поведение слишком зависит от
хоста консоли (легаси conhost.exe vs Windows Terminal/ConPTY), см. _console_red_text()
докстрока в photosort_win.py. Тесты на него удалены вместе с самой функцией."""
import photosort_win as m


def test_console_red_text_noop_without_handle():
    entered = False
    with m._console_red_text():
        entered = True
    assert entered


def test_console_stdout_handle_none_on_non_tty():
    # pytest captures stdout -- оно не tty ни на Windows, ни на Linux, независимо от os.name.
    assert m._console_stdout_handle() is None


def test_console_log_error_line_prints_unchanged_text(capsys):
    m.console_log("ОШИБКА: что-то сломалось")
    assert "ОШИБКА: что-то сломалось" in capsys.readouterr().out


def test_console_log_non_error_line_prints_unchanged_text(capsys):
    m.console_log("обычная строка лога, не ошибка")
    assert "обычная строка лога, не ошибка" in capsys.readouterr().out


def test_console_log_error_prefix_after_leading_whitespace(capsys):
    # :1826/:4395 -- "  ОШИБКА распаковки/записи ..." с ведущими пробелами (сохранённый
    # отступ), console_log() должен считать это той же категорией "строка-ошибка" (lstrip()
    # перед startswith), не только строки без отступа вроде "ОШИБКА: ...".
    m.console_log("  ОШИБКА распаковки file.zip: bad crc")
    assert "  ОШИБКА распаковки file.zip: bad crc" in capsys.readouterr().out


def test_log_unexpected_crash_first_line_has_error_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_app_dir", lambda: str(tmp_path))
    messages = []
    try:
        raise RuntimeError("synthetic crash for test")
    except RuntimeError:
        m._log_unexpected_crash(log=messages.append)
    assert messages[0].lstrip().startswith("ОШИБКА")
    # Reassurance-строки нарочно НЕ получают префикс -- это не сама ошибка, подсвечивать их
    # тоже красным было бы избыточно и пугало бы больше, чем нужно.
    assert not messages[1].lstrip().startswith("ОШИБКА")
    assert not messages[2].lstrip().startswith("ОШИБКА")
