"""Раунд 142 ревью [БЛОКЕР], живая находка: photosort_win.py исполняется как `__main__`
(PyInstaller-бутлоадер/двойной клик по .exe) -- но gui_menu.py делает `import photosort_win as
m` на своей первой строке, а `sys.modules` не считает "__main__" и "photosort_win" одним и тем
же модулем по ключу словаря, поэтому этот `import` заново ИСПОЛНЯЕТ весь файл с нуля, порождая
ВТОРОЙ, независимый экземпляр модуля со своими копиями ВСЕХ module-level глобалов
(_work_console_allocated и т.д.) -- код, работающий через `m.`-путь (gui_menu.py), и код,
работающий unqualified внутри main()/_main() (копия "__main__"), физически не видят изменений
друг друга. Тот же класс бага, что сессия уже нашла и точечно обошла для _ACTIVE_BARS (см.
log_line()'s докстринг) -- здесь исправлен в корне (photosort_win.py, самая первая строка
внутри `if __name__ == "__main__":`): `sys.modules.setdefault("photosort_win",
sys.modules["__main__"])` регистрирует ТОТ ЖЕ объект модуля под его настоящим именем ДО того,
как что-либо успеет импортировать его заново -- дальнейший `import photosort_win` находит уже
существующую запись и переиспользует её, вторая копия больше не создаётся.

Этот файл НЕ гоняет полноценный photosort_win.py как отдельный процесс (это исполнило бы
main()/GUI, слишком тяжело и платформенно-специфично для юнит-теста) -- две вещи проверяются
отдельно и дёшево: (1) сам паттерн `sys.modules.setdefault(...)` в изолированном
мини-репродукторе (тот же приём, каким ревизор изначально подтвердил баг) действительно
устраняет дублирование модуля; (2) реальный photosort_win.py содержит именно эту строку РАНЬШЕ
единственного места, где он импортирует gui_menu."""
import subprocess
import sys
import textwrap
from pathlib import Path

import photosort_win as m


def _run_mini_repro(tmp_path, with_fix: bool) -> subprocess.CompletedProcess:
    fix_line = (
        'sys.modules.setdefault("app_under_test", sys.modules["__main__"])'
        if with_fix else "# (фикс намеренно не применён для этого прогона)"
    )
    app = tmp_path / "app_under_test.py"
    app.write_text(textwrap.dedent(f"""
        import sys

        flag = False


        def set_flag():
            global flag
            flag = True


        if __name__ == "__main__":
            {fix_line}
            import lib_under_test
            lib_under_test.set_flag()
            print("main copy sees flag =", flag)
            print("same module object =",
                  sys.modules.get("app_under_test") is sys.modules["__main__"])
    """), encoding="utf-8")
    lib = tmp_path / "lib_under_test.py"
    lib.write_text(textwrap.dedent("""
        import app_under_test as m


        def set_flag():
            m.set_flag()
    """), encoding="utf-8")
    return subprocess.run([sys.executable, str(app)], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=30)


def test_without_setdefault_fix_the_bug_reproduces(tmp_path):
    """Контрольный прогон -- подтверждает, что баг реален (не выдуман), тем же приёмом,
    которым его изначально нашёл ревизор: без sys.modules.setdefault() копия "__main__" не
    видит флаг, выставленный через импортированную копию того же файла."""
    result = _run_mini_repro(tmp_path, with_fix=False)
    assert result.returncode == 0, result.stderr
    assert "main copy sees flag = False" in result.stdout
    assert "same module object = False" in result.stdout


def test_with_setdefault_fix_the_global_is_shared(tmp_path):
    result = _run_mini_repro(tmp_path, with_fix=True)
    assert result.returncode == 0, result.stderr
    assert "main copy sees flag = True" in result.stdout
    assert "same module object = True" in result.stdout


def test_photosort_win_registers_itself_as_first_statement_of_main_guard():
    """import gui_menu -- ВНУТРИ тела _main() (функция определена текстово раньше по файлу,
    вызывается только во время исполнения main()), поэтому текстовая позиция самого `import
    gui_menu` не отражает порядок исполнения -- сравнивать с ней бессмысленно. Что РЕАЛЬНО
    гарантирует корректность фикса -- то, что sys.modules.setdefault() исполняется ПЕРВЫМ
    оператором внутри `if __name__ == "__main__":`, раньше даже multiprocessing.
    freeze_support() -- эти операторы последовательны внутри одного блока, тут текстовый
    порядок совпадает с порядком исполнения."""
    src = Path(m.__file__).read_text(encoding="utf-8")
    guard_idx = src.index('if __name__ == "__main__":')
    block = src[guard_idx:]
    lines = [ln.strip() for ln in block.splitlines()[1:]]
    first_statement = next(ln for ln in lines if ln and not ln.startswith("#"))
    assert first_statement == (
        'sys.modules.setdefault("photosort_win", sys.modules["__main__"])'
    )
