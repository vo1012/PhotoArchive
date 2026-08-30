#!/usr/bin/env python3
"""
photosort_win.py -- сборка фото/видео архива (portable Windows-версия).

Windows-адаптация photo-sort для локального запуска (обычные локальные пути, без SMB).
Бизнес-логика (дедуп, тиры дат, hybrid-раскладка) идентична Linux-версии photo-sort;
полная спецификация правил ведётся в документации проекта.

Запуск (portable .exe, собранный PyInstaller -- см. README-BUILD.md):
    PhotoArchive.exe --source "D:\\Фото" --target "D:\\Архив фото"
    PhotoArchive.exe                      # спросит source/target интерактивно
    PhotoArchive.exe --source "D:\\Фото" --target "D:\\Архив фото" --dry-run --sample-limit 200

Запуск из исходников (разработка/тестирование, требует Python 3 + зависимости из
requirements -- см. README-BUILD.md):
    python3 photosort_win.py --source <SOURCE> --target <TARGET>
"""

import argparse
import contextlib
import csv
import ctypes
import errno
import fnmatch
import hashlib
import inspect
import itertools
import json
import multiprocessing
import ntpath
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import traceback
import warnings
import webbrowser
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image
import pillow_heif
import imagehash
import yaml
from tqdm import tqdm as _tqdm

import report  # PROMPT_archive_report.md, границы: отдельный модуль, ничего не импортирует
                # обратно отсюда -- см. report.py docstring

# Раунды 164A/166 ревью (внешний аудит): Image.MAX_IMAGE_PIXELS оставлен на дефолте Pillow
# (~89M пикс) -- он и так закрывает главный риск: файл >2·MAX (>179M пикс) поднимает
# DecompressionBombError на Image.open() ДО аллокации, и это подкласс Exception, уже ловится
# try/except вокруг всех трёх Image.open() (image_size_only()/image_phash_and_size()) -> файл
# трактуется как нечитаемый (low_confidence_photo, kept-not-lost). Кап 300M (введён в 77b094e,
# откачен в этом же раунде) только РАСШИРЯЛ полосу "декодируется целиком с warning'ом" -- см. Раунд 166.
# Заглушаем сам DecompressionBombWarning (полоса 89M-179M: PIL декодирует, но печатает
# advisory-warning, ломающий tqdm-бар -- тот же класс, что "Palette images" ниже; настоящие
# бомбы это не затрагивает, у них Error, а не Warning).
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

# 2026-07-11 live-run finding: Pillow prints "Palette images with Transparency expressed in
# bytes should be converted to RGBA images" (UserWarning, via the `warnings` module -> stderr
# by default) on ordinary real-world palette+transparency PNGs/GIFs during normal analysis --
# purely advisory (about how Pillow itself might change behavior in a future version, not
# about anything wrong with the file or our processing), with no coordination with our own
# tqdm progress bar, so it interleaves mid-line on a real console (same class of problem as
# reverse_geocoder's verbose print, see place_for_gps()). Silenced by exact message, not a
# blanket ignore of all warnings, so any other future PIL/library warning still surfaces.
warnings.filterwarnings("ignore", message="Palette images with Transparency.*", category=UserWarning)

__version__ = "0.6.6"           # версия ПРОГРАММЫ (тег/релиз, см. RELEASING.md) -- НЕ путать
                                 # с RULES_VERSION ниже (та про совместимость архива, а не exe)
RULES_VERSION = "2026-08-11"   # дата последнего изменения бизнес-правил -- см. RULES.md;
                                # менять руками при изменении логики раскладки/дедупа/дат
__copyright__ = "© 2026 Vladimir Oleynikov"  # держим строку короткой и везде идентичной
                                              # LICENSE, а не только там, куда мало кто
                                              # заглядывает; заодно закрывает обязательство
                                              # держать копирайт-уведомление легкодоступным при
                                              # распространении .exe, бандлящего сторонние
                                              # GPL/LGPL/Artistic-компоненты (см.
                                              # THIRD_PARTY_LICENSES.md)
__license__ = "Apache License 2.0"  # см. LICENSE/NOTICE -- ЕДИНСТВЕННОЕ место, которое
                      # меняется, если лицензия программы сменится. Финальное решение
                      # 2026-07-13 (после MIT -> PolyForm Noncommercial -> это, см. историю
                      # обсуждения в ROADMAP.md/CHANGELOG.md): коммерческой ценности,
                      # которую защищал бы source-available-вариант, всё равно нет (техника не
                      # уникальна) -- а для репутации/портфолио важнее узнаваемая OSI-лицензия.
                      # Название уже включает слово "License" -- места, которые его печатают,
                      # не должны добавлять его ещё раз следом.

SITE_URL = "https://vo1012.github.io/PhotoArchive"  # 2026-07-20: сайт проекта (GitHub Pages
    # того же публичного репо github.com/vo1012/PhotoArchive, корень, Jekyll) -- то, что видит
    # пользователь программы (--help/--version/интерактивное меню). Сам репозиторий на GitHub
    # по-прежнему существует и остаётся тем, на что ссылаются README.md и документы, читаемые
    # НА GitHub (FAQ.md/QUICKSTART.md/PhotoArchive_ot_avtora.md ссылаются на github.com у себя
    # в исходном .md -- см. build/md_to_pdf.py про то, где именно они переходят на SITE_URL для
    # оффлайн-аудитории). При смене адреса сайта поменять только здесь -- print_welcome_banner()/
    # build_arg_parser() ссылаются на эту же константу.

DONATION_TEXT = (  # 2026-07-24: реальный контакт (почта) публикуется ТОЛЬКО в разделе
    # "Контакты" на сайте -- --help по-прежнему указывает на сайт, не дублирует адрес
    # текстом (тот же принцип единственного места публикации, что раньше применялся к
    # номеру карты; см. историю решения в ROADMAP.md/CHANGELOG.md). Решение 2026-07-15:
    # полный текст, не короткая строка, т.к. --help может быть единственным местом, где
    # пользователь вообще видит эту программу (exe пересылается мессенджерами без
    # README/письма от автора).
    "Этот проект не преследует получение коммерческой выгоды -- программа бесплатна и\n"
    "останется такой для всех, вне зависимости от того, воспользуетесь вы этим предложением\n"
    "или нет. Если PhotoArchive окажется полезной и вы захотите поддержать её разработку --\n"
    "актуальный способ сделать это указан в разделе «Контакты» на сайте проекта:\n"
    f"{SITE_URL}."
)


def _app_dir() -> str:
    """Папка рядом с исполняемым файлом: frozen PyInstaller exe -> папка с .exe;
    иначе -- папка со скриптом. work.db/photoarchive_config.yaml живут здесь же (portable: переносится
    вместе с .exe одной папкой)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel: str) -> str:
    """Путь к бандленному ресурсу (внешний бинарник и т.п.): под frozen PyInstaller --
    onefile ресурсы, добавленные через --add-binary, распаковываются во временную
    sys._MEIPASS; при обычном запуске .py -- берём ./bin/ рядом со скриптом."""
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, rel)


def tool_binary(name: str, exe_name: str) -> str:
    """Резолвит путь к внешнему бинарнику (exiftool/ffprobe/ffmpeg/7z/unrar):
    1) бандленный в bin/ (frozen exe или dev-дерево рядом со скриптом) -- всегда в приоритете,
       если файл реально на месте;
    2) frozen exe БЕЗ файла в bin/ (сломанная сборка -- забыли --add-binary для этого файла) --
       Security audit finding #2 (2026-07-10): всё равно возвращаем ожидаемый (хоть и
       отсутствующий) АБСОЛЮТНЫЙ путь, а не голое имя. На portable frozen exe голое имя
       означает поиск по PATH, где на машине пользователя этого инструмента гарантированно
       нет (portable exe специально не требует системных установок) -- раньше это давало ту
       же самую строку, что и осознанный dev-fallback ниже, и check_bundled_tools() (которая
       отличает "отсутствует" именно по os.path.isabs()) не могла увидеть разницу между
       "сломанная сборка" и "нормальный dev-запуск на Linux с системными пакетами" -- сломанная
       сборка проходила молча, без единого предупреждения, и весь прогон архивации тихо
       деградировал (например, без exiftool -- каждая дата в архиве получала Tier C/эвристику
       вместо реального EXIF, без единой строки об этом в логах);
    3) НЕ frozen (dev-запуск python3 photosort_win.py) без файла в bin/ -- голое имя, в
       расчёте на PATH (dev-запуск на Linux, где эти утилиты уже стоят системно -- см. README
       оригинального photo-sort)."""
    candidate = resource_path(os.path.join("bin", exe_name))
    if os.path.exists(candidate) or getattr(sys, "frozen", False):
        return candidate
    return name


def winlong(path: str) -> str:
    """Extended-length ('\\\\?\\') form of an absolute path for raw filesystem calls
    (open/os.stat/os.listdir/shutil.copy2/os.replace/...), so deeply nested ByDate/Albums
    destinations and deeply nested source trees survive the 260-character Windows MAX_PATH
    limit. Works unconditionally (no admin rights, no registry LongPathsEnabled policy
    needed) -- unlike the manifest-based longPathAware opt-in, which only helps if the
    machine's policy is already on and which a portable, no-install exe cannot set for the
    user. No-op on non-Windows (dev/test on Linux). Idempotent -- an already '\\\\?\\'-prefixed
    path (local or UNC) is returned unchanged.

    UNC (\\\\server\\share\\...) gets the '\\\\?\\UNC\\server\\share\\...' form -- lifts MAX_PATH
    the same way '\\\\?\\' does for drive-letter paths (portable version targets local paths,
    see RULES.md, but Config accepts a UNC SOURCE/TARGET, so a deep NAS path must not hit an
    unhandled OSError; ntpath.abspath normalizes '..'/separators cross-platform for the test).
    Only wraps calls we make directly in Python; exiftool/ffmpeg/ffprobe/7z/unrar are
    separate subprocesses invoked with the plain path and are not covered by this."""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ntpath.abspath(path)[2:]
    return "\\\\?\\" + os.path.abspath(path)


def _makedirs_iterative(path: str):
    """Non-recursive equivalent of `os.makedirs(path, exist_ok=True)`. `os.makedirs()` itself
    recurses once per missing path component -- mirroring SourceWalker's own now-fixed depth
    limit (see ROADMAP.md "RecursionError на очень глубоком дереве папок SOURCE") on the
    TARGET side (a dest_dir built from a deep SOURCE subpath) would otherwise still hit
    Python's OWN RecursionError here, on write instead of on enumeration -- found while
    testing that fix. Walk up to the nearest already-existing ancestor, then create the
    missing suffix top-down in a plain loop -- no recursion at any depth. Expects an
    already-`winlong()`-formed path, same as every direct `os.makedirs()` call it replaces."""
    if os.path.isdir(path):
        return
    missing = []
    cur = path
    while cur and not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d)
        except FileExistsError:
            pass


def _strip_winlong(path: str) -> str:
    """Undo winlong()'s prefix, so paths coming out of a long-path-safe walk stay canonical
    plain strings for DB storage / CSV logs / display -- the prefix is only ever needed at
    the point of the actual filesystem call, re-added there via winlong(). Must stay the exact
    inverse of winlong() for BOTH forms: '\\\\?\\UNC\\server\\share\\...' -> '\\\\server\\share\\...',
    '\\\\?\\C:\\...' -> 'C:\\...' (Раунд 166 ревью: UNC-ветка добавлена вслед за winlong(),
    иначе '\\\\?\\UNC\\nas\\x' обрезалось до 'UNC\\nas\\x' -> порча путей в БД/логах на UNC-TARGET)."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


WORKDIR = _app_dir()
SCRIPT_PATH = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)

EXIFTOOL_BIN = tool_binary("exiftool", "exiftool.exe")
FFPROBE_BIN = tool_binary("ffprobe", "ffprobe.exe")
FFMPEG_BIN = tool_binary("ffmpeg", "ffmpeg.exe")
SEVENZIP_BIN = tool_binary("7z", "7z.exe")
UNRAR_BIN = tool_binary("unrar", "UnRAR.exe")

# ============================================================================
# PROGRESS  (А.4/Задача 4: экранная индикация длительных фаз, без файлового heartbeat)
# ============================================================================

_ACTIVE_BARS = []  # стек открытых ProgressReporter с реальным tqdm-баром -- см. log_line()


def log_line(msg, log=print):
    """log() совместимая обёртка: если сейчас активен хотя бы один экранный прогресс-бар,
    печать идёт БЕЗ порчи строки бара (строка бара очищается, сообщение печатается на
    отдельной строке, бар перерисовывается заново ниже), иначе -- как обычно через log().
    Передаётся как `log=` в run()/run_analyze() ОДИН раз в main() -- дальше все вложенные
    log() по всему конвейеру (SourceWalker, analyze_batch, index_archive, ...) уже вызывают
    именно этот же переданный им callable, так что оборачивать нужно только тут, без правки
    отдельных мест вызова.

    2026-07-11 (live user report): раньше здесь был `_tqdm.write(str(msg), file=sys.stderr)`
    -- штатный способ tqdm напечатать что-то, не сломав активный бар. НЕ РАБОТАЕТ с нашим
    баром: `_tqdm.write()` внутри себя решает, какие активные бары нужно очистить перед
    печатью, СРАВНИВАЯ их поток вывода с переданным `file=` -- а наш бар создан с
    `file=_RussianRateStream(...)` (см. её докстроку), собственным прокси-объектом, а не
    голым `sys.stderr`, который сюда передан. Сравнение не совпадает, tqdm не узнаёт свой же
    бар, ничего не очищает -- сообщение просто дописывается ПРЯМО В КОНЕЦ текущей строки бара
    без разделителя (живой пример: "...обработано  [archive] X.zip: archive_no_media", без
    переноса строки ни до, ни нормально после). Вместо того чтобы полагаться на этот
    внутренний tqdm-механизм сопоставления потоков, работаем напрямую с уже отслеживаемыми
    нами `_ACTIVE_BARS` -- та же последовательность действий (очистить/напечатать/
    перерисовать), которую сделал бы `tqdm.write()`, если бы правильно узнал бар.

    2026-08-24 (живой репорт пользователя, "плывущая" статус-строка -- склейка "Расп"/"аковка"
    и отдельно "[DVD]" без переноса строки): диагностика (временный ctypes-замер реальной
    позиции курсора + id() самого списка `_ACTIVE_BARS`, обе метрики убраны из кода после
    находки) показала, что этот список на живом `.exe` иногда пуст ИМЕННО в момент вызовов
    SourceWalker'а, хотя бар реально виден на экране -- id() отличался от id() того же имени
    списка, к которому реально аппендит `ProgressReporter.__enter__()`, то есть в процессе
    существуют ДВЕ независимые копии модуля (самого self-import'а в файле нет -- вероятная
    связь с PyInstaller onefile/multiprocessing.freeze_support(), корень НЕ найден, см.
    SESSION-HANDOFF.txt). Из-за этого `if _ACTIVE_BARS:` ниже периодически берёт `else`-ветку,
    даже когда бар активен -- защита клеится ненадёжно. Прагматичный фикс (предложен
    пользователем, не ждать находки корня) -- на стороне ВЫЗЫВАЮЩЕГО кода: SourceWalker's
    редкие/важные уведомления идут через `_log_own_line()` (см. её докстринг), которая сама
    форсирует "\\n" перед текстом -- гарантированно своя строка независимо от того, сработает
    ли защита `_ACTIVE_BARS` здесь."""
    if _ACTIVE_BARS:
        for b in _ACTIVE_BARS:
            if b._bar is not None:
                b._bar.clear()
        print(str(msg), file=sys.stderr)
        for b in _ACTIVE_BARS:
            if b._bar is not None:
                b._bar.refresh()
    else:
        log(msg)


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short), ("Top", ctypes.c_short),
        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    """CONSOLE_SCREEN_BUFFER_INFO (WinAPI) -- определение самой структуры чистый ctypes, без
    обращения к windll на этапе импорта, так что модуль по-прежнему безопасно импортировать
    на Linux (dev/тесты/CI) -- тот же принцип, что и у остального кода в файле (см.
    _prevent_sleep(): `if os.name != "nt"` гейт перед реальными WinAPI-вызовами, не перед
    определением)."""
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


_STD_OUTPUT_HANDLE = -11
_FOREGROUND_RED_BRIGHT = 0x0004 | 0x0008
_BACKGROUND_MASK = 0x00F0
_kernel32_console_prototypes_set = False


def _configure_kernel32_console_prototypes():
    """GetStdHandle возвращает HANDLE (указательного размера) -- без явного restype=c_void_p
    ctypes по умолчанию считает его 32-битным c_int и обрежет значение на 64-битном Windows.
    Настраивается один раз (флаг), только на Windows -- на остальных платформах ctypes.windll
    не существует вообще, обращаться к нему нельзя даже под гейтом os.name без короткого
    замыкания (см. вызывающих -- все проверяют os.name первым)."""
    global _kernel32_console_prototypes_set
    if _kernel32_console_prototypes_set:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetStdHandle.argtypes = [ctypes.c_uint]
    kernel32.GetConsoleScreenBufferInfo.restype = ctypes.c_int
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO),
    ]
    kernel32.SetConsoleTextAttribute.restype = ctypes.c_int
    kernel32.SetConsoleTextAttribute.argtypes = [ctypes.c_void_p, ctypes.c_ushort]
    _kernel32_console_prototypes_set = True


def _console_stdout_handle():
    """WinAPI-хендл STDOUT для прямой работы с цветом консоли (SetConsoleTextAttribute) --
    None везде, где это не применимо (не Windows, вывод не в реальный терминал) -- тот же
    гейт, что и isatty() в console_log()/_terminal_wrap_width(), намеренно не красим при
    перенаправлении в файл/пайп."""
    if os.name != "nt" or not sys.stdout.isatty():
        return None
    _configure_kernel32_console_prototypes()
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    except Exception:
        return None
    if not handle:
        return None
    return handle


def _get_console_screen_buffer_info(handle):
    info = _CONSOLE_SCREEN_BUFFER_INFO()
    try:
        ok = ctypes.windll.kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info))
    except Exception:
        return None
    return info if ok else None


def _get_console_attributes(handle):
    info = _get_console_screen_buffer_info(handle)
    return info.wAttributes if info is not None else None


def _set_console_attributes(handle, attributes) -> bool:
    try:
        return bool(ctypes.windll.kernel32.SetConsoleTextAttribute(handle, attributes))
    except Exception:
        return False


@contextlib.contextmanager
def _console_red_text():
    """Ярко-красный текст для одной строки-ошибки (см. console_log()) -- меняет ТОЛЬКО
    foreground-нибл текущего атрибута консоли (сохраняя фон как есть, каким бы он ни был),
    восстанавливает ИСХОДНЫЙ полный атрибут после строки -- иначе подсветка "протекла" бы на
    все последующие строки лога до конца прогона (REVIEW-HANDOFF.md Раунд 15, п.3).

    2026-07-19: белый фон при голом запуске (_console_bare_launch_colors(), тот же Раунд 15)
    сделан и откачен в этой же сессии -- живая проверка на реальной машине показала, что
    видимое поведение слишком зависит от хоста консоли (легаси conhost.exe vs Windows
    Terminal/ConPTY): мигание чёрным перед выходом (диагностировано и было бы почти исправлено
    через GetConsoleProcessList -- пропустить restore, если процесс единственный владелец
    консоли), но статус-строка прогресса местами всё равно отображалась чёрной, хотя
    WinAPI-хендл при прямой проверке через CONOUT$ читал правильный атрибут (0xF0) -- то есть
    расхождение было на уровне рендеринга терминала, не в этом коде, и надёжно проверить это
    для всех хостов консоли, которые встретятся у реальных пользователей, здесь нельзя. Красная
    подсветка ОШИБКА (эта функция) жалоб не вызвала -- оставлена."""
    handle = _console_stdout_handle()
    if handle is None:
        yield
        return
    original = _get_console_attributes(handle)
    if original is None:
        yield
        return
    colored = (original & _BACKGROUND_MASK) | _FOREGROUND_RED_BRIGHT
    if not _set_console_attributes(handle, colored):
        yield
        return
    try:
        yield
    finally:
        _set_console_attributes(handle, original)


def _console_columns(fallback: int = 80) -> int:
    """2026-08-23, живая находка пользователя (статус-строка сборки внезапно теряла
    время/своб.место/скорость, ВСЕГДА, независимо от реального размера окна -- даже
    развёрнутого на весь экран): `shutil.get_terminal_size()` внутри читает
    `sys.__stdout__.fileno()` -- `sys.__stdout__` (в отличие от переприсваиваемого
    `sys.stdout`) заморожен на момент старта интерпретатора и НИКОГДА не обновляется. Для
    windowed-сборки (`build.bat`, `--windowed`, закреплено 2026-08-22) `sys.__stdout__` при
    старте процесса -- `None` (окна консоли ещё не существует, см.
    `_configure_windows_stdio_at_startup()`), и остаётся `None` даже после того, как
    `_ensure_work_console()` создаёт консоль (`AllocConsole()`) и переоткрывает ТЕКУЩИЙ
    `sys.stdout`/`sys.stderr` на `CONOUT$` -- `sys.__stdout__` эту переоткрытую консоль не
    видит вовсе. `shutil.get_terminal_size()` в этом случае тихо (без исключения, без лога)
    возвращает переданный `fallback` -- программа вела себя так, будто консоль ВСЕГДА ровно
    80 колонок, что бы реально ни было на экране (подтверждено эмпирически: `os.get_terminal_
    size(sys.stdout.fileno())` в той же живой консоли корректно вернул 120 -- реальную ширину
    -- пока `shutil.get_terminal_size()` тем же моментом падал на fallback). До windowed-
    сборки эта разница была не видна -- процесс всегда стартовал с уже привязанной реальной
    консолью (унаследованной от родительского терминала), `sys.__stdout__` был валиден с
    самого начала. Фикс -- спрашивать ширину напрямую у ТЕКУЩЕГО `sys.stdout` (`os.
    get_terminal_size()`, не `shutil`-обёртку вокруг `sys.__stdout__`), тот же приём, что уже
    подтверждён живым тестом выше по этому докстрингу."""
    try:
        return os.get_terminal_size(sys.stdout.fileno()).columns
    except (AttributeError, ValueError, OSError):
        return fallback


def _terminal_wrap_width(fraction: float = 2 / 3, min_width: int = 40) -> int:
    """2026-07-12, интерфейс: пользователь пожаловался, что длинные строки в терминальном
    окне смотрятся некрасиво -- ограничиваем СВОИ переносы 2/3 реальной ширины терминала,
    а не даём тексту растягиваться во всю доступную ширину/заворачиваться терминалом как
    попало."""
    columns = _console_columns()
    return max(min_width, int(columns * fraction))


def _wrap_console_text(text: str, width: int) -> str:
    """Построчный word-wrap с висячим отступом под уже имеющийся ведущий отступ строки
    (пункты меню продолжают визуально выделяться, а не съезжают к левому краю). Пустые
    строки и строки короче width не трогаем. break_long_words/break_on_hyphens=False --
    "слово" без пробелов длиннее width (путь, хеш, декоративная рамка из "=") остаётся на
    своей строке как есть, а не режется посередине."""
    out_lines = []
    for line in str(text).split("\n"):
        if not line.strip() or len(line) <= width:
            out_lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        wrapped = textwrap.wrap(
            line.strip(), width=width,
            initial_indent=indent, subsequent_indent=indent + "  ",
            break_long_words=False, break_on_hyphens=False,
        )
        out_lines.append("\n".join(wrapped) if wrapped else line)
    return "\n".join(out_lines)


def console_log(msg):
    """Однопараметровый log()-callable для передачи как log= в run_for_source()/
    run_analyze_for_source() из main() -- единственное место, где нужно завернуть print в
    log_line(); все вложенные log() ниже по конвейеру уже используют именно этот переданный
    им callable, так что оборачивать нужно только тут (включая перенос длинных строк -- см.
    _wrap_console_text()). isatty()-гейт: перенос -- забота о реальном терминальном окне
    пользователя, поэтому включается только когда stdout реально tty, тем же паттерном, что
    уже использует ProgressReporter.is_tty выше -- при перенаправлении в файл/пайп текст
    остаётся как есть, ничем не отличаясь от поведения до этой правки.

    2026-07-19 (REVIEW-HANDOFF.md Раунд 15, "цвет консоли"): строки, начинающиеся (после
    отступа) с "ОШИБКА" -- единый признак ошибки по всей кодовой базе, см. _console_red_text()
    -- печатаются ярко-красным. Крэш-хендлер и оба места, где раньше было строчное "ошибка ...",
    приведены к этому же префиксу отдельно (см. _log_unexpected_crash(), :1678, :4247), чтобы
    реально попасть под это условие, а не только новые строки.

    2026-08-24, живая просьба пользователя ("текст сливается с рамкой окна"): ведущий пробел
    на КАЖДОЙ строке -- те же соображения, что и у ведущего пробела в поле операции статус-
    строки (см. _PHASE_DESC_MAX_LEN рядом), только здесь для обычных console_log()-строк
    (баннеры, "Оцениваю объём работы", разделители "===...", "Найден остаток...", ошибки).
    Строки объект-лога/редких уведомлений SourceWalker'а ("  [archive]"/"  Распаковка"/т.п.,
    см. write_object_line()/write_heavy_notice()) НЕ проходят через console_log() вовсе (прямой
    print() на bar) -- у них уже есть собственный 2-пробельный отступ, задевать не нужно. Тот же
    isatty()-гейт, что и у переноса строк ниже -- при перенаправлении в файл/пайп лишний пробел
    не нужен, ничего не отличается от поведения до этой правки."""
    text = str(msg)
    if sys.stdout.isatty():
        text = _wrap_console_text(text, _terminal_wrap_width())
        text = "\n".join(" " + line for line in text.split("\n"))
    if text.lstrip().startswith("ОШИБКА"):
        with _console_red_text():
            log_line(text, log=print)
    else:
        log_line(text, log=print)


def _fatal_messagebox(text: str) -> None:
    """Последний рубеж сообщения об ошибке -- на случай, если НИ GUI (tkinter), НИ обычная
    консоль (print()/console_log()) не показывают ничего пользователю. 2026-08-22, по прямой
    просьбе пользователя, build.bat теперь собирает WINDOWED (`--windowed`) .exe (см.
    _configure_windows_stdio_at_startup()'s докстринг) -- то, что этот докстринг раньше называл
    гипотетическим будущим ("если exe когда-нибудь соберут как windowed"), теперь реальность
    голого запуска: sys.stdout/stderr там всегда указывают на os.devnull, дубль в stderr ниже
    печатается, но никуда не долетает -- сознательно принятая пользователем цена (см. тот же
    докстринг), это окно теперь ЕДИНСТВЕННЫЙ реальный канал для голого запуска, не один из
    двух. ctypes.windll.user32.MessageBoxW ни от os.devnull, ни от какого-либо ещё канала не
    зависит, тот же локальный-импорт паттерн, что и у _prevent_sleep()/gui_menu.py. No-op вне
    Windows -- вызывать ТОЛЬКО из _main()'s голого-запуска ветки после того, как GUI и
    текстовое меню уже подвели."""
    if os.name == "nt":
        try:
            import ctypes
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(None, text, "PhotoArchive", MB_OK | MB_ICONERROR)
        except Exception:
            pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


_console_freed_for_gui = False
_work_console_allocated = False
_last_bare_launch_object_count = 0  # 2026-08-23, по прямой просьбе пользователя: количество
# объектов последнего прогона [1]/[2]/[3]/[4] голого меню, для показа в GUI-нотисе "Работа
# окончена" (см. gui_menu._make_ok_input_fn()). Модульная переменная, не изменение сигнатуры
# возврата _bare_launch_run_*() -- те же функции вызываются и из текстового режима/CLI
# (run_bare_launch() в этом файле, analyze --target), которым число не нужно; тот же принцип,
# что уже применён к _console_freed_for_gui/_work_console_allocated выше (кросс-cutting
# состояние процесса, не стоит того, чтобы менять сигнатуру для всех вызывающих). Каждая из
# четырёх функций выставляет её ПРЯМО ПЕРЕД успешным return (не в except/None-ветках -- там
# нотис в GUI и так не покажется, report_path=None). GUI-код читает её СРАЗУ после вызова,
# до того как что-либо ещё успеет её перезаписать (следующий _bare_launch_run_*() вызывается
# только на следующей итерации цикла, после того как нотис уже закрыт).


def _configure_windows_stdio_at_startup(has_cli_args: bool) -> None:
    """2026-08-22, по прямой просьбе пользователя -- предыдущая версия этой функции
    (`_free_console_for_gui_bare_launch()`, hide-after-the-fact через ShowWindow/FreeConsole)
    минимизировала, но НЕ убирала чёрный мелькающий экран консоли перед голым GUI-запуском:
    build.bat собирал КОНСОЛЬНУЮ сборку (`console=True`), а консольная сборка означает, что
    ОС создаёт видимое окно консоли ДО того, как вообще начинает исполняться код Python --
    спрятать его можно только ПОСЛЕ того, как этот код успел добежать до вызова, а не
    предотвратить появление. Живой клик-тест подтвердил: экран всё равно виден и гаснет,
    именно то, что пользователь прямо попросил убрать целиком -- "если для этого надо
    отказаться от текстового резерва -- значит отказываемся" (текстовый резерв -- см. ниже).

    Настоящий фикс -- на уровне СБОРКИ, не только кода: build.bat теперь собирает WINDOWED
    (`--windowed`) .exe -- Windows вообще НИКОГДА не создаёт консоль для этого процесса сама,
    ни при каких обстоятельствах (в отличие от консольной сборки, где создание окна -- не
    решение приложения). Это и убирает мелькание полностью, а не просто прячет его быстрее.

    Цена: "текстовый резерв" (см. _fatal_messagebox()'s докстринг) -- та самая автосозданная
    консоль, служившая ПОСЛЕДНИМ запасным каналом для дублирования сообщения об ошибке через
    stderr, если ни GUI, ни сам MessageBoxW почему-то не показались бы -- этого канала больше
    физически не существует для голого запуска (has_cli_args=False): sys.stdout/stderr здесь
    указывают на os.devnull, попытка print() в них проваливается молча, не падает. Пользователь
    явно принял эту цену ради полного отсутствия мелькания -- MessageBoxW остаётся единственным
    каналом сообщения об ошибке для голого запуска.

    ДЛЯ РЕАЛЬНОГО CLI-ПУТИ (has_cli_args=True, например `PhotoArchive.exe --source X --target
    Y` из существующего терминала) windowed-сборка означала бы то же самое молчание -- и это
    было бы настоящей регрессией функциональности, не просто внешним видом, поэтому CLI-путь
    ЯВНО отличается: `AttachConsole(ATTACH_PARENT_PROCESS)` подключается к консоли ТОГО
    терминала, откуда реально запустили .exe (если он есть -- голый двойной клик через
    Проводник никакой консоли-родителя не имеет, AttachConsole тогда просто не срабатывает, и
    CLI-путь тоже проваливается в devnull, что ожидаемо для запуска без какого-либо терминала).
    При успехе sys.stdout/stderr/stdin переоткрываются на CONOUT$/CONIN$ -- тот же приём, что и
    у _ensure_work_console() ниже (AllocConsole() там создаёт НОВУЮ консоль для реальной
    обработки; здесь -- подключение к УЖЕ существующей консоли вызывающего терминала, разные
    API, одинаковый способ переоткрыть stdio после).

    `_console_freed_for_gui=True` выставляется здесь БЕЗУСЛОВНО (было -- только когда GUI
    реально подтверждён) -- при windowed-сборке консоли никогда не было с самого начала
    процесса, семантика "консоль уже недоступна для чтения" (см. _should_pause_before_exit())
    верна с первой же строки, ждать подтверждения GUI больше незачем.

    КРИТИЧНО: no-op не только вне Windows, но и вне FROZEN-сборки (`sys.frozen`) -- вся эта
    функция существует ТОЛЬКО чтобы компенсировать отсутствие консоли у windowed PyInstaller-
    бутлоадера. Обычный dev-запуск (`python photosort_win.py --version`, тот же путь, которым
    идут ci/windows_ci_test.py's subprocess.run()-тесты на самом Windows CI-раннере) -- ОБЫЧНЫЙ
    консольный `python.exe`, sys.stdout уже указывает на что-то рабочее -- трогать его не нужно.

    ВТОРАЯ, отдельная проверка -- `sys.stdout is None` -- нужна ДАЖЕ на frozen windowed-сборке:
    живой запуск собранного `PhotoArchive.exe` через `subprocess.run([EXE, ...],
    capture_output=True)` (именно так работает ci/smoke_test_exe.py -- тестирует УЖЕ СОБРАННЫЙ
    .exe, не dev-скрипт, sys.frozen там True) поймал реальный баг первой версии этой функции:
    windowed-бутлоадер PyInstaller УЖЕ подключает sys.stdout/stderr к предоставленным вызывающим
    пайпам, если они были явно переданы через STARTUPINFO (тот же механизм, каким
    `subprocess.run(capture_output=True)`/`Start-Process -RedirectStandardOutput` перенаправляют
    вывод дочернего процесса, работает независимо от console/windowed подсистемы) -- в этом
    случае sys.stdout НЕ None, это уже рабочий объект. Без проверки функция всё равно пробовала
    AttachConsole(), который в этом случае РЕАЛЬНО УСПЕВАЕТ (процесс ещё ни к чему не прикреплён
    -- пайп это не консоль) и подключается к консоли вызывающего терминала -- дальше код
    переоткрывал sys.stdout на CONOUT$ этой консоли, ЗАМЕНЯЯ уже рабочий пайп -- вывод уходил в
    невидимую для теста консоль, `subprocess.run()`'s захваченный `.stdout` оставался пустым.
    Живой прогон (`Start-Process -RedirectStandardOutput`, эмулирует ci/smoke_test_exe.py)
    воспроизвёл именно это ДО фикса -- пустой захваченный вывод -- и подтвердил чистый после
    (см. коммит). Только когда sys.stdout ДЕЙСТВИТЕЛЬНО None (bootloader не получил ни одного
    валидного хендла -- настоящий голый запуск из терминала без перенаправления, ГДЕ и нужен
    сам AttachConsole) -- функция вообще что-то трогает; если уже рабочий -- не трогается совсем,
    независимо от has_cli_args.

    No-op вне Windows и best-effort (проглатывает сбой ctypes) -- тот же паттерн, что и у
    остальных win32-хелперов этого файла (_fatal_messagebox()/_reclaim_console_focus())."""
    global _console_freed_for_gui
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    _console_freed_for_gui = True
    if sys.stdout is not None:
        # Вызывающий уже явно перенаправил вывод (pipe/файл) -- уже рабочий, не трогаем вообще.
        return
    attached = False
    if has_cli_args:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.AttachConsole.restype = ctypes.c_int
            kernel32.AttachConsole.argtypes = [ctypes.c_uint32]
            ATTACH_PARENT_PROCESS = 0xFFFFFFFF
            attached = bool(kernel32.AttachConsole(ATTACH_PARENT_PROCESS))
        except Exception:
            attached = False
        if attached:
            try:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            except Exception:
                attached = False
    if not attached:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
        except Exception:
            pass


_console_close_handler_ref = None  # см. _install_console_close_handler() -- держит ссылку на
# ctypes-коллбэк живой на весь процесс: если её не хранить в переменной уровня модуля,
# CFUNCTYPE-объект соберёт сборщик мусора, и Windows будет звать уже освобождённую память при
# следующем событии консоли (классическая ловушка ctypes-коллбэков, не паранойя).


def _install_console_close_handler() -> None:
    """2026-08-24, живая просьба пользователя ("крестик/Ctrl-C в окне терминала должны давать
    полный выход, сейчас не так") -- вызывается ОДИН раз, сразу после AllocConsole() в
    _ensure_work_console() выше. Без этой функции Windows всё равно должна закрывать процесс
    при CTRL_CLOSE_EVENT (крестик на окне консоли/Windows Terminal) через дефолтное действие --
    штатный обработчик CPython (см. Modules/signalmodule.c) реагирует только на CTRL_C_EVENT/
    CTRL_BREAK_EVENT и возвращает FALSE на всё остальное, отдавая CTRL_CLOSE_EVENT дефолтному
    действию ОС (завершить процесс) -- но это НЕ гарантирует мгновенности (грейс-период до
    нескольких секунд) и не единственный подозреваемый механизм в этой находке (см. соседний
    фикс exit-кода в main()'s except KeyboardInterrupt -- Windows Terminal с настройкой
    "закрывать по завершении: изящно" по умолчанию НЕ закрывает вкладку сама, если процесс
    вышел с ненулевым кодом, что тоже могло выглядеть как "не полный выход").

    2026-08-24, второй заход (живой вопрос пользователя: "разве нет нормального способа
    отработки крестика?") -- CTRL_CLOSE_EVENT больше не зовёт os._exit(0) напрямую. Голый
    os._exit() гарантированно быстр, но и абсолютно "грязен": обходит весь обычный питоновский
    shutdown, а вместе с ним -- и cleanup, который PyInstaller-бутлоадер для onefile-сборки
    выполняет ПОСЛЕ возврата из Python-кода (удаление распакованной `_MEIxxxxxx` во временной
    папке), и except-блоки place_file()/_handle_archive() (см. их докстринги), которые убирают
    за собой при обычном исключении, но не переживают обход через os._exit(). Настоящая
    "нормальная" обработка -- _thread.interrupt_main(): та же функция, которой Windows уже
    доставляет Ctrl-C (штатный обработчик CPython, Modules/signalmodule.c, реагирует именно
    так) -- поднимает KeyboardInterrupt в главном потоке при первой же проверке ожидающих
    вызовов, тот же путь, что и обычный Ctrl-C/крестик нотиса "Работа окончена"
    (main()'s except KeyboardInterrupt -> _hide_work_console_for_exit() -> sys.exit(0), см. её
    докстринг) -- то есть немедленное скрытие консоли, но ПОЛНОЦЕННЫЙ, а не оборванный, выход.
    Главный поток НЕ обязательно завис на Tk mainloop() -- тик-таймер (`root.after(200, ...)`,
    и у мастера, и у нотиса) в худшем случае даёт ~200мс задержку до реакции; во время реальной
    обработки (не-GUI Python-цикл) прерывание сработает ещё быстрее, тем же способом, каким уже
    работает обычный Ctrl-C посреди сборки.

    Есть и встроенный запасной вариант: если по какой-то причине interrupt_main() не сработал
    (совсем экзотический случай -- главный поток заблокирован в некотором C-вызове без единой
    точки возврата в Python), ОС сама принудительно завершает процесс по CTRL_CLOSE_EVENT самое
    большее через несколько секунд (тот же грейс-период, что описан в первом абзаце выше для
    отсутствующего обработчика вообще) -- то есть худший случай без этой функции не хуже, чем
    был ДО неё, а обычный случай -- быстрее и чище одновременно.

    CTRL_LOGOFF_EVENT/CTRL_SHUTDOWN_EVENT (в отличие от CTRL_CLOSE_EVENT) НЕ переведены на
    interrupt_main() -- это system-wide события (выход из системы/выключение), где бюджет
    времени общий на все процессы сразу, не только наш; гарантированный мгновенный os._exit(0)
    здесь остаётся более безопасным выбором, чем более быстрый, но не строго мгновенный путь
    через главный поток.

    Обработчики консоли вызываются в ОБРАТНОМ порядке регистрации (последний
    зарегистрированный -- первым) -- наш, добавленный уже ПОСЛЕ интерпретатора, получает
    событие раньше штатного. Для CTRL_C_EVENT/CTRL_BREAK_EVENT возвращаем 0 (FALSE, "не
    обработано") -- эти два оставлены штатному обработчику CPython как и раньше (обычный
    KeyboardInterrupt, пауза для отчёта Ctrl-C-посреди-сборки и т.д. не должны меняться).

    No-op вне Windows и best-effort -- тот же паттерн, что и у остальных win32-хелперов этого
    файла. Требует живого клик-теста (крестик на минимизированной/развёрнутой консоли, и в
    простое на экране мастера, и посреди реальной обработки) -- не проверяется pytest'ом, тот
    же класс ограничения, что и у остального Windows-специфичного поведения gui_menu.py."""
    global _console_close_handler_ref
    if os.name != "nt":
        return
    try:
        import _thread
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _handler(event):
            if event == CTRL_CLOSE_EVENT:
                _thread.interrupt_main()
                return 1
            if event in (CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                os._exit(0)
            return 0

        _console_close_handler_ref = HANDLER_ROUTINE(_handler)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE, ctypes.c_int]
        kernel32.SetConsoleCtrlHandler.restype = ctypes.c_int
        kernel32.SetConsoleCtrlHandler(_console_close_handler_ref, True)
    except Exception:
        pass


def _ensure_work_console() -> None:
    """Парная функция к _configure_windows_stdio_at_startup() -- вызывается ОДИН раз за весь
    процесс, из gui_menu.run_bare_launch() сразу после того, как мастер вернул выбор
    пользователя (мастер уже закрыт, реальная обработка -- scan/dry_run/build/passport -- вот-
    вот начнётся и должна быть видна). AllocConsole() создаёт новое окно консоли (windowed-
    сборка, см. build.bat, сама по себе никогда её не создаёт -- см. докстринг
    _configure_windows_stdio_at_startup()); sys.stdout/stderr/stdin переоткрываются на
    CONOUT$/CONIN$ (просто переприсвоить sys.stdout НЕ восстанавливает Win32-хендлы
    STD_*_HANDLE, которые напрямую использует _console_stdout_handle() для покраски "ОШИБКА" --
    но AllocConsole(), по документации MSDN, сам сбрасывает STD_INPUT_HANDLE/STD_OUTPUT_HANDLE/
    STD_ERROR_HANDLE на новую консоль, если они не были явно ПЕРЕНАПРАВЛЕНЫ -- переоткрытых
    через open() здесь достаточно, отдельный SetStdHandle() не требуется).

    САМО СОЗДАНИЕ окна (AllocConsole()) идемпотентно (флаг выставляется ДО самого вызова, тот же
    приём, что и у gui_menu._configure_dpi_awareness()) -- цикл run_bare_launch() возвращается к
    мастеру и может пройти через эту точку много раз за сессию, AllocConsole() нужен только один
    раз. Окно между прогонами сворачивается (см. _hide_work_console() -- зовётся из
    gui_menu.run_bare_launch() в НАЧАЛЕ каждой итерации цикла, пока пользователь снова в мастере)
    -- поэтому на ВСЕХ итерациях, включая повторные, эта функция безусловно РАЗВОРАЧИВАЕТ окно
    заново (ShowWindow(SW_RESTORE) + SetForegroundWindow()), не только создаёт его при первом
    вызове.

    2026-08-23, по прямой просьбе пользователя: SW_SHOW/SW_HIDE (окно физически исчезает, без
    следа на панели задач) заменены на SW_RESTORE/SW_MINIMIZE (окно сворачивается в панель
    задач, как обычное) -- пользователь явно попросил именно такую модель ("окно консоли не
    исчезает совсем, а сворачивается") для этой же связки функций (см. _hide_work_console()).
    Той же правкой убран прежний блокирующий input()-механизм на этой консоли для Ctrl-C/краша
    (main(), except-ветки) -- окно теперь чисто визуальная приборная панель, ни один код нигде
    не ждёт от неё нажатия клавиши.

    No-op вне Windows и best-effort -- тот же паттерн, что и _configure_windows_stdio_at_startup()."""
    global _work_console_allocated
    if os.name != "nt":
        return
    if not _work_console_allocated:
        _work_console_allocated = True
        try:
            ctypes.windll.kernel32.AllocConsole()
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:
            return
        _install_console_close_handler()
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        SW_RESTORE = 9
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _hide_work_console() -> None:
    """Парная функция к _ensure_work_console() выше -- 2026-08-22, по прямой просьбе
    пользователя: пока пользователь снова в мастере gui_menu._Wizard (шаги 1-2, и сам шаг 3 до
    финального подтверждения), окно консоли с логом ПРОШЛОГО прогона не должно мешать -- сворачи-
    ваем его (ShowWindow(SW_MINIMIZE)), не закрываем. Закрытие (FreeConsole()) потребовало бы
    заново переоткрывать sys.stdout/stderr/stdin через CONOUT$/CONIN$ на КАЖДУЮ итерацию цикла
    (тот же приём, что и в _ensure_work_console(), но выполненный лишний раз без необходимости)
    и потеряло бы прокрученную историю лога окна -- просто свернуть/развернуть то же самое окно
    проще и надёжнее.

    2026-08-23, по прямой просьбе пользователя: SW_HIDE (окно исчезает целиком, без следа на
    панели задач) заменён на SW_MINIMIZE (обычное сворачивание, с иконкой на панели задач) --
    см. тот же комментарий в _ensure_work_console() выше.

    No-op, если консоль ещё ни разу не создавалась (_work_console_allocated=False -- самая
    первая итерация цикла, GetConsoleWindow() тогда всё равно вернул бы 0) или вне Windows."""
    if os.name != "nt" or not _work_console_allocated:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        SW_MINIMIZE = 6
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception:
        pass


def _hide_work_console_for_exit() -> None:
    """2026-08-24, живая находка пользователя ("крестик на нотисе 'Работа окончена' закрывает
    нотис, но оставляет терминальное окно") -- вызывается из main()'s except-веток (Keyboard
    Interrupt/_GuiExplicitExit/EOFError/Exception) ПРЯМО перед sys.exit(), для любого голого
    Windows-запуска, не только явного клика "Выход". Раньше единственным способом убрать саму
    рабочую консоль с экрана после решения выйти было дождаться, пока ОС сама закроет окно
    вместе с завершением процесса -- но у windowed onefile-сборки это не мгновенно
    (PyInstaller-бутлоадер чистит распакованный `_MEIxxxxxx` во временной папке уже ПОСЛЕ
    возврата из Python-кода, до пары секунд на этой сборке) -- пользователь успевал заметить
    "закрыл нотис, а окно консоли всё ещё висит на экране" в этом промежутке, хотя процесс
    объективно уже шёл к завершению, не завис. Разница с _hide_work_console() выше: та
    СВОРАЧИВАЕТ (SW_MINIMIZE) для сценария "вернулись в мастер, консоль ещё понадобится этой же
    сессии" -- здесь именно ПОЛНЫЙ ВЫХОД, окно прячется целиком (SW_HIDE), тот же эффект, что
    пользователь уже видит на экранах мастера 1-3 (там крестик ничего не оставляет на экране,
    потому что рабочая консоль в принципе ещё не создана -- см. _ensure_work_console()).

    No-op, если консоль ещё ни разу не создавалась, или вне Windows -- тот же паттерн, что и у
    _hide_work_console()."""
    if os.name != "nt" or not _work_console_allocated:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        SW_HIDE = 0
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def _check_pause_keypress(log=console_log) -> None:
    """2026-08-23, по прямой просьбе пользователя ("Ctrl-C -- только способ убить программу,
    без подтверждений; если нужно остановиться и изучить лог -- добавить паузу обработки по
    пробелу") -- отложенная часть той же переделки консоли GUI-мастера, что и
    _hide_work_console()/_ensure_work_console() выше (см. `CLAUDE.md`, "Рабочая консоль
    GUI-мастера..."). Реализована отдельным заходом сразу следом -- не архитектурно связана с
    show/hide, но продолжает ту же тему "что можно сделать с работающей консолью, не убивая
    процесс".

    Неблокирующий опрос клавиши (msvcrt.kbhit()/getch() -- Windows-only, обычный способ читать
    консольный ввод без реального блокирующего input()). Пробел -- единственная клавиша,
    которая что-то делает: входим в РЕАЛЬНУЮ блокирующую паузу (это и есть цель функции --
    getch() здесь блокирует намеренно, в отличие от остального кода этой сессии, который
    убирает блокирующий ввод из рабочей консоли), печатаем подсказку, ждём следующего нажатия
    ЛЮБОЙ клавиши, продолжаем.

    A (2026-08-28, живая находка пользователя). Раньше опрос стоял ТОЛЬКО между файлами
    верхнего цикла, а "продолжить" читалось обычным getch() из общего буфера консоли. Во
    время долгой операции (распаковка/хеш большого файла/обход содержимого архива) опрос не
    вызывался, пользователь не видел реакции и жал пробел несколько раз подряд -- все нажатия
    копились в буфере, и каждая пара "пробел+пробел" проигрывалась как мгновенный вход-выход
    из паузы, а конечное состояние зависело от чётности числа нажатий ("старт/стоп с
    неизвестным состоянием в конце"). Теперь: при входе в паузу буфер ПОЛНОСТЬЮ осушается,
    ждём ровно ОДНО свежее нажатие, на выходе осушаем ещё раз -- сколько бы раз ни нажали,
    ровно одна пауза, последняя строка на экране всегда честно говорит текущее состояние.
    Ctrl-C во время паузы (и вообще внутри опроса) завершает программу как обычно. Механизм
    зависит от режима консоли и покрыт с обеих сторон: если ENABLE_PROCESSED_INPUT включён
    (умолчание) -- ОС доставляет асинхронный KeyboardInterrupt в произвольной точке, его ловит
    тот же верхний обработчик _run_impl()/run_analyze() (-> отчёт + выход 130), а ветки
    `key == b"\\x03"` ниже просто недостижимы и безвредны; если консоль в raw-режиме -- Ctrl-C
    приходит байтом b"\\x03" через getch(), и эти ветки поднимают KeyboardInterrupt явно, тот
    же обработчик. (Первый путь -- недоказуем без реального Windows, REVIEW-HANDOFF.md Раунд
    148 придирка; на живую проверку, если будет боевой прогон именно с Ctrl-C во время паузы.)

    B (2026-08-28): вызывается не только между файлами верхнего цикла, но и внутри долгих
    операций -- обход содержимого архива (SourceWalker._walk_dir(), по папке и по файлу),
    расчёт sha256 большого файла (sha256_file(progress_cb=...), в т.ч. хеш архива перед
    распаковкой) -- чтобы пробел реагировал без минутных "мёртвых зон", из-за которых и
    начинался долбёж. Одиночный ffmpeg-вызов (video_phash_3frames) прервать нельзя -- там
    опрос только до/после, как у всего остального между-файлового.

    No-op вне Windows (msvcrt не существует) и best-effort (проглатывает любой сбой ctypes/
    msvcrt, КРОМЕ намеренного KeyboardInterrupt) -- не должна мочь сломать реальную обработку
    файлов, худший случай -- пауза просто не сработает."""
    if os.name != "nt":
        return
    try:
        import msvcrt
        if not msvcrt.kbhit():
            return
        key = msvcrt.getch()
        if key == b"\x03":
            raise KeyboardInterrupt
        if key != b" ":
            # любая другая клавиша (в т.ч. многобайтная спец-клавиша: b"\x00"/b"\xe0" + хвост)
            # -- осушить возможный хвост и выйти, не реагируя
            while msvcrt.kbhit():
                if msvcrt.getch() == b"\x03":
                    raise KeyboardInterrupt
            return
        # A: выбросить всё, что пользователь успел набить, пока шла операция -- иначе следующее
        # же буферизованное нажатие мгновенно снимет паузу
        while msvcrt.kbhit():
            if msvcrt.getch() == b"\x03":
                raise KeyboardInterrupt
        log("\n[ПАУЗА] Обработка остановлена. Нажмите любую клавишу, чтобы продолжить…")
        if msvcrt.getch() == b"\x03":
            raise KeyboardInterrupt
        # съесть хвост спец-клавиши, чтобы он не сработал как пробел на следующем опросе
        while msvcrt.kbhit():
            if msvcrt.getch() == b"\x03":
                raise KeyboardInterrupt
        log("[Продолжаю]\n")
    except KeyboardInterrupt:
        raise
    except Exception:
        pass


def _extraction_log_name_budget(min_width: int = 15) -> int:
    """Живой репорт пользователя (редизайн живого вывода Фазы 2, 2026-08-01): SourceWalker.
    _handle_archive()'s "  Распаковка <имя> (<X> ГБ)…" уходит в ProgressReporter.
    write_heavy_notice(), которая переносит строки, не влезающие в окно целиком, и длинное имя
    архива легко выталкивало эту строку за порог, реально перенося её на вторую физическую
    строку. tqdm-бар (см. log_line()) не знает об этом переносе -- его собственный clear()/
    refresh() рассчитан ровно на одну строку. Обрезаем имя так, чтобы строка гарантированно
    влезла в окно целиком.

    2026-08-29: бюджет от ПОЛНОЙ ширины окна (_console_columns()), не 2/3 -- согласовано с
    write_heavy_notice() и _console_tag_line_budget(), обе тоже считают от полной ширины.
    Минус фиксированная текст-обвязка строки (щедрый запас на трёхзначный ГБ-размер)."""
    if not sys.stdout.isatty():
        return 200  # нет реального терминала -- write_heavy_notice() не переносит строки вовсе
    reserve = len("  Распаковка ") + len(" (000.0 ГБ)…")
    return max(min_width, _console_columns() - reserve)


class _RussianRateStream:
    """Прокси над реальным stderr для tqdm (см. ProgressReporter, 2026-07-11 live-run
    finding): tqdm вычисляет rate_fmt/rate_noinv_fmt сам и БЕЗУСЛОВНО приклеивает английский
    суффикс "/s" (harcoded в tqdm.std.format_meter), а не наш unit="файл"/"с" -- получается
    смешение языков "файл/s". Нет публичного параметра tqdm, чтобы это переопределить;
    перехватываем на уровне write() и подменяем ТОЛЬКО этот конкретный суффикс."""

    def __init__(self, real_stream, unit):
        self._real = real_stream
        self._old = f"{unit}/s"
        self._new = f"{unit}/с"

    def write(self, s):
        return self._real.write(s.replace(self._old, self._new))

    def __getattr__(self, name):
        return getattr(self._real, name)


# ProgressReporter(two_line=True)'s "план"-сглаживание (2026-08-01, живой репорт
# пользователя) -- вес недавнего замера относительно накопленного EMA. 0.3 -- умеренное
# сглаживание: один файл-выброс (архив/видео) двигает среднее заметно, но не рывком, и полное
# "забывание" старого выброса занимает ~3-4 файла (1/alpha порядка), а не десятки/сотни, как у
# чистого кумулятивного среднего по всей истории прогона.
_EMA_RATE_ALPHA = 0.3

# Живой репорт пользователя (2026-08-01, боевой прогон D:\): статус-строку не нужно
# перерисовывать на каждом файле -- достаточно раз в 10-20 тиков (пользователь сам назвал этот
# диапазон). Не трогает частоту самого EMA-расчёта (см. update()) -- только то, как часто
# _build_two_line_status() (в т.ч. syscall своб.места, см. _two_line_free_str()) реально
# пересчитывается и уходит в set_description()/print().
_STATUS_REFRESH_EVERY_N = 15

# Фаза 2 two_line-статус-строки (см. _build_two_line_status()) -- resting-тексты операции для
# разных режимов (run_for_source()/run_analyze()). Раньше поле операции считалось ОДНОЙ общей
# шириной на максимум ВСЕХ этих текстов сразу (даже несовместимых, никогда не показывающихся в
# одном прогоне) -- на практике это означало, что короткий resting-текст (например, "analyze —
# метаданные источника", 30 символов) всё равно получал поле шириной под самый длинный из ВСЕХ
# ("analyze (Паспорт архива) — метаданные + хеширование", 51 символ) -- пустое место, которое
# реальный прогон никогда не заполнит. Речь пользователя, 2026-08-11: поле должно считаться по
# тому, что реально может появиться В ЭТОМ прогоне, не по глобальному максимуму -- см.
# ProgressReporter.__init__()'s self._op_field_width.
# 2026-08-24, живая просьба пользователя: название режима убрано из всех четырёх resting-
# текстов ниже -- теперь оно и так есть в шапке параметров запуска (_log_run_start_header(),
# печатается ДО этих строк, см. её докстринг), повторять не нужно, только тратит место в поле
# операции. Заодно исправлена неточность: "analyze (Паспорт архива) — ..." был единственным
# resting-текстом для mode=="analyze" НЕЗАВИСИМО от self_scan -- полный CLI "analyze --source"
# (self_scan=False, не паспорт вообще) показывал бы то же самое "(Паспорт архива)", что и
# реальный паспорт -- generic формулировка ниже верна для обоих случаев одинаково.
#
# 2026-08-24, второй заход (та же просьба, "очень длинное название"): "Хеширую и читаю
# метаданные файлов" (34 символа) всё ещё казалась длинной -- "читаю метаданные" и так
# подразумевается самим действием (та же метаданные-часть, что уже явно названа в analyze-quick
# ниже), хеширование -- единственное, что реально отличает этот проход от него, без остального
# смысл не теряется.
#
# 2026-08-24, третий заход (прямая просьба пользователя): жёсткий потолок длины для ВСЕХ
# resting-текстов ниже -- 2/3 от длины строки, которую пользователь назвал "очень длинной"
# ("Хеширую и читаю метаданные файлов", 33 символа, см. абзац выше) = 22 символа, округлено.
#
# 2026-08-24, четвёртый заход (та же просьба, "можно короче, если сможешь"): реальные значения
# ушли заметно ниже потолка -- голый глагол без объекта на каждый режим (Проверяю/Копирую/
# Сканирую/Хеширую), объект и так понятен из контекста (шапка запуска называет режим, соседние
# поля той же строки уже показывают "всего медиа"/% -- дальше сокращать здесь означало бы уже
# терять смысл, не только длину). _PHASE_DESC_MAX_LEN остаётся потолком НА БУДУЩЕЕ (см.
# test_all_phase_descs_fit_the_length_cap в tests/test_progress_phase2.py -- держит все четыре
# константы под ним, не только на момент этой правки), не целевым значением для каждой строки.
#
# 2026-08-24, пятый заход (SESSION-HANDOFF.txt, "ты не доделал -- текст кое-где поменял, но
# размеры поля не сократил"): транзиентные тексты ниже (_DEFERRED_CONTENT_TRANSIENT_OP/
# _ARCHIVE_CONTENT_TRANSIENT_OP/распаковка-формат в _handle_archive()) сокращены тем же стилем
# -- черновые варианты "Смотрю"/"В архиве"/"Извлекаю (999.9ГБ)" из прошлого раунда диагностики
# применены как есть. Заодно (живая просьба пользователя, тот же заход) -- ведущий пробел перед
# КАЖДЫМ текстом, способным занять поле операции (все resting-тексты ниже И оба транзиентных
# текста, И формат "Извлекаю (X)"), иначе поле операции сливалось с левой рамкой окна консоли на
# глаз -- единообразно, не только для части текстов, иначе отступ то появлялся бы, то пропадал
# при переключении resting/transient. Ширина поля/`_PHASE_DESC_MAX_LEN`-проверки считаются по
# строкам УЖЕ С этим пробелом (он часть самой константы, не добавляется отдельно при выводе).
_PHASE_DESC_MAX_LEN = 22
_DRY_RUN_PHASE_DESC = " Проверяю"
_BUILD_PHASE_DESC = " Копирую"
_ANALYZE_QUICK_PROGRESS_DESC = " Сканирую"
_ANALYZE_PASSPORT_PROGRESS_DESC = " Хеширую"
# Единственные тексты, способные ВРЕМЕННО заменить resting-desc в этом же поле (см.
# SourceWalker's transient_op_cb -- _handle_archive()/_open_deferred_gap()) -- нужны в расчёте
# ширины поля наравне с resting-desc, иначе именно они переполнялись бы после того, как поле
# сузили под resting-текст. "999.9ГБ" -- заведомо щедрый потолок под _fmt_size_gb() (реальные
# архивы на 4+ значных ГБ практически не встречаются, а если встретятся -- строка всё равно не
# порвётся, просто вернётся старое поведение "переполнение чинит рантайм-обрезка ниже").
_DEFERRED_CONTENT_TRANSIENT_OP = " Смотрю"
_ARCHIVE_EXTRACT_TRANSIENT_OP_MAX_LEN = len(" Извлекаю (999.9ГБ)")
# 2026-08-19, живая находка пользователя: "разбор архива" -- transient-op, держащийся ВЕСЬ
# обход распакованного содержимого архива (см. SourceWalker._archive_walk_depth), в т.ч. как
# фолбэк _close_deferred_gap() вместо None, пока обход идёт внутри архива -- иначе тот
# безусловно гасил бы эту пометку в общий resting-desc на каждом первом реальном файле любой
# папки внутри архива (см. её докстринг).
_ARCHIVE_CONTENT_TRANSIENT_OP = " В архиве"
_MAX_TRANSIENT_OP_LEN = max(len(_DEFERRED_CONTENT_TRANSIENT_OP), _ARCHIVE_EXTRACT_TRANSIENT_OP_MAX_LEN,
                             len(_ARCHIVE_CONTENT_TRANSIENT_OP))


_CONSOLE_TAG_LINE_SAFETY_MARGIN = 8  # см. докстринг _console_tag_line_budget()


def _console_tag_line_budget(tail_len: int, min_width: int = 15, tag_width: int = 10) -> int:
    """Бюджет под путь для однострочных построчных сообщений с общим 2-пробельным отступом
    ("  [archive] "/"  [папка]   ", см. ProgressReporter._format_object_line()) и фиксированным
    тегом -- общая часть ProgressReporter._object_line_budget()/SourceWalker._log_archive()
    (SESSION-HANDOFF.txt, 2026-08-05, боевой прогон п.2: у _log_archive() не было аналогичной
    обрезки, длинные пути переносились некрасиво посреди слова самим терминалом). tail_len --
    точная длина хвостового текста ПОСЛЕ пути (например ": найдено медиафайлов 0") -- оба
    вызывающих места знают свой хвост заранее и могут измерить его, не гадать общий запас.

    tag_width (2026-08-09, живая находка пользователя -- буквы A/D после тега): по умолчанию
    10 -- ширина тега БЕЗ буквы решения ("[archive] "/"[папка]   ", см. _log_archive(), её
    формат не меняется). ProgressReporter._object_line_budget() передаёт 11 явно -- см. её
    докстринг за тем, откуда взялась именно эта ширина ("[archive]A "/"[папка]A   ", когда
    буква показывается).

    SESSION-HANDOFF.txt (2026-08-09, боевой прогон, третья находка): _CONSOLE_TAG_LINE_SAFETY_MARGIN
    -- запас прочности сверх точной длины хвоста, ОБЩИЙ для обоих вызывающих мест (раньше
    _object_line_budget() зашивал свои лишние +8 в СОБСТВЕННЫЙ tail_reserve ДО вызова этой
    функции, а _log_archive() передавал точный хвост совсем без запаса -- на одинаковой ширине
    терминала это давало РАЗНЫЙ бюджет [archive]/[папка] и перенос строки там, где у [папка] с
    её более широким запасом такого не случалось). Margin вынесен сюда, в единственное место,
    чтобы оба вызывающих места гарантированно получали одинаковый запас."""
    if not sys.stderr.isatty():
        return 80
    columns = _console_columns()
    return max(min_width, columns - 2 - tag_width - tail_len - _CONSOLE_TAG_LINE_SAFETY_MARGIN)


class ProgressReporter:
    """Живой прогресс одной длительной фазы (индексация/хеширование/распаковка/копирование).

    На терминале (stderr -- tty) -- самообновляющаяся строка tqdm: фаза, обработано/всего
    (если total известен), %, скорость (файлов/с; МБ/с -- см. unit="Б"), ETA (только когда
    total известен -- у tqdm это само получается из total). update(note=...) меняет
    описание бара на текущее длительное действие ("Распаковка family_2015.zip (4.2 ГБ)…"),
    чтобы легитимная пауза не читалась как зависание.

    Не на терминале (файл/пайп) -- это НЕ дефолтное поведение tqdm (оно всё равно шлёт \\r),
    поэтому реализовано явно: периодические обычные строки без ANSI, раз в log_interval_sec
    секунд ИЛИ log_interval_n файлов (что раньше).

    2026-07-11 live-run finding: при total=None (indeterminate) сам tqdm рисовал двойное
    двоеточие ("Фаза N — текст: : 7файл [...]") независимо от того, что в desc -- его
    ДЕФОЛТНЫЙ no-total шаблон безусловно добавляет ": " поверх уже добавленного
    set_description()'ом, а WITH-total шаблон эту дубликацию сам же и проверяет (баг именно
    в tqdm, не в нашем desc). Фикс -- явный bar_format только для total=None (см. __init__).
    Разделяй части desc через " — " (тире) просто для стиля, не для обхода этого бага.

    Использование:
        with ProgressReporter(total=n, desc="Фаза 3 — хеширование") as bar:
            for item in items:
                ...
                bar.update(1, note=short_desc)

    two_line=True (SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2, 2026-07-31/08-01):
    отдельный, самодостаточный режим форматирования статус-строки -- изначально используемый
    ТОЛЬКО run_for_source()'s Фазой 2 (обход источника, реальная сборка/--dry-run), Фаза 1
    (индексация TARGET) и analyze*/analyze-full тогда не трогались, продолжали использовать
    описанный выше однострочный формат as-is. Речь пользователя, 2026-08-02: run_analyze()
    (значит и "Паспорт архива", и CLI analyze/analyze-full) тоже переведён на этот режим -- та
    же ETA/"объектов X/Y" информативность, что и у Фазы 2, раньше её не было вовсе (голый
    счётчик без прогноза). Фаза 1 (index_archive()) по-прежнему НЕ трогается, использует
    однострочный формат as-is (свой отдельный, более короткий предпересчёт, roots уже известны
    заранее -- см. её собственный ProgressReporter(total=len(entries)...) с настоящим total).
    См. update()/set_transient_op()/write_object_line() ниже за подробностями формата.
    """

    def __init__(self, total=None, desc="", unit="файл",
                 log_interval_sec=5.0, log_interval_n=200, disk_usage_path=None,
                 two_line=False, total_estimate=None, note_width=None):
        # SESSION-HANDOFF.txt п.13 (2026-08-05, боевой прогон): однострочный (не two_line) бар
        # Фазы 1 (index_archive()) визуально "гуляет" влево-вправо -- set_description() ниже
        # меняет ДЛИНУ desc в зависимости от того, есть ли note ("большое видео" -- единственное
        # возможное значение для этого бара) или нет, tqdm пересчитывает позицию |###| каждый
        # раз заново. note_width -- ширина, под которую note дополняется пробелами (а при
        # отсутствии note подставляется пустая строка той же длины) -- тот же приём "фиксированный
        # слот", что уже применён к тегу объект-строки (_format_object_line(), "[archive] "/
        # "[папка]   ", оба ровно 10 символов). None (по умолчанию, все остальные бары) --
        # старое поведение, без резервирования (полный перевод Фазы 1 на two_line избыточен ради
        # одной узкой проблемы).
        self._note_width = note_width
        self.desc = desc
        self.unit = unit
        self.total = total
        self.count = 0
        self.is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
        self._t0 = time.time()
        self._last_log_t = self._t0
        self._last_log_n = 0
        self.log_interval_sec = log_interval_sec
        self.log_interval_n = log_interval_n
        self._context_note = None  # see set_context()
        self.two_line = two_line
        # Речь пользователя, 2026-08-11: ширина поля операции в статус-строке (см.
        # _build_two_line_status()) считается ПО ЭТОМУ конкретному бару -- максимум его
        # СОБСТВЕННОГО resting-desc и единственных текстов, способных временно его заменить
        # (_MAX_TRANSIENT_OP_LEN, см. модульные константы выше) -- не по общему максимуму
        # всех desc-текстов программы разом (тот раздувал поле пустым местом для КАЖДОГО
        # режима до ширины самого длинного из ВСЕХ, включая несовместимые режимы, которые в
        # этом же прогоне никогда не покажутся).
        self._op_field_width = max(len(desc), _MAX_TRANSIENT_OP_LEN) if two_line else 0
        # ETA "план" (см. update()/_build_two_line_status()) -- предпересчёт SOURCE до входа
        # в Фазу 2 (_quick_media_count_estimate()), None означает "план недоступен" (плановое
        # время падает обратно на прошедшее -- то же самое, что и раньше без total вообще).
        self.total_estimate = total_estimate
        self._transient_op = None  # see set_transient_op()
        # Живой репорт пользователя (2026-08-01): "план" считался по кумулятивному среднему
        # (elapsed / count) за ВЕСЬ прогон -- один медленный файл (архив/видео) резко сдвигал
        # среднее, а потом оно ощутимо долго "тащилось" назад к норме, визуально скача то
        # вверх, то вниз. _ema_rate -- экспоненциальное скользящее среднее секунд/файл,
        # взвешивает недавние файлы сильнее старых (см. _EMA_RATE_ALPHA/update()) -- то же
        # плановое время реагирует на реальные изменения скорости, но не дёргается на каждый
        # отдельный выброс. None -- ни одного реального (n>0) update() ещё не было, план
        # падает обратно на кумулятивное среднее (см. _build_two_line_status()).
        self._ema_rate = None
        self._last_rate_update_t = None
        # Живой репорт пользователя (2026-08-01, боевой прогон D:\): реальные примеры плана
        # "267ч"/"323ч" при факте в 2-3ч -- одна распаковка архива/хеширование видео (минуты
        # блокировки БЕЗ собственных update()-тиков) целиком попадала в instantaneous
        # ближайшего n>0 update() как будто это время "одного файла". _transient_op_start_t --
        # момент начала ТЕКУЩЕЙ активной тяжёлой операции (None -- сейчас её нет);
        # _pending_heavy_time -- уже ЗАКРЫТЫЕ отрезки такого времени, накопленные с последнего
        # n>0 update(), которые тот вычтет из своего instantaneous (см. _close_transient_segment()/
        # update()). Обе точки входа (set_transient_op() -- распаковка архива; update(n=0,
        # note=...) -- предпометка перед хешированием видео) открывают отрезок; ЗАКРЫВАЕТ его
        # либо парный set_transient_op(None), либо ближайший n>0 update() -- но открывать новый
        # отрезок при закрытии НЕЛЬЗЯ (даже если note не изменился на n>0 завершающем тике),
        # иначе он "просачивается" в интервал уже следующего, обычного файла -- живая находка
        # при написании этого фикса (red-before-green поймал до пуша, см. тест).
        self._transient_op_start_t = None
        self._pending_heavy_time = 0.0
        # 2026-08-06, боевой прогон ("скорость всегда 0"): _pending_heavy_time выше -- верная
        # модель для распаковки архива/хеширования видео (одноразовая пауза, НЕ часть цены
        # файла), но неверная для батч-чтения EXIF (_walk_with_exif_prefetch()) -- там всё
        # время батча И ЕСТЬ реальная цена N файлов, просто измеренная разом, а не поштучно.
        # Исключение этого времени целиком (через set_transient_op()) оставляло в замере
        # только пустой Python-цикл между yield'ами уже готового батча -- почти 0 всегда.
        # set_batch_rate_hint() -- синтетическая цена/файл на batch_rate_hint_remaining
        # ближайших n>0 update(), вместо пересчёта по wall-clock (см. update()).
        self._batch_rate_hint = None
        self._batch_rate_hint_remaining = 0
        # Троттлинг статус-строки (см. _STATUS_REFRESH_EVERY_N) -- считает ТИКИ (n), не вызовы.
        self._ticks_since_refresh = 0
        # Раунд 49 ревью (REVIEW-HANDOFF.md, замечание 1): троттлинг не должен прятать самый
        # первый рендер (конструктор ниже вызывает update(0) как раз чтобы поставить desc "{desc}"
        # непустым с первого кадра, см. комментарий у tqdm_kwargs выше) -- но n==0 САМ ПО СЕБЕ
        # больше не форсирует refresh безусловно (см. update()), иначе троттлинг не срабатывает
        # вовсе в основном цикле _run_impl(), который вызывает update(0, note=None) перед КАЖДЫМ
        # файлом (не только перед видео, где note реально нужен немедленно). Этот флаг форсирует
        # ровно один, самый первый update() -- дальше n==0 форсирует, только если note!=None.
        self._never_refreshed = True
        # "объектов X/Y" (живой репорт пользователя, 2026-08-01, заменяет [прошло/план] --
        # архив/видео с непредсказуемым временем распаковки/хеширования делали оценку времени
        # ненадёжной по своей природе, независимо от EMA/исключения тяжёлых операций выше;
        # X/Y честный и не экстраполирует). self._obj_count -- та же ГРАНУЛЯРНОСТЬ, что и
        # total_estimate (архив = 1 объект, не заглядывая внутрь, см. object_progress_cb в
        # SourceWalker.__init__()) -- НЕ то же самое, что self.count (медиафайлы, см. выше).
        self._obj_count = 0
        # Речь пользователя, 2026-08-09: "объектов X/Y" читался как расхождение/баг, когда
        # total_estimate (оценка, не точный подсчёт) не совпадал с фактом -- ни X могло обогнать
        # Y (недооценка), ни X==Y гарантированно к концу прогона (легитимные пропуски: нет
        # доступа к папке и т.п.). Заменено на "обработано объектов XX.X%" (см.
        # _build_two_line_status()) -- по завершении БЕЗ прерывания форсируется ровно 100%
        # (mark_interrupted() ниже -- вызывающий код помечает прерванный прогон явно, close()
        # не форсирует 100% в этом случае, раз работа реально не закончена).
        self._run_interrupted = False
        # 2026-08-11, по прямой просьбе пользователя -- РЕВЕРТ: раньше "всего медиа" в
        # analyze-режиме показывало не self.count, а декларируемую по имени/расширению оценку
        # (self._media_declared, росла на n_found из write_object_line() ДО реальной обработки)
        # -- заведена именно из-за того, что self.count простаивает на 0 почти весь прогон при
        # пакетном чтении EXIF (_walk_with_exif_prefetch(), до 200 файлов разом) и разом
        # досчитывает в конце. Пользователь предпочёл честное число со скачками по факту батча
        # оценке, расходящейся с report.html (живой пример: "найдено медиа" 1038 против 644 в
        # отчёте) -- self.count используется всегда, без отдельного флага/источника.
        # SESSION-HANDOFF.txt п.4 -- см. write_object_line() за подробностями/разбором, чем это
        # НЕ является (не self._obj_count, тот считает файлы, не папки/архивы).
        self._objects_seen = 0
        # 2026-07-18, user request: показывать свободное место на TARGET в самой строке
        # прогресса (не только в начале/по завершении прогона, см. существующие "Свободно на
        # TARGET" log()-строки в run_for_source()). Пересчитывается РОВНО один раз за вызов
        # update() (то есть на каждый обработанный файл, не на каждый внутренний refresh
        # tqdm) -- по прямой просьбе пользователя, чтобы не плодить лишние disk_usage()
        # syscall'ы на сетевом/внешнем диске.
        self._disk_usage_path = disk_usage_path
        self._disk_free_text = ""
        # 2026-07-11 live-run finding: tqdm's own DEFAULT no-total rendering path
        # unconditionally appends its own ": " after `desc`, even though set_description()
        # already appended one itself -- renders as "Фаза N — текст: : 7файл [...]" (doubled
        # colon). The WITH-total default path already guards against this (checks whether
        # desc already ends in ": " first) -- only the no-total (indeterminate) case needs
        # an explicit bar_format override to sidestep tqdm's buggy branch. Also reworded the
        # bare "{n_fmt}{unit}" counter into "всего обработано N файл" per user feedback, then
        # 2026-07-11 (this session): "N файл" is ungrammatical for most N (needs declension by
        # count -- 1 файл/2 файла/5 файлов) -- reworded to a fixed genitive-plural phrase
        # ("обработано файлов: N") that reads correctly for any N without needing to compute
        # the declension. Only unit="файл" is used anywhere in this codebase today (see
        # ProgressReporter call sites) so hardcoding "файлов" here is safe.
        # two_line: bar_format -- ГОЛЫЙ "{desc}", вся статус-строка (включая счётчик/тайминги/
        # скорость/своб.место) строится нами вручную в _build_two_line_status() и передаётся
        # как desc -- тот же приём, что и ниже, просто template ничего не добавляет от себя.
        if two_line:
            tqdm_kwargs = {"bar_format": "{desc}"}
        else:
            # Ведущий пробел прямо в шаблоне (не в конкретном desc=), 2026-08-24, живая просьба
            # пользователя ("текст сливается с рамкой окна") -- та же логика, что и у
            # console_log()/резервных текстов two_line-поля операции выше, но здесь desc=
            # варьируется от вызывающего кода ("Оцениваю объём работы" и т.п.) -- проще и
            # надёжнее один раз в самом шаблоне, чем следить за каждым отдельным desc=.
            tqdm_kwargs = ({"bar_format": " {desc}всего обработано файлов: {n_fmt} [{elapsed}, {rate_fmt}{postfix}]"}
                           if total is None else {})
        stream = _RussianRateStream(sys.stderr, unit)
        # 2026-07-11 (live user report, later the same session): tqdm's set_description()
        # (called from update()/set_context() below) appends its OWN trailing ": " to its
        # internal desc attribute (confirmed empirically -- NOT documented tqdm behavior worth
        # trusting from memory alone) -- but passing desc= directly to the _tqdm(...)
        # CONSTRUCTOR does NOT go through set_description(), so IT renders one frame with the
        # raw string and no trailing ": " at all, the instant the object is constructed (tqdm
        # refreshes on init). An archive with 0 files (the live report's exact case) never
        # calls update() even once, so its bar rendered ONLY that raw pre-update() frame for
        # its entire lifetime -- "Фаза 1 — индексация архива" glued directly onto the literal
        # "всего обработано..." with zero separator ("архивавсего"). Fix: construct with an
        # EMPTY desc (nothing for the bar_format's {desc} to glue onto, however briefly) and
        # set the real one immediately after via update(0) -- which runs set_description()
        # and gets the correct trailing ": ", so even a zero-file run's only render is already
        # correctly separated from its very first frame, not just from the second one on.
        self._bar = _tqdm(total=total, desc="", unit=unit, file=stream,
                           dynamic_ncols=True, leave=True, **tqdm_kwargs) if self.is_tty else None
        self.update(0)

    def __enter__(self):
        if self._bar is not None:
            _ACTIVE_BARS.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def set_context(self, note):
        """2026-07-11 (this session), user feedback: an archive being extracted already shows
        a "текущее действие" note (see update()'s docstring finding below) so a slow archive
        never reads as a hang -- but plain folders being walked show nothing at all, so on a
        slow network drive/huge directory tree there is no way to tell where the program is
        currently digging. Unlike update()'s per-call `note` (a ONE-OFF that update() itself
        clears back to blank the moment the next file has no note of its own -- see the
        2026-07-11 finding just below), this is a PERSISTENT baseline: SourceWalker calls this
        once per directory entered, and it stays the fallback description until the next
        directory replaces it, surviving any number of ordinary per-file update(note=None)
        calls in between. Refreshes the display immediately (no update() call needed) since
        being inside a slow os.listdir() -- before any file in that directory has been
        processed yet -- is exactly the case this exists for.

        2026-07-12, живой репорт пользователя (строка при распаковке архива обрезалась
        терминалом даже во всю ширину экрана): вызывающий код (SourceWalker) уже обрезает
        note по общему, довольно грубому бюджету (_progress_note_budget(), не знает точную
        длину self.desc). Здесь, зная self.desc ТОЧНО, обрезаем ещё раз по более точному
        бюджету -- реальная ширина терминала минус фактическая длина desc минус запас под
        хвостовые tqdm-счётчики. _truncate_progress_note() идемпотентна для уже обрезанного
        текста (берёт символы с конца, второго "…" не появляется)."""
        if note and self._bar is not None and sys.stderr.isatty():
            note = _truncate_progress_note(note, maxlen=self._context_note_budget())
        self._context_note = note
        if self._bar is not None:
            self._bar.set_description(f"{self.desc} — {note}" if note else self.desc)

    def _context_note_budget(self, tail_reserve: int = 58, min_width: int = 15) -> int:
        """2026-07-12: бюджет под note, ТОЧНО учитывающий длину self.desc (в отличие от
        _progress_note_budget()'s общей оценки "самый длинный реальный префикс"). tail_reserve
        -- запас под хвостовые tqdm-счётчики этого bar_format ("всего обработано файлов: N
        [MM:SS, X.Xфайл/с]"), с большим запасом на случай многочасового прогона с 6-значным
        счётчиком файлов."""
        columns = _console_columns()
        if self._disk_usage_path is not None:
            # ", своб.9999.9ГБ" -- postfix, добавленный disk_usage_path (см. update()), тоже
            # часть хвоста и должен учитываться в бюджете, иначе note обрежется криво.
            tail_reserve += 20
        overhead = len(self.desc) + len(" — ") + tail_reserve
        return max(min_width, columns - overhead)

    def _probe_free_space(self) -> str:
        try:
            free = shutil.disk_usage(winlong(self._disk_usage_path)).free
        except OSError:
            return "своб.на TARGET недоступно"
        return f"своб.{free / 1024**3:.1f}ГБ"

    def _two_line_free_str(self) -> str:
        """Как _probe_free_space(), но БЕЗ префикса "своб." -- в two_line-формате этот
        префикс уже литерал в самом шаблоне статус-строки (см. _build_two_line_status())."""
        try:
            free = shutil.disk_usage(winlong(self._disk_usage_path)).free
        except OSError:
            return "н/д"
        return f"{free / 1024**3:.1f}ГБ"

    def _build_two_line_status(self, force_complete: bool = False) -> str:
        """Статус-строка Фазы 2 (SESSION-HANDOFF.txt, редизайн живого вывода): формат
        зафиксирован пользователем поколоночно --
        <операция, лево, self._op_field_width> | всего медиа <счётчик, право, 8> |
        обработано объектов <XX.X%, право, 6> | <ЧЧ:ММ:СС, право, 8> | <скорость, право, 2
        знака>с/файл[ | своб.<место, право, 10>]. Время без подписи "занято" (речь
        пользователя, 2026-08-11) -- единственное чистое ЧЧ:ММ:СС в строке, читается
        однозначно и без неё. Ширина поля операции -- см.
        self._op_field_width в __init__() (2026-08-11: посчитана по ЭТОМУ бару -- максимум его
        собственного resting-desc и единственных текстов, способных временно его заменить --
        не общий максимум по всем режимам программы разом, см. модульные константы
        _MAX_TRANSIENT_OP_LEN и комментарий рядом с ними). "|" между КАЖДОЙ
        парой блоков -- речь пользователя 2026-08-02 ("необходим разделитель между блоками
        информации"), раньше блоки отделялись только пробелами и сливались на глаз. Обычный
        ASCII "|", не символ рисования рамок (│, U+2502) -- проверено эмпирически: попытка с
        │ падала UnicodeEncodeError в консоли с однобайтовой кодовой страницей (cp1251), banner
        программы (print_welcome_banner()) тоже использует голый "=" * N, не Unicode-рамку --
        нет установленного прецедента, что расширенные box-drawing символы безопасны в этой
        консоли, рисковать некуда, раз есть гарантированно безопасная ASCII-альтернатива.
        Числа НЕ обрезаются при переполнении разрядности -- f"{x:>N}" сам просто сдвигает
        дальше вправо, это и требуется (числа важнее ровных колонок при многочасовом прогоне).
        " | своб.<...>" целиком опущено, когда disk_usage_path не передан (dry-run -- решение
        2026-07-25, живой "тающий" остаток на TARGET вводил в заблуждение в read-only режиме,
        см. _disk_usage_path в run_for_source()). Формат выше -- то, что показывается, когда
        строка целиком влезает в реальную ширину терминала; на узком терминале поля снимаются
        прогрессивно (время -> своб.место -> скорость -> сам текст операции обрезается) -- см.
        разбор в конце метода (REVIEW-HANDOFF.md, Раунд 86).

        "объектов X/Y" (2026-08-01, живой репорт пользователя): заменяло прежний [прошло/план]
        целиком -- любая экстраполяция времени по своей природе ненадёжна, когда среди файлов
        попадаются архивы/видео с непредсказуемой длительностью распаковки/хеширования (живые
        примеры прошлой итерации того же дня: план 267ч/323ч при факте 2-3ч, см. историю EMA/
        _close_transient_segment() ниже -- та работа не выброшена, "секунд/файл" по-прежнему
        нужен для этой же строки, просто больше не умножается на остаток для прогноза). X/Y --
        честный счётчик без прогноза: X (self._obj_count) и Y (self.total_estimate) -- ОДНА и
        та же гранулярность (архив = 1 объект целиком, не по файлам внутри, см.
        SourceWalker.__init__()'s object_progress_cb/_quick_media_count_estimate()), поэтому Y
        не ставится под сомнение сменой скорости, в отличие от старого "план". 2026-08-17: эта
        гранулярность считает только media-кандидатов (image/raw/video/архив/DVD-юнит) --
        немедийные файлы (мгновенное, дешёвое решение при обходе) больше не входят ни в X, ни в
        Y вовсе (боевой прогон: источник с большой долей таких файлов доводил X до Y почти
        сразу, задолго до реальной обработки медиафайлов в остальном дереве).

        "обработано объектов XX.X%" (2026-08-09, речь пользователя): X/Y выше читалось как
        расхождение/баг, когда Y (оценка, не точный подсчёт -- см. _quick_media_count_estimate())
        не совпадал с фактом: X мог обогнать Y (недооценка), и X==Y не гарантировано к концу
        прогона даже на успешном прогоне (легитимные пропуски -- нет доступа к папке и т.п.).
        Тот же честный X/Y, просто в процентах -- min(X/Y, 100%) защищает от "101%" при
        недооценённом Y. Точность: ВСЕГДА 1 знак после запятой (речь пользователя, 2026-08-10,
        Раунд 86 follow-up) -- раньше было целыми до 99% (мотивация: не создавать иллюзию
        точности, которой у Y-оценки нет) и с 1 знаком только от 99% и выше (иначе "99%" мог бы
        провисеть неизменным долго на большом архиве и читаться как зависание). Тот же довод
        оказался применим и к НИЖНЕЙ границе, не только к верхней: на большом Y (сотни тысяч
        объектов) один батч-тик (см. add_object_progress()/defer_media_object_tick) продвигает
        X на величину заметно меньше 1% от Y -- при целых процентах "0%" мог бы провисеть
        так же долго, как раньше "99%", ровно тот же класс проблемы, что чинили в этом же
        раунде для самой метрики. Знак после запятой у ОТНОШЕНИЯ не обещает точности САМОГО
        Y -- это просто более точный вывод честно посчитанного X/Y, тот прежний довод не
        держится.

        2026-08-17, речь пользователя ("завуалировать" остаточный эффект после фикса
        media-кандидатов, см. докстрин X/Y выше): даже с 1 знаком после запятой X/Y*100 может
        реально быть меньше 0.05% (округляется в "0.0%") достаточно долго -- источник с большой
        долей media-кандидатов, ожидающих одного batch-тика (defer_media_object_tick), либо
        просто очень большой Y. "0.0%" читается как зависание ровно тем же способом, что и целые
        "0%"/"99%" выше -- тот же довод, применённый ещё раз, к предельно малым, а не только
        целым, значениям. Пол в 0.1% (max(pct, 0.1), НИЖЕ force_complete/100%-ветки, значит не
        путается с ними) -- значение, а не точность: как только total_estimate известен, строка
        никогда не показывает буквальный "0.0%", даже до первого тика. Не обещание "хоть что-то
        уже обработано" -- тот же класс намеренной неточности, что и у "1 знак после запятой"
        выше (сигнал "не зависло", не точная метрика).
        force_complete (см. close()) форсирует ровно "100.0%" на успешном
        (не прерванном Ctrl+C) завершении прогона -- по прямой просьбе пользователя "в конце
        работы всегда должно быть 100%", та же логика, что и у стандартных прогресс-баров
        (apt/npm и т.п.), даже если реальный X/Y к этому моменту не сошлись бы день в день.
        total_estimate=0/None (оценка недоступна) -- откатывается на голый счётчик X без "%",
        там процент не определён.

        "занято ЧЧ:ММ:СС" (2026-08-07, речь пользователя): общее время работы текущей фазы с
        момента __init__() (self._t0), тот же elapsed, что и раньше считался только для rate,
        теперь ещё и показан отдельным полем -- перед скоростью, как попросил пользователь.
        Раньше (2026-08-06) для батч-чтения EXIF в analyze() (_walk_with_exif_prefetch(),
        до 200 файлов одним спавном exiftool -- update() не тикает, пока батч не досчитан)
        сюда же ставился текстовый transient_op ("чтение метаданных, файлов: N…"), заменявший
        `op` на время батча -- убран (2026-08-07, по прямой просьбе пользователя, "я этого не
        просил"/непонятно, откуда число): это поле решает ту же задачу ("не подумать, что
        зависло") без привязки к конкретной операции -- время между двумя update() просто
        видно как растущее "занято", а не превращается в отдельную непрошенную строку.
        tqdm.format_interval() -- та же функция, что форматирует "{elapsed}" в однострочном
        (не two_line) режиме ниже (__init__()'s bar_format) -- тот же формат ЧЧ:ММ:СС/ММ:СС,
        не изобретать новый."""
        elapsed = max(time.time() - self._t0, 1e-6)
        op = self._transient_op or self.desc
        # _ema_rate (см. update()) -- сглаженная секунд/файл по недавним файлам, не
        # кумулятивное среднее по всей истории прогона (живой репорт пользователя, 2026-08-01:
        # то дёргалось то вверх, то вниз при разбросе стоимости файлов). None -- ни одного
        # реального (n>0) update() ещё не было -- падаем обратно на кумулятивное среднее.
        rate = self._ema_rate if self._ema_rate is not None else elapsed / max(self.count, 1)
        if not self.total_estimate:
            obj_part = str(self._obj_count)
        elif force_complete:
            obj_part = "100.0%"
        else:
            # max(..., 0.1) -- см. докстрин выше ("завуалировать" зависание при пренебрежимо
            # малом X/Y*100): никогда не показывает буквальный "0.0%".
            #
            # min(..., 99.9) + усечение (не округление) до 1 знака -- живой боевой прогон,
            # 2026-08-19: источник с одним архивом, вмещающим гигантское количество вложенных
            # файлов/вложенных архивов -- этот архив тикает ОДНИМ объектом (см. _tick_object()),
            # только по завершении ВСЕГО своего содержимого, и может составлять ничтожную долю
            # Y, оставаясь при этом последним, самым долгим объектом прогона (у пользователя --
            # 2 часа). Обычные файлы вокруг него досчитались за 4 минуты, X/Y стало ~99.96% --
            # f"{99.96:.1f}%" ОКРУГЛЯЕТ до буквального "100.0%" (та же ошибка формата, что и у
            # round()), хотя реально не готово: "100.0%" держалось бы весь остаток прогона,
            # неотличимо от настоящего завершения. Буквальный "100.0%" теперь печатает ТОЛЬКО
            # force_complete-ветка выше -- здесь верхняя граница строго 99.9 (даже когда
            # X>=Y из-за недооценённого Y, см. докстрин про "X мог обогнать Y" выше), а
            # `int(...*1000)/10` усекает вместо округления, чтобы 99.96 не округлилось вверх и
            # само по себе, без верхней границы. Совместно с set_transient_op("разбор архива")
            # в _handle_archive() (та же находка) -- если % всё равно подолгу стоит на месте
            # из-за одного такого архива, поле операции слева честно объясняет, чем занят
            # прогон, вместо застывшего числа без единой живой подсказки.
            pct = max(min(int(self._obj_count / self.total_estimate * 1000) / 10, 99.9), 0.1)
            obj_part = f"{pct:.1f}%"
        # Речь пользователя, 2026-08-02 ("в строке статуса необходим разделитель между блоками
        # информации"): раньше блоки (операция/всего медиа/объектов/скорость/своб.) отделялись
        # только пробелами -- на глаз сливались в одну нечитаемую полосу цифр, особенно при
        # длинном тексте операции. Обычный ASCII "|" -- гарантированно кодируется в любой
        # консольной кодовой странице (см. докстринг метода за разбором, почему НЕ Unicode │).
        # Внутреннее форматирование самих блоков (выравнивание/ширина полей) не тронуто --
        # только разделители между ними, тесты на конкретные подстроки внутри блока
        # ("обработано объектов    42%", "2.00с/файл") не задеты.
        sep = " | "
        free_part = f"{sep}своб.{self._two_line_free_str():>10}" if self._disk_usage_path is not None else ""
        media_count = self.count
        # 2026-08-06, речь пользователя: "s" -- латиница, единственная в остальном кириллической
        # строке ("s/файл" рядом с "объектов"/"всего медиа") -- заменено на кириллическую "с".
        # Безопасно в любой кодовой странице консоли (обычная русская буква, не Unicode-символ
        # рисования рамок вроде │ выше по докстрингу, тот действительно падал в cp1251).
        media_label = "всего медиа"

        def _build_base(op_text, pad=True):
            op_field = f"{op_text:<{self._op_field_width}}" if pad else op_text
            return (f"{op_field}{sep}{media_label} {media_count:>8}{sep}"
                    f"обработано объектов {obj_part:>6}")

        base = _build_base(op)
        rate_part = f"{sep}{rate:>9.2f}с/файл"
        tail = rate_part + free_part
        # Речь пользователя, 2026-08-11: подпись "занято" перед временем убрана -- само поле
        # (единственное чистое ЧЧ:ММ:СС в строке, между "|") уже читается однозначно.
        elapsed_part = f"{sep}{_tqdm.format_interval(elapsed):>8}"
        line = base + elapsed_part + tail
        # REVIEW-HANDOFF.md, Раунд 86, замечание 1: проверка ширины терминала раньше снимала
        # ТОЛЬКО "занято" (единственное поле, добавленное правкой 2026-08-07) и на этом
        # останавливалась -- для самого длинного текста операции (analyze/Паспорт, 51 символ,
        # см. self._op_field_width) один этот шаг не помогал вообще: base+tail БЕЗ "занято"
        # уже 123 символа, обычное окно cmd.exe -- 80. Теперь -- прогрессивное снятие полей в
        # порядке убывания важности (занято -> своб.место -> скорость -> текст операции ->
        # "всего медиа", см. Раунд 87 ниже), и если даже голый "op | всего медиа | обработано
        # объектов %" не влезает -- сам текст операции обрезается по реальной ширине (не число
        # -- "Числа НЕ обрезаются" из докстринга относится к числовым полям, не к тексту
        # операции). Если не влезает даже "…" (1 символ) + "всего медиа" -- снимается и это
        # поле (Раунд 87, ниже по коду). "обработано объектов %" -- единственное поле, которое
        # НИКОГДА не снимается: это и есть сигнал "не зависло", ради которого затевался весь
        # Раунд 86. sys.stderr.isatty() -- та же проверка, что и раньше (не на реальном
        # терминале -- в файл/пайп -- перенос не имеет значения, показываем строку целиком).
        if sys.stderr.isatty():
            columns = _console_columns()
            if len(line) > columns:
                line = base + tail
            if len(line) > columns and free_part:
                line = base + rate_part
            if len(line) > columns:
                line = base
            if len(line) > columns:
                suffix_len = len(base) - self._op_field_width
                available = max(columns - suffix_len, 1)
                if available < len(op):
                    shrunk_op = op[:max(available - 1, 0)] + "…" if available > 1 else "…"
                else:
                    shrunk_op = op
                line = _build_base(shrunk_op, pad=False)
            if len(line) > columns:
                # REVIEW-HANDOFF.md, Раунд 87: предыдущий шаг обрезает ТЕКСТ операции, но сам
                # "всего медиа NNNN"-суффикс никогда не уходил -- на его фиксированной длине
                # (media_label + счётчик) минимальная длина строки даже при op="…" (1 символ)
                # не зависела от реальной ширины терминала (эмпирически ~55 колонок для
                # "найдено медиа"/8-значного счётчика), ниже этого порога переполнение снова
                # росло неограниченно с сужением терминала -- тот же класс проблемы, что чинил
                # весь Раунд 86, просто сдвинутый на другой порог, не устранённый. "всего
                # медиа" не защищено докстрингом метода (только "обработано объектов %" -- явно
                # объявлен неснимаемым), поэтому снимается здесь же, следующим по важности после
                # текста операции -- сам процент по-прежнему несёт сигнал "не зависло" в
                # одиночку.
                pct_suffix = f"{sep}обработано объектов {obj_part:>6}"
                available = max(columns - len(pct_suffix), 1)
                if available < len(op):
                    shrunk_op = op[:max(available - 1, 0)] + "…" if available > 1 else "…"
                else:
                    shrunk_op = op
                line = f"{shrunk_op}{pct_suffix}"
        return line

    def _close_transient_segment(self) -> None:
        """Закрывает уже открытый отрезок тяжёлой операции (если есть, см.
        _transient_op_start_t/_pending_heavy_time в __init__()), копит его длительность --
        НЕ открывает новый сама по себе. Открытие -- ответственность set_transient_op()
        (распаковка архива) или update()'s n==0 предпометки (хеширование видео), не побочный
        эффект закрытия: если бы n>0-завершение тика реоткрывало отрезок только потому, что
        note не изменился (тот же текст на предпометке и на завершающем тике одного и того же
        видео), этот "хвостовой" отрезок просачивался бы в интервал уже СЛЕДУЮЩЕГО, обычного
        файла -- живая находка при написании этого фикса, red-before-green поймал до пуша."""
        if self._transient_op_start_t is not None:
            self._pending_heavy_time += time.time() - self._transient_op_start_t
            self._transient_op_start_t = None

    def set_transient_op(self, text) -> None:
        """two_line-режим: устанавливает/снимает операцию, ВРЕМЕННО заменяющую resting-текст
        (self.desc, "сортировка и копирование"/"Проверяю источник...") в статус-строке -- для
        действий без собственного построчного прогресса (распаковка архива/хеширование
        видео), тот же принцип "легитимная пауза не должна читаться как зависание", что и у
        старого update(note=...) для однострочного режима. В отличие от update(note=...) (тот
        привязан к очередному тику по файлу), этот метод вызывается НАПРЯМУЮ из середины
        _handle_archive() -- распаковка блокирует поток МЕЖДУ двумя yield'ами генератора
        обхода, ни один update() физически не происходит, пока она идёт, поэтому текст должен
        обновиться на экране немедленно, не дожидаясь следующего файла. text=None -- вернуться
        к обычной resting-операции.

        Живой репорт пользователя (2026-08-01, 267ч/323ч примеры): закрывает уже открытый
        отрезок тяжёлой операции (см. _close_transient_segment()) и, если text не пуст,
        открывает новый -- этот "старт-стоп" учёт нужен, чтобы ближайший n>0 update() исключил
        время самой распаковки из своего instantaneous."""
        self._close_transient_segment()
        if text:
            self._transient_op_start_t = time.time()
        self._transient_op = text
        if self.two_line and self._bar is not None:
            self._bar.set_description(self._build_two_line_status())
            self._bar.refresh()

    def set_batch_rate_hint(self, per_item_seconds: float, n_items: int) -> None:
        """2026-08-06, боевой прогон ("скорость всегда 0"): для батч-чтения EXIF
        (_walk_with_exif_prefetch()) обычный wall-clock замер в update() даёт ~0 -- реальная
        цена N файлов потрачена РАЗОМ, ДО того как они вообще начали yield'иться, а сами
        yield'ы внутри уже готового батча идут друг за другом почти мгновенно (никакого I/O
        между ними). _pending_heavy_time (см. set_transient_op()) была бы неверной моделью
        здесь -- это НЕ одноразовая пауза, не связанная с ценой файла (как распаковка
        архива), а ровно она и есть, просто измеренная разом на N файлов вместо одного.

        n_items ближайших n>0 update() используют per_item_seconds как instantaneous
        НАПРЯМУЮ, вместо перерасчёта по wall-clock -- после исчерпания update() сам
        возвращается к обычному замеру. Тот же эффект на EMA, что и от N настоящих замеров
        подряд с одинаковым instantaneous (см. update())."""
        if n_items <= 0:
            return
        self._batch_rate_hint = per_item_seconds
        self._batch_rate_hint_remaining = n_items

    def add_object_progress(self, n: int = 1) -> None:
        """"объектов X/Y" (см. self._obj_count в __init__()) -- вызывается из
        SourceWalker.object_progress_cb на каждый файл/архив ВЕРХНЕГО уровня SOURCE, той же
        гранулярностью, что и total_estimate (_quick_media_count_estimate()). Не трогает
        отображение сама по себе -- следующий обычный update() (для медиафайла) подхватит
        новое значение в _build_two_line_status(), троттлинг не обходим отдельным refresh()
        здесь же (иначе троттлинг статус-строки, см. _STATUS_REFRESH_EVERY_N, потерял бы смысл
        -- этот колбэк вызывается чаще, чем медиа-тики, включая пропущенные не-медиа файлы)."""
        self._obj_count += n

    def mark_interrupted(self) -> None:
        """Речь пользователя, 2026-08-09 ("обработано объектов XX%"): вызывающий код зовёт это
        из своего `except KeyboardInterrupt:` (см. run_analyze()/_run_impl()) ДО close() --
        close()/_build_two_line_status() форсируют 100% только на НЕПРЕРВАННОМ прогоне, раз
        "обработано" при прерывании -- буквально неправда, работа реально не закончена."""
        self._run_interrupted = True

    def _object_line_budget(self, letter: str = "", min_width: int = 15) -> int:
        """Бюджет под путь в объект-строке (см. write_object_line()) -- своя, отдельная от
        _context_note_budget()/_progress_note_budget() оценка: путь здесь на СВОЕЙ строке, не
        делит её с self.desc, зато делит с 2-пробельным отступом (см. _format_object_line() --
        тот же отступ, что и у всех self.log()-строк SourceWalker, "  [archive]"/
        "  [skip_marker]"/"  Распаковка" и т.д. -- живой репорт пользователя, 2026-08-01:
        "скачет" левый край без него), фиксированным тегом ("[archive] "/"[папка]   ", 10
        символов без буквы решения, 11 с ней -- см. letter ниже) и текстовым хвостом
        " найдено медиафайлов N" -- см. _console_tag_line_budget() за общей частью расчёта
        (2026-08-05, теперь также SourceWalker._log_archive()). Запас под само число N --
        теперь общий _CONSOLE_TAG_LINE_SAFETY_MARGIN внутри _console_tag_line_budget()
        (2026-08-09, см. её докстринг), не отдельный "+8" здесь -- раньше он был зашит именно
        тут, и именно поэтому [archive] (без такого же запаса) переносился там, где [папка] --
        нет.

        letter (2026-08-09, живая находка пользователя): "A"/"D" (альбом/по дате, см.
        _format_object_line()) добавляет 1 символ к тегу -- бюджет под путь должен сжаться на
        тот же 1 символ, иначе строка перестанет укладываться в реальную ширину терминала
        ровно на границе. "" (по умолчанию, режимы analyze/[4] Паспорт, где буква не
        показывается) -- бюджет не меняется, тот же расчёт, что и раньше."""
        tail_reserve = len(" найдено медиафайлов ")
        tag_width = 11 if letter else 10
        return _console_tag_line_budget(tail_reserve, min_width=min_width, tag_width=tag_width)

    def _format_object_line(self, tag: str, path: str, n_found: int, letter: str = "") -> str:
        # letter (2026-08-09, живая находка пользователя, режимы --dry-run/[3] реальная сборка):
        # "A" -- файлы уйдут в альбом (find_album() нашёл совпадение), "D" -- разберутся по
        # дате (не нашёл, ByDate). "" (по умолчанию, analyze/[4] Паспорт) -- буква не
        # показывается вовсе, старый формат без изменений. Буква -- сразу после закрывающей "]"
        # тега (по прямой просьбе пользователя), паддинг у "[папка]" на 2 символа больше, чем у
        # "[archive]" (короче само слово), чтобы после буквы у ОБОИХ тегов было ровно по одному
        # пробелу перед путём (не 1 у archive и 3 у папка, как получилось бы при наивной
        # вставке буквы в старый фиксированный паддинг).
        if letter:
            label = f"[archive]{letter} " if tag == "archive" else f"[папка]{letter}   "  # оба ровно 11 символов
        else:
            label = "[archive] " if tag == "archive" else "[папка]   "  # оба ровно 10 символов
        truncated = _truncate_progress_note(path, maxlen=self._object_line_budget(letter))
        # Живой репорт пользователя (2026-08-01): двоеточие после имени -- отделяет путь от
        # хвоста "найдено медиафайлов N", тот же приём, что и у status-стиля _log_archive()
        # ("X: archive_no_media"), теперь и здесь для единообразия.
        return f"  {label}{truncated}: найдено медиафайлов {n_found}"

    def write_object_line(self, tag: str, path: str, n_found: int, letter: str = "") -> None:
        """two_line-режим: печатается РОВНО один раз на объект (папку/архив), при входе в
        него -- см. _format_object_line() за форматом (letter -- см. её докстринг). self._bar.write()
        НЕ используется -- та же причина, что и у log_line() (см. её докстринг): tqdm.write()
        распознаёт "свои" активные бары СРАВНЕНИЕМ потоков вывода, а наш бар создан с
        собственным _RussianRateStream-прокси, не голым sys.stderr -- совпадения не будет,
        tqdm ничего не очистит и допишет строку прямо в хвост текущей строки бара. Тот же
        ручной приём clear/print/refresh, что и в log_line().

        SESSION-HANDOFF.txt п.4 (2026-08-05, боевой прогон): self._objects_seen -- ровно один
        тик на каждый вызов (папка ИЛИ архив, любая глубина реального дерева SOURCE) -- НЕ
        то же самое, что self._obj_count (add_object_progress()/"объектов X/Y" в статус-строке)
        -- та величина считает ФАЙЛЫ (любое имя в дереве, денаминатор _quick_media_count_estimate()),
        не папки/архивы. Проверено эмпирически перед реализацией (не поверено на слово старой
        находке в SESSION-HANDOFF.txt, которая перепутала эти две величины)."""
        self._objects_seen += 1
        line = self._format_object_line(tag, path, n_found, letter)
        if self._bar is not None:
            self._bar.clear()
            print(line, file=sys.stderr)
            self._bar.refresh()
        else:
            print(line, file=sys.stderr)

    def write_heavy_notice(self, line: str, wrap: bool = True) -> None:
        """Как write_object_line(), но для уже готового текста (SourceWalker's редкие/важные
        уведомления -- "Распаковка ...", "[DVD] новый DVD-диск", "[skip_marker]", ошибки чтения
        директории -- см. SourceWalker._log_own_line()). 2026-08-24, живой репорт пользователя:
        эти сообщения шли через self.log() -- модульный console_log()/log_line(), координирующий
        clear()/print()/refresh() через _ACTIVE_BARS -- на практике ненадёжный (см. докстринг
        log_line()): "Расп"/"аковка" склеивались без переноса строки. Временный обходной путь
        (жёсткий "\\n" перед текстом, БЕЗ clear()/refresh()) устранил склейку, но открыл другой
        баг -- бар не переиспользует свою старую строку (clear() не вызывается вовсе), поэтому
        КАЖДОЕ такое уведомление оставляет позади себя "замороженный" кадр бара, из-за чего
        строка визуально "уезжает вверх"/дублируется на каждом архиве/папке. Правильный фикс --
        этот метод: прямая ссылка на self._bar (передаётся вызывающим кодом как отдельный
        колбэк, см. SourceWalker.__init__()'s heavy_notice_cb), а не глобальный реестр -- та же
        гарантия, что и у write_object_line(), не зависит от того, сколько копий модуля
        загружено в процессе.

        2026-08-24, живой репорт пользователя (та же сессия, следующий заход): print() здесь
        напрямую, В ОБХОД console_log() -- вместе с координацией бара потерялся и перенос
        длинных строк (_wrap_console_text()), который раньше делал console_log() для этих же
        сообщений -- длинный путь DVD-диска пошёл ОДНОЙ строкой без переноса. Тот же
        isatty()-гейт здесь, что и в console_log().

        wrap=False (2026-08-28, живой боевой прогон): вызывающий код уже обрезал текст под
        ПОЛНУЮ ширину терминала (как объект-строка write_object_line(), которая не переносится
        вовсе) -- перенос рвал бы такую строку там, где место ещё есть. См.
        SourceWalker._log_archive().

        Порог переноса -- ПОЛНАЯ ширина окна (_console_columns()), не 2/3
        (_terminal_wrap_width()). 2026-08-29, два живых репорта "необоснованный перенос": это
        однострочные статус-уведомления SourceWalker'а, а не проза меню -- перенос по 2/3 рвал
        их посреди фразы, оставляя треть окна пустой. 2/3 остаётся только для console_log()
        (читаемость абзацев меню/справки)."""
        if wrap and sys.stdout.isatty():
            line = _wrap_console_text(line, _console_columns())
        if self._bar is not None:
            self._bar.clear()
            print(line, file=sys.stderr)
            self._bar.refresh()
        else:
            print(line, file=sys.stderr)

    def update(self, n=1, note=None):
        self.count += n
        if self.two_line:
            # Живой репорт пользователя (2026-08-01): "план" на кумулятивном среднем "прыгал"
            # то вверх, то вниз -- один медленный файл долго "тащил" среднее за собой. Здесь
            # -- сглаженный (EMA) замер: время МЕЖДУ этим и предыдущим реальным (n>0) update()
            # делённое на n файлов, взвешенное с накопленным _ema_rate (см. _EMA_RATE_ALPHA).
            # n=0 (note-only обновление текста ДО блокирующего вызова, см. вызывающий код) не
            # засчитывается как файл -- не в счёт для скорости. Самый первый n>0 update() ещё
            # не имеет предыдущего замера -- используем момент создания бара (self._t0) как
            # точку отсчёта, а не пропускаем измерение вовсе (иначе план оставался бы на
            # кумулятивном среднем на протяжении всего первого файла без всякой причины).
            if note and n == 0:
                # Предпометка перед блокирующим вызовом (хеширование видео, см. вызывающий
                # код) -- начало тяжёлого отрезка, тот же учёт, что и у set_transient_op()
                # (распаковка архива) -- см. _close_transient_segment()/_pending_heavy_time в
                # __init__(). Именно n==0 -- обычный завершающий тик (n>0) этого же видео,
                # приходящий следом с ТЕМ ЖЕ note, не должен открывать отрезок заново (см.
                # докстринг _close_transient_segment() за причиной).
                self._close_transient_segment()
                self._transient_op_start_t = time.time()
            self._transient_op = note
            if n > 0:
                self._close_transient_segment()
                now_t = time.time()
                if self._batch_rate_hint is not None and self._batch_rate_hint_remaining > 0:
                    # 2026-08-06, боевой прогон ("скорость всегда 0"): см.
                    # set_batch_rate_hint() -- реальная цена этих файлов уже потрачена РАЗОМ,
                    # ДО этого update(), wall-clock между самими yield'ами уже готового батча
                    # ничего не измеряет. _pending_heavy_time отбрасываем, не вычитаем --
                    # он же покрывает ровно тот же интервал, что и хинт (см.
                    # _flush_exif_prefetch_batch()), учитывать его отдельно было бы задвоением.
                    instantaneous = self._batch_rate_hint
                    self._pending_heavy_time = 0.0
                    self._batch_rate_hint_remaining -= n
                    if self._batch_rate_hint_remaining <= 0:
                        self._batch_rate_hint = None
                        self._batch_rate_hint_remaining = 0
                else:
                    since = self._last_rate_update_t if self._last_rate_update_t is not None else self._t0
                    # Живой репорт пользователя (2026-08-01, боевой прогон D:\, реальные примеры
                    # плана 267ч/323ч): вычитаем время, накопленное выше (set_transient_op()
                    # снаружи ИЛИ n==0 предпометка сразу над этим блоком) в _pending_heavy_time --
                    # распаковку архива/хеширование видео, попавшие МЕЖДУ этим и предыдущим n>0
                    # update(), не засчитываем как "время одного файла".
                    heavy = self._pending_heavy_time
                    self._pending_heavy_time = 0.0
                    instantaneous = max((now_t - since) - heavy, 0.0) / n
                self._ema_rate = (instantaneous if self._ema_rate is None else
                                   _EMA_RATE_ALPHA * instantaneous
                                   + (1 - _EMA_RATE_ALPHA) * self._ema_rate)
                self._last_rate_update_t = now_t
            # Троттлинг (см. _STATUS_REFRESH_EVERY_N): обычные тики (n>0, без note) перерисовывают
            # строку раз в N файлов, не на каждом -- живой репорт пользователя, 2026-08-01.
            # note!=None -- всегда немедленно (та же причина, что и у set_transient_op():
            # "легитимная пауза не должна читаться как зависание", троттлинг не должен эту
            # немедленность прятать). Раунд 49 ревью: n==0 САМ ПО СЕБЕ больше НЕ форсирует --
            # _run_impl()'s основной цикл вызывает update(0, note=None) перед КАЖДЫМ файлом
            # (не только видео), из-за чего _ticks_since_refresh обнулялся раньше, чем успевал
            # дорасти до порога от последующих n>0 тиков -- троттлинг не срабатывал вовсе.
            # self._never_refreshed (см. __init__) по-прежнему форсирует самый первый рендер.
            self._ticks_since_refresh += n
            force_refresh = (note is not None or self._never_refreshed
                              or self._ticks_since_refresh >= _STATUS_REFRESH_EVERY_N)
            if not force_refresh:
                if self._bar is not None:
                    self._bar.update(n)
                return
            self._ticks_since_refresh = 0
            self._never_refreshed = False
            line = self._build_two_line_status()
            if self._bar is not None:
                self._bar.set_description(line)
                self._bar.update(n)
                return
            now = time.time()
            if (now - self._last_log_t >= self.log_interval_sec
                    or self.count - self._last_log_n >= self.log_interval_n):
                print(line, file=sys.stderr)
                self._last_log_t = now
                self._last_log_n = self.count
            return
        effective_note = note or self._context_note
        if self._disk_usage_path is not None:
            self._disk_free_text = self._probe_free_space()
        if self._bar is not None:
            # 2026-07-11 finding (live production run): previously only set_description()
            # when note was truthy -- tqdm's description is sticky, so a note from one large
            # video ("хеширование большого видео") stayed on screen for every subsequent
            # photo with note=None, falsely suggesting the run was stuck processing video.
            # Always (re)set it, falling back to the persistent context note (see
            # set_context()) or the plain phase description if neither is set.
            if self._note_width:
                # SESSION-HANDOFF.txt п.13 -- фиксированный слот: " — " + note ВСЕГДА
                # присутствует, даже пустым (пробелы той же ширины), иначе сама длина desc
                # менялась бы в зависимости от того, есть note или нет -- ровно то, что и
                # сдвигало |###| влево-вправо.
                note_field = f"{effective_note:<{self._note_width}}" if effective_note else " " * self._note_width
                self._bar.set_description(f"{self.desc} — {note_field}")
            else:
                self._bar.set_description(f"{self.desc} — {effective_note}" if effective_note else self.desc)
            if self._disk_usage_path is not None:
                self._bar.set_postfix_str(self._disk_free_text)
            self._bar.update(n)
            return
        now = time.time()
        if (now - self._last_log_t >= self.log_interval_sec
                or self.count - self._last_log_n >= self.log_interval_n):
            self._emit_plain_line(effective_note)
            self._last_log_t = now
            self._last_log_n = self.count

    def _emit_plain_line(self, note=None):
        elapsed = max(time.time() - self._t0, 1e-6)
        rate = self.count / elapsed
        pct = f"{100 * self.count / self.total:.0f}%" if self.total else "?"
        total_part = f"/{self.total}" if self.total else ""
        extra = f" -- {note}" if note else ""
        disk_extra = f", {self._disk_free_text}" if self._disk_free_text else ""
        print(f"[{self.desc}] {self.count}{total_part} ({pct}), {rate:.1f} {self.unit}/с{extra}{disk_extra}",
              file=sys.stderr)

    @property
    def object_count(self) -> int:
        """SESSION-HANDOFF.txt п.4 -- сколько раз write_object_line() реально вызвана (папка
        ИЛИ архив, любая глубина реального дерева SOURCE), для сохранения в AnalyzeStats ДО
        close() (см. run_analyze())."""
        return self._objects_seen

    def close(self):
        # Речь пользователя, 2026-08-09 ("в конце работы всегда должно быть 100%"): последний
        # реально отрисованный _build_two_line_status() мог застыть на любом X/Y-проценте --
        # обычный тик просто больше не приходит после последнего файла, close() раньше не
        # перерисовывал строку сам. force_complete=True только если прогон НЕ прерван (см.
        # mark_interrupted()/её докстринг) -- "готово" при реальном Ctrl+C было бы неправдой.
        if self.two_line:
            final_line = self._build_two_line_status(force_complete=not self._run_interrupted)
            if self._bar is not None:
                self._bar.set_description(final_line)
                self._bar.refresh()
        if self._bar is not None:
            self._bar.close()
            if self in _ACTIVE_BARS:
                _ACTIVE_BARS.remove(self)
        elif self.count:
            if self.two_line:
                print(final_line, file=sys.stderr)
            else:
                self._emit_plain_line(note="фаза завершена")


# ============================================================================
# CONFIG  (from pipeline/config.py)
# ============================================================================


IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "bmp", "webp", "gif"}
RAW_EXTS = {"cr2", "cr3", "nef", "arw", "dng"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "3gp", "mts", "m2ts", "wmv", "flv", "webm", "mod", "tod",
              "vob"}
# 2026-08-07, по прямой просьбе пользователя (реальный боевой прогон, домашнее видео с DVD):
# отдельностоящий .vob (НЕ внутри папки VIDEO_TS) идёт обычным путём видео -- хеш/дедуп/near-dup/
# откат даты на mtime (как у .mod/.tod выше, тот же класс "старый формат без надёжных метаданных
# съёмки"). .vob ВНУТРИ папки VIDEO_TS обрабатывается отдельно, целым DVD-юнитом, см.
# SourceWalker._handle_dvd_unit() -- эта запись в VIDEO_EXTS его не касается (тот код путь
# перехватывает папку VIDEO_TS раньше, чем её содержимое дошло бы до обычной по-файловой
# классификации).
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2"}
ARCHIVE_MULTI_EXTS = {"tar.gz", "tar.bz2"}

# Security audit finding #4: the archive-bomb check below only looks at total_size vs.
# compressed_size ratio -- an archive with millions of near-empty entries (e.g. 2M files of
# 0-1 bytes each) reports a tiny total_size, sails straight past that check with almost no
# free-space requirement, and only THEN grinds the per-file pipeline (SHA256, work.db insert,
# 7 CSV log rows per entry) for hours, while bloating NTFS metadata far past what total_size
# suggested (many tiny files cost real disk space in cluster/MFT overhead). A real family
# archive of any format essentially never has this many entries in one file.
MAX_ARCHIVE_ENTRIES = 200_000


def format_formats_report() -> str:
    """Печатает те же множества, что реально использует file_type() для классификации
    -- в отличие от таблицы расширений в RULES.md, не может разойтись с кодом."""
    groups = [
        ("Изображения", IMAGE_EXTS),
        ("RAW", RAW_EXTS),
        ("Видео", VIDEO_EXTS),
        ("Архивы", ARCHIVE_EXTS | ARCHIVE_MULTI_EXTS),
    ]
    lines = ["Расширения файлов, которые PhotoArchive распознаёт (без точки, регистр не важен):", ""]
    for title, exts in groups:
        lines.append(f"  {title}: {', '.join(sorted(exts))}")
    lines.append("")
    lines.append('Всё остальное -- тип "other", не медиафайл.')
    return "\n".join(lines)


HARD_EXCLUDE_DIRS = {
    # 2026-07-11 finding (live production run, SOURCE=C:\): "Default"/"Default User" are
    # Windows' hidden template profiles (used to create new user accounts), never a real
    # user -- every subfolder under them is permission-locked for everyone by OS design, so
    # a whole-disk scan hit a wall of scary "Отказано в доступе" messages for a profile that
    # was never going to have photos in it. "Мои видеозаписи"/"Моя музыка"/"мои рисунки" (and
    # the English-locale equivalents) are legacy compatibility JUNCTIONS Windows creates
    # under every real user's Documents\, pointing at that same user's real Videos\/Music\/
    # Pictures\ -- Windows itself refuses to enumerate through them (Access Denied) to avoid
    # double-walking; the real folders are already reached directly and unaffected by this.
    "system volume information",
    "default", "default user",
    "мои видеозаписи", "моя музыка", "мои рисунки",
    "my videos", "my music", "my pictures",
    # 2026-07-11 finding (live production run, SOURCE=C:\, second round): four more
    # top-level Windows folders that are always Access-Denied for any real user account --
    # "Documents and Settings" is a legacy Windows XP-era junction pointing at "Users" (same
    # double-walking protection as the Мои видеозаписи junctions above), "MSOCache" is
    # Microsoft Office's installer cache (SYSTEM/admin only), "PerfLogs" and "Recovery" are
    # Windows' own performance-log and recovery-partition-mirror folders (also SYSTEM/admin
    # only) -- none of these can ever contain a real user photo.
    "documents and settings", "msocache", "perflogs", "recovery",
    # 2026-07-11 (session on managing this list): "__служебные_файлы" is a different kind of
    # entry than the ones above -- not about OS access, about not letting the tool re-ingest
    # its OWN logs/tmp_extract as a "new" source. Kept unconditional for the same reason
    # SKIP_PHOTOSORT.txt is (see below) -- a quick name-based gate in addition to the marker.
    "__служебные_файлы",
}

# 2026-07-11 (session on managing this list): unlike HARD_EXCLUDE_DIRS above, none of these
# are actually inaccessible -- they're a heuristic ("almost certainly not a photo"), not an OS
# restriction. $RECYCLE.BIN in particular is readable by its owning user (each drive's
# \$RECYCLE.BIN\<SID>\ subfolder belongs to that user, no elevation needed) -- a user wanting
# to recover deleted photos may legitimately want it scanned. Kept as a sensible, user-editable
# DEFAULT (see Config.default_exclude_dirs/photoarchive_config.yaml.example) rather than hardcoded, so
# removing an entry here doesn't require touching code.
DEFAULT_EXCLUDE_DIR_NAMES = ["node_modules", ".git", "$recycle.bin"]

# Слой 1 (гейт обхода): "настоящие" системные папки резолвятся через переменные окружения,
# а не хардкодом имён -- переносимо между версиями/языками Windows. Обход внутрь них
# гейтится Config.scan_system_dirs (по умолчанию False -- не заходить). Применяется только
# при рекурсии (SourceWalker._walk_dir, is_root=False); явно указанный SOURCE внутрь такой
# папки всегда обрабатывается (см. RULES.md "ПРАВИЛО ЯВНОГО УКАЗАНИЯ").
SYSTEM_DIR_ENV_VARS = (
    "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
    "LOCALAPPDATA", "APPDATA", "TEMP",
)


def _resolve_system_dirs():
    dirs = []
    for var in SYSTEM_DIR_ENV_VARS:
        v = os.environ.get(var)
        if v:
            dirs.append(os.path.normcase(os.path.realpath(v)))
    return dirs


SYSTEM_DIRS = _resolve_system_dirs()


def is_under_system_dir(path: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    return any(real == d or real.startswith(d + os.sep) for d in SYSTEM_DIRS)


# Слой 2 (маршрутизация сомнительного внутри того, что реально обходится): "шумные" зоны
# определяются по имени СЕГМЕНТА пути, могут встретиться где угодно (не только в системных
# папках) -- поэтому это НЕ повод пропускать обход (в отличие от HARD_EXCLUDE_DIRS/
# default_exclude_dirs выше), а повод строже классифицировать. Раньше temp/tmp/.cache/
# .thumbnails были в EXCLUDE_DIRS (жёсткий
# скип, фото внутри терялись бы молча); теперь это зона noisy -- см. classify_zone().
NOISE_SEGMENT_NAMES = {
    "cache", "code cache", "gpucache", "temp", "tmp", ".cache", ".thumbnails",
    "thumbnailcache",
}


def classify_zone(path: str) -> str:
    for seg in path.split(os.sep):
        if seg.strip().lower() in NOISE_SEGMENT_NAMES:
            return "noisy"
    return "normal"

EXCLUDE_FILES_PATTERNS = [
    "thumbs.db", "desktop.ini", ".ds_store", "*.tmp", "*.part",
    "hiberfil.sys", "pagefile.sys", "swapfile.sys",  # locked Windows system files, never readable
    "ntuser.dat", "ntuser.dat.log*",
]
SIDECAR_PATTERNS = ["*.xmp", "*.aae"]
SKIP_MARKER = "SKIP_PHOTOSORT.txt"

# Security audit finding #1 (2026-07-10): TMP_EXTRACT_DIR is user-configurable
# (photoarchive_config.yaml, no path validation) and its contents get shutil.rmtree'd at the start of
# EVERY run, including --dry-run, to clean up after a crashed previous run. If tmp_extract_dir
# is ever misconfigured (typo, or a maliciously "helpful" photoarchive_config.yaml) to point at an existing
# unrelated folder -- e.g. the user's Desktop -- that folder's entire contents were silently
# deleted on the very first launch. Fix: only ever delete entries that look like OUR OWN
# extraction dirs (named after the archive's sha256 hex digest, see _handle_archive() below) --
# anything else is left untouched and just logged as a warning, regardless of what
# tmp_extract_dir turns out to be.
_OWN_TMP_EXTRACT_ENTRY_RE = re.compile(r"^[0-9a-f]{64}$")

# 2026-08-19, живая находка ревизора (Раунд 107): единый _DRY_RUN_TMP_EXTRACT_DIR (см. её
# докстринг ниже по файлу) без per-процесс изоляции позволял конкурентному прогону удалить
# АКТИВНУЮ распаковку архива другого, ещё не завершившегося прогона (sha256-имя папки не несёт
# никакой информации о том, кто её создал и жив ли он ещё). Верхний уровень
# _DRY_RUN_TMP_EXTRACT_DIR теперь -- PID-подпапки (по одной на suppress_logs=True-процесс), а
# не сами sha256-папки распаковки напрямую -- см. Config.__post_init__/_sweep_stale_dry_run_pid_dirs().
_OWN_TMP_EXTRACT_PID_DIR_RE = re.compile(r"^\d+$")

# 2026-07-11: a sensible, user-editable DEFAULT (see Config.dump_segment_names/
# photoarchive_config.yaml.example) -- real heuristic "probably not an album" names, safe to let a user
# add/remove entries in photoarchive_config.yaml. Deliberately does NOT include the self-protection names
# below (DUMP_SEGMENT_NAMES_PROTECTED) -- those must never be removable via config.
DEFAULT_DUMP_SEGMENT_NAMES = [
    "dcim", "camera", "camera uploads", "фотокамера", "photostream", "моменты",
    "screenshots", "скриншоты", "downloads", "загрузки", "saved pictures",
    "pictures", "изображения", "фотопленка",
    "users", "home",
    "desktop", "рабочий стол",  # 2026-07-11 finding: universal Windows profile folder, same
                                # category as downloads/pictures above -- a loose photo dropped
                                # on the Desktop is not a deliberately-named album
    "camera roll",  # 2026-07-11 finding: standard Windows/OneDrive phone-sync folder name,
                    # observed on a real archive alongside "camera"/"camera uploads" above
    # NB: no generic "archive"/"архив" entry here (added then reverted 2026-07-11) -- that
    # word is too plausible as someone's real, deliberately-named album folder to blanket-
    # whitelist. The actual problem (a Yandex.Disk export's every zip unpacking into an
    # internal folder literally called "archive") is fixed at the root instead: an archive's
    # OWN filename now anchors the album whenever nothing meaningful exists on the disk-side
    # path leading to it, and folder names found INSIDE any archive are never trusted on
    # their own to name an album -- see find_album()'s archive_boundary_idx.
]
# 2026-07-11: our OWN archive's top-level segments (p.5.2б) -- if SOURCE is pointed at the
# root of an already-built archive ("cascade" re-run), these must never themselves be
# swallowed whole as one giant "album". UNLIKE DEFAULT_DUMP_SEGMENT_NAMES above, this is NOT
# exposed as a photoarchive_config.yaml override -- if a user removed "albums" from an editable list
# (accidentally or not) and then pointed SOURCE at an already-built TARGET, the "Albums"
# segment would stop being recognised as internal scaffolding and get swallowed as a real
# album. Always unioned into the effective dump-name set inside is_dump_segment(), on top of
# whatever the user configured.
DUMP_SEGMENT_NAMES_PROTECTED = frozenset({
    "bydate", "albums", "raw",
    "_unsorted",  # disputed files' top-level home (see Config.dispute) -- same self-eating
                  # protection as bydate/albums/raw above
    "__photoarchive__",  # 2026-07-20 (пятый заход): предлагаемое по умолчанию имя папки-
                         # архива верхнего уровня (см. confirm_drive_root_target_interactively()/
                         # resolve_drive_root_conflict()/prompt_target_submenu()) -- без этой
                         # защиты сканирование ДИСКА ЦЕЛИКОМ (не самого архива, а всего, что
                         # выше него) приняло бы "__PhotoArchive__" за имя альбома, и все
                         # настоящие альбомы внутри (Albums\Свадьба\...) схлопнулись бы в один
                         # притворный альбом с именем контейнера, тот же принцип, что и с
                         # bydate/albums/raw/_unsorted выше, только для родительской папки.
})
# run_analyze(self_scan=True) (живой репорт пользователя, 2026-08-01, "Паспорт архива"): верхние
# сегменты, внутри которых find_album() ЗАКОНОМЕРНО не находит альбом на TARGET (ByDate/RAW --
# листовые день/месяц-папки безусловно dump-тэгнуты, см. DUMP_TAG в is_dump_segment();
# _Unsorted -- сам по себе dump-имя) -- не считать это "файлом вне архива, добавленным в обход
# программы" (n_dump_items). "albums" сюда НЕ входит: файл прямо в "Albums\" без имени альбома
# под ним -- это и есть настоящая находка "мимо программы".
_PASSPORT_SELF_SCAN_RECOGNIZED_TOP = frozenset({"bydate", "raw", "_unsorted"})
DEFAULT_DUMP_SEGMENT_PREFIXES = ["whatsapp", "telegram"]
# 2026-07-11, по запросу пользователя: ручной способ пометить конкретную папку-источник как
# "не альбом, сортировать по дате", даже если её имя иначе выглядело бы как настоящий альбом
# (например, папка облачной синхронизации "Яндекс_диск"). Пользователь переименовывает СВОЮ
# папку вручную ("~Яндекс_диск") -- программа исходники никогда не переименовывает сама, это
# однозначно read-only сигнал. См. is_dump_segment().
FORCE_DUMP_PREFIX = "~"
# 2026-08-11, по запросу пользователя ("хочу весь диск D: разложить по датам, тильку на
# каждую папку верхнего уровня проставлять не хочу"): тот же список dump_segment_names/
# extra_dump_segment_names, что и обычные имена папок выше, но запись вида "D:" (буква диска
# + двоеточие, БЕЗ обратного слэша) значит "весь этот диск целиком -- по датам", не "папка,
# которая называется D:". Коллизии с реальной папкой в принципе быть не может: двоеточие --
# один из символов, запрещённых Windows в имени файла/папки (см. _WINDOWS_INVALID_CHARS_RE
# ниже) -- папка не может называться "D:" НИ ПРИ КАКИХ обстоятельствах, а голая буква без
# двоеточия ("D") -- уже обычное, вполне легальное имя папки и под этот шаблон не подходит
# (не матчит регекс ниже, требующий двоеточие ровно вторым и последним символом), проверяется
# как любое другое имя через is_dump_segment(). Config.__post_init__() вынимает такие записи
# из dump_segment_names_lower (там от них всё равно не было бы толку -- ни один сегмент пути
# не может содержать ":", is_dump_segment() никогда бы не совпал) в отдельное множество
# bydate_only_drives -- см. его же докстринг за тем, как оно используется.
_DRIVE_MARKER_RE = re.compile(r"^[a-z]:$")


def _split_drive_markers(names: set) -> tuple:
    """Разделяет уже нормализованное (нижний регистр, см. _clean_str_set()) множество имён на
    (обычные dump-имена без изменений, множество "d:"-меток дисков целиком) -- см.
    _DRIVE_MARKER_RE выше за объяснением синтаксиса и почему коллизия с реальным именем папки
    невозможна."""
    drives = frozenset(n for n in names if _DRIVE_MARKER_RE.match(n))
    rest = {n for n in names if n not in drives}
    return rest, drives


def _source_drive_is_bydate_only(source: str, bydate_only_drives) -> bool:
    """True, если буква диска source входит в bydate_only_drives (множество "d:"-строк, см.
    _split_drive_markers() выше) -- Config.__post_init__() кладёт результат в
    self.source_bydate_only, find_album() при этом флаге пропускает поиск альбома вообще (см.
    его же параметр bydate_only).

    Вынесена в отдельную чистую функцию (а не инлайн-выражение в __post_init__) по той же
    причине, что и _volume_likely_gone() (REVIEW-HANDOFF.md, Раунд 41 [БЛОКЕР] 2, см. её
    докстринг): ntpath.splitdrive(), не os.path.splitdrive() -- этот код разбирает букву
    Windows-диска (программа только для Windows), а os.path.splitdrive() молча алиасится на
    posixpath на Linux, где тесты и гоняются (ubuntu-latest, .github/workflows/ci.yml), и
    никогда не распознал бы букву диска вообще. Отдельная функция даёт юнит-тестам вызвать её
    напрямую с литеральной Windows-строкой (`r"D:\\Photos"`), в обход Config.__post_init__()'s
    os.path.isabs() -- та проверка сама алиасится на posixpath и отклонила бы такой путь как
    "не абсолютный" при прогоне на Linux, так что полноценный Config(source="D:\\...") в этом
    тестовом окружении в принципе не собрать."""
    return ntpath.splitdrive(source)[0].lower() in bydate_only_drives


DUMP_SEGMENT_REGEXES = [
    re.compile(r"^\d{3}[A-Za-z]+$"),
    # 2026-07-11 finding: Windows' own default name for an unrenamed new folder (seen THREE
    # times on one real archive, including a numbered "Новая папка (2)" sibling -- Windows
    # appends " (N)" for each further unnamed folder in the same place) -- unambiguous, nobody
    # deliberately leaves a real photo album named this.
    re.compile(r"^новая папка(\s\(\d+\))?$"),
    re.compile(r"^new folder(\s\(\d+\))?$"),
]
# 2026-07-11: a bare 6-8 digit folder name (YYYYMMDD/YYMMDD-shaped, e.g. "20240802") is dump
# ONLY when deciding which segment gets to NAME the album -- an album literally called
# "20240802" is never wanted. But once a real album has already been found further up the
# path, that same folder, dragged in unrenamed straight from a camera/phone export, very
# plausibly represents a deliberate day-grouping the user wants kept -- the same reasoning
# that already exempts a date WITH separators ("2015-08-20") from being dump at all. Any OTHER
# bare digit sequence (short like "101", or 9+ digits) has no such exemption -- see
# is_dump_segment()'s n.isdigit() branch, which handles ALL pure-digit segments together so
# a blanket "^\d+$" pattern here can no longer silently shadow this 6-8-digit exemption
# (that shadowing was a real bug caught by test_bare_digit_date_folder_kept_inside_album_but_not_as_album_name).
DUMP_SEGMENT_DATE_REGEX = re.compile(r"^\d{6,8}$")

# Задача B (SESSION-HANDOFF.txt, "проактивные советы для [2] Пробный прогон"): ещё одна
# date-подобная форма подпапки, кроме голых 6-8 цифр выше -- с разделителями ("2020-05" /
# "2020-05-01"), которую облачные синхронизаторы (Google Photos/iCloud/OneDrive и т.п.) тоже
# нередко создают сами. Используется ТОЛЬКО для агрегации album_profiles (см.
# _process_record()) -- не участвует в маршрутизации/is_dump_segment(), потому что
# is_dump_segment() уже намеренно НЕ считает такие имена dump (реальный альбом может законно
# называться "2020-05-01", см. её же докстринг) -- здесь же нужен просто структурный сигнал,
# не решение "альбом или нет".
_ALBUM_PROFILE_DATE_SUBDIR_REGEX = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _looks_like_date_subdir(name: str) -> bool:
    n = name.strip()
    return bool(DUMP_SEGMENT_DATE_REGEX.match(n) or _ALBUM_PROFILE_DATE_SUBDIR_REGEX.match(n))

# Public name (see __version__ banner / PhotoArchive.exe), deliberately NOT the internal
# "photosort" codename -- an end user re-ingesting their own already-built archive as a new
# SOURCE (p.5.2) has only ever seen "PhotoArchive". Appended verbatim to the end of every
# day/month/month-unknown folder name build_bydate_dest_dir() generates.
DUMP_TAG = " [PhotoArchive]"

# Precomputed defaults used whenever is_dump_segment()/find_album() are called WITHOUT an
# explicit cfg (e.g. bare calls in ci/windows_ci_test.py) -- keeps those call sites working
# unmodified. PROTECTED names are unioned in here too, same as Config.dump_segment_names_lower
# does for the configurable path (see Config.__post_init__).
_DEFAULT_DUMP_SEGMENT_NAMES_LOWER = frozenset(
    n.lower() for n in DEFAULT_DUMP_SEGMENT_NAMES) | DUMP_SEGMENT_NAMES_PROTECTED
_DEFAULT_DUMP_SEGMENT_PREFIXES_TUPLE = tuple(DEFAULT_DUMP_SEGMENT_PREFIXES)


def is_dump_segment(name: str, *, dump_names=None, dump_prefixes=None) -> bool:
    """2026-08-08 (альбомный редизайн, по прямому запросу пользователя -- "чем проще, тем
    лучше для пользователя"): один и тот же результат для сегмента НЕЗАВИСИМО от его позиции
    на пути -- day-folder-экземпция (`for_subpath`, было до этой версии) убрана целиком, как и
    любые другие позиционные исключения. Служебное имя -- всегда служебное, будь оно первым
    сегментом под SOURCE или глубоко внутри уже найденной ветки (см. find_album()).

    dump_names/dump_prefixes (2026-07-11, photoarchive_config.yaml exposure): the effective, already-
    lowered set/tuple to check against -- production call sites pass
    cfg.dump_segment_names_lower/cfg.dump_segment_prefixes_tuple (user config ∪
    DUMP_SEGMENT_NAMES_PROTECTED, see Config.__post_init__). Left at their default (None) this
    falls back to the module defaults (DEFAULT_DUMP_SEGMENT_NAMES ∪ PROTECTED,
    DEFAULT_DUMP_SEGMENT_PREFIXES) -- callers with no cfg in scope (tests) keep working as-is."""
    if dump_names is None:
        dump_names = _DEFAULT_DUMP_SEGMENT_NAMES_LOWER
    if dump_prefixes is None:
        dump_prefixes = _DEFAULT_DUMP_SEGMENT_PREFIXES_TUPLE
    stripped = name.strip()
    if stripped.endswith(DUMP_TAG):
        # A day/month folder we generated ourselves (build_bydate_dest_dir) -- unambiguous,
        # no user ever types this tag by hand. This is the ONLY thing that marks a
        # date-shaped segment as dump -- see below, "looks like a date" alone is no longer
        # sufficient (p.5.2): a real user album can legitimately be named "2000-10-10".
        return True
    if stripped.startswith(FORCE_DUMP_PREFIX):
        # 2026-07-11, по просьбе пользователя: ручной способ заставить программу считать
        # конкретную папку НЕ альбомом, даже если её имя иначе выглядело бы как настоящее
        # (пример: папка синхронизации облака "Яндекс_диск" -- реальное, осмысленное на вид
        # имя, но пользователь хочет, чтобы её содержимое раскладывалось по дате). Пользователь
        # переименовывает СВОЮ папку-источник вручную (программа исходники не трогает) --
        # "~Яндекс_диск".
        return True
    n = stripped.lower()
    if n in dump_names:
        return True
    if n.startswith(dump_prefixes):
        return True
    for rx in DUMP_SEGMENT_REGEXES:
        if rx.match(n):
            return True
    if n.isdigit():
        # Любой голый цифровой сегмент -- dump безусловно, включая 6-8-значный день-номер
        # (`20240802`) -- раньше у него была экземпция ВНУТРИ уже найденного альбома
        # (DUMP_SEGMENT_DATE_REGEX), больше нет никаких позиционных исключений.
        return True
    return False


def _clean_str_set(items) -> set:
    """Нормализует пользовательский список из photoarchive_config.yaml в множество строк
    в нижнем регистре -- элементы, которые НЕ являются строкой (в частности, вложенный
    список/словарь), молча отбрасываются, а не приводятся через str().

    Причина именно отбрасывать, а не str()-ить что попало: YAML-якоря/алиасы (`&a`/`*a`)
    позволяют СЖАТО описать экспоненциально большую вложенную структуру ("billion laughs") --
    yaml.safe_load() сам по себе парсит такой файл мгновенно (алиасы -- это просто ссылки на
    один и тот же Python-объект, не копии), но str()/repr() на такой ссылке рекурсивно
    разворачивает её ЦЕЛИКОМ в строку. Крошечный (несколько сотен байт) файл конфига с
    десятком уровней вложенности разворачивался в этой функции в сотни МБ строк и держал
    Config(...) занятым ~15 секунд -- найдено адверсариальным тестированием, не гипотетически.
    Обычный корректный конфиг (список строк) этот фильтр не меняет никак."""
    return {item.strip().lower() for item in (items or []) if isinstance(item, str)}


@dataclass
class Config:
    source: str
    target: str
    workdir: str = None  # None -> WORKDIR (папка рядом с exe/скриптом), см. __post_init__
    place_lookup: str = "offline"
    home_country: str = "RU"
    archive_hash_cache: bool = True
    # SESSION-HANDOFF.txt, 2026-08-09 (одиннадцатая задача, "как ускорить анализ"): sniff_signature()
    # читает первые 32 байта КАЖДОГО файла отдельным open() -- заметные накладные расходы на
    # медленном/сетевом диске. По умолчанию ВЫКЛЮЧЕНА -- проверка не выполняется в обычном
    # анализе ([1]/CLI analyze, mode=="analyze-quick"), безусловно ВСЕГДА выполняется в
    # self-scan ("Паспорт архива") независимо от этого флага (полная проверка уже собранного
    # архива, там сокращать смысла нет, см. photosort_win.py:run_analyze()).
    check_signature: bool = False
    max_archive_depth: int = 8
    max_dest_path: int = 240
    small_image_px: int = 640
    free_space_margin_gb: float = 10.0
    dry_run: bool = False
    sample_limit: int = 0
    read_retry_count: int = 3
    read_retry_delay: float = 5.0
    bydate_granularity: str = "month"  # day | month | year | flat -- гранулярность папок ByDate
        # 2026-07-11: дефолт сменён с "day" на "month" по прямой просьбе пользователя --
        # архивы, уже собранные под "day" (append-only), НЕ переименовываются задним числом;
        # смена дефолта касается только НОВЫХ TARGET (или явных photoarchive_config.yaml/CLI-переопределений).
    scan_system_dirs: bool = False  # заходить ли в системные папки (см. SYSTEM_DIR_ENV_VARS)
    default_exclude_dirs: list = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIR_NAMES))
        # 2026-07-11: редактируемый (в отличие от HARD_EXCLUDE_DIRS) список папок, пропускаемых
        # по умолчанию -- эвристика "скорее всего не фото", не защита ОС. Пользователь может
        # убрать любое имя в photoarchive_config.yaml (например "$recycle.bin", если хочет вытащить
        # удалённые файлы) -- см. DEFAULT_EXCLUDE_DIR_NAMES/photoarchive_config.yaml.example.
    extra_exclude_dirs: list = field(default_factory=list)  # доп. исключения ПОВЕРХ default_exclude_dirs
    dump_segment_names: list = field(default_factory=lambda: list(DEFAULT_DUMP_SEGMENT_NAMES))
        # 2026-07-11: тот же паттерн default_+extra_, что default_exclude_dirs выше --
        # редактируемая эвристика "не альбом, скорее всего мусорное имя" (dcim, camera,
        # downloads, ...). НЕ включает DUMP_SEGMENT_NAMES_PROTECTED (bydate/albums/raw/
        # _unsorted) -- те самозащита архива от самопоедания при каскадном прогоне и никогда
        # не отдаются в конфиг, см. dump_segment_names_lower ниже.
    extra_dump_segment_names: list = field(default_factory=list)  # доп. имена ПОВЕРХ dump_segment_names
    dump_segment_prefixes: list = field(default_factory=lambda: list(DEFAULT_DUMP_SEGMENT_PREFIXES))
    extra_dump_segment_prefixes: list = field(default_factory=list)  # доп. префиксы ПОВЕРХ dump_segment_prefixes
    mirror_raw: bool = True  # False = избыточный RAW (есть парный JPEG) не мирроить; одинокий RAW спасается всегда
    tmp_extract_dir: str = None  # None -> тот же физический том, что TARGET (см. __post_init__)
    raw_layout: str = "mirror"  # mirror (по умолчанию) | sibling -- см. raw_dest_dir()
    debug: bool = False  # p.5.3: подробные [DEBUG]-строки в actions.log (причины решений,
                          # полный traceback на ошибках) -- для тестеров/разбора багов между
                          # релизами, НЕ ротируется отдельно от остального actions.log
    suppress_logs: bool = False  # ТЗ-меню 2026-07-10, раздел 5: интерактивный "пробный
        # прогон" из голого меню репетирует archive dry_run=True, НО не создаёт __служебные_файлы\
        # и не пишет CSV/summary.txt в TARGET -- результат только на экране. НЕТ отдельной
        # argparse/yaml-ручки под этим именем -- поле не читается напрямую из
        # photoarchive_config.yaml/CLI-флага "--suppress-logs" (такого флага нет). Два места
        # конструируют Config с этим флагом: интерактивный слой (run_bare_launch()) напрямую;
        # CLI `_main()` -- run_for_source(..., suppress_logs=args.dry_run) (речь пользователя,
        # 2026-08-18 -- раньше CLI --dry-run сюда всегда передавал False и писал настоящие
        # CSV/archive_cache.db/архивный скелет в TARGET, не убирая за собой; теперь тот же
        # механизм, что уже использует интерактивный [2]).

    def __post_init__(self):
        if self.bydate_granularity not in ("day", "month", "year", "flat"):
            raise ValueError(
                f"bydate_granularity должен быть day/month/year/flat, получено: {self.bydate_granularity!r}"
            )
        if self.raw_layout not in ("mirror", "sibling"):
            raise ValueError(
                f"raw_layout должен быть mirror/sibling, получено: {self.raw_layout!r} "
                "(flat сознательно не реализован)"
            )
        # Security audit finding #7: photoarchive_config.yaml is user-editable and none of these numeric
        # fields were range-checked. free_space_margin_gb in particular is finding #2 --
        # a negative value silently defeats every free-space check in the program (see
        # _handle_archive()/atomic_copy()), letting a run fill the disk to literally 0 bytes
        # free before an unhandled OSError finally stops it.
        if self.free_space_margin_gb < 0:
            raise ValueError(
                f"free_space_margin_gb не может быть отрицательным (получено "
                f"{self.free_space_margin_gb!r}) -- отрицательный запас отключает защиту от "
                f"заполнения диска."
            )
        if self.max_archive_depth < 1:
            raise ValueError(
                f"max_archive_depth должен быть не меньше 1 (получено {self.max_archive_depth!r})"
            )
        if self.max_dest_path < 10:
            raise ValueError(
                f"max_dest_path должен быть не меньше 10 символов, иначе не остаётся места даже "
                f"на короткое имя с расширением (получено {self.max_dest_path!r})"
            )
        if self.small_image_px < 0:
            raise ValueError(
                f"small_image_px не может быть отрицательным (получено {self.small_image_px!r})"
            )
        if self.sample_limit < 0:
            raise ValueError(
                f"sample_limit не может быть отрицательным -- 0 значит «без ограничения» "
                f"(получено {self.sample_limit!r})"
            )
        if self.read_retry_count < 0:
            raise ValueError(
                f"read_retry_count не может быть отрицательным (получено {self.read_retry_count!r})"
            )
        if self.read_retry_delay < 0:
            raise ValueError(
                f"read_retry_delay не может быть отрицательным (получено {self.read_retry_delay!r})"
            )
        if not os.path.isabs(self.source):
            raise ValueError(
                f"SOURCE ({self.source}) не является полным путём -- укажите полный путь, "
                f"начиная с буквы диска (D:\\...) или \\\\сервер\\ресурс\\..."
            )
        if not os.path.isabs(self.target):
            raise ValueError(
                f"TARGET ({self.target}) не является полным путём -- укажите полный путь, "
                f"начиная с буквы диска (D:\\...) или \\\\сервер\\ресурс\\..."
            )
        self.source = os.path.abspath(self.source)
        self.target = os.path.abspath(self.target)
        source_real = os.path.normcase(os.path.realpath(self.source))
        target_real = os.path.normcase(os.path.realpath(self.target))
        if source_real == target_real:
            raise ValueError(
                f"SOURCE и TARGET указывают на один и тот же путь ({self.target}) -- "
                f"архив читал бы сам себя как источник. Укажите разные пути."
            )
        # На native Windows нет физической защиты ro-mount, которая раньше не давала
        # процессу писать в источник -- источники read-only теперь только потому, что код
        # НИКОГДА не формирует путь записи внутри дерева источника. TARGET подпапкой внутри
        # SOURCE -- ПОДДЕРЖИВАЕМЫЙ, документированный сценарий (например, SOURCE=D:\,
        # TARGET=D:\Архив фото), от самопоедания в этом случае защищает
        # SourceWalker._walk_dir (пропускает TARGET целиком при обходе) -- см. RULES.md.
        # А вот обратное -- SOURCE внутри TARGET -- ничем не защищено: walk() не сравнивает
        # с target_real свой собственный корень (проверка только "not is_root"), так что
        # SOURCE, указывающий вглубь TARGET, читал бы (и мог бы повторно поглощать) файлы,
        # которые этот же прогон только что сам туда записал. Явно отклоняем такой запуск.
        if source_real.startswith(target_real + os.sep):
            raise ValueError(
                f"SOURCE ({self.source}) находится внутри TARGET ({self.target}) -- "
                f"прогон мог бы повторно поглощать файлы, только что записанные им же самим. "
                f"Укажите SOURCE вне дерева TARGET."
            )
        self.workdir = self.workdir or WORKDIR
        # Review Round 22 (2026-07-20) [БЛОКЕР]: TARGET == WORKDIR (или TARGET внутри WORKDIR)
        # раньше ничем не защищался -- confirm_drive_root_target_interactively()/
        # resolve_drive_root_conflict() могли (до переименования папки в 18eb212) молча
        # подставить TARGET = WORKDIR, а FAQ.md советует при неудаче "удалить TARGET целиком":
        # если TARGET совпал с WORKDIR, это стирает саму программу+config+логи. Симметрично
        # уже существующей проверке SOURCE/TARGET выше (по той же normcase(realpath(...))+
        # startswith схеме).
        workdir_real = os.path.normcase(os.path.realpath(self.workdir))
        if target_real == workdir_real:
            raise ValueError(
                f"TARGET ({self.target}) совпадает с рабочей папкой программы ({self.workdir}) -- "
                f"это папка, где лежат сам PhotoArchive.exe, photoarchive_config.yaml, work.db и "
                f"логи. Использовать её как архив опасно: инструкция «при неудаче удалить TARGET "
                f"целиком» (см. FAQ.md) стёрла бы саму программу вместе с настройками и логами. "
                f"Укажите другую папку для TARGET."
            )
        if workdir_real.startswith(target_real + os.sep):
            raise ValueError(
                f"Рабочая папка программы ({self.workdir}) находится внутри TARGET ({self.target}) -- "
                f"по той же причине, что и выше: TARGET целиком удалить было бы нельзя, не потеряв "
                f"саму программу. Укажите TARGET вне папки, где лежит PhotoArchive.exe."
            )
        self.index_db = os.path.join(self.workdir, "work.db")
        self.albums_root = os.path.join(self.target, "Albums")
        self.bydate_root = os.path.join(self.target, "ByDate")
        self.raw_root = os.path.join(self.target, "RAW")
        self.undated_root = os.path.join(self.bydate_root, "0000-undated")  # см. RULES.md
        # "УМБРЕЛЛА" __служебные_файлы\ (переименована из "_photosort\" 2026-07-11 -- то имя
        # было внутренним старым названием проекта до ребрендинга в PhotoArchive и ничего не
        # говорило пользователю о назначении папки; всё остальное в интерактивном слое и так
        # уже целиком на русском, см. RULES.md): все служебные (не медиа-) папки архива живут
        # под одним корнем, а не разбросаны по TARGET как раньше (_disputed, _logs, _prompt,
        # _tmp_extract у самого TARGET) -- проще один раз объяснить пользователю и один раз
        # защитить маркером SKIP_PHOTOSORT.txt (см. ensure_target_layout).
        self.photosort_dir = os.path.join(self.target, "__служебные_файлы")
        # 2026-07-11 finding: disputed files are REAL photos (just not confidently
        # classified), not disposable metadata like logs/tmp_extract -- burying them one
        # level inside a folder that otherwise holds only safe-to-delete service data invites
        # exactly the "looks technical, must be safe to delete" mistake. Moved to a TOP-LEVEL
        # sibling of Albums/ByDate/RAW, same standing as those, and deliberately NOT under the
        # SKIP_PHOTOSORT.txt umbrella -- same reasoning as ByDate\0000-undated: if TARGET is
        # later reused as SOURCE, a disputed file should be free to "graduate" into a real
        # place once rules/evidence improve, not be walled off. "_unsorted" renamed to
        # "_Unsorted" same session, same consistency reasoning as the umbrella rename above.
        self.dispute = os.path.join(self.target, "_Unsorted")
        self.logs = os.path.join(self.photosort_dir, "logs")
        self.prompt_dir = os.path.join(self.photosort_dir, "prompt")
        # TMP_EXTRACT_DIR: конфигурируемый (см. photoarchive_config.yaml.example). Дефолт -- НЕ системный
        # %TEMP%, а {TARGET}\__служебные_файлы\tmp_extract\ -- уже гарантированно на том же
        # физическом ТОМЕ, что TARGET (это подпапка самого TARGET), так что финализация
        # файлов из архива по умолчанию всегда получает быстрый rename (см. place_file()/
        # same_volume() в блоке IO_COPY), без специальной логики поиска "корня тома".
        # Причина держать распаковку вообще НЕ внутри дерева Albums/ByDate/RAW: source может
        # быть read-only носителем (CD/DVD, смонтированный ISO), а класть временные файлы
        # прямо в архивные корни нельзя (append-only, никаких временных артефактов в них).
        # Явный tmp_extract_dir в конфиге -- всегда в приоритете (пользователь может указать
        # другой том, например быстрый SSD -- тогда финализация деградирует до copy, см.
        # report_environment()).
        #
        # 2026-08-19, живая находка пользователя (Ctrl+C посреди разбора архива в dry-run):
        # suppress_logs=True ([2] Пробный прогон/CLI --dry-run) обязан не трогать TARGET
        # вовсе (см. ensure_target_layout()'s `not cfg.suppress_logs` гейт и докстринг
        # run_for_source()) -- но archive-распаковка ВСЕГДА реальна (нужно заглянуть внутрь
        # архива, чтобы честно показать, что было бы скопировано), и без этой ветки
        # tmp_extract по умолчанию всё равно указывал бы под TARGET. _handle_archive()
        # аккуратно убирает hash-именованную папку с распакованным содержимым по завершении
        # (и _cleanup_own_tmp_extract_entries() -- после Ctrl+C), но родительскую цепочку
        # __служебные_файлы\tmp_extract\ (и тем самым сам TARGET, если его не было) этим не
        # убрать -- она успевала физически возникнуть на диске как побочный эффект. Причина
        # same-volume-rename выше (быстрая финализация вместо copy) здесь неприменима: dry_run
        # никогда не доходит до place_file()/финализации (см. _process_record()) -- обмен
        # тома ничего не замедляет.
        #
        # НЕ self.workdir (первая версия фикса, отклонена пользователем на месте): портативный
        # .exe многие запускают прямо с флешки -- WORKDIR тогда физически СОВПАДАЕТ с этой
        # флешкой (или тем же ограниченным томом), ровно то ограниченное место, которого
        # распаковка архива в dry-run обязана НЕ требовать. Системный %TEMP% (tempfile.
        # gettempdir(), тот же источник, что уже использует _NO_TARGET_PLACEHOLDER ниже по
        # файлу) почти всегда на системном диске, не на носителе запуска программы -- и dry-run
        # физически не пишет ничего, кроме этой временной распаковки, так что много места не
        # нужно (реальная сборка по-прежнему намеренно НЕ использует %TEMP% по умолчанию -- см.
        # обоснование выше, там same-volume-rename и большие объёмы реальны).
        # 2026-08-19, живая находка ревизора (Раунд 107): _DRY_RUN_TMP_EXTRACT_DIR -- ОБЩИЙ
        # путь на всех suppress_logs=True-процессов сразу; без PID-подпапки конкурентный прогон
        # мог удалить активную распаковку другого, ещё не завершившегося прогона (воспроизведено
        # ревизором исполнением) -- см. _OWN_TMP_EXTRACT_PID_DIR_RE/_sweep_stale_dry_run_pid_dirs().
        # os.getpid() читается один раз здесь (не при каждом обращении к cfg.tmp_extract) --
        # значение неизменно на весь прогон текущего процесса.
        if self.tmp_extract_dir:
            self.tmp_extract = os.path.abspath(self.tmp_extract_dir)
        elif self.suppress_logs:
            self.tmp_extract = os.path.join(_DRY_RUN_TMP_EXTRACT_DIR, str(os.getpid()))
        else:
            self.tmp_extract = os.path.join(self.photosort_dir, "tmp_extract")
        self.default_exclude_dirs_lower = _clean_str_set(self.default_exclude_dirs)
        self.extra_exclude_dirs_lower = _clean_str_set(self.extra_exclude_dirs)
        # 2026-07-11: эффективный набор dump-имён/префиксов для is_dump_segment()/find_album()
        # -- ВСЕГДА объединяет пользовательский photoarchive_config.yaml с DUMP_SEGMENT_NAMES_PROTECTED
        # (bydate/albums/raw/_unsorted), даже если пользователь их не указывал или случайно
        # убрал -- эти четыре не редактируемы в принципе, см. поле dump_segment_names выше.
        combined_dump_names, self.bydate_only_drives = _split_drive_markers(
            _clean_str_set(self.dump_segment_names) | _clean_str_set(self.extra_dump_segment_names)
        )
        self.dump_segment_names_lower = combined_dump_names | DUMP_SEGMENT_NAMES_PROTECTED
        self.dump_segment_prefixes_tuple = tuple(
            _clean_str_set(self.dump_segment_prefixes)
            | _clean_str_set(self.extra_dump_segment_prefixes)
        )
        # 2026-08-11, по запросу пользователя: "D:" в dump_segment_names/
        # extra_dump_segment_names (см. _DRIVE_MARKER_RE/bydate_only_drives выше) значит "этот
        # SOURCE целиком -- по датам, без поиска альбомов вообще", как если бы КАЖДЫЙ сегмент
        # пути был отравлен -- проверяется ОДИН раз здесь, по букве диска ИМЕННО ЭТОГО SOURCE
        # (Config создаётся заново на каждый source, см. run_for_source()/
        # run_analyze_for_source() -- self.source уже финальный абсолютный путь к этому
        # моменту). Сама проверка вынесена в отдельную чистую функцию
        # (_source_drive_is_bydate_only()) по тому же мотиву, что и у _volume_likely_gone() --
        # см. её докстринг.
        self.source_bydate_only = _source_drive_is_bydate_only(self.source, self.bydate_only_drives)

# ============================================================================
# DB  (from pipeline/db.py)
# ============================================================================


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive (
    path TEXT PRIMARY KEY,
    root TEXT,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    phash TEXT,
    duration REAL,
    type TEXT,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER
);
CREATE INDEX IF NOT EXISTS idx_archive_sha ON archive(sha256);
CREATE INDEX IF NOT EXISTS idx_archive_size ON archive(size);

CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    read_path TEXT,            -- actual on-disk path usable for opening the file right now
    origin_display TEXT,       -- human path for logs, e.g. "Foto2015.zip -> 2015/Crimea/IMG_1234.jpg"
    rel_path TEXT,             -- path relative to the walked root (source root or archive internal root),
                               -- used for album/dump segment detection
    size INTEGER,
    mtime REAL,
    ext TEXT,
    type TEXT,                 -- image / raw / video / archive / other
    sha256 TEXT,
    phash TEXT,
    width INTEGER,
    height INTEGER,
    aspect REAL,
    duration REAL,
    exif_dt TEXT,
    camera TEXT,
    gps_lat REAL,
    gps_lon REAL,
    place TEXT,
    is_media INTEGER,
    media_note TEXT,
    raw_pair_read_path TEXT,   -- for RAW: paired jpeg read_path (if any); for JPEG: paired RAW read_path
    date_value TEXT,
    date_tier TEXT,
    date_conf TEXT,
    date_evidence TEXT,
    decision TEXT,
    dest_path TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_sha ON source(sha256);
CREATE INDEX IF NOT EXISTS idx_source_size ON source(size);

CREATE TABLE IF NOT EXISTS archive_cache (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    phash TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER,
    -- Речь пользователя, 2026-08-02 ("почему Фаза 1 быстрая, а паспорт медленный -- разве не
    -- один алгоритм?"): Фаза 1 никогда не зовёт exiftool (ей нужны только sha256/pHash для
    -- пула дедупа) -- паспорт зовёт БЕЗУСЛОВНО на каждый файл (дата/камера/GPS), даже при
    -- полном попадании в кэш выше, потому что EXIF-поля тут не кэшировались вовсе. exif_cached
    -- -- 0/NULL для старых строк (мигрировавших ALTER TABLE, см. connect()) -- отличает
    -- "exif ещё не проверялся" (нужен настоящий вызов exiftool) от "проверялся, снимок
    -- действительно без EXIF" (exif_dt/camera/gps_lat пустые, но ЭТО и есть закэшированный
    -- ответ, не "не знаю").
    exif_cached INTEGER,
    exif_dt TEXT,
    exif_dt_source TEXT,
    camera TEXT,
    gps_lat REAL,
    gps_lon REAL
);

-- 2026-08-07, по прямой просьбе пользователя (боевой прогон, домашнее видео на DVD): реестр
-- уже скопированных DVD-юнитов (папка VIDEO_TS со всем содержимым -- см.
-- SourceWalker._handle_dvd_unit()). fingerprint -- комбинированный хеш всех файлов юнита
-- (_dvd_unit_fingerprint()), не отдельного файла -- юнит либо уже архивирован целиком, либо
-- нет, "объединение недопустимо" (требование пользователя), частичного совпадения не бывает.
-- Живёт в ТОМ ЖЕ archive_cache.db, что и archive_cache выше -- тот же принцип персистентности
-- между прогонами, не отдельный файл ради одной маленькой таблицы.
CREATE TABLE IF NOT EXISTS dvd_units (
    fingerprint TEXT PRIMARY KEY,
    dest_path TEXT,
    n_files INTEGER,
    total_bytes INTEGER,
    created_at TEXT
);
"""


def _migrate_archive_cache_exif_columns(conn: sqlite3.Connection) -> None:
    """Речь пользователя, 2026-08-02: exif_cached/exif_dt/exif_dt_source/camera/gps_lat/gps_lon
    -- новые колонки archive_cache. "CREATE TABLE IF NOT EXISTS" (SCHEMA выше) -- no-op на уже
    существующем archive_cache.db (например, смигрированном ещё до этой правки, где кэш уже
    накопил реальные sha256/pHash пользователя) -- новые колонки сами не появятся, ALTER TABLE
    нужен явно. Проверка через PRAGMA table_info (не try/except на "duplicate column" -- та
    ошибка на некоторых сборках sqlite3 неотличима по тексту от других OperationalError, дороже
    и менее прямолинейно, чем прочитать список колонок заранее)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(archive_cache)")}
    for col, coltype in (
        ("exif_cached", "INTEGER"), ("exif_dt", "TEXT"), ("exif_dt_source", "TEXT"),
        ("camera", "TEXT"), ("gps_lat", "REAL"), ("gps_lon", "REAL"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE archive_cache ADD COLUMN {col} {coltype}")


def connect(index_db_path: str) -> sqlite3.Connection:
    """winlong() -- index_db_path раньше был всегда рядом с .exe (WORKDIR, короткий путь по
    построению), но с archive_cache_db_path() (речь пользователя, 2026-08-02) кэш теперь может
    жить глубоко внутри TARGET -- тот же MAX_PATH-риск, что и у любого другого прямого
    файлового вызова в этом кодовом пути (см. os.stat(winlong(...))/_makedirs_iterative()),
    просто раньше ни разу не проявлялся для этой конкретной функции. Без этого
    sqlite3.connect() падает на >260-символьном пути -- живая находка live CI (test_long_path)."""
    conn = sqlite3.connect(winlong(index_db_path))
    conn.executescript(SCHEMA)
    _migrate_archive_cache_exif_columns(conn)
    conn.commit()
    return conn


def db_reset(index_db_path: str) -> sqlite3.Connection:
    """work.db is ephemeral: rebuilt fresh every run (except archive_cache table,
    which is intentionally preserved across runs when ARCHIVE_HASH_CACHE=1)."""
    conn = sqlite3.connect(winlong(index_db_path))
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM archive")
    conn.execute("DELETE FROM source")
    conn.commit()
    return conn


def archive_cache_db_path(archive_root: str) -> str:
    """Речь пользователя, 2026-08-02 ("где хранятся хеши? ... я бы хранил в архиве рядом с
    логами"): персистентный кэш хешей АРХИВА живёт РЯДОМ С ЕГО ЛОГАМИ
    (<archive_root>\\__служебные_файлы\\archive_cache.db), не рядом с .exe. До этой правки он
    жил в work.db в WORKDIR (папке программы) -- тот же архив, прогнанный через ДРУГУЮ копию
    портативного .exe (другую папку), не видел уже посчитанную историю хешей вообще, хотя сам
    архив на диске не менялся -- живой пример: work.db на Рабочем столе содержал 32244 живых
    хеша для реального архива пользователя, недостижимых для любой другой копии .exe.

    archive_root -- либо cfg.target (обычная сборка/`index_archive()`, self_scan=False), либо
    cfg.source под self_scan=True (Паспорт архива, run_passport() -- там cfg.target -- фиктивный
    _NO_TARGET_PLACEHOLDER, реальный архив это source, см. run_analyze())."""
    return os.path.join(archive_root, "__служебные_файлы", "archive_cache.db")


def _open_archive_cache_conn(archive_root: str) -> sqlite3.Connection:
    """None, если у archive_root ещё даже нет служебной папки -- например "analyze --target"
    (Паспорт, self-scan, см. run_passport()) запущен на папке, которая архивом ещё не является
    (analyze-режимы read-only, __служебные_файлы здесь заводить рано, см. докстринг у ANALYZE
    выше). Вызывающая сторона в этом случае просто работает без кэша (тот же эффект, что и
    archive_hash_cache=False) -- ensure_target_layout() уже создаёт эту папку до реальной
    сборки (_run_impl()), так что там соединение открывается штатно."""
    photosort_dir = os.path.join(archive_root, "__служебные_файлы")
    if not os.path.isdir(winlong(photosort_dir)):
        return None
    return connect(archive_cache_db_path(archive_root))

# ============================================================================
# HASHING  (from pipeline/hashing.py)
# ============================================================================


pillow_heif.register_heif_opener()


def sha256_file(path: str, chunk_size: int = 4 * 1024 * 1024, progress_cb=None) -> str:
    """progress_cb (2026-08-28, часть B фикса паузы -- см. _check_pause_keypress()):
    необязательный колбэк, вызывается после каждого прочитанного чанка. Нужен, чтобы пауза по
    пробелу реагировала посреди хеширования одного большого файла (многогигабайтное видео/
    архив), а не только между файлами. По умолчанию None -- ноль накладных расходов для всех
    прочих вызывающих (проверка копии, dump-скан и т.п.)."""
    h = hashlib.sha256()
    with open(winlong(path), "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            if progress_cb is not None:
                progress_cb()
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_phash_and_size(path: str):
    """Returns (phash_hex, width, height) or (None, None, None) if unreadable."""
    try:
        with Image.open(winlong(path)) as im:
            im = im.convert("L")
            w, h = im.size
            ph = imagehash.phash(im)
            return str(ph), w, h
    except Exception:
        return None, None, None


def image_size_only(path: str):
    """Returns (width, height) or (None, None) if unreadable -- используется analyze-quick
    (skip_hash=True в analyze_batch): PIL лениво декодирует заголовок для .size без разбора
    полных пиксельных данных, поэтому это на порядок дешевле, чем image_phash_and_size()
    (там ещё и convert("L") + imagehash.phash -- полное декодирование + DCT)."""
    try:
        with Image.open(winlong(path)) as im:
            return im.size
    except Exception:
        return None, None


_hamming_format_warned = False


def hamming(hash_a: str, hash_b: str) -> int:
    global _hamming_format_warned
    if hash_a is None or hash_b is None:
        return 999
    try:
        ha = imagehash.hex_to_hash(hash_a)
        hb = imagehash.hex_to_hash(hash_b)
        return ha - hb
    except Exception as e:
        # REVIEW-HANDOFF.md round 13, ticket 2c: hash_a/hash_b can come from a persistent
        # sqlite hash cache spanning years of runs -- if the hash format ever changes (e.g.
        # imagehash upgrade between .exe builds), dedup could silently stop finding
        # duplicates across the whole archive with zero trace. Warn once per process (not
        # per call -- a format mismatch fires on every comparison, would otherwise flood the
        # log) instead of staying fully silent.
        if not _hamming_format_warned:
            _hamming_format_warned = True
            log_line(f"ВНИМАНИЕ: hamming() не смог разобрать формат хеша ({e!r}) -- "
                     f"near-dup поиск может молча пропускать дубликаты (сообщение выводится "
                     f"один раз за прогон)")
        return 999


def ffprobe_json(path: str) -> dict:
    try:
        out = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True, timeout=60,
        )
        import json
        return json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def video_duration_and_resolution(path: str):
    info = ffprobe_json(path)
    duration = None
    width = height = None
    bitrate = None
    fmt = info.get("format", {})
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except Exception:
            pass
    if fmt.get("bit_rate"):
        try:
            bitrate = int(fmt["bit_rate"])
        except Exception:
            pass
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            width = s.get("width")
            height = s.get("height")
            if duration is None and s.get("duration"):
                try:
                    duration = float(s["duration"])
                except Exception:
                    pass
            break
    return duration, width, height, bitrate


def video_phash_3frames(path: str, duration: float):
    """Extract frames at 10/50/90% and phash each. Returns list of up to 3 hex phashes."""
    if not duration or duration <= 0:
        offsets = [0.5, 1.0, 1.5]
    else:
        offsets = [duration * 0.10, duration * 0.50, duration * 0.90]
    hashes = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, off in enumerate(offsets):
            frame_path = os.path.join(tmpdir, f"frame_{i}.jpg")
            try:
                subprocess.run(
                    [
                        FFMPEG_BIN, "-y", "-ss", str(max(off, 0)), "-i", path,
                        "-frames:v", "1", "-q:v", "3", frame_path,
                    ],
                    capture_output=True, timeout=30,
                )
                if os.path.exists(frame_path):
                    ph, _, _ = image_phash_and_size(frame_path)
                    if ph:
                        hashes.append(ph)
            except Exception:
                continue
    return hashes


def video_hashes_match(hashes_a, hashes_b, threshold=6) -> bool:
    if not hashes_a or not hashes_b:
        return False
    n = min(len(hashes_a), len(hashes_b))
    if n == 0:
        return False
    for i in range(n):
        if hamming(hashes_a[i], hashes_b[i]) > threshold:
            return False
    return True

# ============================================================================
# METADATA  (from pipeline/metadata.py)
# ============================================================================


EXIF_TAGS = [
    "-DateTimeOriginal", "-CreateDate", "-GPSDateStamp",
    "-QuickTime:CreateDate", "-MediaCreateDate", "-TrackCreateDate",
    "-XMP:DateCreated", "-IPTC:DateCreated",
    "-Make", "-Model",
    "-ImageWidth", "-ImageHeight",
    "-GPSLatitude", "-GPSLongitude",
    "-FileType",
]

_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_exif_date(s):
    if not s or not isinstance(s, str):
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None


def exiftool_batch(paths, batch_size=200, log=print):
    """Returns dict: path -> tag dict (raw exiftool JSON entry).
    Paths go through an -@ argfile, not raw argv: exiftool.exe on Windows does its own
    wildcard-expansion of command-line arguments (no shell globbing on Windows, so exiftool
    does it itself) and mis-parses non-ASCII bytes in the process -- any path with Cyrillic
    (or other non-Latin1) characters, e.g. a typical album name, fails with "Wildcards don't
    work in the directory specification" / "No matching files" and silently yields no EXIF
    data at all, downgrading the file straight to Tier B/C dates. -@ argfile reads paths
    from a file instead of argv, bypassing that layer entirely."""
    results = {}
    for i in range(0, len(paths), batch_size):
        chunk = paths[i:i + batch_size]
        if not chunk:
            continue
        argfile_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".args", delete=False, encoding="utf-8"
            ) as argfile:
                argfile_path = argfile.name
                for p in chunk:
                    argfile.write(p + "\n")
            out = subprocess.run(
                [EXIFTOOL_BIN, "-j", "-n", "-charset", "filename=utf8"] + EXIF_TAGS
                + ["-@", argfile_path],
                capture_output=True, timeout=120,
            )
            data = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
            # Match by position, not by the echoed SourceFile string: exiftool's JSON
            # output preserves -@ argfile input order, and on Windows the SourceFile it
            # echoes back can differ from the original path string (separator style /
            # Unicode normalization), which would silently break a string-keyed lookup and
            # downgrade every match to Tier B/C dates without any error. Positional zip is
            # exact when counts line up; fall back to string matching only if they don't
            # (e.g. exiftool dropped an unreadable file from its output).
            if len(data) == len(chunk):
                for p, entry in zip(chunk, data, strict=True):
                    results[p] = entry
            else:
                for entry in data:
                    sf = entry.get("SourceFile")
                    if sf:
                        results[sf] = entry
        except Exception as e:
            # REVIEW-HANDOFF.md round 13, ticket 2c: a chunk-wide failure here silently
            # loses EXIF for every file in the chunk (dates fall back to Tier B/C) -- on a
            # 20-50k file archive that's a large, invisible quality drop if exiftool trips
            # on one chunk (corrupt argfile, timeout). One line per failed chunk, not per
            # file, so this can't flood the log on a genuinely bad run.
            log_line(f"ВНИМАНИЕ: exiftool не смог обработать чанк файлов {i}-{i + len(chunk)} "
                     f"({e!r}) -- EXIF-даты для этих {len(chunk)} файлов будут заменены менее "
                     f"точными", log=log)
            continue
        finally:
            if argfile_path:
                try:
                    os.unlink(argfile_path)
                except OSError:
                    pass
    return results


def best_exif_datetime(tags: dict):
    """Tier A candidate date from EXIF/QuickTime/XMP/IPTC tags, in priority order."""
    for key in (
        "DateTimeOriginal", "CreateDate", "QuickTime:CreateDate",
        "MediaCreateDate", "TrackCreateDate", "XMP:DateCreated",
        "IPTC:DateCreated", "GPSDateStamp",
    ):
        dt = parse_exif_date(tags.get(key))
        if dt:
            return dt, key
    return None, None


def gps_from_tags(tags: dict):
    lat = tags.get("GPSLatitude")
    lon = tags.get("GPSLongitude")
    if lat is None or lon is None:
        return None, None
    try:
        lat, lon = float(lat), float(lon)
    except Exception:
        return None, None
    if lat == 0.0 and lon == 0.0:
        # "Null Island" -- битый/пустой GPS-тег exiftool нередко отдаёт как 0/0, а не как
        # отсутствие значения; настоящий снимок ровно в этой точке океана практически исключён.
        return None, None
    return lat, lon


def camera_from_tags(tags: dict):
    make = (tags.get("Make") or "").strip()
    model = (tags.get("Model") or "").strip()
    if make and model:
        if make.lower() in model.lower():
            return model
        return f"{make} {model}"
    return make or model or None

# ============================================================================
# CLASSIFY  (from pipeline/classify.py)
# ============================================================================


def ext_of(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith(".tar.gz"):
        return "tar.gz"
    if name.endswith(".tar.bz2"):
        return "tar.bz2"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def file_type(path: str) -> str:
    e = ext_of(path)
    if e in IMAGE_EXTS:
        return "image"
    if e in RAW_EXTS:
        return "raw"
    if e in VIDEO_EXTS:
        return "video"
    if e in ARCHIVE_EXTS or e in ("tar.gz", "tar.bz2"):
        return "archive"
    return "other"


# "обработано объектов X/Y" (_quick_media_count_estimate()/SourceWalker._tick_object()) считает
# ровно то, что тикнет _walk_dir() -- см. _would_walk_tick() ниже и докстрины обеих функций
# (2026-08-17: не считать немедийные файлы; Раунд 159: не считать бэйр .gz/.bz2).


def classify_image(path: str, width, height, camera, size_bytes: int, small_image_px: int = 640):
    """Returns (is_media: bool, note: str|None).
    Two-tier minimum size rule:
      max side < 256px            -> not media (probable icon), always disputed.
      256px <= max side < SMALL_IMAGE_PX and no camera EXIF -> media, but flagged 'small_image'
                                      (kept, not lost -- for later batch review).
      max side < SMALL_IMAGE_PX with camera EXIF, or max side >= SMALL_IMAGE_PX -> ordinary photo, no flag.
    """
    e = ext_of(path)
    if e in ("ico", "svg"):
        return False, "icon_or_svg"
    if e == "gif":
        try:
            with Image.open(winlong(path)) as im:
                if getattr(im, "is_animated", False):
                    return False, "animated_gif"
        except Exception:
            pass

    if width is None or height is None:
        return True, "low_confidence_photo"

    max_side = max(width, height)
    if max_side < 256:
        return False, "tiny_image"

    if max_side < small_image_px and not camera:
        return True, "small_image"

    return True, None


# --- А.2 (analyze-режимы): грубая проверка "расширение соответствует сигнатуре файла" ---
# Не претендует на криминалистическую точность -- задача чисто диагностическая (найти
# явно переименованные/битые-по-контейнеру файлы в источнике до сборки архива), не часть
# основного конвейера классификации "медиа/не медиа" (тот не меняется).
_SIGNATURE_TABLE = [
    (b"\xff\xd8\xff", "image"),                    # JPEG
    (b"\x89PNG\r\n\x1a\n", "image"),                # PNG
    (b"GIF87a", "image"), (b"GIF89a", "image"),     # GIF
    (b"BM", "image"),                               # BMP
    (b"II*\x00", "image"), (b"MM\x00*", "image"),   # TIFF / многие RAW-контейнеры
    (b"PK\x03\x04", "archive"), (b"PK\x05\x06", "archive"),  # ZIP
    (b"Rar!\x1a\x07", "archive"),                   # RAR
    (b"7z\xbc\xaf\x27\x1c", "archive"),             # 7z
    (b"\x1f\x8b", "archive"),                       # GZIP (в т.ч. tar.gz)
]


def sniff_signature(path: str):
    """Читает первые байты файла и грубо определяет РЕАЛЬНЫЙ тип по сигнатуре (magic bytes),
    независимо от расширения в имени. Возвращает "image"/"video"/"archive"/None
    (None = сигнатура не распознана вообще -- сравнивать не с чем, не считается
    несоответствием). Используется только в analyze-режимах (RULES.md, "несоответствие
    расширения и сигнатуры") -- не влияет на решения обычной сборки архива."""
    try:
        with open(winlong(path), "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"hevc", b"hevx"):
            return "image"
        return "video"  # остальные ftyp-бренды (isom/mp42/qt  /M4V ...) -- видео-контейнеры
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    for sig, kind in _SIGNATURE_TABLE:
        if head.startswith(sig):
            return kind
    return None


def _coarse_kind(ftype: str) -> str:
    """Огрубление file_type() до трёх категорий сравнения с sniff_signature(): raw трактуем
    как image-подобный (TIFF-based контейнеры RAW дают ту же сигнатуру II*/MM*, что и TIFF --
    точная проверка конкретного RAW-вендора вне разумного объёма диагностической эвристики)."""
    if ftype in ("image", "raw"):
        return "image"
    if ftype == "video":
        return "video"
    if ftype == "archive":
        return "archive"
    return "other"

# ============================================================================
# ARCHIVES  (from pipeline/archives.py)
# ============================================================================


def detect_archive_format(path: str):
    e = ext_of(path)
    if e == "zip":
        return "zip"
    if e == "7z":
        return "7z"
    if e == "rar":
        return "rar"
    if e == "tar":
        return "tar"
    if e in ("tar.gz", "tgz"):
        return "tar.gz"
    if e in ("tar.bz2",):
        return "tar.bz2"
    if e == "gz" and path.lower().endswith(".tar.gz"):
        return "tar.gz"
    if e == "bz2" and path.lower().endswith(".tar.bz2"):
        return "tar.bz2"
    return None


def _would_walk_tick(name: str) -> bool:
    """True, если SourceWalker._walk_dir() тикнет этот файл в счётчик «обработано объектов X/Y»
    -- то же решение, что _quick_media_count_estimate() обязан учитывать в знаменателе Y (иначе
    X и Y разъезжаются, бар «залипает»). Настоящий многофайловый архив (detect_archive_format
    truthy) тикает как ОДИН объект; image/raw/video -- каждый по объекту; всё остальное не
    тикает вовсе -- в т.ч. бэйр `.gz`/`.bz2`, который detect_archive_format() отвергает: с 0.6.4
    `_walk_dir()` пропускает такой одиночный сжатый файл как `"other"` (Раунд 159 ревью: до
    этого хелпера `file_type()=="archive"` завышал Y на каждый `.sync/core-*.log.gz`)."""
    return detect_archive_format(name) is not None or file_type(name) in ("image", "raw", "video")


class ArchiveInfo:
    def __init__(self, total_size=0, encrypted=False, entries=0, ok=True, path_traversal=False,
                 has_media_candidate=True, media_count=0):
        self.total_size = total_size
        self.encrypted = encrypted
        self.entries = entries
        self.ok = ok
        self.path_traversal = path_traversal
        # 2026-07-11 finding (live production run): a whole-disk scan hits plenty of
        # installers/backups/configs zipped up with zero photos inside -- every one of them
        # was being FULLY extracted just to discover that afterwards (see media_count in
        # _handle_archive()). The archive's own listing (already parsed for the size
        # estimate/zip-slip check above) already names every member -- default True (unknown
        # format/parse failure) so nothing is ever skipped on a guess, only when the listing
        # was actually readable and genuinely contains no plausible media/nested-archive name.
        self.has_media_candidate = has_media_candidate
        # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2: "[archive] ... найдено
        # медиафайлов N" -- нужен ТОЧНЫЙ счётчик, не просто bool. Не переиспользует
        # has_media_candidate/_member_name_is_media_candidate как есть -- та пара тоже
        # засчитывает вложенные архивы (ARCHIVE_EXTS) как "кандидатов" (у вложенного архива
        # будет своя собственная строка, когда обход дойдёт до него после распаковки внешнего),
        # что здесь дало бы завышенное число. Считается ТОЛЬКО по IMAGE_EXTS|RAW_EXTS|
        # VIDEO_EXTS, см. _member_name_is_strict_media().
        self.media_count = media_count


def _member_name_is_media_candidate(name: str) -> bool:
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext in IMAGE_EXTS or ext in RAW_EXTS or ext in VIDEO_EXTS or ext in ARCHIVE_EXTS


def _member_name_is_strict_media(name: str) -> bool:
    """Как _member_name_is_media_candidate(), но БЕЗ ARCHIVE_EXTS -- для точного счётчика
    "найдено медиафайлов N" в объект-строке Фазы 2 (SESSION-HANDOFF.txt, редизайн живого
    вывода): вложенный архив -- не медиафайл, у него будет своя отдельная строка."""
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext in IMAGE_EXTS or ext in RAW_EXTS or ext in VIDEO_EXTS


def _fmt_size_gb(size_bytes) -> str:
    """SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2 -- размер для транзиентной операции
    статус-строки ("Извлекаю (X)"/"хеширование видеофайла (X)"): "<0.1ГБ" для дробей меньше
    0.1 (иначе крошечный файл печатался бы как "0.0ГБ" -- нечитаемо), иначе одна цифра после
    запятой."""
    gb = size_bytes / 1024**3
    return "<0.1ГБ" if gb < 0.1 else f"{gb:.1f}ГБ"


def _looks_like_path_traversal(member_name: str) -> bool:
    """Security audit finding #8: a zip/7z/rar member name containing a literal '..'
    segment, or that is itself an absolute path, could make the extracting tool write
    outside dest_dir ("zip-slip") -- unlike tar (extracted here member-by-member with
    sanitize_windows_component() + filter="data", see extract_archive()), zip/7z/rar
    extraction is fully delegated to the external 7z.exe/UnRAR.exe subprocess, whose own
    path-traversal protection this code has no way to verify. Checked against the archive's
    OWN listing (already fetched for the free-space estimate) before extraction ever starts,
    independent of whatever the external binary would or wouldn't have done."""
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    return any(seg == ".." for seg in normalized.split("/"))


def _list_7z(path: str) -> ArchiveInfo:
    try:
        out = subprocess.run(
            [SEVENZIP_BIN, "l", "-slt", path],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return ArchiveInfo(ok=False)

    sep_idx = text.find("----------")
    if sep_idx == -1:
        return ArchiveInfo(ok=False)
    remainder = text[sep_idx + len("----------"):]
    blocks = re.split(r"\n\s*\n", remainder)

    total = 0
    encrypted = False
    entries = 0
    path_traversal = False
    has_media_candidate = False
    media_count = 0
    for block in blocks:
        if "Path =" not in block:
            continue
        is_folder = False
        size = 0
        block_encrypted = False
        member_path = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("Path ="):
                member_path = line.split("=", 1)[1].strip()
            elif line.startswith("Folder ="):
                is_folder = line.split("=", 1)[1].strip() == "+"
            elif line.startswith("Attributes ="):
                # 7-Zip 26.02, живой боевой прогон 2026-08-29: `l -slt` для .7z-архивов
                # больше НЕ печатает строку "Folder = +/-" -- каталог виден только по
                # DOS-атрибуту D ("Attributes = D"). Без этой ветки каждая папка внутри .7z
                # считалась файловой записью, entries завышался на число папок, и проверка
                # archive_path_traversal_suspected (extracted_count < entries, см.
                # _handle_dvd_unit()/count_extracted_files()) выбрасывала ВЕСЬ архив целиком.
                # .zip 26.02 по-прежнему печатает "Folder =" -- эта ветка ей не мешает
                # (Attributes у файла .zip никогда не начинается с D).
                attr_first = line.split("=", 1)[1].strip().split()
                if attr_first and attr_first[0].startswith("D"):
                    is_folder = True
            elif line.startswith("Size ="):
                try:
                    size = int(line.split("=", 1)[1].strip())
                except ValueError:
                    size = 0
            elif line.startswith("Encrypted ="):
                block_encrypted = line.split("=", 1)[1].strip() == "+"
        if not is_folder:
            total += size
            entries += 1
            if member_path and _member_name_is_media_candidate(member_path):
                has_media_candidate = True
            if member_path and _member_name_is_strict_media(member_path):
                media_count += 1
        if block_encrypted:
            encrypted = True
        if member_path and _looks_like_path_traversal(member_path):
            path_traversal = True
    return ArchiveInfo(total_size=total, encrypted=encrypted, entries=entries, ok=True,
                        path_traversal=path_traversal, has_media_candidate=has_media_candidate,
                        media_count=media_count)


def _list_rar(path: str) -> ArchiveInfo:
    try:
        out = subprocess.run(
            [UNRAR_BIN, "lt", "-p-", path],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return ArchiveInfo(ok=False)

    total = 0
    entries = 0
    encrypted = "encrypted" in text.lower() or "password" in text.lower()
    for m in re.finditer(r"^\s*Size:\s*(\d+)", text, re.MULTILINE):
        total += int(m.group(1))
        entries += 1
    if entries == 0 and "Type: File" not in text and "is not RAR archive" in text:
        return ArchiveInfo(ok=False)
    member_names = [m.group(1).strip() for m in re.finditer(r"^\s*Name:\s*(.+)$", text, re.MULTILINE)]
    path_traversal = any(_looks_like_path_traversal(n) for n in member_names)
    has_media_candidate = any(_member_name_is_media_candidate(n) for n in member_names)
    media_count = sum(1 for n in member_names if _member_name_is_strict_media(n))
    return ArchiveInfo(total_size=total, encrypted=encrypted, entries=entries, ok=True,
                        path_traversal=path_traversal, has_media_candidate=has_media_candidate,
                        media_count=media_count)


def _list_tar(path: str, mode: str) -> ArchiveInfo:
    try:
        with tarfile.open(winlong(path), mode) as tf:
            total = 0
            entries = 0
            has_media_candidate = False
            media_count = 0
            for m in tf.getmembers():
                if m.isfile():
                    total += m.size
                    entries += 1
                    if _member_name_is_media_candidate(m.name):
                        has_media_candidate = True
                    if _member_name_is_strict_media(m.name):
                        media_count += 1
            return ArchiveInfo(total_size=total, encrypted=False, entries=entries, ok=True,
                                has_media_candidate=has_media_candidate, media_count=media_count)
    except Exception:
        return ArchiveInfo(ok=False)


TAR_MODES = {"tar": "r:", "tar.gz": "r:gz", "tar.bz2": "r:bz2"}

# TarFile.extract(..., filter="data") -- the path-traversal-safe extraction filter (PEP 706)
# -- only exists on Python >=3.9.17/3.10.12/3.11.4/3.12. Detected once at import time instead
# of assuming the build machine is new enough: on an older interpreter, passing filter="data"
# unconditionally would raise TypeError for every single tar member and silently make
# tar/tar.gz/tar.bz2 sources non-functional (each member logged as "failed to extract").
_TAR_EXTRACT_SUPPORTS_DATA_FILTER = "filter" in inspect.signature(tarfile.TarFile.extract).parameters


def list_archive(path: str, fmt: str) -> ArchiveInfo:
    if fmt in ("zip", "7z"):
        return _list_7z(path)
    if fmt == "rar":
        return _list_rar(path)
    if fmt in TAR_MODES:
        return _list_tar(path, TAR_MODES[fmt])
    return ArchiveInfo(ok=False)


@dataclass
class ExtractOutcome:
    """Результат extract_archive(). __bool__ == ok, чтобы существующее
    `if not extract_archive(...)` продолжало работать без изменений. Поля skipped_meta/
    failures наполняет только tar-ветка (A/C/D, 2026-08-28): zip/7z/rar распаковывает
    внешний бинарник, построчной статистики по своим членам он сюда не отдаёт."""
    ok: bool
    skipped_meta: int = 0          # symlink/hardlink/устройство/fifo -- намеренно НЕ распакованы (A)
    failures: list = field(default_factory=list)   # до _EXTRACT_FAILURE_SAMPLE_CAP строк "<имя>: <русская причина>" (D)
    failure_total: int = 0         # сколько всего реальных сбоёв распаковки (failures может быть обрезан)

    def __bool__(self):
        return self.ok


_EXTRACT_FAILURE_SAMPLE_CAP = 5


def _short_extract_error(e: Exception) -> str:
    """Короткая русская причина для строки о нераспаковавшемся файле (D, 2026-08-28) --
    вместо сырого текста исключения tarfile (по-английски, часто дублирует имя файла)."""
    if isinstance(e, OSError):
        if e.errno == errno.ENAMETOOLONG or getattr(e, "winerror", None) in (206, 3):
            return "слишком длинный путь"
        if e.errno == errno.ENOSPC:
            return "нет места на диске"
        if e.errno in (errno.EACCES, errno.EPERM):
            return "отказано в доступе"
        return "ошибка записи файла"
    name = type(e).__name__
    if "Link" in name:
        return "ссылка за пределы архива"
    if "Absolute" in name:
        return "абсолютный путь внутри архива"
    if "SpecialFile" in name:
        return "специальный файл (устройство/сокет)"
    return "не удалось распаковать"


def extract_archive(path: str, fmt: str, dest_dir: str, log=print) -> ExtractOutcome:
    _makedirs_iterative(winlong(dest_dir))
    try:
        if fmt in ("zip", "7z"):
            out = subprocess.run(
                [SEVENZIP_BIN, "x", f"-o{dest_dir}", "-y", path],
                capture_output=True, timeout=1800,
            )
            return ExtractOutcome(ok=out.returncode == 0)
        if fmt == "rar":
            out = subprocess.run(
                [UNRAR_BIN, "x", "-y", path, dest_dir + os.sep],
                capture_output=True, timeout=1800,
            )
            return ExtractOutcome(ok=out.returncode == 0)
        if fmt in TAR_MODES:
            # Member-by-member (не extractall целиком): tar может быть собран не на Windows
            # и содержать имя с символом, который NTFS не примет (':', '?', ...) -- одно
            # такое имя иначе роняло бы исключение и обрывало распаковку ВСЕГО архива.
            # Здесь -- как и для имени назначения при копировании -- сегменты санитизируются
            # заранее, а один нераспаковавшийся файл просто копится в failures и пропускается.
            if not _TAR_EXTRACT_SUPPORTS_DATA_FILTER:
                log("  ВНИМАНИЕ: интерпретатор Python, которым собран .exe, слишком старый для "
                    "filter=\"data\" (защита от path traversal при распаковке tar) -- "
                    "распаковка продолжится БЕЗ этой защиты. Пересоберите на Python >=3.11.4/3.12.")
            extract_kwargs = {"filter": "data"} if _TAR_EXTRACT_SUPPORTS_DATA_FILTER else {}
            skipped_meta = 0
            failures = []
            failure_total = 0
            with tarfile.open(winlong(path), TAR_MODES[fmt]) as tf:
                for member in tf.getmembers():
                    # A (2026-08-28): символьные/жёсткие ссылки, блочные/символьные устройства
                    # и FIFO -- не файлы с данными, фотоархиву не нужны, а безопасный
                    # распаковщик (filter="data") всё равно отверг бы ссылку на абсолютный путь
                    # исключением на КАЖДУЮ (реальный боевой прогон: 178 таких строк подряд от
                    # backup-архива прошивки роутера). Не пытаемся распаковывать вовсе -- только
                    # считаем, наверх уходит одно число (см. ExtractOutcome / _handle_archive()).
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        skipped_meta += 1
                        continue
                    safe_name = "/".join(
                        sanitize_windows_component(p) for p in member.name.split("/") if p
                    )
                    if not safe_name:
                        continue
                    member.name = safe_name
                    try:
                        tf.extract(member, winlong(dest_dir), **extract_kwargs)
                    except Exception as e:
                        failure_total += 1
                        if len(failures) < _EXTRACT_FAILURE_SAMPLE_CAP:
                            failures.append(f"{member.name}: {_short_extract_error(e)}")
            return ExtractOutcome(ok=True, skipped_meta=skipped_meta,
                                   failures=failures, failure_total=failure_total)
    except Exception as e:
        log(f"  ОШИБКА распаковки {path}: {e}")
        return ExtractOutcome(ok=False)
    return ExtractOutcome(ok=False)


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: str) -> bool:
    """True если path -- Windows reparse point (symlink ИЛИ junction). Определяется через
    st_file_attributes (тот же приём, что is_hidden_path() для DOS_ATTR_HIDDEN_BIT) вместо
    os.path.islink(), который на Windows исторически распознаёт только IO_REPARSE_TAG_SYMLINK,
    но НЕ IO_REPARSE_TAG_MOUNT_POINT (junction) -- os.path.isjunction() существует только с
    Python 3.12, а поддерживаемый минимум интерпретатора для сборки этого exe ниже (см.
    _TAR_EXTRACT_SUPPORTS_DATA_FILTER выше). st_file_attributes ловит оба вида reparse point
    единообразно на любой версии Windows-Python. os.lstat (не stat) -- не следует за самим
    reparse point при проверке. Fallback на os.path.islink() на не-Windows (dev/test на
    Linux, где junction как явления не существует)."""
    try:
        st = os.lstat(winlong(path))
    except OSError:
        return False
    if hasattr(st, "st_file_attributes"):
        return bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    return os.path.islink(path)


def count_extracted_files(root: str) -> int:
    """Post-extraction defense in depth (Phase 2 audit finding 7): `_looks_like_path_traversal()`
    parses the human-readable text output of `7z l -slt`/`unrar lt` with regexes -- not a
    fully trusted layer (no concrete bypass is known, but it's still string-parsing external
    tool output, not a structured API). A zip/7z/rar member whose traversal the pre-extraction
    regex check missed would get written by 7z.exe/UnRAR.exe OUTSIDE extract_dir -- which means
    it simply won't be found by walking extract_dir afterwards, so a LOWER file count than the
    archive's own listing claimed is the detectable symptom of a successful escape. Counts only
    regular files (not directories) under `root`, followlinks=False like
    find_reparse_point_in_tree() (a reparse point itself is caught by that separate check, not
    walked into here)."""
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(winlong(root), followlinks=False):
        total += len(filenames)
    return total


def find_reparse_point_in_tree(root: str):
    """Post-extraction defense in depth (2026-07-10 Phase 2 audit): zip/7z/rar extraction is
    fully delegated to the external 7z.exe/UnRAR.exe subprocess -- unlike tar, which is
    protected at extraction time by tarfile's filter="data" (PEP 706, see extract_archive()),
    which explicitly refuses to extract a symlink member whose target would resolve outside
    the destination directory. If 7z.exe/UnRAR.exe ever materializes a symlink/junction member
    from a zip/7z/rar archive as a real Windows reparse point inside extract_dir, walking that
    tree afterwards (SourceWalker._walk_dir) could follow it to an arbitrary location elsewhere
    on disk -- the walker's cycle-detection (ancestors=...) only catches a loop back onto an
    already-open ancestor of the CURRENT walk, not a one-way escape to an unrelated directory.
    Worst case: a booby-trapped "family photos.zip" silently pulls unrelated files (e.g. the
    victim's Documents folder) into the resulting archive.

    PARTIALLY CONFIRMED on real hardware 2026-07-24 (still open for the specific code path this
    function's own logic executes -- see SESSION-HANDOFF.txt): a zip member crafted as a Unix
    symlink (S_IFLNK external_attr, same shape as ci/windows_ci_test.py's
    test_archive_symlink_rejected) makes bin/7z.exe fail extraction OUTRIGHT on a normal,
    non-elevated Windows account without Developer Mode -- "Cannot create symbolic link: client
    does not have the required privilege" -- so `extract_archive()` already returns a falsy
    ExtractOutcome and the whole archive is rejected via `archive_extract_failed`, this
    function is never even reached. That is itself a real, confirmed safety net for the overwhelming majority of
    PhotoArchive's target users (regular Windows accounts). Whether 7z.exe/UnRAR.exe can still
    materialize a reparse point when the *extracting* process DOES hold
    SeCreateSymbolicLinkPrivilege (Developer Mode enabled, or elevated/admin) remains
    unconfirmed -- attempted on this same machine, blocked short of a full verification by the
    privilege only taking effect in a fresh logon session (a mid-session Developer Mode toggle
    didn't unblock symlink creation without a restart). This check still costs nothing when it
    never triggers, and remains the right defense-in-depth for that narrower, still-open case.
    os.walk(followlinks=False) does not descend into a reparse point
    (so this scan itself can't be tricked into walking outside `root`), but still lists it once
    at its parent level -- enough to detect and reject it. Returns the first reparse point path
    found, or None if the tree is clean."""
    for dirpath, dirnames, filenames in os.walk(winlong(root), followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if _is_reparse_point(full):
                return _strip_winlong(full)
    return None


def free_space_bytes(path: str) -> int:
    """Free space of the volume containing `path`. `path` itself (or its TARGET/
    tmp_extract ancestors) may not exist yet -- e.g. analyze-* modes never create TARGET,
    and tmp_extract is created lazily on first archive extraction -- so walk up to the
    nearest existing ancestor (worst case: the drive root, which always exists) rather than
    calling disk_usage() directly on a path shutil can't stat."""
    p = os.path.abspath(path)
    while p and not os.path.isdir(winlong(p)):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    usage = shutil.disk_usage(winlong(p))
    return usage.free


def cleanup_dir(path: str):
    if os.path.isdir(winlong(path)):
        shutil.rmtree(winlong(path), ignore_errors=True)


def _sweep_tmp_extract_dir(tmp_extract_dir: str, log=print) -> None:
    """Подчищает СОБСТВЕННЫЕ (sha256-именованные, см. _OWN_TMP_EXTRACT_ENTRY_RE/
    _handle_archive()) папки распаковки архивов внутри tmp_extract_dir -- защита от
    накопления мусора после прерванного прогона (Ctrl+C, крах). Не трогает ничего, что не
    похоже на собственную временную папку программы (см. докстринг _OWN_TMP_EXTRACT_ENTRY_RE
    выше -- почему это важно). Один прогон может подмести НЕСКОЛЬКО таких директорий за раз --
    см. _cleanup_own_tmp_extract_entries(), где эта функция реально вызывается."""
    if not (os.path.isdir(winlong(tmp_extract_dir)) and os.listdir(winlong(tmp_extract_dir))):
        return
    entries = [n for n in os.listdir(winlong(tmp_extract_dir)) if n != SKIP_MARKER]
    # Only remove entries that look like our own archive_hash extraction dirs (see
    # _handle_archive()) -- see _OWN_TMP_EXTRACT_ENTRY_RE comment above for why.
    recognized = [n for n in entries if _OWN_TMP_EXTRACT_ENTRY_RE.match(n)]
    unrecognized = [n for n in entries if n not in recognized]
    if recognized:
        log(f"TMP_EXTRACT не пуст ({tmp_extract_dir}) — очищаю {len(recognized)} временных папок распаковки")
        for name in recognized:
            cleanup_dir(os.path.join(tmp_extract_dir, name))
    if unrecognized:
        log(f"ВНИМАНИЕ: в TMP_EXTRACT_DIR ({tmp_extract_dir}) есть {len(unrecognized)} "
            f"файлов/папок, не похожих на собственные временные файлы программы -- "
            f"НЕ трогаю их. Если это чужая папка (например, tmp_extract_dir в photoarchive_config.yaml "
            f"указан по ошибке) -- поправьте настройку. Первые: "
            f"{unrecognized[:5]}")


def _sweep_stale_photosort_tmp_files(target: str, log=print) -> None:
    """2026-08-24, живая просьба пользователя ("добавь чистку при старте") -- сметает
    осиротевшие staging-файлы atomic_copy() (".photosort_tmp_*", см. её докстринг) под TARGET.
    Они остаются на диске, только если процесс был убит настолько резко, что даже except-блок
    вокруг shutil.copy2()/os.replace() не успел отработать -- LOGOFF/SHUTDOWN os._exit(), крах,
    Task Manager, пропажа питания. Обычный Ctrl-C/крестик (KeyboardInterrupt) сюда не относится
    -- atomic_copy()'s собственные except-блоки уже убирают свой tmp_path сами, orphan'ов не
    остаётся. Само место назначения файла НИКОГДА не бывает частичным независимо от причины
    прерывания (copy во временный файл + os.replace(), см. atomic_copy()'s докстринг) -- эта
    функция просто убирает мусорные огрызки, не восстанавливает и не может потерять данные.

    Полный os.walk() по TARGET, не привязка к конкретному PID/маркеру -- staging-файлы рассеяны
    по ЛЮБОЙ папке альбома/даты (dest_dir внутри atomic_copy() -- место назначения КОНКРЕТНОГО
    файла, не единый корень, как у tmp_extract/_sweep_tmp_extract_dir() выше), точечно
    отследить их без полного обхода нечем. Безопасно относительно гонки с другим живым
    прогоном на этот же TARGET -- вызывается ТОЛЬКО из Фазы 0 реальной сборки (run()), которая
    уже держит TargetLock на весь свой прогон, конкурентного владельца этих файлов быть не
    может. Обычные метаданные каталогов, без чтения содержимого файлов -- тот же порядок
    стоимости, что и у ensure_target_layout()/report_environment() рядом, вызывается один раз
    на старте (не dry-run/analyze/паспорт -- suppress_logs=True никогда не создаёт такие файлы
    вовсе, см. _run_impl())."""
    if not os.path.isdir(winlong(target)):
        return
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(winlong(target)):
        for name in filenames:
            if name.startswith(".photosort_tmp_"):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    if removed:
        log(f"Найдено и убрано {removed} недокопированных временных файлов "
            f"(.photosort_tmp_*, остаток аварийно прерванного прогона) в {target}")


def _pid_is_alive(pid: int) -> bool:
    """True, если процесс с данным PID ещё РЕАЛЬНО выполняется -- ТОЛЬКО проверка, ничего не
    завершает. На Windows -- OpenProcess() с PROCESS_QUERY_LIMITED_INFORMATION (0x1000), не
    PROCESS_ALL_ACCESS/TerminateProcess: os.kill(pid, 0) на Windows -- НЕ безобидная проверка
    существования (в отличие от POSIX) -- CPython реализует его через TerminateProcess(handle,
    sig), т.е. os.kill(pid, 0) реально пытается завершить процесс с кодом выхода 0, а не просто
    проверить его -- использование его здесь убило бы ровно тот чужой живой прогон, которого эта
    функция обязана не трогать.

    OpenProcess() САМ ПО СЕБЕ недостаточен -- проверено эмпирически (red на первой версии этой
    функции, живой тест test_real_build_sweeps_stale_global_dry_run_tmp_extract_leftover не
    проходил): PID остаётся зарезервирован (и OpenProcess по нему успешно открывает хендл), пока
    существует ХОТЯ БЫ ОДИН открытый хендл к процессу -- в т.ч. у subprocess.Popen текущего
    (родительского) процесса, даже после того, как сам дочерний процесс уже завершился и
    Popen.wait() вернул код возврата. Нужна дополнительная проверка реального статуса --
    GetExitCodeProcess(): STILL_ACTIVE (259) значит "жив", любое другое значение -- завершился
    (даже если хендл на него ещё существует у кого-то).

    Ещё один эмпирический нюанс (проверено вручную, PID 4 = System, всегда живой, но обычному
    пользователю недоступен даже с PROCESS_QUERY_LIMITED_INFORMATION): OpenProcess() тоже
    возвращает NULL, когда процесс СУЩЕСТВУЕТ, но недоступен (ERROR_ACCESS_DENIED=5), не только
    когда его действительно нет (ERROR_INVALID_PARAMETER=87) -- не разбирая эти два случая,
    "недоступен" был бы неотличим от "уже умер" и стал бы восприниматься как приглашение удалить
    чужую директорию, чей процесс на самом деле жив, просто с правами, которые эта проверка не
    может обойти -- GetLastError() после NULL-хендла отличает их."""
    if os.name == "nt":
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.windll.kernel32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True  # не смогли прочитать статус -- безопасный дефолт: считаем живым
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True  # не можем проверить -- безопасный дефолт: считаем живым, не трогаем
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError и т.п. -- процесс существует, просто не наш
    return True


def _sweep_stale_dry_run_pid_dirs(log=print) -> None:
    """Верхний уровень _DRY_RUN_TMP_EXTRACT_DIR -- PID-подпапки (см. Config.__post_init__), не
    сами sha256-папки распаковки напрямую (та плоская схема и была причиной находки Раунда 107
    ревью: общий путь без per-процесс изоляции, конкурентный прогон мог удалить чужую активную
    распаковку). Подметает остатки процессов, убитых "жёстко" (Task Manager/крах/пропажа
    питания -- Ctrl+C перехватывается штатно и подчищает свою PID-папку сам, см.
    _cleanup_own_tmp_extract_entries()) -- НЕ трогает PID-папки ЖИВЫХ чужих процессов.

    Staleness -- через реальную проверку "жив ли PID" (_pid_is_alive()), не через mtime-порог:
    mtime не обновляется на самой директории, пока внутри неё идёт долгая обработка уже
    распакованного архива (архив с гигантским содержимым, часы работы ПОСЛЕ того, как сама
    распаковка закончилась) -- mtime-порог ложно счёл бы такую директорию устаревшей и удалил
    её у ещё живого процесса, ровно тот сценарий, ради которого делался фикс "разбор архива"
    2026-08-19 (не регрессировать его этим же заходом).

    Умозрительный остаточный риск (Раунд 108 ревью, придирка, не поднят как замечание --
    вероятность низкая, направление ошибки безопасное): если жёстко убитый прогон оставляет
    мёртвую PID-папку, и ДО следующего запуска программы ОС успевает выдать тот же самый PID
    совершенно постороннему процессу -- _pid_is_alive() вернёт True (посторонний процесс
    действительно жив), и эта функция примет чужую папку за "ещё активный прогон", оставив её
    нетронутой, пока тот посторонний процесс не завершится. Не потеря данных и не регрессия
    относительно старого поведения -- тот же класс "утечка в %TEMP% переживает дольше, чем
    хотелось бы", уже принятый как некритичный в Раунде 106 придирке 2 для похожего сценария."""
    root = _DRY_RUN_TMP_EXTRACT_DIR
    if not os.path.isdir(winlong(root)):
        return
    own_pid = str(os.getpid())
    for name in os.listdir(winlong(root)):
        if name in (SKIP_MARKER, own_pid):
            continue
        entry_path = os.path.join(root, name)
        if not _OWN_TMP_EXTRACT_PID_DIR_RE.match(name):
            continue  # не похоже на наш PID-каталог -- не трогаем, тот же принцип, что и _sweep_tmp_extract_dir
        if _pid_is_alive(int(name)):
            continue  # чужой прогон ещё жив -- возможно, активно распаковывает архив
        log(f"Найден остаток прерванного прогона (PID {name} больше не существует) в {entry_path} — очищаю")
        cleanup_dir(entry_path)


_MEI_OWNER_MARKER_FILENAME = ".photoarchive_owner_pid"
_MEI_DIR_RE = re.compile(r"^_MEI[0-9]+$")


def _mark_own_mei_extraction_dir() -> None:
    """2026-08-24, живая просьба пользователя ("что-то осталось -- не должно копиться, каждый
    новый запуск должен подчищать всё, что было до него") -- пишет PID текущего процесса в
    маленький файл-маркер ВНУТРИ sys._MEIPASS (распакованная PyInstaller onefile-бутлоадером
    папка -- exiftool/ffmpeg/7z/питон-рантайм, сотня+ МБ). Обычно её убирает сам бутлоадер ПОСЛЕ
    возврата из Python-кода -- но при os._exit()/крахе/Task Manager/пропаже питания этот шаг
    просто не наступает, папка остаётся в %TEMP% навсегда без этой пары функций.

    Маркер нужен, чтобы _sweep_stale_mei_extraction_dirs() ниже (следующий запуск) могла
    отличить "это НАША осиротевшая папка, её процесс мёртв" от чужой -- имя `_MEI<цифры>`
    генерирует ЛЮБОЕ PyInstaller onefile-приложение, не только эта программа, слепое удаление
    неотмеченных папок рискует стереть временные файлы совершенно постороннего работающего
    приложения. No-op вне frozen-сборки (sys._MEIPASS просто не существует) и best-effort --
    тот же паттерн, что и у остальных win32-хелперов этого файла."""
    mei_dir = getattr(sys, "_MEIPASS", None)
    if not mei_dir:
        return
    try:
        with open(os.path.join(mei_dir, _MEI_OWNER_MARKER_FILENAME), "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _sweep_stale_mei_extraction_dirs(log=print) -> None:
    """Парная функция к _mark_own_mei_extraction_dir() выше -- вызывается на КАЖДОМ запуске
    (любой режим, не только реальная сборка, см. _main()), сразу после разбора argv. Смотрит на
    СОСЕДНИЕ с текущей `_MEIxxxxxx` папки в том же родителе (обычно %TEMP%) -- те, что несут
    наш маркер (_mark_own_mei_extraction_dir()) и чей записанный там PID уже мёртв
    (_pid_is_alive()), убираются целиком. Папки БЕЗ маркера (чужое приложение, или наша же
    осиротевшая папка от прогона ДО появления этой пары функций) НЕ трогаются -- тот же принцип
    "в сомнении -- не трогаем", что и у _sweep_tmp_extract_dir()/_sweep_stale_dry_run_pid_dirs()
    выше (тот же остаточный риск переиспользования PID тоже принят, см. _pid_is_alive()'s
    докстринг за симметричным случаем).

    No-op вне frozen-сборки и best-effort -- тот же паттерн, что и у остальных win32-хелперов
    этого файла."""
    mei_dir = getattr(sys, "_MEIPASS", None)
    if not mei_dir:
        return
    parent = os.path.dirname(mei_dir)
    own_name = os.path.basename(mei_dir)
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    for name in entries:
        if name == own_name or not _MEI_DIR_RE.match(name):
            continue
        candidate = os.path.join(parent, name)
        try:
            with open(os.path.join(candidate, _MEI_OWNER_MARKER_FILENAME),
                      "r", encoding="ascii") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue  # без нашего маркера -- не наша папка (или прогон до этого фикса), не трогаем
        if _pid_is_alive(pid):
            continue  # другой наш процесс ещё жив (несколько одновременных запусков) -- не трогаем
        log(f"Найден остаток прерванного запуска (PID {pid} больше не существует) в "
            f"{candidate} — очищаю")
        shutil.rmtree(candidate, ignore_errors=True)


def _cleanup_own_tmp_extract_entries(cfg: "Config", log=print) -> None:
    """Живая находка пользователя, 2026-08-09: временные распакованные папки архива
    (__служебные_файлы\\tmp_extract\\<hash>\\...) оставались на диске после Ctrl+C ВО ВРЕМЯ
    самого прогона (--dry-run) -- раньше только эта же по сути проверка запускалась в начале
    СЛЕДУЮЩЕГО прогона (см. Фазу 0 _run_impl()), ничего не подчищало сразу после текущего
    прерывания, и run_analyze() (`[1]`/CLI analyze, `[4]` Паспорт архива) не запускал такую
    проверку вообще, ни в начале, ни после прерывания. Теперь одна и та же функция вызывается
    в обоих местах (до основного цикла -- остатки чужого прошлого прерывания; сразу после
    `except KeyboardInterrupt` текущего цикла -- остатки этого же прогона, не дожидаясь
    следующего запуска программы) -- см. _run_impl()/run_analyze().

    2026-08-19, живая находка ревизора (Раунд 106, придирка 2, по итогам фикса
    _DRY_RUN_TMP_EXTRACT_DIR -- см. её докстрин): dry-run/analyze/паспорт (suppress_logs=True)
    теперь распаковывают в ЕДИНЫЙ глобальный путь под %TEMP%, не привязанный к конкретному
    TARGET -- если такой прогон убьют "жёстко" (не через Ctrl+C, где перехват уже отрабатывает
    надёжно -- Task Manager/крах/пропажа питания), раньше следующий ЛЮБОЙ прогон НА ТОМ ЖЕ
    TARGET подчищал остатки (tmp_extract был его собственной подпапкой), теперь же путь общий и
    от TARGET не зависит -- реальная сборка (свой, другой, TARGET-путь tmp_extract) эти остатки
    больше не увидит вовсе, нужен именно следующий suppress_logs=True прогон где угодно. Чтобы
    не сужать гарантию подчистки, а расширить её (любой следующий прогон программы, а не только
    на том же TARGET, как было раньше) -- подметаем ОБА места: собственный cfg.tmp_extract
    ТЕКУЩЕГО прогона (своя PID-подпапка, если это suppress_logs=True) и общий
    _DRY_RUN_TMP_EXTRACT_DIR (чужие PID-подпапки, но только те, чей процесс уже мёртв -- см.
    _sweep_stale_dry_run_pid_dirs(), 2026-08-19 Раунд 107: раньше эта вторая подметка была
    безусловной, без разбора чья именно папка -- ровно та гонка, что нашёл ревизор)."""
    _sweep_tmp_extract_dir(cfg.tmp_extract, log=log)
    _sweep_stale_dry_run_pid_dirs(log=log)

# ============================================================================
# WALKER  (from pipeline/walker.py)
# ============================================================================


@dataclass
class SourceItem:
    read_path: str
    origin_display: str
    rel_path: str          # posix-style path used for album/dump-segment detection
    size: int
    mtime: float
    ftype: str              # image / raw / video / other
    sibling_path: str = None  # same-basename sibling (RAW<->image pair) in the same directory, if any
    zone: str = "normal"    # "normal" | "noisy" -- см. classify_zone()
    archive_no_crc: bool = False  # True only for items extracted from tar/tar.gz/tar.bz2 --
                                   # those formats carry no per-file content checksum at all
                                   # (unlike zip/7z/rar, whose extractors verify CRC and fail
                                   # the whole extraction on mismatch), so place_file() must
                                   # not take its CRC-trusted same-volume rename shortcut for
                                   # these -- see place_file() docstring.
    archive_boundary_idx: int = None  # 2026-07-11: index (in rel_path.split("/")) of the
        # OUTERMOST archive's own filename segment, for items that came from inside any
        # archive; None for items found directly on disk. See find_album() -- an archive's
        # internal folder names are never trusted as an album name on their own (a generic
        # word like "archive" inside a zip must not silently become Albums\archive\); if no
        # real album exists on the disk-side portion of the path, the archive's OWN filename
        # becomes the album instead, and everything inside it becomes that album's subpath.
    dvd_dest_path: str = None  # 2026-08-07: non-None ONLY for a file inside a VIDEO_TS DVD
        # unit (see SourceWalker._handle_dvd_unit()) -- the destination is already fully
        # decided at walk time (whole unit is one atomic "new"/"duplicate" decision, no
        # per-file find_album()/resolve_date()/dedup-pool routing, no partial merge -- direct
        # user requirement, 2026-08-07 conversation). _process_dvd_item() checks this field
        # FIRST in the main loop, before analyze_batch()/_process_record() ever see the item.
    dvd_sha256: str = None  # precomputed by _handle_dvd_unit() while building the unit's
        # fingerprint -- place_file()'s hash-verify reuses it instead of hashing the file a
        # second time.
    dvd_unit_fingerprint: str = None  # 2026-08-07 (Раунд 71 ревью, фикс блокера): тот же
        # fingerprint, что и dvd_units_copied[]["fingerprint"] -- позволяет основному циклу
        # (_run_impl()) считать, сколько файлов ЭТОГО КОНКРЕТНОГО юнита реально успешно
        # скопировались, и регистрировать юнит в БД/отчёте, только если это число сошлось с
        # n_files юнита целиком. См. _handle_dvd_unit()/_process_dvd_item().
    source_tree_path: str = None  # 2026-08-14: ВСЕГДА SOURCE-относительный, архивные сегменты
        # -- ПОЛНОЕ имя файла с расширением (в отличие от rel_path -- расширение срезано для
        # find_album(), и origin_display -- при depth==0 абсолютен, при архиве внутри подпапки
        # теряет/задваивает путь до архива) -- см. SourceWalker._walk_dir()'s tree_rel_prefix
        # докстрин. Только для НЕ-DVD-item -- см. report.py:_render_source_tree_card()/
        # AnalyzeStats.source_tree_counts.
    dvd_source_tree_key: str = None  # то же самое, но для DVD-юнита (item.dvd_dest_path не
        # None) -- путь к самой папке VIDEO_TS целиком (не к отдельному файлу внутри неё, весь
        # юнит сворачивается в один узел дерева, см. photosort_win.py:_source_tree_parent_key()).


def _matches_any(name: str, patterns) -> bool:
    lname = name.lower()
    return any(fnmatch.fnmatch(lname, pat) for pat in patterns)


def _strip_trailing_arrow(s: str) -> str:
    """Removes exactly one trailing " → " separator (used to join nested archive names) --
    NOT str.rstrip(" → "), which strips any trailing run of space/→ characters and can eat
    into a nested archive's own name if it happens to end in one of those characters (ruff
    B005: misleading multi-character strip, found 2026-07-17)."""
    return s[:-len(" → ")] if s.endswith(" → ") else s


def _quick_media_count_estimate(source: str, cfg: Config, on_progress=None) -> int:
    """SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2 -- быстрый предпересчёт SOURCE для
    планового времени ("план" в [прошло/план]): считает то, что реально тикнет _walk_dir()
    (image/raw/video + настоящий многофайловый архив как 1 объект, см. _would_walk_tick())
    -- по имени/расширению, без os.stat/хеширования/открытия файла --
    под ТЕМИ ЖЕ правилами исключения папок, что и SourceWalker._walk_dir()
    (HARD_EXCLUDE_DIRS/default_exclude_dirs/extra_exclude_dirs/системные папки) -- ссылается на
    те же общие списки/cfg-поля, не копирует их отдельным списком. Не полноценный дубль
    _walk_dir() (нет бухгалтерии found_archive_roots/archive_logs и т.п. -- не нужна для одной
    лишь оценки количества).

    2026-08-17 (боевой прогон, источник с очень большой долей немедийных файлов): раньше
    считались ВСЕ файлы без разбора типа ("сознательная переоценка, дешевле точного счёта") --
    источник, где немедийные файлы (мгновенное, дешёвое решение при обходе) численно доминируют
    над реальными медиа (дорогая exif/hash-обработка), доводил "обработано объектов %"
    (SourceWalker._tick_object(), та же гранулярность) до 100% почти сразу после того, как такая
    папка дощупана обходом, хотя реальная обработка медиафайлов в остальном дереве только
    начиналась. Классификация по расширению (file_type()) не требует stat()/чтения файла --
    та же дешёвая цена, что и раньше, просто с фильтром; исключённые (EXCLUDE_FILES_PATTERNS)/
    sidecar (SIDECAR_PATTERNS) файлы не нужно фильтровать отдельно -- ни один из этих паттернов
    не пересекается с image/raw/video/архивными расширениями, file_type() уже даёт им "other".

    on_progress(delta), если передан -- вызывается после каждой отсканированной директории с
    числом media-кандидатов, найденных ИМЕННО в ней (не кумулятивным итогом) -- для собственного
    живого индикатора предпересчёта ("Оцениваю объём работы: найдено N файлов…", см.
    run_for_source()), чтобы сам предпересчёт на медленном/сетевом диске не выглядел зависшим."""
    if os.path.isfile(winlong(source)):
        return 1  # одиночный файл/архив как SOURCE -- оценка не важна, не усложняем частным случаем
    count = 0
    root_under_system_dir = is_under_system_dir(source)
    stack = []

    def _scan(dirpath, is_root=False):
        nonlocal count
        try:
            with os.scandir(winlong(dirpath)) as it:
                scanned = list(it)
        except OSError:
            return
        # SESSION-HANDOFF.txt, 2026-08-09 (боевой прогон, вторая находка): реальный обход
        # (_walk_dir()) тикает VIDEO_TS-юнит и SKIP_MARKER-папку ОДНОЙ гранулярностью, отличной
        # от по-файлового счёта ниже -- имена нужны ДО решения, считать ли содержимое папки
        # по файлам или как единое целое/пропустить вовсе, поэтому dir/file разделяются здесь
        # одним проходом, а не как раньше (delta считался по ходу, без предварительного
        # разбора имён файлов).
        subdir_names = []
        file_names = []
        for entry in scanned:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                subdir_names.append(entry.name)
            else:
                file_names.append(entry.name)
        # _walk_dir(): SKIP_MARKER пропускает папку целиком (не считает её файлы, не спускается
        # в подпапки) -- КРОМЕ самого корня SOURCE (ПРАВИЛО ЯВНОГО УКАЗАНИЯ, тот же принцип, что
        # и у пропуска exclude-списков для корня ниже).
        if not is_root and SKIP_MARKER in file_names:
            return
        # _walk_dir(): VIDEO_TS -- ОДНА неделимая единица (см. секцию "DVD-VIDEO UNITS" выше),
        # реальный обход тикает её как единый объект, не заглядывая внутрь -- знаменатель должен
        # считать так же, иначе X (реальные тики) никогда не догонит Y (эта оценка) на источнике
        # с DVD-рипами.
        if _is_video_ts_dir(dirpath, file_names):
            count += 1
            if on_progress is not None:
                on_progress(1)
            return
        delta = sum(1 for name in file_names if _would_walk_tick(name))
        count += delta
        if delta and on_progress is not None:
            on_progress(delta)
        for name in subdir_names:
            # os.path.join(dirpath, name) -- NOT entry.path: scandir() was called on
            # winlong(dirpath) (\\?\-prefixed for long-path safety), so entry.path would
            # silently inherit that prefix, while cfg.target/realpath() comparisons below never
            # carry it -- os.path.realpath() does NOT strip \\?\ back off, so the two forms of
            # the same path would compare unequal and the target-skip check below would never
            # fire (live bug, caught by test_quick_media_count_estimate_never_descends_into_target
            # before it shipped). Same convention _walk_dir() already uses elsewhere: store
            # plain paths, wrap with winlong() only at the actual syscall boundary.
            stack.append(os.path.join(dirpath, name))

    _scan(source, is_root=True)  # корень -- ПРАВИЛО ЯВНОГО УКАЗАНИЯ (RULES.md), без проверки исключений
    while stack:
        dirpath = stack.pop()
        base_lower = os.path.basename(dirpath).lower()
        if base_lower in HARD_EXCLUDE_DIRS:
            continue
        if base_lower in cfg.default_exclude_dirs_lower:
            continue
        if base_lower in cfg.extra_exclude_dirs_lower:
            continue
        if not cfg.scan_system_dirs and not root_under_system_dir and is_under_system_dir(dirpath):
            continue
        try:
            if os.path.realpath(dirpath) == os.path.realpath(cfg.target):
                continue
        except OSError:
            pass
        _scan(dirpath)
    return count


# ============================================================================
# DVD-VIDEO UNITS (VIDEO_TS) -- 2026-08-07, реальный боевой прогон пользователя, домашнее видео
# на DVD не попало в архив (.vob/.ifo/.bup не распознавались как медиа вообще). По итогам
# разговора с пользователем: папка VIDEO_TS (и всё, что физически внутри неё лежит) -- ОДНА
# неделимая единица, копируется в Albums/<имя>/VIDEO_TS/ целиком или не копируется вовсе,
# "объединение DVD-папок недопустимо" (ничего не дописывается в уже скопированный юнит на
# повторных прогонах) -- см. SourceWalker._handle_dvd_unit()/_process_dvd_item() ниже.
# Отдельностоящий .vob (не внутри VIDEO_TS) в эту категорию не попадает -- см. VIDEO_EXTS выше,
# идёт обычным по-файловым путём видео.
# ============================================================================


def _is_video_ts_dir(dirpath: str, files: list) -> bool:
    """Тот же сигнал, что раньше использовался только для report.html-упоминания "не
    скопировано" (живой репорт пользователя, 2026-08-01): имя папки ИМЕННО "video_ts"
    (регистронезависимо, стандартное имя DVD-Video) с хотя бы одним .vob/.ifo/.bup внутри --
    ложных срабатываний почти не бывает, так называют папку только реальные DVD-рипы."""
    return (os.path.basename(dirpath).lower() == "video_ts"
            and any(ext_of(n) in ("vob", "ifo", "bup") for n in files))


def _source_tree_parent_key(item) -> str:
    """Ключ родительского узла ("папка"/"архив") для AnalyzeStats.source_tree_counts (реальное
    дерево SOURCE, см. её докстринг).

    НЕ item.origin_display (первая, отвергнутая попытка, 2026-08-14): при depth==0/вне архива
    строится из АБСОЛЮТНОГО cur_dirpath (см. её же докстрин про этот квирк, уже обойдённый в
    _analyze_source_abs_path()) -- абсолютный путь ломает дерево (разные прогоны на разных
    SOURCE перестают иметь общий корень "/"-сегментов), к тому же для файла ВНУТРИ архива,
    лежащего в подпапке, origin_display теряет эту подпапку ДО стрелки (origin_prefix помнит
    только имя архива, "Архив.zip → ", не путь до него) и задваивает её ПОСЛЕ (тот же
    cur_rel_prefix, что уже строит rel_path, повторно приклеен за стрелкой) -- неверный порядок
    сегментов, архив оказывался бы веткой ВЫШЕ своей настоящей папки.

    НЕ item.rel_path (вторая, тоже отвергнутая попытка -- работала, но архив в дереве
    показывался БЕЗ расширения, rel_path строится из имени архива без расширения для
    find_album(), сознательно не менялось в угоду этой задаче). item.source_tree_path --
    ТРЕТЬЕ, отдельное поле (SourceWalker._walk_dir()'s tree_rel_prefix, её докстрин) именно
    под эту задачу: ВСЕГДА SOURCE-относителен, архив -- полное имя файла с расширением, без
    потери/задвоения пути. Родитель -- путь без последнего сегмента (имени самого файла).

    DVD-юнит (item.dvd_dest_path не None) -- ОДНА неделимая единица (секция "DVD-VIDEO UNITS"
    выше), сворачивается в один узел-лист "VIDEO_TS" -- item.dvd_source_tree_key уже указывает
    ИМЕННО на эту папку целиком (та же tree_rel_prefix-механика, отдельное поле -- см. его
    докстрин), не на отдельный .vob/.ifo/.bup файл внутри."""
    if item.dvd_dest_path is not None:
        return item.dvd_source_tree_key
    return "/".join(item.source_tree_path.split("/")[:-1])


def _dvd_unit_file_records(video_ts_dirpath: str, progress_cb=None) -> list:
    """Рекурсивный список ВСЕХ файлов внутри VIDEO_TS -- (relpath, size, mtime, full_path,
    sha256). relpath с прямыми слэшами, регистр ИМЕНИ СОХРАНЁН как на диске (нужен для
    реального имени файла в месте назначения -- "копировать как есть" включает регистр,
    "VTS_01_0.VOB" не должен стать "vts_01_0.vob") -- за нормализацию регистра для стабильного
    fingerprint отвечает _dvd_unit_fingerprint() отдельно, не эта функция. mtime -- нужен
    _handle_dvd_unit()'s ByDate-ветке (VIDEO_TS без альбома, см. её докстринг) -- считается
    здесь же, а не вторым проходом по тем же файлам. full_path -- ОБЫЧНЫЙ (не
    \\\\?\\-префиксный) путь, та же конвенция, что и везде в _walk_dir() (plain paths in,
    winlong() только на границе самого syscall) -- SourceItem.read_path ожидает именно такой.
    Обычная DVD-структура плоская (без подпапок), но "копировать как есть" (прямое требование
    пользователя) не полагается на это -- рекурсивно, на случай нестандартного рипа."""
    records = []
    for dirpath, _dirnames, filenames in os.walk(winlong(video_ts_dirpath)):
        plain_dirpath = dirpath[4:] if dirpath.startswith("\\\\?\\") else dirpath
        for name in filenames:
            # B (REVIEW-HANDOFF.md Раунд 148, замечание 2): фингерпринт юнита хеширует ВСЕ VOB
            # подряд (гигабайты); опрос паузы снаружи (_walk_dir()) сюда не дотягивается.
            if progress_cb is not None:
                progress_cb()
            plain_full = os.path.join(plain_dirpath, name)
            rel = os.path.relpath(plain_full, video_ts_dirpath).replace("\\", "/")
            try:
                st = os.stat(winlong(plain_full))
            except OSError:
                continue
            records.append((rel, st.st_size, st.st_mtime, plain_full,
                            sha256_file(plain_full, progress_cb=progress_cb)))
    records.sort(key=lambda r: r[0].lower())
    return records


def _dvd_unit_fingerprint(records: list) -> str:
    """Комбинированный отпечаток целого DVD-юнита -- см. докстринг таблицы dvd_units в SCHEMA.
    Не хеш байт содержимого (то же самое дал бы конкатенацию гигабайт VOB-ов без пользы) --
    хеш СПИСКА (имя, размер, sha256) уже посчитанных отдельных файлов, тех же, что реально
    копируются (mtime намеренно НЕ входит -- тот же диск, скопированный в другой момент/другим
    инструментом, может дать другой mtime при идентичном содержимом, дедуп не должен на это
    полагаться). Имя -- в нижнем регистре ТОЛЬКО здесь (не в самих records, см. их докстринг)
    -- одна и та же болванка, скопированная дважды разными инструментами с разным регистром
    имён, должна давать одинаковый fingerprint."""
    joined = "\n".join(f"{rel.lower()}|{size}|{sha}" for rel, size, _mtime, _full, sha in records)
    return sha256_bytes(joined.encode("utf-8"))


def _dvd_volume_label(drive_root: str):
    """Метка тома съёмного/оптического диска -- None на не-Windows, при сбое API, или для
    пустой метки. drive_root -- "D:\\\\" (со слэшем на конце, как ожидает
    GetVolumeInformationW). Вызывающая сторона (_dvd_unit_volume_label_if_live_disc()) уже
    проверила GetDriveTypeW ДО этого вызова -- сюда не передаётся то, что не
    DRIVE_REMOVABLE/DRIVE_CDROM."""
    if os.name != "nt":
        return None
    buf = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_root), buf, 261, None, None, None, None, 0)
    except Exception:
        return None
    if not ok:
        return None
    label = buf.value.strip()
    return label or None


def _dvd_unit_volume_label_if_live_disc(video_ts_dirpath: str, check_volume_label: bool):
    """Высший приоритет именования DVD-юнита -- ТОЛЬКО если VIDEO_TS реально лежит на
    смонтированном съёмном/оптическом приводе прямо сейчас (живой диск в дисководе,
    DRIVE_REMOVABLE/DRIVE_CDROM), см. check_volume_label -- вызывающая сторона (_walk_dir())
    передаёт True ТОЛЬКО для depth==0 (настоящее дерево SOURCE, не содержимое архива -- том
    cfg.tmp_extract при depth>=1 -- это диск, на котором установлена программа, не имеет
    отношения ни к какому DVD). None -- нет живого диска с меткой, вызывающая сторона
    (_handle_dvd_unit()) переходит к следующему приоритету (find_album() на путь снаружи
    VIDEO_TS). Если VIDEO_TS -- это давно скопированная на обычный жёсткий диск папка (частый
    реальный случай), метка тома означала бы метку ВСЕГО диска-бэкапа -- одну и ту же для
    десятков разных VIDEO_TS-папок где-то в дереве, что как раз против цели "не объединять
    разные DVD"."""
    if not (check_volume_label and os.name == "nt"):
        return None
    try:
        drive = os.path.splitdrive(os.path.abspath(video_ts_dirpath))[0]
        root = f"{drive}\\" if drive else None
        if not root:
            return None
        DRIVE_REMOVABLE, DRIVE_CDROM = 2, 5
        dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if dtype in (DRIVE_REMOVABLE, DRIVE_CDROM):
            return _dvd_volume_label(root)
    except Exception:
        pass
    return None


def _unique_dvd_dest_name(parent_dir: str, base_name: str, reserved: set) -> str:
    """Windows-конвенция " (2)", " (3)", ... (см. DUMP_SEGMENT_REGEXES выше, тот же приём,
    что и у Windows для "Новая папка (2)") -- при коллизии имени DVD-юнита (например, две
    разных болванки с одинаковой генерик-меткой тома вроде "DVD_RW", или два разных диска без
    альбома, случайно попавшие в одну и ту же ByDate-корзину) НЕ объединяет содержимое, заводит
    отдельную папку -- прямое требование пользователя, 2026-08-07 ("объединение DVD-папок
    недопустимо"). `reserved` -- имена, уже выбранные ДРУГИМИ новыми DVD-юнитами В ЭТОМ ЖЕ
    прогоне, В ЭТОМ ЖЕ parent_dir (в dry_run на диске ничего ещё не создано, os.path.isdir() в
    одиночку не увидел бы такую коллизию до конца прогона). Используется и для Albums/<имя>
    (parent_dir=cfg.albums_root), и для ByDate/.../VIDEO_TS (parent_dir=дата-корзина,
    base_name="VIDEO_TS" буквально) -- см. _handle_dvd_unit()."""
    candidate = base_name
    n = 2
    while (parent_dir, candidate) in reserved or os.path.isdir(winlong(os.path.join(parent_dir, candidate))):
        candidate = f"{base_name} ({n})"
        n += 1
    return candidate


class SourceWalker:
    def __init__(self, cfg: Config, log=print, progress_cb=None,
                 object_line_cb=None, transient_op_cb=None, object_progress_cb=None,
                 dvd_unit_registry: dict = None, show_placement_letter: bool = False,
                 defer_media_object_tick: bool = False, heavy_notice_cb=None):
        self.cfg = cfg
        self.log = log
        # См. _log_own_line()/ProgressReporter.write_heavy_notice() -- прямая ссылка на бар для
        # редких/важных уведомлений, в обход ненадёжного self.log()/_ACTIVE_BARS (2026-08-24).
        # None для любого вызывающего кода, который не передаёт (работает как раньше -- голый
        # "\n"+self.log() фолбэк в _log_own_line()).
        self._heavy_notice_cb = heavy_notice_cb
        # Живая находка пользователя, 2026-08-09: "A"/"D" (альбом/по дате) после тега
        # "[папка]"/"[archive]" в консоли -- ТОЛЬКО --dry-run/[3] реальная сборка (по прямой
        # просьбе пользователя), НЕ analyze/[4] Паспорт архива -- та же SourceWalker/
        # ProgressReporter обслуживает оба случая, различие только в этом флаге, который
        # передаёт вызывающий код (_run_impl() -- True, run_analyze() -- по умолчанию False).
        self._show_placement_letter = show_placement_letter
        # Раунд 50 ревью (REVIEW-HANDOFF.md, БЛОКЕР 1): раньше выставлялось только в walk()'s
        # ветке для SOURCE-папки -- ветка для SOURCE-одиночного-файла-архива (RULES.md:347,
        # штатный режим "Фаза 2а") делает return раньше той строки, атрибут никогда не
        # создавался. _walk_dir() обращается к нему безусловно, как только обход доходит до
        # ЛЮБОЙ неархивной-корневой директории -- т.е. как только у извлечённого архива есть
        # хотя бы одна подпапка (AttributeError под дефолтным конфигом, scan_system_dirs=False).
        # Здесь, безусловно для обоих случаев -- is_under_system_dir() одинаково корректна и
        # для файла, и для папки (только проверяет сегменты пути, не спрашивает файловую
        # систему, есть ли по пути реально папка).
        self._root_under_system_dir = is_under_system_dir(cfg.source)
        # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2 -- ДВА новых, независимых от
        # progress_cb колбэка (тот остаётся как есть для Фазы 1/analyze*, см. _progress_cb
        # ниже): object_line_cb(tag, path, n_found) -- см. ProgressReporter.write_object_line(),
        # печатается РОВНО один раз при входе в папку/архив; transient_op_cb(text|None) -- см.
        # ProgressReporter.set_transient_op(), для распаковки архива (единственное место в
        # SourceWalker, где нужна временная операция БЕЗ ожидания следующего update() -- см.
        # _handle_archive()). Оба None для любого вызывающего кода, который их не передаёт
        # (Фаза 1/analyze*) -- работает как раньше, никаких новых строк не печатается.
        self._object_line_cb = object_line_cb
        self._transient_op_cb = transient_op_cb
        # Живая находка пользователя, 2026-08-19 (боевой прогон, архив с гигантским
        # количеством вложенных файлов/вложенных архивов внутри): "распаковка (X ГБ)"
        # (transient_op_cb, см. _handle_archive() ниже) гасилась в None СРАЗУ после самой
        # физической распаковки -- дальнейший обход распакованного содержимого (у такого
        # архива -- самая долгая часть всего прогона, час-два) не показывал НИЧЕГО в поле
        # операции статус-строки, откатывался на статичный resting-текст ("Пробный прогон"/
        # "Сборка архива"), хотя "обработано объектов %" тем временем честно застревает
        # (архив тикает ОДНИМ объектом, только по завершении ВСЕГО своего содержимого, см.
        # _tick_object()) -- застрявший % без единой живой подсказки читался как зависание.
        # Счётчик (не bool) -- вложенные архивы сами рекурсивно вызывают _handle_archive() из
        # этого же обхода: без счётчика собственное "разбор архива"/None вложенного вызова
        # затирало бы пометку внешнего архива, хотя обработка внешнего ещё не закончена.
        self._archive_walk_depth = 0
        # Живой репорт пользователя (2026-08-01): "объектов X/Y" в статус-строке -- ТРЕТИЙ,
        # отдельный от object_line_cb/self.count (медиафайлы) счётчик, той же ГРАНУЛЯРНОСТИ,
        # что и _quick_media_count_estimate() (настоящий многофайловый архив -- 1 штука, не
        # заглядывая внутрь; image/raw/video/DVD-юнит -- 1 штука; см. _would_walk_tick()).
        # 2026-08-17: EXCLUDE/SIDECAR/тип "other" НЕ считаются вовсе (ни в X, ни в Y) -- источник,
        # где такие файлы численно доминируют, раньше доводил X до Y почти сразу. Раунд 159: бэйр
        # `.gz`/`.bz2` (detect_archive_format отвергает) тоже НЕ считаются -- та же асимметрия
        # в обратную сторону (Y > X, бар залипал ниже 100%). object_progress_cb(1) -- вызывается РОВНО там же, где
        # _quick_media_count_estimate() засчитывает файл, и ТОЛЬКО на depth==0 (настоящее
        # дерево SOURCE, не содержимое распакованного архива, которое
        # _quick_media_count_estimate() никогда не открывает -- см. её докстринг). Вложенный
        # архив (найден УЖЕ внутри другого архива, depth>=1) поэтому естественно не получает
        # своего тика -- он и так уже "внутри" тика внешнего архива.
        self._object_progress_cb = object_progress_cb
        # REVIEW-HANDOFF.md, Раунд 86, замечание 2: для run_analyze()/Паспорта (батч-чтение
        # EXIF через _walk_with_exif_prefetch(), до 200 файлов на спавн exiftool.exe) обычный
        # тик "сразу после yield" тикает В МОМЕНТ, когда обёртка забрала имя файла в свой
        # внутренний pending -- задолго до того, как exiftool реально прочитал его метаданные
        # батчем -- числитель обгоняет знаменатель, хотя батч-спавны exiftool на самом деле
        # только начинаются -- тот же класс проблемы, что уже чинили для self.count (см.
        # ProgressReporter.__init__(), "self.count простаивал на 0 почти весь прогон"), но с
        # противоположным симптомом. defer_media_object_tick=True (передаёт только
        # run_analyze()) отключает ТОЛЬКО этот один тик (см. yield item ниже в files-цикле) --
        # вместо него тикает сам run_analyze(), поштучно, после analyze_batch() для каждого
        # media-кандидата (:7057/:7070 и далее) -- НЕ одним вызовом на весь батч на его отправку
        # в exiftool_batch(), как было раньше (2026-08-10 -- 2026-08-17): такая батч-гранулярность
        # засчитывала видео из батча "готовыми" ДО того, как video_duration_and_resolution()/
        # ffprobe -- самая медленная часть analyze_batch() для видео -- реально их обрабатывала,
        # держала "объектов %" на 100% до десятков минут на боевом прогоне с крупными видео
        # (живая находка 2026-08-18, REVIEW-HANDOFF.md Раунд 100, фикс 62f3c91). Остальные тики
        # (stat_failed -- уже честный, работа для media-кандидата закончена немедленно; архив/
        # DVD-юнит целиком -- отдельная гранулярность, не затронута) остаются как есть.
        # EXCLUDE/SIDECAR/тип "other" сюда не относятся вовсе -- с 2026-08-17 они не тикают ни
        # здесь, ни где-либо ещё (см. комментарий у object_progress_cb выше).
        self._defer_media_object_tick = defer_media_object_tick
        # 2026-07-11, user feedback: an archive being extracted already shows a "текущее
        # действие" note (see _handle_archive()) so a slow archive never reads as a hang --
        # plain folders showed nothing at all, no way to tell where the program is currently
        # digging on a slow network drive/huge tree. Optional callback (typically
        # ProgressReporter.set_context(), see its docstring) -- default None keeps every
        # caller that doesn't pass one (or constructs SourceWalker without a live progress
        # bar at all) working exactly as before.
        self._progress_cb = progress_cb
        self.archive_logs = []   # list of (archive_display, status, note)
        # Вспомогательные строки для archives.log (пропущенные служебные записи / частичные
        # сбои распаковки, 2026-08-28) -- отдельно от archive_logs НАМЕРЕННО: тег не с
        # префиксом "archive_", их НЕ должны считать счётчики n_archives_found/archives_seen
        # (одна запись на архив там, здесь -- дополнительная к ней). list of (display, tag, text).
        self.archive_notes = []
        self.sidecar_logs = []   # list of (display_path,)
        self.skipped_marker_logs = []  # list of (display_path,)
        # 2026-08-07: DVD-Video (VIDEO_TS) теперь копируется целиком как один юнит (см.
        # _handle_dvd_unit() ниже) -- заменяет старый dvd_folders (который только упоминал
        # "не скопировано" в отчёте, см. git history для прежнего поведения).
        # dvd_unit_registry -- {fingerprint: dest_path}, уже известные юниты с прошлых
        # прогонов (архив_cache.db, см. вызывающую сторону) -- {} по умолчанию для вызывающего
        # кода, который его не передаёт (старые тесты и т.п.), тогда всё выглядит новым.
        # 2026-08-08 (живой боевой прогон F:->D:, дубль DVD-юнита внутри ОДНОГО прогона не
        # ловился): `dvd_unit_registry or {}` -- баг identity, не только "None -> {}" -- на
        # самом частом реальном случае (свежий архив, БД ещё не содержит ни одного dvd_units-
        # ряда) вызывающая сторона передаёт уже пустой `{}` (falsy), и `x or {}` в этом случае
        # молча создаёт НОВЫЙ отдельный dict, теряя identity с объектом вызывающей стороны --
        # правки этого объекта основным циклом run() (см. ниже, регистрация юнита сразу по
        # подтверждению) физически не были бы видны здесь. Явная проверка на None сохраняет
        # identity даже для пустого словаря.
        self._dvd_unit_registry = dvd_unit_registry if dvd_unit_registry is not None else {}
        self.dvd_units_copied = []   # list of dict(name, dest_path, n_files, total_bytes, fingerprint)
        self.dvd_units_skipped_duplicate = []  # list of dict(name, dest_path) -- уже был в архиве
        self._dvd_names_reserved = set()  # имена новых юнитов, уже выбранные в ЭТОМ прогоне
                                           # (см. _unique_dvd_dest_name())
        # SESSION-HANDOFF.txt, Сценарий 3 (SOURCE отключается физически посреди обхода):
        # os.stat() ниже в _walk_dir() уже был обёрнут в try/except OSError, но раньше просто
        # молча `continue`-ил -- реальный боевой тест такого отключения поймал файлы,
        # исчезающие без единого следа ни в одном логе. Теперь каждый такой файл собирается
        # сюда и переносится в unreadable.csv тем же способом, что archive_logs/sidecar_logs
        # ниже по файлу (run_for_source()) -- пользователь должен видеть, что файл вообще был.
        self.stat_failed_logs = []  # list of (display_path, error_str)
        # REVIEW-HANDOFF.md, Раунд 32, задача 4: os.listdir() на директорию тоже может
        # провалиться (права доступа/длинный путь/повреждённая ФС) -- вся папка теряется молча
        # без этого счётчика, отчёт не мог дать пользователю сигнал "не всё было прочитано".
        self.listdir_failed = []  # list of dirpath
        # 2026-07-11 (session on managing the exclude-dir list): pропуски по имени папки
        # (hard/default/extra) считаются, а не печатаются построчно -- на полном скане диска
        # node_modules/.git может встретиться сотни раз, построчный print был бы спамом.
        # Ключ -- (reason, name), см. excluded_dir_summary(). Гейт системных папок (см.
        # is_under_system_dir ниже) срабатывает максимум по разу на каждый реально
        # присутствующий SYSTEM_DIR_ENV_VARS-корень (рекурсия сразу останавливается), поэтому
        # для него достаточно списка путей, не агрегации.
        self._excluded_dir_hits = {}
        self.system_dir_skips = []  # list of (dirpath,)
        self._target_real = os.path.realpath(cfg.target)
        # Живая находка (боевой прогон, 2026-08-09) -- см. is_under_system_dir()-гейт ниже в
        # _walk_dir(): содержимое, РАСПАКОВАННОЕ ИЗ АРХИВА, физически лежит под cfg.tmp_extract
        # (может оказаться под системной директорией, если TARGET там -- например
        # _NO_TARGET_PLACEHOLDER под %TEMP%) -- такой путь нужно узнавать безусловно, не только
        # по origin_prefix (пуст для САМОГО ВЕРХНЕГО архива, если он же и есть SOURCE -- см.
        # walk()'s ветку "SOURCE is a single archive file", origin_prefix="" передаётся туда
        # явно), иначе первая попытка фикса (через origin_prefix) не покрывала этот случай.
        self._tmp_extract_prefix = os.path.normcase(cfg.tmp_extract).rstrip(os.sep) + os.sep
        # ROADMAP.md, analyze как "2 части": сырые пути (realpath) папок, чей родитель
        # оказался найденным архивом (__служебные_файлы встречена где-то в дереве SOURCE) --
        # см. классификацию/исключение вложенности в classify_found_archives().
        self.found_archive_roots = []
        # Двухфазный обход (2026-08-03, по запросу пользователя -- "нормальный альбом рушится,
        # если ByDate уже заполнен раньше по алфавиту"): self._phase управляет, включена ли
        # отсрочка (см. _walk_dir()/_is_terminal_bydate_branch()) -- 1 во время самого первого,
        # "боевого" прохода walk() (единственная фаза, где что-либо реально откладывается), 2/3
        # во время последующего "дренажа" отложенных списков ниже -- отсрочка там больше не
        # нужна (всё, что могло быть отложено ЕЩЁ раз, уже гарантированно ByDate, см.
        # _is_terminal_bydate_branch()'s докстринг), проверки просто не выполняются повторно.
        self._phase = 1
        # (archive_path, rel_prefix, origin_prefix, depth, archive_boundary_idx,
        # tree_rel_prefix) -- архивы, чьё СОБСТВЕННОЕ имя (тильда/dump или без букв) безусловно
        # решает их судьбу как ByDate независимо от места на диске (см. find_album()) --
        # отложены на Фазу 2, чтобы весь "чистый" альбомный контент (Фаза 1) гарантированно
        # успел занять место в пуле дедупа первым. tree_rel_prefix (2026-08-14) добавлен
        # последним элементом -- см. _walk_dir()'s докстрин про него же.
        self._deferred_tilde_archives = []
        # (dirpath, rel_prefix, origin_prefix, ancestors, archive_boundary_idx, archive_no_crc,
        # depth, tree_rel_prefix) -- точки входа в поддеревья, для которых
        # _is_terminal_bydate_branch() вернул True (папка отравлена тильдой/dump-именем НИЖЕ
        # уже найденного альбома, либо путь исчерпал поиск альбома вовсе) -- отложены на Фазу
        # 3, разворачиваются обычным _walk_dir() (фаза уже не 1, повторных проверок/
        # откладываний внутри не происходит). tree_rel_prefix (2026-08-14) -- см. _walk_dir()'s
        # докстрин про него же; здесь ПЕРЕСЧИТАН через _tree_rel(rel) в момент откладывания
        # (живая находка тестами -- "как есть" терял путь до самой отложенной папки: у обычной
        # папки, в отличие от архива, нет своего заранее посчитанного "new_tree_rel_prefix").
        self._deferred_bydate_roots = []
        # Файлы-листья без альбома (find_album() уже дал окончательный None прямо на месте,
        # см. _walk_dir()) -- в отличие от папок/архивов их незачем "разворачивать" отдельным
        # проходом в Фазе 3, они уже полностью готовы к yield, просто копятся здесь до конца
        # Фазы 1.
        self._deferred_stray_files = []
        # {cur_dirpath: (disp_for_object, folder_media_count)} -- "[папка] ... найдено
        # медиафайлов N" для папки, чьи файлы откладываются на Фазу 3 (см. _walk_dir()) --
        # НЕ печатается сразу (была бы напечатана задолго до реальной обработки, живая жалоба
        # пользователя 2026-08-07: "он считает, что то, что прошло через экран -- уже
        # обработано"), печатается позже, при первом же файле ИЗ ЭТОЙ папки в
        # _drain_deferred_phases() -- см. её докстринг. Ключ -- cur_dirpath (реальный
        # физический путь, тот же, что войдёт в item.read_path через os.path.join(cur_dirpath,
        # name)) -- НЕ cur_rel_prefix/disp_for_object: те совпадают для разных физических папок
        # с одинаковым именем внутри РАЗНЫХ архивов (origin_prefix отличается, но не участвует
        # в rel_path самого item), cur_dirpath отличается всегда.
        self._pending_folder_announcements = {}
        # REVIEW-HANDOFF.md, Раунд 58 [БЛОКЕР]: временные распакованные папки архивов, из
        # которых Фаза 1 что-то ОТЛОЖИЛА (не спустилась целиком), нельзя чистить сразу же по
        # завершении их собственного _walk_dir() -- отложенная запись ссылается на путь ВНУТРИ
        # такой папки, а читается только позже, в Фазе 2/3, когда каталог уже удалён (живая,
        # воспроизводимая, ПОЛНАЯ потеря содержимого архива с dump-веткой внутри). См.
        # _handle_archive()/_drain_deferred_phases().
        self._pending_cleanup_dirs = []
        # REVIEW-HANDOFF.md, Раунд 155 [замечание]: архив, чьё медиа целиком ушло в отложенный
        # Проход 2/3 (голая дата/дамп-ветка внутри, _is_terminal_bydate_branch()), логировался
        # как archive_no_media и не попадал в archives_extracted, хотя фото реально
        # распаковано и лежит в ByDate -- нарушение RULES.md:449. _handle_archive() кладёт
        # сюда отложенный статус (со срезами по трём _deferred_*-спискам), а
        # _drain_deferred_phases() дописывает его настоящим, когда содержимое реально прочитано.
        self._pending_archive_status = []
        # REVIEW-HANDOFF.md, Раунд 58 [ЗАМЕЧАНИЕ]: любое из трёх откладываний Фазы 1 (dump-
        # ветка/тильда-архив/файл-лист без альбома) означает, что _walk_dir() потратило
        # реальное, не мгновенное время (os.listdir()/сниффинг типа каждого файла, см.
        # sibling_by_base) БЕЗ единого yield'а -- раньше (Фаза 1 обходила всё вперемешку) эта
        # работа была размазана по всему прогону вместе с обычными тиками, теперь же
        # концентрируется в "невидимые" для ProgressReporter паузы. См. живой репорт "план
        # 267ч/323ч" в ProgressReporter.__init__() -- тот же класс искажения, для которого там
        # уже есть готовый механизм (set_transient_op()/_pending_heavy_time), просто не был
        # подключён к ЭТОЙ, новой в этом же коммите паузе. _open_deferred_gap()/
        # _close_deferred_gap() ниже -- тонкая обёртка поверх self._transient_op_cb:
        # открывается БЕЗУСЛОВНО при входе в КАЖДУЮ папку Фазы 1 (см. _walk_dir(), до
        # os.listdir() -- заранее не известно, обычная это папка-альбом или дамп-ветка, узнаётся
        # только по факту), идемпотентно (если уже открыт -- no-op); закрывается перед ЛЮБЫМ
        # следующим реальным yield'ом, откуда бы он ни пришёл (обычный файл Фазы 1, отложенный
        # поддерево Фазы 3, файл-лист без альбома) -- для обычной папки-альбома открытие и
        # закрытие происходят почти сразу же друг за другом (первый же файл альбома обычно и
        # есть первый yield), для dump-ветки сегмент остаётся открытым куда дольше, ровно то
        # время, что раньше искажало EMA.
        self._deferred_gap_open = False

    def _log_own_line(self, msg: str, wrap: bool = True) -> None:
        """Живая находка пользователя, 2026-08-24: редкие/важные уведомления SourceWalker'а
        ("Распаковка ...", "[DVD] новый DVD-диск", "[skip_marker]", ошибки чтения директории и
        т.п.) печатались через self.log() -- ту же обёртку console_log()/log_line(), что и
        обычные строки, но log_line()'s защита от порчи активного tqdm-бара (bar.clear()/
        print()/refresh() по модульному _ACTIVE_BARS) на практике оказалась ненадёжной -- живой
        репорт поймал явную склейку текста с последней строкой бара БЕЗ переноса ("...0.00с/
        файл:Расп" + "аковка..." на следующей физической строке, затем то же самое с "[DVD]").
        Диагностика подтвердила: в момент печати _ACTIVE_BARS пуст (сравнение id() показало ДВЕ
        разные копии этого модуля в процессе -- корень пока не найден, самого self-import'а в
        файле нет).

        Первая версия фикса (тот же день, первый заход) -- голый "\\n" перед текстом БЕЗ
        clear()/refresh() -- устранила склейку, но открыла другой баг, тоже живой репорт
        пользователя: бар не переиспользует свою строку, каждое уведомление оставляет позади
        "замороженный" кадр -- статус-строка визуально дублировалась и "уезжала вверх".
        Правильный фикс -- heavy_notice_cb (см. __init__(), ProgressReporter.write_heavy_notice()):
        прямая ссылка на self._bar того же бара, не зависит от того, сколько копий модуля
        загружено -- та же гарантия, что уже даёт write_object_line(). "\\n"+self.log() --
        фолбэк ТОЛЬКО для вызывающего кода, не передавшего callback (например, analyze-режимы
        без two_line-бара) -- там нет активного бара, портить нечего, но и координировать не с
        чем, а голая пустая строка перед сообщением всё равно безопаснее случайной склейки."""
        if self._heavy_notice_cb is not None:
            self._heavy_notice_cb(msg, wrap=wrap)
        else:
            self.log("\n" + msg)

    def _open_deferred_gap(self) -> None:
        """См. self._deferred_gap_open в __init__(). Идемпотентно -- второе и последующие
        откладывания подряд (без промежуточного реального yield'а) не переоткрывают сегмент,
        так же как set_transient_op() сам по себе не переоткрывает уже открытый отрезок."""
        if not self._deferred_gap_open and self._transient_op_cb is not None:
            self._transient_op_cb(_DEFERRED_CONTENT_TRANSIENT_OP)
            self._deferred_gap_open = True

    def _close_deferred_gap(self) -> None:
        """См. self._deferred_gap_open в __init__(). Вызывать перед ЛЮБЫМ реальным yield'ом --
        no-op, если сегмент не был открыт (обычный, самый частый случай -- Фаза 1 без единого
        откладывания).

        2026-08-19, живая находка пользователя: "возврат к обычному" -- не всегда None. Если
        обход сейчас идёт ВНУТРИ архива (self._archive_walk_depth > 0, см. _handle_archive()),
        безусловный None стирал бы пометку "разбор архива" на каждом первом реальном файле
        любой папки внутри архива (эта функция и открытие/закрытие "отложенного" сегмента --
        общая для обычного дерева И для содержимого архива машинерия _walk_dir(), не разная).
        Без этого фолбэка пользователь на источнике с большим архивом видел бы поле операции
        мигающим между "проверяю отложенное содержимое…" и статичным resting-текстом
        ("Пробный прогон"/"Сборка архива"), ни разу не увидев, что физически идёт разбор
        архива -- ровно та же путаница, которую сама пометка должна была устранить."""
        if self._deferred_gap_open:
            self._transient_op_cb(_ARCHIVE_CONTENT_TRANSIENT_OP if self._archive_walk_depth > 0 else None)
            self._deferred_gap_open = False

    def _record_excluded_dir(self, name: str, reason: str):
        key = (reason, name)
        self._excluded_dir_hits[key] = self._excluded_dir_hits.get(key, 0) + 1

    def excluded_dir_summary(self):
        """list of (name, reason, count), one row per distinct (reason, name) pair."""
        return [(name, reason, count) for (reason, name), count in self._excluded_dir_hits.items()]

    def _log_archive(self, display, status, note="", count=None, silent: bool = False, letter: str = ""):
        self.archive_logs.append((display, status, note))
        # archives.log (RunLogs.archive_event(), см. вызывающий код в _run_impl()) по-прежнему
        # получает исходные display/status/note без изменений независимо от silent -- он
        # читает archive_logs выше, не то, что печатается ниже.
        if silent:
            return
        # Живой репорт пользователя (2026-08-02, "зачем 2 раза и почему в одном перенос?"):
        # archive_no_media раньше ВСЕГДА печаталась второй строкой здесь, дублируя
        # write_object_line() (СРАЗУ после листинга архива, тем же текстом "найдено
        # медиафайлов N") -- настоящий повтор, не просто разное форматирование одного факта
        # (тот более ранний фикс 2026-08-01 выровнял только текст обеих строк, не убрал сам
        # повтор). Для ветки "не media_candidate по листингу" (_handle_archive(), ДО попытки
        # распаковки) это ВСЕГДА 100% то же самое число, что уже показал write_object_line()
        # (has_media_candidate вычисляется из того же info.media_count) -- вызывающий код
        # передаёт silent=True именно для этого случая. Для ветки "0 после РЕАЛЬНОЙ
        # распаковки" (media_count пересчитан по факту, может отличаться от предварительного
        # info.media_count из листинга) печать остаётся -- там это не повтор, а новое,
        # уточнённое число.
        if status == "archive_no_media":
            tail = ": найдено медиафайлов 0"
        elif status == "archive_extracted" and count is not None:
            # "распаковано," спереди -- это ВТОРОЕ, подтверждённое после реальной распаковки
            # число (может отличаться от предварительного из листинга архива в object-line,
            # см. media_count в _handle_archive()), не буквальный повтор той же строки.
            tail = f": распаковано, найдено медиафайлов {count}"
        else:
            tail = f": {status} {note}".rstrip()
        # SESSION-HANDOFF.txt (2026-08-05, боевой прогон п.2): раньше display печатался как
        # есть, без обрезки под ширину терминала -- длинные пути некрасиво переносились самим
        # терминалом посреди слова ("...: найдено" / "медиафайлов 0"), тогда как
        # write_object_line() уже решает ту же задачу приёмом "…"+хвост пути (см.
        # _console_tag_line_budget()). Хвост здесь известен ТОЧНО (не оценка, весь текст уже
        # собран выше) -- бюджет считается по его реальной длине, не запасу под неизвестное N.
        #
        # letter (2026-08-28, живой боевой прогон): "A"/"D" (альбом/по дате) сразу после "]"
        # тега -- тот же формат и та же проба (_handle_archive() зовёт _placement_letter() один
        # раз и передаёт сюда), что и у объект-строки write_object_line() того же архива.
        # Раньше эти статус-строки ("распаковано, найдено N", "archive_no_media",
        # bomb/no-space/traversal и т.п.) буквы не несли вовсе, хотя соседняя объект-строка
        # того же архива её показывала. "" (analyze/[4] Паспорт) -- буквы нет, старый формат.
        # tag_width 11 с буквой / 10 без -- как в ProgressReporter._object_line_budget().
        tag_width = 11 if letter else 10
        display = _truncate_progress_note(
            display, maxlen=_console_tag_line_budget(len(tail), tag_width=tag_width))
        line = f"  [archive]{letter} {display}{tail}" if letter else f"  [archive] {display}{tail}"
        # Живой боевой прогон, 2026-08-28: строка уходит в ProgressReporter.write_heavy_notice().
        # Путь здесь уже обрезан под ПОЛНУЮ ширину терминала (_console_tag_line_budget()), ровно
        # как у объект-строки write_object_line() (та не переносится вовсе). Перенос нужен лишь
        # редкой ветке со свободным длинным note (path_traversal и т.п.), реально не влезающей в
        # окно целиком -- гейтим переносом только её; сам перенос идёт по краю окна, не по 2/3
        # (write_heavy_notice(), 2026-08-29).
        self._log_own_line(line, wrap=len(line) > _console_columns())

    def walk(self):
        source = self.cfg.source
        if os.path.isfile(winlong(source)):
            # "объектов X/Y": _quick_media_count_estimate() возвращает 1 для этого же случая
            # (SOURCE -- одиночный файл) без разбора, архив это или обычное медиа -- тик здесь
            # безусловно, до detect_archive_format()/file_type() ниже, той же логикой.
            if self._object_progress_cb is not None:
                self._object_progress_cb(1)
            # SOURCE is a single archive file (or a folder-of-parts handled by caller)
            fmt = detect_archive_format(source)
            if fmt:
                yield from self._handle_archive(source, rel_prefix="", origin_prefix="", depth=1,
                                                 archive_boundary_idx=0)
                # 2026-08-03: even a single-archive SOURCE can contain internal subfolders that
                # defer (see _walk_dir()) -- must drain the same way as the folder-SOURCE branch
                # below, not return early (a bare early return here silently dropped every
                # deferred item, a real bug caught by
                # test_source_archive_file_without_subfolder_still_works turning up empty).
                yield from self._drain_deferred_phases()
                return
            # a single plain media file given directly as SOURCE
            t = file_type(source)
            if t in ("image", "raw", "video"):
                st = os.stat(winlong(source))
                yield SourceItem(source, source, os.path.basename(source), st.st_size, st.st_mtime, t,
                                  zone=classify_zone(source), source_tree_path=os.path.basename(source))
            return

        # ПРАВИЛО ЯВНОГО УКАЗАНИЯ (RULES.md): если сам SOURCE уже лежит внутри системной
        # папки (например, SOURCE=C:\Users\X\AppData\Local\SomeApp), пользователь явно
        # выбрал это дерево целиком -- гейт системных папок ниже не должен повторно
        # срабатывать на КАЖДОМ вложенном уровне (иначе всё глубже первого подкаталога
        # молча терялось бы без единой строки в лог, т.к. is_under_system_dir() истинен
        # для любого потомка системного корня). self._root_under_system_dir фиксирует это один
        # раз для всего обхода -- см. __init__() (безусловно там, не только в этой ветке).
        root_real = os.path.normcase(os.path.realpath(source))
        self._phase = 1
        yield from self._walk_dir(source, rel_prefix="", origin_prefix="", depth=0, is_root=True,
                                   ancestors=(root_real,))
        yield from self._drain_deferred_phases()

    def _drain_deferred_phases(self):
        """Фаза 2/3 двухфазного обхода (2026-08-03, см. __init__()) -- вызывается ОБОИМИ
        ветками walk() (SOURCE -- одиночный файл-архив и SOURCE -- папка), не только второй:
        даже одиночный файл-архив может содержать внутри себя вложенные под-папки/архивы,
        которые Фаза 1 отложила (см. _walk_dir())."""
        # Фаза 2: архивы с тильда/dump-собственным именем, отложенные Фазой 1 -- их судьба
        # (безусловный ByDate) не зависит от места на диске, поэтому им не нужен никакой
        # повторный обход, достаточно списка точек входа, накопленного во время единственного
        # прохода Фазы 1. _handle_archive() -- тот же метод, что и в Фазе 1, self._phase уже не
        # 1, так что новых откладываний внутри не происходит (см. _walk_dir() ниже) -- всё
        # найденное внутри такого архива обрабатывается безусловно.
        # Раунд 155: media_count каждой отложенной записи -- чтобы _pending_archive_status
        # (архивы, чьё содержимое ушло сюда целиком) досчитать по своим срезам ниже.
        _tilde_media = [0] * len(self._deferred_tilde_archives)
        _bydate_media = [0] * len(self._deferred_bydate_roots)
        _stray_media = [0] * len(self._deferred_stray_files)

        self._phase = 2
        for _i, (archive_path, rel_prefix, origin_prefix, depth, boundary,
                 tree_rel_prefix) in enumerate(self._deferred_tilde_archives):
            for item in self._handle_archive(archive_path, rel_prefix, origin_prefix, depth,
                                              archive_boundary_idx=boundary,
                                              tree_rel_prefix=tree_rel_prefix):
                if item.ftype in ("image", "raw", "video"):
                    _tilde_media[_i] += 1
                yield item
            # "объектов X/Y" (см. _walk_dir()'s _tick_object()): архив тикает здесь, а не в
            # момент, когда Фаза 1 впервые увидела его имя -- отложенный архив реально
            # обрабатывается только сейчас. depth здесь -- уже depth+1 (то, что уйдёт ВНУТРЬ
            # _handle_archive() для его содержимого, см. точку захвата в _walk_dir()), поэтому
            # depth==1 соответствует исходному depth==0 (архив найден НЕ внутри другого архива)
            # -- тот же смысл, что и depth==0 в _walk_dir(), просто со сдвигом на +1.
            if depth == 1 and self._object_progress_cb is not None:
                self._object_progress_cb(1)

        # Фаза 3: поддеревья, отравленные тильда/dump-папкой ниже уже найденного альбома (или
        # исчерпавшие поиск альбома вовсе) -- отложены Фазой 1 как точки входа, разворачиваются
        # обычным _walk_dir() (self._phase уже не 1 -- ни дальнейших откладываний, ни повторных
        # is_dump_segment()-проверок, всё внутри безусловно ByDate, включая любые архивы любых
        # имён, найденные там -- см. find_album()'s "отравление ветки").
        self._phase = 3
        for _i, (dirpath, rel_prefix, origin_prefix, ancestors, boundary, archive_no_crc,
                 depth, tree_rel_prefix) in enumerate(self._deferred_bydate_roots):
            for item in self._walk_dir(dirpath, rel_prefix, origin_prefix, depth, is_root=False,
                                        ancestors=ancestors, archive_no_crc=archive_no_crc,
                                        archive_boundary_idx=boundary,
                                        tree_rel_prefix=tree_rel_prefix):
                if item.ftype in ("image", "raw", "video"):
                    _bydate_media[_i] += 1
                yield item

        # Файлы-листья без альбома (см. __init__()) -- уже полностью готовые SourceItem, ничего
        # разворачивать не нужно, просто yield'им их следом -- _close_deferred_gap() перед
        # первым же из них (см. её докстринг) закрывает сегмент, открытый ещё в Фазе 1 в
        # момент первого же добавления в этот список.
        for _i, item in enumerate(self._deferred_stray_files):
            # "[папка] ... найдено медиафайлов N" (см. __init__()'s _pending_folder_
            # announcements) -- печатается здесь, на первом же файле этой папки, а не когда
            # Фаза 1 её увидела -- pop(), не get(), чтобы напечатать РОВНО один раз на папку,
            # не на каждый её файл.
            folder_key = os.path.dirname(item.read_path)
            pending = self._pending_folder_announcements.pop(folder_key, None)
            if pending is not None and self._object_line_cb is not None:
                self._object_line_cb("folder", *pending)
            self._close_deferred_gap()
            if item.ftype in ("image", "raw", "video"):
                _stray_media[_i] += 1
            yield item
            # "объектов X/Y" -- тикает здесь (реальная обработка), не в момент, когда Фаза 1
            # впервые увидела файл. archive_boundary_idx is None -- файл найден НЕ внутри
            # архива (тот же смысл, что и depth==0 в _walk_dir()/_tick_object()) -- файлы
            # внутри архива, оставшиеся без альбома, не тикают отдельно, архив уже тикнул
            # как единое целое (см. выше). См. defer_media_object_tick в __init__() -- тот же
            # принцип, что и в основном files-цикле _walk_dir(): вместо этого тикает сам
            # run_analyze(), поштучно, после analyze_batch() для этого же файла (2026-08-18).
            if (item.archive_boundary_idx is None and self._object_progress_cb is not None
                    and not self._defer_media_object_tick):
                self._object_progress_cb(1)

        # Раунд 155 [замечание]: архивы, чьё медиа целиком ушло в отложенный проход выше --
        # теперь оно реально прочитано, досчитываем media_count по срезам и дописываем
        # настоящий статус (archive_no_media только если рекурсивно НИЧЕГО не нашлось,
        # RULES.md:449; иначе archive_extracted -> попадает в archives_extracted).
        for st in self._pending_archive_status:
            total = (st["media_now"]
                     + sum(_bydate_media[st["bydate"][0]:st["bydate"][1]])
                     + sum(_tilde_media[st["tilde"][0]:st["tilde"][1]])
                     + sum(_stray_media[st["stray"][0]:st["stray"][1]]))
            if total == 0:
                self._log_archive(st["display"], "archive_no_media", letter=st["letter"])
            else:
                self._log_archive(st["display"], "archive_extracted",
                                   f"{total} медиафайлов", count=total,
                                   silent=total == st["listing_media"], letter=st["letter"])
        self._pending_archive_status = []

        # REVIEW-HANDOFF.md, Раунд 58 [БЛОКЕР]: временные распакованные папки архивов,
        # отложенная очистка которых копилась в _handle_archive() (см. self._pending_
        # cleanup_dirs в __init__()) -- Фазы 2/3 выше уже прочитали из них всё, что им было
        # нужно, теперь безопасно почистить.
        for extract_dir in self._pending_cleanup_dirs:
            cleanup_dir(extract_dir)

    def _handle_dvd_unit(self, video_ts_dirpath, disp_base, archive_no_crc,
                          check_volume_label, album, subpath, album_prefix, human_disk_name,
                          source_tree_key=None):
        """2026-08-07 -- см. секцию "DVD-VIDEO UNITS" выше. Генератор: yield'ит один
        SourceItem на файл ТОЛЬКО для нового (не дубль) DVD-юнита -- для уже известного
        (fingerprint совпал с dvd_unit_registry) не yield'ит ничего вообще, юнит либо
        архивирован целиком, либо нет ("объединение недопустимо", требование пользователя).
        Реальное копирование -- НЕ здесь: _walk_dir()/этот метод остаются обходом источника
        (генератор без доступа к run_logs/stats/cache_conn); SourceItem.dvd_dest_path/
        dvd_sha256 несут уже принятое решение дальше по конвейеру, до _process_dvd_item() в
        основном цикле (run()/run_analyze()), который проверяет эти поля ПЕРВЫМ, раньше
        analyze_batch()/_process_record() -- ни find_album(), ни resolve_date(), ни Pool
        дедуп этих файлов никогда не видят.

        Куда кладём -- три приоритета (решено с пользователем 2026-08-07, включая уточнение
        того же дня "что если VIDEO_TS лежит внутри ~Папки?"):
        1. Метка тома живого съёмного/оптического диска -- check_volume_label (только
           depth==0, см. _walk_dir()) + _dvd_unit_volume_label_if_live_disc().
        2. album/subpath -- РЕЗУЛЬТАТ find_album() на путь СНАРУЖИ VIDEO_TS (считает
           вызывающая сторона, _walk_dir(), тем же способом, что и для обычного файла на этом
           месте) -- Albums/<album>/<subpath>/VIDEO_TS/... Тот же find_album(), что и для
           любого обычного файла -- НЕ отдельная упрощённая эвристика ("взять имя
           родительской папки"), поэтому корректно отравляется тем же служебным сегментом,
           который отравил бы обычный файл на этом месте (напр. "Альбом/DCIM/VIDEO_TS" целиком
           падает в ByDate, а не даёт лже-альбом "DCIM" или сохраняет "Альбом").
        3. album is None (find_album() не нашёл ничего -- ЛИБО путь отравлен тильда/dump-
           папкой, ЛИБО ни одного осмысленного сегмента вовсе) -- VIDEO_TS уходит в ByDate,
           ЦЕЛИКОМ, тем же принципом "один файл", что и обычный файл без альбома: у обычного
           файла внутри "~Папка/photo.jpg" альбома тоже нет, он идёт в ByDate. Дата -- САМЫЙ
           РАННИЙ mtime среди файлов юнита (единая для всего юнита -- не переоткрывать решение
           на каждый файл отдельно, иначе разные файлы одного диска рискуют разъехаться по
           разным датным корзинам, что и есть "рассыпаться", которого просил избежать
           пользователь), низкий tier точности (как у любого видео без EXIF-даты съёмки, тот
           же класс, что .mod/.tod)."""
        records = _dvd_unit_file_records(
            video_ts_dirpath,
            progress_cb=(lambda: _check_pause_keypress(log=self.log)) if os.name == "nt" else None)
        if not records:
            return
        fingerprint = _dvd_unit_fingerprint(records)
        display_name = human_disk_name or "VIDEO_TS"
        known_dest = self._dvd_unit_registry.get(fingerprint)
        if known_dest is not None:
            self.dvd_units_skipped_duplicate.append({
                "name": display_name,
                "dest_path": known_dest,
            })
            # Тот же приём против переноса, что у "[DVD] новый DVD-диск ->" ниже и у
            # _log_archive(): путь под бюджет полной ширины окна, wrap только если всё равно
            # не влез (2026-08-29, живой репорт -- эта строка единственная из [DVD]/[archive]/
            # [папка], оставшаяся без обрезки). Полный путь всё равно в actions.log.
            dup_tag = "[DVD] дубль уже архивированного диска, пропущен: "
            dup_path = _truncate_progress_note(
                disp_base, maxlen=_console_tag_line_budget(0, tag_width=len(dup_tag)))
            dup_line = f"  {dup_tag}{dup_path}"
            self._log_own_line(dup_line, wrap=len(dup_line) > _console_columns())
            return

        volume_label = _dvd_unit_volume_label_if_live_disc(video_ts_dirpath, check_volume_label)
        if volume_label:
            unit_name = _unique_dvd_dest_name(self.cfg.albums_root, volume_label,
                                               self._dvd_names_reserved)
            self._dvd_names_reserved.add((self.cfg.albums_root, unit_name))
            dest_dir = build_album_dest_dir(self.cfg.albums_root, unit_name, ["VIDEO_TS"])
        elif album is not None:
            album_dir = build_album_dest_dir(self.cfg.albums_root, album_prefix, subpath)
            unit_name = _unique_dvd_dest_name(album_dir, "VIDEO_TS", self._dvd_names_reserved)
            self._dvd_names_reserved.add((album_dir, unit_name))
            dest_dir = os.path.join(album_dir, unit_name)
        else:
            earliest_mtime = min(mtime for _rel, _size, mtime, _full, _sha in records)
            date_value = datetime.fromtimestamp(earliest_mtime)
            date_dir = build_bydate_dest_dir(self.cfg.bydate_root, date_value, precision="day",
                                              place=None, granularity=self.cfg.bydate_granularity)
            unit_name = _unique_dvd_dest_name(date_dir, "VIDEO_TS", self._dvd_names_reserved)
            self._dvd_names_reserved.add((date_dir, unit_name))
            dest_dir = os.path.join(date_dir, unit_name)

        total_bytes = sum(size for _rel, size, _mtime, _full, _sha in records)
        self.dvd_units_copied.append({
            "name": display_name, "dest_path": dest_dir, "n_files": len(records),
            "total_bytes": total_bytes, "fingerprint": fingerprint,
        })
        # Живая находка пользователя, 2026-08-09 (дополнение к A/D-буквам после [папка]/
        # [archive]): "[DVD]" -- та же буква, тем же принципом (сразу после "]", один
        # пробел перед текстом), но решение здесь уже известно (volume_label/album посчитаны
        # вызывающей стороной _walk_dir() ВЫШЕ, см. три приоритета в докстринге) -- пробный
        # find_album() не нужен, в отличие от _placement_letter() для папки/архива.
        # Тег "[dvd_unit]" переименован в "[DVD]" по прямой просьбе пользователя (короче,
        # 2026-08-09) -- тот же принцип, что и у остальных тегов, никакой особой логики
        # выравнивания под старое имя не было (letter_part не завязан на длину тега).
        letter = ""
        if self._show_placement_letter:
            letter = "A" if (volume_label or album is not None) else "D"
        letter_part = f"{letter} " if letter else " "
        # Живой боевой прогон 2026-08-28: та же проблема, что чинил _log_archive() (86f2b2f) --
        # строка уходит в write_heavy_notice(), а тут ДВА длинных пути (dest_dir + disp_base) и
        # ничего не обрезано под ширину, поэтому реальный DVD-путь рвался посреди слова. Тот же
        # приём: disp_base (исходный путь -- контекст, полный вариант всё равно уходит в
        # actions.log per-file через run_logs.appended()) под фикс-кап, dest_dir -- под остаток
        # ширины, wrap только если всё равно не влезло в окно целиком.
        dvd_tag = f"[DVD]{letter_part}новый DVD-диск -> "
        disp_short = _truncate_progress_note(disp_base, maxlen=48) if disp_base else ""
        dvd_tail = (f" ({len(records)} файлов, {disp_short})" if disp_short
                    else f" ({len(records)} файлов)")
        dest_shown = _truncate_progress_note(
            dest_dir, maxlen=_console_tag_line_budget(len(dvd_tail), tag_width=len(dvd_tag)))
        dvd_line = f"  {dvd_tag}{dest_shown}{dvd_tail}"
        self._log_own_line(dvd_line, wrap=len(dvd_line) > _console_columns())
        for rel, size, mtime, full_path, sha in records:
            dest_path = os.path.join(dest_dir, *rel.split("/"))
            yield SourceItem(
                read_path=full_path,
                origin_display=f"{disp_base}/{rel}" if disp_base else full_path,
                rel_path=f"{display_name}/VIDEO_TS/{rel}",
                size=size, mtime=mtime, ftype="video",
                zone=classify_zone(full_path), archive_no_crc=archive_no_crc,
                dvd_dest_path=dest_path, dvd_sha256=sha,
                dvd_unit_fingerprint=fingerprint,
                dvd_source_tree_key=source_tree_key,
            )

    def _placement_letter(self, rel_prefix: str, archive_boundary_idx) -> str:
        """"A"/"D" (альбом/по дате) -- см. __init__()'s show_placement_letter. Пробный
        find_album() с фиктивным именем файла "x" в конце (тот же приём, что уже использует
        DVD-юнит выше/объявление "[папка] ... найдено медиафайлов N" ниже) -- определяет,
        найдётся ли альбом ДЛЯ ЭТОГО пути-контейнера (папки или архива), не читая реальные
        файлы внутри. "" если show_placement_letter выключен (по умолчанию, analyze/[4]
        Паспорт архива -- буква там не показывается вовсе, см. __init__())."""
        if not self._show_placement_letter:
            return ""
        probe_rel = f"{rel_prefix}/x" if rel_prefix else "x"
        probe_album, _s, _p = find_album(
            probe_rel, archive_boundary_idx,
            dump_names=self.cfg.dump_segment_names_lower,
            dump_prefixes=self.cfg.dump_segment_prefixes_tuple,
            bydate_only=self.cfg.source_bydate_only)
        return "A" if probe_album is not None else "D"

    def _walk_dir(self, dirpath, rel_prefix, origin_prefix, depth, is_root=False, ancestors=(),
                  archive_no_crc=False, archive_boundary_idx=None, tree_rel_prefix=""):
        # 2026-08-14, прямая просьба пользователя ("архивы с расширением в дереве источника"):
        # tree_rel_prefix -- ТРЕТИЙ параллельный префикс, отдельный от rel_prefix (album/dump-
        # детекция, архив без расширения -- report.py:_source_tree_parent_key() раньше пробовал
        # переиспользовать его и origin_prefix, оба оказались непригодны для этой задачи) и
        # origin_prefix (человекочитаемое "архив.zip → путь" -- при переходе в архив внутри
        # подпапки ТЕРЯЕТ путь до архива, тот же путь задним числом дублируется дальше через
        # rel_prefix -- неверный порядок при попытке восстановить реальное дерево). ВСЕГДА
        # SOURCE-относителен (никогда не абсолютен, в отличие от origin_prefix/disp при
        # depth==0), архивные сегменты -- ПОЛНОЕ имя файла (с расширением). Растёт ТОЛЬКО в
        # точке обнаружения архива (ниже по функции) и в _handle_dvd_unit() -- обычный спуск по
        # подпапкам его не трогает (та же роль, что origin_prefix уже играет для этой цели).
        # ROADMAP.md "RecursionError на очень глубоком дереве папок SOURCE": до этой правки
        # descent в подпапки шёл через `yield from self._walk_dir(...)` -- дерево глубиной
        # ~1000+ уровней (путь при этом всего пара КБ, ничего экстремального для
        # Windows-длинных-путей) роняло RecursionError и обрывало ВЕСЬ прогон, даже независимые
        # файлы вне глубокой ветки. Явный стек вместо рекурсии по подпапкам -- единственный
        # рекурсивный вызов был в самом САМОМ КОНЦЕ метода, без обработки результата после
        # (ничего не делается с yield'нутыми элементами здесь же), поэтому эквивалентная замена
        # прямая: pending-подпапки кладутся в стек вместо рекурсивного спуска. origin_prefix/
        # depth/archive_no_crc/archive_boundary_idx/tree_rel_prefix (2026-08-14, см. её
        # докстрин ниже) остаются константами на весь вызов (как и раньше передавались в
        # рекурсию без изменений) -- по стеку путешествуют только то, что реально менялось на
        # каждом уровне: dirpath/rel_prefix/is_root/ancestors.
        # Архивная рекурсия (_handle_archive ниже, вложенные архивы) НЕ трогается -- она уже
        # ограничена max_archive_depth и не растёт с глубиной папок SOURCE, отдельный, гораздо
        # более мелкий источник глубины стека.
        # 2026-08-14: rel_prefix (параметр, НЕ cur_rel_prefix -- константа на весь вызов, та же
        # роль, что и у origin_prefix) -- собственный "старт" ЭТОГО вызова _walk_dir(). Любой
        # cur_rel_prefix/rel внутри этого вызова ВСЕГДА начинается ровно с rel_prefix (только
        # растёт через подпапки/имя файла) -- в частности, для вызова, начатого на содержимом
        # архива, rel_prefix уже несёт собственное (расширение-срезанное) имя архива, которое
        # ТАКЖЕ уже зашито в tree_rel_prefix (расширение сохранено, см. её докстрин выше) --
        # наивная склейка tree_rel_prefix + cur_rel_prefix задваивала бы сегмент архива (живая
        # находка тестами, 2026-08-14: "Album.zip/Album" вместо "Album.zip"). Срезаем этот
        # уже учтённый tree_rel_prefix'ом префикс перед склейкой.
        def _tree_rel(local_rel):
            extra = local_rel[len(rel_prefix):].lstrip("/")
            return "/".join(p for p in (tree_rel_prefix, extra) if p)

        stack = [(dirpath, rel_prefix, is_root, ancestors)]
        while stack:
            cur_dirpath, cur_rel_prefix, cur_is_root, cur_ancestors = stack.pop()

            # B (2026-08-28, см. _check_pause_keypress()): обход содержимого распакованного
            # архива (тот же _walk_dir(), рекурсивно) на источнике с гигантским деревом --
            # час-два без единого выхода в верхний цикл, где стоит основной опрос паузы.
            _check_pause_keypress(log=self.log)

            if not cur_is_root:
                if os.path.realpath(cur_dirpath) == self._target_real:
                    continue  # self-eating protection: never descend into TARGET
                base = os.path.basename(cur_dirpath)
                base_lower = base.lower()
                if base_lower in HARD_EXCLUDE_DIRS:
                    self._record_excluded_dir(base_lower, "защищено программой, не настраивается")
                    if base_lower == "__служебные_файлы":
                        # ROADMAP.md, analyze как "2 части": побочный продукт того же обхода,
                        # не отдельный проход -- любая папка __служебные_файлы, встреченная
                        # где угодно в дереве SOURCE (кроме самого TARGET, см. self-eating
                        # protection выше -- TARGET проверяется первым и никогда не доходит до
                        # этой ветки), помечает своего родителя как найденный архив.
                        self.found_archive_roots.append(os.path.realpath(os.path.dirname(cur_dirpath)))
                    continue
                if base_lower in self.cfg.default_exclude_dirs_lower:
                    self._record_excluded_dir(base_lower, "по умолчанию -- настраивается через default_exclude_dirs")
                    continue
                if base_lower in self.cfg.extra_exclude_dirs_lower:
                    self._record_excluded_dir(base_lower, "добавлено пользователем через extra_exclude_dirs")
                    continue
                # Живая находка (боевой прогон, 2026-08-09): is_under_system_dir() -- эвристика
                # "это похоже на мусор ОС, не настоящее SOURCE-содержимое пользователя", не
                # применима к содержимому, распакованному ИЗ АРХИВА -- такой путь физически
                # лежит под cfg.tmp_extract (см. self._tmp_extract_prefix в __init__()),
                # независимо от того, куда физически смотрит сам tmp_extract на диске. Без этой
                # проверки — CLI/`[1]` `analyze` БЕЗ явного `--target` (cfg.target =
                # _NO_TARGET_PLACEHOLDER, живёт под %TEMP%, а %TEMP% сам в SYSTEM_DIR_ENV_VARS)
                # тихо считал содержимое ЛЮБОГО архива с непустой подпапкой внутри "системной
                # папкой" и отбрасывал его целиком — воспроизведено живьём: архив с реальными
                # медиафайлами по предварительному листингу показывал "найдено медиафайлов 0"
                # после распаковки, файлы пропадали из total_files/n_images во всех счётчиках
                # analyze. Реальная сборка (`archive`/`--dry-run`) не задета на практике -- там
                # TARGET обязателен и выбирается пользователем (обычно не под системной папкой),
                # но тот же баг сработал бы и там, укажи пользователь TARGET под системной
                # директорией -- фикс общий, не завязан на конкретный вызывающий режим. Первая
                # попытка фикса проверяла origin_prefix вместо пути -- он пуст для САМОГО
                # ВЕРХНЕГО архива, если он же и есть SOURCE (walk()'s ветка "SOURCE is a single
                # archive file" передаёт origin_prefix="" явно), не покрывала этот случай --
                # поймано regression-тестом на этом же сценарии, не оставлено недиагностированным.
                if (not os.path.normcase(cur_dirpath).startswith(self._tmp_extract_prefix)
                        and not self.cfg.scan_system_dirs
                        and not self._root_under_system_dir and is_under_system_dir(cur_dirpath)):
                    self.system_dir_skips.append(cur_dirpath)
                    continue

            if self._progress_cb is not None:
                # 2026-07-11, user feedback: set BEFORE the (potentially slow, e.g. network
                # drive/huge directory) os.listdir() call below, not after -- being inside a
                # slow listdir with nothing processed yet is exactly the case this exists to
                # make visible. origin_prefix is set only while walking an archive's extracted
                # temp dir (a meaningless hash-named path on disk) -- shown as
                # "archive.zip → subdir" instead, same convention as every other
                # archive-nested display elsewhere.
                disp = f"{origin_prefix}{cur_rel_prefix}" if origin_prefix else cur_dirpath
                self._progress_cb(_truncate_progress_note(disp))

            # REVIEW-HANDOFF.md, Раунд 58 [ЗАМЕЧАНИЕ]: os.listdir() ниже -- та же потенциально
            # медленная операция, для которой self._progress_cb() выше уже существует (см. его
            # комментарий, 2026-07-11) -- но КАКАЯ ИМЕННО из посещаемых в Фазе 1 папок окажется
            # dump-веткой (или содержит файлы без альбома), заранее неизвестно, узнаётся только
            # ПОСЛЕ os.listdir()+сниффинга типа каждого файла (см. append-точки
            # _deferred_bydate_roots/_deferred_tilde_archives/_deferred_stray_files ниже по
            # функции). Открываем сегмент здесь безусловно в Фазе 1 И в Фазе 3 (2026-08-08,
            # альбомный редизайн: с уходом позиционных исключений is_dump_segment()/
            # find_album() отравляют ВЕСЬ путь СРАЗУ, как только встретился служебный сегмент,
            # даже до того, как в него спустились -- целые dump-поддеревья теперь откладываются
            # ЦЕЛИКОМ на Фазу 3 гораздо чаще и раньше, чем раньше, вместо построчной обработки
            # прямо в Фазе 1 -- без этого их os.listdir()/классификация файлов при разворачивании
            # в _drain_deferred_phases() перестала бы прятаться от EMA ровно там, где раньше
            # прятался этот же самый контент). Фаза 2 повторных откладываний не делает, но там и
            # нет промежуточного os.listdir() внутри самого _walk_dir() -- не нужен.
            if self._phase in (1, 3):
                self._open_deferred_gap()

            try:
                entries = sorted(os.listdir(winlong(cur_dirpath)))
            except OSError as e:
                self._log_own_line(f"  не удалось прочитать директорию {cur_dirpath}: {e}")
                # REVIEW-HANDOFF.md, Раунд 32, задача 4: раньше только текст в лог -- ничего
                # не считалось, отчёт не давал пользователю базы для сверки "не пропало ли
                # что-то молча" (права доступа/длинный путь/повреждённая ФС на старой флешке).
                self.listdir_failed.append(cur_dirpath)
                continue

            # REVIEW-HANDOFF.md, Раунд 24 (2026-07-21): найденные архивы (found_archive_roots)
            # раньше опознавались ТОЛЬКО по буквальному имени __служебные_файлы (см. выше) --
            # в отличие от _target_has_existing_archive()/warn_if_target_nested_in_archive(),
            # у которых уже есть fallback на Albums+ByDate, если служебную папку переименовали
            # или удалили. entries уже прочитан строкой выше ради обычного разбора файлов/
            # подпапок -- доп. проверка не стоит нового os.listdir/I-O. Дубликат с
            # marker-детекцией выше (тот же корень найден и по __служебные_файлы, и по
            # Albums+ByDate одновременно) безвреден -- classify_found_archives() дедуплицирует
            # raw_roots перед классификацией.
            entries_lower = {e.lower() for e in entries}
            if "albums" in entries_lower and "bydate" in entries_lower:
                self.found_archive_roots.append(os.path.realpath(cur_dirpath))
            # SESSION-HANDOFF.txt п.7 (2026-08-05, боевой прогон): раньше analyze проактивно
            # пропускал (`continue`) содержимое найденного архива целиком -- расходилось с тем,
            # что реально делает сборка ([3]/CLI archive/--dry-run обходят и учитывают ВСЁ
            # содержимое SOURCE без исключений). found_archive_roots по-прежнему собирается (для
            # рекомендации "на источнике уже есть архив" в отчёте, см.
            # _render_analyze_recommendations()) -- просто больше не приводит к пропуску обхода,
            # Albums/ByDate внутри найденного архива теперь считаются как обычная папка.

            if not cur_is_root:
                if SKIP_MARKER in entries:
                    disp = origin_prefix + cur_rel_prefix
                    self.skipped_marker_logs.append(disp)
                    self._log_own_line(f"  [skip_marker] {disp}")
                    continue

            subdirs = []
            files = []
            for name in entries:
                full = os.path.join(cur_dirpath, name)
                if os.path.isdir(winlong(full)):
                    subdirs.append(name)
                else:
                    files.append(name)

            # "объектов X/Y" (см. __init__()): тикает В МОМЕНТ ЗАВЕРШЕНИЯ разбора имени --
            # сразу после yield/yield-from (когда вызывающий код уже полностью обработал item
            # и вернулся за следующим), либо сразу на месте для media-кандидата, чьё решение
            # окончательно и мгновенно (ошибка stat()). Речь пользователя, 2026-08-07
            # (живой боевой прогон F:→D:, "объектов 5577/27918 | всего медиа 2476 -- а что в
            # остальных?"): раньше тик стоял ДО любых проверок, безусловно на каждое имя при
            # самом обходе -- убегал далеко вперёд "всего медиа" на файлах/архивах, отложенных
            # Фазой 1 на Фазу 2/3 (_deferred_stray_files/_deferred_tilde_archives, см.
            # _drain_deferred_phases()), хотя реально ещё не обработан ни один из них. Момент
            # "имя увидено" и "разбор окончен" для отложенных путей -- РАЗНЫЕ моменты, для
            # неотложенных -- совпадают (yield блокирует генератор до полной обработки
            # вызывающим кодом, "увидено" и "окончено" физически не могут разъехаться).
            # depth==0 -- эта функция вызывается рекурсивно и на содержимом РАСПАКОВАННОГО
            # архива (depth>=1), которое знаменатель (_quick_media_count_estimate()) никогда не
            # видит (не открывает архивы) -- архив/DVD-юнит считается РОВНО ОДИН раз как единое
            # целое (тикает здесь же, после yield from на его обработку), не по файлам внутри.
            #
            # 2026-08-17 (боевой прогон, источник с очень большой долей немедийных файлов):
            # exclude/sidecar/тип "other" здесь БОЛЬШЕ НЕ ТИКАЮТ (см. вызывающий код files-цикла
            # ниже -- решение "не медиа" принимается на месте, `continue` без `_tick_object()`).
            # Раньше все файлы весили "1" поровну в X и Y -- источник, где немедийные файлы
            # (мгновенное, дешёвое решение) численно доминируют над реальными медиа (дорогая
            # exif/hash-обработка), доводил X до Y почти сразу после того, как такая папка
            # дощупана обходом, хотя реальная (медленная) обработка медиафайлов в остальном
            # дереве только начиналась -- "обработано объектов 100%" держалось клэмпом
            # (min(X/Y*100, 100.0) в _build_two_line_status()) буквально весь остаток прогона.
            # Теперь X и Y считают ТОЛЬКО media-кандидатов (image/raw/video/archive/DVD-юнит) --
            # та же гранулярность, что и у _quick_media_count_estimate() ниже (обновлена тем же
            # заходом) -- немедийные файлы по-прежнему обходятся и логируются как раньше, просто
            # не входят в счёт этой конкретной метрики.
            def _tick_object():
                if depth == 0 and self._object_progress_cb is not None:
                    self._object_progress_cb(1)

            # 2026-08-07, по прямой просьбе пользователя (боевой прогон, домашнее видео на
            # DVD): VIDEO_TS -- ОДНА неделимая единица (см. секцию "DVD-VIDEO UNITS" выше),
            # копируется целиком через _handle_dvd_unit() либо признаётся дублем уже
            # архивированного диска -- в обоих случаях `continue` пропускает обычную
            # по-файловую обработку этой папки (sibling-pairing/per-file loop/subdirs-push
            # ниже) -- её содержимое уже полностью учтено (рекурсивно, см.
            # _dvd_unit_file_records()) внутри самого _handle_dvd_unit().
            if _is_video_ts_dir(cur_dirpath, files):
                disp_base = f"{origin_prefix}{cur_rel_prefix}" if origin_prefix else cur_dirpath
                # 2026-08-14: в отличие от disp_base выше (абсолютный путь при depth==0/вне
                # архива -- нужен origin_display/логам для показа реального пути) -- _tree_rel()
                # (см. её докстрин выше, в начале _walk_dir()): ВСЕГДА относителен, архивные
                # предки (если VIDEO_TS лежит внутри архива) -- с расширением, без потери/
                # задвоения пути. Источник для SourceItem.dvd_source_tree_key (см. её докстрин)
                # -- дерево реальной структуры SOURCE в analyze-отчёте
                # (AnalyzeStats.source_tree_counts).
                source_tree_key = _tree_rel(cur_rel_prefix)
                # outer_rel_prefix -- логический путь СНАРУЖИ VIDEO_TS (cur_rel_prefix
                # ЗАКАНЧИВАЕТСЯ сегментом "video_ts" -- это ТЕКУЩАЯ папка, не то, что вокруг
                # неё) -- используется и для find_album() ниже (2026-08-07, уточнение того же
                # дня "а что если VIDEO_TS лежит внутри ~Папки?" -- та же проверка, что и для
                # обычного файла на этом месте, отравление тильда/dump-веткой демотирует ВЕСЬ
                # юнит в ByDate целиком), и для human_disk_name (человекочитаемое имя "диска"
                # ДЛЯ ОТЧЁТА/логов -- НЕ для реального пути назначения, тот решает
                # find_album()/метка тома/ByDate, см. _handle_dvd_unit()). БЕЗ этого разделения
                # find_album() отдал бы "video_ts" САМ КАК subpath-сегмент, и _handle_dvd_unit()
                # приклеила бы "VIDEO_TS" ЕЩЁ раз поверх (живая находка при первом прогоне
                # тестов этой правки -- Albums/<альбом>/VIDEO_TS/VIDEO_TS/файл, задвоение).
                outer_rel_prefix = (cur_rel_prefix.rsplit("/", 1)[0] if "/" in cur_rel_prefix
                                     else "")
                # human_disk_name: depth==0 -- реальная родительская папка на диске (надёжнее
                # логического outer_rel_prefix для настоящей файловой системы). depth>=1
                # (VIDEO_TS ВНУТРИ архива, cur_dirpath живёт под cfg.tmp_extract):
                # os.path.dirname(cur_dirpath) непредсказуем (синтетический путь распаковки) --
                # берём последний сегмент ЛОГИЧЕСКОГО outer_rel_prefix вместо него.
                if depth == 0:
                    human_disk_name = os.path.basename(os.path.dirname(cur_dirpath))
                else:
                    human_disk_name = (os.path.basename(outer_rel_prefix) if outer_rel_prefix
                                        else _strip_trailing_arrow(origin_prefix))
                fake_rel_path = f"{outer_rel_prefix}/x" if outer_rel_prefix else "x"
                album, subpath, album_prefix = find_album(
                    fake_rel_path, archive_boundary_idx,
                    dump_names=self.cfg.dump_segment_names_lower,
                    dump_prefixes=self.cfg.dump_segment_prefixes_tuple,
                    bydate_only=self.cfg.source_bydate_only)
                yield from self._handle_dvd_unit(cur_dirpath, disp_base, archive_no_crc,
                                                  check_volume_label=(depth == 0),
                                                  album=album, subpath=subpath,
                                                  album_prefix=album_prefix,
                                                  human_disk_name=human_disk_name,
                                                  source_tree_key=source_tree_key)
                # VIDEO_TS -- одна неделимая единица (см. комментарий выше), тикает как ОДИН
                # объект целиком, а не по файлам внутри -- та же логика, что и у архива.
                _tick_object()
                continue

            # same-basename RAW<->image sibling pairing (scoped to this directory)
            # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2: этот же проход уже вызывает
            # file_type() на каждый файл папки -- расширен на video (не только image/raw) для
            # "[папка] ... найдено медиафайлов N", не заводить второй проход ради этого счётчика.
            sibling_by_base = {}
            folder_media_count = 0
            for name in files:
                t = file_type(os.path.join(cur_dirpath, name))
                if t in ("image", "raw", "video"):
                    folder_media_count += 1
                if t not in ("image", "raw"):
                    continue
                base_noext = os.path.splitext(name)[0].lower()
                sibling_by_base.setdefault(base_noext, {})[t] = os.path.join(cur_dirpath, name)

            if self._object_line_cb is not None:
                disp_for_object = f"{origin_prefix}{cur_rel_prefix}" if origin_prefix else cur_dirpath
                # Речь пользователя, 2026-08-07 ("получается, что выводить нужно в начале
                # обработки"): если у папки нет шанса найти альбом (Фаза 1, найдётся ли альбом
                # -- решается по СЕГМЕНТАМ пути, не по конкретному имени файла, "x" -- тот же
                # пробный приём, что уже используется для DVD-юнита выше), её файлы всё равно
                # уйдут в _deferred_stray_files -- печатать строку сейчас означало бы печатать
                # задолго до того, как Фаза 3 реально возьмётся за эту папку. folder_media_count
                # == 0 -- печатаем сразу как раньше, откладывать нечего (ни один файл не уйдёт в
                # очередь).
                #
                # Живая находка пользователя, 2026-08-09: та же самая probe_album нужна и для
                # буквы "A"/"D" (см. _placement_letter()) -- probe_album==None ЗНАЧИТ "уйдёт по
                # дате" ровно в том же смысле, что уже используется для defer_announcement,
                # поэтому одна проба на оба вопроса, не дублировать find_album(). Внутри
                # need_probe -- та же проверка, что раньше гейтила единственный вызов, плюс
                # self._show_placement_letter (буква нужна и вне phase==1/media>0, например для
                # пустых папок или Фазы 2/3, где раньше probe_album вообще не вычислялся).
                defer_announcement = False
                probe_album = None
                need_probe = self._show_placement_letter or (self._phase == 1 and folder_media_count > 0)
                if need_probe:
                    probe_rel = f"{cur_rel_prefix}/x" if cur_rel_prefix else "x"
                    probe_album, _s, _p = find_album(
                        probe_rel, archive_boundary_idx,
                        dump_names=self.cfg.dump_segment_names_lower,
                        dump_prefixes=self.cfg.dump_segment_prefixes_tuple,
                        bydate_only=self.cfg.source_bydate_only)
                if self._phase == 1 and folder_media_count > 0:
                    defer_announcement = probe_album is None
                letter = ("A" if probe_album is not None else "D") if self._show_placement_letter else ""
                if defer_announcement:
                    self._pending_folder_announcements[cur_dirpath] = (disp_for_object, folder_media_count, letter)
                else:
                    self._object_line_cb("folder", disp_for_object, folder_media_count, letter)

            def _defer_raw_with_sibling(name, _dirpath=cur_dirpath, _sibling_by_base=sibling_by_base):
                t = file_type(os.path.join(_dirpath, name))
                if t != "raw":
                    return 0
                base_noext = os.path.splitext(name)[0].lower()
                return 1 if "image" in _sibling_by_base.get(base_noext, {}) else 0

            files.sort(key=_defer_raw_with_sibling)

            for name in files:
                # B (2026-08-28, см. _check_pause_keypress()): большая плоская папка внутри
                # архива -- десятки тысяч файлов без выхода в верхний цикл с опросом паузы.
                _check_pause_keypress(log=self.log)
                full = os.path.join(cur_dirpath, name)
                if _matches_any(name, EXCLUDE_FILES_PATTERNS) or name == SKIP_MARKER:
                    # "объектов X/Y" больше не считает non-media файлы вовсе (см. докстрин
                    # _tick_object() выше) -- НЕ тикаем: _quick_media_count_estimate() эти файлы
                    # тоже не учитывает.
                    continue
                if _matches_any(name, SIDECAR_PATTERNS):
                    self.sidecar_logs.append(origin_prefix + cur_rel_prefix + "/" + name if cur_rel_prefix else origin_prefix + name)
                    continue

                rel = f"{cur_rel_prefix}/{name}" if cur_rel_prefix else name
                disp = f"{origin_prefix}{rel}" if origin_prefix else rel
                # 2026-08-14 -- см. tree_rel_prefix/_tree_rel() докстрины у _walk_dir(); только
                # для НЕ-архивных файлов (SourceItem.source_tree_path) -- архивы вычисляют свой
                # собственный new_tree_rel_prefix отдельно, в ветке `if fmt:` ниже.
                tree_rel = _tree_rel(rel)

                fmt = detect_archive_format(full)
                if fmt:
                    base_no_ext = name[: -(len(ext_of(name)) + 1)] if ext_of(name) else name
                    new_rel_prefix = f"{cur_rel_prefix}/{base_no_ext}" if cur_rel_prefix else base_no_ext
                    new_origin_prefix = f"{origin_prefix}{name} → "
                    # 2026-08-14: в отличие от new_rel_prefix (расширение срезано, для
                    # find_album()) и new_origin_prefix (расширение есть, но БЕЗ cur_rel_prefix
                    # -- теряет путь до архива внутри подпапки, см. докстрин tree_rel_prefix у
                    # _walk_dir()) -- полный, верно упорядоченный SOURCE-относительный путь ДО
                    # архива включительно, с расширением, через тот же _tree_rel(), что и файлы.
                    new_tree_rel_prefix = _tree_rel(f"{cur_rel_prefix}/{name}" if cur_rel_prefix else name)
                    # 2026-07-11: record the OUTERMOST archive's own name-segment index the
                    # first time we cross into any archive -- a nested archive-inside-archive
                    # keeps the outer one's boundary (see find_album()), not its own.
                    this_boundary = archive_boundary_idx
                    if this_boundary is None:
                        this_boundary = cur_rel_prefix.count("/") + 1 if cur_rel_prefix else 0
                    # Двухфазный обход (см. __init__()): архив с собственным тильда/dump-именем
                    # (или без букв вовсе) безусловно ByDate независимо от места на диске --
                    # откладываем его извлечение на Фазу 2, чтобы весь "чистый" альбомный
                    # контент Фазы 1 успел первым занять место в пуле дедупа. Только в Фазе 1 --
                    # в Фазах 2/3 self._phase уже не 1, повторных откладываний не бывает.
                    if self._phase == 1 and _is_terminal_bydate_branch(
                            new_rel_prefix.split("/"), archive_boundary_idx=this_boundary,
                            dump_names=self.cfg.dump_segment_names_lower,
                            dump_prefixes=self.cfg.dump_segment_prefixes_tuple):
                        # Откладывается на Фазу 2 (_drain_deferred_phases()) -- НЕ тикаем
                        # сейчас, разбор архива ещё не начат, тик -- там же, сразу после
                        # yield from self._handle_archive() в цикле по _deferred_tilde_archives.
                        self._deferred_tilde_archives.append(
                            (full, new_rel_prefix, new_origin_prefix, depth + 1, this_boundary,
                             new_tree_rel_prefix))
                        continue
                    yield from self._handle_archive(full, new_rel_prefix, new_origin_prefix, depth + 1,
                                                     archive_boundary_idx=this_boundary,
                                                     tree_rel_prefix=new_tree_rel_prefix)
                    # Архив -- одна неделимая единица (см. _tick_object()), тикает здесь целиком
                    # СРАЗУ ПОСЛЕ того, как весь его контент уже проехал через yield from --
                    # то есть вызывающий код (run_for_source()) уже полностью его обработал.
                    _tick_object()
                    continue

                t = file_type(full)
                if t == "other" or t == "archive":
                    # t == "archive" здесь -- ТОЛЬКО бэйр .gz/.bz2, который detect_archive_format()
                    # выше уже отверг (одиночный сжатый файл: core.log.gz, dump.sql.gz, UTF-8.gz --
                    # НЕ многофайловый архив; настоящие .zip/.7z/.rar/.tar/.tar.gz/.tgz/.tar.bz2 все
                    # ушли в ветку `if fmt:` выше и сюда не доходят). Живой боевой прогон
                    # (2026-08-29): без этой ветки такой файл получал SourceItem(ftype="archive")
                    # и КОПИРОВАЛСЯ в Albums/ как "unknown_type" (мусор -- .sync-логи YandexDisk,
                    # locale-файлы). Распаковать его нечем, медиа внутри почти не бывает -- пропуск,
                    # как и "other".
                    #
                    # Files with no plausible photo/video relevance (.exe, .docx, .pdf, ...)
                    # are silently ignored: not copied, not disputed, not logged. Only
                    # image/raw/video/archive extensions enter the pipeline at all; borderline
                    # cases within those (icons, tiny images, broken files) are still routed to
                    # _disputed later via the is_media classification.
                    # "объектов X/Y" не тикает за них (см. докстрин _tick_object()) -- источник с
                    # горой немедийных файлов (боевой прогон пользователя, 2026-08-17) больше не
                    # доминирует в знаменателе и не разгоняет числитель раньше реальной работы.
                    continue

                try:
                    st = os.stat(winlong(full))
                except OSError as e:
                    self.stat_failed_logs.append((disp, str(e)))
                    self._log_own_line(f"  не удалось прочитать {disp}: {e}")
                    _tick_object()
                    continue

                sibling_path = None
                if t in ("image", "raw"):
                    base_noext = os.path.splitext(name)[0].lower()
                    other_type = "raw" if t == "image" else "image"
                    sibling_path = sibling_by_base.get(base_noext, {}).get(other_type)

                item = SourceItem(full, disp, rel, st.st_size, st.st_mtime, t, sibling_path,
                                   zone=classify_zone(full), archive_no_crc=archive_no_crc,
                                   archive_boundary_idx=archive_boundary_idx,
                                   source_tree_path=tree_rel)
                # Двухфазный обход (см. __init__()): в отличие от папки/архива, файл -- лист,
                # find_album() для него уже даёт ОКОНЧАТЕЛЬНЫЙ ответ без всякой двусмысленности
                # "а вдруг альбом найдётся глубже" (там просто нет ничего глубже). Файл без
                # альбома (album is None) -- ByDate, откладываем на Фазу 3 тем же принципом,
                # что и поддеревья/архивы: чистый альбомный контент должен успеть в пул первым.
                if self._phase == 1:
                    album, _subpath, _prefix = find_album(
                        rel, archive_boundary_idx,
                        dump_names=self.cfg.dump_segment_names_lower,
                        dump_prefixes=self.cfg.dump_segment_prefixes_tuple,
                        bydate_only=self.cfg.source_bydate_only)
                    if album is None:
                        # Откладывается на Фазу 3 -- НЕ тикаем сейчас, файл ещё не обработан,
                        # тик -- там же, сразу после yield в цикле по _deferred_stray_files
                        # (_drain_deferred_phases()).
                        self._deferred_stray_files.append(item)
                        continue
                self._close_deferred_gap()
                yield item
                # См. defer_media_object_tick в __init__(): run_analyze() тикает эту же
                # единицу сама, позже, поштучно, после analyze_batch() (2026-08-18).
                if not self._defer_media_object_tick:
                    _tick_object()

            # LIFO стек -- пушим в ОБРАТНОМ sorted-порядке, чтобы pop() отдавал подпапки в том
            # же порядке (по возрастанию имени), в каком их раньше обходила рекурсия; порядок
            # обхода влияет на то, какой из дублей "выигрывает" имя при дедупе (см. RULES.md).
            for name in reversed(subdirs):
                full = os.path.join(cur_dirpath, name)
                # Security audit finding #3: a directory junction/symlink can point back at an
                # ancestor of itself (deliberately, as a booby trap on a hostile SOURCE, or by
                # accident) -- os.path.isdir()/os.listdir() both follow reparse points on
                # Windows, and neither is guarded anywhere else in this walk, so without this
                # check such a loop recurses forever (now: grows the stack forever) instead of
                # terminating. realpath() resolves the reparse point; if it matches anything
                # already open on this branch of the walk, it's a cycle -- skip it.
                full_real = os.path.normcase(os.path.realpath(full))
                if full_real in cur_ancestors:
                    self._log_own_line(f"  [symlink_loop] пропущена зацикленная папка (junction/symlink "
                             f"ведёт на себя или предка по дереву): {full}")
                    continue
                rel = f"{cur_rel_prefix}/{name}" if cur_rel_prefix else name
                # Двухфазный обход (см. __init__()): эта папка отравлена тильда/dump-именем
                # НИЖЕ уже найденного альбома (или путь исчерпал поиск альбома вовсе) --
                # НЕ спускаемся сейчас, всё поддерево целиком откладывается на Фазу 3 (см.
                # _is_terminal_bydate_branch()'s докстринг про принципиальную разницу с
                # "ещё ищем" -- та ветка ДОЛЖНА обходиться нормально и в Фазе 1, реальный
                # альбом может найтись глубже).
                if self._phase == 1 and _is_terminal_bydate_branch(
                        rel.split("/"), archive_boundary_idx=archive_boundary_idx,
                        dump_names=self.cfg.dump_segment_names_lower,
                        dump_prefixes=self.cfg.dump_segment_prefixes_tuple):
                    # 2026-08-14: НЕ tree_rel_prefix "как есть" (живая находка тестами -- у
                    # отложенной обычной ПАПКИ, в отличие от архива, до этой точки нет своего
                    # "new_tree_rel_prefix", а _tree_rel() внутри деренного re-walk безусловно
                    # СРЕЗАЕТ rel_prefix-баланс, считая его уже учтённым в tree_rel_prefix --
                    # без пересчёта здесь путь до самой "~synced"-папки терялся бы целиком).
                    # _tree_rel(rel) досчитывает его тем же способом, что и для архива.
                    self._deferred_bydate_roots.append(
                        (full, rel, origin_prefix, cur_ancestors + (full_real,),
                         archive_boundary_idx, archive_no_crc, depth, _tree_rel(rel)))
                    continue
                stack.append((full, rel, False, cur_ancestors + (full_real,)))

    def _handle_archive(self, archive_path, rel_prefix, origin_prefix, depth, archive_boundary_idx=None,
                         tree_rel_prefix=""):
        # Живой репорт пользователя (2026-08-01): архив верхнего уровня показывал только голое
        # имя без пути ("upravlenie_osvesheniem.zip"), в отличие от [папка]-строк (см.
        # disp_for_object в _walk_dir()), которые в той же ситуации честно показывают
        # cur_dirpath. Причина: origin_prefix ДЛЯ АРХИВА уже включает его собственное имя
        # (new_origin_prefix = f"{origin_prefix}{name} → ", см. вызывающий код _walk_dir()) --
        # "самоссылающийся", никогда не пуст даже для первого, невложенного архива, поэтому
        # старое "if origin_prefix else basename" никогда не срабатывало на реальный путь.
        # depth==1 -- archive_path гарантированно живёт под SOURCE (не под tmp_extract, где он
        # был бы для depth>1) -- тот же приём, что и note password_protected-архива ниже (Раунд
        # 47 ревью): для depth>1 archive_path эфемерен (будет удалён cleanup_dir() ещё до конца
        # прогона), там остаётся origin-трейл ("outer.zip → inner.zip"), не реальный путь.
        if depth == 1:
            display_name = archive_path
            full_display = archive_path
        else:
            display_name = _strip_trailing_arrow(origin_prefix) if origin_prefix else os.path.basename(archive_path)
            full_display = origin_prefix if origin_prefix else os.path.basename(archive_path)

        # Живой боевой прогон, 2026-08-28: та же проба размещения ("A"/"D"), что и у объект-
        # строки этого архива ниже (:_object_line_cb) -- считаем ОДИН раз здесь, чтобы её несли
        # ВСЕ статус-строки _log_archive() этого архива (не только "распаковано, найдено N", но
        # и bomb/no-space/traversal/no-media), а не расходились с объект-строкой. "" если
        # show_placement_letter выключен (analyze/[4]) -- _placement_letter() сам это гейтит,
        # вызов дёшев (find_album() по фиктивному "x", без I/O).
        placement_letter = self._placement_letter(rel_prefix, archive_boundary_idx)

        if depth > self.cfg.max_archive_depth:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                               "превышена глубина вложенности", letter=placement_letter)
            return

        fmt = detect_archive_format(archive_path)
        info = list_archive(archive_path, fmt)
        # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2: "[archive] ... найдено
        # медиафайлов N" -- печатается здесь, СРАЗУ после листинга, а не только для архивов,
        # реально дошедших до распаковки -- это индикатор "программа сейчас смотрит на этот
        # объект", не обещание, что он будет распакован (проверки ниже могут его ещё
        # отклонить). info.ok=False -- листинг не читается, media_count заведомо 0 и был бы
        # ложным "ничего не найдено" -- не печатаем вовсе, вместо этого нужный сигнал уже даёт
        # archive_bomb_suspected в archives.log чуть ниже по функции.
        if info.ok and self._object_line_cb is not None:
            # Живая находка пользователя, 2026-08-09: буква "A"/"D" -- та же проба, что и у
            # объявления папки (_placement_letter()/find_album() с фиктивным "x" в конце) --
            # rel_prefix здесь уже включает собственное имя архива последним сегментом (см.
            # вызывающий код _walk_dir(): new_rel_prefix = f"{cur_rel_prefix}/{base_no_ext}"),
            # тот же смысл, что cur_rel_prefix у папки. Посчитана один раз выше (placement_letter).
            self._object_line_cb("archive", _strip_trailing_arrow(full_display), info.media_count,
                                  placement_letter)

        try:
            compressed_size = os.path.getsize(winlong(archive_path))
        except OSError as e:
            # 2026-07-11 (live user report): the user deleted this archive file WHILE
            # PhotoArchive was still scanning it -- this generator method runs during the
            # enumeration phase (Phase 2), a completely different code path from
            # place_file()/resolve_dest_path()'s already-guarded copy phase (see
            # _log_write_failure()'s docstring for that earlier fix). This raw os.path.getsize()
            # was never wrapped, so a vanished-mid-scan file raised OSError straight out of the
            # generator, through _walk_dir()/walk(), all the way to main() -- which only catches
            # KeyboardInterrupt/EOFError -- crashing the entire run with a raw traceback instead
            # of skipping just this one archive and continuing with everything else.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_read_error",
                               f"файл исчез или недоступен во время обработки: {e}",
                               letter=placement_letter)
            return

        if info.ok:
            if info.encrypted:
                # Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): note обычно
                # человекочитаемая причина у других статусов -- здесь вместо неё реальный
                # абсолютный путь архива (archive_path) -- для depth>1 full_display остаётся
                # ОТНОСИТЕЛЬНЫМ origin-трейлом (origin_prefix), из него одного file://-ссылку
                # не построить (2026-08-01: для depth==1 full_display теперь тоже archive_path,
                # см. его вычисление в начале _handle_archive(), но ветка ниже всё равно нужна
                # для depth>1).
                #
                # REVIEW-HANDOFF.md, Раунд 45, замечание 1: depth>1 -- архив НАЙДЕН ВНУТРИ уже
                # распакованного другого архива (см. докстринг про depth в walk()/_walk_dir()
                # выше -- depth инкрементируется ТОЛЬКО на переходе в архив-в-архиве, не на
                # обычном спуске по папкам) -- archive_path тогда живёт под cfg.tmp_extract и
                # будет удалён cleanup_dir() внешнего _handle_archive() ещё ДО того, как
                # report.py вообще начнёт писать отчёт (сам прогон ещё не закончился). Ссылка
                # на такой путь была бы мертворождённой -- не передаём note вовсе для этого
                # случая, report.py (_file_link_or_text()) естественно откатывается на текст.
                #
                # REVIEW-HANDOFF.md, Раунд 47, замечание 1: первая версия фикса передавала ""
                # для depth>1 -- report.py использует ОДИН И ТОТ ЖЕ note и как текст ссылки, и
                # как её href (_file_link_or_text(html.escape(p), p)), у пустой строки нет ни
                # ссылки, ни текста -- архив пропадал из списка не мёртвой ссылкой, а вовсе
                # ничем (плюс "осиротевший" "; "-разделитель у соседних записей). full_display
                # (тот же относительный "outer.zip → secret.zip", что уже используется как
                # текст лога/статуса чуть ниже) -- не абсолютный путь, _file_link_or_text() не
                # построит по нему ссылку, но покажет как читаемый текст, тот же паттерн, что
                # уже применяется к origin_display на level=="analyze".
                note = archive_path if depth == 1 else _strip_trailing_arrow(full_display)
                self._log_archive(_strip_trailing_arrow(full_display), "archive_password_protected",
                                   note, letter=placement_letter)
                return
            if info.path_traversal:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_path_traversal_suspected",
                                   "член архива содержит '..' или абсолютный путь -- не распаковываю",
                                   letter=placement_letter)
                return
            if info.total_size > 2 * 1024**3 and compressed_size > 0 and info.total_size > compressed_size * 100:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                                   f"ratio={info.total_size / max(compressed_size,1):.0f}x",
                                   letter=placement_letter)
                return
            if info.entries > MAX_ARCHIVE_ENTRIES:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                                   f"entries={info.entries} (лимит {MAX_ARCHIVE_ENTRIES})",
                                   letter=placement_letter)
                return
            required = info.total_size + int(self.cfg.free_space_margin_gb * 1024**3)
        else:
            # Листинг архива не читается -- реальный распакованный размер неизвестен, и
            # угадывать его коэффициентом (compressed_size*3) не даёт настоящей защиты: архив,
            # специально сконструированный ломать листинг, с тем же успехом может распаковаться
            # в тысячи раз больше заявленного и заполнить весь том прямо во время распаковки
            # (мимо этой предполётной проверки). Раз надёжной оценки места нет -- считаем
            # такой архив подозрительным и не распаковываем, а не гадаем с потолка.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                               "листинг архива не читается, распакованный размер неизвестен",
                               letter=placement_letter)
            return

        free = free_space_bytes(self.cfg.tmp_extract if os.path.isdir(winlong(self.cfg.tmp_extract)) else self.cfg.target)
        if required > free:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_skipped_no_space",
                               f"нужно ~{required/1024**3:.1f}ГБ, свободно {free/1024**3:.1f}ГБ",
                               letter=placement_letter)
            return

        # 2026-07-11 finding (live production run): a whole-disk scan runs into plenty of
        # installers/backups/configs zipped up with zero photos inside -- the listing already
        # parsed above (info.has_media_candidate) already names every member, so there is no
        # need to actually extract anything just to discover that afterwards. Same log status
        # ("archive_no_media") as the post-extraction empty-result case below, just reached
        # without ever touching tmp_extract for this archive.
        #
        # Живой репорт пользователя (2026-08-02): silent=True -- write_object_line() (:2889)
        # уже напечатал "найдено медиафайлов N" для этого архива ДО этой точки, с N, взятым
        # из того же info.media_count, из которого вычислен has_media_candidate -- N здесь
        # гарантированно 0, то есть уже показанное число. Печатать его снова -- буквальный
        # повтор той же строки (плюс на длинных путях -- перенос, т.к. эта вторая копия не
        # обрезалась под ширину терминала, в отличие от write_object_line()). archive_logs
        # (для archives.log/n_archives_found) по-прежнему пишется -- silent тушит только
        # консоль, см. докстринг _log_archive().
        if not info.has_media_candidate:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_no_media", silent=True,
                               letter=placement_letter)
            return

        try:
            # B (2026-08-28, см. _check_pause_keypress()): хеш многогигабайтного архива --
            # такая же "мёртвая зона" для паузы по пробелу, как и хеш большого видео.
            archive_hash = sha256_file(
                archive_path,
                progress_cb=(lambda: _check_pause_keypress(log=self.log)) if os.name == "nt" else None)
        except OSError as e:
            # Same race as the os.path.getsize() guard above, just later -- this reads the
            # WHOLE archive to hash it (real wall-clock time on a multi-GB file, exactly the
            # window the live user report happened in: "программа его продолжала распаковывать,
            # а потом срубилась"). Same fix, same reasoning.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_read_error",
                               f"файл исчез или недоступен во время обработки: {e}",
                               letter=placement_letter)
            return
        extract_dir = os.path.join(self.cfg.tmp_extract, archive_hash)

        # Задача 4: распаковка может занять минуты на большом архиве без собственного
        # прогресса (7z/unrar/tarfile не отдают построчный процент сюда) -- явная строка
        # "текущее действие", чтобы легитимная пауза не читалась как зависание. Идёт через
        # _log_own_line() -> write_heavy_notice() -- живой репорт пользователя (редизайн живого
        # вывода Фазы 2, 2026-08-01) поймал смежную проблему: write_heavy_notice() переносит
        # строки, не влезающие в окно целиком (_wrap_console_text()) -- длинное имя архива легко
        # выталкивает эту строку за порог, реально перенося её на вторую физическую строку.
        # tqdm-бар (см. log_line()) не знает об этом переносе -- его собственный clear()/
        # refresh() рассчитан ровно на одну строку, путается, и "хвост" перенесённой строки
        # остаётся видимым как визуальный дубль на следующем clear()/refresh()-цикле (раньше
        # такие циклы были редки -- новый объект-строка/transient-op механизм сделал их
        # намного чаще, скрытая возможность стала заметной на практике). Обрезаем ИМЯ архива
        # (не весь текст) под тот же бюджет переноса, тем же приёмом (_truncate_progress_note(),
        # от начала, идемпотентно), что и везде в этом файле -- хвост важнее (дата/расширение).
        display_name_for_log = _truncate_progress_note(display_name, maxlen=_extraction_log_name_budget())
        # _fmt_size_gb() -- та же функция, что уже используется в статус-строке (transient_op_cb
        # чуть ниже) -- живой репорт пользователя (2026-08-01): ручной f"{...:.1f} ГБ}" здесь
        # печатал "0.0 ГБ" для мелких архивов, "<0.1ГБ" читается однозначно.
        self._log_own_line(f"  Распаковка {display_name_for_log} ({_fmt_size_gb(compressed_size)})…")
        # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2: та же пауза, что и строкой
        # выше, но теперь ЕЩЁ и в статус-строке (ProgressReporter.set_transient_op()) -- этот
        # self.log() вызов -- отдельная, всегда печатаемая строка лога, transient_op_cb --
        # НЕЗАВИСИМЫЙ канал именно в бар, раньше распаковка была видна ТОЛЬКО в логе.
        if self._transient_op_cb is not None:
            self._transient_op_cb(f" Извлекаю ({_fmt_size_gb(compressed_size)})")
        outcome = extract_archive(archive_path, fmt, extract_dir, log=self.log)
        if not outcome:
            if self._transient_op_cb is not None:
                self._transient_op_cb(None)
            self._log_archive(_strip_trailing_arrow(full_display), "archive_extract_failed",
                               letter=placement_letter)
            cleanup_dir(extract_dir)
            return

        # C/D/E (2026-08-28): раньше tar-ветка extract_archive() печатала СЫРОЙ текст
        # исключения (по-английски) отдельной строкой на КАЖДЫЙ пропущенный член -- боевой
        # прогон дал 178 таких строк подряд от backup-архива прошивки роутера. Теперь одна
        # спокойная русская строка на архив в консоль (_log_own_line) + запись в archives.log
        # через archive_notes (отдельно от archive_logs -- не путать счётчики архивов, см.
        # __init__()). Настоящие сбои распаковки -- тем же способом, тоже по-русски, с кэпом.
        arch_disp = _strip_trailing_arrow(full_display)
        if outcome.skipped_meta:
            n = outcome.skipped_meta
            self._log_own_line(f"  в архиве пропущено служебных записей: {n} "
                               f"(ссылки и устройства — не файлы с данными)")
            self.archive_notes.append(
                (arch_disp, "meta_entries_skipped",
                 f"{n} служебных записей (ссылки/устройства) не распакованы"))
        if outcome.failure_total:
            shown = "; ".join(outcome.failures)
            more = outcome.failure_total - len(outcome.failures)
            tail = f"; и ещё {more}" if more > 0 else ""
            self._log_own_line(f"  не удалось распаковать файлов из архива: "
                               f"{outcome.failure_total} ({shown}{tail})")
            self.archive_notes.append(
                (arch_disp, "extract_partial_failure",
                 f"{outcome.failure_total} файлов не распаковано: {shown}{tail}"))

        # Живая находка пользователя, 2026-08-19: с этой точки и до конца функции (обход
        # распакованного содержимого -- на источнике с гигантским количеством вложенных
        # файлов/вложенных архивов это САМАЯ долгая часть всего прогона, час-два) поле
        # операции статус-строки должно показывать "разбор архива", а не молча откатываться
        # на статичный resting-текст, пока "обработано объектов %" честно стоит на месте
        # (архив тикает ОДНИМ объектом, только по завершении ВСЕГО содержимого, см.
        # _tick_object()). Счётчик, не bool/прямой set -- вложенный архив рекурсивно вызывает
        # этот же метод из _walk_dir() ниже; без счётчика его собственные
        # set("разбор архива")/set(None) затирали бы пометку внешнего архива, хотя обработка
        # внешнего ещё не закончена. try/finally покрывает ВСЕ выходы отсюда (ранние return
        # ниже по symlink/path-traversal находкам, и обычное завершение) -- не только
        # успешный путь.
        if self._archive_walk_depth == 0 and self._transient_op_cb is not None:
            self._transient_op_cb(_ARCHIVE_CONTENT_TRANSIENT_OP)
        self._archive_walk_depth += 1
        try:
            if fmt not in TAR_MODES:
                # tar/tar.gz/tar.bz2 already refuses (at extraction time, via filter="data") any
                # symlink member whose target would resolve outside dest_dir -- an in-bounds tar
                # symlink is legitimate content, not a reason to reject the whole archive. zip/7z/
                # rar extraction has no such built-in check (see find_reparse_point_in_tree()
                # docstring), so any reparse point found there is treated as suspicious outright.
                reparse = find_reparse_point_in_tree(extract_dir)
                if reparse:
                    self._log_archive(_strip_trailing_arrow(full_display), "archive_symlink_suspected",
                                       f"извлечённое дерево содержит symlink/junction ({reparse}) -- "
                                       f"содержимое архива не читаю", letter=placement_letter)
                    cleanup_dir(extract_dir)
                    return

                # Finding 7: если часть членов архива ушла за пределы extract_dir (traversal,
                # который не поймал текстовый парсер листинга -- см. count_extracted_files()),
                # здесь физически найдётся МЕНЬШЕ файлов, чем заявлено в листинге архива.
                extracted_count = count_extracted_files(extract_dir)
                if extracted_count < info.entries:
                    self._log_archive(_strip_trailing_arrow(full_display), "archive_path_traversal_suspected",
                                       f"распаковано {extracted_count} файлов из {info.entries} по листингу "
                                       f"-- похоже, часть содержимого вышла за пределы папки распаковки",
                                       letter=placement_letter)
                    cleanup_dir(extract_dir)
                    return

            media_count = 0
            extract_dir_real = os.path.normcase(os.path.realpath(extract_dir))
            # REVIEW-HANDOFF.md, Раунд 58 [БЛОКЕР] (см. __init__()'s self._pending_cleanup_dirs):
            # если _walk_dir() ниже отложит что-то (в self._phase == 1 -- только тогда это вообще
            # возможно, см. _walk_dir()) -- считаем это по росту счётчика отложенного ДО/ПОСЛЕ, не
            # по факту, что генератор исчерпан ("исчерпан" больше не значит "всё физически
            # посещено"). Если отложилось -- extract_dir остаётся на диске, реальная очистка
            # переносится в самый конец walk() (_drain_deferred_phases()), уже после того как
            # Фазы 2/3 прочитают из него всё, что им нужно.
            # Раунд 155 ревью: три append-only списка отложенного -- срез [before:after] по
            # каждому даёт РОВНО те записи, что этот архив (рекурсивно, включая вложенные
            # архивы, чьи _handle_archive() уже отработали и добавили СВОИ записи) отложил на
            # Фазы 2/3. Нужно и для решения про cleanup (было), и чтобы досчитать media_count
            # архива после дренажа (см. блок ниже + _drain_deferred_phases()).
            _bydate_before = len(self._deferred_bydate_roots)
            _tilde_before = len(self._deferred_tilde_archives)
            _stray_before = len(self._deferred_stray_files)
            try:
                for item in self._walk_dir(extract_dir, rel_prefix, origin_prefix, depth, is_root=True,
                                            ancestors=(extract_dir_real,), archive_no_crc=(fmt in TAR_MODES),
                                            archive_boundary_idx=archive_boundary_idx,
                                            tree_rel_prefix=tree_rel_prefix):
                    if item.ftype in ("image", "raw", "video"):
                        media_count += 1
                    yield item
            finally:
                _bydate_slice = (_bydate_before, len(self._deferred_bydate_roots))
                _tilde_slice = (_tilde_before, len(self._deferred_tilde_archives))
                _stray_slice = (_stray_before, len(self._deferred_stray_files))
                _anything_deferred = (_bydate_slice[1] > _bydate_slice[0]
                                      or _tilde_slice[1] > _tilde_slice[0]
                                      or _stray_slice[1] > _stray_slice[0])
                if _anything_deferred:
                    self._pending_cleanup_dirs.append(extract_dir)
                else:
                    cleanup_dir(extract_dir)

            if _anything_deferred:
                # Раунд 155 ревью [замечание]: медиасодержимое этого архива целиком (или
                # частично) ушло в отложенный проход -- media_count здесь ещё НЕ финальный
                # (голая дата/дамп-ветка внутри архива → _is_terminal_bydate_branch() → Фаза 3).
                # Логировать сейчас archive_no_media (RULES.md:449 -- «рекурсивно нет медиа»)
                # было бы ложью: рекурсия ещё не пройдена. Откладываем и сам статус --
                # _drain_deferred_phases() допишет его, досчитав медиа из отложенных срезов.
                self._pending_archive_status.append({
                    "display": _strip_trailing_arrow(full_display),
                    "letter": placement_letter,
                    "media_now": media_count,
                    "listing_media": info.media_count,
                    "bydate": _bydate_slice,
                    "tilde": _tilde_slice,
                    "stray": _stray_slice,
                })
            elif media_count == 0:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_no_media",
                                   letter=placement_letter)
            else:
                # SESSION-HANDOFF.txt п.6 (2026-08-05, боевой прогон): та же симметрия, что и у
                # archive_no_media выше (0==0 подавлен, живой репорт 2026-08-02) -- write_object_line()
                # (:3133) уже напечатал предварительное info.media_count ДО распаковки; печатать эту,
                # ПОДТВЕРЖДЁННУЮ распаковкой цифру снова -- повтор ровно тогда, когда она совпала с
                # предварительной. Печатаем только если распаковка выявила расхождение (media_count
                # отличается от того, что уже показано) -- иначе это новая информация, не повтор.
                self._log_archive(_strip_trailing_arrow(full_display), "archive_extracted",
                                   f"{media_count} медиафайлов", count=media_count,
                                   silent=media_count == info.media_count, letter=placement_letter)
        finally:
            self._archive_walk_depth -= 1
            if self._archive_walk_depth == 0 and self._transient_op_cb is not None:
                self._transient_op_cb(None)

# ============================================================================
# PROCESS  (from pipeline/process.py)
# ============================================================================


class ReadError(Exception):
    """Raised when the source file itself could not be read (lock, permission, disk I/O
    hiccup) after retries -- distinct from a file that reads fine but is corrupt/
    unrecognisable media (which stays a classification concern, not a read concern)."""


@dataclass
class SourceRecord:
    item: SourceItem
    sha256: str = None
    phash: str = None            # image: single; video: "|"-joined 3 frame hashes
    width: int = None
    height: int = None
    aspect: float = None
    duration: float = None
    bitrate: int = None
    exif_dt = None                # datetime or None
    exif_dt_source: str = None
    camera: str = None
    gps_lat: float = None
    gps_lon: float = None
    is_media: bool = True
    media_note: str = None
    broken: bool = False          # ffprobe failed / unreadable image / 0 bytes
    is_hidden: bool = False
    read_error: bool = False
    read_error_msg: str = None


DOS_ATTR_HIDDEN_BIT = 0x2  # FILE_ATTRIBUTE_HIDDEN


def is_hidden_path(read_path: str) -> bool:
    """Hidden-file detection. On native Windows (the portable target platform), the DOS
    hidden attribute is available directly from the filesystem via os.stat() --
    st_file_attributes only exists on Windows builds of Python. Falls back to the Unix
    dotfile convention when st_file_attributes is unavailable (dev-testing this script
    directly on Linux, where there is no DOS attribute to read)."""
    try:
        st = os.stat(winlong(read_path))
        if hasattr(st, "st_file_attributes"):
            return bool(st.st_file_attributes & DOS_ATTR_HIDDEN_BIT)
    except OSError:
        pass
    name = os.path.basename(read_path)
    return name.startswith(".")


def _volume_likely_gone(path: str) -> bool:
    """Cheap probe: is the drive containing `path` still reachable? Used inside the
    read-retry loop below to fail fast once the whole volume is gone (surprise physical
    disconnection) instead of blindly burning retries*delay seconds on every remaining
    file in the same already-listed directory -- a single missing/locked file is worth
    retrying, thousands of files failing because the drive itself vanished are not.

    REVIEW-HANDOFF.md, Раунд 41 [БЛОКЕР] 2: explicit ntpath, not the platform `os.path` --
    this parses a Windows drive letter (this program only ever runs on Windows in
    production, see README), so it must recognize "Z:\\..." regardless of which OS Python
    itself is running under. `os.path.splitdrive` silently aliases to posixpath's version
    on Linux, which never recognizes a drive letter at all -- unit tests run on
    ubuntu-latest in CI (see .github/workflows/ci.yml) would see this function permanently
    return False, unable to exercise the real logic at all."""
    drive = ntpath.splitdrive(ntpath.abspath(path))[0]
    if not drive:
        return False
    try:
        return not os.path.isdir(winlong(drive + "\\"))
    except OSError:
        return True


def sha256_file_with_retry(path: str, retries: int, delay: float, progress_cb=None) -> str:
    last_err = None
    for attempt in range(retries):
        try:
            # progress_cb передаём только когда он реально есть -- иначе зовём в один
            # позиционный аргумент, как раньше (тесты монкейпатчат sha256_file lambda p: ...)
            return sha256_file(path, progress_cb=progress_cb) if progress_cb else sha256_file(path)
        except OSError as e:
            last_err = e
            if attempt < retries - 1:
                if _volume_likely_gone(path):
                    break
                time.sleep(delay)
    raise ReadError(str(last_err))


def analyze_batch(items: list, retries: int = 3, retry_delay: float = 5.0,
                   small_image_px: int = 640, log=print, skip_hash: bool = False,
                   pool=None, cache: dict = None, tags_by_path: dict = None) -> list:
    """Phase 3: compute hashes/metadata/classification for a batch of SourceItem.
    Returns list of SourceRecord in the same order as items.
    A record with read_error=True means the file could not be read at all (locked /
    permission / disk I/O hiccup) after `retries` attempts -- the caller decides whether to
    defer it for an end-of-run retry or log it straight to unreadable.csv.

    skip_hash=True (используется ТОЛЬКО режимом analyze-quick, см. run_analyze()): не
    считать sha256 (chunked-чтение всего файла) и не считать pHash (imagehash.phash --
    решающая доля затрат для изображений; video_phash_3frames -- три отдельных ffmpeg-вызова
    на файл для видео). Экономит именно самое дорогое в конвейере, оставляя дешёвые вещи
    (exiftool-даты, размеры кадра, базовая проверка "открывается ли файл") включёнными --
    этого достаточно для быстрой метаданными-диагностики источника. Не даёт read_error-
    ретраев (те опираются на попытку sha256_file_with_retry) -- любой файл, который не
    открылся, в quick-режиме сразу считается broken, без отложенного повтора в конце
    прогона; это осознанное упрощение read-only диагностического режима, не влияющее на
    поведение обычной сборки (skip_hash по умолчанию False, здесь ничего не меняется).

    pool (опционально, Pool): если передан, точный sha256-дубль в пуле пропускает расчёт
    pHash -- decide() (см. его image/video-ветки) всегда проверяет pool.find_exact(sha256)
    раньше, чем читает rec.phash/rec.aspect, так что на этом пути результат заведомо не будет
    прочитан (ревизорская находка раунда 4, REVIEW-HANDOFF.md). Размер кадра для
    classify_image()/is_media всё равно нужен -- используется тот же дешёвый
    image_size_only(), что и skip_hash (заголовок без полного декода), вместо дорогого
    decode+DCT в image_phash_and_size()/video_phash_3frames(). Не влияет на итоговое решение:
    точный дубль уходит в skipped_present до того, как decide() вообще читает phash/aspect.

    cache (задача 7, SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"; опционально):
    {read_path: (size, mtime, sha256, phash, duration, width, height, bitrate)} -- тот же
    архивный кэш, что уже читает/пишет index_archive() (archive_cache -- собственный файл
    ВНУТРИ архива, __служебные_файлы\\archive_cache.db, см. archive_cache_db_path(), не
    work.db). Валиден на файл, только если (size,mtime) совпадают -- тот же принцип, что и в
    index_archive(). При
    попадании sha256/phash/duration/width/height/bitrate берутся из кэша вместо
    sha256_file_with_retry()/image_phash_and_size()/video_duration_and_resolution()/
    video_phash_3frames() -- ровно то дорогое, что skip_hash пропускает выше, только по
    другой причине (уже посчитано раньше для этого же файла, не "не нужно вовсе"). Только
    для чтения здесь -- запись в archive_cache остаётся за вызывающей стороной
    (run_analyze()), у analyze_batch() по архитектуре нет доступа к БД.

    tags_by_path (речь пользователя, "какие есть варианты сделать паспорт быстрее" --
    2026-08-02): если передан, использовать ГОТОВЫЙ словарь тегов вместо собственного вызова
    exiftool_batch() -- run_analyze() зовёт эту функцию с items=[один элемент] на каждой
    итерации обхода (см. её докстринг), а exiftool_batch() ничего не знает о том, что таких
    вызовов будет ещё тысячи -- каждый спавнит СВОЙ процесс exiftool на один файл, хотя сама
    exiftool_batch() умеет батчить по 200 через -@argfile (REVIEW-HANDOFF-ARCHIVE.md, раунд 4:
    ×28-36 накладных расходов на спавн против батча; раунд 19 ошибочно закрыл находку, проверив
    только что exiftool_batch() умеет батчить, не что вызывающий код реально это делает --
    им обоим НЕ был). None (по умолчанию) -- прежнее поведение, каждый вызов сам считает свои
    теги, ничего не меняется для вызовов из _run_impl() (:6024/:6106), которые сюда не заходят."""
    if tags_by_path is None:
        image_video_paths = [it.read_path for it in items if it.ftype in ("image", "raw", "video")]
        tags_by_path = exiftool_batch(image_video_paths, log=log) if image_video_paths else {}

    # B (2026-08-28, см. _check_pause_keypress()): опрос паузы по пробелу посреди хеширования
    # большого файла, а не только между файлами верхнего цикла.
    _pause_cb = (lambda: _check_pause_keypress(log=log)) if os.name == "nt" else None

    records = []
    for it in items:
        _check_pause_keypress(log=log)
        rec = SourceRecord(item=it)
        rec.is_hidden = is_hidden_path(it.read_path)

        if it.size == 0:
            rec.broken = True
            rec.is_media = False
            rec.media_note = "empty_file"
            records.append(rec)
            continue

        cached = cache.get(it.read_path) if cache else None
        cache_hit = bool(cached and cached[0] == it.size and abs(cached[1] - it.mtime) < 1e-6)
        # Речь пользователя, 2026-08-02: cached[8] -- exif_cached (см. SCHEMA/архива_cache_
        # db_path()). Отдельный от cache_hit флаг -- строка может быть известна кэшу по хешу
        # (старая, ДО этой правки, или из index_archive(), которая exif не пишет), но ещё ни
        # разу не проверена на EXIF -- тогда exif_cache_hit=False, ниже честно используются
        # tags (которые в этом случае РЕАЛЬНО были запрошены у exiftool, см.
        # _exif_cache_ready() в _tag_prefetch_pairs() -- та же проверка, тот же смысл).
        exif_cache_hit = bool(cache_hit and len(cached) > 8 and cached[8])

        if not skip_hash:
            if cache_hit:
                rec.sha256 = cached[2]
            else:
                try:
                    rec.sha256 = sha256_file_with_retry(it.read_path, retries, retry_delay,
                                                        progress_cb=_pause_cb)
                except ReadError as e:
                    rec.read_error = True
                    rec.read_error_msg = str(e)
                    records.append(rec)
                    continue

        tags = tags_by_path.get(it.read_path, {})

        if tags:
            dt, src = best_exif_datetime(tags)
            rec.exif_dt, rec.exif_dt_source = dt, src
            rec.camera = camera_from_tags(tags)
            rec.gps_lat, rec.gps_lon = gps_from_tags(tags)
        elif exif_cache_hit:
            # _tag_prefetch_pairs()/_exif_cache_ready() уже решили не звать exiftool вовсе для
            # этого file -- tags пуст не потому, что EXIF не нашёлся, а потому что мы его и не
            # спрашивали. Берём уже посчитанный ответ из archive_cache (индексы 9-13, см.
            # SCHEMA) -- те же значения, что и при живом запросе, просто бесплатно.
            rec.exif_dt = datetime.fromisoformat(cached[9]) if cached[9] else None
            rec.exif_dt_source = cached[10]
            rec.camera = cached[11]
            rec.gps_lat, rec.gps_lon = cached[12], cached[13]

        exact_dup = bool(pool is not None and rec.sha256 and pool.find_exact(rec.sha256))

        if it.ftype == "raw":
            # RAW formats (CR2/NEF/ARW/DNG) aren't decodable by Pillow; dedup for RAW is
            # SHA-256-only (see dedup.py), so no phash/aspect is needed. Always camera output.
            #
            # Речь пользователя, 2026-08-02: width/height для RAW и раньше уже кэшировались
            # (через ЭТИ ЖЕ cached[5]/cached[6] -- тот же общий индекс, что и у image/video
            # ниже, _seed_archive_cache() пишет их для ЛЮБОГО ftype), просто эта ветка не
            # проверяла cache_hit вовсе и всегда лезла в tags -- на exif_cache_hit=True (tags
            # пуст) без этой правки rec.width/height ошибочно стали бы None. Теперь -- тот же
            # cache_hit-паттерн, что уже есть у image/video чуть ниже.
            if cache_hit:
                rec.width, rec.height = cached[5], cached[6]
            else:
                try:
                    w = tags.get("ImageWidth")
                    h = tags.get("ImageHeight")
                    rec.width, rec.height = w, h
                except Exception:
                    pass
            rec.is_media = True

        elif it.ftype == "image":
            if cache_hit:
                ph, w, h = cached[3], cached[5], cached[6]
            elif skip_hash or exact_dup:
                w, h = image_size_only(it.read_path)
                ph = "-" if w is not None else None  # заглушка: не None -> "не broken"
            else:
                ph, w, h = image_phash_and_size(it.read_path)
            if ph is None:
                rec.broken = True
                rec.is_media = False
                rec.media_note = "unreadable_image"
                records.append(rec)
                continue
            # cache_hit -- значение уже известно бесплатно (из archive_cache), не нулим даже
            # при exact_dup -- в отличие от свежепосчитанного пути ниже, где null -- реальная
            # экономия (decide() всё равно не читает rec.phash после sha-совпадения, см.
            # докстринг выше), здесь эта экономия уже случилась до вызова этой функции.
            rec.phash, rec.width, rec.height = (ph if cache_hit else
                                                 (None if (skip_hash or exact_dup) else ph)), w, h
            rec.aspect = (w / h) if h else None
            is_media, note = classify_image(it.read_path, w, h, rec.camera, it.size,
                                                       small_image_px)
            rec.is_media, rec.media_note = is_media, note

        elif it.ftype == "video":
            if cache_hit:
                duration, w, h, bitrate = cached[4], cached[5], cached[6], cached[7]
            else:
                duration, w, h, bitrate = video_duration_and_resolution(it.read_path)
            if duration is None and w is None:
                rec.broken = True
                rec.is_media = False
                rec.media_note = "unreadable_video"
                records.append(rec)
                continue
            rec.duration, rec.width, rec.height, rec.bitrate = duration, w, h, bitrate
            if cache_hit:
                rec.phash = cached[3]
            elif not skip_hash and not exact_dup:
                frames = video_phash_3frames(it.read_path, duration or 1.0)
                rec.phash = "|".join(frames) if frames else None
            rec.is_media = True

        records.append(rec)

    return records

# ============================================================================
# DEDUP  (from pipeline/dedup.py)
# ============================================================================


@dataclass
class PoolEntry:
    sha256: str
    ftype: str                 # image / video / raw
    dest_path: str
    size: int
    aspect: float = None
    width: int = None
    height: int = None
    phash: str = None          # image: single hex phash; video: "|"-joined 3 frame hashes
    duration: float = None
    has_camera: bool = False
    bitrate: int = None
    phash_int: int = None      # image only -- see hamming_int()/Pool.add()


def _aspect_bucket(aspect: float) -> int:
    return round(aspect * 50)  # ~2% grid


def _phash_to_int(phash_hex) -> int:
    try:
        return int(phash_hex, 16)
    except (TypeError, ValueError):
        return None


def hamming_int(a: int, b: int) -> int:
    """Same result as hamming(hex, hex) (XOR-and-count-set-bits on the same underlying bit
    pattern, verified equal for every real phash this codebase generates) but on pre-parsed
    ints instead of re-parsing both hex strings through imagehash.hex_to_hash() on every
    call -- ~340x cheaper per call. Used only in Pool's near-dup image lookup, whose fallback
    branch scans the whole pool for every file that misses the aspect-bucket match (i.e. most
    files) -- second-review finding, 2026-07-17: at real family-archive scale (tens of
    thousands of photos) the O(n^2) call count made the re-parsing cost alone dominate the
    whole run (measured ~48us/call before this fix -> ~17 hours of hamming() calls alone at
    50k photos; ~0.14us/call after -> ~3 minutes). hamming(str, str) is left
    as-is for find_near_dup_video()'s frame-hash comparisons and existing tests -- only this
    codepath was hot enough to matter."""
    if a is None or b is None:
        return 999
    return (a ^ b).bit_count()


class Pool:
    def __init__(self):
        self.by_sha = {}
        self.by_aspect_bucket = defaultdict(list)
        self.by_duration_bucket = defaultdict(list)

    def add(self, entry: PoolEntry):
        self.by_sha[entry.sha256] = entry
        if entry.ftype in ("image",) and entry.aspect and entry.phash:
            entry.phash_int = _phash_to_int(entry.phash)
            self.by_aspect_bucket[_aspect_bucket(entry.aspect)].append(entry)
        elif entry.ftype == "video" and entry.duration is not None:
            self.by_duration_bucket[int(entry.duration)].append(entry)

    def find_exact(self, sha256: str):
        return self.by_sha.get(sha256)

    def find_near_dup_image(self, aspect: float, phash: str, threshold=6):
        """Returns (entry, aspect_matches, hamming_distance) for the best-quality near-dup
        within threshold, or (None, None, None). Among all cluster matches, the entry is
        chosen by _quality_key (same criterion as image_is_strictly_better) rather than the
        nearest by Hamming distance -- otherwise appended_better/appended_near_dup could be
        decided against the wrong cluster member. The distance of the chosen entry is still
        surfaced (p.5.7) so the caller can log how close the match was, now that near-dups
        are appended, not skipped. Uses hamming_int()/cached PoolEntry.phash_int, not the
        string-based hamming() -- see hamming_int()'s docstring for why."""
        query_int = _phash_to_int(phash)
        bucket = _aspect_bucket(aspect)
        candidates = []
        for b in (bucket - 1, bucket, bucket + 1):
            for entry in self.by_aspect_bucket.get(b, []):
                d = hamming_int(entry.phash_int, query_int)
                if d <= threshold:
                    rel_diff = abs(entry.aspect - aspect) / max(entry.aspect, 1e-6)
                    if rel_diff <= 0.02:
                        candidates.append((entry, d))
        if candidates:
            best, best_dist = max(candidates, key=lambda pair: _quality_key(pair[0]))
            return best, True, best_dist

        # Fallback: a crop can have a different aspect (so it never lands in the buckets
        # above) but a similar phash. Scan every image entry in the pool -- by_aspect_bucket
        # already holds all of them, no separate phash-prefix index needed. This is the hot
        # O(n) loop hamming_int() exists for -- it runs once per file that has no aspect-
        # bucket match, i.e. most files in a real run.
        candidates = []
        for entry in itertools.chain(*self.by_aspect_bucket.values()):
            d = hamming_int(entry.phash_int, query_int)
            if d <= threshold:
                candidates.append((entry, d))
        if candidates:
            best, best_dist = max(candidates, key=lambda pair: _quality_key(pair[0]))
            return best, False, best_dist
        return None, None, None

    def find_near_dup_video(self, duration: float, frame_hashes, threshold=6, max_delta=1.0):
        """Returns (entry, hamming_distance) for the closest near-dup, or (None, None) --
        distance is the worst (max) per-frame Hamming distance among the matched frames,
        i.e. the one closest to the threshold (see p.5.7 note on find_near_dup_image)."""
        if not frame_hashes:
            return None, None
        buckets = set()
        base = int(duration) if duration else 0
        for delta in (-1, 0, 1):
            buckets.add(base + delta)
        for b in buckets:
            for entry in self.by_duration_bucket.get(b, []):
                if entry.duration is None or abs(entry.duration - duration) > max_delta:
                    continue
                entry_hashes = entry.phash.split("|") if entry.phash else []
                if video_hashes_match(entry_hashes, frame_hashes, threshold):
                    n = min(len(entry_hashes), len(frame_hashes))
                    max_dist = max((hamming(entry_hashes[i], frame_hashes[i]) for i in range(n)), default=0)
                    return entry, max_dist
        return None, None


def _quality_key(entry: PoolEntry):
    """Ordering used to pick the best of several near-dup candidates: pixel area, then file
    size, then EXIF camera presence -- same criterion as image_is_strictly_better below."""
    return ((entry.width or 0) * (entry.height or 0), entry.size, entry.has_camera)


def image_is_strictly_better(candidate: PoolEntry, existing: PoolEntry) -> bool:
    return _quality_key(candidate) > _quality_key(existing)


def video_is_strictly_better(candidate: PoolEntry, existing: PoolEntry) -> bool:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    if cand_px != exist_px:
        return cand_px > exist_px
    cand_br = candidate.bitrate or 0
    exist_br = existing.bitrate or 0
    if cand_br != exist_br:
        return cand_br > exist_br
    if candidate.size != existing.size:
        return candidate.size > existing.size
    return False


def _pct_vs(candidate_value, existing_value) -> str:
    """p.5.3б: relative diff for a [DEBUG] near_dup criterion string, e.g. 'area+12%'.
    existing_value==0 has no meaningful percentage -- report the raw delta instead."""
    if not existing_value:
        return f"{candidate_value - existing_value:+d}"
    return f"{(candidate_value - existing_value) / existing_value * 100:+.0f}%"


def _image_compare_debug(candidate: PoolEntry, existing: PoolEntry) -> str:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    return (f"image_is_strictly_better(area{_pct_vs(cand_px, exist_px)}, "
            f"size{_pct_vs(candidate.size, existing.size)}, "
            f"exif_camera={str(bool(candidate.has_camera)).lower()})")


def _video_compare_debug(candidate: PoolEntry, existing: PoolEntry) -> str:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    return (f"video_is_strictly_better(area{_pct_vs(cand_px, exist_px)}, "
            f"bitrate{_pct_vs(candidate.bitrate or 0, existing.bitrate or 0)}, "
            f"size{_pct_vs(candidate.size, existing.size)})")

# ============================================================================
# DECIDE  (from pipeline/decide.py)
# ============================================================================


@dataclass
class Decision:
    decision: str
    matched_dest: str = None
    note: str = None
    debug_detail: str = None  # p.5.3б: criterion string for [DEBUG] near_dup lines -- pure
                               # data (decide() stays a pure function, no logging inside it)
    hamming: int = None  # near_dup_edges.csv (PROMPT_archive_report.md, 1.2б): same `dist`
                          # already computed by find_near_dup_image/find_near_dup_video, just
                          # not previously threaded out past the near-dup `note` text


def decide(pool: Pool, rec: SourceRecord, mirror_raw: bool = True) -> Decision:
    if rec.broken or not rec.is_media:
        return Decision("disputed", note=rec.media_note or "not_media")

    if rec.item.ftype == "raw":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")
        has_jpeg = bool(rec.item.sibling_path)
        # MIRROR_RAW управляет только избыточным RAW (есть парный JPEG). Одинокий RAW --
        # единственный носитель кадра -- мирроится ВСЕГДА независимо от флага (см. RULES.md).
        if has_jpeg and not mirror_raw:
            return Decision("raw_skipped", note="raw_skipped_has_jpeg")
        if not has_jpeg and not mirror_raw:
            return Decision("raw_mirrored", note="raw_lone_mirrored")
        return Decision("raw_mirrored", note="raw_with_jpeg" if has_jpeg else "raw_without_jpeg")

    if rec.item.ftype == "image":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")

        if not rec.phash or not rec.aspect:
            return Decision("appended_uncertain", note="no_phash_available")

        entry, aspect_ok, dist = pool.find_near_dup_image(rec.aspect, rec.phash)
        if entry is None:
            return Decision("appended_new")

        if aspect_ok:
            candidate = PoolEntry(
                sha256=rec.sha256, ftype="image", dest_path=None, size=rec.item.size,
                aspect=rec.aspect, width=rec.width, height=rec.height, phash=rec.phash,
                has_camera=bool(rec.camera),
            )
            detail = _image_compare_debug(candidate, entry)
            if image_is_strictly_better(candidate, entry):
                return Decision("appended_better", matched_dest=entry.dest_path, note="better_quality_appended",
                                 debug_detail=detail, hamming=dist)
            # p.5.7: near-dup no longer excludes the file from the archive -- a burst-shot
            # sequence can have one frame that matters (a bird mid-flight) that perceptual
            # hashing can't tell apart from its neighbors on technical metrics alone. Append
            # both, log which existing file it's close to and by how much, and let a human
            # clean up duplicates later if they want to (source: user).
            return Decision("appended_near_dup", matched_dest=entry.dest_path,
                             note=f"near_dup_of={os.path.basename(entry.dest_path)}_hamming={dist}",
                             debug_detail=detail, hamming=dist)

        return Decision("appended_crop", matched_dest=entry.dest_path, note="kept_both_possible_crop",
                         hamming=dist)

    if rec.item.ftype == "video":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")

        if not rec.phash:
            return Decision("appended_uncertain", note="no_phash_available")

        frame_hashes = rec.phash.split("|")
        entry, dist = pool.find_near_dup_video(rec.duration or 0.0, frame_hashes)
        if entry is None:
            return Decision("appended_new")

        candidate = PoolEntry(
            sha256=rec.sha256, ftype="video", dest_path=None, size=rec.item.size,
            width=rec.width, height=rec.height, duration=rec.duration, bitrate=rec.bitrate,
        )
        detail = _video_compare_debug(candidate, entry)
        if video_is_strictly_better(candidate, entry):
            return Decision("appended_better", matched_dest=entry.dest_path, note="better_quality_appended",
                             debug_detail=detail, hamming=dist)
        # p.5.7: same reasoning as the image branch above -- append instead of skip.
        return Decision("appended_near_dup", matched_dest=entry.dest_path,
                         note=f"near_dup_of={os.path.basename(entry.dest_path)}_hamming={dist}",
                         debug_detail=detail, hamming=dist)

    return Decision("appended_uncertain", note="unknown_type")

# ============================================================================
# DATES  (from pipeline/dates.py)
# ============================================================================


_MIN_YEAR = 1900  # семейный архив может включать оцифрованные плёночные фото старше 1990


def _valid(y, mo=1, d=1):
    if y < _MIN_YEAR or y > datetime.now().year:
        return False
    try:
        datetime(y, mo, d)
        return True
    except ValueError:
        return False


# strict filename date patterns (counters like IMG_1234 are intentionally NOT matched)
_FNAME_PATTERNS = [
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"IMG[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"Screenshot_(\d{4})-(\d{2})-(\d{2})"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"PXL[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"VID[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{4})_(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), 1)),
]

_FOLDER_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")  # 1900..2099; _valid() сузит верх/низ


def date_from_filename(name: str):
    base = os.path.splitext(name)[0]
    for rx, extract in _FNAME_PATTERNS:
        m = rx.search(base)
        if m:
            try:
                y, mo, d = extract(m)
                if _valid(y, mo, d):
                    return datetime(y, mo, d), "filename_pattern"
            except ValueError:
                continue
    return None, None


def date_from_folder_name(rel_path: str):
    """Year found in an ancestor folder segment of rel_path; year must be valid."""
    parts = rel_path.split("/")[:-1]
    for part in reversed(parts):
        m = _FOLDER_YEAR_RE.search(part)
        if m:
            y = int(m.group(1))
            if _valid(y):
                return datetime(y, 1, 1), "folder_name_year"
    return None, None


def mtime_is_copy_artifact(mtimes: list, window_seconds=5) -> bool:
    """Many files in a folder sharing an identical/narrow mtime window => copy event, not a
    reliable date signal."""
    if len(mtimes) < 3:
        return False
    mtimes = sorted(mtimes)
    span = mtimes[-1] - mtimes[0]
    return span <= window_seconds


def folder_cluster_median(tier_ab_dates: list):
    if not tier_ab_dates:
        return None
    ts = sorted(d.timestamp() for d in tier_ab_dates)
    med = statistics.median(ts)
    return datetime.fromtimestamp(med)


class DateContext:
    """Accumulates per-directory evidence across the run so later files in the
    same folder can borrow tier A/B dates from earlier siblings (folder-cluster
    inference) and so copy-artifact mtimes can be recognised."""

    def __init__(self):
        self.dir_tier_ab_dates = defaultdict(list)
        self.dir_mtimes = defaultdict(list)

    def record(self, dirname, dt, tier, mtime):
        if tier in ("A", "B"):
            self.dir_tier_ab_dates[dirname].append(dt)
        self.dir_mtimes[dirname].append(mtime)


def resolve_date(ctx: DateContext, rel_path: str, mtime: float, exif_dt=None, exif_source=None, *,
                  use_folder_name_date: bool = True):
    """Phase 4.5: returns (date_value, tier, confidence, evidence, precision).
    precision is 'day' (full date known) or 'year' (only the year is reliable,
    e.g. a bare year found in a folder name) -> routes to the month-unknown bucket.

    use_folder_name_date=False (живой репорт пользователя, 2026-08-01, "Паспорт архива"):
    run_passport() переиспользует этот же конвейер с cfg.source=TARGET -- folder-based
    вывод даты (date_from_folder_name() ниже) на обычном SOURCE честный независимый сигнал
    (папка, которую НАЗВАЛ пользователь), но на TARGET сами ByDate-папки называет программа
    (build_bydate_dest_dir()) -- их имя ("2024-07-15 Москва") УЖЕ является выводом этой же
    самой функции с прошлого прогона (Tier C/D, низкая уверенность), не новым независимым
    доказательством. Без этого флага паспорт на втором проходе считывал бы собственную
    разметку архива как будто это свежее подтверждение и завышал Tier до B ("средняя
    уверенность") для файлов, у которых её на самом деле нет -- количество "дата определена
    лишь приблизительно" в паспорте оказывалось в разы меньше, чем должно быть."""
    dirname = os.path.dirname(rel_path)
    name = os.path.basename(rel_path)

    if exif_dt:
        ctx.record(dirname, exif_dt, "A", mtime)
        return exif_dt, "A", "high", exif_source, "day"

    dt, ev = date_from_filename(name)
    if dt:
        ctx.record(dirname, dt, "B", mtime)
        return dt, "B", "medium", ev, "day"

    dt, ev = date_from_folder_name(rel_path) if use_folder_name_date else (None, None)
    if dt:
        ctx.record(dirname, dt, "B", mtime)
        return dt, "B", "medium", ev, "year"

    neighbors = ctx.dir_tier_ab_dates.get(dirname, [])
    if neighbors:
        med = folder_cluster_median(neighbors)
        ctx.record(dirname, med, "C", mtime)
        return med, "C", "low", "inferred_from_folder_cluster", "day"

    sibling_mtimes = ctx.dir_mtimes.get(dirname, [])
    if not mtime_is_copy_artifact(sibling_mtimes + [mtime]):
        dt = datetime.fromtimestamp(mtime)
        ctx.record(dirname, dt, "C", mtime)
        return dt, "C", "low", "mtime", "day"

    ctx.record(dirname, None, "D", mtime)
    return None, "D", "none", "no_signal", None

# ============================================================================
# PLACEMENT  (from pipeline/placement.py)
# ============================================================================


def find_album(rel_path: str, archive_boundary_idx: int = None, *,
                dump_names=None, dump_prefixes=None, bydate_only: bool = False):
    """2026-08-08 (альбомный редизайн, по прямому запросу пользователя -- "чем проще, тем
    лучше для пользователя, никаких схлопываний, никаких объединений альбомов"): один-единственный
    вопрос решает всё -- отравлен путь служебным сегментом или нет.

    Идём по КАЖДОМУ сегменту пути от корня SOURCE (папки и архивы -- на равных, см. ниже про
    archive_boundary_idx). Если ЛЮБОЙ сегмент -- служебный (is_dump_segment(), без каких-либо
    исключений по позиции) -- путь отравлен целиком, файл падает в ByDate, как будто альбома не
    было вовсе. Если ни один сегмент не служебный -- ВЕСЬ путь целиком становится альбомным
    деревом в Albums\\, один в один, и КАЖДАЯ папка на этом пути -- свой собственный альбом
    (папка1\\папка2\\папка3 -- три разных альбома, вложенных друг в друга; см. RULES.md).

    Returns (album_name, subpath_segments, album_prefix) or (None, None, None).
    album_name/album_prefix -- ВЕРХНИЙ сегмент (segments[0]) и его же имя (для обратной
    совместимости сигнатуры и для build_album_dest_dir()) -- subpath_segments -- всё, что
    глубже; album_prefix + subpath_segments вместе восстанавливают исходный полный путь.
    Идентичность КАЖДОЙ отдельной папки дерева для целей счёта -- ответственность вызывающей
    стороны (см. n_albums_detected), не этой функции.

    dump_names/dump_prefixes: forwarded as-is to every is_dump_segment() call below (see that
    function's docstring) -- production call sites pass cfg.dump_segment_names_lower/
    cfg.dump_segment_prefixes_tuple, bare calls (tests) fall back to module defaults.

    bydate_only (2026-08-11, по запросу пользователя -- "D:" в dump_segment_names, см.
    _DRIVE_MARKER_RE/Config.source_bydate_only): вернуть (None, None, None) безусловно, БЕЗ
    прохода по сегментам вообще -- как если бы КАЖДЫЙ сегмент пути был отравлен, для целого
    SOURCE, не для одной ветки. Production call sites передают cfg.source_bydate_only.

    АРХИВ = ЭКВИВАЛЕНТ ПАПКИ (2026-08-03, уточнено 2026-08-08): archive_boundary_idx -- индекс
    сегмента с именем архива (без расширения) для файла ВНУТРИ zip/rar/7z/tar (None для
    обычного файла на диске, см. SourceItem.archive_boundary_idx). Никакого отдельного кода
    для архивов больше не нужно -- имя архива это просто ОДИН ИЗ segments (на своей позиции
    archive_boundary_idx), проверяется той же is_dump_segment(), что и любая папка, в том же
    едином проходе ниже. Если оно служебное -- отравляет всё, что внутри архива (и только
    внутри него -- у файлов-соседей архива в их СОБСТВЕННОМ rel_path этого сегмента вообще
    нет, они его не "видят"). Если не служебное -- участвует в зеркалировании на равных с
    папками.
    """
    if bydate_only:
        return None, None, None
    segments = rel_path.split("/")[:-1]
    if not segments:
        return None, None, None
    for seg in segments:
        if is_dump_segment(seg, dump_names=dump_names, dump_prefixes=dump_prefixes):
            return None, None, None
    return segments[0], segments[1:], segments[0]


def _is_terminal_bydate_branch(segments, archive_boundary_idx: int = None, *,
                                dump_names=None, dump_prefixes=None) -> bool:
    """SourceWalker two-phase traversal (2026-08-03): used ONLY to decide whether to keep
    descending into a folder/archive during Фаза 1, or defer it whole for Фаза 2/3 -- NOT a
    replacement for find_album() (which stays the single source of truth for final
    placement). `segments` is the path from SOURCE's root down to and including the
    folder/archive currently being looked at (no trailing filename, unlike find_album()).

    2026-08-08 (альбомный редизайн): с уходом позиционных исключений find_album()'s поиск
    стал безусловным (любой служебный сегмент отравляет ВСЁ, независимо от того, где он
    встретился) -- значит и здесь больше нет неоднозначного "ещё не решено, ищем глубже"
    промежуточного состояния: если СРЕДИ УЖЕ ПРОЙДЕННЫХ сегментов (включая текущую
    папку/архив) есть хотя бы один служебный -- результат уже окончательный (True), что бы ни
    нашлось глубже. archive_boundary_idx не нужен даже как отдельная проверка -- имя архива
    это просто один из segments на своей позиции, участвует в том же единообразном проходе."""
    return any(is_dump_segment(seg, dump_names=dump_names, dump_prefixes=dump_prefixes)
               for seg in segments)


# 2026-08-29, прямое решение пользователя: маркер-файл `__ПРОПУЩЕННЫЕ_ДУБЛИ.txt` (по одному в
# каждой папке альбома, где был пропущен внутриветочный дубль) удалён целиком. Те же данные --
# по каждому пропущенному файлу, с чем совпал -- полностью есть в `skipped.csv` и в детализации
# `report_detail.xlsx` (строки "дубликат"); отдельный `.txt` в дереве архива признан лишним
# засорением. `run_logs.skipped(...)` (CSV) и статистика `skipped_present` не затронуты.


_place_cache: dict = {}  # (rounded_lat, rounded_lon, home_country) -> place string or None


def place_for_gps(lat, lon, home_country="RU"):
    """REVIEW-HANDOFF.md, Раунд 42 [БЛОКЕР] 1 (продолжение Раунда 41): реальная причина
    ~130-800 мс/вызов у `rg.search()` -- не построение KD-дерева и не размер датасета, а
    режим `mode=2` (по умолчанию в библиотеке) -- `cKDTree_MP.pquery()` на КАЖДЫЙ вызов
    спавнит свежий пул `multiprocessing.Process` (по числу ядер) и джойнит его; на Windows
    (`spawn`, не `fork()`) это и есть весь наблюдаемый расход. Измерено
    эмпирически (не на слово): `mode=1` (однопоточный `scipy.spatial.cKDTree.query()`,
    без spawn'а процессов) на той же машине -- ~0.01 мс/вызов, единственный
    некомпенсируемый расход остаётся на самое первое обращение в процессе (загрузка
    ~145k городов + построение дерева, ~0.3 с, происходит один раз благодаря `@singleton`
    в самой библиотеке, см. `reverse_geocoder/__init__.py`). Раунд 41 [БЛОКЕР] 1 был
    закрыт кешем по округлённым координатам, потому что диагноз тогда был "вызов дорог
    по своей природе, батчить нельзя (SOURCE обходится по одному элементу -- см. `run_for_
    source()`)" -- верным было только следствие (нельзя батчить), не причина: сам вызов
    дорог не по своей природе, а из-за неверного режима, поэтому решение
    "не батчить, а кешировать" боролось не с той стоимостью и не спасало маршруты без
    пространственной кластеризации GPS (поход/дорога/шоппинг-улица -- см. таблицу
    Раунда 42). `mode=1` устраняет стоимость для ЛЮБОГО паттерна маршрута без изменения
    архитектуры обхода источника.

    Кеш по (lat, lon), округлённым до 2 знаков (~1.1 км на экваторе), оставлен как есть --
    он больше не критичен для устранения блокера, но не вреден (экономит даже те ~0.01 мс
    на точных повторах) и уже покрыт тестами (`tests/test_place_cache.py`)."""
    if lat is None or lon is None:
        return None
    cache_key = (round(lat, 2), round(lon, 2), home_country)
    if cache_key in _place_cache:
        return _place_cache[cache_key]
    try:
        import reverse_geocoder as rg
        # 2026-07-11 live-run finding: verbose=True (its default) prints "Loading formatted
        # geocoded file..." etc. straight to stdout on its one-time lazy-load, with no
        # coordination with our own tqdm progress bar (writes to stderr) -- the two interleave
        # mid-line on a real console, producing garbled output like "обработка источника: :
        # 7файл [00:02, 3.62файл/s]Loading formatted geocoded file...". Silencing it here is a
        # supported library parameter, not a workaround.
        # mode=1 -- см. докстринг функции: mode=2 (умолчание библиотеки) спавнит процессы
        # на каждый вызов, mode=1 использует тот же закешированный (`@singleton`) экземпляр
        # без multiprocessing, ~80000x быстрее на измерении Раунда 42.
        result = rg.search([(lat, lon)], mode=1, verbose=False)
        if not result:
            _place_cache[cache_key] = None
            return None
        r = result[0]
        city = r.get("name")
        cc = r.get("cc")
        if not city:
            _place_cache[cache_key] = None
            return None
        place = city if cc == home_country else f"{city}, {cc}"
        _place_cache[cache_key] = place
        return place
    except Exception:
        # Not cached -- a transient failure (e.g. the lazy geocoded-file load hiccups) should
        # not permanently poison this bucket for every later file that lands in it.
        return None


_FS_MAX_COMPONENT_BYTES = 255  # hard filesystem limit per path component (ext4 etc.)

_WINDOWS_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), "COM¹", "COM²", "COM³",
    *(f"LPT{i}" for i in range(1, 10)), "LPT¹", "LPT²", "LPT³",
}


def sanitize_windows_component(name: str) -> str:
    """Делает один сегмент пути (имя файла или папки) легальным на Windows/NTFS, независимо
    от того, откуда он взялся -- имя альбома из папки источника, город из reverse_geocoder,
    или файл, распакованный из архива (zip/tar могли быть собраны не на Windows и содержать
    символы вроде ':' или '?', которые NTFS не примет). На native Windows нет отдельного
    ro-контура, который раньше физически не давал таким именам попасть в дерево TARGET --
    единственная защита теперь в том, что мы сами никогда не формируем недопустимый путь.
    Не трогает разделители пути -- вызывается только на уже разбитых сегментах, никогда на
    целом пути."""
    if not name:
        return "_"
    cleaned = _WINDOWS_INVALID_CHARS_RE.sub("_", name)
    cleaned = cleaned.rstrip(" .")  # Windows отбрасывает хвостовые точки/пробелы у компонента
    if not cleaned:
        cleaned = "_"
    base, ext = os.path.splitext(cleaned)
    if base.upper() in _WINDOWS_RESERVED_NAMES:  # "CON.jpg" тоже зарезервировано, не только "CON"
        cleaned = base + "_" + ext
    return cleaned


def truncate_segment(name: str, max_len: int) -> str:
    """Truncate a single path segment to at most max_len characters AND, more importantly,
    at most 255 UTF-8 bytes -- multi-byte scripts (Cyrillic etc.) hit the filesystem's
    per-component byte limit long before the character-count budget, which caused real
    ENAMETOOLONG failures on long Russian filenames. Also sanitizes Windows-illegal
    characters/reserved device names first (sanitize, then truncate: replacing an invalid
    char with '_' never changes byte length, so the budget math below stays correct)."""
    name = sanitize_windows_component(name)
    byte_budget = min(max_len, _FS_MAX_COMPONENT_BYTES)
    if len(name) <= max_len and len(name.encode("utf-8")) <= byte_budget:
        return name
    root, ext = os.path.splitext(name)
    ext_bytes = ext.encode("utf-8", "ignore")
    root_byte_budget = max(1, byte_budget - len(ext_bytes))
    root_bytes = root.encode("utf-8", "ignore")[:root_byte_budget]
    while root_bytes and (root_bytes[-1] & 0xC0) == 0x80:  # don't split a multi-byte char
        root_bytes = root_bytes[:-1]
    return root_bytes.decode("utf-8", "ignore") + ext


def safe_mirror_dir(root: str, rel_dir: str, max_segment_len: int = 100) -> str:
    """Build a directory path mirroring a (possibly very long / deeply nested) source
    tree, truncating each segment so the result stays filesystem-safe (avoids
    ENAMETOOLONG on real-world Windows paths with long Cyrillic folder names)."""
    if not rel_dir or rel_dir in (".", ""):
        return root
    parts = [truncate_segment(p, max_segment_len) for p in rel_dir.split("/") if p]
    return os.path.join(root, *parts) if parts else root


def build_album_dest_dir(albums_root: str, album_prefix: str, subpath: list) -> str:
    """album_prefix -- "/"-разделённый путь (может быть многосегментным), НЕ голое имя --
    но сам по себе не обязательно полный путь от корня SOURCE: find_album() возвращает в нём
    только segments[0] (см. её докстринг), а остаток пути несёт subpath. Полный путь
    восстанавливается ЗДЕСЬ, склейкой album_prefix + subpath -- ни один вызывающий код не
    должен полагаться на album_prefix как на самодостаточный полный путь (Раунд 77 ревью,
    REVIEW-HANDOFF.md, ПРИДИРКА 1 -- прежняя формулировка этого докстринга была неточной).
    Каждый сегмент обеих частей санитизируется отдельно."""
    album_segments = album_prefix.split("/")
    parts = [albums_root] + [sanitize_windows_component(p) for p in album_segments + list(subpath)]
    return os.path.join(*parts)


def build_bydate_dest_dir(bydate_root: str, date_value, precision: str, place: str,
                           granularity: str = "day") -> str:
    """granularity: day (по умолчанию, текущее поведение) | month | year | flat.
    precision=='year' (сама дата известна только с точностью до года) всегда даёт
    month-unknown-корзину независимо от granularity -- сузить её до month/day нечем.
    place (город из reverse_geocoder) санитизируется перед склейкой в имя папки -- он не
    сегмент пути сам по себе, а часть строки, поэтому чистить нужно ДО f-string, а не
    после (санитайзер не трогает пробелы, только Windows-запрещённые символы)."""
    place = sanitize_windows_component(place) if place else place
    if granularity == "flat":
        return bydate_root
    year = date_value.year
    if granularity == "year":
        return os.path.join(bydate_root, str(year))
    if precision == "year":
        return os.path.join(bydate_root, str(year), f"{year}-00 month-unknown{DUMP_TAG}")
    if granularity == "month":
        month_folder = date_value.strftime("%Y-%m")
        if place:
            month_folder = f"{month_folder} {place}"
        return os.path.join(bydate_root, str(year), f"{month_folder}{DUMP_TAG}")
    day_folder = date_value.strftime("%Y-%m-%d")
    if place:
        day_folder = f"{day_folder} {place}"
    return os.path.join(bydate_root, str(year), f"{day_folder}{DUMP_TAG}")


def build_mirror_dest_dir(root: str, rel_dir: str) -> str:
    if not rel_dir:
        return root
    parts = [sanitize_windows_component(p) for p in rel_dir.split("/") if p]
    return os.path.join(root, *parts) if parts else root


def raw_dest_dir(item: "SourceItem", rec: "SourceRecord", cfg: "Config",
                  dest_path_by_read_path: dict, date_ctx: "DateContext") -> str:
    """Папка назначения для RAW-кандидата (те, что реально мирроятся -- см. decide()),
    в зависимости от RAW_LAYOUT (photoarchive_config.yaml, см. RULES.md):

    mirror (по умолчанию) -- отдельный корень {TARGET}\\RAW\\, зеркалящий структуру
    основного архива (RAW\\Albums\\..., RAW\\ByDate\\YYYY\\...). Основной архив остаётся
    чистой галереей, все RAW сносятся одной отдельной папкой.

    sibling -- RAW кладётся в подпапку RAW\\ РЯДОМ с тем местом, куда лёг (или лёг бы) его
    JPEG-партнёр: Albums\\Море 2015\\RAW\\IMG.CR2, ByDate\\2019\\2019-07-15 Москва
    [PhotoArchive]\\RAW\\IMG.CR2. Удобно для сценария "фотограф хранит RAW при кадре";
    удаление альбома удаляет и его RAW заодно. Одинокий RAW (нет парного JPEG) кладётся в
    RAW-подпапку той папки,
    куда лёг бы его JPEG по обычной логике размещения (альбом/дата) -- см. RULES.md,
    правило "одинокий RAW спасается всегда" не зависит от RAW_LAYOUT.

    В обоих случаях RAW и JPEG никогда не оказываются в одной папке (гарантируется самим
    построением путей), и оба варианта одинаково участвуют в дедупе через общий pool.
    """
    sibling_dest = dest_path_by_read_path.get(item.sibling_path) if item.sibling_path else None

    if cfg.raw_layout == "sibling":
        if sibling_dest:
            return os.path.join(os.path.dirname(sibling_dest), "RAW")
        album, subpath, album_prefix = find_album(item.rel_path, item.archive_boundary_idx,
                                                    dump_names=cfg.dump_segment_names_lower,
                                                    dump_prefixes=cfg.dump_segment_prefixes_tuple,
                                                    bydate_only=cfg.source_bydate_only)
        if album:
            return os.path.join(build_album_dest_dir(cfg.albums_root, album_prefix, subpath), "RAW")
        date_value, tier, conf, evidence, precision = resolve_date(
            date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
        if date_value is None:
            return os.path.join(safe_mirror_dir(cfg.undated_root, os.path.dirname(item.rel_path)), "RAW")
        return os.path.join(
            build_bydate_dest_dir(cfg.bydate_root, date_value, precision, None, cfg.bydate_granularity),
            "RAW",
        )

    # mirror (по умолчанию) -- прежняя логика, без изменений
    if sibling_dest:
        for src_root, dst_root in ((cfg.albums_root, os.path.join(cfg.raw_root, "Albums")),
                                    (cfg.bydate_root, os.path.join(cfg.raw_root, "ByDate"))):
            if sibling_dest.startswith(src_root + os.sep):
                rel_dir = os.path.dirname(os.path.relpath(sibling_dest, src_root))
                return os.path.join(dst_root, rel_dir) if rel_dir != "." else dst_root
        return os.path.join(cfg.raw_root, "ByDate", "_misc")
    album, subpath, album_prefix = find_album(item.rel_path, item.archive_boundary_idx,
                                                dump_names=cfg.dump_segment_names_lower,
                                                dump_prefixes=cfg.dump_segment_prefixes_tuple,
                                                bydate_only=cfg.source_bydate_only)
    if album:
        return build_album_dest_dir(os.path.join(cfg.raw_root, "Albums"), album_prefix, subpath)
    date_value, tier, conf, evidence, precision = resolve_date(
        date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
    if date_value is None:
        # Симметрично основному дереву: RAW_ROOT/ByDate/0000-undated/<дерево источника> --
        # см. RULES.md, тот же принцип, что и "0000-undated" в основном ByDate.
        return safe_mirror_dir(os.path.join(cfg.raw_root, "ByDate", "0000-undated"),
                                os.path.dirname(item.rel_path))
    return build_bydate_dest_dir(os.path.join(cfg.raw_root, "ByDate"), date_value, precision, None,
                                  cfg.bydate_granularity)


def resolve_dest_path(dest_dir: str, filename: str, candidate_sha256: str, sha256_of_file_fn, max_len: int,
                       stats: dict = None):
    """Handle name collisions: identical content at an occupied name => duplicate (skip);
    otherwise append _1, _2, ... Returns (final_path, is_duplicate).
    stats (p.5.3а, optional): if given, counts a "warn_path_truncated" occurrence whenever
    the filename actually had to be shortened -- surfaced in summary.txt.

    Живая находка пользователя (2026-08-09): dest_dir раньше создавался здесь БЕЗУСЛОВНО
    (_makedirs_iterative()), включая dry_run=True -- эта функция зовётся из _process_record()
    ДО проверки `if not is_dup and not cfg.dry_run:` (нужно решить, дубль ли файл, независимо
    от режима), поэтому пробный прогон/CLI --dry-run реально создавал ByDate/Albums/...-ветки
    на диске, хотя ни один файл не копировался -- пользователю приходилось чистить их вручную
    перед реальной сборкой. Убрано -- НЕ нужно для самой проверки коллизии: os.path.exists()
    ниже безопасно возвращает False для пути в ещё не существующей папке (нет исключения,
    обычное поведение os.path.exists() для любого отсутствующего предка), значит "коллизий
    нет" корректно определяется и без создания dest_dir. Реальное копирование (place_file()->
    atomic_copy()) уже само создаёт dest_dir непосредственно перед записью -- этот вызов был
    чистой избыточностью для реальной сборки и единственной причиной бага для dry-run."""
    name = truncate_segment(filename, max_len)
    if stats is not None and name != sanitize_windows_component(filename):
        # Compare against the SANITIZED (not raw) filename -- sanitize_windows_component()
        # alone (illegal-char replacement) is a different, already-documented concern than
        # length truncation; only count it here if the name was actually shortened.
        stats["warn_path_truncated"] = stats.get("warn_path_truncated", 0) + 1
    root, ext = os.path.splitext(name)
    candidate_path = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(winlong(candidate_path)):
        try:
            existing_sha = sha256_of_file_fn(candidate_path)
        except OSError:
            existing_sha = None
        if existing_sha == candidate_sha256:
            return candidate_path, True
        n += 1
        candidate_path = os.path.join(dest_dir, f"{root}_{n}{ext}")
    return candidate_path, False

# ============================================================================
# IO_COPY  (from pipeline/io_copy.py)
# ============================================================================


class InsufficientSpace(Exception):
    pass


class TargetLocked(Exception):
    pass


class _InterruptedRunReport(KeyboardInterrupt):
    """Ctrl+C-пакет, живой баг-репорт 2026-07-28: `raise KeyboardInterrupt` в
    _bare_launch_run_build() (после генерации отчёта с баннером прерывания) долетал до
    main()'s except KeyboardInterrupt как обычный, "безымянный" KeyboardInterrupt -- report_path,
    уже посчитанный и залогированный на экран ("Отчёт (данные на момент остановки): ..."),
    нигде не переживал этот re-raise. main() поэтому звал _pause_before_exit(True) БЕЗ
    report_path -- пользователь видел общую подсказку "Нажмите Enter для выхода" вместо
    "...чтобы открыть отчёт в браузере", и браузер ни разу не открывался, хотя файл на диске
    был полностью корректен. Несёт report_path тем же путём, каким сама KeyboardInterrupt уже
    летит -- main() достаёт его через getattr(e, "report_path", None)."""
    def __init__(self, report_path=None):
        super().__init__()
        self.report_path = report_path


LOCK_STALE_SECONDS = 12 * 3600


def _target_lock_path(target: str) -> str:
    return os.path.join(target, "__служебные_файлы", "LOCK")


def _read_lock_pid(lock_path: str):
    """PID процесса-владельца из LOCK-файла (TargetLock.__enter__ пишет туда str(os.getpid())).
    None -- файл пуст/нечитаем/содержит не число (старый формат до этой записи, повреждение)."""
    try:
        with open(winlong(lock_path), "r", encoding="ascii", errors="ignore") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class TargetLock:
    """p.5.4б: защита от ДВУХ ОДНОВРЕМЕННЫХ прогонов archive на один TARGET -- аудитом кода
    найдена единственная реальная дыра в гарантии "чужое содержимое не затирается":
    resolve_dest_path() проверяет занятость имени через os.path.exists() (TOCTOU), а
    os.replace() на Windows безусловно перезаписывает существующий файл. Два случайных
    параллельных прогона (двойной клик мимо, забыли про уже работающий) могут независимо
    счесть один и тот же dest_path свободным.

    Простой exclusive-create lock-файл, НЕ полноценный распределённый лок. Детект "устарел"
    (Раунд 156 ревью: докстринг синхронизирован с кодом d98b0ed): в первую очередь -- по
    реальной проверке "жив ли процесс-владелец" (_pid_is_alive() над PID, который __enter__
    сам пишет в файл; без сторонних зависимостей -- ctypes/OpenProcess() на Windows,
    os.kill(pid, 0) на POSIX). Прерванный по Ctrl-C/крахом прогон (его __exit__ снять LOCK не
    успел) распознаётся сразу, не за 12 часов. Порог по времени (mtime > 12ч) остался -- но
    теперь как ЗАПАСНОЙ путь на случай, когда PID из файла прочитать не удалось (старый
    формат до d98b0ed, повреждение). Этого достаточно для реалистичного сценария ("забыл, что
    прогон уже идёт"), не для состязания с кем-то, кто специально хочет обойти защиту -- как и
    остальная "защита от дурака" в этом файле."""

    def __init__(self, target: str, log=print, dry_run: bool = False):
        self.lock_path = _target_lock_path(target)
        self.log = log
        self._acquired = False
        # PROMPT_archive_report.md, 1.1а: report.html удаляется здесь ТОЛЬКО для реального
        # archive-прогона (dry_run=False). Докстринг исправлен 2026-08-19 (Раунд 107 ревью,
        # придирка): раньше здесь утверждалось, что "CLI --dry-run тоже проходит через
        # TargetLock" -- верно было ДО Раунда 102 (c7f8920, 2026-08-18); с тех пор `_main()`
        # передаёт `suppress_logs=args.dry_run` наравне с `dry_run=args.dry_run` (см. её
        # единственный вызов -- run(), :8288), а run() пропускает TargetLock целиком при
        # suppress_logs=True -- значит CLI --dry-run (и вообще ЛЮБОЙ suppress_logs=True прогон)
        # больше НЕ доходит до этого класса вовсе. self.dry_run здесь на практике сейчас всегда
        # False (единственный вызывающий -- run(), достижимый только когда suppress_logs=False,
        # а suppress_logs и dry_run в _main() всегда равны друг другу) -- ветка ниже оставлена
        # не мёртвым кодом, а осознанным запасом на случай, если появится ДРУГОЙ (не CLI)
        # вызывающий код с dry_run=True/suppress_logs=False одновременно.
        self.dry_run = dry_run

    def __enter__(self):
        real_path = winlong(self.lock_path)
        _makedirs_iterative(winlong(os.path.dirname(self.lock_path)))
        try:
            fd = os.open(real_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder_pid = _read_lock_pid(real_path)
            try:
                age = time.time() - os.path.getmtime(real_path)
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            # Владелец LOCK записан в сам файл (os.write ниже). Если этот процесс уже НЕ жив
            # (Ctrl-C/крах/пропажа питания -- __exit__ снять LOCK не успел), файл -- заведомо
            # мусор: снимаем сразу, не дожидаясь 12-часового mtime-порога. _pid_is_alive()
            # консервативна ("в сомнении -- жив", как и у _sweep_stale_dry_run_pid_dirs()),
            # поэтому holder_dead=True только когда процесс достоверно завершился. PID
            # неизвестен (старый формат/повреждение) -> holder_dead=False -> падаем на прежнее
            # mtime-правило. Закрывает и нестыковку, отмеченную Раундом 107 ревью
            # (RULES.md: TargetLock оставался на mtime, пока PID-подпапки dry-run уже нет).
            holder_dead = holder_pid is not None and not _pid_is_alive(holder_pid)
            if not holder_dead and age <= LOCK_STALE_SECONDS:
                remaining = LOCK_STALE_SECONDS - age
                rem_h, rem_m = int(remaining // 3600), int((remaining % 3600) // 60)
                raise TargetLocked(
                    f"похоже, другой прогон PhotoArchive уже работает с этим TARGET -- "
                    f"файл {self.lock_path} создан {age:.0f} сек назад. Если это не так "
                    f"(прошлый прогон аварийно завершился только что) -- удалите файл "
                    f"вручную и запустите снова. Если ничего не делать, программа сама "
                    f"снимет устаревший LOCK и позволит запуститься примерно через "
                    f"{rem_h}ч {rem_m}мин."
                ) from None
            if holder_dead:
                self.log(f"ВНИМАНИЕ: LOCK-файл принадлежал процессу (PID {holder_pid}), "
                         f"которого больше нет -- прошлый прогон был прерван (Ctrl-C/крах/"
                         f"питание). Снимаю LOCK и продолжаю.")
            else:
                self.log(f"ВНИМАНИЕ: обнаружен устаревший LOCK-файл ({age / 3600:.1f}ч) -- "
                         f"похоже, прошлый прогон был прерван аварийно (питание/крэш). "
                         f"Удаляю и продолжаю.")
            try:
                os.remove(real_path)
            except OSError:
                pass
            try:
                fd = os.open(real_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Другой прогон стартовал ровно в это же окно и успел пересоздать LOCK между
                # нашими os.remove() и os.open() -- редчайшая гонка, но не отдаём сырой traceback.
                raise TargetLocked(
                    "не удалось снять устаревший LOCK -- похоже, одновременно стартовал "
                    "другой прогон PhotoArchive. Попробуйте запустить ещё раз."
                ) from None
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        self._acquired = True
        if not self.dry_run:
            report_path = os.path.join(os.path.dirname(self.lock_path), "report.html")
            try:
                os.remove(winlong(report_path))
            except OSError:
                pass  # несуществующий файл -- не ошибка (best-effort, как _write_row)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._acquired:
            try:
                os.remove(winlong(self.lock_path))
            except OSError:
                pass
        return False


def inspect_target_lock(target: str):
    """Состояние LOCK-файла TARGET БЕЗ изменений на диске. None -- LOCK нет (или недоступен для
    stat). Иначе dict: pid (int|None), pid_alive (bool|None -- None когда pid неизвестен),
    age_seconds (float). Для преполётной проверки GUI перед реальной сборкой -- см.
    gui_menu._ensure_target_unlocked(): движок (TargetLock.__enter__) сам молча снимает LOCK
    заведомо мёртвого процесса, а вот «PID ещё жив» / «PID неизвестен» доводит до пользователя
    отдельным окном, а не тихим возвратом в меню."""
    path = winlong(_target_lock_path(target))
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    pid = _read_lock_pid(path)
    return {
        "pid": pid,
        "pid_alive": (_pid_is_alive(pid) if pid is not None else None),
        "age_seconds": age,
    }


def clear_target_lock(target: str, log=print) -> bool:
    """Снять LOCK-файл TARGET вручную (пользователь в GUI подтвердил, что прошлый прогон мёртв).
    True -- файл удалён либо его и не было; False -- удалить не удалось (залогировано)."""
    try:
        os.remove(winlong(_target_lock_path(target)))
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log(f"  Не удалось снять LOCK-файл: {e}")
        return False


def atomic_copy(src_path: str, dest_path: str, expected_sha256: str, margin_bytes: int):
    """Copy src -> temp file in the same directory as dest -> verify hash matches source
    -> atomic rename to dest_path. A broken/partial copy can never end up at dest_path.
    Raises InsufficientSpace if free space (after the copy) would dip below margin.
    """
    dest_dir = os.path.dirname(dest_path)
    _makedirs_iterative(winlong(dest_dir))

    size = os.path.getsize(winlong(src_path))
    free = shutil.disk_usage(winlong(dest_dir)).free
    if free - size < margin_bytes:
        raise InsufficientSpace(
            f"свободно {free/1024**3:.2f}ГБ, нужно {size/1024**3:.2f}ГБ + запас {margin_bytes/1024**3:.1f}ГБ"
        )

    # dir= уже в extended-length форме -> tmp_path, который вернёт mkstemp, тоже будет с
    # префиксом \\?\ (winlong() ниже это распознаёт и не удваивает префикс).
    fd, tmp_path = tempfile.mkstemp(prefix=".photosort_tmp_", dir=winlong(dest_dir))
    os.close(fd)
    try:
        shutil.copy2(winlong(src_path), tmp_path)
        # B (REVIEW-HANDOFF.md Раунд 148, замечание 2): пост-копи верификация хеша на КАЖДОЕ
        # размещение файла -- для одного гигантского видео та же «мёртвая зона» для паузы по
        # пробелу, что и уже покрытый хеш архива. progress_cb -- то же, что в analyze_batch().
        actual_sha = sha256_file(
            tmp_path, progress_cb=(_check_pause_keypress if os.name == "nt" else None))
        if actual_sha != expected_sha256:
            raise IOError(f"hash mismatch after copy: {src_path} -> {tmp_path} "
                           f"(expected {expected_sha256}, got {actual_sha})")
        os.replace(tmp_path, winlong(dest_path))
    except OSError as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        # Security audit finding #2: the margin check above is the intended guard, but a
        # misconfigured/zero/negative free_space_margin_gb (see Config.__post_init__
        # validation) could let it through -- if the OS itself then runs out of space
        # mid-copy, surface it as the same friendly InsufficientSpace stop instead of an
        # unhandled OSError traceback (which would leave the disk sitting at 0 bytes free).
        if e.errno == errno.ENOSPC:
            raise InsufficientSpace(f"диск заполнился во время копирования: {e}") from e
        raise
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def same_volume(path_a: str, path_b: str) -> bool:
    """True, если оба пути физически на одном томе (сравнение st_dev -- на Windows это
    серийный номер тома, надёжнее парсинга буквы диска: работает и для путей без буквы
    диска и не путается с монтированными точками). Оба пути должны существовать на момент
    вызова. Любая ошибка stat -- считаем "не один том" (безопасный дефолт: тогда используется
    обычное копирование с hash-verify, а не быстрый rename)."""
    try:
        return os.stat(winlong(path_a)).st_dev == os.stat(winlong(path_b)).st_dev
    except OSError:
        return False


def place_file(item: "SourceItem", dest_path: str, expected_sha256: str, cfg: "Config", run_logs,
                stats: dict = None) -> None:
    """Финализация одного файла на его месте в TARGET (Фаза 5).

    Файл ИЗ архива-источника (лежит внутри TMP_EXTRACT), если TMP_EXTRACT и место
    назначения физически на одном томе -- атомарный rename (os.replace), без повторного
    чтения байт: целостность уже подтверждена CRC-проверкой распаковщика при извлечении
    (успешная распаковка zip/7z/rar = файл цел), повторный sha256-verify избыточен.
    Rename не потребляет свободное место (файл просто меняет родителя на том же томе),
    поэтому проверка FREE_SPACE_MARGIN_GB здесь не нужна -- она уже была бы сделана перед
    распаковкой архива (см. SourceWalker._handle_archive).

    Исправлено в рамках аудита 2026-07-10 (Фаза 2, целостность данных): предыдущий
    комментарий утверждал это и про tar/tar.gz/tar.bz2 -- НЕВЕРНО. У формата tar вообще нет
    чек-суммы содержимого файла (только чек-сумма заголовка) -- tarfile.extract() "успешно"
    извлечёт член с побитыми в середине байтами без единой ошибки, в отличие от zip/7z/rar,
    где 7z.exe/UnRAR.exe реально сверяют CRC и извлечение целиком проваливается (см.
    extract_archive()) при несовпадении. item.archive_no_crc (выставляется SourceWalker
    только для tar-источников) поэтому НИКОГДА не берёт этот rename-шорткат -- всегда идёт
    полный atomic_copy() с hash-verify ниже. Это не может обнаружить повреждение, случившееся
    ВНУТРИ самой распаковки tar (сверять tar-контент не с чем -- у формата просто нет
    контрольной суммы), но хотя бы не выдаёт rename без единой проверки за "формат с
    подтверждённой при распаковке целостностью", каким tar не является.

    Во всех остальных случаях (обычный файл источника, включая CD/DVD напрямую; файл из
    tar/tar.gz/tar.bz2; либо файл из zip/7z/rar, но TMP_EXTRACT_DIR оказался на другом томе,
    чем TARGET) -- прежняя схема без изменений: atomic_copy (temp-файл рядом с dest ->
    hash-verify -> atomic rename)."""
    dest_dir = os.path.dirname(dest_path)
    from_archive = item.read_path.startswith(cfg.tmp_extract + os.sep)
    if from_archive and not item.archive_no_crc:
        _makedirs_iterative(winlong(dest_dir))
        if same_volume(cfg.tmp_extract, dest_dir):
            os.replace(winlong(item.read_path), winlong(dest_path))
            run_logs.action(f"renamed(from_archive,same_volume): {item.read_path} -> {dest_path}")
            return
        if stats is not None:
            # p.5.3а: TMP_EXTRACT_DIR on a different volume than this file's destination --
            # already warned once at startup (report_environment) if the config-level paths
            # differ; this counts the actual number of files that degraded to copy because
            # of it, for summary.txt.
            stats["warn_cross_volume_tmp_extract"] = stats.get("warn_cross_volume_tmp_extract", 0) + 1
    elif from_archive and stats is not None:
        stats["tar_verified_copy"] = stats.get("tar_verified_copy", 0) + 1
    atomic_copy(item.read_path, dest_path, expected_sha256, int(cfg.free_space_margin_gb * 1024**3))


def _seed_archive_cache(conn, dest_path: str, size: int, sha256, phash, duration, width, height, bitrate,
                         exif_dt=None, exif_dt_source=None, camera=None, gps_lat=None, gps_lon=None) -> None:
    """Раунд 5 ревью, вариант D (REVIEW-HANDOFF.md): файл, который этот же прогон только что
    разместил через place_file(), уже имеет достоверные sha256/phash/duration/width/height/
    bitrate из analyze_batch() -- нет смысла ждать следующего index_archive() (следующий SOURCE
    того же batch'а или следующая сессия), который увидит этот путь как "незнакомый" и
    перечитает те же самые байты с нуля только чтобы получить то, что уже известно сейчас.
    Строго дешевле, не компромисс -- те же самые байты, не оценка (sha256 к тому же дважды
    подтверждён верификацией внутри atomic_copy(), если copy шёл этим путём). Не меняет
    инвариант "TARGET перепроверяется с нуля" для файлов, которые программа не размещала сама
    -- только сеет кэш для тех, что разместила.

    exif_dt/exif_dt_source/camera/gps_lat/gps_lon (речь пользователя, 2026-08-02): та же
    экономия, что и для sha256/phash выше, теперь и для EXIF-полей -- вызывающий код уже
    получил их из analyze_batch()'s exiftool-вызова при размещении, писать всегда безусловно
    (exif_cached=1) -- в отличие от sha256/phash (которых у "broken"-файла может не быть),
    exiftool на РЕАЛЬНО размещённый (значит прочитанный, не сломанный) файл уже отработал, раз
    мы вообще дошли до place_file(); None-значения полей -- легитимный кэшированный ответ "у
    этого файла нет такого EXIF-тега", не "не проверяли".

    mtime берётся РЕАЛЬНЫМ os.stat() уже физически размещённого файла, а не из SourceRecord
    источника -- именно (path,size,mtime) размещённого файла на TARGET index_archive() будет
    сверять при следующем проходе (см. его cache-lookup), а не метаданные исходника."""
    try:
        st = os.stat(winlong(dest_path))
    except OSError:
        # Тот же класс гонки, что и guard'ы в index_archive()/_handle_archive() -- если файл
        # уже пропал с TARGET между place_file() и этим stat(), просто не сеем кэш для него;
        # следующий index_archive() либо не найдёт файл вовсе, либо перехеширует его заново.
        return
    conn.execute(
        "INSERT OR REPLACE INTO archive_cache"
        "(path,size,mtime,sha256,phash,duration,width,height,bitrate,"
        "exif_cached,exif_dt,exif_dt_source,camera,gps_lat,gps_lon) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dest_path, st.st_size, st.st_mtime, sha256, phash, duration, width, height, bitrate,
         1, exif_dt.isoformat() if exif_dt else None, exif_dt_source, camera, gps_lat, gps_lon),
    )

# ============================================================================
# LOGS  (from pipeline/logs.py)
# ============================================================================

LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024
LOG_ROTATE_KEEP = 3


def _rotate_log_if_needed(path: str):
    """p.5.3в: every file under __служебные_файлы/logs/ accumulates for the whole lifetime of the
    archive (opened "a" once per run/crash, never truncated) -- without this, a long-lived
    archive used by a non-technical user ("домохозяйки не будут чистить логи") grows these files
    without bound. At 20MB, rename to <name>-YYYYMMDD-HHMMSS.<ext> and keep only the
    LOG_ROTATE_KEEP most recent rotated copies per base name (older ones deleted). None of
    these files is ever read back by the program itself (grep-confirmed -- the real
    source of truth is work.db) -- rotation can only affect a human reading the log later,
    never program logic. Called right before a file is opened for append, so a rotated
    file's replacement always starts empty (CSV header re-written on next _init_csv call).
    Also reused for crash.log (next to the .exe, not under logs/ -- see
    _log_unexpected_crash), which has no CSV header to restore."""
    real_path = winlong(path)
    try:
        size = os.path.getsize(real_path)
    except OSError:
        return
    if size < LOG_ROTATE_MAX_BYTES:
        return
    root, ext = os.path.splitext(path)
    rotated_path = f"{root}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    try:
        os.replace(real_path, winlong(rotated_path))
    except OSError:
        return
    base_name = os.path.basename(root)
    dirpath = os.path.dirname(path) or "."
    pattern = re.compile(r"^" + re.escape(base_name) + r"-\d{8}-\d{6}" + re.escape(ext) + r"$")
    try:
        rotated_siblings = sorted(
            (f for f in os.listdir(winlong(dirpath)) if pattern.match(f)), reverse=True,
        )
    except OSError:
        return
    for stale in rotated_siblings[LOG_ROTATE_KEEP:]:
        try:
            os.remove(winlong(os.path.join(dirpath, stale)))
        except OSError:
            pass


class RunLogs:
    def __init__(self, logs_dir: str):
        self.logs_dir = logs_dir
        _makedirs_iterative(winlong(logs_dir))
        self._files = {}
        self._writers = {}
        self._init_csv("appended",
                        ["timestamp", "source", "dest", "reason", "flags", "date", "duration", "place",
                         "camera"])
        self._init_csv("skipped", ["timestamp", "source", "matched_with", "reason"])
        self._init_csv("disputes", ["timestamp", "source", "reason", "dest", "was_hidden"])
        self._init_csv("dates_review", ["timestamp", "dest", "date", "tier", "confidence", "evidence", "source"])
        self._init_csv("albums_merged", ["timestamp", "album", "source_variant"])
        self._init_csv("unreadable", ["timestamp", "source", "error"])
        self._init_csv("rejected_noise", ["timestamp", "source", "reason"])
        self._init_csv("near_dup_edges", ["timestamp", "source", "dest", "matched_dest", "category", "hamming"])
        self._init_csv("undated_media", ["timestamp", "source", "dest"])
        actions_path = os.path.join(logs_dir, "actions.log")
        _rotate_log_if_needed(actions_path)
        self.actions_log = open(winlong(actions_path), "a", encoding="utf-8")
        archives_path = os.path.join(logs_dir, "archives.log")
        _rotate_log_if_needed(archives_path)
        self.archives_log = open(winlong(archives_path), "a", encoding="utf-8")

    def _init_csv(self, name, header):
        path = os.path.join(self.logs_dir, f"{name}.csv")
        _rotate_log_if_needed(path)
        is_new = not os.path.exists(winlong(path))
        f = open(winlong(path), "a", newline="", encoding="utf-8")
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
            f.flush()
        self._files[name] = f
        self._writers[name] = w

    def _ts(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _write_row(self, name, row):
        """TARGET can vanish mid-run (disk unplugged) -- confirmed via real-hardware test
        2026-07-18: a write/flush failure here used to propagate out of _log_write_failure()
        (which calls unreadable() to log ANOTHER file's write error) and crash the entire run
        with an unhandled traceback, defeating the "log it, count it, keep going" design that
        function exists for. Every RunLogs method had the same unguarded flush(), not just
        unreadable() -- centralized here so the fix can't drift out of sync between them.
        Silently dropped on failure; the caller's own console message (_log_write_failure)
        already told the user about the underlying write error."""
        try:
            self._writers[name].writerow(row)
            self._files[name].flush()
        except OSError:
            pass

    def appended(self, source, dest, reason, flags="", date="", duration="", place="", camera=""):
        """date (SESSION-HANDOFF.txt, баг 9): реальная дата файла (`YYYY-MM-DD` -- полная,
        либо `YYYY` -- только год, precision=="year", см. resolve_date()), а не то, что можно
        восстановить разбором `dest` -- у файлов в Albums\\... нет сегмента ByDate в пути
        вообще, report.py не может взять дату из dest для них никак иначе. "" -- дата
        неизвестна (Tier D) либо вызывающий код её не передал (raw-зеркало, old call sites).

        duration (4.6, PROMPT_report_marketing.md): длительность видео в секундах (float,
        `rec.duration`, уже посчитана `video_duration_and_resolution()` при полном хешировании),
        персистентная колонка -- по аналогии с `date` выше, чтобы кумулятивная сумма по всему
        архиву считалась дешёвым чтением CSV, а не повторным чтением контейнера каждого видео
        при каждом рендере отчёта (см. обсуждение "ось стоимости" в самом разделе 4.6). "" --
        не видео либо длительность не удалось определить.

        place (живая находка 2026-07-25, боевой прогон F:\\, весь архив ушёл в Albums\\..., ни
        одного города в отчёте): та же проблема, что была у `date` до бага 9 -- город из
        `place_for_gps()` раньше попадал в имя папки ТОЛЬКО для ByDate-маршрута, для Albums-
        файлов не считался вообще, report.py не мог взять место из dest никак. Теперь
        `_process_record()` считает его один раз независимо от маршрутизации и пишет сюда
        всегда, когда есть GPS и `place_lookup: offline`. "" -- нет GPS-тега, geocoding выключен
        (`place_lookup: off`) либо reverse_geocoder не смог определить город по координатам.

        camera (пункт E, "большой разбор report.html", SESSION-HANDOFF.txt): `rec.camera`
        (`camera_from_tags()`, EXIF Make/Model) -- та же персистентная логика, что и `duration`
        выше (дешёвое чтение CSV вместо повторного чтения EXIF каждого файла на каждом
        рендере отчёта). До этого пункта `rec.camera` использовался ТОЛЬКО как
        `bool(rec.camera)` (флаг для сравнения дублей/near-dup) -- сама строка никуда не
        сохранялась. "" -- нет EXIF Make/Model (скриншоты, картинки из интернета,
        отсканированные без EXIF)."""
        self._write_row("appended", [self._ts(), source, dest, reason, flags, date, duration, place,
                                      camera])

    def skipped(self, source, matched_with, reason):
        self._write_row("skipped", [self._ts(), source, matched_with, reason])

    def disputed(self, source, reason, dest, was_hidden=False):
        self._write_row("disputes", [self._ts(), source, reason, dest, int(was_hidden)])

    def unreadable(self, source, error):
        self._write_row("unreadable", [self._ts(), source, error])

    def rejected_noise(self, source, reason):
        self._write_row("rejected_noise", [self._ts(), source, reason])

    def date_review(self, dest, date_value, tier, confidence, evidence, source):
        self._write_row("dates_review", [
            self._ts(), dest, date_value.isoformat() if date_value else "", tier, confidence, evidence, source
        ])

    def album_merged(self, album, source_variant):
        self._write_row("albums_merged", [self._ts(), album, source_variant])

    def near_dup_edge(self, source, dest, matched_dest, category, hamming):
        """PROMPT_archive_report.md, 1.2б: одно ребро графа near-dup-совпадений -- ЧЕМ
        (matched_dest) обосновано решение appended_near_dup/appended_better/appended_crop.
        Аддитивный лог, не меняет appended.csv/другие существующие -- Лист 3 отчёта строит
        по этим рёбрам кластеры (union-find), а не парсит basename из текста note appended.csv."""
        self._write_row("near_dup_edges", [self._ts(), source, dest, matched_dest, category, hamming])

    def undated_media(self, source, dest):
        """Ревизорская находка (REVIEW-HANDOFF.md, раунд 3): Tier D (`resolve_date()` ->
        `date_value=None`) никогда не попадает в `dates_review.csv` -- та проверка гейтится на
        `date_value is not None` (см. вызывающий код), а Tier D по конструкции всегда
        `date_value=None`. Без сигнала report.py не может отличить Tier D от Tier A (оба
        "не встретились в dates_review.csv"). НЕ расширяем dates_review.csv (документирован в
        RULES.md/README/FAQ как "не-Tier-A даты, которые ЕСТЬ, но не по EXIF" -- Tier D это
        не тот случай, значения нет вообще) -- новый аддитивный лог, тот же паттерн, что
        near_dup_edges.csv."""
        self._write_row("undated_media", [self._ts(), source, dest])

    def action(self, line):
        try:
            self.actions_log.write(f"[{self._ts()}] {line}\n")
            self.actions_log.flush()
        except OSError:
            pass

    def debug_action(self, line):
        """p.5.3б: [DEBUG]-строка в actions.log -- caller gates this on cfg.debug, this
        method itself has no opinion on whether debug mode is on (keeps RunLogs config-
        agnostic, like every other method here)."""
        try:
            self.actions_log.write(f"[{self._ts()}] [DEBUG] {line}\n")
            self.actions_log.flush()
        except OSError:
            pass

    def archive_event(self, display, status, note=""):
        try:
            self.archives_log.write(f"[{self._ts()}] {display}: {status} {note}\n".rstrip() + "\n")
            self.archives_log.flush()
        except OSError:
            pass

    def write_summary(self, text: str):
        path = os.path.join(self.logs_dir, "summary.txt")
        try:
            _rotate_log_if_needed(path)
            with open(winlong(path), "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def close(self):
        # Same TARGET-vanished concern as _write_row -- close() can raise too, and one
        # failed close() must not stop the rest from being attempted.
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                pass
        for f in (self.actions_log, self.archives_log):
            try:
                f.close()
            except OSError:
                pass


class NullRunLogs:
    """ТЗ-меню 2026-07-10, раздел 5: используется вместо RunLogs, когда
    cfg.suppress_logs=True (интерактивный "пробный прогон" из голого меню) -- та же
    поверхность методов, каждый no-op, НИЧЕГО не создаёт и не открывает на диске (в отличие
    от RunLogs.__init__, который безусловно делает os.makedirs()+открывает файлы)."""

    def appended(self, *a, **kw): pass
    def skipped(self, *a, **kw): pass
    def disputed(self, *a, **kw): pass
    def unreadable(self, *a, **kw): pass
    def rejected_noise(self, *a, **kw): pass
    def date_review(self, *a, **kw): pass
    def album_merged(self, *a, **kw): pass
    def near_dup_edge(self, *a, **kw): pass
    def undated_media(self, *a, **kw): pass
    def action(self, *a, **kw): pass
    def debug_action(self, *a, **kw): pass
    def archive_event(self, *a, **kw): pass
    def write_summary(self, *a, **kw): pass
    def close(self): pass


class CollectingRunLogs:
    """PROMPT_archive_report.md, 1.2а: третья реализация той же поверхности методов, что
    RunLogs/NullRunLogs -- вместо файла на диске (RunLogs) или чистого no-op (NullRunLogs)
    копит те же аргументы в память, строками в форме будущих CSV: каждый метод кладёт dict
    в self.rows["<имя>"], где "<имя>" и имена ключей ДОСЛОВНО совпадают с именем/заголовком
    соответствующего RunLogs._init_csv(...) -- report.py читает TARGET-уровень через
    csv.DictReader по файлам логов и WORKDIR-уровень через CollectingRunLogs.rows напрямую,
    обе формы идентичны, шаблону/чартам не нужно знать происхождение. Подставляется в ту же
    точку инъекции, что и NullRunLogs (`run_logs = ...` в _run_impl) -- ноль изменений в
    _process_record или где-либо в логике принятия решений."""

    def __init__(self):
        self.rows = {
            "appended": [], "skipped": [], "disputes": [], "dates_review": [],
            "albums_merged": [], "unreadable": [], "rejected_noise": [], "near_dup_edges": [],
            "undated_media": [],
        }

    def _ts(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def appended(self, source, dest, reason, flags="", date="", duration="", place="", camera=""):
        self.rows["appended"].append({"timestamp": self._ts(), "source": source, "dest": dest,
                                       "reason": reason, "flags": flags, "date": date,
                                       "duration": duration, "place": place, "camera": camera})

    def skipped(self, source, matched_with, reason):
        self.rows["skipped"].append({"timestamp": self._ts(), "source": source,
                                      "matched_with": matched_with, "reason": reason})

    def disputed(self, source, reason, dest, was_hidden=False):
        self.rows["disputes"].append({"timestamp": self._ts(), "source": source, "reason": reason,
                                       "dest": dest, "was_hidden": int(was_hidden)})

    def unreadable(self, source, error):
        self.rows["unreadable"].append({"timestamp": self._ts(), "source": source, "error": error})

    def rejected_noise(self, source, reason):
        self.rows["rejected_noise"].append({"timestamp": self._ts(), "source": source, "reason": reason})

    def date_review(self, dest, date_value, tier, confidence, evidence, source):
        self.rows["dates_review"].append({
            "timestamp": self._ts(), "dest": dest,
            "date": date_value.isoformat() if date_value else "", "tier": tier,
            "confidence": confidence, "evidence": evidence, "source": source,
        })

    def album_merged(self, album, source_variant):
        self.rows["albums_merged"].append({"timestamp": self._ts(), "album": album,
                                            "source_variant": source_variant})

    def near_dup_edge(self, source, dest, matched_dest, category, hamming):
        self.rows["near_dup_edges"].append({
            "timestamp": self._ts(), "source": source, "dest": dest,
            "matched_dest": matched_dest, "category": category, "hamming": hamming,
        })

    def undated_media(self, source, dest):
        self.rows["undated_media"].append({"timestamp": self._ts(), "source": source, "dest": dest})

    def action(self, *a, **kw): pass
    def debug_action(self, *a, **kw): pass
    def archive_event(self, *a, **kw): pass
    def write_summary(self, *a, **kw): pass
    def close(self): pass

# ============================================================================
# ARCHIVE_INDEX  (from pipeline/archive_index.py)
# ============================================================================


def _walk_media_files(root: str, exclude_dirs=None):
    """Yields (path, ftype) for every image/raw/video file under root, pruning any subtree
    whose path exactly matches one of exclude_dirs (used to keep 0000-undated out of the
    dedup base -- see index_archive). Comparison is case-insensitive (NTFS) via normcase,
    same convention as is_under_system_dir()."""
    exclude_norm = {os.path.normcase(os.path.abspath(d)) for d in (exclude_dirs or [])}
    # winlong(root) so the walk itself survives deeply-nested Albums/ByDate trees (the exact
    # kind of path this tool builds) on re-indexing in later runs -- os.walk inherits the
    # extended-length prefix into every subsequent os.path.join it does internally. Stripped
    # back to a plain path immediately so DB storage/logs/comparisons stay canonical; winlong()
    # is re-applied at the point of each actual filesystem call (see index_archive).
    for dirpath, dirnames, filenames in os.walk(winlong(root)):
        stripped_dirpath = _strip_winlong(dirpath)
        if os.path.normcase(os.path.abspath(stripped_dirpath)) in exclude_norm:
            dirnames[:] = []  # prune: don't descend into this subtree either
            continue
        for fn in filenames:
            p = _strip_winlong(os.path.join(dirpath, fn))
            t = file_type(p)
            if t in ("image", "raw", "video"):
                yield p, t


# SESSION-HANDOFF.txt п.12 (2026-08-05, боевой прогон): живой пример -- 32406 файлов, ~2
# файла/сек, ETA ~4.5ч, но видна только через ~12с ПОСЛЕ старта бара, не до него. Порог
# простой (абсолютное число холодных файлов), не точная наука -- см. index_archive() за
# самим предупреждением.
_COLD_CACHE_WARNING_THRESHOLD = 500


def index_archive(cfg: Config, conn, log=print):
    """Phase 1: index Albums/ + ByDate/ + RAW/ into the dedup pool (archive table).
    __служебные_файлы/ (disputed/logs/prompt/tmp_extract) and ByDate/0000-undated (+ its RAW
    mirror, RAW/ByDate/0000-undated) are intentionally excluded -- not part of the dedup
    ground truth. Excluding __служебные_файлы/ needs no special-casing here: it lives outside
    albums_root/bydate_root/raw_root entirely (see Config.__post_init__), so the roots
    walked below never reach it. 0000-undated DOES sit inside bydate_root/raw_root (it's
    real archive content, re-readable as a source -- see RULES.md), so it needs an explicit
    prune: a file in the dedup base would block its own promotion once new rules manage to
    date it (see RULES.md, "КРИТИЧНО" note in the undated section).
    """
    roots = [cfg.albums_root, cfg.bydate_root, cfg.raw_root]
    excludes_by_root = {
        cfg.bydate_root: [cfg.undated_root],
        cfg.raw_root: [os.path.join(cfg.raw_root, "ByDate", "0000-undated")],
    }
    total_files = 0
    total_bytes = 0
    cur = conn.cursor()

    # Речь пользователя, 2026-08-02: archive_cache -- отдельное соединение к отдельному файлу
    # ВНУТРИ архива (archive_cache_db_path(cfg.target)), не таблица в этом же `conn`
    # (work.db в WORKDIR) -- см. докстринг archive_cache_db_path()/_open_archive_cache_conn().
    # `conn`/`cur` здесь по-прежнему только для эфемерной таблицы `archive` (база дедупа этого
    # прогона, работает как раньше).
    #
    # REVIEW-HANDOFF.md, Раунд 54, замечание 1: `not cfg.suppress_logs` -- до переноса кэша
    # внутрь архива открыть это соединение при "Пробном прогоне" (suppress_logs=True) было
    # безобидно (файл создавался в WORKDIR, не в TARGET). Теперь `_open_archive_cache_conn()`
    # открывает файл ВНУТРИ TARGET -- на уже существующем архиве (обычный сценарий "раз в год
    # добавляю фото") это создаёт/пишет настоящий файл ВНУТРИ TARGET, нарушая задокументированную
    # гарантию suppress_logs "никогда не пишет в TARGET" (см. run()'s docstring,
    # _bare_launch_run_dryrun()). Тот же принцип, что уже защищает ensure_target_layout() чуть
    # ниже по файлу (:6070) -- suppress_logs=True не создаёт и не трогает ничего в TARGET,
    # включая archive_cache.db. CLI `--dry-run` (речь пользователя, 2026-08-18) теперь ТОЖЕ
    # ходит сюда с `suppress_logs=True` (`_main()` передаёт `suppress_logs=args.dry_run`) --
    # тот же принцип защищает и его: кэш хешей просто не читается/не пишется, TARGET не
    # трогается вовсе, ничем не хуже пустого кэша на первом прогоне.
    cache = {}
    cache_conn = None
    if cfg.archive_hash_cache and not cfg.suppress_logs:
        cache_conn = _open_archive_cache_conn(cfg.target)
        if cache_conn is not None:
            for row in cache_conn.execute(
                "SELECT path, size, mtime, sha256, phash, duration, width, height, bitrate FROM archive_cache"
            ):
                cache[row[0]] = row[1:]

    # Раунд 5 ревью (2026-07-18/19, REVIEW-HANDOFF.md): дерево архива обходится один раз
    # (os.walk дорог на медленных/сетевых дисках -- целевая аудитория проекта), список
    # материализуется в память -- даёт честный total для бара (%/ETA) без повторного os.walk.
    entries = [
        (root, path, ftype)
        for root in roots if os.path.isdir(winlong(root))
        for path, ftype in _walk_media_files(root, exclude_dirs=excludes_by_root.get(root))
    ]

    # SESSION-HANDOFF.txt п.12: холодный кэш (первая индексация большого архива, или кэш
    # только что создан) может занимать часы без предупреждения заранее -- ETA видна только
    # ~12с ПОСЛЕ старта бара (первый refresh), не до него. `path in cache` -- дешёвая проверка
    # по словарю (без I/O, никакого os.stat) -- не точное совпадение size/mtime (это уже нужно
    # было бы читать файл), только грубый "точно холодный" сигнал, которого достаточно для
    # качественного предупреждения. Без числовой оценки времени в сообщении -- нет надёжной
    # априорной скорости диска до реального старта.
    #
    # REVIEW-HANDOFF.md, Раунд 66, придирка: `cfg.archive_hash_cache=False` -- явный опт-аут
    # пользователя от персистентного кэша (не дефолт) -- `cache` тогда ПОСТОЯННО пуст на
    # КАЖДОМ прогоне (см. условие открытия cache_conn выше), не только на первом. Без этой
    # проверки предупреждение печаталось бы на каждой обычной сборке для этой узкой аудитории,
    # хотя рамка сообщения ("это может занять заметное время") задумана как разовый heads-up
    # про холодный старт, не постоянное свойство выбранной конфигурации.
    #
    # REVIEW-HANDOFF.md, Раунд 67, замечание 1: та же логика применима к `suppress_logs=True`
    # ([2] Пробный прогон) -- `cache_conn` там тоже никогда не открывается (см. условие чуть
    # выше по функции, :5190), `cache` архитектурно пуст на КАЖДОМ прогоне, не временно
    # "холодный первый раз". `not cfg.suppress_logs` -- то же самое условие, что уже защищает
    # открытие cache_conn, не новое понятие.
    cold_count = sum(1 for _root, path, _ftype in entries if path not in cache)
    if cfg.archive_hash_cache and not cfg.suppress_logs and cold_count >= _COLD_CACHE_WARNING_THRESHOLD:
        log(f"  {cold_count} из {len(entries)} файлов ещё не в кэше -- это может занять "
            f"заметное время.")

    processed_paths = set()
    # Речь пользователя, 2026-08-07 ("Если архива нет, то индексировать нечего, можно строку
    # не выводить"): на новом (только что созданном, ещё пустом) TARGET entries всегда пуст --
    # раньше ProgressReporter всё равно конструировался (total=len(entries) or None -- 0
    # превращалось в None, indeterminate-режим tqdm), печатался один "пустой" кадр бара
    # ("...: всего обработано файлов: 0 [00:00, ?файл/с]") без единой реальной итерации цикла
    # ниже -- индексировать нечего, а строка всё равно появлялась. `if entries:` -- бар (и его
    # кадр) просто не создаётся вовсе, когда индексировать действительно нечего; `total=len(
    # entries)` без `or None` -- внутри этой ветки entries гарантированно непуст, запасной
    # indeterminate-случай больше не нужен.
    if entries:
        with ProgressReporter(total=len(entries), desc=" Просматриваю уже собранный архив", unit="файл",
                               note_width=len("большое видео")) as bar:
            # B (REVIEW-HANDOFF.md Раунд 148 замечание 2 / Раунд 149 придирка): Фаза 1 --
            # самая первая фаза ЛЮБОГО прогона; без опроса паузу нельзя поставить, даже не
            # дойдя до копирования. Одна замкнутая на реальный log= вида (как analyze_batch()
            # и _handle_dvd_unit()) -- и на поштучный опрос, и внутрь самого хеша.
            _pause_cb = (lambda: _check_pause_keypress(log=log)) if os.name == "nt" else None
            for root, path, ftype in entries:
                if _pause_cb is not None:
                    _pause_cb()
                try:
                    st = os.stat(winlong(path))
                except OSError:
                    continue
                size, mtime = st.st_size, st.st_mtime
                note = "большое видео" if ftype == "video" and size > 200 * 1024**2 else None

                cached = cache.get(path)
                if cached and cached[0] == size and abs(cached[1] - mtime) < 1e-6:
                    sha, phash, duration, width, height, bitrate = cached[2], cached[3], cached[4], cached[5], cached[6], cached[7]
                else:
                    # Раунд 7 ревью (REVIEW-HANDOFF.md): тот же приём, что и фикс раунда 6 в
                    # run_for_source() (f33534d) -- note должен появиться на экране ДО блокирующего
                    # sha256_file()/video_phash_3frames(), не после, иначе бар всю паузу молча
                    # показывает состояние предыдущего файла. n=0 -- только текст, счётчик не трогаем.
                    bar.update(0, note=note)
                    width = height = bitrate = None
                    try:
                        sha = sha256_file(path, progress_cb=_pause_cb)
                    except OSError:
                        # Same class of race as the archive-scan guards in _handle_archive()
                        # (2026-07-11, live user report) -- this indexes the user's OWN existing
                        # archive (Phase 1, dedup base), so a file removed/renamed here from
                        # outside the program between the os.stat() above and this read must not
                        # crash the whole run either. Skipping it here just means Phase 1 doesn't
                        # index a file that's no longer actually there -- same effect as if it
                        # had never been stat-able in the first place (see the os.stat() guard).
                        continue
                    phash = None
                    duration = None
                    if ftype in ("image", "raw"):
                        phash, width, height = image_phash_and_size(path)
                    elif ftype == "video":
                        duration, width, height, bitrate = video_duration_and_resolution(path)
                        frames = video_phash_3frames(path, duration)
                        phash = "|".join(frames) if frames else None
                    if cache_conn is not None:
                        cache_conn.execute(
                            "INSERT OR REPLACE INTO archive_cache"
                            "(path,size,mtime,sha256,phash,duration,width,height,bitrate) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (path, size, mtime, sha, phash, duration, width, height, bitrate),
                        )

                cur.execute(
                    "INSERT OR REPLACE INTO archive(path,root,size,mtime,sha256,phash,duration,type,width,height,bitrate) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (path, root, size, mtime, sha, phash, duration, ftype, width, height, bitrate),
                )
                processed_paths.add(path)
                total_files += 1
                total_bytes += size
                bar.update(1, note=note)

    if cache_conn is not None:
        # archive_cache -- персистентна между прогонами (её собственный файл, не эфемерная
        # `archive` в work.db) -- иначе росла бы бессрочно (файлы, удалённые/переименованные
        # из архива, оставляли бы в кэше вечный мусор). Только что законченный обход roots --
        # это полная актуальная правда "что реально сейчас есть в архиве": любой путь в `cache`
        # (загружен в начале функции), не попавший в processed_paths (успешно стат'нутые и
        # проиндексированные этим же проходом файлы -- то же множество, что раньше отражала
        # таблица `archive`), безопасно считать устаревшим. Питоновский set-diff, не SQL
        # "NOT IN (SELECT ...)" -- archive_cache теперь в ДРУГОМ файле/соединении, чем
        # эфемерная `archive` (см. archive_cache_db_path()), кросс-файловый JOIN одним
        # запросом здесь недоступен без ATTACH DATABASE.
        stale = cache.keys() - processed_paths
        if stale:
            cache_conn.executemany("DELETE FROM archive_cache WHERE path=?", [(p,) for p in stale])
        cache_conn.commit()
        cache_conn.close()

    conn.commit()
    if entries:
        # Симметрично отсутствию бара выше -- на пустом TARGET (новый архив, entries==[])
        # печатать "проиндексировано существующего архива -- 0 файлов" нечего индексировать
        # означает и нечего докладывать: строка сама по себе была бы небольшой бессмыслицей
        # ("существующий архив" на самом деле ещё не существует).
        log(f"Фаза 1: проиндексировано существующего архива — {total_files} файлов, "
            f"{total_bytes / (1024**3):.2f} ГБ")
    return total_files, total_bytes

# ============================================================================
# ANALYZE  (А.2: CLI-подкоманда "analyze" -- read-only диагностика источника; внутреннее
# значение mode остаётся "analyze-quick", см. _CLI_ANALYZE_MODE_MAP)
# Отдельные РЕЖИМЫ (не флаг DRY_RUN -- у DRY_RUN другой смысл, "что я сделаю при сборке":
# он проходит ВСЮ Фазу 4/4.5/5 и пишет обычные __служебные_файлы\logs\*.csv в TARGET, просто без физического
# копирования байт). Analyze-режимы вообще не пишут в TARGET ни файлов, ни логов -- это
# read-only диагностика ИСТОЧНИКА, единственный побочный эффект на диске -- work.db
# (эфемерный индекс, как и у обычной сборки) и, по желанию вызывающего, analyze_report.csv,
# оба в WORKDIR, а не в TARGET. Исключение -- archive_cache под self_scan=True (Паспорт
# архива, задача 2 речи пользователя 2026-08-02): там cfg.source И ЕСТЬ уже собранный архив
# (см. run_passport()), archive_cache_db_path(cfg.source) пишет внутрь него же, рядом с
# его собственными логами -- не "в TARGET", а буквально в тот же архив, что и паспортируется,
# так что это не нарушает "не пишет в TARGET" (TARGET здесь -- фиктивный placeholder, ничего
# по нему не создаётся).
# ============================================================================


@dataclass
class AnalyzeStats:
    mode: str
    interrupted: bool = False  # Ctrl+C-пакет (2026-08-07, распространено с _RunState.interrupted
        # на analyze/паспорт): выставляется run_analyze()'s основным циклом, если работа
        # прервана KeyboardInterrupt -- stats на этот момент уже содержит всё, что успело
        # посчитаться, вызывающий код решает, показывать ли баннер прерывания в отчёте.
    total_files: int = 0
    total_bytes: int = 0
    n_images: int = 0
    n_raw: int = 0
    n_videos: int = 0
    n_archives_found: int = 0
    # 2026-08-06, боевой прогон: n_archives_found считает ЛЮБОЙ статус archive_* (в т.ч.
    # archive_no_media/password_protected/read_error/bomb_suspected -- контейнер без единого
    # реально извлечённого медиафайла); n_archives_with_media -- только archive_extracted
    # (см. _handle_archive(), логируется исключительно при media_count>0) -- честное число для
    # текста вида "часть памяти лежит в архивах", не считающего пустые/битые/запароленные.
    n_archives_with_media: int = 0
    n_archives_encrypted: int = 0
    n_archives_nested: int = 0
    # Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): путь для каждого
    # запароленного архива -- раньше был только счётчик n_archives_encrypted выше, путь
    # реально уже есть в walker.archive_logs, но никуда не попадал.
    encrypted_archive_paths: list = field(default_factory=list)
    n_no_exif_date: int = 0
    n_future_date: int = 0
    n_before_1990: int = 0
    n_tier_c_estimated: int = 0
    n_copy_artifact_mtime: int = 0
    n_broken_or_zero: int = 0
    # SESSION-HANDOFF.txt, 2026-08-09 (боевой прогон, шестая находка): n_broken_or_zero выше
    # смешивал ДВЕ разные по природе категории под одним счётчиком -- содержимое НЕ распознано
    # (item.size==0 или rec.broken, TARGET-уровень зовёт это "disputed") и файл физически НЕ
    # УДАЛОСЬ прочитать (rec.read_error, TARGET-уровень зовёт это "unreadable", disputes.csv/
    # unreadable.csv -- РАЗНЫЕ CSV уже на TARGET-уровне, см. run_logs.disputed()/.unreadable()).
    # Analyze-уровень (self_scan=False, обычный [1]/CLI analyze) теперь тоже различает их --
    # self-scan/"Паспорт архива" по-прежнему читает только общий n_broken_or_zero выше, эта
    # находка его не касается (см. _render_passport_integrity()). Путь -- реальный абсолютный
    # (_analyze_source_abs_path(cfg, item), см. run_analyze()) -- рабочая file://-
    # ссылка в отчёте, тот же паттерн, что encrypted_archive_paths выше.
    disputed_paths: list = field(default_factory=list)
    unreadable_paths: list = field(default_factory=list)
    n_signature_mismatch: int = 0
    n_raw_without_jpeg: int = 0
    n_jpeg_without_raw: int = 0
    n_albums_detected: int = 0
    n_dump_items: int = 0
    # PROMPT_archive_report.md, 1.2а: тот же счётчик, но по папке -- для Листа 3 report.html
    # ("в каких папках свалка"), n_dump_items выше остаётся плоским int для существующего
    # консольного вывода/analyze_report.csv, эта разбивка аддитивна, ничего не заменяет.
    dump_items_by_folder: Counter = field(default_factory=Counter)
    # Живая находка пользователя, 2026-08-24 (Паспорт архива): _render_passport_integrity()
    # (report.py) показывает только счётчик ("N файлов лежат не внутри альбома/даты"), не
    # пути -- в отличие от архивов/битых файлов/дублей, у которых полный список путей уже есть
    # в passport_detail.xlsx (см. _build_passport_detail_rows()). dump_items_by_folder выше
    # агрегирует по папке (для Листа 3 обычного прогона), не хранит имя файла -- этого
    # достаточно для report.html, но недостаточно, чтобы найти КОНКРЕТНЫЙ файл в паспорте.
    # item.origin_display -- тот же формат пути, что уже хранят exact_dup_edges/near_dup_edges
    # (см. их докстринг), _passport_abs_path()/_passport_normalize_dest() понимают его как есть.
    dump_item_paths: list = field(default_factory=list)
    # SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата в analyze-отчёте): YY/QQ --
    # медиафайлы, которые нашли альбом, и медиафайлы, которым альбом не нашёлся (разложатся по
    # дате) -- фильтр по item.ftype in ("image", "raw", "video"), в отличие от n_dump_items/
    # dump_items_by_folder выше, которые считают ЛЮБОЙ немедийный "лишний" файл тоже.
    n_media_in_albums: int = 0
    n_media_by_date: int = 0
    # ZZ -- отдельный Counter, НЕ dump_items_by_folder: тот считает любой файл без альбома
    # (в т.ч. немедийный, "россыпь"/Sheet3), этот -- только медиафайлы без альбома, для числа
    # "ZZ обычных папок" в analyze-отчёте (len() счётчика).
    bydate_media_by_folder: Counter = field(default_factory=Counter)
    # SESSION-HANDOFF.txt, "большой разбор report.html", пункт A (дерево структуры архива) --
    # плоский счётчик на "бакет" (не на реальную папку -- см. run_analyze() докстринг про
    # month-granularity/RAW/_Unsorted-упрощение). 2026-08-14: рендер дерева ("Структура
    # архива"/"Структура источника") больше НЕ читает это поле -- заменён на source_tree_counts_
    # image/video/raw ниже (реальная структура, не предсказанный бакет) -- tree_folder_counts
    # остаётся ТОЛЬКО ради report.py:_deep_nested_albums() (проверка "Целостности архива" в
    # паспорте на глубоко вложенные альбомы), tree_folder_bytes убран целиком -- байты по
    # бакету нигде больше не читались (report.py:_build_archive_tree() был единственным
    # потребителем, удалён тем же заходом).
    tree_folder_counts: Counter = field(default_factory=Counter)
    # 2026-08-14, прямая просьба пользователя: "дерево, по аналогии с паспортом" для analyze --
    # НЕ то же самое дерево, что tree_folder_counts выше (тот -- ПРЕДСКАЗАННАЯ раскладка
    # Albums/ByDate/RAW/_Unsorted, бакет по типу назначения, не по реальному месту на диске).
    # Это -- РЕАЛЬНАЯ структура SOURCE как она сейчас физически лежит (папки/архивы как узлы,
    # архив -- отдельная ветка тем же принципом, что и папка). Ключ -- "/"-путь РОДИТЕЛЬСКОГО
    # узла (папки/архива) файла, см. _source_tree_parent_key(); значение -- число найденных
    # ПРЯМО в этом узле (не рекурсивно -- вложенность строит report.py во время рендера, тот же
    # принцип, что и у tree_folder_counts). Считает ВСЕ найденные медиафайлы, включая битые/
    # нечитаемые (по прямому решению пользователя -- "найденные", не "доступные для архива") --
    # см. точку инкремента в run_analyze() (сразу после is_dvd_unit_item). Три отдельных
    # Counter'а, не один (2026-08-14, прямая просьба пользователя: дерево показывает разбивку
    # "N фото/N видео/N raw" на каждый узел, не общую сумму) -- тот же принцип, что уже у
    # format_counts_image/format_counts_raw/format_counts_video выше.
    source_tree_counts_image: Counter = field(default_factory=Counter)
    source_tree_counts_video: Counter = field(default_factory=Counter)
    source_tree_counts_raw: Counter = field(default_factory=Counter)
    # PROMPT_archive_report.md, Лист 1/2 report.html: гистограмма "фото по годам"/"самое
    # старое фото" для WORKDIR-уровня -- resolve_date() уже вызывается для любого режима
    # (даже analyze-quick), просто раньше date_value нигде не сохранялся за пределами
    # текущей итерации цикла.
    dates_by_year: Counter = field(default_factory=Counter)
    dates_by_year_month: Counter = field(default_factory=Counter)
    oldest_date: object = None  # datetime | None
    oldest_display: str = None
    # SESSION-HANDOFF.txt, 2026-07-31: analyze_batch() уже читает GPSLatitude/GPSLongitude в
    # той же exiftool-пачке, что и дату/камеру (photosort_win.py:2597) -- place_for_gps() поверх
    # уже прочитанных координат почти бесплатен (кэш по координатам, см. _place_cache), поэтому
    # analyze/analyze-quick/analyze-full и [4] Паспорт архива (сам через run_analyze()) теперь
    # тоже получают "Географию" в отчёте, не только реальная сборка архива.
    cities: Counter = field(default_factory=Counter)
    # Пункт E ("большой разбор report.html", SESSION-HANDOFF.txt): та же exiftool-пачка, что
    # уже читает дату/GPS выше (cities) -- rec.camera тоже уже прочитан, просто не сохранялся
    # никуда за пределами текущей итерации. Файлы без EXIF-камеры (пустая строка/None) -- не
    # добавляются вовсе, не искусственная категория "неизвестно" (см. report.py:
    # _top_cameras_chart()).
    cameras: Counter = field(default_factory=Counter)
    tier_counts: Counter = field(default_factory=Counter)  # "A"/"B"/"C"/"D" -> count
    # mode="analyze" (полный проход хеширования -- точный/near-дедуп; 2026-08-04: достижимо
    # через run_passport()'s self_scan=True -- CLI "analyze --target ..." см. CLI_MODES/
    # _main()):
    n_exact_dupes: int = 0
    n_diff_name_same_content: int = 0
    n_near_dupes: int = 0
    predicted_unique_count: int = 0
    predicted_unique_bytes: int = 0
    # PROMPT_archive_report.md, 1.2б: рёбра near-dup-графа, та же форма строк, что у
    # near_dup_edges.csv / CollectingRunLogs.rows["near_dup_edges"] -- decide() уже вычисляет
    # matched_dest/hamming для analyze/analyze-full, раньше просто не сохранялось никуда.
    near_dup_edges: list = field(default_factory=list)
    # Речь пользователя, 2026-08-02 (задача 4, доработка Паспорта архива): та же идея, что
    # near_dup_edges выше, только для точных (не near-) дублей -- "Дублей внутри архива нет"
    # раньше был голым числом (n_exact_dupes), без единого пути, по которому это можно
    # проверить/найти. {"dest": item.origin_display, "matched_dest": existing.dest_path} --
    # тот же union-find-кластеризуемый граф, что и у near_dup_edges (см. report.py
    # _cluster_near_dup(), переиспользуется как есть для обоих видов рёбер).
    exact_dup_edges: list = field(default_factory=list)
    # Речь пользователя, 2026-08-02 (задача 3): "N файлов дата определена лишь приблизительно
    # или не определена вовсе" (n_approx_or_missing = tier_counts[C]+tier_counts[D]) не
    # различал Albums/ (дата ни на что не влияет по RULES.md, блок UNDATED -- место файла в
    # Albums/ определяется структурой исходных папок, не датой) и ByDate/ (там точность даты
    # РЕШАЕТ, в какую подпапку попадёт файл -- единственный случай, где это действие вообще
    # имеет смысл). Тот же принцип, что уже применён к report.py's _cluster_dates_review()/
    # _cluster_undated() для обычного пополнения (фильтр "не Albums") -- здесь дублируется на
    # уровне AnalyzeStats, т.к. паспорт не использует per-file dest/tier построчно (дешевле
    # одного дополнительного счётчика, чем тащить весь список путей ради паспорта).
    n_tier_cd_bydate: int = 0
    # SESSION-HANDOFF.txt, 2026-08-09 (боевой прогон, пятая находка): объединённый чек-лист
    # "получили дату неточно или не получили вовсе" (report.py) разбивает итог по тирам B/C/D
    # РАЗДЕЛЬНО (не по 5 пересекающимся полям n_no_exif_date/etc выше -- те не складываются в
    # чистую сумму, см. находку) -- n_tier_cd_bydate выше считает только C+D вместе, без B, и
    # используется ДРУГИМ местом (_render_passport_integrity(), не трогать). Три новых счётчика
    # ниже -- та же семантика "тир X И НЕ в альбоме", что и n_tier_cd_bydate, просто на тир
    # тоньше -- считаются в том же цикле run_analyze(), заодно с tier_counts/n_tier_cd_bydate.
    n_tier_b_bydate: int = 0
    n_tier_c_bydate: int = 0
    n_tier_d_bydate: int = 0
    # ROADMAP.md, analyze как "2 части", часть 2 ("на этом диске найден архив PhotoArchive"):
    # top-level -- найденные архивы, НЕ вложенные в другой найденный архив (см.
    # classify_found_archives()). Раунд 44 ревью (придирка) -- парный field "nested" (вложенные
    # архивы, улика ручного вмешательства) удалён 2026-08-07: classify_found_archives() всё ещё
    # вычисляет и возвращает его (используется _render_found_archives() напрямую -- тестами,
    # см. tests/test_found_archives.py), но AnalyzeStats-поле нигде не читалось вне тестов самого
    # поля -- мёртвый пассажир.
    found_archive_top_level: list = field(default_factory=list)
    # SESSION-HANDOFF.txt п.9 (2026-08-05, боевой прогон): "Объём по категориям" (байты по
    # фото/raw/видео) отсутствовал в analyze-отчётах -- total_bytes/n_images/n_raw/n_videos уже
    # считались, но не байты ПО категории. item.size уже в руках там же, где растут
    # n_images/n_raw/n_videos (см. run_analyze()) -- просто ещё один Counter рядом.
    bytes_by_kind: Counter = field(default_factory=Counter)
    # SESSION-HANDOFF.txt п.4 (2026-08-05, боевой прогон): общее число объектов (папка+архив,
    # ProgressReporter.object_count -- см. её докстринг за разбором, чем это НЕ является) +
    # разбивка файлов по месту ("root"/"folder"/"archive", из item.rel_path/
    # item.archive_boundary_idx в цикле run_analyze()). Заодно чинит неточность фразы «россыпь +
    # N архивов» (report.py, _render_cta_block()) -- та включалась просто по archives_found>0,
    # без проверки, есть ли вообще что-то ВНЕ архивов (россыпь может отсутствовать полностью).
    n_objects_total: int = 0
    files_by_location: Counter = field(default_factory=Counter)
    # SESSION-HANDOFF.txt (2026-08-05, боевой прогон, разбор накопления п.3а): та же разбивка
    # по месту, но байтами, не штуками -- тот же приём, что уже применён к bytes_by_kind рядом
    # с n_images/n_raw/n_videos (см. run_analyze()) -- item.size уже в руках там же, где растёт
    # files_by_location.
    bytes_by_location: Counter = field(default_factory=Counter)
    # SESSION-HANDOFF.txt, 2026-08-11 ("большой разбор report.html", Задача A) -- глубина
    # вложенности папок SOURCE, макс. по всем встреченным item (архивные item тоже считаются --
    # их rel_path включает сегмент архива, см. SourceItem.archive_boundary_idx). Одно
    # сравнение на файл, та же ось стоимости, что и у files_by_location рядом.
    #
    # 2026-08-11, речь пользователя (живой боевой прогон по C:\ целиком): считалась по ЛЮБОМУ
    # файлу, включая совершенно не-медийные (код, кэши, установщики) -- на сканировании всего
    # диска число разъезжалось с реальной глубиной, на которой лежат фото/видео. Теперь считает
    # ТОЛЬКО подтверждённые медиафайлы (см. run_analyze()'s is_media).
    max_depth: int = 0
    # Тот же боевой прогон, тот же довод, что у max_depth выше -- сколько РАЗЛИЧНЫХ папок (по
    # полному пути, вся цепочка предков) реально ведут хотя бы к одному подтверждённому
    # медиафайлу, не общее число папок-объектов, встреченных обходом (то, что даёт
    # n_objects_total - archives_found). См. run_analyze()'s _folders_with_media.
    n_folders_with_media: int = 0
    # Задача A, п.2: те же n_images/n_raw/n_videos/bytes_by_kind выше, но БЕЗ файлов, которые
    # физически уедут в _Unsorted (битые/пустые/не прочитанные) -- "found" (счётчики выше)
    # считают вообще всё найденное, "available" ниже -- то, что реально доступно для архива
    # (ТЗ пользователя, Раздел 2 report.html). Инкрементируются в ТОЙ ЖЕ точке цикла, что и
    # прежние счётчики available-версии не пересчитывают то, что уже посчитано -- просто
    # позже по коду, после отсева broken/zero/read_error.
    n_images_available: int = 0
    n_raw_available: int = 0
    n_videos_available: int = 0
    bytes_by_kind_available: Counter = field(default_factory=Counter)
    # Задача A, п.3: tier_counts выше включает RAW (дата RAW обычно зеркалит JPEG-партнёра,
    # см. RAW_LAYOUT=mirror) -- ТЗ пользователя просит "надёжность дат" без RAW отдельно.
    # Аддитивный Counter, tier_counts не трогается (см. _render_passport_integrity()).
    tier_counts_no_raw: Counter = field(default_factory=Counter)
    # Задача A, п.4: dates_by_year выше -- один общий Counter на все типы; ТЗ просит фото/видео
    # раздельно, RAW не показывается вовсе (та же RAW-логика, что у tier_counts_no_raw).
    dates_by_year_photo: Counter = field(default_factory=Counter)
    dates_by_year_video: Counter = field(default_factory=Counter)
    # Задача A, п.5: топ-5 форматов по расширению, раздельно по категории -- DVD-юнит-файлы
    # (.VOB/.IFO/.BUP) исключены из format_counts_video (см. is_dvd_unit_item в run_analyze()) --
    # тот же довод, что и у ftype=="video" для DVD выше: .IFO/.BUP -- служебные файлы навигации,
    # не видеоформат, с которым пользователь ассоциирует "видео"; сам .VOB считается отдельно,
    # см. dvd_vob_count ниже.
    format_counts_image: Counter = field(default_factory=Counter)
    format_counts_raw: Counter = field(default_factory=Counter)
    format_counts_video: Counter = field(default_factory=Counter)
    # Задача A, п.6: число .vob-файлов на всех DVD-юнитах (VIDEO_TS) вместе -- ТЗ пользователя:
    # "DVD по числу .vob", не по имени расширения вперемешку с .IFO/.BUP. Не меняет гранулярность
    # тика прогресса (DVD-юнит по-прежнему тикает как единое целое, см. defer_media_object_tick) --
    # это отдельный счётчик для отчёта, не для ProgressReporter.
    dvd_vob_count: int = 0
    # Речь пользователя, 2026-08-11: "DVD -- это структура VIDEO_TS (считается как единица) и
    # отдельно стоящий vob -- просто отдельный формат видеофайла" -- dvd_vob_count выше считает
    # ФРАГМЕНТЫ (.vob-файлы внутри юнитов), не сами DVD-диски. n_dvd_units -- число РАЗЛИЧНЫХ
    # ФИЗИЧЕСКИХ папок VIDEO_TS, найденных в SOURCE (по SourceItem.dvd_source_tree_key -- см.
    # run_analyze()), та единица подсчёта, которую пользователь имеет в виду под "DVD".
    # Отдельностоящий .vob (НЕ внутри VIDEO_TS) сюда не попадает вовсе -- он уже считается как
    # обычный формат в format_counts_video (см. VIDEO_EXTS/is_dvd_unit_item -- этот путь не
    # менялся, работал правильно и раньше).
    #
    # 2026-08-14, живая находка (боевой прогон пользователя): раньше дедуплицировалось по
    # item.dvd_unit_fingerprint (хеш СОДЕРЖИМОГО юнита) -- корректно для решения "копировать ли
    # повторно при реальной сборке" (RULES.md, "объединение DVD-папок недопустимо" -- второй
    # физический диск с тем же содержимым не копируется повторно), но НЕ для этого счётчика:
    # два РАЗНЫХ физических диска с ОДИНАКОВЫМ содержимым (например, две копии одной и той же
    # болванки) схлопывались в "1 диск", хотя в SOURCE их физически два -- диаграмма "Топ
    # форматов — видео" показывала обманчивое "12 файлов (1 диск)" вместо "12 файлов (2
    # диска)". dvd_source_tree_key -- РЕАЛЬНЫЙ путь узла (см. её докстрин), различается для
    # разных физических папок даже при полностью идентичном содержимом -- та величина, что
    # реально нужна для "сколько DVD-папок физически нашлось", не "сколько из них не повторяют
    # друг друга по содержимому".
    n_dvd_units: int = 0
    # Задача A, п.7: n_archives_encrypted выше -- только password_protected; ТЗ пользователя
    # хочет отдельно "архив не открылся" (read_error -- I/O-сбой при открытии/распаковке;
    # bomb_suspected -- превышена глубина вложенности, отказ намеренный) -- та же by-status
    # логика, что у n_archives_encrypted/encrypted_archive_paths рядом (walker.archive_logs,
    # см. конец run_analyze()).
    n_archives_failed: int = 0
    failed_archive_paths: list = field(default_factory=list)
    # Задача A, п.8: то же место в отчёте, что disputed_paths/unreadable_paths выше (путь как
    # плоская строка) -- но плоская file://-склейка (_analyze_source_abs_path()) уже сегодня
    # даёт нерабочую ссылку для файлов ИЗНУТРИ архива (SESSION-HANDOFF.txt, 2026-08-11, живая
    # находка: item.origin_display для архивных item -- "витринная" строка вида "Album.zip →
    # DCIM/IMG.jpg", физический файл лежит во временной tmp_extract-папке, к моменту чтения
    # отчёта уже вычищенной). disputed_records/unreadable_records -- та же информация СТРУКТУРНО
    # (item.archive_boundary_idx уже даёт признак "изнутри архива" бесплатно, просто раньше не
    # сохранялся) -- готовит фикс ложных ссылок для report.py (Задача D), сами
    # disputed_paths/unreadable_paths выше не убираются и не меняются этим полем, чтобы не ломать
    # текущий (ещё не переделанный) рендер Раздела 3 между Задачами A и D.
    disputed_records: list = field(default_factory=list)
    unreadable_records: list = field(default_factory=list)
    # SESSION-HANDOFF.txt, 2026-08-11 (Windows-сессия, отложенная задача): analyze не сообщал о
    # непрочитанных папках вообще -- walker.listdir_failed (SourceWalker._walk_dir()'s except
    # OSError) собирается ОДИНАКОВО в обоих режимах, но пробрасывался в отчёт только на пути
    # реальной сборки (run_for_source()/_run_impl()). run_analyze() теперь читает тот же список
    # (см. конец run_analyze()) -- та же информация, тот же паттерн, что и остальные *_paths
    # выше, не новая абстракция.
    # ИЗВЕСТНЫЙ КРАЕВОЙ СЛУЧАЙ (осознанно не устранён, см. SESSION-HANDOFF.txt, 2026-08-12):
    # если listdir() падает ВНУТРИ временно распакованного архива (cfg.tmp_extract), путь здесь
    # -- реальный абсолютный путь на момент захвата, но tmp_extract безусловно вычищается до
    # возврата из run_analyze() (_cleanup_own_tmp_extract_entries) -- file://-ссылка в отчёте
    # будет мёртвой. Тот же класс проблемы, что уже решён для disputed_records/unreadable_records
    # (архивный путь текстом "внутри архива X", не мёртвой ссылкой) -- здесь НЕ реплицирован
    # (не тот же archive_boundary_idx/origin-trail), маловероятный edge case (tmp_extract
    # только что создан 7z/UnRAR, редко успевает сломаться) -- пользователь согласился оставить.
    listdir_failed_paths: list = field(default_factory=list)


# Речь пользователя, "какие есть варианты сделать паспорт быстрее" (2026-08-02): вместе с
# analyze_batch()'s новым tags_by_path= -- run_analyze()'s единственный потребитель этой пары.
# batch_size=200 -- то же число, что и default exiftool_batch()'s собственного -@argfile-чанка,
# так что накопленный здесь батч укладывается в ОДИН спавн exiftool, не в несколько.
_ANALYZE_EXIF_PREFETCH_BATCH_SIZE = 200


def _exif_cache_ready(cache: dict, item) -> bool:
    """True -- archive_cache уже содержит валидные (size+mtime совпадают) EXIF-поля для этого
    item, exiftool звать не нужно вовсе. Речь пользователя, 2026-08-02 ("почему Фаза 1
    быстрая, а паспорт медленный -- разве не один алгоритм?"): Фаза 1 никогда не читает EXIF
    (ей нужны только sha256/pHash для пула дедупа), паспорт читает БЕЗУСЛОВНО на каждый файл,
    даже при полном попадании по хешу -- раньше дата/камера/GPS не кэшировались вовсе.
    exif_cached (индекс 8) -- отдельный от самого хеш-попадания флаг: файл может быть
    известен кэшу по хешу (старая строка, из ДО этой правки, или из index_archive(), которая
    exif не пишет), но ещё ни разу не проверен на EXIF -- тогда exiftool всё равно нужен."""
    cached = cache.get(item.read_path) if cache else None
    return bool(cached and cached[0] == item.size and abs(cached[1] - item.mtime) < 1e-6
                and len(cached) > 8 and cached[8])


def _tag_prefetch_pairs(items: list, cache: dict, log=print) -> list:
    """[(item, tags_by_path)] для одного батча -- tags_by_path один и тот же словарь для всех
    item в батче (общий результат одного exiftool_batch() на весь батч), не по одному на item.

    cache -- см. _exif_cache_ready(): item, для которых EXIF уже в кэше, вообще не попадают в
    paths ниже -- tags_by_path для них останется пуст, тот же сигнал "бери из кэша", что
    analyze_batch() уже понимает для sha256/phash (см. её же cache= параметр)."""
    paths = [it.read_path for it in items
             if it.ftype in ("image", "raw", "video") and not _exif_cache_ready(cache, it)]
    tags = exiftool_batch(paths, log=log) if paths else {}
    return [(it, tags) for it in items]


def _walk_with_exif_prefetch(items_iter, tmp_extract_dir: str, batch_size: int, cache: dict = None,
                              log=print, rate_hint_cb=None):
    """Оборачивает обход SourceWalker.walk(): yield (item, tags_by_path) в ТОМ ЖЕ порядке,
    что и исходный обход, но exiftool зовётся одним батч-спавном на до batch_size файлов
    вместо спавна на каждый -- при этом каждый item по-прежнему обрабатывается вызывающим
    кодом строго по одному, как и раньше (это НЕ read-ahead для хеширования/копирования,
    только для чтения EXIF-тегов).

    Файлы ВНУТРИ распакованного архива (item.read_path под cfg.tmp_extract) намеренно НЕ
    накапливаются в батч -- см. предупреждение в _run_impl()'s SOURCE-цикле (:6002-6006,
    "NB: items are analyzed and placed one at a time"): SourceWalker чистит tmp_extract
    архива в finally сразу же, как обход продвигается ЗА последний item этого архива --
    отложенная (батчем) обработка более раннего item того же архива рисковала бы читать
    уже удалённый физический файл. Здесь та же защита: как только встречен archive-item,
    любой накопленный батч НЕ-архивных item немедленно сбрасывается (тегируется и
    отдаётся), сам archive-item тегируется и отдаётся В ОДИНОЧКУ сразу же (без накопления
    следующих item поверх него) -- обход никогда не убегает вперёд дальше уже увиденного
    archive-item, ровно как и в исходном небатчинговом коде.

    cache=None (по умолчанию) -- ни один item не считается exif-закэшированным
    (_exif_cache_ready() безусловно False), exiftool зовётся для всех, как и раньше --
    вызывающий код (run_analyze() для analyze-full/обычного analyze без self_scan) просто не
    передаёт cache, если archive_cache недоступен.

    rate_hint_cb (2026-08-06, боевой прогон -- "скорость всегда 0"; текстовый transient_op_cb,
    который раньше стоял рядом с этим параметром -- "чтение метаданных, файлов: N…" -- убран
    2026-08-07 по прямой просьбе пользователя, статус-строку заменяет общее "занято" время
    прогона, см. ProgressReporter._build_two_line_status()): пока идёт сбор батча + сам вызов
    exiftool_batch() на весь батч сразу -- НИ ОДИН update() не происходит (yield ещё не
    случился), реальное время этого промежутка иначе осело бы как ~0 в wall-clock между
    yield'ами уже готового батча. rate_hint_cb(секунд_на_файл, N) -- сообщает бару синтетическую
    скорость на N ближайших update() (см. ProgressReporter.set_batch_rate_hint()), только для
    батчей БОЛЬШЕ 1 файла (одиночный archive-item и так измеряется честно обычным путём).

    "объектов %" здесь БОЛЬШЕ НЕ тикает (2026-08-18, боевой прогон -- источник с горой мелких
    media-файлов + немного крупных/видео рядом): раньше object_progress_cb тикал ВЕСЬ батч
    ОДНИМ вызовом на ОТПРАВКУ (до exiftool_batch(), см. REVIEW-HANDOFF.md, Раунд 86 follow-up)
    -- заявленная неточность "максимум один батч вперёд реального прогресса" на практике
    оказалась НЕ маленькой: если батч содержит видео, video_duration_and_resolution() (ffprobe)
    для НИХ вызывается ПОЗЖЕ, поштучно, в run_analyze()'s основном цикле (analyze_batch()) --
    сам батч уже тикнул как "готово" за секунды до того, как эти видео реально дощупаны, и если
    это ПОСЛЕДНИЙ батч (или единственные видео источника попали именно в него), "обработано
    объектов 100%" держится клэмпом (min(X/Y*100, 100.0)) весь остаток прогона, пока ffprobe
    молча дообрабатывает эти видео -- тот же класс бага, что чинили Раунды 96-99, просто на
    уровень ниже (там весило поровну "медиа vs немедиа", здесь -- "лёгкое медиа vs дорогое
    медиа"). Тик теперь -- в run_analyze() ПОСЛЕ analyze_batch() для каждого item (её докстринг,
    поиск "обработано объектов" там же) -- честно отражает реальное завершение, включая ffprobe."""
    tmp_prefix = tmp_extract_dir + os.sep
    pending = []
    for item in items_iter:
        if item.read_path.startswith(tmp_prefix):
            if pending:
                yield from _flush_exif_prefetch_batch(pending, cache, log, rate_hint_cb)
                pending = []
            yield from _tag_prefetch_pairs([item], cache, log=log)
            continue
        pending.append(item)
        if len(pending) >= batch_size:
            yield from _flush_exif_prefetch_batch(pending, cache, log, rate_hint_cb)
            pending = []
    if pending:
        yield from _flush_exif_prefetch_batch(pending, cache, log, rate_hint_cb)


def _flush_exif_prefetch_batch(pending: list, cache, log, rate_hint_cb=None):
    """См. rate_hint_cb в _walk_with_exif_prefetch(). Засекает реальное время вызова
    exiftool_batch() (внутри _tag_prefetch_pairs()) и передаёт средний секунд/файл в
    rate_hint_cb, для батчей больше 1 файла.

    "объектов %" здесь больше не тикает (2026-08-18, см. докстринг _walk_with_exif_prefetch())
    -- тикает run_analyze() сама, поштучно, после реальной обработки каждого item."""
    n = len(pending)
    t0 = time.time()
    try:
        yield from _tag_prefetch_pairs(pending, cache, log=log)
    finally:
        if rate_hint_cb is not None and n > 1:
            rate_hint_cb((time.time() - t0) / n, n)


def _is_windows_abs_path(s: str) -> bool:
    """Платформонезависимая проверка "это уже абсолютный Windows-путь" -- НЕ os.path.isabs()
    (на POSIX-раннере, где os.path == posixpath, тот считает абсолютным только путь с
    ведущим "/", "C:\\Users\\..." дал бы False -- тот же класс несовместимости, от которого
    уже есть защита у _win_dirname()/_win_basename() в report.py). Буквенный диск ("C:\\...")
    или UNC ("\\\\server\\share\\...") -- единственные формы, которые реально возвращает
    _handle_dvd_unit()'s disp_base (см. _analyze_source_abs_path())."""
    return (len(s) >= 3 and s[1] == ":" and s[2] == "\\") or s.startswith("\\\\")


def _analyze_source_abs_path(cfg: Config, item) -> str:
    """SESSION-HANDOFF.txt, 2026-08-09 (задачи 4/6): реальный абсолютный путь для
    disputed_paths/unreadable_paths (рабочая file://-ссылка в отчёте) -- item.origin_display
    сам по себе НЕ включает корень SOURCE (см. его докстринг в SourceItem) и использует "/" как
    разделитель (POSIX-style, тот же принцип, что item.rel_path -- см. комментарии в
    _walk_dir()), НЕ os.sep -- голая os.path.join(cfg.source, item.origin_display) без замены
    разделителя дала бы смешанный путь вида "F:\\Photos\\2015/Crimea/IMG_1234.jpg": сама
    file://-ссылка это бы пережила (_file_link_or_text() всё равно заменяет "\\" на "/" целиком
    для href), но _win_dirname()/_win_basename() (группировка по папке, отображаемое имя файла)
    расщепляют только по "\\" -- без нормализации здесь "базовым именем" ошибочно оказалась бы
    "2015/Crimea/IMG_1234.jpg" целиком, а не только "IMG_1234.jpg".

    REVIEW-HANDOFF.md, Раунд 80 [ЗАМЕЧАНИЕ]: os.path.join() здесь -- та же ошибка, от которой
    уже есть документированная защита в report.py (_win_dirname()/_win_basename(), см. их
    докстрины) -- os.path на не-Windows раннере (public-репозиторий гоняет tests/ на
    ubuntu-latest в CI) это posixpath, который "\\" не считает разделителем: результат
    join() на POSIX -- ".../NewBatch/Sub\\broken.jpg" одним куском, не тот же путь, что дал бы
    os.path.join(source, "Sub", "broken.jpg") на Windows. Ручная склейка через f-string (тот
    же принцип, что rpartition("\\") у _win_dirname()) даёт identичный на Windows результат
    (единственная целевая платформа программы), но остаётся корректной строкой и на POSIX --
    сам путь всё равно никогда не читается с диска POSIX-раннером, только сравнивается как
    текст в тестах.

    Живая находка (report.html, боевой прогон analyze без --target, 2026-08-09):
    item.origin_display НЕ всегда SOURCE-относителен, вопреки исходному предположению этой
    функции выше -- SourceWalker._handle_dvd_unit() (VIDEO_TS/DVD-юнит) строит его как
    f"{disp_base}/{rel}", а disp_base ЗА ПРЕДЕЛАМИ архива -- это cur_dirpath, УЖЕ абсолютный
    путь (см. её докстринг и _walk_dir()'s вызывающий код). Слепое приклеивание cfg.source к
    уже-абсолютному пути давало "C:\\C:\\Users\\..." -- нерабочую file://-ссылку в отчёте.
    _is_windows_abs_path() -- дешёвая проверка ДО склейки, покрывает и disputed_paths, и
    unreadable_paths (оба вызывающих места используют эту же функцию)."""
    rel = item.origin_display.replace("/", "\\")
    if _is_windows_abs_path(rel):
        return rel
    return cfg.source.rstrip("\\") + "\\" + rel


def _analyze_dispute_record(cfg: Config, item) -> dict:
    """AnalyzeStats.disputed_records/unreadable_records -- структурная версия
    _analyze_source_abs_path() выше (см. её докстринг за разбором самого бага, который эта
    функция готовит починить в report.py, Задача D): item.archive_boundary_idx отличает файл
    ИЗНУТРИ архива (для него абсолютного пути на диске в принципе не существует -- физический
    файл жил во временной tmp_extract-папке, уже вычищенной к моменту чтения отчёта) от файла,
    реально лежащего на SOURCE. "display" -- item.origin_display в обоих случаях (для файла на
    диске report.py его не использует, abs_path уже достаточен; для архивного -- это и есть
    "витринная" строка вида "Album.zip → DCIM/IMG.jpg", из которой report.py строит текст без
    file://-ссылки)."""
    if item.archive_boundary_idx is not None:
        return {"in_archive": True, "abs_path": None, "display": item.origin_display}
    return {"in_archive": False, "abs_path": _analyze_source_abs_path(cfg, item),
            "display": item.origin_display}


def run_analyze(cfg: Config, mode: str, log=print, self_scan: bool = False) -> AnalyzeStats:
    """mode: analyze-quick (метаданные, без SHA/pHash; CLI -- "analyze --source ...", см.
    _CLI_ANALYZE_MODE_MAP) | analyze (+ полный проход хеширования, точный+near-дедуп ВНУТРИ
    источника -- достижимо через run_passport()'s self_scan=True, CLI -- "analyze --target
    ...", см. CLI_MODES/_main()).

    Переиспользует РЕАЛЬНЫЙ конвейер (SourceWalker, analyze_batch, resolve_date, find_album,
    decide()+Pool) вплоть до Фазы 4.5 включительно -- решения о дедупе и дате считаются той же
    логикой, что и настоящая сборка, только результат никогда не материализуется на диск
    (Фаза 5 не вызывается вовсе).

    self_scan=True -- run_passport() указывает cfg.source=TARGET (уже собранный архив, не
    "сырой" источник) -- два места ниже (find_album()/dump-счётчик, resolve_date()) получают
    поправку на то, что часть "сигналов", которые эта же логика доверчиво читает на обычном
    SOURCE, на TARGET на самом деле собственная разметка программы с прошлого прогона, а не
    независимое доказательство (живой репорт пользователя, 2026-08-01, см. докстринг
    resolve_date()/_PASSPORT_SELF_SCAN_RECOGNIZED_TOP ниже)."""
    # Живая просьба пользователя, 2026-08-24 ("раньше такое было"): та же разделитель+шапка
    # параметров запуска, что и у _run_impl() (сборка/пробный прогон) -- Анализ/Паспорт архива
    # раньше не печатали её вовсе. self_scan+mode=="analyze"/mode=="analyze-quick" -- те же два
    # ярлыка режима, что уже использует _BARE_LAUNCH_MODE_LABELS ("Паспорт архива"/"Сканирование
    # источника") -- голый mode=="analyze" без self_scan достижим только через полный CLI
    # ("analyze --source ..." без --target), не имеет своего пункта голого меню, отдельный ярлык.
    if self_scan and mode == "analyze":
        _mode_label = "Паспорт архива"
    elif mode == "analyze-quick":
        _mode_label = "Сканирование источника"
    else:
        _mode_label = "Анализ источника"
    _log_run_start_header(_mode_label, cfg, log=log)
    # Остатки чужого прошлого прерванного прогона (Ctrl+C/крах) -- та же проверка, что
    # _run_impl()'s Фаза 0 делает для реальной сборки/--dry-run, раньше отсутствовала здесь
    # вовсе (живая находка пользователя, 2026-08-09). Симметричный вызов после основного цикла
    # ниже подчищает то же самое для ЭТОГО прогона, если он сам будет прерван.
    _cleanup_own_tmp_extract_entries(cfg, log=log)
    stats = AnalyzeStats(mode=mode)
    date_ctx = DateContext()
    album_names = set()

    pool = Pool()

    # Задача 7 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): mode=="analyze" с
    # self_scan=True -- это и есть "Паспорт архива" (run_passport()), cfg.source указывает на
    # уже собранный архив. Обычная сборка ТОГО ЖЕ архива уже посчитала и закэшировала эти же
    # SHA-256/pHash для этих же путей (archive_cache -- собственный файл ВНУТРИ архива, см.
    # archive_cache_db_path(), задача 2 речи пользователя 2026-08-02) -- и через
    # index_archive() при индексации существующего архива (Фаза 1), и через
    # _seed_archive_cache() сразу после place_file()
    # для только что дописанных файлов. Паспорт эти хеши раньше игнорировал и считал заново
    # с нуля на КАЖДЫЙ файл. mode=="analyze-full" НЕ получает этот кэш -- та ветка хеширует
    # SOURCE (новые, ещё не архивированные файлы), для которых archive_cache заведомо пуст,
    # читать/писать таблицу впустую нет смысла (bin/README-BIN.md не про это -- это про
    # архивный кэш work.db, не про exiftool/7z/ffmpeg).
    #
    # REVIEW-HANDOFF.md, Раунд 52 [ЗАМЕЧАНИЕ]: гейт раньше проверял только mode=="analyze" --
    # то же самое строковое значение mode использует и обычный документированный CLI-режим
    # `analyze` (run_analyze_for_source(), self_scan=False по умолчанию, read-only предпросмотр
    # произвольного SOURCE, TARGET не читается/не пишется, см. README.md:244) -- без self_scan в
    # условии он тоже читал и писал archive_cache, засоряя общий work.db ключами вида "путь
    # SOURCE-файла", которого в архиве никогда не было. self_scan=True (Паспорт архива) --
    # единственный сценарий, где cfg.source реально указывает на уже собранный архив и где эти
    # пути валидны как ключи archive_cache.
    # Речь пользователя, 2026-08-02: cfg.source -- реальный архив под self_scan=True (cfg.target
    # здесь -- фиктивный _NO_TARGET_PLACEHOLDER, см. run_passport()), поэтому кэш открывается
    # ВНУТРИ него (archive_cache_db_path(cfg.source)), не в work.db рядом с .exe -- та же копия
    # архива, прогнанная через другую копию .exe, теперь видит ту же самую историю хешей.
    # _open_archive_cache_conn() возвращает None, если у cfg.source ещё даже нет служебной
    # папки (например self_scan запущен на папке, которая архивом ещё не является) -- тогда
    # работаем без кэша, как и раньше при archive_hash_cache=False.
    archive_cache = None
    archive_cache_conn = None
    if mode == "analyze" and self_scan and cfg.archive_hash_cache:
        archive_cache_conn = _open_archive_cache_conn(cfg.source)
    if archive_cache_conn is not None:
        archive_cache = {}
        for row in archive_cache_conn.execute(
            "SELECT path, size, mtime, sha256, phash, duration, width, height, bitrate, "
            "exif_cached, exif_dt, exif_dt_source, camera, gps_lat, gps_lon "
            "FROM archive_cache"
        ):
            archive_cache[row[0]] = row[1:]

    progress_desc = {
        "analyze-quick": _ANALYZE_QUICK_PROGRESS_DESC,
        "analyze": _ANALYZE_PASSPORT_PROGRESS_DESC,
    }.get(mode, mode)
    # Речь пользователя, 2026-08-02 ("подумай, как сделать информативной интерактив при
    # построении паспорта"): та же ETA-machinery, что уже есть у Фазы 2 реальной сборки
    # (_quick_media_count_estimate() + object_progress_cb + ProgressReporter(two_line=True)) --
    # ProgressReporter's докстринг раньше явно резервировал two_line ТОЛЬКО за Фазой 2
    # (analyze*/Фаза 1 -- "продолжают использовать... однострочный формат as-is"), это была
    # констатация текущего охвата на момент того редизайна, не запрет расширять. run_analyze()
    # (значит и "Паспорт архива", и CLI analyze/analyze-full) раньше показывал только голый
    # растущий счётчик и скорость, без "сколько ещё осталось" -- на большом архиве (боевой
    # прогон пользователя, 2026-08-02) это оставляло пользователя гадать. Быстрый предпересчёт
    # ДО старта основного бара, синхронно (не фоновым потоком) -- тот же довод, что и у Фазы 2:
    # один физический источник, параллельный обход того же дерева рискует замедлить оба
    # прохода на медленных/сетевых дисках.
    with ProgressReporter(total=None, desc=" Оцениваю объём работы", unit="файл") as est_bar:
        total_estimate = _quick_media_count_estimate(cfg.source, cfg, on_progress=est_bar.update)
    # Без `with`/reindent остального тела: явный close() перед return ниже (см. дальше по
    # функции) -- вызывающий печатает чек-лист сразу после возврата stats.
    bar = ProgressReporter(total=None, desc=progress_desc, unit="файл",
                            two_line=True, total_estimate=total_estimate)
    bar.__enter__()
    walker = SourceWalker(cfg, log=log, object_line_cb=bar.write_object_line,
                           transient_op_cb=bar.set_transient_op,
                           object_progress_cb=bar.add_object_progress,
                           defer_media_object_tick=True, heavy_notice_cb=bar.write_heavy_notice)
    # REVIEW-HANDOFF.md, Раунд 54, замечание 2 + Раунд 55, придирка: батч <= cfg.sample_limit,
    # когда он задан -- без этого прогрев набирал полные _ANALYZE_EXIF_PREFETCH_BATCH_SIZE=200
    # файлов ДО первой проверки sample_limit (она снаружи генератора, физически не может
    # сработать раньше) -- "--sample-limit N" (дешёвый тест на малой выборке, в т.ч. на
    # медленном сетевом источнике) реально тратил exiftool на до 200 файлов вместо N, молча.
    # sample_limit=0 -- "без лимита" (тот же falsy-смысл, что и везде в этой функции).
    prefetch_batch_size = (min(_ANALYZE_EXIF_PREFETCH_BATCH_SIZE, cfg.sample_limit)
                            if cfg.sample_limit else _ANALYZE_EXIF_PREFETCH_BATCH_SIZE)
    walker_iter = _walk_with_exif_prefetch(
        walker.walk(), cfg.tmp_extract, prefetch_batch_size, cache=archive_cache, log=log,
        rate_hint_cb=bar.set_batch_rate_hint)
    # itertools.islice(), не ручной `break` по счётчику: обычный `for` вызвал бы next() НА ОДИН
    # item больше лимита (Python сначала получает значение, потом исполняет тело цикла с
    # проверкой) -- этот лишний next() заставлял бы генератор набирать ЕЩЁ один полный
    # prefetch_batch_size-батч ради одного лишнего item (Раунд 55, придирка: реальная цена была
    # min(200,N)×2, не ×1, при уже применённом фиксе выше). islice() останавливается, не
    # запрашивая следующий item вообще -- ровно N item и ни одного лишнего батча.
    if cfg.sample_limit:
        walker_iter = itertools.islice(walker_iter, cfg.sample_limit)
    # Ctrl+C-пакет (2026-08-07, Раунд 71 ревью follow-up, распространено на все режимы
    # по прямой просьбе пользователя): раньше этот цикл не ловил KeyboardInterrupt вообще
    # (в отличие от _run_impl()) -- Ctrl+C во время analyze/паспорта проваливался наружу
    # без единого отчёта. Теперь -- тот же принцип, что и у _run_impl(): прерывание
    # ловится здесь, stats.interrupted=True, всё уже собранное на момент прерывания
    # (archive_logs/found_archive_top_level ниже, финализация summary) досчитывается как
    # обычно -- вызывающий код (run_analyze_for_source()/run_passport()/CLI analyze/
    # _bare_launch_run_*()) решает, что показать пользователю по этому флагу.
    # Речь пользователя, 2026-08-11: множество различённых папок (по полному пути, ЛЮБАЯ
    # глубина в цепочке предков файла -- та же гранулярность, что и у n_folders в
    # "Расположение"), в которых нашёлся хотя бы один подтверждённый медиафайл -- локальная
    # переменная, не поле AnalyzeStats (нужна только для финального len(), см. её присвоение
    # stats.n_folders_with_media ниже, после цикла).
    _folders_with_media = set()
    # Речь пользователя, 2026-08-11: DVD -- структура VIDEO_TS, считается КАК ЕДИНИЦА (диск),
    # не по числу .vob-файлов внутри неё (dvd_vob_count выше -- фрагменты, другая единица).
    # Все файлы одного юнита несут один и тот же dvd_source_tree_key (реальный путь узла, см.
    # её докстрин у SourceItem) -- множество различённых значений = число различных ФИЗИЧЕСКИХ
    # DVD-папок, см. stats.n_dvd_units ниже, после цикла. НЕ dvd_unit_fingerprint (живая
    # находка 2026-08-14, боевой прогон пользователя: два физически разных диска с одинаковым
    # содержимым схлопывались в "1", хотя в SOURCE их два -- см. докстрин n_dvd_units).
    _dvd_units_seen = set()
    try:
        for item, tags_by_path in walker_iter:
            # 2026-08-23, живая находка пользователя: пауза по пробелу (_check_pause_keypress())
            # изначально была добавлена только в _run_impl() (build/dry-run) -- пользователь явно
            # потребовал "должна отрабатывать независимо от фазы", т.е. и во время analyze/
            # Паспорта тоже, не только реальной записи на диск.
            _check_pause_keypress(log=log)
            # REVIEW-HANDOFF.md, Раунд 86, замечание 2 + follow-up (2026-08-10, речь
            # пользователя): "объектов %" для этого item тикнула уже ВНУТРИ
            # _walk_with_exif_prefetch()/_flush_exif_prefetch_batch() -- на отправку батча в
            # exiftool, а не на завершение (см. object_progress_cb= выше и докстринг
            # _flush_exif_prefetch_batch()) -- здесь тикать второй раз не нужно, defer_media_
            # object_tick=True на SourceWalker уже отключил тик на самом обходе.
            # Ведущий пробел, 2026-08-24, живая просьба пользователя -- для two_line-бара это
            # note ЗАМЕНЯЕТ поле операции (см. update()'s self._transient_op = note), тот же
            # текст должен подчиняться той же конвенции отступа, что и resting/transient-тексты
            # рядом (_DRY_RUN_PHASE_DESC и т.п.).
            bar.update(1, note=" большое видео" if (
                item.ftype == "video" and item.size > 200 * 1024**2) else None)
            stats.total_files += 1
            stats.total_bytes += item.size
            # Речь пользователя, 2026-08-11 (живой боевой прогон по C:\ целиком): "глубина
            # вложенности"/"папок" в "Расположение" считались по ЛЮБОМУ файлу, включая
            # совершенно не-медийные (код, кэши, установщики) -- на сканировании всего диска
            # это раздувало оба числа значениями, не имеющими отношения к фото/видео вообще
            # (тот же класс путаницы, что и archives_found против archives_with_media, см.
            # _render_cta_block() в report.py). Теперь оба считаются ТОЛЬКО по подтверждённым
            # медиафайлам (is_media) -- тот же корень/подпапка/архив разбор, что и у
            # files_by_location ниже, просто раньше независимо от типа файла.
            is_media = item.ftype in ("image", "raw", "video")
            if is_media:
                item_depth = item.rel_path.count("/")
                if item_depth > stats.max_depth:
                    stats.max_depth = item_depth
                if item.archive_boundary_idx is None and "/" in item.rel_path:
                    folder = item.rel_path.rsplit("/", 1)[0]
                    parts_seen = folder.split("/")
                    prefix = ""
                    for seg in parts_seen:
                        prefix = f"{prefix}/{seg}" if prefix else seg
                        _folders_with_media.add(prefix)
            # SESSION-HANDOFF.txt п.4: разбивка по месту, той же гранулярностью, что и путь item --
            # archive_boundary_idx not None -- файл пришёл из распакованного архива (любой глубины
            # вложенности внутри него, "archive" не различает уровни), иначе "/" в rel_path решает
            # корень/подпапка (rel_path всегда posix-style, см. SourceItem).
            if item.archive_boundary_idx is not None:
                stats.files_by_location["archive"] += 1
                stats.bytes_by_location["archive"] += item.size
            elif "/" in item.rel_path:
                stats.files_by_location["folder"] += 1
                stats.bytes_by_location["folder"] += item.size
            else:
                stats.files_by_location["root"] += 1
                stats.bytes_by_location["root"] += item.size

            if item.ftype == "image":
                stats.n_images += 1
            elif item.ftype == "raw":
                stats.n_raw += 1
            elif item.ftype == "video":
                stats.n_videos += 1
            if item.ftype in ("image", "raw", "video"):
                stats.bytes_by_kind[item.ftype] += item.size

            # Живая находка (боевой прогон, отчёт пользователя, 2026-08-09): VIDEO_TS/DVD-юнит
            # (item.dvd_dest_path не None, см. SourceWalker._handle_dvd_unit()) -- ftype
            # безусловно "video" для КАЖДОГО файла юнита (:3370-е, докстринг там же), включая
            # .IFO/.BUP -- служебные файлы навигации/бэкапа DVD-структуры, не проигрываемое видео
            # само по себе. analyze_batch() для ftype=="video" вызывает video_duration_and_
            # resolution() (ffprobe) -- ГАРАНТИРОВАННО проваливается на .IFO/.BUP (это не
            # видеопоток), помечая их "не удалось распознать" -- ложное срабатывание на КАЖДОМ
            # отсканированном DVD-рипе, не краевой случай (Задача D, закрыта ранее в этой же
            # сессии). Реальная сборка (_run_impl()) не даёт этим item попасть в analyze_batch()
            # вовсе -- тот же принцип здесь, но ТОЛЬКО для analyze_batch()/sniff_signature()
            # ниже: живая находка пользователя, 2026-08-09 ("2013 не попал в структуру") --
            # первая версия фикса (безусловный `continue` здесь) пропускала ВЕСЬ блок
            # классификации (find_album/tree_folder_counts/dates_by_year/tier_counts/
            # n_dump_items и т.д.), не только broken-проверку -- DVD-содержимое пропадало из
            # дерева структуры/диаграммы "по годам"/"надёжность дат" целиком, разъезжаясь с
            # total_files/n_videos (те продолжали его считать). is_dvd_unit_item ниже пускает
            # DVD-item через ОСТАЛЬНОЙ конвейер тем же путём, что и обычный файл без EXIF --
            # только сама генерическая ffprobe/сигнатура-проверка (неприменимая к .IFO/.BUP)
            # заменяется на заглушку-rec ("ничего не известно", не broken).
            is_dvd_unit_item = item.dvd_dest_path is not None
            if is_dvd_unit_item and item.dvd_source_tree_key is not None:
                _dvd_units_seen.add(item.dvd_source_tree_key)

            # 2026-08-14, прямая просьба пользователя: дерево реальной структуры SOURCE
            # (report.py:_render_source_tree_card()) -- считает ВСЕ найденные фото/видео/RAW,
            # включая то, что ниже по циклу окажется битым/нечитаемым/дублем (is_media уже
            # решает "это фото/видео/raw", независимо от дальнейшей судьбы файла -- та же
            # точка, что и n_images/n_raw/n_videos выше, до любых broken-проверок).
            if is_media:
                _tree_key = _source_tree_parent_key(item)
                if item.ftype == "image":
                    stats.source_tree_counts_image[_tree_key] += 1
                elif item.ftype == "video":
                    stats.source_tree_counts_video[_tree_key] += 1
                else:
                    stats.source_tree_counts_raw[_tree_key] += 1

            # 2026-08-08 (альбомный редизайн, живая находка на self_scan="Паспорт архива"):
            # "Albums" -- защищённое dump-имя (DUMP_SEGMENT_NAMES_PROTECTED, самозащита от
            # каскадного самопоедания), и под новым безусловным отравлением оно теперь топит
            # ЛЮБОЙ реальный альбом под собой -- на self_scan (TARGET читается КАК SOURCE)
            # item.rel_path буквально начинается с "Albums/", а значит find_album() перестал
            # бы находить вообще ЛЮБОЙ правильно разложенный файл архива. Раньше (без
            # позиционных исключений) "Albums" просто пропускался при поиске якоря -- здесь
            # воссоздаём тот же эффект вручную, ТОЛЬКО для self_scan: срезаем верхний сегмент
            # "Albums" перед вызовом find_album(), как будто его не было -- файл прямо в
            # "Albums\" без альбома под ним (_rest пуст либо сам find_album() не находит
            # ничего дальше) по-прежнему падает в "не найдено", это и есть настоящая находка
            # "файл добавлен мимо программы", см. _PASSPORT_SELF_SCAN_RECOGNIZED_TOP ниже.
            # is_dvd_unit_item: НЕ звать find_album() вовсе -- живая находка при реализации
            # этого же фикса (2026-08-09): item.rel_path для DVD-юнита построен как
            # f"{display_name}/VIDEO_TS/{rel}" (см. _handle_dvd_unit()), где display_name на
            # глубине 0 -- ИМЯ РОДИТЕЛЬСКОЙ папки (не имя альбома) -- generic find_album()
            # ошибочно принимает этот сегмент за настоящее имя альбома (не распознаётся как
            # dump-имя), давая предсказание, не совпадающее с тем, что реально решает
            # _handle_dvd_unit() (метка тома/альбом СНАРУЖИ video_ts/ByDate, три приоритета, её
            # докстринг). Вместо повторной реализации той же логики здесь -- DVD-содержимое для
            # статистики analyze/паспорта всегда считается по маршруту ByDate (album=None) --
            # самый частый реальный случай (DVD без метки тома живого диска), не идеальное
            # предсказание, но не хуже прежнего "молчания" и не вводит в заблуждение неверным
            # именем альбома.
            if is_dvd_unit_item and self_scan:
                # Живая находка пользователя, 2026-08-24 (Паспорт архива): item.rel_path для
                # DVD-юнита -- синтетический (f"{display_name}/VIDEO_TS/{rel}", см.
                # _handle_dvd_unit()) -- display_name это метка ЖИВОГО диска либо голое
                # "VIDEO_TS", когда её нет; self_scan никогда не видит живой диск (сканирует
                # уже архивированные файлы на диске) -- значит display_name здесь ВСЕГДА голое
                # "VIDEO_TS", без связи с реальным местом юнита внутри TARGET (например,
                # Albums\Альбом\...\VIDEO_TS\). Безусловный album=None ветки ниже (верный для
                # ПРЕДСКАЗАНИЯ будущего места на обычном SOURCE, см. её докстринг) на self_scan
                # ошибочно топил ЛЮБОЙ DVD-юнит, реально лежащий внутри настоящего альбома, в
                # "вне альбома/даты" -- юнит физически СТОИТ там, куда его положила сама
                # программа. item.read_path -- реальный абсолютный путь на диске (self_scan:
                # cfg.source == TARGET) -- относительно него find_album() видит истинное
                # положение, той же Albums-обрезкой, что и обычные файлы ниже. self_scan_rel_path
                # используется и здесь, и в RECOGNIZED_TOP-проверке чуть ниже -- та же причина
                # (item.rel_path там тоже был бы синтетическим "VIDEO_TS/...").
                try:
                    self_scan_rel_path = os.path.relpath(item.read_path, cfg.source).replace("\\", "/")
                except ValueError:
                    self_scan_rel_path = item.rel_path
                find_album_rel_path, find_album_boundary = self_scan_rel_path, None
                _top, _sep, _rest = find_album_rel_path.partition("/")
                if _sep and _top.strip().lower() == "albums":
                    find_album_rel_path = _rest
                album, subpath, album_prefix = find_album(find_album_rel_path, find_album_boundary,
                                                           dump_names=cfg.dump_segment_names_lower,
                                                           dump_prefixes=cfg.dump_segment_prefixes_tuple,
                                                           bydate_only=cfg.source_bydate_only)
            elif is_dvd_unit_item:
                album, subpath, album_prefix = None, [], None
                self_scan_rel_path = item.rel_path
            else:
                find_album_rel_path, find_album_boundary = item.rel_path, item.archive_boundary_idx
                if self_scan:
                    _top, _sep, _rest = item.rel_path.partition("/")
                    if _sep and _top.strip().lower() == "albums":
                        find_album_rel_path = _rest
                        find_album_boundary = (None if find_album_boundary is None
                                                else max(0, find_album_boundary - 1))
                album, subpath, album_prefix = find_album(find_album_rel_path, find_album_boundary,
                                                           dump_names=cfg.dump_segment_names_lower,
                                                           dump_prefixes=cfg.dump_segment_prefixes_tuple,
                                                           bydate_only=cfg.source_bydate_only)
                self_scan_rel_path = item.rel_path
            # SESSION-HANDOFF.txt, "большой разбор report.html", пункт A (дерево структуры
            # архива) -- бакет для этого элемента, тем же строителем путей, что и реальная
            # сборка (build_album_dest_dir()/build_bydate_dest_dir()), но с фиктивным
            # относительным корнем ("Albums"/"ByDate", не cfg.albums_root/cfg.bydate_root) --
            # обе функции чистые строители строк, без I/O, безопасно звать и для self_scan
            # (паспорт), и для обычного analyze/dry-run (предсказание БУДУЩЕГО места, тот же
            # принцип, что и у остального run_analyze() -- переиспользовать решающую логику
            # реальной сборки, не изобретать отдельную). RAW -- намеренное упрощение: реальный
            # RAW_LAYOUT=mirror зеркалит место JPEG-партнёра (может зависеть от ЕГО решения),
            # RAW_LAYOUT=sibling кладёт рядом -- для одной сводной диаграммы плоский бакет
            # "RAW" точнее, чем неполное дублирование этой логики. granularity="month"/place=None
            # -- по решению пользователя (глубина дерева "до месяцев", не до дня и не с городом
            # в имени папки, иначе один архив с гео-разметкой давал бы сотни узких бакетов).
            if item.ftype == "raw":
                tree_key = "RAW"
            elif album:
                tree_key = build_album_dest_dir("Albums", album_prefix, subpath).replace("\\", "/")
            else:
                tree_key = None  # см. ниже -- решается по дате (или "_Unsorted" при broken/zero)
            if album:
                # 2026-08-08 (альбомный редизайн, RULES.md): "альбом -- это каждая папка в
                # дереве" -- папка1\папка2\папка3 -- ТРИ разных альбома, не один. Считаем
                # КАЖДЫЙ промежуточный путь от корня SOURCE до файла отдельно, не только
                # верхний сегмент (album_prefix -- это только segments[0], см. find_album()).
                full_segments = [album_prefix] + subpath
                for i in range(len(full_segments)):
                    album_names.add("/".join(full_segments[:i + 1]))
                if item.ftype in ("image", "raw", "video"):
                    stats.n_media_in_albums += 1
            elif self_scan and self_scan_rel_path.split("/", 1)[0].strip().lower() in _PASSPORT_SELF_SCAN_RECOGNIZED_TOP:
                # Живой репорт пользователя (2026-08-01): на TARGET (self_scan) find_album()
                # ЗАКОНОМЕРНО не находит альбом под ByDate/RAW/_Unsorted -- их листовые
                # день/месяц-папки безусловно dump-тэгнуты (см. is_dump_segment()/DUMP_TAG), а
                # промежуточный "2024" -- голый цифровой сегмент, тоже безусловно dump
                # (is_dump_segment()). Это не "файл вне архива", а ровно то место, куда
                # программа его и положила -- верхний сегмент
                # пути уже отвечает на вопрос "альбом или дата", просто без строки-имени.
                # Albums сюда сознательно не входит: файл прямо в "Albums/" без имени альбома
                # под ним -- это и есть настоящая находка "мимо программы", её нужно ловить.
                # self_scan_rel_path (== item.rel_path, кроме DVD-юнитов -- см. её вычисление
                # выше) использует "/" как разделитель (тот же паттерн, что find_album()
                # :3455 / _process_decided_item :5285) -- НЕ os.sep, проверено фактическим
                # прогоном (первая версия фикса ошибочно сплитила по "\\", тест краснел до
                # исправления, см. ci/windows_ci_test.py).
                pass
            else:
                stats.n_dump_items += 1
                stats.dump_items_by_folder[os.path.dirname(item.rel_path)] += 1
                stats.dump_item_paths.append(item.origin_display)
                if item.ftype in ("image", "raw", "video"):
                    stats.n_media_by_date += 1
                    stats.bydate_media_by_folder[os.path.dirname(item.rel_path)] += 1

            if item.ftype == "raw" and not item.sibling_path:
                stats.n_raw_without_jpeg += 1
            if item.ftype == "image" and not item.sibling_path:
                stats.n_jpeg_without_raw += 1

            if is_dvd_unit_item:
                # Речь пользователя, 2026-08-09: sniff_signature()/analyze_batch() (ffprobe)
                # неприменимы к .IFO/.BUP (не видеопоток сами по себе, см. докстринг выше) --
                # заглушка-rec ("ничего не известно", is_media=True по умолчанию, broken=False)
                # пускает item дальше по конвейеру (resolve_date/tree_folder_counts/tier_counts/
                # dates_by_year ниже) тем же путём, что и обычный файл без EXIF -- ни разу не
                # "битый"/"не прочитано" (реальная сборка тоже никогда не оспаривает DVD-юнит
                # поштучно, см. _process_dvd_item()).
                rec = SourceRecord(item=item)
            else:
                # SESSION-HANDOFF.txt, 2026-08-09 (одиннадцатая задача): self_scan (Паспорт
                # архива) -- проверка сигнатуры ВСЕГДА безусловна (полная проверка уже
                # собранного архива). Обычный анализ ([1]/CLI analyze) -- только если
                # пользователь явно включил флаг в конфиге (по умолчанию cfg.check_signature=
                # False, проверка пропускается). Когда проверка не выполняется --
                # n_signature_mismatch не увеличивается вовсе (не может быть найдено то, что не
                # проверялось) -- поле рендерится ТОЛЬКО в Паспорте архива (self_scan всегда
                # True там), обычный [1]-отчёт этот пункт вообще не показывает, ни сейчас, ни
                # после этой правки -- ложному "0" протечь некуда (обсуждено и закрыто с
                # пользователем в этой же сессии).
                if self_scan or cfg.check_signature:
                    real_kind = sniff_signature(item.read_path)
                    if real_kind is not None and real_kind != _coarse_kind(item.ftype):
                        stats.n_signature_mismatch += 1

                if item.size == 0:
                    stats.n_broken_or_zero += 1
                    # Пустой файл -- содержимое НЕ распознано (TARGET-уровень назвал бы это
                    # "disputed", см. AnalyzeStats.disputed_paths выше), не путать с
                    # rec.read_error ниже ("не прочитано" -- I/O-сбой, файл вообще не открылся).
                    stats.disputed_paths.append(_analyze_source_abs_path(cfg, item))
                    stats.disputed_records.append(_analyze_dispute_record(cfg, item))
                    # Битый/пустой файл всегда уходит в _Unsorted при реальной сборке (см.
                    # отчёт "N файлов не удалось распознать -- Лежат в _Unsorted"), независимо
                    # от того, что вычислил tree_key выше (реальный альбом/RAW тут не место
                    # назначения).
                    stats.tree_folder_counts["_Unsorted"] += 1
                    # "объектов %" -- см. докстринг _walk_with_exif_prefetch()/2026-08-18:
                    # тикаем здесь, ПОСЛЕ решения "битый", не в момент отправки батча в
                    # exiftool. archive_boundary_idx is None -- та же гранулярность, что и
                    # _quick_media_count_estimate() (архив -- единица, не по файлам внутри,
                    # уже тикнул отдельно в SourceWalker, см. defer_media_object_tick).
                    if item.archive_boundary_idx is None:
                        bar.add_object_progress(1)
                    continue
                if item.ftype not in ("image", "raw", "video"):
                    if item.archive_boundary_idx is None:
                        bar.add_object_progress(1)
                    continue

                cached = archive_cache.get(item.read_path) if archive_cache is not None else None
                cache_hit = bool(cached and cached[0] == item.size and abs(cached[1] - item.mtime) < 1e-6)
                exif_hit = bool(cache_hit and len(cached) > 8 and cached[8])
                recs = analyze_batch([item], retries=cfg.read_retry_count, retry_delay=cfg.read_retry_delay,
                                      small_image_px=cfg.small_image_px, log=log,
                                      skip_hash=(mode == "analyze-quick"), pool=pool, cache=archive_cache,
                                      tags_by_path=tags_by_path)
                rec = recs[0]
                # "объектов %" -- тикаем ЗДЕСЬ, ПОСЛЕ analyze_batch() (значит и после
                # video_duration_and_resolution()/ffprobe для видео -- самой медленной части
                # analyze-quick на источнике с крупными видео), не в момент, когда батч этого
                # item был всего лишь ОТПРАВЛЕН в exiftool (см. докстринг
                # _walk_with_exif_prefetch()/2026-08-18). Покрывает и broken/read_error-исход
                # ниже (rec уже реально посчитан -- пусть и с ошибкой -- к этому моменту), и
                # успешный. archive_boundary_idx is None -- тот же смысл, что и выше.
                if item.archive_boundary_idx is None:
                    bar.add_object_progress(1)
                if rec.read_error or rec.broken:
                    stats.n_broken_or_zero += 1
                    # rec.read_error -- файл физически не удалось прочитать (I/O-сбой, TARGET-
                    # уровень зовёт это "не прочитано"/unreadable.csv); rec.broken -- файл
                    # прочитан, но содержимое не распознано (та же категория, что
                    # item.size==0 выше, TARGET-уровень зовёт это "disputed"/disputes.csv).
                    # Этот участок уже ПОСЛЕ фильтра item.ftype in (image, raw, video) выше --
                    # rec.read_error здесь гарантированно медиа, доп. проверка не нужна (в
                    # отличие от item.size==0 выше, тот срабатывает ДО фильтра типа).
                    if rec.read_error:
                        stats.unreadable_paths.append(_analyze_source_abs_path(cfg, item))
                        stats.unreadable_records.append(_analyze_dispute_record(cfg, item))
                    else:
                        stats.disputed_paths.append(_analyze_source_abs_path(cfg, item))
                        stats.disputed_records.append(_analyze_dispute_record(cfg, item))
                    stats.tree_folder_counts["_Unsorted"] += 1
                    continue
                # Речь пользователя, 2026-08-02: пишем, если ЛИБО хеш, ЛИБО EXIF были
                # свежепосчитаны (не оба сразу нужны -- частый случай "хеш уже в кэше с
                # прошлого паспорта, но эта версия кода только что впервые узнала
                # дату/камеру/GPS для того же файла" тоже обязан записаться, иначе следующий
                # паспорт снова спросит exiftool). Полностью тёплая строка (cache_hit и
                # exif_hit оба True) ничего не пишет -- нечего обновлять.
                if archive_cache_conn is not None and (not cache_hit or not exif_hit) and rec.sha256:
                    # Свежепосчитанный (не из кэша) файл -- сеем archive_cache тем же
                    # принципом, что и _seed_archive_cache() при обычной сборке: следующий
                    # паспорт/сборка того же архива увидит эти же (path,size,mtime) и не
                    # станет считать заново.
                    archive_cache_conn.execute(
                        "INSERT OR REPLACE INTO archive_cache"
                        "(path,size,mtime,sha256,phash,duration,width,height,bitrate,"
                        "exif_cached,exif_dt,exif_dt_source,camera,gps_lat,gps_lon) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (item.read_path, item.size, item.mtime, rec.sha256, rec.phash,
                         rec.duration, rec.width, rec.height, rec.bitrate,
                         1, rec.exif_dt.isoformat() if rec.exif_dt else None, rec.exif_dt_source,
                         rec.camera, rec.gps_lat, rec.gps_lon),
                    )

            # Задача A, п.2: эта точка цикла достигается ТОЛЬКО для item, которые реально
            # доступны для архива -- is_dvd_unit_item пропустил все broken/read_error проверки
            # выше безусловно (DVD-юнит никогда не оспаривается поштучно), non-DVD-ветка дошла
            # сюда, только не сработав ни один `continue` (size==0/ftype-фильтр/read_error-
            # broken) -- ftype здесь гарантированно image/raw/video в обоих случаях.
            if item.ftype == "image":
                stats.n_images_available += 1
            elif item.ftype == "raw":
                stats.n_raw_available += 1
            elif item.ftype == "video":
                stats.n_videos_available += 1
            stats.bytes_by_kind_available[item.ftype] += item.size

            # Задача A, п.5/6: топ-форматов -- та же "available" точка цикла, что и счётчики
            # выше (ТЗ пользователя относит топ-форматов к Разделу 2 "Доступно для архива", не
            # к "найдено всего" -- битый/нечитаемый файл не должен участвовать в топ-5 формата).
            # DVD-юнит-файлы исключены из format_counts_video (см. докстринг поля в
            # AnalyzeStats) -- .vob внутри юнита считается отдельно (dvd_vob_count), .ifo/.bup
            # внутри юнита не считаются никуда (служебные, не формат, с которым пользователь
            # ассоциирует "видео"). ext -- по rel_path (посикс-style, splitext не зависит от
            # разделителя -- ищет последнюю точку в имени файла).
            item_ext = os.path.splitext(item.rel_path)[1].lower()
            if is_dvd_unit_item:
                if item_ext == ".vob":
                    stats.dvd_vob_count += 1
            elif item.ftype == "image":
                stats.format_counts_image[item_ext] += 1
            elif item.ftype == "raw":
                stats.format_counts_raw[item_ext] += 1
            elif item.ftype == "video":
                stats.format_counts_video[item_ext] += 1

            dirname = os.path.dirname(item.rel_path)
            date_value, tier, conf, evidence, precision = resolve_date(
                date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source,
                use_folder_name_date=not self_scan)
            if tree_key is None:
                # Не RAW и не в признанном альбоме -- бакет по дате, тем же принципом, что и
                # реальная ByDate-раскладка. date_value=None (Tier D) -- отдельный плоский бакет
                # (реальная папка "0000-undated" отдельным деревом мирроит структуру SOURCE, для
                # сводной диаграммы это была бы неограниченная глубина ради редкого случая).
                tree_key = (build_bydate_dest_dir("ByDate", date_value, precision, None, "month")
                            .replace("\\", "/")) if date_value is not None else "ByDate/0000-undated"
            stats.tree_folder_counts[tree_key] += 1
            if rec.exif_dt is None:
                stats.n_no_exif_date += 1
            if date_value:
                now_year = datetime.now().year
                if date_value.year > now_year:
                    stats.n_future_date += 1
                elif date_value.year < 1990:
                    stats.n_before_1990 += 1
                # PROMPT_archive_report.md, Лист 1/2: "фото по годам"/"самое старое фото" --
                # resolve_date() уже вызывается безусловно для ЛЮБОГО analyze-режима (в отличие
                # от decide(), которому нужен skip_hash=False) -- тот же принцип "не выбрасывать
                # уже посчитанное", что и у dump_items_by_folder/near_dup_edges выше.
                stats.dates_by_year[date_value.year] += 1
                stats.dates_by_year_month[date_value.strftime("%Y-%m")] += 1
                # Задача A, п.4: та же разбивка, что format_counts_*/tier_counts_no_raw --
                # фото/видео раздельно, RAW не попадает ни в одну из двух (см. докстринг поля).
                if item.ftype == "image":
                    stats.dates_by_year_photo[date_value.year] += 1
                elif item.ftype == "video":
                    stats.dates_by_year_video[date_value.year] += 1
                if stats.oldest_date is None or date_value < stats.oldest_date:
                    stats.oldest_date = date_value
                    stats.oldest_display = item.origin_display
            # PROMPT_archive_report.md, Лист 2 "Надёжность дат" (донат A/B/C/D) -- та же логика,
            # что near_dup_edges/dates_by_year выше: tier уже посчитан resolve_date() для каждого
            # файла, n_tier_c_estimated ниже отражает только срез "C", донату нужна полная
            # разбивка -- аддитивный Counter, существующий n_tier_c_estimated не трогаем.
            stats.tier_counts[tier] += 1
            # Задача A, п.3: та же разбивка без RAW, что и у dates_by_year_photo/_video выше.
            if item.ftype != "raw":
                stats.tier_counts_no_raw[tier] += 1
            if tier == "C":
                stats.n_tier_c_estimated += 1
            elif tier == "D" and mtime_is_copy_artifact(date_ctx.dir_mtimes.get(dirname, [])):
                stats.n_copy_artifact_mtime += 1
            if tier in ("C", "D") and not album:
                stats.n_tier_cd_bydate += 1
            # SESSION-HANDOFF.txt, 2026-08-09 (пятая находка): разбивка B/C/D РАЗДЕЛЬНО для
            # объединённого чек-листа "получили дату неточно или не получили вовсе" в
            # report.py -- изначально фильтровалась album-исключением (тем же, что и
            # n_tier_cd_bydate выше), рассуждение было "файл в Albums/ -- точность даты ни на
            # что не влияет". Речь пользователя, 2026-08-11 (живая находка -- расхождение с
            # диаграммой "Надёжность дат", которая album НЕ исключает): "для анализа признак
            # альбома не существует. Анализ просто показывает, что есть на диске" -- album --
            # решение о РАСКЛАДКЕ (Albums/ vs ByDate/ при реальной сборке), не факт о самом
            # файле, и не должно фильтровать НИКАКОЙ analyze-счётчик. Фильтр снят -- считает
            # ВСЕ non-RAW медиафайлы тем же тиром, что и tier_counts_no_raw (диаграмма) --
            # суммы теперь совпадают. n_tier_cd_bydate выше (для _render_passport_integrity(),
            # self_scan УЖЕ построенного архива, album там -- реальный факт, не прогноз) не
            # тронут.
            # REVIEW-HANDOFF.md, Раунд 89: album-фильтр сняли (см. выше), но RAW-фильтр --
            # тот же, что уже стоит у tier_counts_no_raw тремя строками выше -- забыли повторить
            # здесь, хотя комментарий прямо заявлял точное совпадение с tier_counts_no_raw.
            if item.ftype != "raw":
                if tier == "B":
                    stats.n_tier_b_bydate += 1
                elif tier == "C":
                    stats.n_tier_c_bydate += 1
                elif tier == "D":
                    stats.n_tier_d_bydate += 1

            if cfg.place_lookup == "offline":
                place = place_for_gps(rec.gps_lat, rec.gps_lon, cfg.home_country)
                if place:
                    stats.cities[place] += 1

            if rec.camera:
                stats.cameras[rec.camera] += 1

            if mode == "analyze" and rec.sha256:
                decision = decide(pool, rec, cfg.mirror_raw)
                if decision.decision == "skipped_present":
                    stats.n_exact_dupes += 1
                    existing = pool.find_exact(rec.sha256)
                    if existing and existing.dest_path:
                        if os.path.basename(existing.dest_path) != os.path.basename(item.read_path):
                            stats.n_diff_name_same_content += 1
                        # Задача 4, речь пользователя 2026-08-02: та же форма ребра, что и
                        # near_dup_edges чуть ниже (dest/matched_dest -- оба item.origin_display/
                        # PoolEntry.dest_path, посикс-разделитель "/", см. докстринг поля) --
                        # report.py-рендер паспорта сам нормализует "/" -> "\\" при отображении
                        # (тот же приём, что уже применён к oldest_display в
                        # _render_passport_summary()).
                        stats.exact_dup_edges.append({
                            "dest": item.origin_display, "matched_dest": existing.dest_path,
                        })
                elif decision.decision == "raw_skipped":
                    pass  # MIRROR_RAW=false + есть JPEG -- осознанно не копируется, не "новое"
                else:
                    # appended_new / appended_better / appended_crop / appended_near_dup /
                    # appended_uncertain / raw_mirrored -- p.5.7: near-dup is appended, not
                    # skipped, so it counts toward predicted_unique_* like any other appended file.
                    if decision.decision in ("appended_better", "appended_crop", "appended_near_dup"):
                        stats.n_near_dupes += 1
                        if decision.matched_dest is not None:
                            # PROMPT_archive_report.md, 1.2б: как и в _process_record, но "dest"
                            # здесь = item.origin_display -- analyze-уровень ничего не копирует,
                            # нет реального dest_path, тот же плейсхолдер, что чуть ниже кладётся
                            # в PoolEntry.dest_path для этого же item.
                            stats.near_dup_edges.append({
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "source": item.origin_display, "dest": item.origin_display,
                                "matched_dest": decision.matched_dest,
                                "category": decision.decision, "hamming": decision.hamming,
                            })
                    stats.predicted_unique_count += 1
                    stats.predicted_unique_bytes += item.size
                    if item.ftype == "raw":
                        entry = PoolEntry(sha256=rec.sha256, ftype="raw", dest_path=item.origin_display,
                                           size=item.size)
                    else:
                        pool_ftype = "image" if item.ftype == "image" else "video"
                        entry = PoolEntry(sha256=rec.sha256, ftype=pool_ftype, dest_path=item.origin_display,
                                           size=item.size, aspect=rec.aspect, width=rec.width, height=rec.height,
                                           phash=rec.phash, duration=rec.duration, bitrate=rec.bitrate,
                                           has_camera=bool(rec.camera))
                    pool.add(entry)
    except KeyboardInterrupt:
        stats.interrupted = True
        bar.mark_interrupted()  # "обработано объектов XX%" не форсирует 100% на прерванном прогоне

    stats.n_objects_total = bar.object_count  # SESSION-HANDOFF.txt п.4 -- ДО close(), не после
    stats.n_folders_with_media = len(_folders_with_media)
    stats.n_dvd_units = len(_dvd_units_seen)
    # SESSION-HANDOFF.txt, 2026-08-11 (отложенная задача): реальная сборка (см. run_for_source())
    # пробрасывает только len(walker.listdir_failed) в отчёт -- analyze хочет сами пути (тот же
    # паттерн, что encrypted_archive_paths/failed_archive_paths ниже), cur_dirpath уже реальный
    # абсолютный путь (см. SourceWalker._walk_dir()), доп. преобразование не нужно.
    stats.listdir_failed_paths = list(walker.listdir_failed)
    bar.close()  # ДО того, как вызывающий код продолжит писать в консоль -- не портить формат бара
    # Живая находка пользователя, 2026-08-09: раньше run_analyze() не подчищал tmp_extract ни в
    # начале, ни после прерывания вовсе (в отличие от _run_impl(), см. её Фазу 0) -- временные
    # распакованные архивы копились под %TEMP% (см. _NO_TARGET_PLACEHOLDER) неограниченно.
    # Безусловно (не только при stats.interrupted) -- дешёвый no-op, если нечего чистить.
    _cleanup_own_tmp_extract_entries(cfg, log=log)

    for display, status, note in walker.archive_logs:
        if status.startswith("archive_"):
            stats.n_archives_found += 1
        if status == "archive_extracted":
            stats.n_archives_with_media += 1
        if status == "archive_password_protected":
            stats.n_archives_encrypted += 1
            # note -- реальный абсолютный путь архива (см. _handle_archive()), не display
            # (относительный origin_prefix, из него file://-ссылку не построить).
            stats.encrypted_archive_paths.append(note)
        # Задача A, п.7: "не открылся" -- read_error (I/O-сбой при открытии/распаковке) и
        # bomb_suspected (превышена глубина вложенности, отказ намеренный) -- та же by-status
        # логика, что у password_protected выше, но НЕ note -- у этих двух статусов note это
        # человекочитаемая причина ("превышена глубина вложенности"/"файл исчез..."), не путь
        # (в отличие от password_protected, где note намеренно переопределён на archive_path,
        # см. комментарий на _handle_archive()). display -- archive_path при depth==1 (реальный
        # абсолютный путь), относительный origin-трейл при depth>1 (тот же паттерн, что и note
        # у password_protected для depth>1) -- report.py откатится на текст, если это не путь.
        if status in ("archive_read_error", "archive_bomb_suspected"):
            stats.n_archives_failed += 1
            stats.failed_archive_paths.append(display)
        if display.count(" → ") >= 2:
            stats.n_archives_nested += 1

    stats.n_albums_detected = len(album_names)
    stats.found_archive_top_level, _nested = classify_found_archives(
        walker.found_archive_roots, cfg, mode)

    if archive_cache_conn is not None:
        archive_cache_conn.commit()
        archive_cache_conn.close()

    return stats


def write_analyze_report_csv(path: str, stats: AnalyzeStats):
    """SESSION-HANDOFF.txt п.2 (2026-08-05, боевой прогон): раньше был ещё print_analyze_report()
    -- консольный чек-лист, дублировавший то, что и так показывает report.html (см.
    run_analyze_for_source()/_finalize_analyze_report()) -- убран целиком, эта функция осталась
    единственным консольным/файловым выводом того же набора метрик: metric,value -- ТОЛЬКО
    скалярные поля AnalyzeStats (int/float/bool/str/None). Пишется в WORKDIR (НЕ в TARGET -- ни один
    analyze-режим не пишет в TARGET), перезаписывается на каждый прогон (снимок текущего
    анализа, не append-only лог, в отличие от __служебные_файлы\\logs\\*.csv реальной сборки).

    Аудит 2026-07-21 (SESSION-HANDOFF.txt, "потенциально лишние режимы/CLI-флаги/конфиги"):
    до этой правки функция дампила буквально ВСЕ поля через vars(stats).items(), включая
    структурные (Counter/list/dict -- dump_items_by_folder, dates_by_year, near_dup_edges,
    tier_counts, found_archive_top_level/nested и т.п.), добавленные в AnalyzeStats уже
    ПОСЛЕ появления этой функции (2026-07-08), ради report.html (появился 2026-07-18,
    PROMPT_archive_report.md) -- в CSV они попадали как нечитаемые Python-repr строки вида
    "Counter({2024: 12})", не парсящиеся ничем, кроме eval(). report.html показывает те же
    данные нормально (графики/таблицы/чек-лист), так что здесь их просто пропускаем --
    возвращаемся к изначальному контракту "metric,value" из докстринга, а не расширяем его
    неявно каждым новым полем AnalyzeStats."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        for field_name, value in vars(stats).items():
            if isinstance(value, (Counter, list, dict)):
                continue
            w.writerow([field_name, value])


def write_dryrun_report_csv(path: str, stats: dict):
    """Пакет п.1 (SESSION-HANDOFF.txt, "консольный вывод дублирует report.html"): по образцу
    write_analyze_report_csv() выше -- machine-readable снимок [2] Пробный прогон в WORKDIR,
    "metric,value", только скалярные поля `stats` (_sum_stats(results) + free_disk_bytes,
    см. вызывающий код _bare_launch_run_dryrun() -- тот же словарь, что уже питает
    report.html/консольную сводку) -- перезаписывается на каждый прогон, НЕ append-only (та
    же логика, что и у analyze_report.csv, не полноценный RunLogs -- [2] эфемерен по
    конструкции, история ему не нужна, см. ROADMAP.md). Без этого файла числа [2] сегодня
    переживают только report.html (WORKDIR) и терминальный скроллбек -- закрыл вкладку
    браузера или консоль, потерял."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        for key, value in stats.items():
            if isinstance(value, (Counter, list, dict)):
                continue
            w.writerow([key, value])

# ============================================================================
# MAIN  (from pipeline/main.py)
# ============================================================================

PHOTOSORT_DIR_EXPLANATION = (
    "Служебная папка архива PhotoArchive (__служебные_файлы\\): logs (логи прогонов), "
    "prompt (версии правил сборки), tmp_extract (временная распаковка архивов-источников, "
    "чистится автоматически). Не медиа-контент, не трогать вручную во время прогона. Спорные "
    "файлы (не смогли уверенно распознать) лежат отдельно, в _Unsorted\\ рядом с "
    "Albums\\/ByDate\\/RAW\\ -- это настоящие фото, не служебные данные. Этот маркер исключает "
    "всё поддерево __служебные_файлы\\ из обхода источника, если сам архив используется как "
    "SOURCE для другого TARGET."
)


def ensure_target_layout(cfg: Config):
    # ByDate/0000-undated (undated) намеренно НЕ создаётся заранее здесь -- это часть
    # основного архивного дерева (не служебная папка), появляется лениво через
    # resolve_dest_path() при первом недатированном файле, как любая обычная ByDate/YYYY.
    for d in (cfg.albums_root, cfg.bydate_root, cfg.raw_root,
              cfg.dispute, cfg.tmp_extract, cfg.logs, cfg.prompt_dir):
        _makedirs_iterative(winlong(d))
    marker = os.path.join(cfg.photosort_dir, "SKIP_PHOTOSORT.txt")
    if not os.path.exists(winlong(marker)):
        with open(winlong(marker), "w", encoding="utf-8") as f:
            f.write(PHOTOSORT_DIR_EXPLANATION + "\n")


def check_rules_version(cfg: Config, log=print):
    """Заменяет byte-diff версионирование оригинала (которое копировало целиком
    prompt.md/photosort.py в {TARGET}/__служебные_файлы/prompt/ при изменении). Здесь скрипт --
    один onefile .exe (сотни МБ), копировать его в TARGET на каждый прогон нельзя, поэтому
    сравнивается короткая строка RULES_VERSION (см. верх файла, RULES.md), а не байты
    исполняемого файла. version.txt -- append-only история смен версий правил, как и
    раньше никогда не затирается."""
    dest = os.path.join(cfg.prompt_dir, "version.txt")
    if not os.path.exists(winlong(dest)):
        with open(winlong(dest), "w", encoding="utf-8") as f:
            f.write(f"{RULES_VERSION}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tfirst_run\n")
        log(f"Версия правил: первая — {RULES_VERSION} (записана в __служебные_файлы\\prompt\\version.txt)")
        return
    with open(winlong(dest), "r", encoding="utf-8") as f:
        lines = [line for line in f.read().splitlines() if line.strip()]
    last_version = lines[-1].split("\t", 1)[0] if lines else None
    if last_version == RULES_VERSION:
        return
    with open(winlong(dest), "a", encoding="utf-8") as f:
        f.write(f"{RULES_VERSION}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tchanged_from={last_version}\n")
    log(f"ВНИМАНИЕ: версия правил изменилась с прошлого прогона ({last_version} -> "
        f"{RULES_VERSION}). Архив мог быть собран другой версией правил (см. "
        f"__служебные_файлы\\prompt\\version.txt). appended.csv будет отмечать текущую версию.")


def build_pool_from_archive_table(conn) -> Pool:
    """Width/height/bitrate now come straight from the archive table (populated by
    index_archive) instead of re-decoding every already-archived image via PIL on every
    single run -- besides the wasted I/O, the old version never set width/height for
    *video* entries at all, which silently broke cross-run video near-dup comparison
    (video_is_strictly_better saw existing videos as 0x0 and always treated the new file as
    strictly better, re-appending near-duplicate videos across runs instead of skipping)."""
    pool = Pool()
    for row in conn.execute(
        "SELECT path, size, mtime, sha256, phash, duration, type, root, width, height, bitrate FROM archive"
    ):
        path, size, mtime, sha256, phash, duration, ftype, root, width, height, bitrate = row
        entry = PoolEntry(sha256=sha256, ftype=ftype, dest_path=path, size=size, duration=duration,
                           phash=phash, width=width, height=height, bitrate=bitrate)
        if ftype == "image" and width and height:
            entry.aspect = width / height
        pool.add(entry)
    return pool


def _iter_ancestors(path: str):
    """Yields every ancestor directory of path, from its immediate parent up to the
    filesystem/drive root (inclusive), stopping once os.path.dirname stops changing."""
    parent = os.path.dirname(path)
    prev = None
    while parent and parent != prev:
        yield parent
        prev = parent
        parent = os.path.dirname(parent)


def warn_if_target_nested_in_archive(cfg: Config, log=print) -> bool:
    """А.3: TARGET мог по ошибке быть указан подпапкой УЖЕ существующего архива photo-sort
    (например, TARGET=D:\\Архив фото\\Albums\\Свадьба вместо самого D:\\Архив фото) -- тогда
    служебные папки (__служебные_файлы\\) расплодятся по веткам вместо единого архива. НЕ блокирует
    запуск: отдельные тематические архивы верхнего уровня -- нормальный, поддерживаемый
    сценарий (RULES.md, "ЭКСПЛУАТАЦИЯ"); предупреждение реагирует только на вложенность В
    существующий архив, не на соседство с ним. Признак существующего архива у предка --
    его собственная папка __служебные_файлы\\ (умбрелла Задачи 1) ИЛИ одновременно Albums\\ и
    ByDate\\ (более старый архив/ручное дерево без __служебные_файлы). Тематические папки должны
    быть альбомами ВНУТРИ одного архива, а не отдельными TARGET. Returns True if it warned
    (p.5.3а: summary.txt counts this, see report_environment)."""
    target_real = os.path.realpath(cfg.target)
    for parent in _iter_ancestors(target_real):
        if not os.path.isdir(winlong(parent)):
            continue
        try:
            entries = {e.lower() for e in os.listdir(winlong(parent))}
        except OSError:
            continue
        if "__служебные_файлы" in entries or ("albums" in entries and "bydate" in entries):
            log(f"ВНИМАНИЕ: TARGET ({cfg.target}) похож на подпапку уже существующего "
                f"архива photo-sort в {parent} -- служебные папки (__служебные_файлы\\) могут "
                f"расплодиться по веткам вместо единого архива. Если TARGET указан веткой "
                f"существующего архива по ошибке (нужен был сам {parent} или его Albums\\-"
                f"подпапка как тема внутри него) -- поправьте TARGET. Если это осознанно "
                f"отдельный тематический архив верхнего уровня -- игнорируйте.")
            return True
    return False


def _target_has_existing_archive(target: str) -> bool:
    """True, если сам TARGET (не его предки, см. warn_if_target_nested_in_archive()) уже
    содержит структуру существующего архива photo-sort -- та же сигнатура ("__служебные_файлы" в
    listdir, либо одновременно "albums" и "bydate" -- более старый архив/ручное дерево без
    неё). Используется подменю выбора диска (RULES.md, "ЗАПУСК" п.3) для статуса "уже есть —
    допишу новые фото"/"папка уже есть" и подменю Паспорта архива (показывает только диски,
    где архив реально уже существует). Несуществующий/недоступный TARGET -> False."""
    real_target = winlong(target)
    if not os.path.isdir(real_target):
        return False
    try:
        entries = {e.lower() for e in os.listdir(real_target)}
    except OSError:
        return False
    return "__служебные_файлы" in entries or ("albums" in entries and "bydate" in entries)


_FOUND_ARCHIVE_ORGANIZED_SEGMENTS = {"albums", "bydate", "raw", "_unsorted"}


def classify_found_archives(raw_roots: list, cfg: Config, mode: str) -> tuple:
    """ROADMAP.md, analyze как "2 части": raw_roots -- сырые пути (realpath), собранные
    SourceWalker.found_archive_roots за время обхода SOURCE (каждый -- родитель встреченной
    где-то в дереве папки __служебные_файлы).

    cfg/mode сохранены в сигнатуре ради обратной совместимости вызова (см. run_analyze()) --
    2026-08-04: TARGET раньше тоже добавлялся сюда для mode=="analyze-full" (сверка с уже
    существующим архивом) -- этот режим убран целиком (CLI-подкоманда analyze-full удалена,
    её единственная незамещённая часть, прикидка свободного места, перенесена в dry-run, см.
    CLI_MODES), TARGET в roots больше никогда не добавляется.

    Возвращает (top_level: list[str], nested: dict[str, list[str]]):
    - top_level -- найденные архивы, НЕ вложенные в дерево другого найденного архива (вложенный
      архив НЕ считается отдельным найденным и не суммируется, см. ROADMAP.md).
    - nested -- top-level путь -> список путей, найденных ВНУТРИ его организованной структуры
      (Albums/ByDate/RAW/_Unsorted) -- прямая улика ручного вмешательства в обход программы,
      эскалирует оговорку части 2 для этого архива (см. report.py). Вложенные архивы,
      найденные ГДЕ-ТО ЕЩЁ внутри родителя (не в его Albums/ByDate/RAW/_Unsorted), тоже
      исключаются из top_level, но не эскалируют предупреждение -- редкий случай, никакой
      организованной структуры программа сама туда не кладёт."""
    roots = list(dict.fromkeys(raw_roots))
    roots_nc = {r: os.path.normcase(r) for r in roots}
    top_level = []
    nested = {}
    for r in roots:
        r_nc = roots_nc[r]
        parents = [p for p in roots if p != r and r_nc.startswith(roots_nc[p] + os.sep)]
        if not parents:
            top_level.append(r)
            continue
        parent = max(parents, key=lambda p: len(roots_nc[p]))
        first_segment = os.path.relpath(r, parent).split(os.sep)[0].lower()
        if first_segment in _FOUND_ARCHIVE_ORGANIZED_SEGMENTS:
            nested.setdefault(parent, []).append(r)
    return top_level, nested


def _log_run_start_header(mode_label: str, cfg: Config, log=print) -> None:
    """Разделитель + шапка параметров запуска (режим/SOURCE/TARGET/WORKDIR/TMP_EXTRACT_DIR/время
    старта) -- печатается в НАЧАЛЕ каждого прогона в рабочую консоль, общую для всех прогонов
    одной сессии GUI-мастера (см. CLAUDE.md, "Рабочая консоль GUI-мастера..." -- окно между
    прогонами сворачивается, не пересоздаётся, вывод предыдущего прогона остаётся выше в
    буфере). Живая просьба пользователя, 2026-08-24 ("раньше такое было", позже уточнено --
    добавить и рабочую папку) -- визуально отделять разные запуски друг от друга и сразу видеть
    параметры каждого, не листая вверх до предыдущего. Используется и `_run_impl()` (сборка/
    пробный прогон), и `run_analyze()` (анализ/паспорт) -- единственный общий момент, где эти
    четыре режима бы иначе печатали разный по форме заголовок."""
    log("")
    log("=" * 70)
    log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {mode_label}")
    log(f"  SOURCE: {cfg.source}")
    log(f"  TARGET: {cfg.target}")
    log(f"  WORKDIR: {cfg.workdir}")
    log(f"  TMP_EXTRACT_DIR: {cfg.tmp_extract}")
    log("=" * 70)


def report_environment(cfg: Config, log=print, stats: dict = None):
    # ТЗ-меню 2026-07-10, раздел 5: suppress_logs=True (интерактивный "пробный прогон")
    # сознательно НЕ создаёт TARGET заранее (ensure_target_layout() пропущен) -- на первом
    # прогоне в свежую папку disk_usage() на несуществующем пути упал бы FileNotFoundError.
    # Поднимаемся до ближайшего существующего предка (в худшем случае -- до буквы диска,
    # которая существует всегда) -- та же файловая система, тот же ответ.
    usage_probe = winlong(cfg.target)
    while not os.path.isdir(usage_probe):
        parent = os.path.dirname(usage_probe)
        if parent == usage_probe:
            break
        usage_probe = parent
    try:
        # Self-audit 2026-07-10: even the drive root can be missing (typo'd/removed drive
        # letter typed into "своя папка" with suppress_logs=True, so nothing upstream ever
        # touched the filesystem to catch it earlier) -- report_environment() is not the
        # place to invent a new fatal error for that, just say "unknown" and carry on.
        free = shutil.disk_usage(usage_probe).free
        log(f"Свободно на TARGET: {free / 1024**3:.2f} ГБ")
    except OSError:
        log(f"Свободно на TARGET: не удалось определить (диск {usage_probe} недоступен)")
    log(f"DRY_RUN={int(cfg.dry_run)}  SAMPLE_LIMIT={cfg.sample_limit}")
    nested = warn_if_target_nested_in_archive(cfg, log=log)
    # А.1: rename-финализация файлов из архива работает только на одном томе с TARGET --
    # предупредить один раз в начале прогона, если это не так, а не молча деградировать
    # к копированию без объяснения, почему сборка архивов вдруг медленнее ожидаемого.
    cross_volume_config = os.path.isdir(winlong(cfg.tmp_extract)) and not same_volume(cfg.tmp_extract, cfg.target)
    if cross_volume_config:
        log(f"ВНИМАНИЕ: TMP_EXTRACT_DIR ({cfg.tmp_extract}) на другом томе, чем TARGET "
            f"({cfg.target}) -- быстрая rename-финализация файлов из архивов недоступна, "
            f"будет использовано обычное копирование с hash-verify (медленнее). См. README.md.")
    if stats is not None:
        stats["warn_nested_target"] = stats.get("warn_nested_target", 0) + int(nested)
        # Фактическое число файлов, деградировавших до copy из-за кросс-volume tmp_extract,
        # копится в place_file() (p.5.3а) -- этот флаг только объясняет ПРИЧИНУ в консоли/логе.


class _RunState:
    def __init__(self, cfg, pool, date_ctx, run_logs, stats):
        self.cfg = cfg
        self.pool = pool
        self.date_ctx = date_ctx
        self.run_logs = run_logs
        self.stats = stats
        # Раунд 5 ревью, вариант D (REVIEW-HANDOFF.md): выделенное sqlite-соединение только
        # для сева archive_cache во время Фазы 2 -- см. _run_impl(), где оно открывается/
        # закрывается. None, если archive_hash_cache выключен или это dry_run (тогда
        # place_file() не вызывается вовсе -- сеять нечего).
        self.cache_conn = None
        self.dest_path_by_read_path = {}
        self.merged_albums_seen = set()
        self.stopped_for_space = False
        self.interrupted = False  # Ctrl+C-пакет: см. _run_impl() -- KeyboardInterrupt во время
                                   # основного цикла обхода источника ловится там же, где
                                   # стоит уже готовая for-петля, тем же приёмом, что и
                                   # stopped_for_space выше (break + finalize CSV/summary
                                   # нормально, не raw traceback).


def _ftype_bucket(ftype: str) -> str:
    """image/raw/video -- как есть, всё остальное (SourceItem.ftype=="other", не медиафайл по
    расширению) сворачивается в "other" -- 2026-07-26, по просьбе пользователя разбить
    статистику "Пополнения архива" на "итого + в т.ч. фото/RAW/видео" (report.py:
    _render_this_run()). "other" не показывается в отчёте отдельно (просьба была именно про
    фото/RAW/видео), но считается, чтобы бакеты в сумме всегда совпадали с "итого"."""
    return ftype if ftype in ("image", "raw", "video") else "other"


def _stats_inc_typed(stats: dict, key: str, ftype: str) -> None:
    """stats[key] -- общий счётчик (как раньше, ни один вызывающий код не должен был
    измениться), stats[f"{key}_{bucket}"] -- та же величина, разбитая по типу файла (см.
    _ftype_bucket()). Один инкремент вместо двух в каждом call site."""
    stats[key] = stats.get(key, 0) + 1
    typed_key = f"{key}_{_ftype_bucket(ftype)}"
    stats[typed_key] = stats.get(typed_key, 0) + 1


def _log_write_failure(item, dest_hint, e, cfg, run_logs, stats, log):
    """Security audit finding #1 (2026-07-10 follow-up): both resolve_dest_path() (its own
    os.makedirs) and place_file() can raise any OSError other than InsufficientSpace -- a file
    locked by an antivirus/indexer for a few hundred ms right after creation, a bad sector on a
    failing/scratched source disc (CD/DVD sources are an explicitly supported scenario, see
    RULES.md), a reserved device name (CON/NUL/...) that extreme truncate_segment() truncation
    happened to produce, or a destination path segment blocked by an unrelated same-named plain
    file. Before this helper existed, two of the three place_file() call sites in
    _process_record() only caught InsufficientSpace, and resolve_dest_path() itself was never
    guarded at all -- any other exception propagated all the way out of
    run()/run_for_source()/_main(), killing the ENTIRE run (and, for --source all, every
    remaining source in the batch) with a raw traceback instead of skipping the one problem file
    and continuing. Shared by all three call sites so the "log it, count it, keep going"
    behavior can't drift out of sync again. dest_hint is dest_dir, not dest_path -- dest_path
    may not exist yet if resolve_dest_path() itself is what failed."""
    log(f"  ОШИБКА записи {item.read_path} -> {dest_hint}: {e}")
    if cfg.debug:
        run_logs.debug_action("traceback: begin")
        for line in traceback.format_exc().splitlines():
            run_logs.debug_action(line)
        run_logs.debug_action("traceback: end")
    run_logs.unreadable(item.origin_display, f"write_failed: {e}")
    stats["write_failed"] = stats.get("write_failed", 0) + 1


def _process_dvd_item(item: "SourceItem", st: _RunState, log=print) -> str:
    """DVD-юнит-файл (item.dvd_dest_path уже установлен SourceWalker._handle_dvd_unit()) --
    вызывается ПЕРВЫМ в основном цикле, раньше analyze_batch()/_process_record(), которых
    этот файл не видит вообще: дедуп-решение "новый/дубль" для DVD-юнита принимается ЦЕЛИКОМ
    на всю папку VIDEO_TS ещё в самом обходе (см. _handle_dvd_unit()) -- сюда попадают только
    файлы уже решённо-нового юнита, find_album()/resolve_date()/Pool не участвуют.

    Тот же паттерн try/except, что и три InsufficientSpace-ветки _process_record() ниже
    (намеренно не общий helper -- вызывающих мест всего два, а раскладка по
    raw/image/video-веткам там завязана на decision/date, которых здесь нет вовсе).

    Возврат -- "ok" | "failed" | "stop" (2026-08-07, Раунд 71 ревью, фикс блокера: раньше был
    bool с той же семантикой, что и у _process_record() ("остановить прогон"), но вызывающая
    сторона не могла отличить успех от отдельного сбоя файла (оба давали False) -- а именно
    это отличие нужно, чтобы решить, регистрировать ли DVD-юнит в реестре dvd_units целиком,
    см. вызывающий цикл в _run_impl())."""
    cfg, run_logs, stats = st.cfg, st.run_logs, st.stats
    dest_path = item.dvd_dest_path
    try:
        if not cfg.dry_run:
            place_file(item, dest_path, item.dvd_sha256, cfg, run_logs, stats=stats)
            if st.cache_conn is not None:
                _seed_archive_cache(st.cache_conn, dest_path, item.size, item.dvd_sha256,
                                     None, None, None, None, None)
    except InsufficientSpace as e:
        log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). "
            f"Освободите место и запустите снова.")
        return "stop"
    except Exception as e:
        _log_write_failure(item, os.path.dirname(dest_path), e, cfg, run_logs, stats, log)
        return "failed"
    run_logs.appended(item.origin_display, dest_path, "DVD-Video (VIDEO_TS), скопирован целиком")
    run_logs.action(f"appended(dvd_unit): {item.origin_display} -> {dest_path}")
    stats["appended_videos"] = stats.get("appended_videos", 0) + 1
    stats["bytes_appended"] = stats.get("bytes_appended", 0) + item.size
    return "ok"


def _process_record(rec, st: _RunState, log=print):
    """Runs decide+date+placement+atomic-copy for one already-hashed record.
    Returns True if the run must stop (out of space)."""
    cfg, pool, date_ctx, run_logs, stats = st.cfg, st.pool, st.date_ctx, st.run_logs, st.stats
    item = rec.item

    # Зоны доверия (слой 2, см. classify_zone): в шумной зоне (кэши/temp) любое сомнение --
    # не медиа ИЛИ погранично-неуверенное (small_image/low_confidence_photo) -- уходит только
    # строкой в rejected_noise.csv, без копирования и без _disputed. Уверенно опознанное фото
    # (media_note не в этом наборе) архивируется как обычно из ЛЮБОЙ зоны, в т.ч. шумной.
    is_uncertain = (not rec.is_media) or (rec.media_note in ("small_image", "low_confidence_photo"))
    if item.zone == "noisy" and is_uncertain:
        run_logs.rejected_noise(item.origin_display, rec.media_note or "not_media")
        stats["rejected_noise"] += 1
        return False

    if not rec.is_media:
        dest_dir = safe_mirror_dir(cfg.dispute, os.path.dirname(item.rel_path))
        # rec.sha256 is only ever None for the size==0 special case (analyze_batch skips
        # hashing empty files) -- sha256_bytes(b"") is the real hash of an empty file, so
        # identical 0-byte placeholders still dedup correctly instead of comparing against
        # an empty-string sentinel that can never match anything.
        expected_sha = rec.sha256 or sha256_bytes(b"")
        try:
            dest_path, is_dup = resolve_dest_path(
                dest_dir, os.path.basename(item.rel_path),
                expected_sha, sha256_file, cfg.max_dest_path, stats=stats,
            )
            if not is_dup and not cfg.dry_run:
                # place_file: rename для файлов из архива на одном томе с dest, иначе
                # прежняя схема (temp file -> hash-verify -> atomic rename) -- см. IO_COPY.
                # _disputed всё так же внутри TARGET, так что степень защиты не меняется.
                place_file(item, dest_path, expected_sha, cfg, run_logs, stats=stats)
        except InsufficientSpace as e:
            log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). "
                f"Освободите место и запустите снова.")
            return True
        except Exception as e:
            _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
            return False
        if not is_dup:
            run_logs.disputed(item.origin_display, rec.media_note or "not_media", dest_path,
                               was_hidden=rec.is_hidden)
            run_logs.action(f"disputed: {item.origin_display} -> {dest_path}")
        _stats_inc_typed(stats, "disputed", item.ftype)
        return False

    decision = decide(pool, rec, cfg.mirror_raw)
    if cfg.debug and decision.debug_detail:
        run_logs.debug_action(f"near_dup: source={item.origin_display} vs "
                               f"existing={decision.matched_dest} criterion={decision.debug_detail} "
                               f"-> {decision.decision}")

    if decision.decision == "skipped_present":
        run_logs.skipped(item.origin_display, decision.matched_dest, decision.note)
        _stats_inc_typed(stats, decision.decision, item.ftype)
        # А.4: оценка "сэкономлено места дедупом" -- сколько байт НЕ скопировано (не "освобождено":
        # программа ничего не удаляет) благодаря тому, что содержимое уже есть в архиве/пуле
        # этого прогона. Near-dup (p.5.7) больше не сюда -- такие файлы теперь дописываются,
        # место для них не экономится.
        stats["bytes_saved_by_dedup"] += item.size
        return False

    if decision.decision == "raw_skipped":
        # MIRROR_RAW=false + есть парный JPEG уже в основном архиве -- избыточный RAW
        # осознанно не копируется никуда, только строка в skipped.csv. matched_with -- путь
        # JPEG-партнёра В АРХИВЕ (тот же lookup dest_path_by_read_path, что уже использует
        # raw_dest_dir() для варианта raw_mirrored/RAW_LAYOUT=sibling), не путь в источнике --
        # решение пользователя 2026-08-15 (детализация-xlsx, PROMPT_report_detail_xlsx.md):
        # колонка "Куда/с чем дуп" должна показывать, куда JPEG реально лёг, не где он лежал
        # на диске источника. Если JPEG сам не был скопирован в ЭТОМ прогоне (уже был в
        # архиве раньше -- skipped_present, dest_path_by_read_path не пополняется в той
        # ветке -- см. выше) или ещё не обработан на момент этого вызова -- lookup пуст,
        # откат на путь в источнике (лучше показать хоть что-то, чем пустую ячейку).
        jpeg_dest = st.dest_path_by_read_path.get(item.sibling_path) if item.sibling_path else None
        run_logs.skipped(item.origin_display, jpeg_dest or item.sibling_path or "", decision.note)
        stats["raw_skipped"] += 1
        return False

    if decision.decision == "raw_mirrored":
        dest_dir = raw_dest_dir(item, rec, cfg, st.dest_path_by_read_path, date_ctx)
        # REVIEW-HANDOFF.md, раунд 29 [БЛОКЕР]: без этого raw_without_jpeg в Albums\...
        # оставался невидим для дат/года/города -- та же причина, что и баг 9 (report.py не
        # может восстановить дату из dest без сегмента ByDate в пути), просто не покрытая
        # фиксом бага 9, который писал date= только в image/video-ветке ниже.
        date_value, _tier, _conf, _evidence, precision = resolve_date(
            date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
        if date_value is None:
            date_col = ""
        elif precision == "year":
            date_col = str(date_value.year)
        else:
            date_col = date_value.strftime("%Y-%m-%d")
        # Тот же живой фикс, что и в image/video-ветке ниже (2026-07-25) -- одинокий RAW
        # (raw_without_jpeg) может нести собственный GPS-тег не хуже JPEG, city-репортинг не
        # должен зависеть от того, есть ли у него парный JPEG.
        raw_place = place_for_gps(rec.gps_lat, rec.gps_lon, cfg.home_country) if cfg.place_lookup == "offline" else None

        try:
            dest_path, is_dup = resolve_dest_path(
                dest_dir, os.path.basename(item.rel_path), rec.sha256, sha256_file, cfg.max_dest_path,
                stats=stats)
            if not is_dup and not cfg.dry_run:
                place_file(item, dest_path, rec.sha256, cfg, run_logs, stats=stats)
                if st.cache_conn is not None:
                    _seed_archive_cache(st.cache_conn, dest_path, item.size, rec.sha256, rec.phash,
                                        rec.duration, rec.width, rec.height, rec.bitrate,
                                        rec.exif_dt, rec.exif_dt_source, rec.camera,
                                        rec.gps_lat, rec.gps_lon)
        except InsufficientSpace as e:
            log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). "
                f"Освободите место и запустите снова.")
            return True
        except Exception as e:
            _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
            return False
        if not is_dup:
            pool.add(PoolEntry(sha256=rec.sha256, ftype="raw", dest_path=dest_path, size=item.size))
            st.dest_path_by_read_path[item.read_path] = dest_path
            run_logs.appended(item.origin_display, dest_path, decision.note, date=date_col,
                               place=raw_place or "", camera=rec.camera or "")
            run_logs.action(f"appended(raw): {item.origin_display} -> {dest_path}")
            stats["raw_mirrored"] += 1
            stats["bytes_appended"] += item.size
            stats["bytes_appended_raw"] += item.size
        else:
            # Пакет A п.6 (SESSION-HANDOFF.txt, узкий, но реальный сценарий -- тот же класс,
            # что Раунд 70 ЗАМЕЧАНИЕ у image/video-ветки, см. её "identical_at_destination"
            # чуть ниже: index_archive() на Фазе 1 временно не смог прочитать уже существующий
            # файл, pool не знает его хеш, но resolve_dest_path() всё равно ловит совпадение
            # своим независимым от pool механизмом сравнения на диске). Раньше эта ветка молча
            # `return False` -- дедуп происходил верно (файл не копировался повторно), но не
            # оставлял НИКАКОГО следа ни в skipped.csv, ни в статистике -- пользователь не мог
            # узнать о нём иначе как из logs\.
            run_logs.skipped(item.origin_display, dest_path, "identical_at_destination")
            _stats_inc_typed(stats, "skipped_present", item.ftype)
            stats["bytes_saved_by_dedup"] += item.size
        return False

    # image / video appended_*
    date_value, tier, conf, evidence, precision = resolve_date(
        date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
    # p.5.3а: heuristic co-occurrence count, not a proven causal one -- exiftool (a separate
    # subprocess, not covered by winlong()) can silently fail to read EXIF on a source path
    # past the legacy 260-char MAX_PATH, degrading the date straight to Tier C (see
    # RULES.md/2.5г). Any other Tier-C cause (folder cluster, mtime, ...) at a long path also
    # gets counted here -- there's no separate signal distinguishing "long path caused this"
    # from "no EXIF for some other reason", so this only tells the user "look here", not "this
    # is definitely why".
    if tier == "C" and len(item.read_path) > 259:
        stats["warn_tier_c_long_path"] = stats.get("warn_tier_c_long_path", 0) + 1

    album, subpath, album_prefix = find_album(item.rel_path, item.archive_boundary_idx,
                                               dump_names=cfg.dump_segment_names_lower,
                                               dump_prefixes=cfg.dump_segment_prefixes_tuple,
                                               bydate_only=cfg.source_bydate_only)
    # Живая находка (2026-07-25, боевой прогон F:\): раньше place_for_gps() вызывался ТОЛЬКО
    # в ветке "нет альбома" ниже (нужен для имени папки ByDate\...) -- файлы, попавшие в
    # Albums\... (у альбома уже есть человеческое имя, не нужен для ПУТИ), никогда не получали
    # geo-lookup вообще, из-за чего report.py не мог заполнить "География" ни диаграммой, ни
    # списком городов для архивов, целиком состоящих из альбомов. Считаем place один раз здесь,
    # независимо от маршрутизации -- используется в folder-naming только для ByDate (см. ниже),
    # но теперь всегда передаётся в run_logs.appended() для отчёта (см. колонку "place").
    place = place_for_gps(rec.gps_lat, rec.gps_lon, cfg.home_country) if cfg.place_lookup == "offline" else None
    if cfg.debug:
        segments = item.rel_path.split("/")[:-1]
        if segments:
            deepest = segments[-1]
            has_tag = deepest.strip().endswith(DUMP_TAG)
            tag_flag = "да" if has_tag else "нет"
            if album:
                run_logs.debug_action(f"album_decision: segment='{deepest}' tag={tag_flag} -> album='{album}'")
            else:
                run_logs.debug_action(f"album_decision: segment='{deepest}' tag={tag_flag} -> dump")
    final_decision = decision.decision
    if album:
        # Задача 0/B (SESSION-HANDOFF.txt, "проактивные советы для [2] Пробный прогон"):
        # структурный профиль альбома (сколько файлов, разброс по годам/камерам/дата-
        # подпапкам) -- для report.py's эвристики "похоже на облачную синхронизацию, не
        # курируемый альбом" (Задача B). Пишется БЕЗУСЛОВНО -- порог/лимит применяются только
        # на рендере (report.py), не здесь, иначе многоисточниковый --dry-run/--source all не
        # смог бы честно просуммировать через разные вызовы (см. _sum_stats()). Только
        # image/video/raw попадают в основную album-ветку (raw_mirrored -- отдельная ветка
        # выше, там своя маршрутизация через raw_dest_dir(), профиль туда сознательно не
        # протянут -- минимальный охват для этой эвристики, RAW без JPEG-партнёра в альбоме
        # статистически редок).
        profile = stats.setdefault("album_profiles", {}).setdefault(
            album_prefix,
            {"name": album, "n": 0, "years": set(), "cameras": set(), "date_subdirs": set()},
        )
        profile["n"] += 1
        if date_value is not None:
            profile["years"].add(date_value.year)
        if rec.camera:
            profile["cameras"].add(rec.camera)
        for seg in subpath:
            if _looks_like_date_subdir(seg):
                profile["date_subdirs"].add(seg)
        album_dir = build_album_dest_dir(cfg.albums_root, album_prefix, subpath)
        # Раунд 77 ревью (REVIEW-HANDOFF.md, [ЗАМЕЧАНИЕ] 1): раньше ключевался голым
        # album_prefix (только ВЕРХНИЙ сегмент пути) -- под новой моделью ("каждая папка --
        # свой альбом") это больше не 1:1 с физической папкой назначения: PlaceA/Sub1 и
        # PlaceA/Sub2 делят один и тот же album_prefix ("PlaceA"), поэтому проверка для Sub2
        # короткозамыкала на том, что Sub1 уже "видел" этот album_pref, и реальная дозапись в
        # уже существующую на диске Sub2 молча не логировалась в albums_merged.csv. album_dir
        # (== dest_dir, уже вычислен строкой выше) уникален на физическую папку -- ключуем по
        # нему.
        if album_dir not in st.merged_albums_seen:
            st.merged_albums_seen.add(album_dir)
            if os.path.isdir(winlong(album_dir)):
                run_logs.album_merged(album, item.origin_display)
        dest_dir = album_dir
    elif date_value is None:
        dest_dir = safe_mirror_dir(cfg.undated_root, os.path.dirname(item.rel_path))
        final_decision = "undated"
    else:
        dest_dir = build_bydate_dest_dir(cfg.bydate_root, date_value, precision, place,
                                          cfg.bydate_granularity)

    try:
        dest_path, is_dup = resolve_dest_path(
            dest_dir, os.path.basename(item.rel_path), rec.sha256, sha256_file, cfg.max_dest_path,
            stats=stats)
        if not is_dup and not cfg.dry_run:
            place_file(item, dest_path, rec.sha256, cfg, run_logs, stats=stats)
            if st.cache_conn is not None:
                _seed_archive_cache(st.cache_conn, dest_path, item.size, rec.sha256, rec.phash,
                                    rec.duration, rec.width, rec.height, rec.bitrate,
                                    rec.exif_dt, rec.exif_dt_source, rec.camera,
                                    rec.gps_lat, rec.gps_lon)
    except InsufficientSpace as e:
        log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). Освободите место и запустите снова.")
        return True
    except Exception as e:
        _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
        return False

    if is_dup:
        run_logs.skipped(item.origin_display, dest_path, "identical_at_destination")
        _stats_inc_typed(stats, "skipped_present", item.ftype)
        stats["bytes_saved_by_dedup"] += item.size  # А.4
        return False

    pool_ftype = "image" if item.ftype == "image" else "video"
    pool.add(PoolEntry(
        sha256=rec.sha256, ftype=pool_ftype, dest_path=dest_path, size=item.size,
        aspect=rec.aspect, width=rec.width, height=rec.height, phash=rec.phash,
        duration=rec.duration, bitrate=rec.bitrate, has_camera=bool(rec.camera),
    ))
    st.dest_path_by_read_path[item.read_path] = dest_path
    flags = rec.media_note if rec.media_note in ("small_image", "low_confidence_photo") else ""
    # SESSION-HANDOFF.txt, баг 9: писать дату отдельной колонкой -- report.py не может
    # восстановить её из dest для файлов в Albums\... (нет сегмента ByDate в пути вообще).
    # precision=="year" -- только год достоверен (см. resolve_date()/build_bydate_dest_dir()),
    # писать месяц/день оттуда же было бы ложной точностью.
    if date_value is None:
        date_col = ""
    elif precision == "year":
        date_col = str(date_value.year)
    else:
        date_col = date_value.strftime("%Y-%m-%d")
    # 4.6 (PROMPT_report_marketing.md): длительность видео -- только для video, только если
    # реально определена (ffprobe-подобное чтение контейнера может не суметь, rec.duration
    # тогда None).
    duration_col = str(rec.duration) if pool_ftype == "video" and rec.duration is not None else ""
    run_logs.appended(item.origin_display, dest_path, decision.note or decision.decision,
                       flags=flags, date=date_col, duration=duration_col, place=place or "",
                       camera=rec.camera or "")
    run_logs.action(f"appended: {item.origin_display} -> {dest_path}")
    if decision.matched_dest is not None and decision.decision in (
            "appended_near_dup", "appended_better", "appended_crop"):
        # PROMPT_archive_report.md, 1.2б: raw-путь не участвует -- decide() никогда не
        # возвращает near-dup-семейство для raw, этот блок только для image/video.
        run_logs.near_dup_edge(item.origin_display, dest_path, decision.matched_dest,
                                decision.decision, decision.hamming)
    if tier != "A" and date_value is not None:
        run_logs.date_review(dest_path, date_value, tier, conf, evidence, item.origin_display)
        if cfg.debug:
            run_logs.debug_action(f"date: dest={dest_path} tier={tier} confidence={conf} "
                                   f"evidence={evidence} source={item.origin_display}")
    elif tier == "D":
        # REVIEW-HANDOFF.md, раунд 3: Tier D всегда date_value=None (resolve_date()), поэтому
        # никогда не проходит условие выше -- без этого report.py не может отличить "нет даты
        # вообще" от "точная EXIF-дата" (обе категории одинаково отсутствуют в dates_review.csv).
        run_logs.undated_media(item.origin_display, dest_path)
        # 2026-08-08 (Пакет A п.3, SESSION-HANDOFF.txt, живая находка): stats["undated"] (см.
        # summary.txt/"Без надёжной даты"/report.py "не удалось распознать дату") раньше
        # инкрементировался ТОЛЬКО через final_decision=="undated" ниже -- ветка "нет альбома И
        # нет даты" (см. выше). Файл БЕЗ надёжной даты, но с найденным альбомом (final_decision
        # остаётся "appended_new"/"appended_better"/... -- альбом решает маршрут, не дата) не
        # учитывался вовсе, хотя undated_media.csv (строка выше) пишет его безусловно -- живой
        # прогон дал прямое расхождение (CSV: 4 файла, "Без надёжной даты: 0"). Обе подписи
        # обещают "не удалось определить дату" без оговорки про альбом -- считаем здесь ЛЮБОЙ
        # Tier D файл с альбомом, дополняя (не дублируя) случай без альбома, который по-прежнему
        # считается ниже через final_decision -- вместе они дают точное совпадение с
        # undated_media.csv.
        if album:
            stats["undated"] = stats.get("undated", 0) + 1
    stats[final_decision] = stats.get(final_decision, 0) + 1
    # 2026-07-26, по просьбе пользователя: "похожие кадры" в _render_this_run() -- отдельный
    # агрегат сверх трёх decision-ключей выше (appended_near_dup/appended_better/appended_crop
    # уже существуют по отдельности, report.py сам суммирует их в n_near_dup) -- raw сюда
    # никогда не попадает, decide() не возвращает near-dup-семейство для raw (см. комментарий
    # у near_dup_edge() чуть выше), поэтому бакет только image/video, без "other"/raw.
    if final_decision in ("appended_near_dup", "appended_better", "appended_crop"):
        near_dup_key = f"near_dup_{pool_ftype}"
        stats[near_dup_key] = stats.get(near_dup_key, 0) + 1
    stats["bytes_appended"] += item.size
    stats[f"bytes_appended_{pool_ftype}"] += item.size
    # А.4: разбивка "уникальных" по типу для итоговой сводки (фото vs видео)
    stats["appended_images" if pool_ftype == "image" else "appended_videos"] += 1
    # Security audit finding #5: p.5.7 made near-dup always append (never skip) -- track its
    # bytes separately so unbounded growth from a hostile/corrupted burst-shot SOURCE is
    # visible in the summary instead of hiding inside the aggregate archive size.
    if decision.decision in ("appended_near_dup", "appended_better", "appended_crop"):
        stats["bytes_near_dup"] = stats.get("bytes_near_dup", 0) + item.size
    return False


def build_final_summary(stats: dict, walker: "SourceWalker", unreadable_count: int,
                         pool: "Pool", processed_count: int) -> str:
    """А.4: человекочитаемый итог прогона поверх УЖЕ посчитанных чисел -- чистая агрегация
    существующих Фаза-4/4.5/5 решений и финального состояния pool, без новой бизнес-логики.
    "Итоговый архив" -- кумулятивное состояние ВСЕГО архива после этого прогона (то, что
    было в TARGET до старта, проиндексированное в Фазе 1, плюс дописанное сейчас), а не
    только дельта этого запуска -- см. RULES.md/README.md."""
    n_archives_extracted = sum(1 for _, status, _ in walker.archive_logs if status == "archive_extracted")
    # p.5.7: near-dup no longer means "skipped" -- appended_near_dup/appended_better/
    # appended_crop are the three near-dup outcomes, all now actually copied into the archive.
    n_near_dup = stats["appended_near_dup"] + stats["appended_better"] + stats["appended_crop"]
    n_broken_or_unreadable = stats["disputed"] + unreadable_count

    n_pool_images = sum(1 for e in pool.by_sha.values() if e.ftype == "image")
    n_pool_videos = sum(1 for e in pool.by_sha.values() if e.ftype == "video")
    pool_bytes = sum(e.size or 0 for e in pool.by_sha.values())

    # 2026-07-11: сумма пропусков по имени папки (HARD_EXCLUDE_DIRS/default_exclude_dirs/
    # extra_exclude_dirs) + по гейту системных папок -- разбивка по причинам только в
    # actions.log (см. _run_impl), тут только итоговое число, чтобы не раздувать сводку.
    n_excluded_dirs = sum(c for _, _, c in walker.excluded_dir_summary()) + len(walker.system_dir_skips)

    lines = [
        "\n--- Итог прогона ---\n",
        f"Обработано: {processed_count} файлов\n",
        f"Фотографий: {stats['appended_images']} | Видео: {stats['appended_videos']}\n",
        f"Точных дубликатов: {stats['skipped_present']} | Near-dup: {n_near_dup} "
        f"(~{stats.get('bytes_near_dup', 0) / 1024**3:.2f} ГБ, все сохранены -- похожие кадры "
        f"не удаляются, см. README)\n",
        f"Битых/нечитаемых: {n_broken_or_unreadable} | Без надёжной даты: {stats['undated']}\n",
        f"Архивов распаковано: {n_archives_extracted}\n",
        f"В _Unsorted: {stats['disputed']} | В rejected_noise: {stats['rejected_noise']}\n",
        f"Сэкономлено места при сборке (точные дубли не копировались повторно): "
        f"~{stats['bytes_saved_by_dedup'] / 1024**3:.2f} ГБ\n",
        f"Итоговый архив: {n_pool_images} уникальных фото + {n_pool_videos} видео, "
        f"размер {pool_bytes / 1024**3:.2f} ГБ\n",
    ]
    if n_excluded_dirs:
        lines.append(f"Пропущено служебных/системных папок: {n_excluded_dirs} "
                      f"(подробности -- actions.log)\n")
    return "".join(lines)


def run(cfg: Config, log=print, shared_pool=None, print_summary=True):
    """p.5.4б: весь реальный прогон обёрнут TargetLock -- см. его докстринг про TOCTOU-гонку,
    единственную найденную дыру в защите TARGET от параллельных запусков. Исключение --
    cfg.suppress_logs (ТЗ-меню 2026-07-10, раздел 5): интерактивный "пробный прогон" никогда
    не пишет в TARGET (см. _run_impl), поэтому лочить нечего -- LOCK-файл сам по себе создал
    бы __служебные_файлы\\, что suppress_logs как раз обязан НЕ делать.

    shared_pool (раунд 5 ревью, REVIEW-HANDOFF.md, вариант A): опциональный Pool из
    предыдущего вызова run() в рамках ОДНОГО batch-процесса (несколько SOURCE подряд на один
    TARGET) -- TargetLock тем не менее берётся заново на каждый SOURCE (дёшево, не архивный
    пересканирование, риск гонки не выше уже принятого TOCTOU в докстринге TargetLock).

    print_summary (пакет п.4, SESSION-HANDOFF.txt): пробрасывается в _run_impl() как есть --
    см. её докстринг."""
    if cfg.suppress_logs:
        return _run_impl(cfg, log=log, shared_pool=shared_pool, print_summary=print_summary)
    with TargetLock(cfg.target, log=log, dry_run=cfg.dry_run):
        return _run_impl(cfg, log=log, shared_pool=shared_pool, print_summary=print_summary)


def _run_impl(cfg: Config, log=print, shared_pool=None, print_summary=True):
    run_start = time.monotonic()
    # p.5.3а: stats создаётся ДО отчёта окружения -- report_environment() тоже пишет в него
    # счётчики предупреждений (вложенность TARGET, кросс-volume tmp_extract), которые потом
    # уходят в обогащённый summary.txt вместе со всем остальным.
    stats = {
        "appended_new": 0, "appended_better": 0, "appended_crop": 0, "appended_uncertain": 0,
        "appended_near_dup": 0,  # p.5.7: near-dup image/video, appended (not skipped)
        "skipped_present": 0, "disputed": 0, "raw_mirrored": 0,
        "raw_skipped": 0, "rejected_noise": 0,
        "undated": 0, "bytes_appended": 0,
        # А.4 (итоговая человекочитаемая сводка) -- чистые агрегаты поверх решений выше,
        # никакой новой бизнес-логики не добавляют:
        "appended_images": 0, "appended_videos": 0, "bytes_saved_by_dedup": 0,
        # report.py, "Объём по категориям" (_render_run_copied()): та же сумма, что даёт
        # bytes_appended, но разбита по фото/видео/RAW -- item.size известен независимо от
        # cfg.dry_run (SOURCE физически существует всегда), в отличие от os.path.getsize(dest),
        # которым report.py считал байты раньше (dest не существует в dry-run, см. _row_size()).
        # DVD (VIDEO_TS) сюда сознательно не входит -- тот же принцип, что уже применяет
        # report.py к bytes_by_kind (DVD -- отдельный именованный пункт, не смешивается с
        # "видео").
        "bytes_appended_image": 0, "bytes_appended_video": 0, "bytes_appended_raw": 0,
        # Security audit finding #5: bytes copied specifically as near-dup (see build_final_summary)
        "bytes_near_dup": 0,
        # p.5.3а: счётчики предупреждений по типам, для обогащённого summary.txt
        "warn_nested_target": 0, "warn_cross_volume_tmp_extract": 0,
        "warn_path_truncated": 0, "warn_tier_c_long_path": 0,
    }

    _log_run_start_header("Пробный прогон" if cfg.dry_run else "Сборка архива", cfg, log=log)
    log("=== Фаза 0: окружение ===")
    # Остатки чужого прошлого прерванного прогона (Ctrl+C/крах) -- см. _cleanup_own_tmp_extract_
    # entries() докстринг. Симметричный вызов после основного цикла ниже (после except
    # KeyboardInterrupt) подчищает то же самое для ЭТОГО прогона, если он сам будет прерван.
    _cleanup_own_tmp_extract_entries(cfg, log=log)

    if not cfg.suppress_logs:
        ensure_target_layout(cfg)
        check_rules_version(cfg, log=log)
        # Остатки atomic_copy()'s staging-файлов от аварийно убитого прошлого прогона -- см.
        # _sweep_stale_photosort_tmp_files()'s докстринг. Безопасно здесь -- run() уже держит
        # TargetLock на этот TARGET (see её __enter__/__exit__), конкурентного владельца этих
        # файлов быть не может.
        _sweep_stale_photosort_tmp_files(cfg.target, log=log)
    report_environment(cfg, log=log, stats=stats)
    phase0_end = time.monotonic()

    if shared_pool is not None:
        # Раунд 5 ревью (REVIEW-HANDOFF.md, вариант A): в рамках одного batch-процесса
        # (несколько SOURCE подряд на один TARGET) архив уже был полностью проиндексирован
        # первым источником, а pool.add() ниже по функции уже держит всё, что каждый
        # следующий источник сам дописал в TARGET -- повторное индексирование здесь считало
        # бы ту же самую (в рамках процесса неизменную, если не считать самой программы)
        # файловую систему заново без надобности. Одиночный SOURCE (shared_pool=None,
        # подавляющее большинство запусков) эту ветку не видит вообще -- полное
        # индексирование при первом/единственном вызове run() в процессе не меняется.
        log("=== Фаза 1: индекс архива (база дедупа) — пропущена, использован общий пул этого batch'а ===")
        pool = shared_pool
        phase1_end = time.monotonic()
    else:
        conn = db_reset(cfg.index_db)
        log("=== Фаза 1: индекс архива (база дедупа) ===")
        index_archive(cfg, conn, log=log)
        pool = build_pool_from_archive_table(conn)
        conn.close()  # не нужен дальше в этом прогоне; важно закрывать явно для --source all,
                      # где run() вызывается многократно в одном процессе на один и тот же work.db
        phase1_end = time.monotonic()

    # PROMPT_archive_report.md, 1.2а: CollectingRunLogs -- третья реализация той же
    # поверхности методов -- собирает report.html-данные в памяти для suppress_logs=True
    # (интерактивный [2] "Пробный прогон", единственный вызывающий с этим флагом сегодня).
    # CLI --dry-run (suppress_logs=False) продолжает писать настоящие CSV в TARGET, как
    # раньше -- report.html для него читает те же файлы, что и TARGET-уровень (см.
    # _finalize_target_report), не через этот класс.
    run_logs = CollectingRunLogs() if cfg.suppress_logs else RunLogs(cfg.logs)
    date_ctx = DateContext()

    st = _RunState(cfg, pool, date_ctx, run_logs, stats)
    # Раунд 5 ревью, вариант D: выделенное соединение для сева archive_cache в течение всей
    # Фазы 2 -- Фаза 1 либо уже закрыла своё conn (ветка else выше), либо не открывала его
    # вовсе (shared_pool). dry_run никогда не доходит до place_file() (см. _process_record),
    # поэтому сеять там нечего -- не открываем соединение впустую.
    #
    # Речь пользователя, 2026-08-02: cfg.target (не work.db в WORKDIR) -- ensure_target_layout()
    # выше уже создала __служебные_файлы\ до этой точки (suppress_logs=False здесь всегда,
    # иначе dry_run=True и мы не дошли бы до этой ветки), так что _open_archive_cache_conn()
    # надёжно откроет соединение, не вернёт None.
    st.cache_conn = (_open_archive_cache_conn(cfg.target)
                      if (cfg.archive_hash_cache and not cfg.dry_run) else None)

    # 2026-08-07: реестр уже архивированных DVD-юнитов (см. секцию "DVD-VIDEO UNITS" выше) --
    # НАМЕРЕННО читается из ТОГО ЖЕ st.cache_conn, не отдельного соединения. Живая находка на
    # этом же шаге: отдельное безусловное _open_archive_cache_conn(cfg.target) здесь ломало
    # test_suppress_logs_dry_run_does_not_write_archive_cache_into_target -- сам факт открытия
    # соединения sqlite3.connect()+executescript(SCHEMA) на ЕЩЁ НЕ существующий archive_cache.db
    # уже создаёт файл ВНУТРИ TARGET, даже если из него потом только читают -- нарушает
    # задокументированную гарантию "suppress_logs/dry_run никогда не пишет в TARGET". Раз
    # st.cache_conn уже корректно гейтится этим же условием (cfg.archive_hash_cache and not
    # cfg.dry_run), реестр просто наследует то же ограничение -- при dry_run/выключенном
    # archive_hash_cache он пуст (VIDEO_TS всегда выглядит "новым"), но TARGET не трогается.
    # Известное упрощение: [2] Пробный прогон/CLI analyze (run_analyze(), другой цикл) этот
    # реестр не читает вообще -- превью всегда покажет VIDEO_TS как "новый" диск, даже если он
    # уже реально заархивирован; сама РЕАЛЬНАЯ сборка (эта функция, не dry_run) решает правильно.
    dvd_unit_registry = {}
    if st.cache_conn is not None:
        for fp, dest in st.cache_conn.execute("SELECT fingerprint, dest_path FROM dvd_units"):
            dvd_unit_registry[fp] = dest

    log("=== Фаза 2/2а: обход источника ===")

    processed_count = 0
    unreadable_count = 0
    # 2026-07-26, по просьбе пользователя: разбивка unreadable_count по типу файла (фото/RAW/
    # видео/прочее) для _render_this_run() -- отдельные счётчики, не Counter(), чтобы не менять
    # существующую переменную unreadable_count (используется ниже и в summary_lines как есть).
    unreadable_count_by_type = Counter()
    pending_retry = []  # SourceItem list: read failed 3x but the file persists (not archive-tmp)
                         # -> gets one more attempt at the end of the run.
    dvd_unit_ok_counts = {}  # 2026-08-07 (Раунд 71 ревью, фикс блокера): fingerprint -> сколько
        # файлов этого DVD-юнита реально успешно скопировано (_process_dvd_item() вернул "ok").
        # walker.dvd_units_copied регистрирует юнит НАМЕРЕНИЕМ, на этапе обхода, до того как
        # хоть один файл скопирован -- ниже, после цикла, юнит считается архивированным (и
        # только тогда попадает в БД/отчёт) лишь если это число сошлось с n_files юнита
        # целиком; иначе частично скопированный юнит остался бы навсегда помечен "готов" в
        # реестре, а недостающие файлы -- потеряны без следа на следующем прогоне (см.
        # REVIEW-HANDOFF.md, Раунд 71, БЛОКЕР).
    dvd_unit_failed_fingerprints = set()  # 2026-08-07 (Раунд 73 ревью, фикс замечания):
        # отдельно от dvd_unit_ok_counts -- fingerprint попадает сюда, ТОЛЬКО когда
        # _process_dvd_item() реально вернул "failed"/"stop" (настоящая ошибка записи/нехватка
        # места) хотя бы для одного файла юнита. Нужен отдельно от "ok_counts < n_files", т.к.
        # ТО ЖЕ САМОЕ условие неполноты срабатывает и без единой ошибки -- --sample-limit может
        # оборвать основной цикл (`break` ниже) серединой юнита: файлы, успевшие дойти до
        # place_file() ДО обрыва, реально и без сбоев лежат на диске, cleanup ниже не должен их
        # трогать (тот же принцип "оставить как есть", что и у обычных файлов под
        # --sample-limit, см. REVIEW-HANDOFF.md, Раунд 73, ЗАМЕЧАНИЕ).

    # Задача 4: total неизвестен заранее (walker -- генератор, находит файлы и вложенные
    # архивы по ходу обхода) -- бар без total (без tqdm-встроенного %/ETA -- у tqdm это
    # умеет считаться только от total). SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2:
    # "плановое" время в статус-строке ([прошло/план]) считается САМИ, не через total/tqdm
    # ETA -- см. _quick_media_count_estimate() ниже + ProgressReporter(two_line=True).
    # Пакет "человеческие названия фаз" (SESSION-HANDOFF.txt): один и тот же цикл ниже и
    # сканирует источник, и (если не dry_run) копирует в TARGET -- название бара должно
    # честно отражать оба случая, не обещать копирование там, где ничего не пишется.
    _source_phase_desc = _DRY_RUN_PHASE_DESC if cfg.dry_run else _BUILD_PHASE_DESC
    # SESSION-HANDOFF.txt п.12: живой постфикс "своб.XГБ" вводил в заблуждение в dry-run --
    # tmp_extract\ внутри TARGET реально получает байты при распаковке архивов даже в
    # пробном прогоне, из-за чего число "уменьшается" в ощутимо read-only режиме. Показывать
    # только там, где TARGET действительно меняется -- в режиме копирования.
    _disk_usage_path = None if cfg.dry_run else cfg.target
    # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2, алгоритм ETA: быстрый
    # предпересчёт SOURCE (без stat/hash/классификации, см. _quick_media_count_estimate())
    # СИНХРОННО перед основным циклом -- согласовано явно НЕ фоновым потоком (один физический
    # источник, параллельный обход того же дерева рискует замедлить оба прохода на медленных
    # дисках/сети). Свой простой индикатор -- переиспользует ОБЫЧНЫЙ (не two_line) режим
    # ProgressReporter, тот же принцип "не должно выглядеть зависшим", без ETA у самого себя.
    with ProgressReporter(total=None, desc=" Оцениваю объём работы", unit="файл") as est_bar:
        total_estimate = _quick_media_count_estimate(cfg.source, cfg, on_progress=est_bar.update)
    with ProgressReporter(total=None, desc=_source_phase_desc, unit="файл",
                           disk_usage_path=_disk_usage_path, two_line=True,
                           total_estimate=total_estimate) as bar:
        walker = SourceWalker(cfg, log=log, object_line_cb=bar.write_object_line,
                               transient_op_cb=bar.set_transient_op,
                               object_progress_cb=bar.add_object_progress,
                               dvd_unit_registry=dvd_unit_registry, show_placement_letter=True,
                               heavy_notice_cb=bar.write_heavy_notice)
        # NB: items are analyzed and placed one at a time (no read-ahead batching).
        # Files extracted from an archive live in TMP_EXTRACT only until that archive's
        # generator scope closes (walker.py cleans up in a `finally` right after its last
        # item is yielded) -- pulling several items ahead into a batch before processing
        # them risks the physical file already being deleted by the time we hash/copy it.
        try:
            for item in walker.walk():
                # 2026-08-23, по прямой просьбе пользователя: пауза по пробелу, см.
                # _check_pause_keypress()'s докстринг -- между файлами, не внутри одного.
                _check_pause_keypress(log=log)
                if cfg.sample_limit and processed_count >= cfg.sample_limit:
                    break

                # 2026-08-07: DVD-юнит-файл -- дедуп-решение УЖЕ принято целиком на весь диск
                # в _handle_dvd_unit() (см. SourceItem.dvd_dest_path), analyze_batch()/
                # _process_record() этот item не должны видеть вообще (ни find_album(), ни
                # resolve_date(), ни Pool near-dup) -- см. _process_dvd_item().
                if item.dvd_dest_path is not None:
                    processed_count += 1
                    note = f"копирование DVD-видео ({_fmt_size_gb(item.size)})"
                    bar.update(0, note=note)
                    dvd_result = _process_dvd_item(item, st, log=log)
                    if dvd_result == "ok":
                        fp = item.dvd_unit_fingerprint
                        dvd_unit_ok_counts[fp] = dvd_unit_ok_counts.get(fp, 0) + 1
                        # 2026-08-08 (живой боевой прогон F:->D:, реальная находка): раньше
                        # dvd_unit_registry обновлялся ТОЛЬКО в финализации после конца ВСЕГО
                        # обхода (см. ниже) -- второй физически идентичный VIDEO_TS, встреченный
                        # ПОЗЖЕ в том же прогоне, не мог быть пойман как дубль ПЕРВОГО, тоже
                        # скопированного в этом же прогоне (~132 МБ впустую на живом архиве
                        # пользователя). walker.walk() -- обычный генератор без read-ahead (см.
                        # комментарий у его создания выше), поэтому все файлы одного юнита
                        # обрабатываются здесь подряд, без чужих между ними -- как только счётчик
                        # ok сходится с n_files юнита, регистрируем его немедленно в ТОМ ЖЕ
                        # объекте dvd_unit_registry, что уже передан walker'у (identity важна --
                        # см. фикс `or {}` -> `is not None` в SourceWalker.__init__ выше), чтобы
                        # walker._handle_dvd_unit() увидел его на следующем же юните ЭТОГО
                        # прогона, не дожидаясь следующего запуска программы.
                        unit_entry = next(
                            (u for u in walker.dvd_units_copied if u["fingerprint"] == fp), None)
                        if unit_entry is not None and dvd_unit_ok_counts[fp] >= unit_entry["n_files"]:
                            dvd_unit_registry[fp] = unit_entry["dest_path"]
                    else:
                        # "failed"/"stop" -- настоящая ошибка (не путать с юнитом, до которого
                        # цикл просто не дошёл целиком из-за --sample-limit/конца SOURCE, см.
                        # dvd_unit_failed_fingerprints выше).
                        dvd_unit_failed_fingerprints.add(item.dvd_unit_fingerprint)
                    if dvd_result == "stop":
                        st.stopped_for_space = True
                        bar.update(1, note=note)
                        break
                    bar.update(1, note=note)
                    continue

                # SESSION-HANDOFF.txt, редизайн живого вывода Фазы 2: порог 200 МБ убран
                # полностью (согласовано с пользователем) -- показывать формат размера ВСЕГДА,
                # для любого видео, независимо от размера; само число уже говорит, большое
                # видео или маленькое, спец-текст под порог был нужен только пока размера не
                # было в строке вовсе.
                # 2026-08-24, живая просьба пользователя (единый потолок ~22-25 символов для
                # ВСЕХ текстов поля операции, ведущий пробел -- та же конвенция, что и у
                # resting/transient-констант рядом): "хеширование видеофайла (X)" (это note,
                # для two_line-бара ЗАМЕНЯЕТ поле операции, см. update()'s self._transient_op =
                # note) было заметно длиннее потолка (32 символа с "999.9ГБ") -- короткий noun,
                # тем же стилем, что уже применён к транзиентным операциям (" В архиве"/
                # " Извлекаю (X)").
                note = f" Видео ({_fmt_size_gb(item.size)})" if item.ftype == "video" else None
                # Раунд 6 ревью (REVIEW-HANDOFF.md, живой баг-репорт "программа зависла"): note
                # должен появиться на экране ДО блокирующего analyze_batch(), не после -- иначе
                # бар всю паузу молча показывает состояние предыдущего файла, что визуально
                # неотличимо от зависания. n=0 -- только обновление текста, счётчик не трогаем
                # (тот же приём, что и self.update(0) в ProgressReporter.__init__).
                bar.update(0, note=note)
                records = analyze_batch([item], retries=cfg.read_retry_count, retry_delay=cfg.read_retry_delay,
                                         small_image_px=cfg.small_image_px, log=log, pool=pool)

                for rec in records:
                    processed_count += 1

                    if rec.read_error:
                        if item.read_path.startswith(cfg.tmp_extract + os.sep):
                            # physical file will vanish once this archive's TMP_EXTRACT is cleaned up
                            # -- no later retry is possible, log it now.
                            run_logs.unreadable(item.origin_display, rec.read_error_msg)
                            unreadable_count += 1
                            unreadable_count_by_type[_ftype_bucket(item.ftype)] += 1
                        else:
                            pending_retry.append(item)
                        bar.update(1, note=note)
                        continue

                    if _process_record(rec, st, log=log):
                        st.stopped_for_space = True
                        bar.update(1, note=note)
                        break
                    bar.update(1, note=note)

                if st.stopped_for_space:
                    break
        except KeyboardInterrupt:
            # Ctrl+C-пакет (по прямой просьбе пользователя, ТОЛЬКО [3]/CLI archive): ловится
            # здесь, не в main(), чтобы CSV-логи/summary ниже по функции успели дописаться
            # нормально (RunLogs пишет построчно по ходу работы -- то, что уже успело
            # обработаться, уже на диске) -- тот же приём, что и st.stopped_for_space выше.
            # НЕ проглатывается совсем: st.interrupted пробрасывается через RunResult до
            # вызывающего кода (_bare_launch_run_build()/_main()), который генерирует отчёт с
            # баннером прерывания и заново возбуждает KeyboardInterrupt -- поведение "Ctrl+C
            # останавливает программу" не меняется, только теперь есть отчёт перед выходом.
            st.interrupted = True
            bar.mark_interrupted()  # "обработано объектов XX%" не форсирует 100% на прерванном прогоне

        # Живая находка пользователя, 2026-08-09: временные распакованные папки архива
        # (__служебные_файлы\tmp_extract\<hash>\...) оставались на диске после Ctrl+C ВО ВРЕМЯ
        # самого прогона (в т.ч. --dry-run) -- раньше только Фаза 0 в начале СЛЕДУЮЩЕГО прогона
        # подчищала такие остатки, ничего не делало сразу после ЭТОГО прерывания. Безусловно
        # (не только при st.interrupted) -- дешёвый no-op на успешном прогоне, где чистить
        # нечего (обычный обход уже подчищает всё сам по ходу, см. _handle_archive()).
        _cleanup_own_tmp_extract_entries(cfg, log=log)

        # Архивные события (extracted/no_media/password_protected/bomb_suspected/...) копятся
        # в walker.archive_logs по ходу walk() -- по завершении обхода переносим их в
        # archives.log (иначе файл существовал бы, но всегда оставался пустым).
        for display, status, note in walker.archive_logs:
            run_logs.archive_event(display, status, note)
        # E (2026-08-28): пропущенные служебные записи / частичные сбои распаковки tar --
        # отдельный список (см. SourceWalker.__init__()), в консоль уже ушли одной строкой
        # через _log_own_line(), здесь persist'им в archives.log тем же каналом.
        for display, tag, text in walker.archive_notes:
            run_logs.archive_event(display, tag, text)
        # ТЗ-меню 2026-07-10, раздел 5: "Загляну внутрь: N сжатых файлов" в человеческой
        # сводке пробного прогона -- чистая инструментация поверх уже собранного списка,
        # никакой новой бизнес-логики.
        stats["archives_seen"] = len(walker.archive_logs)
        # Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): пути запароленных
        # архивов -- та же идея, что archives_seen выше, только фильтр по статусу. note --
        # реальный абсолютный путь (см. _handle_archive()), не display (относительный
        # origin_prefix, из него file://-ссылку не построить).
        stats["encrypted_archives"] = [note for _, status, note in walker.archive_logs
                                        if status == "archive_password_protected"]
        # 2026-08-07: DVD-Video (VIDEO_TS) теперь копируется целиком (см. секцию "DVD-VIDEO
        # UNITS" выше) -- тот же принцип, что и archives_seen/encrypted_archives выше (уже
        # собранные walker'ом списки, никакой новой бизнес-логики здесь), но с записью в
        # реестр (dvd_units-таблица archive_cache.db), чтобы следующий прогон узнал уже
        # архивированный диск и не скопировал его повторно. НЕ при dry_run -- st.cache_conn
        # уже None в этом случае (см. его открытие выше), ничего физически не копировалось,
        # писать в реестр нечего.
        #
        # 2026-08-07 (Раунд 71 ревью, фикс блокера): walker.dvd_units_copied регистрирует
        # юнит НАМЕРЕНИЕМ на этапе обхода, ДО того как хоть один файл физически скопирован --
        # брать его напрямую для реестра/отчёта означало бы пометить юнит "архивирован" даже
        # если часть файлов не дошла до TARGET (антивирус/плохой сектор/Ctrl+C/нехватка
        # места), после чего недостающие файлы теряются безвозвратно (следующий прогон видит
        # fingerprint в реестре и считает юнит уже полностью заархивированным дублем). Юнит
        # считается реально завершённым, только если dvd_unit_ok_counts (см. основной цикл
        # выше, инкрементируется ТОЛЬКО когда _process_dvd_item() вернул "ok" для файла) сошёлся
        # с n_files юнита целиком -- частично скопированный юнит НЕ регистрируется в БД и не
        # попадает в отчёт "скопировано целиком"; при следующем прогоне walker снова увидит его
        # как новый (fingerprint отсутствует в реестре) и повторит попытку с нуля.
        dvd_units_confirmed = [
            u for u in walker.dvd_units_copied
            if dvd_unit_ok_counts.get(u["fingerprint"], 0) >= u["n_files"]
        ]
        stats["dvd_units_copied"] = dvd_units_confirmed
        stats["dvd_units_skipped_duplicate"] = list(walker.dvd_units_skipped_duplicate)
        if st.cache_conn is not None and dvd_units_confirmed:
            now_iso = datetime.now().isoformat()
            st.cache_conn.executemany(
                "INSERT OR REPLACE INTO dvd_units(fingerprint,dest_path,n_files,total_bytes,created_at) "
                "VALUES (?,?,?,?,?)",
                [(u["fingerprint"], u["dest_path"], u["n_files"], u["total_bytes"], now_iso)
                 for u in dvd_units_confirmed],
            )
        # REVIEW-HANDOFF.md, Раунд 72 [ЗАМЕЧАНИЕ]: побочный эффект фикса Раунда 71 выше --
        # частично скопированный юнит НЕ регистрируется в БД (верно), но его недокопированная
        # папка оставалась на диске НАВСЕГДА, ничем не помеченная -- следующий прогон видит
        # тот же диск как "новый" (fingerprint не в реестре), но _unique_dvd_dest_name()
        # трактует уже существующую папку как "коллизия с ДРУГИМ диском" (её единственная
        # цель -- не путать два РАЗНЫХ диска с одинаковым именем, см. её докстринг) и заводит
        # рядом "VIDEO_TS (2)", копируя ВСЁ заново с нуля -- архив накапливает необъяснённый
        # мусор (обрезанный, нерабочий DVD-рип неотличим на вид от второго настоящего диска).
        # Удаляем недокопированную папку здесь же, best-effort, СИММЕТРИЧНО тому, как обычный
        # файл, не попавший в pool/archive_cache, "самоисцеляется" сам по себе на следующем
        # прогоне (см. диагноз Раунда 71) -- целостность инварианта "юнит копируется целиком
        # либо не копируется вовсе" восстанавливается для содержимого TARGET, не только для
        # реестра БД.
        #
        # REVIEW-HANDOFF.md, Раунд 73 [ЗАМЕЧАНИЕ]: "неполный" (ok_counts < n_files) НЕ значит
        # "сбойный" -- --sample-limit может оборвать основной цикл выше (`break`) ровно
        # серединой юнита, БЕЗ единой ошибки; файлы, успевшие дойти до place_file() ДО обрыва,
        # реально и безошибочно лежат на диске -- тот же принцип "оставить как есть", что и у
        # обычных (не-DVD) файлов под тем же --sample-limit, требует НЕ удалять их. Чистим
        # только когда неполнота вызвана НАСТОЯЩЕЙ причиной: явный "failed"/"stop" хотя бы для
        # одного файла юнита (dvd_unit_failed_fingerprints) ИЛИ весь прогон прерван
        # KeyboardInterrupt (st.interrupted -- цикл в этом случае не может дойти до конца
        # SOURCE/sample_limit естественным путём, единственная причина неполноты юнита в этом
        # случае -- само прерывание).
        for u in walker.dvd_units_copied:
            if dvd_unit_ok_counts.get(u["fingerprint"], 0) >= u["n_files"]:
                continue  # завершён целиком -- нечего чистить
            if u["fingerprint"] not in dvd_unit_failed_fingerprints and not st.interrupted:
                continue  # неполный не из-за сбоя (--sample-limit/конец SOURCE) -- не трогаем
            # Раунд 73, придирка: cleanup_dir() -- no-op, если папка не создавалась вовсе
            # (например, самый первый файл юнита провалился ДО того, как place_file() успел
            # создать структуру папок) -- лог должен отражать это, а не безусловно утверждать
            # "удалена", когда физически удалять было нечего.
            if os.path.isdir(winlong(u["dest_path"])):
                cleanup_dir(u["dest_path"])
                run_logs.action(f"[DVD] недокопированная папка удалена: {u['dest_path']}")

        # 2026-07-11 (сессия про управляемый список служебных папок): раньше пропуски по
        # HARD_EXCLUDE_DIRS/default_exclude_dirs/extra_exclude_dirs и по гейту системных
        # папок не попадали в actions.log вообще (только мимолётный print), а
        # skipped_marker_logs/sidecar_logs копились, но ни разу не читались -- даже
        # промаркированные пропуски терялись безвозвратно. Переносим все четыре источника в
        # actions.log тем же способом, что и archive_logs выше.
        for name, reason, count in walker.excluded_dir_summary():
            run_logs.action(f"[EXCLUDE] {name}: пропущено {count} раз ({reason})")
        for path in walker.system_dir_skips:
            run_logs.action(f"[SYSTEM_DIR] {path}: пропущено (scan_system_dirs=false)")
        for disp in walker.skipped_marker_logs:
            run_logs.action(f"[skip_marker] {disp}")
        for disp in walker.sidecar_logs:
            run_logs.action(f"[sidecar] {disp}")
        for disp, err in walker.stat_failed_logs:
            run_logs.unreadable(disp, err)
            unreadable_count += 1
            # os.stat() провалился раньше, чем SourceItem с ftype вообще собран -- только путь
            # (disp), тип определяем по расширению тем же способом, что и сам SourceItem.
            unreadable_count_by_type[_ftype_bucket(file_type(disp))] += 1

        if pending_retry and not st.stopped_for_space and not st.interrupted:
            try:
                log(f"Повторное чтение {len(pending_retry)} отложенных файлов в конце прогона...")
                for item in pending_retry:
                    records = analyze_batch([item], retries=1, retry_delay=cfg.read_retry_delay,
                                             small_image_px=cfg.small_image_px, log=log, pool=pool)
                    rec = records[0]
                    if rec.read_error:
                        run_logs.unreadable(item.origin_display, rec.read_error_msg)
                        unreadable_count += 1
                        unreadable_count_by_type[_ftype_bucket(item.ftype)] += 1
                        continue
                    if _process_record(rec, st, log=log):
                        st.stopped_for_space = True
                        bar.update(1, note="повтор чтения (диск может быть медленным)")
                        break
                    bar.update(1, note="повтор чтения (диск может быть медленным)")
            except KeyboardInterrupt:
                st.interrupted = True  # см. комментарий у основного цикла выше
                bar.mark_interrupted()  # "обработано объектов XX%" не форсирует 100% на прерванном прогоне

    if st.cache_conn is not None:
        st.cache_conn.commit()
        st.cache_conn.close()

    phase2_end = time.monotonic()

    summary_lines = []
    summary_lines.append(f"\n===== Прогон {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    # p.5.3а: версия/инструменты/тайминги -- ВСЕГДА, без флага debug (для сравнения между
    # тестерами с разными бинарниками в bin/ и разбора багов между релизами). Сырой английский
    # дамп stats ниже по функции -- уже ТОЛЬКО под debug (см. 2026-07-11).
    summary_lines.append(f"PhotoArchive {__version__} (rules {RULES_VERSION}) {__copyright__}, "
                         f"{__license__}\n")
    tool_versions = detect_tool_versions()
    summary_lines.append("Инструменты: " + ", ".join(f"{k}={v}" for k, v in tool_versions.items()) + "\n")
    summary_lines.append(
        f"Тайминги: Фаза 0={phase0_end - run_start:.1f}с, Фаза 1={phase1_end - phase0_end:.1f}с, "
        f"Фаза 2-5={phase2_end - phase1_end:.1f}с, всего={phase2_end - run_start:.1f}с\n"
    )
    summary_lines.append(f"SOURCE: {cfg.source}\n")
    summary_lines.append(f"TARGET: {cfg.target}\n")
    summary_lines.append(f"Обработано элементов источника: {processed_count}\n")
    for k, v in stats.items():
        if k == "bytes_appended":
            summary_lines.append(f"  объём дописанного: {v / 1024**3:.2f} ГБ\n")
        elif cfg.debug:
            # 2026-07-11, по замечанию пользователя: сырой дамп английских ключей
            # внутреннего словаря статистики (appended_new, skipped_present, warn_* и т.п.)
            # был безусловным (без debug) с самого начала (p.5.3а) -- задумывался для
            # сравнения между тестерами/разбора багов, но обычному пользователю смешивал
            # английские имена с русским текстом вокруг, при том что те же цифры уже есть в
            # человекочитаемом "Итог прогона" ниже (build_final_summary). Теперь только под
            # debug=true -- как и остальные [DEBUG]-подробности в actions.log.
            summary_lines.append(f"  {k}: {v}\n")
    if unreadable_count:
        summary_lines.append(f"{unreadable_count} файлов не прочитано — см. unreadable.csv\n")
    # Тем же принципом, что build_final_summary() ниже (А.4: чистая агрегация уже посчитанных
    # чисел, без новой бизнес-логики) -- стаскиваем в stats то, что иначе доступно только как
    # параметры ЭТОГО вызова (unreadable_count/walker) и терялось бы при возврате из функции.
    # PROMPT_archive_report.md, раздел "Этот прогон" (2026-07-20): report.html получает
    # просуммированный по всем SOURCE stats (через RunResult.stats/_sum_stats()) -- без этого
    # сохранения "нечитаемых"/"распакованных архивов" нечем было бы показать в отчёте отдельно
    # от кумулятивной истории архива.
    stats["unreadable_count"] = unreadable_count
    for bucket, n in unreadable_count_by_type.items():
        stats[f"unreadable_count_{bucket}"] = n
    stats["archives_extracted"] = sum(1 for _, status, _ in walker.archive_logs if status == "archive_extracted")
    # REVIEW-HANDOFF.md, Раунд 32, задача 4: "всего найдено на источнике" -- база для сверки,
    # что отчёт ничего не потерял молча (сумма категорий ниже должна совпадать с этим числом).
    # listdir_failed_count -- отдельный прямой сигнал (не просто база для ручного сложения):
    # если > 0, хотя бы одна папка не была прочитана вообще (права доступа/длинный путь/
    # повреждённая ФС) -- report.py показывает явное предупреждение, не только цифру.
    stats["processed_count"] = processed_count
    stats["listdir_failed_count"] = len(walker.listdir_failed)
    try:
        # TARGET can vanish mid-run (disk unplugged) -- confirmed via real-hardware test
        # 2026-07-18: this used to crash the whole run right at the finish line, after every
        # file had already been processed (successfully or as write_failed) -- same class of
        # bug as report_environment()'s guard above, just at the other end of the run.
        free = shutil.disk_usage(winlong(cfg.target)).free
        summary_lines.append(f"Свободно на TARGET по завершении: {free / 1024**3:.2f} ГБ\n")
    except OSError:
        summary_lines.append("Свободно на TARGET по завершении: не удалось определить (диск недоступен)\n")
    if st.stopped_for_space:
        summary_lines.append("ОСТАНОВЛЕНО: недостаточно места на TARGET. Освободите место и запустите снова.\n")
    if st.interrupted:
        summary_lines.append("ПРЕРВАНО: работа остановлена пользователем (Ctrl+C).\n")
    summary_lines.append(build_final_summary(stats, walker, unreadable_count, pool, processed_count))
    summary_text = "".join(summary_lines)
    run_logs.write_summary(summary_text)
    run_logs.close()

    # Пакет п.4 (SESSION-HANDOFF.txt): print_summary=False -- ТОЛЬКО _bare_launch_run_build()
    # ([3] голого меню) передаёт его -- эта техническая сводка (тайминги/версии инструментов/
    # build_final_summary()) дублирует то, что и так уже показывает report.html/короткие
    # текстовые подтверждения того же шага. Обычный CLI archive (print_summary=True по
    # умолчанию) не затронут -- контракт для headless-автоматизации не меняется (RULES.md).
    # write_summary()/close() выше -- отдельные вызовы, самим параметром не затронуты.
    if print_summary:
        log(summary_text)
    # PROMPT_archive_report.md, 1.2а: только CollectingRunLogs (suppress_logs=True, см. выше)
    # имеет .rows -- RunLogs/NullRunLogs не имеют этого атрибута, getattr(..., None) вместо
    # isinstance() держит эту функцию не завязанной на конкретный класс.
    collected_rows = getattr(run_logs, "rows", None)
    # Живой репорт пользователя (2026-08-01): секция "Пополнение архива" в report.html не
    # показывала, сколько времени заняла сборка -- run_start уже существовал (используется
    # в логах Фазы 0/фаза-таймингах выше), просто раньше не доживал до stats. _sum_stats()
    # (см. её докстринг) суммирует любое числовое поле автоматически -- многоисточниковый
    # прогон (--source all/несколько --source) получит суммарное время по всем SOURCE без
    # отдельной плюмбинги.
    stats["duration_seconds"] = time.monotonic() - run_start
    return stats, processed_count, st.stopped_for_space, collected_rows, pool, st.interrupted

# ============================================================================
# STARTUP: конфиг, проверка бандленных бинарников, интерактивный ввод, CLI
# ============================================================================

CONFIG_YAML_PATH = os.path.join(WORKDIR, "photoarchive_config.yaml")

# 2026-07-11: содержимое ДОЛЖНО совпадать с photoarchive_config.yaml.example в корне репозитория --
# ДЕРЖАТЬ ДВА ТЕКСТА В СИНХРОНЕ ВРУЧНУЮ при правке одного из них. photoarchive_config.yaml.example
# остаётся в репозитории как есть (для тех, кто смотрит исходники/собирает сам) -- эта
# константа существует ОТДЕЛЬНО, потому что собранный .exe не имеет доступа к файлам
# репозитория и не может просто скопировать photoarchive_config.yaml.example на диск, см.
# load_yaml_config() ниже.
DEFAULT_CONFIG_YAML_TEMPLATE = """\
# PhotoArchive: необязательный файл расширенных настроек.
# Скопируйте в photoarchive_config.yaml (в ту же папку, где лежит PhotoArchive.exe / photosort_win.py) и
# раскомментируйте/поправьте нужное -- если файла нет, используются значения по умолчанию
# (те же, что показаны здесь).
#
# source/target/dry-run/sample-limit сюда НЕ входят -- они всегда задаются через CLI-флаги
# (--source/--target/--dry-run/--sample-limit) или интерактивный ввод при запуске.
# Незнакомые ключи в этом файле игнорируются с предупреждением в лог.
#
# Этот файл был автоматически создан программой при первом запуске (photoarchive_config.yaml не
# существовал) -- полностью закомментирован, ни на что не влияет, пока вы не раскомментируете
# нужные строки.

# place_lookup: offline         # город из geotag через reverse_geocoder; off = без места
# home_country: RU              # в своей стране пишем только город, за рубежом "Город, Страна"
# archive_hash_cache: true      # true (по умолчанию) = кэш хешей архива по (path,size,mtime) для
                                # ускорения повторных прогонов на растущем архиве; false = всегда
                                # пересчитывать всё заново (медленнее, но нечувствительно к
                                # теоретической коллизии path+size+mtime при разном содержимом)
# check_signature: false        # false (по умолчанию) = не проверять сигнатуру файла (первые
                                # байты содержимого) против расширения в обычном анализе
                                # ("Сканирование источника" / CLI analyze) -- ускоряет анализ на
                                # медленном/сетевом диске; true = проверять. "Паспорт архива"
                                # проверяет сигнатуру ВСЕГДА, независимо от этого флага -- это
                                # полная проверка уже собранного архива, там сокращать не нужно
# max_archive_depth: 8          # потолок вложенности архив-в-архиве
# max_dest_path: 240            # символов на сегмент пути (плюс жёсткий лимит 255 байт UTF-8)
# small_image_px: 640           # граница "маленького, но не иконки" фото
# free_space_margin_gb: 10.0
# read_retry_count: 3           # попыток прочитать файл источника перед отложенным повтором
# read_retry_delay: 5.0         # секунд между попытками

# bydate_granularity: month     # day | month | year | flat -- гранулярность папок ByDate
#   day:                  ByDate/2019/2019-07-15 Москва/
#   month (по умолчанию): ByDate/2019/2019-07 Москва/
#   year:                 ByDate/2019/
#   flat:                 ByDate/   (все дампы одной кучей, без подпапок по дате)
#   Смена этой настройки НЕ переименовывает уже собранные папки (архив append-only) --
#   касается только новых файлов, дописываемых после смены.

# scan_system_dirs: false       # заходить ли при рекурсии в системные папки (WINDIR,
                                # ProgramFiles, ProgramFiles(x86), ProgramData, LOCALAPPDATA,
                                # APPDATA, TEMP -- определяются через переменные окружения).
                                # false (по умолчанию) экономит время на заведомо не-фото
                                # системных деревьях при SOURCE=C:\\ целиком. Явно указанный
                                # SOURCE вглубь такой папки обрабатывается всегда, флаг не
                                # мешает (см. README.md, раздел "Зоны доверия и системные папки").
# default_exclude_dirs: [node_modules, .git, "$recycle.bin"]
                                # редактируемый список папок, пропускаемых по умолчанию при
                                # рекурсии -- это ЭВРИСТИКА ("скорее всего не фото"), а не
                                # защита ОС, поэтому можно менять свободно. Например, уберите
                                # "$recycle.bin", если хотите, чтобы программа заглянула в
                                # Корзину и попыталась спасти удалённые фото (каждый диск
                                # хранит там подпапку, принадлежащую именно вашей учётной
                                # записи -- прав администратора для чтения не нужно).
                                # НЕ входят и не настраиваются через этот список: папки,
                                # которые программа пропускает БЕЗУСЛОВНО, потому что реально
                                # недостижимы ни для кого (System Volume Information,
                                # Default/Default User, Мои видеозаписи/Моя музыка/мои
                                # рисунки и их англ. варианты, __служебные_файлы) -- см.
                                # README.md, раздел "Зоны доверия и системные папки".
# extra_exclude_dirs: []        # доп. имена папок, которые пропускать при рекурсии, ПОВЕРХ
                                # default_exclude_dirs выше -- например [MyBackupTool, OldSync]

# dump_segment_names: [dcim, camera, "camera uploads", фотокамера, photostream, моменты,
#   screenshots, скриншоты, downloads, загрузки, "saved pictures", pictures, изображения,
#   фотопленка, users, home, desktop, "рабочий стол", "camera roll"]
                                # редактируемый список ИМЁН папок (без учёта регистра), которые
                                # считаются "не альбом, скорее всего служебная/авто-папка" при
                                # выборе, как назвать альбом -- см. README.md, раздел
                                # "Раскладка: альбом или по дате". НЕ входят и не настраиваются через этот
                                # список: bydate/albums/raw/_unsorted -- эти четыре защищают
                                # уже собранный архив от самопоедания при повторном прогоне
                                # (SOURCE = уже готовый архив) и всегда учитываются программой
                                # независимо от этого списка.
# extra_dump_segment_names: []  # доп. имена ПОВЕРХ dump_segment_names -- например [YandexDisk]
                                # Отдельно: запись вида "D:" (буква диска + двоеточие, БЕЗ
                                # обратного слэша) значит не имя папки, а "весь этот диск
                                # целиком -- по датам, без поиска альбомов вообще", если SOURCE
                                # указывает на этот диск -- например [D:] раскладывает ЛЮБОЙ
                                # SOURCE на диске D: по датам без переименования и без
                                # перечисления папок верхнего уровня. Реальная папка не может
                                # называться буквально "D:" (двоеточие запрещено Windows в
                                # имени файла/папки), коллизия с папкой "D" исключена.
# dump_segment_prefixes: [whatsapp, telegram]
                                # редактируемый список ПРЕФИКСОВ имени папки (без учёта
                                # регистра) -- совпадает по смыслу с dump_segment_names, но
                                # проверяет НАЧАЛО имени, а не имя целиком (например
                                # "WhatsApp Images" тоже совпадёт с "whatsapp").
# extra_dump_segment_prefixes: [] # доп. префиксы ПОВЕРХ dump_segment_prefixes

# mirror_raw: true              # false = избыточный RAW (есть парный JPEG уже в основном
                                # архиве) не копировать в RAW-зеркало (лог
                                # raw_skipped_has_jpeg); одинокий RAW (без JPEG) мирроится
                                # ВСЕГДА независимо от этого флага -- единственный носитель
                                # кадра никогда не пропускается молча.

# raw_layout: mirror            # mirror (по умолчанию) | sibling -- где физически лежит
                                # RAW-зеркало (flat сознательно не сделан -- см. README.md).
                                #   mirror  -- "Семейный архив (RAW отдельно)": отдельный
                                #     корень RAW\\, зеркалящий структуру основного архива
                                #     (RAW\\Albums\\..., RAW\\ByDate\\YYYY\\...).
                                #   sibling -- "Режим фотографа (RAW рядом)": подпапка RAW\\
                                #     рядом с самим кадром -- Albums\\Море 2015\\RAW\\IMG.CR2.
                                # Одинокий RAW спасается всегда независимо от этого значения.

# tmp_extract_dir: null          # null (по умолчанию) = {TARGET}\\__служебные_файлы\\tmp_extract\\
                                # -- подпапка самого TARGET, а не системный %TEMP%, поэтому
                                # уже гарантированно на том же физическом ТОМЕ, что и весь
                                # архив. На этом же томе финализация файлов из архива --
                                # мгновенный rename, а не копирование. Можно переопределить
                                # своим путём (например, если TARGET на медленном/сетевом
                                # диске, а быстрый SSD -- на другой букве); при другом томе
                                # финализация автоматически деградирует до обычного
                                # копирования с hash-verify.

# debug: false                   # true = подробные [DEBUG]-строки в __служебные_файлы\\logs\\
                                # actions.log -- причина каждого решения (альбом/dump,
                                # near-dup, tier/evidence даты) и полный traceback на
                                # ошибках вместо короткого сообщения. Для тестеров/разбора
                                # багов между релизами -- summary.txt при этом обогащён
                                # ВСЕГДА, независимо от этого флага (версии, тайминги,
                                # предупреждения). ВАЖНО: actions.log не ротируется по типу
                                # строки -- разово включённый debug оставит подробные строки
                                # вперемешку с обычными во всех будущих прогонах того же
                                # архива (см. README.md).

# ============================================================
# СПРАВОЧНО, НЕ РЕДАКТИРУЕТСЯ -- ниже НЕ настройки этого файла, а список того, что реально
# влияет на раскладку "альбом или по датам", но зашито в коде и не выведено в config.yaml.
# Точные значения -- в исходном коде photosort_win.py (открыт, см. README.md, раздел
# "Лицензия"), полное описание -- в README.md.
# ============================================================
#
# DUMP_SEGMENT_NAMES_PROTECTED = bydate, albums, raw, _unsorted
#   Эти четыре имени никогда не могут стать названием альбома -- защита уже собранного
#   архива от самопоедания, если SOURCE указывает на уже готовый TARGET ("каскадный" повторный
#   прогон). Добавляются к dump_segment_names безусловно, убрать их через этот файл нельзя.
#
# DUMP_SEGMENT_REGEXES (по regex, без учёта регистра):
#   - ^\\d{3}[A-Za-z]+$          имя вида "100CANON"/"101MSDCF" (стандартный DCIM-формат камер)
#   - ^новая папка(\\s\\(\\d+\\))?$ / ^new folder(\\s\\(\\d+\\))?$
#                               неизменённое имя новой папки Windows, включая нумерованные
#                               дубли ("Новая папка (2)")
#
# DUMP_TAG = " [PhotoArchive]"
#   Суффикс, добавляемый к каждой ByDate-папке (день/месяц/"дата неизвестна") -- отличает наши
#   автосозданные папки от одноимённых, которые пользователь мог создать вручную.
#
# FORCE_DUMP_PREFIX = "~"
#   Переименуйте папку-источник вручную в "~ИмяПапки" -- программа всегда посчитает её dump
#   (сортировка по дате), даже если имя иначе выглядело бы как настоящий альбом. Полезно для
#   папок облачной синхронизации ("Яндекс.Диск") или решения постфактум "эта папка избыточна".
#   Программа никогда не переименовывает исходники сама -- только читает.
#
# Альбом или по дате (2026-08-08, RULES_VERSION, "чем проще, тем лучше для пользователя"):
#   find_album() идёт по КАЖДОМУ сегменту пути от корня SOURCE, включая собственное имя
#   архива (без расширения), если файл внутри .zip/.rar/.7z/.tar -- на равных с папками, той
#   же функцией is_dump_segment(). Если ЛЮБОЙ сегмент служебный -- путь отравлен целиком, файл
#   падает в ByDate, БЕЗ исключений по позиции (ни день-папка внутри альбома, ни архив как
#   "второй шанс" после диска, ни юзернейм профиля -- позиционных исключений больше нет вовсе).
#   Если ни один сегмент не служебный -- путь целиком зеркалится в Albums\\, и КАЖДАЯ папка на
#   нём -- свой собственный, отдельный альбом ("Мои фото\\Свадьба" и "Мои фото\\Отпуск" -- ДВА
#   разных альбома, не один общий "Мои фото" с двумя подпапками; то же правило одинаково
#   разбивает и подпапки одного события, "Свадьба 2015\\Церемония"/"Свадьба 2015\\Банкет").
#   Изменение НЕ ретроактивно -- уже собранные архивы старым правилом нужно пересобирать
#   заново, если нужна новая раскладка.
"""

# поля Config, которые можно переопределить через photoarchive_config.yaml -- сознательно НЕ включает
# source/target/dry_run/sample_limit: они всегда приходят из CLI/интерактивного ввода
CONFIG_YAML_FIELDS = {
    "place_lookup", "home_country", "archive_hash_cache",
    "check_signature",
    "max_archive_depth", "max_dest_path", "small_image_px", "free_space_margin_gb",
    "read_retry_count", "read_retry_delay", "bydate_granularity",
    "scan_system_dirs",
    "default_exclude_dirs", "extra_exclude_dirs", "mirror_raw",
    "tmp_extract_dir", "raw_layout", "debug",
    "dump_segment_names", "extra_dump_segment_names",
    "dump_segment_prefixes", "extra_dump_segment_prefixes",
}


def _ensure_config_yaml_exists(path: str, log=print) -> None:
    """2026-07-11: если файла нет, best-effort создаём его из DEFAULT_CONFIG_YAML_TEMPLATE --
    полностью закомментирован, ни на что не влияет сам по себе, но избавляет пользователя от
    необходимости искать/копировать photoarchive_config.yaml.example (у собранного .exe нет доступа к
    файлам репозитория). Если запись не удалась (папка только для чтения, права доступа и
    т.п.) -- не фатально, просто предупреждение в лог, работаем на дефолтах как раньше.

    Вынесено ОТДЕЛЬНО от load_yaml_config() (2026-07-11, по живой находке пользователя):
    голый запуск .exe, прерванный Ctrl-C на самом первом вопросе меню (`run_bare_launch()`)
    или на первом интерактивном "Откуда"/"Куда" (частичный CLI), никогда не доходит до
    Config()/run_for_source() -- туда, где load_yaml_config() раньше вызывался впервые --
    поэтому photoarchive_config.yaml не успевал появиться, хотя пользователю он нужен сразу (админы читают
    конфиг, не документацию). Теперь вызывается САМОЙ ПЕРВОЙ строкой и в run_bare_launch(), и
    в интерактивной ветке _main(), до единственного input()."""
    if os.path.exists(path):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG_YAML_TEMPLATE)
        log(f"photoarchive_config.yaml не найден -- создан по умолчанию ({path}), "
            f"полностью закомментирован (ни на что не влияет, пока не отредактируете)")
    except OSError as e:
        log(f"ВНИМАНИЕ: не удалось создать photoarchive_config.yaml по умолчанию ({path}): {e} -- "
            f"работаем на встроенных значениях по умолчанию")


def load_yaml_config(path: str, log=print) -> dict:
    """Необязательный файл с расширенными настройками (см. photoarchive_config.yaml.example) --
    единственный способ поменять их без правки photosort.py. CLI/интерактивный ввод
    (source/target/dry-run/sample-limit) всегда важнее и сюда не относится.

    Если файла нет -- сначала пытается его создать (см. _ensure_config_yaml_exists()), затем
    как обычно возвращает {} (только что записанный файл целиком закомментирован, override'ов
    нет)."""
    _ensure_config_yaml_exists(path, log=log)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        log(f"ВНИМАНИЕ: {path} должен быть словарём ключ: значение, содержимое проигнорировано")
        return {}
    unknown = set(data) - CONFIG_YAML_FIELDS
    if unknown:
        log(f"ВНИМАНИЕ: ключи {', '.join(sorted(unknown))} в {path} не настраиваются через "
            f"YAML (проигнорированы) -- см. photoarchive_config.yaml.example")
    return {k: v for k, v in data.items() if k in CONFIG_YAML_FIELDS}


def check_bundled_tools(log=print):
    """Проверка, что бандленные внешние бинарники реально доступны -- защита от битой
    сборки (недостающий exiftool.exe/ffmpeg.exe и т.п. в bin/), а не проверка системных
    пакетов, как в Linux-оригинале (там PyInstaller-сборки не было, зависимости ставились
    через apt/pip на лету). Здесь всё уже должно быть внутри .exe/рядом с ним."""
    missing = []
    for label, path in (
        ("exiftool", EXIFTOOL_BIN), ("ffprobe", FFPROBE_BIN), ("ffmpeg", FFMPEG_BIN),
        ("7z", SEVENZIP_BIN), ("unrar", UNRAR_BIN),
    ):
        if os.path.isabs(path) and not os.path.exists(path):
            missing.append(f"{label} (ожидался в {path})")
    if missing:
        log("ВНИМАНИЕ: не найдены бандленные внешние инструменты:\n  " + "\n  ".join(missing) +
            "\nСборка повреждена или собрана без --add-binary для этих файлов. "
            "Функции, зависящие от них (EXIF-даты, видео, 7z/rar-архивы), будут падать в лог ошибок.")


def _detect_tool_version(binary: str, args: list) -> str:
    """p.5.3а: real installed version of a bundled external tool, for summary.txt -- у
    разных бета-тестеров разные конкретные бинарники в bin/ (см. bin/README-BIN.md), так
    что версия из кода (EXIFTOOL_BIN и т.п. -- это только ПУТЬ) не то же самое, что версия
    самого бинарника. Best-effort: first non-empty line of stdout/stderr, or "?" if the
    binary is missing/times out (mirrors check_bundled_tools() not treating this as fatal)."""
    try:
        out = subprocess.run([binary] + args, capture_output=True, timeout=10)
    except Exception:
        return "?"
    text = (out.stdout or b"").decode("utf-8", "replace") + (out.stderr or b"").decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "?"


def detect_tool_versions() -> dict:
    return {
        "exiftool": _detect_tool_version(EXIFTOOL_BIN, ["-ver"]),
        "7z": _detect_tool_version(SEVENZIP_BIN, []),
        "ffmpeg": _detect_tool_version(FFMPEG_BIN, ["-version"]),
        "ffprobe": _detect_tool_version(FFPROBE_BIN, ["-version"]),
        "unrar": _detect_tool_version(UNRAR_BIN, []),
    }


def _strip_surrounding_quotes(path: str) -> str:
    """Windows при перетаскивании папки мышкой в консоль вставляет путь в двойных кавычках
    (`"C:\\Users\\Мама\\Фото"`) -- одного .strip() недостаточно, кавычки остаются частью пути
    и валят любую проверку существования папки ниже по конвейеру."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1].strip()
    return path


def interactive_input(input_fn=input):
    # p.5.4: голый английский жаргон "SOURCE"/"TARGET" смущает нетехнического пользователя в
    # интерактивном вводе -- переведено на русский без этих слов (согласовано с пользователем,
    # см. SESSION-HANDOFF.txt). CLI-флаги --source/--target и photoarchive_config.yaml НЕ переименованы --
    # это контракт для технических пользователей/скриптов, менять его отдельный вопрос.
    source = input_fn(
        "Откуда брать фото (папка, диск, файл-архив.zip, или all — все диски; "
        "можно перетащить папку сюда мышкой): "
    ).strip()
    target = input_fn(
        "Куда сложить архив (папка; можно перетащить папку сюда мышкой): "
    ).strip()
    return _strip_surrounding_quotes(source), _strip_surrounding_quotes(target)


TARGET_OWN_STRUCTURE_NAMES = {"__служебные_файлы", "_unsorted", "albums", "bydate", "raw"}


def _target_needs_confirmation(target: str) -> bool:
    """p.5.4а: риск-пропорциональная проверка ИМЕННО TARGET -- три случая:
    1. TARGET не существует, или существует, но пустая папка -> без подтверждения (ошибиться
       некритично, пустую папку потом просто удалить).
    2. TARGET существует и содержит ТОЛЬКО нашу собственную структуру (__служебные_файлы/, Albums/,
       ByDate/, RAW/, ничего больше на верхнем уровне) -> тоже без подтверждения -- штатное
       повторное пополнение уже существующего архива, самый частый сценарий (пугать здесь --
       приучить нажимать "да" не глядя именно там, где оно реально нужно).
    3. TARGET существует и содержит ЧТО-ТО ЕЩЁ (похоже на чужую папку, не на наш архив) ->
       нужно подтверждение.
    Возвращает True только для случая 3."""
    real_target = winlong(target)
    if not os.path.isdir(real_target):
        return False
    try:
        entries = os.listdir(real_target)
    except OSError:
        return False
    if not entries:
        return False
    return not all(e.lower() in TARGET_OWN_STRUCTURE_NAMES for e in entries)


def confirm_target_interactively(target: str, input_fn=input, log=print) -> bool:
    """True, если можно продолжать. Строгое подтверждение (ввод слова «да», не просто Enter,
    чтобы не проскакивало на автомате) -- только для случая 3 из _target_needs_confirmation().
    Вызывать ТОЛЬКО из интерактивного пути (main()) -- явные --source/--target из CLI никогда
    не показывают это подтверждение, это осознанное действие технического пользователя/
    скрипта, не должно ломать автоматизацию (согласовано с пользователем, p.5.4)."""
    if not _target_needs_confirmation(target):
        return True
    answer = input_fn(
        f"В папке {target} уже что-то есть — новые файлы будут добавлены туда же. "
        f"Продолжить? (введите «да»): "
    ).strip().lower()
    if answer != "да":
        log("Отменено пользователем.")
        return False
    return True


def _normalize_bare_drive_letter(path: str) -> str:
    """'C:' (голая буква диска БЕЗ обратного слеша) -- в терминах Windows это "drive-relative"
    путь, а НЕ полный путь: он неоднозначен сам по себе (зависит от текущей директории
    процесса именно на этом диске, os.path.isabs('C:') == False) -- удобный для пользователя
    короткий ввод ("источник C:, архив D:"), но без нормализации был бы отклонён проверкой
    полного пути (p.5.9) с малопонятной для нетехнического пользователя ошибкой. Нормализуем
    ТОЛЬКО эту конкретную двухсимвольную форму ('C:', любая буква) в однозначный корень
    ('C:\\') -- любая более длинная форма ('C:Фото' без слеша -- тоже реальная
    Windows-неоднозначность) НЕ трогаем, там угадывать нельзя, пусть проверка полного пути
    отклонит её как есть."""
    if len(path) == 2 and path[1] == ":" and path[0].isalpha():
        return path + "\\"
    return path


def _is_bare_drive_root(target: str) -> bool:
    """True, если TARGET указывает не на конкретную папку, а на корень тома целиком (голый
    'D:\\'/'D:', без единой вложенной папки) -- у корня os.path.dirname(path) совпадает с
    самим path (родителя нет), у любой настоящей вложенной папки -- нет. Инвариант работает
    одинаково на Windows ('D:\\') и POSIX ('/', актуально только для dev/теста на Linux, где
    буквы дисков как понятие не существуют)."""
    normalized = os.path.abspath(target)
    return os.path.dirname(normalized) == normalized


def confirm_drive_root_target_interactively(target: str, input_fn=input, log=print) -> str:
    """Если TARGET -- голый корень диска (см. _is_bare_drive_root), спрашивает пользователя,
    добавить ли к пути папку __PhotoArchive__ -- иначе весь __служебные_файлы\\/Albums\\/ByDate\\/RAW лёг
    бы прямо в корень тома, что не всегда красиво. Один и тот же бинарный вопрос ("добавить имя
    папки к диску или нет"), но формулировка зависит от того, существует ли __PhotoArchive__ уже:
    при повторном прогоне на тот же диск (дозапись в уже существующий архив) вопрос "создать
    папку?" был бы вводящим в заблуждение -- она уже есть, предлагаем её ИСПОЛЬЗОВАТЬ, а не
    создать заново. В отличие от confirm_target_interactively(), отказ НЕ отменяет прогон --
    пользователь может осознанно писать в корень (например выделенный под архив внешний диск,
    где лишняя вложенная папка не нужна) -- просто оставляет TARGET как есть. Возвращает
    (возможно изменённый) TARGET. Вызывать ТОЛЬКО из интерактивного пути (main()), только для
    archive (analyze-* ничего не пишет в TARGET) -- явные --target из CLI/photoarchive_config.yaml никогда
    не показывают этот вопрос и пишут в корень как есть (согласовано с пользователем
    2026-07-10, по аналогии с confirm_target_interactively -- явный ввод для скриптов/
    автоматизации не должен неожиданно перенаправляться).

    Имя папки -- __PhotoArchive__, не голое "PhotoArchive" (решение пользователя 2026-07-20,
    пятый заход): подавляющее большинство пользователей и без подсказки программы заводят
    папку "PhotoArchive" просто чтобы положить туда .exe -- на портативной установке (.exe и
    архив на одном диске) это раньше означало, что предлагаемое имя архива совпадало с папкой
    самой программы. Двойное подчёркивание продолжает уже принятый в проекте визуальный язык
    "это не пользовательская папка" (__служебные_файлы) -- узнаваемо, но не то, что пользователь
    наберёт интуитивно."""
    if not _is_bare_drive_root(target):
        return target
    photoarchive_dir = os.path.join(target, "__PhotoArchive__")
    if os.path.isdir(winlong(photoarchive_dir)):
        prompt = (
            f"В корне диска ({target}) уже есть папка __PhotoArchive__ — использовать её для "
            f"архива? Если нет — архив будет собран прямо в корне диска. (да/нет): "
        )
    else:
        prompt = (
            f"TARGET указан как корень диска ({target}) — создать в нём папку __PhotoArchive__ и "
            f"архивировать туда? Если нет — архив будет собран прямо в корне диска. (да/нет): "
        )
    answer = input_fn(prompt).strip().lower()
    if answer == "да":
        return photoarchive_dir
    return target


def resolve_drive_root_conflict(sources: list, target: str, interactive: bool,
                                 input_fn=input, log=print) -> str:
    """Единая точка разрешения TARGET, когда он указан как голый корень диска (см.
    _is_bare_drive_root). Два разных случая -- НЕ путать выбор с вынужденной необходимостью:

    1. TARGET (по realpath) совпадает с ОДНИМ ИЗ sources -- частый паттерн у нетехнического
       пользователя ("источник C:, архив тоже C:" -- ожидает получить папку с архивом на этом
       же диске из фотографий этого диска). Собрать архив ПРЯМО в этот же корень нельзя в
       принципе (самопоедание -- прогон читал бы собственную запись как источник;
       Config.__post_init__ всё равно отклонил бы это с ошибкой "SOURCE и TARGET совпадают").
       Единственное разумное разрешение здесь одно, а не выбор из вариантов -- поэтому НИЧЕГО
       не спрашиваем (даже в интерактиве, где обычно спрашивают) и просто подставляем
       {TARGET}\\__PhotoArchive__ с информационной строкой в лог. Работает ОДИНАКОВО для
       CLI/photoarchive_config.yaml и интерактивного ввода -- иначе `--source C:\\ --target C:\\` в
       скрипте/автоматизации просто упал бы с ошибкой конфигурации, хотя намерение
       однозначно читается из самого ввода. SOURCE=all считается тем же случаем, если TARGET
       -- голый корень диска: expand_sources() больше не исключает диск TARGET (см. его
       докстринг), так что "all" гарантированно развернётся в т.ч. и в сам этот корень.
    2. TARGET -- голый корень диска, но НЕ совпадает ни с одним source -- настоящий выбор
       (создать __PhotoArchive__\\ или писать прямо в корень), см.
       confirm_drive_root_target_interactively() -- но ТОЛЬКО в интерактиве, явные
       --target из CLI/photoarchive_config.yaml пишут в корень как есть без вопросов (как и раньше).

    Возвращает (возможно изменённый) TARGET."""
    if not _is_bare_drive_root(target):
        return target
    target_real = os.path.normcase(os.path.realpath(os.path.abspath(target)))
    conflicts = any(
        s.strip().lower() == "all"
        or os.path.normcase(os.path.realpath(os.path.abspath(s))) == target_real
        for s in sources
    )
    if conflicts:
        redirected = os.path.join(target, "__PhotoArchive__")
        log(f"TARGET указан так же, как и один из источников ({target}) -- архив будет "
            f"собран в {redirected}, а не в самом корне диска (иначе прогон читал бы "
            f"собственную же запись как источник).")
        return redirected
    if interactive:
        return confirm_drive_root_target_interactively(target, input_fn=input_fn, log=log)
    return target


def local_drive_roots():
    """Список локальных дисков Windows (C:\\, D:\\, ...) для SOURCE=all. На не-Windows
    (dev-запуск на Linux) возвращает пустой список -- там "all" не имеет смысла без
    /mnt/win-конвенции оригинала, которую portable-версия больше не использует."""
    if os.name != "nt":
        return []
    import string
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


# ROADMAP.md "Коды возврата PhotoArchive.exe не отражают неудачу": до этой правки TargetLocked/
# ошибка конфигурации/InsufficientSpace все давали process exit code 0, неотличимо от
# настоящего успеха для скрипта автоматизации, проверяющего %ERRORLEVEL%. 1 уже занят под
# неожиданный краш (_log_unexpected_crash), 130 -- под Ctrl+C/EOF (main()) -- три новых кода
# ниже намеренно не пересекаются ни с тем, ни с другим.
EXIT_TARGET_LOCKED = 2
EXIT_CONFIG_ERROR = 3
EXIT_INSUFFICIENT_SPACE = 4


@dataclass
class RunResult:
    """Возврат run_for_source() для одного SOURCE. failed=True -- для этого SOURCE вообще
    ничего не обработалось (TargetLocked/ошибка конфигурации); интерактивные вызывающие
    (run_bare_launch()) трактуют failed так же, как раньше трактовали голый None. exit_code --
    то, что CLI-путь (_main()) в конце аггрегирует по всем источникам в единственный process
    exit code; для успешного прогона, который тем не менее остановился раньше времени
    (stopped_for_space), failed остаётся False (что-то реально скопировалось), но exit_code
    всё равно ненулевой -- эти два поля отвечают на разные вопросы, не дублируют друг друга."""
    failed: bool
    exit_code: int = 0
    stats: dict = None
    processed_count: int = 0
    stopped_for_space: bool = False
    collected_rows: dict = None  # PROMPT_archive_report.md, 1.2а: только suppress_logs=True
                                  # (CollectingRunLogs.rows), иначе None -- см. _run_impl
    pool: "Pool" = None  # раунд 5 ревью, вариант A: для передачи вызывающему batch-циклу как
                         # shared_pool следующего SOURCE -- None при failed=True (см. run_for_source)
    interrupted: bool = False  # Ctrl+C-пакет: KeyboardInterrupt поймана внутри _run_impl(),
                                # failed остаётся False (та же логика, что и stopped_for_space
                                # -- что-то реально могло успеть обработаться до прерывания).
                                # Вызывающий код (_bare_launch_run_build()/_main()) обязан
                                # остановить цикл по остальным SOURCE и заново возбудить
                                # KeyboardInterrupt после генерации отчёта -- не проглатывать
                                # тихо (см. их же комментарии).


def run_for_source(source, target, dry_run, sample_limit, log=print, suppress_logs=False,
                    shared_pool=None, print_summary=True) -> RunResult:
    """print_summary (пакет п.4, SESSION-HANDOFF.txt; 2026-08-09 -- распространено на [2]
    _bare_launch_run_dryrun(), раньше пропущено там, живая находка пользователя): False у
    _bare_launch_run_build() ([3] голого меню) и _bare_launch_run_dryrun() ([2]) -- подавляет
    техническую консольную сводку внутри _run_impl() (дублирует report.html), не трогая
    write_summary()/CSV-логи. Обычный CLI archive не передаёт этот параметр (остаётся True по
    умолчанию) -- контракт для headless-автоматизации не меняется."""
    yaml_overrides = load_yaml_config(CONFIG_YAML_PATH, log=log)
    try:
        cfg = Config(source=source, target=target, dry_run=dry_run, sample_limit=sample_limit,
                     suppress_logs=suppress_logs, **yaml_overrides)
    except ValueError as e:
        log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        return RunResult(failed=True, exit_code=EXIT_CONFIG_ERROR)
    try:
        stats, processed_count, stopped_for_space, collected_rows, pool, interrupted = run(
            cfg, log=log, shared_pool=shared_pool, print_summary=print_summary)
    except TargetLocked as e:
        log(f"ОШИБКА: {e}")
        return RunResult(failed=True, exit_code=EXIT_TARGET_LOCKED)
    exit_code = EXIT_INSUFFICIENT_SPACE if stopped_for_space else 0
    return RunResult(failed=False, exit_code=exit_code, stats=stats, collected_rows=collected_rows,
                      processed_count=processed_count, stopped_for_space=stopped_for_space, pool=pool,
                      interrupted=interrupted)


def run_analyze_for_source(source, target, sample_limit, mode, log=print):
    """А.2: аналог run_for_source() для analyze-режимов -- собирает Config (dry_run/logs
    сюда не относятся, analyze-режимы никогда не пишут в TARGET), считает AnalyzeStats и
    сохраняет машинный analyze_report.csv в WORKDIR.

    SESSION-HANDOFF.txt п.2 (2026-08-05, боевой прогон): раньше здесь ещё печатался
    человекочитаемый чек-лист (print_analyze_report()) -- дублировал то, что и так показывает
    report.html, построенный сразу следом вызывающим кодом (_finalize_analyze_report(), см.
    _bare_launch_run_view()/_main()) -- та же функция уже безусловно печатает короткий
    указатель "Отчёт: <путь>" независимо от того, откроется браузер сам (интерактивное меню
    [1]) или нет (headless CLI `analyze --source`)."""
    yaml_overrides = load_yaml_config(CONFIG_YAML_PATH, log=log)
    try:
        cfg = Config(source=source, target=target, sample_limit=sample_limit, **yaml_overrides)
    except ValueError as e:
        log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        return None
    stats = run_analyze(cfg, mode, log=log)
    report_path = os.path.join(cfg.workdir, "analyze_report.csv")
    write_analyze_report_csv(report_path, stats)
    log(f"Машинный отчёт: {report_path}")
    return stats


def run_passport(target: str, log=print) -> AnalyzeStats:
    """[4] Паспорт архива -- полная проверка уже собранного архива с нуля (не из истории
    CSV-логов TARGET, см. _finalize_target_report()/report._render_cta_block()) -- переиспользует
    run_analyze() указанием cfg.source=TARGET. DUMP_SEGMENT_NAMES_PROTECTED (bydate/albums/raw/
    _unsorted) защищает от самопоедания при этом сценарии ("SOURCE указывает на уже готовый
    TARGET, каскадный повторный прогон", см. photoarchive_config.yaml.example) -- find_album()
    не путает Albums/ByDate/RAW/_Unsorted с настоящим именем альбома; __служебные_файлы уже в
    HARD_EXCLUDE_DIRS.

    self_scan=True (живой репорт пользователя, 2026-08-01): та же защита сама по себе НЕ
    делает найденные внутри ByDate/RAW/_Unsorted файлы "файлами внутри альбома/даты" -- у
    find_album() для них закономерно нет ответа (см. _PASSPORT_SELF_SCAN_RECOGNIZED_TOP), а
    их собственное имя папки ("2024-07-15 Москва") -- это разметка, которую программа сама
    же и сгенерировала на прошлом прогоне, не новое независимое доказательство даты. Раньше
    здесь ошибочно предполагалось, что это уже обработано -- проверено фактическим прогоном
    на реальном архиве, было не так (164 ложных "файл вне альбома", заниженное число
    "дата определена лишь приблизительно").

    mode="analyze" (не "analyze-full") -- дедуп ищется ВНУТРИ самого обхода (Pool строится по
    ходу walk(), полный проход хеширования), это и есть "целостность архива" паспорта: если
    что-то внутри уже собранного архива дедуплицируется само с собой, это находка, не сверка с
    отдельным внешним TARGET, которого у паспорта нет. cfg.target получает
    _NO_TARGET_PLACEHOLDER -- реально не читается для mode="analyze" (см. run_analyze()),
    Config.__post_init__ просто требует source != target.

    2026-08-24, живая просьба пользователя: паспорт раньше был готов "проверить" ЛЮБУЮ папку
    (Desktop, Downloads, что угодно) -- self_scan просто трактовал бы всё как "не внутри
    альбома/даты", без единого предупреждения, что это вообще не архив, а не осмысленный
    результат проверки. Единственная точка входа для всех трёх вызывающих (меню [4], CLI
    "analyze --target", GUI-мастер) -- сюда и добавлена жёсткая проверка, а не дублируется в
    каждом из трёх мест по отдельности (GUI и так уже блокирует "Далее" тем же самым
    _target_has_existing_archive() через _describe_passport_target() -- эта проверка здесь для
    двух остальных путей, у которых собственного клиентского гейта нет; для GUI это defense-
    in-depth, не единственная линия защиты). Та же сигнатура архива (__служебные_файлы, либо
    Albums+ByDate), что уже использует подменю выбора диска -- не переизобретаем."""
    if not _target_has_existing_archive(target):
        log(f"ОШИБКА: {target} не похож на архив PhotoArchive (нет папки __служебные_файлы, "
            f"ни характерных Albums/ByDate) -- Паспорт проверяет уже собранный архив, не любую "
            f"папку. Укажите путь к архиву.")
        return None
    yaml_overrides = load_yaml_config(CONFIG_YAML_PATH, log=log)
    try:
        cfg = Config(source=target, target=_NO_TARGET_PLACEHOLDER, sample_limit=0, **yaml_overrides)
    except ValueError as e:
        log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        return None
    return run_analyze(cfg, "analyze", log=log, self_scan=True)


def _open_report_in_browser(out_path: str) -> None:
    """webbrowser.open() не понимает \\\\?\\-префикс (winlong()) -- report.html/summary.txt
    пути на практике коротки (рядом с TARGET/.exe, не глубоко в ByDate/Albums), обычный
    os.path.abspath() ей достаточен.

    2026-08-23, живая находка пользователя: _reclaim_console_focus() ниже -- код 2026-07-21,
    из ЭПОХИ ТЕКСТОВОГО МЕНЮ ("голое меню тут же ждёт следующего выбора режима", её же
    докстринг) -- в сегодняшней GUI-модели это предположение ложно: следующий экран -- окно
    МАСТЕРА (gui_menu._Wizard), не консоль, а рабочая консоль вообще не должна получать фокус
    (она приборная панель, см. CLAUDE.md, "Рабочая консоль GUI-мастера..."). Хуже того --
    вызывалась она БЕЗУСЛОВНО, синхронно блокируя на ~1с (time.sleep(0.3)+time.sleep(0.7))
    ПРЯМО ПЕРЕД тем, как gui_menu.run_bare_launch() дойдёт до _hide_work_console() -- то есть
    насильно возвращала фокус консоли, которую через мгновение сворачивают, и ни разу не
    участвовала в том, чтобы окно НОВОГО мастера (создаётся уже ПОСЛЕ этой функции) вообще
    получило фокус -- сама и была источником гонки с браузером, которую пытался закрыть
    _Wizard.build_shell()'s _force_show_normal(). Вызываем её только для текстового меню
    (_console_freed_for_gui==False -- не-Windows dev-сессия, где консоль реально следующий
    экран) -- на GUI-пути её заменяет build_shell()'s собственная логика показа окна."""
    try:
        webbrowser.open(os.path.abspath(out_path))
    except Exception:
        pass
    if not _console_freed_for_gui:
        _reclaim_console_focus()


def _reclaim_console_focus() -> None:
    """2026-07-21, по прямой просьбе пользователя: webbrowser.open() выше переключает фокус
    Windows на окно браузера -- пользователь хочет, чтобы после формирования отчёта фокус
    оставался на консоли (голое меню тут же ждёт следующего выбора режима). Явно возвращаем
    фокус на консольное окно этого процесса (GetConsoleWindow()/SetForegroundWindow()) сразу
    после запуска браузера. Пара попыток с паузой, не одна -- браузер запускает своё окно
    асинхронно (для уже запущенного браузера обычно укладывается в первую попытку, холодный
    старт браузера может успеть перехватить фокус уже ПОСЛЕ первой попытки — вторая наверстывает
    без того, чтобы блокировать меню надолго). Best-effort и no-op вне Windows (dev/тест на
    Linux) -- отсутствие фокуса на консоли не мешает работе программы, только удобство."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        for delay in (0.3, 0.7):
            time.sleep(delay)
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _finalize_target_report(target: str, level: str, any_succeeded: bool, total_processed: int,
                             open_browser: bool, log=print, run_stats: dict = None,
                             run_start: str = None, interrupted: bool = False,
                             source_paths: list = None, data: dict = None) -> str:
    """PROMPT_archive_report.md, разделы 1.1/1.1а/1.2: report.html после archive-прогона
    (level="target", файл персистентно в TARGET\\__служебные_файлы\\) или CLI --dry-run
    (level="workdir", файл эфемерно в WORKDIR) -- различаются только пунктом назначения файла
    и текстом-обёрткой (level). Данные (`data`, см. ниже): level=="target" (реальная сборка,
    suppress_logs всегда False) читает настоящие CSV-логи TARGET с диска, level=="workdir"
    (CLI --dry-run, речь пользователя 2026-08-18, suppress_logs=args.dry_run в _main() --
    больше НЕ пишет в TARGET вообще) получает уже собранный вызывающим кодом `data` (in-memory
    строки этого прогона + существующая история TARGET, тот же приём слияния, что уже
    использует `_bare_launch_run_dryrun()`). Вызывается ОДИН раз на весь вызов (после цикла по
    expanded source), не на каждый --source -- один прогон = один файл (раздел 1.1).

    any_succeeded=False -- TargetLocked/ошибка конфига для ВСЕГО вызова (одинаковы для всех
    source в одном вызове, общий TARGET/конфиг) -- ничего не пишем, не удаляем (раздел 1.1а,
    "если лок вообще не был захвачен").

    run_stats -- сумма RunResult.stats по всем SOURCE этого вызова -- секция "Пополнение
    архива"/"Пробный прогон" в отчёте (report._render_this_run()). Передаётся ОБОИМ уровням
    (2026-07-20, третий заход) -- level=="workdir" (CLI --dry-run) теперь тоже показывает эту
    секцию (текст меняется на гипотетический -- см. _render_this_run()), просто без "Ваш
    архив"/диаграмм следом (report._generate_from_model() решает по level).

    run_start -- момент начала ЭТОГО вызова, до цикла по source -- отбирает для Листа 3
    только "новое в этом пополнении" (report._split_rows_by_time(), REVIEW-HANDOFF.md Раунд
    44: до 2026-07-31 функция ещё и строила отдельную "накопилось раньше"-половину, но её
    давно уже нигде не рендерит ни один уровень -- убрана вместе с вычислением). Передаётся
    обоим уровням: то же "новое"-отсеивание нужно и level=="workdir" (CLI --dry-run пишет
    реальные CSV TARGET без реального копирования файла -- без отсеивания по времени в чек-лист
    попали бы и фантомные записи прошлых --dry-run, см. report._generate_from_model()).

    2026-07-20, просьба пользователя: level=="target" больше НЕ открывает браузер здесь молча
    -- возвращает путь к файлу (или None, если открывать не нужно/нечего), вызывающий код
    передаёт его в _pause_before_exit(), которая открывает браузер ПОСЛЕ явного Enter, тем же
    действием что и обычный выход. level=="workdir" (--dry-run) оставлен как был (открывается
    сразу) -- Enter-гейтинг для WORKDIR-уровня по-прежнему не спроектирован (см. ROADMAP.md),
    не трогать сейчас заодно с этим -- изменилось только СОДЕРЖАНИЕ отчёта, не момент показа.

    interrupted (Ctrl+C-пакет): работа остановлена KeyboardInterrupt во время [3]/CLI archive
    (см. _RunState.interrupted). Снимает гейт any_succeeded=False ниже -- источник мог быть
    прерван до того, как хоть один RunResult успел вернуться "успешным" по обычной логике
    (result.failed==False), хотя частичные данные уже могли записаться в CSV TARGET (RunLogs
    пишет построчно по ходу работы). Без этого снятия гейта отчёт о прерывании молча не
    формировался бы ровно в том случае, который эта функция и должна покрыть."""
    if not any_succeeded and not interrupted:
        return None
    photosort_dir = os.path.join(target, "__служебные_файлы")  # см. Config.photosort_dir
    out_path = (os.path.join(photosort_dir, "report.html") if level == "target"
                else os.path.join(WORKDIR, "report.html"))
    if total_processed == 0:
        reason = ("Работа прервана пользователем до того, как что-либо успело обработаться."
                   if interrupted else
                   "Источник оказался недоступен или пуст — ни один файл не обработан.")
        report.generate_placeholder_report(reason, out_path, interrupted=interrupted,
                                            suggest_other_location=not interrupted,
                                            app_version=__version__)
    else:
        # data (речь пользователя, 2026-08-18): CLI --dry-run больше не пишет настоящие CSV в
        # TARGET (см. _main(), suppress_logs=args.dry_run) -- вызывающий код собирает те же
        # данные в памяти (CollectingRunLogs + слияние с существующей историей TARGET, тот же
        # приём, что уже использует _bare_launch_run_dryrun()) и передаёт их сюда готовыми.
        # data=None (level=="target", реальная сборка -- suppress_logs там всегда False) --
        # старое поведение, читаем настоящие CSV с диска.
        # Живой боевой прогон 2026-08-28: прогон к этому моменту уже закрыл прогресс-бар (он
        # застыл на 100%), а parse_target_logs() + HTML + report_detail.xlsx ещё считаются --
        # консоль стоит без единой строки, окно GUI "Работа окончена" физически ждёт возврата
        # отсюда, пользователь решает, что программа зависла. write_only-режим xlsx срезал это
        # с минут до секунд (см. report_detail_xlsx._write_flat_xlsx()), но и на секундах
        # застывшие 100% без ориентира читаются как зависание. Печатаем ЭТАПЫ с номером
        # X/Y (по предложению пользователя) -- честный "сколько ещё": Y = ровно то, что
        # видит эта функция (чтение логов + сборка отчёта; для CLI --dry-run логи уже
        # переданы готовыми, шаг один). Глубже (HTML vs xlsx отдельными шагами) -- отдельного
        # прогресса report.py наружу не отдаёт, дробить callback'ом ради ~5-секундной операции
        # несоразмерно. Масштаб ("N записей") -- чтобы пауза была объяснима.
        _total_steps = 2 if data is None else 1
        log("Формирую итоговый отчёт…")
        if data is None:
            log(f"  [1/{_total_steps}] читаю логи прогона…")
            data = report.parse_target_logs(os.path.join(photosort_dir, "logs"))
        _n_events = sum(len(data.get(k, ())) for k in ("appended", "skipped", "disputes", "unreadable"))
        _scale = f" ({_n_events} записей)" if _n_events > 3000 else ""
        log(f"  [{_total_steps}/{_total_steps}] собираю страницу и детализированную таблицу{_scale}…")
        report.generate_report(data, out_path, level=level, run_stats=run_stats,
                                run_start=run_start, target_path=target, interrupted=interrupted,
                                app_version=__version__, source_paths=source_paths)
    if not interrupted:
        log(f"Отчёт: {out_path}")
    if level == "workdir":
        if open_browser:
            _open_report_in_browser(out_path)
        return None
    return out_path if open_browser else None


def _finalize_analyze_report(stats, open_browser: bool, log=print, source_path: str = None) -> str:
    """analyze/analyze-quick/analyze-full (раздел 1.2): "один слот, не персистентно
    per-источник" -- вызывается ВНУТРИ цикла по source (не после), каждый анализ
    перезаписывает WORKDIR\\report.html независимо от исхода предыдущего (см. раздел 1.2,
    "отчёт по последней операции"), в отличие от _finalize_target_report выше.

    source_path (речь пользователя, 2026-08-09): SOURCE именно ЭТОГО вызова -- рендерится в
    заголовке отчёта (report._render_report_meta()), см. report.generate_report_from_analyze_
    stats()'s докстринг.

    Возвращает путь к отчёту (или None, если stats is None -- ошибка конфига, отчёт не
    формировался). 2026-07-21: голое меню (_bare_launch_run_view()) теперь вызывает это с
    open_browser=False и само решает, когда открыть браузer (после общей паузы
    _pause_for_report(), см. run_bare_launch()) -- CLI-путь (_run_impl) по-прежнему передаёт
    open_browser=interactive_mode и открывает сразу, как и раньше, возврат пути его не
    касается.

    2026-07-31, пункт I (SESSION-HANDOFF.txt): "Часть 2" ("На этом диске найден архив...",
    found_archives-параметр) больше НЕ передаётся -- теперь, когда есть отдельное действие
    ([4] Паспорт архива) для полной проверки существующего архива, дублирующая мини-версия
    того же отчёта внутри analyze не нужна. found_archive_top_level по-прежнему считается
    (питает пункт "уже есть собранный архив" в _render_analyze_recommendations()) -- просто
    больше не разворачивается в полноценный блок здесь."""
    if stats is None:
        return None  # run_analyze_for_source() уже вернула None при ошибке конфига -- не трогаем
    out_path = os.path.join(WORKDIR, "report.html")
    if stats.total_files == 0 and not stats.interrupted:
        report.generate_placeholder_report(
            "Источник оказался недоступен или пуст — ни один файл не обработан.", out_path,
            suggest_other_location=True, app_version=__version__)
    else:
        # Ctrl+C-пакет: interrupted -- прервано ДО того, как что-либо нашлось (total_files==0)
        # всё равно рендерится обычным путём, не placeholder'ом "источник пуст" -- это неверно
        # (источник не обязательно пуст, просто не успели дойти до первого файла), баннер
        # прерывания сам объясняет пустоту честнее, чем текст про "недоступен или пуст".
        #
        # REVIEW-HANDOFF.md Раунд 151, замечание 2: тот же класс симптома "застывшие 100%
        # без ориентира", что закрыт для _finalize_target_report() -- generate_report_from_
        # analyze_stats() (HTML + passport_detail.xlsx тем же _write_flat_xlsx()) молчит
        # секунды-десятки секунд на большом Паспорте. Тот же приём -- один этап с явным
        # "сколько": для Паспорта чтения логов нет (stats уже в памяти), шаг всегда один.
        _n_detail = (len(stats.encrypted_archive_paths) + len(stats.failed_archive_paths)
                     + len(stats.disputed_records) + len(stats.unreadable_records)
                     + len(stats.exact_dup_edges) + len(stats.near_dup_edges)
                     + len(stats.dump_item_paths))
        _scale = f" ({_n_detail} записей)" if _n_detail > 3000 else ""
        log("Формирую итоговый отчёт…")
        log(f"  [1/1] собираю страницу и детализированную таблицу{_scale}…")
        report.generate_report_from_analyze_stats(stats, out_path, level="analyze",
                                                    interrupted=stats.interrupted,
                                                    app_version=__version__,
                                                    source_path=source_path)
    log(f"Отчёт: {out_path}")
    if open_browser:
        _open_report_in_browser(out_path)
    return out_path


def resolve_sources(args) -> list:
    """--source может повторяться, --source-list добавляет пути построчно (пустые строки
    и строки, начинающиеся с #, игнорируются). Оба механизма складываются в один список."""
    sources = list(args.source) if args.source else []
    if args.source_list:
        with open(args.source_list, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    return sources


# 2026-08-04: было три analyze-режима (analyze-quick/analyze/analyze-full) + отдельный
# CLI-флаг для [4] Паспорт архива так и не появился (RULES.md, 2026-07-31). Средний "analyze"
# дублировал dry-run (тот считает дедуп точнее -- против живого пула, не только внутри
# источника), "analyze-full" дублировал его же плюс терял единственную свою уникальную роль
# (прикидка "влезет ли на диск") -- перенесена в dry-run, см. _bare_launch_run_dryrun().
# Сначала осталось два CLI-режима ("analyze" = переименованный analyze-quick, и отдельная
# "analyze-passport" = CLI-доступ к [4]) -- в этом же заходе, той же датой, по прямому
# предложению пользователя объединены в один: "analyze" ветвится по тому, какой из
# --source/--target дан (см. build_arg_parser()/_main()), отдельной подкоманды для [4]
# больше нет вовсе.
CLI_MODES = ("archive", "analyze")

# CLI-имя подкоманды "analyze" -> внутреннее значение AnalyzeStats.mode/run_analyze()'s mode.
# НЕ переименовано 1:1 -- строка "analyze" (без "-quick") уже занята внутри run_analyze() под
# self-scan Паспорта (run_passport() -- полный проход хеширования, без сверки с TARGET,
# self_scan=True). Внутреннее значение "analyze-quick" оставлено как есть, только CLI-имя,
# под которым оно доступно пользователю, поменялось.
_CLI_ANALYZE_MODE_MAP = {"analyze": "analyze-quick"}


def _add_source_args(p: argparse.ArgumentParser):
    p.add_argument("--source", action="append", default=None,
                    help="источник; флаг можно повторять для нескольких источников за один запуск")
    p.add_argument("--source-list", default=None,
                    help="файл со списком SOURCE, по одному пути на строку")
    p.add_argument("--sample-limit", type=int, default=0,
                    help="не более N файлов источника (быстрый тест на малой выборке)")


class _FormatsAction(argparse.Action):
    """Как встроенный action="version": печатает и выходит сразу при разборе, не дожидаясь
    проверки required=True на subparsers (--formats задаётся без подкоманды)."""

    def __call__(self, parser, namespace, values, option_string=None):
        print(format_formats_report())
        parser.exit()


def build_arg_parser() -> argparse.ArgumentParser:
    """Подкоманды: archive (по умолчанию, поведение как раньше) + analyze (read-only,
    дальнейшее ветвление по тому, какой из --source/--target дан -- НЕ отдельная подкоманда
    "analyze-passport", см. 2026-08-04 у CLI_MODES).

    "analyze --source X" (диагностика источника) и "analyze --target Y" (проверка целостности
    уже собранного архива, self-scan) -- РОВНО один из двух, не оба сразу и не ни одного;
    argparse сам по себе это не выражает (--source-list -- отдельный от --source флаг,
    складывается с ним же, не с --target -- см. resolve_sources()), поэтому проверяется
    вручную в _main() сразу после parse_args(), не здесь."""
    parser = argparse.ArgumentParser(
        description="PhotoArchive -- сборщик семейного фото- и видеоархива (см. README)",
        epilog=f"Сайт проекта: {SITE_URL}\n\n{DONATION_TEXT}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", "-V", action="version",
                         version=f"PhotoArchive {__version__} -- Сборщик семейного фото- и "
                                  f"видеоархива (rules {RULES_VERSION})\n"
                                  f"{__copyright__}, {__license__} -- сторонние "
                                  f"компоненты: см. THIRD_PARTY_LICENSES")
    parser.add_argument("--formats", action=_FormatsAction, nargs=0,
                         help="показать распознаваемые расширения файлов и выйти")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    p_archive = subparsers.add_parser("archive", help="собрать архив (поведение по умолчанию)")
    _add_source_args(p_archive)
    p_archive.add_argument("--target", default=None, help="куда собирать архив")
    p_archive.add_argument("--dry-run", action="store_true",
                            help="прогнать все решения БЕЗ копирования; в отличие от analyze "
                                 "всё же пишет обычные __служебные_файлы\\logs\\*.csv в TARGET")

    p_analyze = subparsers.add_parser(
        "analyze",
        help="read-only диагностика: --source -- быстро проверить источник (метаданные, без "
             "SHA/pHash); --target -- проверить целостность уже собранного архива "
             "(self-scan, полный проход хеширования) -- дать ровно один из двух")
    _add_source_args(p_analyze)
    p_analyze.add_argument("--target", default=None,
                            help="путь к уже собранному архиву для проверки целостности "
                                 "(взаимоисключимо с --source/--source-list)")

    return parser


def expand_sources(sources: list, target: str) -> list:
    """SOURCE=all -> все локальные диски (см. local_drive_roots()), включая диск TARGET.
    Диск TARGET сознательно НЕ исключается: самопоедание уже предотвращено на уровне пути
    самой SourceWalker._walk_dir (обрывает рекурсию, дойдя до TARGET, до спуска внутрь --
    см. RULES.md), а не на уровне "весь диск целиком". Исключение диска TARGET раньше было
    унаследовано из старой Linux/SMB-версии (там "all" значило "все примонтированные
    /mnt/win/*") и на практике тихо теряло фото, лежащие на диске TARGET вне папки архива."""
    expanded = []
    for s in sources:
        if s.strip().lower() == "all":
            expanded.extend(local_drive_roots())
        else:
            expanded.append(s)
    return expanded


BARE_LAUNCH_MENU_CHOICES = {
    "1": "view",
    "2": "dry_run",
    "3": "build",
    "4": "passport",  # 2026-07-31: [4] Паспорт архива -- см. prompt_bare_launch_menu()
    "": "view",  # Enter без ввода -> безопасный дефолт [1], ничего не трогает
    # 2026-07-12: "0" сознательно НЕ отображается: на самом главном меню возвращаться в
    # главное меню некуда (см. prompt_bare_launch_menu()) -- если ввести "0" здесь, это
    # просто невалидный ввод, как и любая другая нераспознанная строка.
}

# SESSION-HANDOFF.txt (2026-08-05, боевой прогон, п.1): подменю выбора источника/архива
# (prompt_source_submenu()/prompt_target_submenu()/prompt_passport_target_submenu()) не
# напоминали, какой режим верхнего меню сейчас выбран -- пользователь запросил ЗАМЕТНОЕ
# напоминание ("хоть звёздочками обрамляй"). Названия -- те же готовые строки, что уже есть в
# prompt_bare_launch_menu() (см. её [1]/[2]/[3]/[4] ниже), без скобочных пометок вроде
# "(read-only)".
_BARE_LAUNCH_MODE_LABELS = {
    "view": "Сканирование источника",
    "dry_run": "Пробный прогон",
    "build": "Сборка архива",
    "passport": "Паспорт архива",
}

_MENU_BACK = object()  # sentinel: "0" в подменю с allow_back=True -- в главное меню, не выход

# Config.target для analyze-режимов, которым реальный TARGET не нужен вообще (mode="analyze"/
# "analyze-quick" никогда не читают cfg.target -- см. run_analyze()) -- Config.__post_init__
# требует, чтобы source != target и оба были абсолютными путями, реального каталога не
# требует. Используется [1] Сканирование источника (source=реальный SOURCE) И [4] Паспорт
# архива (source=TARGET, который проверяется -- этому же плейсхолдеру подставляется в target,
# т.к. у паспорта нет отдельного "второго" TARGET для сверки, см. run_passport()).
_NO_TARGET_PLACEHOLDER = os.path.join(tempfile.gettempdir(), "PhotoArchive_no_target_placeholder")

# 2026-08-19: единый глобальный КОРЕНЬ распаковки для suppress_logs=True (Config.__post_init__)
# -- не привязан к конкретному TARGET/SOURCE, в отличие от прежнего дефолта под TARGET.
# Модульная константа (не инлайн-строка на месте использования в Config), т.к. используется в
# НЕСКОЛЬКИХ местах, которым нужно ссылаться на РОВНО ТОТ ЖЕ путь: Config.__post_init__ (где под
# ним назначается PID-подпапка текущего прогона, cfg.tmp_extract = <этот путь>/<pid>) и
# _sweep_stale_dry_run_pid_dirs() (которая подметает ЧУЖИЕ PID-подпапки здесь же -- живая
# находка ревизора, Раунд 106 придирка 2 → Раунд 107 замечание: раньше остатки "жёсткого" (не
# KeyboardInterrupt) прерывания dry-run подчищал только следующий прогон НА ТОМ ЖЕ TARGET;
# перевод на единый путь под %TEMP% без per-процесс изоляции (просто эта константа как есть,
# без PID) решил ЭТУ проблему, но открыл новую -- конкурентный прогон мог удалить чужую АКТИВНУЮ
# распаковку (общий путь, sha256-имя папки не несёт информации о владельце). PID-подпапка -- и
# то, и другое сразу: подчистка не завязана на TARGET, но по-прежнему не трогает живые чужие
# прогоны -- см. _pid_is_alive()/_sweep_stale_dry_run_pid_dirs()).
_DRY_RUN_TMP_EXTRACT_DIR = os.path.join(tempfile.gettempdir(), "PhotoArchive_tmp_extract")


def _progress_note_budget(min_width: int = 20, reserve: int = 80) -> int:
    """2026-07-12, живой репорт пользователя: "самые длинные строки при распаковке архива...
    не помещаются даже на полном экране". Старый фиксированный maxlen=60 в
    _truncate_progress_note() не учитывал ни реальную ширину терминала, ни то, что рядом с
    note в той же самой строке tqdm ещё печатает префикс ("Фаза N — текст — ") и хвостовые
    счётчики (обработано/скорость/ETA) -- в сумме легко вылезало за пределы экрана ЛЮБОЙ
    ширины, не только узкой. tqdm сам НЕ обрезает desc (dynamic_ncols управляет только
    собственно полосой прогресса `{bar}`), так что бюджет под note приходится считать здесь.
    reserve -- грубая (не посимвольно точная -- note обрезается по вызову ДО того, как ProgressReporter
    вообще знает итоговый desc) оценка веса всего остального: самый длинный реальный префикс
    ("Фаза 2-5 — обработка источника — " ~33 симв.) плюс типичный tqdm-хвост
    ("XXX [MM:SS, N.Nфайл/с]" ~40+ симв.)."""
    if not sys.stderr.isatty():
        return 60  # нет реального терминала (файл/пайп) -- поведение как было до этой правки
    columns = _console_columns()
    return max(min_width, columns - reserve)


def _truncate_progress_note(text: str, maxlen: int = None) -> str:
    """2026-07-11, user feedback: unlike _display_path() below (center-truncation, keeps
    head+tail to disambiguate between similarly-named full paths in one-off messages), a
    progress-bar "programm сейчас здесь копается" note only needs the END of the path -- the
    part that actually changes as the walk descends deeper -- the drive/source root at the
    start is already shown once elsewhere (the SOURCE: ... banner line) and would just be
    dead weight repeated on every directory. Leading "…" makes clear the start was cut, not
    that this is the whole (suspiciously short-looking) path.

    2026-07-12: maxlen defaults to _progress_note_budget() (real terminal width minus a
    reserve for the rest of the tqdm line) instead of a flat 60 -- see that function's
    docstring for why."""
    if maxlen is None:
        maxlen = _progress_note_budget()
    if len(text) <= maxlen:
        return text
    return "…" + text[-(maxlen - 1):]


def _display_path(path: str, maxlen: int = 60) -> str:
    """ТЗ-меню 2026-07-10, раздел 0: длинные пути в интерактивном выводе -- обрезка по
    центру с '...', показывает начало (диск/корень) и конец (то, что реально отличает один
    путь от другого) одновременно."""
    if len(path) <= maxlen:
        return path
    keep = maxlen - 3
    head = keep // 2
    tail = keep - head
    return path[:head] + "..." + path[-tail:]


@contextlib.contextmanager
def _prevent_sleep():
    """ТЗ-меню 2026-07-10, раздел 9 "Предотвращение сна компьютера": держит систему от сна
    на длинных операциях штатным SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED).
    ES_DISPLAY_REQUIRED сознательно НЕ ставится -- экрану гаснуть не мешаем, только
    сну/гибернации. Снятие -- гарантированно через finally при любом исходе (успех/ошибка/
    Ctrl-C). No-op вне Windows (dev/тест на Linux)."""
    if os.name != "nt":
        yield
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass


def enumerate_menu_drives() -> list:
    """ТЗ-меню 2026-07-10, раздел 2: список дисков для подменю выбора источника/архива --
    ТОЛЬКО фильтр для пунктов МЕНЮ, --source/--target/«своя папка» по-прежнему принимают
    любой путь как есть. Показываем фиксированные локальные (DRIVE_FIXED) и вставленные
    читаемые съёмные/оптические (DRIVE_REMOVABLE/DRIVE_CDROM -- os.path.exists() уже
    отсеивает пустые приводы без носителя раньше, чем до них доходит GetDriveTypeW). НЕ
    показываем сетевые замапленные диски (DRIVE_REMOTE) -- не входят в перечисленные типы.
    Пустой список вне Windows (dev/тест на Linux, где буквы дисков не существуют)."""
    if os.name != "nt":
        return []
    import string
    DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_CDROM = 2, 3, 5
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except Exception:
            continue
        if dtype in (DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_CDROM):
            drives.append(root)
    return drives


def _menu_choice(n_options: int, default: int = None, input_fn=input, log=print,
                  allow_back: bool = False) -> int:
    """Общий цикл выбора номера пункта меню (1..n_options) -- невалидный ввод переспрашивает
    в цикле, без предела попыток, без падения (раздел 0 ТЗ). allow_back=True: "0" возвращает
    sentinel _MENU_BACK вместо числа -- не пересекается с диапазоном 1..n_options.

    2026-07-12, упрощение по прямой просьбе пользователя ("меню перегружено"): "0" везде
    означает ОДНО и то же -- вернуться в главное меню (run_bare_launch()), а не "шаг назад"
    на один экран (была более сложная стек-based версия того же дня, отменена в тот же
    заход). Дефолт allow_back=False не меняет поведение существующих вызовов (частичный CLI
    из _main() -- там возврата в меню нет, самого меню не существует)."""
    prompt = "  Ваш выбор" + (f" [по умолчанию {default}]" if default else "") + ": "
    while True:
        answer = input_fn(prompt).strip()
        if allow_back and answer == "0":
            return _MENU_BACK
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= n_options:
            return int(answer)
        back_hint = ", 0 — главное меню" if allow_back else ""
        log(f"  Не понял ввод — введите число от 1 до {n_options}"
            + (", или нажмите Enter" if default else "") + back_hint + ".")


def _log_menu_line_wrapped(head: str, sep: str, tail, log, indent: str = "    ") -> None:
    """Строка меню диска "head+sep+tail" (prompt_target_submenu()/
    prompt_passport_target_submenu()) -- если целиком не помещается в ширину терминала,
    переносит tail на новую строку с отступом indent, вместо непредсказуемого разрыва
    посреди слова самим терминалом (SESSION-HANDOFF.txt, 2026-08-05, боевой прогон п.7:
    "[1] Диск C: → ...(папки пока нет — возможное место для / архива) (тот же диск...)" --
    разрыв ровно между словами "для" и "архива"). При переносе sep опускается -- он нужен
    только чтобы отделить tail от head НА ОДНОЙ строке, второй строке отступ уже даёт indent.

    tail -- одна строка (как раньше) ИЛИ список независимых "атомарных" кусков (например,
    отдельно статус и отдельно "(тот же диск, что и источник)"). 2026-08-06, боевой прогон:
    на достаточно узком терминале ОДНОЙ строки с status+suffix, перенесённой первым проходом
    выше, всё равно не хватало -- терминал переносил её САМ, снова посреди фразы ("...
    добавилось" / "бы)  (тот же диск...)"), потому что прежняя версия считала status+suffix
    одним неразрывным блоком. Список кусков жадно упаковывается по строкам (каждый кусок
    остаётся целым, разрыв только МЕЖДУ кусками) -- при 1 куске поведение то же, что раньше.

    REVIEW-HANDOFF.md, Раунд 69, замечание 2: жадная упаковка сама по себе не защищала от
    куска, который САМ ПО СЕБЕ длиннее доступной ширины (columns - len(indent)) -- такой
    кусок всё равно уходил в log() одной строкой длиннее терминала, терминал переносил его
    ещё раз посреди слова, ровно тот баг-класс, который вся эта функция должна закрывать.
    Теперь слишком длинный кусок переносится ПО СЛОВАМ тем же приёмом, что и
    _wrap_console_text() (textwrap, break_long_words=False -- одно "слово" без пробелов
    длиннее ширины остаётся как есть, не режется посередине)."""
    parts = [tail] if isinstance(tail, str) else list(tail)
    columns = _console_columns() if sys.stdout.isatty() else 80
    full_tail = "  ".join(parts)
    line = f"{head}{sep}{full_tail}"
    if len(line) <= columns:
        log(line)
        return
    log(head)
    avail = max(columns - len(indent), 1)
    current = ""
    for part in parts:
        candidate = f"{current}  {part}" if current else part
        if len(candidate) <= avail:
            current = candidate
            continue
        if current:
            log(f"{indent}{current}")
            current = ""
        if len(part) <= avail:
            current = part
            continue
        wrapped = textwrap.wrap(part, width=avail, break_long_words=False,
                                 break_on_hyphens=False) or [part]
        for w in wrapped[:-1]:
            log(f"{indent}{w}")
        current = wrapped[-1]
    if current:
        log(f"{indent}{current}")


def prompt_source_submenu(input_fn=input, log=print, allow_back: bool = False,
                           mode_label: str = None):
    """ТЗ-меню 2026-07-10, раздел 2: выбор источника -- локальные диски
    (enumerate_menu_drives()) + «своя папка». Первый пункт -- дефолт по Enter. Поддерживает
    перетаскивание папки мышкой (Windows вставляет путь в кавычках, снимаем их).
    allow_back=True добавляет пункт [0] Главное меню и может вернуть sentinel _MENU_BACK
    вместо пути -- нумерация дисков/«своей папки» не сдвигается, 0 не пересекается с 1..N.

    mode_label (SESSION-HANDOFF.txt, 2026-08-05, боевой прогон п.1): название режима верхнего
    меню (см. _BARE_LAUNCH_MODE_LABELS) -- печатается заметной строкой `*** НАЗВАНИЕ ***` перед
    заголовком, тот же голый ASCII-приём ("=" * N), что и print_welcome_banner(), не
    Unicode-рамка. None (частичный CLI-доспрос, run_bare_launch() его не задаёт) -- строка не
    печатается, поведение как раньше."""
    drives = enumerate_menu_drives()
    log("")
    if mode_label:
        log(f"*** {mode_label} ***")
        log("")
    log("  Откуда взять фотографии?")
    log("")
    for i, d in enumerate(drives, 1):
        log(f"    [{i}] Диск {d[:2]}  — найти фотографии на всём диске")
    custom_n = len(drives) + 1
    log(f"    [{custom_n}] Указать свою папку")
    if allow_back:
        log("")
        log("    [0] Главное меню")
        log("    [Ctrl-C] Выход из программы")
    log("")
    choice = _menu_choice(custom_n, default=1 if drives else None, input_fn=input_fn, log=log,
                          allow_back=allow_back)
    if choice is _MENU_BACK:
        return _MENU_BACK
    if choice <= len(drives):
        return drives[choice - 1]
    path = input_fn("  Путь к папке (можно перетащить папку сюда мышкой): ").strip()
    return _strip_surrounding_quotes(path)


def prompt_target_submenu(sources: list, input_fn=input, log=print, allow_back: bool = False,
                           dry_run: bool = False, mode_label: str = None):
    """ТЗ-меню 2026-07-10, раздел 3: выбор архива -- ТОЛЬКО для [2]/[3]. Диск-пункты всегда
    предлагают `буква:\\__PhotoArchive__` целиком (снимает старый вопрос "создавать ли папку в
    корне диска" -- confirm_drive_root_target_interactively() в этом пути больше не
    вызывается для диск-пунктов, только для «своей папки», если введён голый корень).
    Имя папки -- __PhotoArchive__, не голое "PhotoArchive" (2026-07-20, пятый заход) -- см.
    confirm_drive_root_target_interactively() за обоснованием (коллизия с папкой, которую
    пользователь и так заводит для .exe).
    allow_back=True -- см. prompt_source_submenu().

    2026-08-05 (SESSION-HANDOFF.txt п.14): dry_run=True -- этот же экран используется и [2]
    (пробный прогон, ничего не пишет), и [3] (реальная сборка) -- формулировки "Куда сложить
    архив?"/"уже есть — допишу новые фото" были написаны для [3] и пугали пользователя на [2]
    обещанием действия, которого dry-run не совершает. dry_run=True меняет заголовок и статусы
    на нейтральные "что покажет пробный прогон", [3] (dry_run=False, дефолт) не меняется.

    mode_label -- см. prompt_source_submenu()."""
    drives = enumerate_menu_drives()
    log("")
    if mode_label:
        log(f"*** {mode_label} ***")
        log("")
    log("  Какой архив проверить пробным прогоном?" if dry_run else "  Куда сложить архив?")
    log("")
    sources_is_all = any(s.strip().lower() == "all" for s in sources)
    source_drive_letters = {
        os.path.splitdrive(os.path.abspath(s))[0].upper()
        for s in sources if s.strip().lower() != "all"
    }
    for i, d in enumerate(drives, 1):
        candidate = os.path.join(d, "__PhotoArchive__")
        if os.path.isdir(winlong(candidate)) and _target_has_existing_archive(candidate):
            status = "уже есть — проверю, что добавилось бы" if dry_run else "уже есть — допишу новые фото"
        elif os.path.isdir(winlong(candidate)):
            status = "папка уже есть"
        elif dry_run:
            status = "папки пока нет — возможное место для архива"
        else:
            # 2026-07-11 (this session), user feedback: this submenu runs BEFORE the caller
            # decides dry_run vs. real build (see run_bare_launch()) -- "папка будет создана"
            # overpromised action that a dry-run preview never actually takes ("пробный прогон
            # ничего не пишет"). Neutral present-tense status instead, true regardless of what
            # the user picks next.
            status = "папки пока нет"
        same_disk = sources_is_all or d[:2].upper() in source_drive_letters
        # 2026-08-06, боевой прогон: status и suffix -- два независимых атомарных куска, не
        # один неразрывный tail (см. _log_menu_line_wrapped()) -- иначе на узком терминале
        # даже перенесённая на новую строку пара "status+suffix" сама не помещалась и
        # переносилась ЕЩЁ раз терминалом, снова посреди фразы.
        tail_parts = [f"({status})"]
        if same_disk:
            tail_parts.append("(тот же диск, что и источник)")
        _log_menu_line_wrapped(f"    [{i}] Диск {d[:2]}  →  {candidate}", "   ",
                                tail_parts, log)
    custom_n = len(drives) + 1
    log(f"    [{custom_n}] Указать свою папку")
    if allow_back:
        log("")
        log("    [0] Главное меню")
        log("    [Ctrl-C] Выход из программы")
    log("")
    choice = _menu_choice(custom_n, default=None, input_fn=input_fn, log=log,
                          allow_back=allow_back)
    if choice is _MENU_BACK:
        return _MENU_BACK
    if choice <= len(drives):
        return os.path.join(drives[choice - 1], "__PhotoArchive__")
    path = input_fn("  Путь к папке (можно перетащить папку сюда мышкой): ").strip()
    return _strip_surrounding_quotes(path)


def prompt_passport_target_submenu(input_fn=input, log=print, allow_back: bool = False,
                                    mode_label: str = None):
    """[4] Паспорт архива -- выбор УЖЕ СУЩЕСТВУЮЩЕГО архива для проверки, не место для новой
    сборки (в отличие от prompt_target_submenu() выше, написанного для [2]/[3] -- его текст
    "Куда сложить архив?"/статусы "папки пока нет" читались бы неверно здесь: паспорту нечего
    "складывать"). Показывает только диски, где __PhotoArchive__ реально уже существует (та же
    проверка, что и "уже есть — допишу новые фото" статус выше) -- плюс всегда доступный пункт
    "своя папка" (архив мог быть создан вручную под другим именем/не в корне диска, тот же
    случай, что и у обычного submenu).

    mode_label -- см. prompt_source_submenu()."""
    drives = enumerate_menu_drives()
    candidates = [d for d in drives
                  if _target_has_existing_archive(os.path.join(d, "__PhotoArchive__"))]
    log("")
    if mode_label:
        log(f"*** {mode_label} ***")
        log("")
    log("  Какой архив проверить?")
    log("")
    for i, d in enumerate(candidates, 1):
        _log_menu_line_wrapped(f"    [{i}] Диск {d[:2]}  →", "  ",
                                os.path.join(d, "__PhotoArchive__"), log)
    if not candidates:
        log("    Готовых архивов на дисках по умолчанию не найдено.")
    custom_n = len(candidates) + 1
    log(f"    [{custom_n}] Указать свою папку")
    if allow_back:
        log("")
        log("    [0] Главное меню")
        log("    [Ctrl-C] Выход из программы")
    log("")
    choice = _menu_choice(custom_n, default=None, input_fn=input_fn, log=log,
                          allow_back=allow_back)
    if choice is _MENU_BACK:
        return _MENU_BACK
    if choice <= len(candidates):
        return os.path.join(candidates[choice - 1], "__PhotoArchive__")
    path = input_fn("  Путь к папке (можно перетащить папку сюда мышкой): ").strip()
    return _strip_surrounding_quotes(path)


def _sum_stats(dicts: list) -> dict:
    total = {}
    for d in dicts:
        for k, v in d.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
            elif isinstance(v, list):
                # Живая находка (PROMPT_report_run_redesign.md, Раздел 2 «Что не скопировано»,
                # Фаза 0, 2026-08-14): stats["encrypted_archives"]/["dvd_units_copied"]/
                # ["dvd_units_skipped_duplicate"] -- списки путей/dict, не числа -- без этой
                # ветки они молча выпадали из total (ни в одну из веток выше не попадали),
                # даже при ОДНОМ источнике -- report.py.get("encrypted_archives") всегда
                # получал None/[], отчёт никогда не показывал запароленные архивы/DVD-дубли,
                # подтверждено исполнением (см. промпт), не только чтением кода.
                total.setdefault(k, []).extend(v)
            elif k == "album_profiles" and isinstance(v, dict):
                # Задача 0 (SESSION-HANDOFF.txt, 2026-08-07): album_profiles -- dict[str, dict]
                # с ВЛОЖЕННЫМИ set/int (n/years/cameras/date_subdirs), не плоский
                # {альбом: число} -- общая числовая ветка выше (merged[k] = merged.get(k, 0)
                # + n) технически не ляжет (0 + dict -- TypeError), нужна отдельная ветка: числа
                # сложить, множества объединить по каждому album_prefix.
                merged = total.setdefault(k, {})
                for prefix, profile in v.items():
                    dest = merged.setdefault(prefix, {
                        "name": profile.get("name"), "n": 0,
                        "years": set(), "cameras": set(), "date_subdirs": set(),
                    })
                    dest["n"] += profile.get("n", 0)
                    dest["years"] |= profile.get("years") or set()
                    dest["cameras"] |= profile.get("cameras") or set()
                    dest["date_subdirs"] |= profile.get("date_subdirs") or set()
    return total


# Пакет п.4 (SESSION-HANDOFF.txt): _print_human_view_summary()/_print_human_dryrun_summary()
# (ТЗ-меню 2026-07-10, разделы 4/5) удалены целиком 2026-07-24 -- обе печатали числовую
# сводку [1]/[2] непосредственно в консоль, полностью дублируя report.html (пп.1-3 той же
# задачи сделали report.html/dryrun_report.csv самодостаточным источником этих цифр).
# Единственные вызывающие места были _bare_launch_run_view()/_bare_launch_run_dryrun() ниже.


def _confirm_build_summary(sources: list, target: str, input_fn=input, log=print) -> bool:
    """ТЗ-меню 2026-07-10, разделы 6/9 (развилка 4 раздела 11): единственное подтверждение
    перед реальной записью. Если TARGET к тому же похож на чужую непустую папку
    (_target_needs_confirmation()==True) -- предупреждение добавляется В ТОТ ЖЕ вопрос,
    вместо отдельного второго «да» (не показывать два подтверждения подряд)."""
    log("")
    log("  Проверьте, всё ли верно:")
    log("")
    for s in sources:
        if s.strip().lower() == "all":
            desc = "все локальные диски"
        elif _is_bare_drive_root(s):
            desc = f"диск {s} (весь диск)"
        else:
            desc = _display_path(s)
        log(f"    Беру фотографии с:   {desc}")
    log(f"    Складываю архив в:   {_display_path(target)}")
    log("")
    if _target_needs_confirmation(target):
        log(f"  В папке {_display_path(target)} уже есть что-то, помимо архива — новые файлы")
        log("  будут добавлены туда же.")
    log("  Ваши исходные фотографии останутся на месте — я их не трогаю.")
    answer = input_fn("  Начать сборку? (да / нет): ").strip().lower()
    if answer != "да":
        log("Отменено.")
        return False
    return True


def _pause_before_exit(interactive_mode: bool, input_fn=input, report_path: str = None):
    """ТЗ-меню 2026-07-10, раздел 0: пауза в конце ЛЮБОГО интерактивного сценария --
    критично для запуска мышкой (иначе окно моргнёт и пропадёт). НЕ ставится для полного
    CLI (раздел 9а) -- консоль никуда не денется, а пауза повесит любой вызывающий скрипт.

    report_path (2026-07-20, просьба пользователя): раньше report.html открывался в браузере
    молча, сразу по завершении обработки, до того как пользователь успевал прочитать консоль
    -- теперь тот же Enter, что и обычный выход, дополнительно открывает браузер, явно об
    этом предупредив в самой подсказке, а не тихо. None -- отчёт не создавался/открывать не
    нужно, подсказка та же, что была всегда."""
    if not interactive_mode:
        return
    prompt = ("\nНажмите Enter, чтобы открыть отчёт в браузере и закрыть это окно: "
              if report_path else "\nНажмите Enter для выхода: ")
    try:
        input_fn(prompt)
    except EOFError:
        pass
    if report_path:
        _open_report_in_browser(report_path)


def _pause_for_report(report_path: str, input_fn=input, log=print, auto_open_browser: bool = True):
    """2026-07-21, по прямой просьбе пользователя (по итогам живого прогона релиза v0.1.1) --
    общая пауза после ЛЮБОГО из трёх пунктов голого меню ([1]/[2]/[3]), не только после
    сборки: работа уже закончена, результат уже виден на экране, Enter -- явный сигнал "я
    прочитал", после которого открывается report.html и меню возвращается к выбору режима.
    В отличие от _pause_before_exit() выше (которая используется только однократными
    CLI-прогонами, где после неё программа ДЕЙСТВИТЕЛЬНО завершается) -- здесь программа не
    закрывается, единственный способ выйти из голого меню целиком остаётся Ctrl+C/закрытие
    окна (EOFError, если она всё же случится здесь -- например, стандартный ввод закрыт --
    НЕ гасится, всплывает как обычно и завершает всю программу через main(), см. её docstring).

    None -- отчёт не формировался (сборка отклонена/не удалась, или ни один SOURCE не
    обработался) -- тогда ждать нечего, вызывающий код просто возвращается в меню сам.

    auto_open_browser (2026-08-23, по прямой просьбе пользователя): текстовый режим (CLI/dev
    без GUI, единственный вызывающий здесь input_fn=input) не тронут -- Enter по-прежнему сам
    открывает браузер, тот же довод, что и в её же комментарии выше. Для GUI (см. вызывающие
    места в gui_menu.py, все передают False) открытие браузера стало отдельным, необязательным
    действием пользователя -- кликабельной ссылкой ВНУТРИ самого нотиса (_notice_window()),
    не побочным эффектом клика по кнопке "В главное меню". `_open_report_in_browser()` вызывает
    сам `gui_menu._make_ok_input_fn()`'s колбэк ссылки, не эта функция -- см. её докстринг."""
    if not report_path:
        return
    input_fn("\nРабота окончена. Нажмите Enter, чтобы открыть отчёт и вернуться в главное меню: ")
    if auto_open_browser:
        _open_report_in_browser(report_path)


def print_welcome_banner(log=print):
    """RULES.md, "ЗАПУСК" п.3/ТЗ-меню раздел 1: приветственный баннер вместо строки-ошибки
    при полностью голом запуске -- тон "веду", не "не хватает данных".

    2026-07-12, по прямой просьбе пользователя: версия программы и подсказка про
    `--help` для опытных пользователей раньше показывались отдельным блоком после КАЖДОГО
    выбора в меню режима (`prompt_bare_launch_menu()`) -- не связано с самим выбором,
    только сбивало с толку. Перенесено сюда, показывается один раз. Формулировка про
    `--help` заменена с "Опытным пользователям: ..." (звучало как деление на "своих"/
    "чужих") на нейтральное "Подробнее о параметрах запуска: ..."."""
    log("=" * 62)
    log("")
    log(f"   PhotoArchive версия {__version__}")
    log(f"   Сайт: {SITE_URL}")
    log("   Бережная сборка семейного фотоархива")
    log("")
    log("   - Ваши оригиналы не изменяются и не удаляются")
    log("   - Фотографии остаются на вашем компьютере — интернет не нужен")
    log("   - Остановить в любой момент: Ctrl+C")
    log("   - Подробнее о параметрах запуска: PhotoArchive --help")
    log("")
    log("=" * 62)


def prompt_bare_launch_menu(input_fn=input, log=print) -> str:
    """Меню режимов для полностью голого запуска (RULES.md, "ЗАПУСК" п.3).
    Enter (пустой ввод) -> безопасный дефолт [1] -- нервный пользователь, жмущий Enter не
    глядя, должен попасть на "сканирование" (ничего не трогает), а не на реальную сборку.
    Невалидный ввод переспрашивает в цикле, не падает.

    2026-07-31: [4] Паспорт архива добавлен четвёртым пунктом (SESSION-HANDOFF.txt,
    design-сессия по "Паспорту архива") -- НЕ внутренняя "Фаза N" внутри [3] (это вернуло бы
    Ctrl+C-конфликт: паспорт снова оказался бы на пути обычной сборки), самостоятельное
    действие, отработавшее по тому же паттерну "отработал -> отчёт -> назад в меню", что и
    остальные три. run_bare_launch() ветвится на этот mode ДО prompt_source_submenu() --
    паспорту SOURCE не нужен вообще, только TARGET (см. prompt_passport_target_submenu()).

    2026-07-12, по прямой просьбе пользователя: раньше здесь тоже был пункт `[0] Выход`, но
    на всех ДРУГИХ экранах `0` означает «вернуться в ЭТО САМОЕ главное меню» -- на самом
    главном меню это не имеет смысла (возвращаться уже некуда), да и разное значение одной
    и той же кнопки на разных экранах нелогично. Явного пункта выхода здесь больше нет --
    Ctrl+C (анонсирован в приветственном баннере) и закрытие окна остаются штатным способом
    выйти. `0`, если всё же ввести, просто не совпадёт ни с одним пунктом -- обычный
    невалидный ввод, переспрос в цикле.

    2026-07-12, тем же вечером, по отдельному отзыву пользователя ("режет слух"): старые
    формулировки смешивали голоса -- [1] говорил от лица пользователя ("что у меня есть",
    двусмысленно чьё), [2] от лица программы ("покажу, что сделаю"), [3] вообще без
    пояснения. Единый стиль -- сухие технические существительные без "я"/"у меня" на всех
    трёх пунктах, пояснение в скобках у каждого."""
    log("")
    log("  Что сделать?")
    log("")
    log("    [1] Сканирование источника   (read-only)")
    log("    [2] Пробный прогон   (dry-run, без записи)")
    log("    [3] Сборка архива")
    log("    [4] Паспорт архива   (проверка уже собранного архива)")
    log("")
    log("    [Ctrl-C] Выход из программы")
    log("")
    while True:
        answer = input_fn("  Ваш выбор [по умолчанию 1]: ").strip()
        if answer in BARE_LAUNCH_MENU_CHOICES:
            return BARE_LAUNCH_MENU_CHOICES[answer]
        log("  Не понял ввод — введите 1, 2, 3 или 4.")


def _bare_launch_run_view(sources: list, log=print) -> str:
    """Шаг [1] меню -- read-only, ничего не пишет в TARGET, TARGET вообще не спрашивается
    (раздел 4 ТЗ). Технически всегда analyze-quick (только метаданные, без SHA/pHash) --
    дубликаты/near-dup/сверка с архивом сюда не относятся, это уровень [2]/[3]. Возвращает
    путь к отчёту (или None при ошибке конфига) -- браузер открывает вызывающий код
    (run_bare_launch()) после общей паузы _pause_for_report(), не эта функция."""
    with _prevent_sleep():
        stats = run_analyze_for_source(sources[0], _NO_TARGET_PLACEHOLDER, 0,
                                        "analyze-quick", log=log)
    if stats is None:
        return None
    # Пакет п.4 (SESSION-HANDOFF.txt): числовая консольная сводка (_print_human_view_summary(),
    # удалена целиком выше) дублировала то, что и так показывает report.html -- убрано ТОЛЬКО
    # после того, как отчёт стал самодостаточным источником этих цифр (пп.1-3 той же задачи).
    report_path = _finalize_analyze_report(stats, open_browser=False, log=log,
                                            source_path=sources[0])
    if stats.interrupted:
        # Ctrl+C-пакет (2026-08-07, распространено с [3]/CLI archive на [1]): тот же приём,
        # что и _bare_launch_run_build() -- отчёт уже сформирован выше (баннер прерывания
        # внутри), заново возбуждаем KeyboardInterrupt, чтобы main() отработал как обычно
        # (пауза с report_path для голого запуска, "Прервано пользователем." + exit 130).
        raise _InterruptedRunReport(report_path)
    global _last_bare_launch_object_count
    _last_bare_launch_object_count = stats.total_files
    return report_path


def _bare_launch_run_passport(target: str, log=print) -> str:
    """Шаг [4] меню -- read-only, ничего не пишет в CSV-логи TARGET (run_passport() -- всегда
    mode="analyze", Фаза 5 никогда не вызывается, см. run_analyze()). Пишет в
    TARGET\\__служебные_файлы\\ (тот же каталог, что у обычного report.html) -- в отличие от
    [1]/[2]/analyze, паспорт всегда про КОНКРЕТНЫЙ существующий архив, не эфемерный
    WORKDIR-снимок -- разумно оставить его результат рядом с самим архивом, не только в WORKDIR
    этого запуска программы. Возвращает путь к отчёту (или None при ошибке конфига/архив
    оказался пуст) -- браузер открывает вызывающий код после общей паузы _pause_for_report(),
    тот же паттерн, что и у [1]/[2]/[3].

    2026-08-04: несмотря на префикс "_bare_launch_", теперь два вызывающих места -- меню [4]
    (run_bare_launch()) И CLI "analyze --target ..." (_main(), без браузера -- полный CLI его
    не открывает, тот же принцип, что у analyze --source/archive; изначально была отдельная
    подкоманда "analyze-passport", объединена с "analyze" тем же днём чуть позже). Имя не
    переименовано вслед за этим -- функция по-прежнему в первую очередь про шаг [4],
    переименование ради одного дополнительного вызывающего было бы чисто косметическим
    churn."""
    with _prevent_sleep():
        stats = run_passport(target, log=log)
    if stats is None:
        return None
    photosort_dir = os.path.join(target, "__служебные_файлы")
    try:
        os.makedirs(winlong(photosort_dir), exist_ok=True)
    except OSError as e:
        log(f"ОШИБКА: не удалось создать {photosort_dir}: {e}")
        return None
    out_path = os.path.join(photosort_dir, "passport.html")
    report.generate_passport_report(stats, out_path, target_path=target,
                                     interrupted=stats.interrupted, app_version=__version__)
    log(f"Паспорт архива: {out_path}")
    if stats.interrupted:
        # Ctrl+C-пакет (2026-08-07, распространено с [3]/CLI archive на [4]/CLI analyze
        # --target): тот же приём, что и _bare_launch_run_view()/_bare_launch_run_build() --
        # отчёт уже записан на диск (в TARGET, не WORKDIR -- см. докстринг выше), заново
        # возбуждаем KeyboardInterrupt для единообразной обработки в main().
        raise _InterruptedRunReport(out_path)
    global _last_bare_launch_object_count
    _last_bare_launch_object_count = stats.total_files
    return out_path


def _bare_launch_run_dryrun(sources: list, target: str, input_fn=input, log=print) -> str:
    """Шаг [2] меню -- раздел 5 ТЗ. НИКАКОГО подтверждения перед этим шагом (безопасен по
    определению): suppress_logs=True репетирует archive dry_run=True БЕЗ создания
    __служебные_файлы\\ и БЕЗ CSV/summary.txt в TARGET -- результат только на экране.
    Возвращает путь к отчёту, или None, если ни один SOURCE не обработался вовсе (отчёт не
    формировался) -- см. _bare_launch_run_view() про общую паузу перед открытием браузера."""
    # REVIEW-HANDOFF.md, Раунд 38: захвачен ДО цикла по source -- строки merged_rows ниже
    # (CollectingRunLogs._ts(), формат "%Y-%m-%d %H:%M:%S") получат метку времени строго >=
    # этого момента, что и нужно _split_rows_by_time() ниже, чтобы отличить их от настоящей
    # истории Target (все реальные записи архива по определению старше).
    run_start = time.strftime("%Y-%m-%d %H:%M:%S")
    target = resolve_drive_root_conflict(sources, target, interactive=True, input_fn=input_fn, log=log)
    expanded = expand_sources(sources, target)
    results = []
    total_processed = 0
    merged_rows = {name: [] for name in report.CSV_NAMES}
    any_interrupted = False  # Ctrl+C-пакет (2026-08-07, распространено с _bare_launch_run_build()
                              # на [2] -- раньше result.interrupted здесь вообще не проверялся,
                              # Ctrl+C во время пробного прогона молча проглатывался: _run_impl()
                              # ловит KeyboardInterrupt и выставляет флаг, но без этой проверки
                              # он никем не читался -- ни отчёта, ни выхода из программы).
    shared_pool = None  # раунд 5 ревью, вариант A: не пересканировать TARGET на каждый SOURCE
                        # этого batch'а -- см. _run_impl/run_for_source
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                log(f"\n########## SOURCE = {s} ##########")
            # Живая находка пользователя (2026-08-09): [3] (_bare_launch_run_build()) уже
            # передаёт print_summary=False (техническая консольная сводка "===== Прогон ...
            # ====="/"Итог прогона" дублирует report.html) -- [2] эту же настройку не
            # передавал, единственный пункт меню, где сводка всё ещё дублировалась на экран.
            result = run_for_source(s, target, dry_run=True, sample_limit=0, log=log,
                                     suppress_logs=True, shared_pool=shared_pool,
                                     print_summary=False)
            if not result.failed:
                results.append(result.stats)
                total_processed += result.processed_count
                shared_pool = result.pool
                # PROMPT_archive_report.md, 1.2а: CollectingRunLogs.rows -- несколько source
                # за один [2] складываются в один отчёт, тот же принцип, что _sum_stats()
                # уже делает для run_stats ниже.
                for name, rows in (result.collected_rows or {}).items():
                    merged_rows.setdefault(name, []).extend(rows)
            if result.interrupted:
                any_interrupted = True
                break
    merged = _sum_stats(results)
    if not results and not any_interrupted:
        return None
    # Пакет п.2 (SESSION-HANDOFF.txt): свободное место на диске -- прогноз именно для [2],
    # не пересчитываемая report.py величина (в отличие от остального run_stats, который
    # report.py сам не заново вычисляет, а просто читает переданное) -- считается здесь ОДИН
    # раз, кладётся в merged, откуда идёт и в CSV (история), и в report.html
    # (_render_this_run() читает run_stats.get("free_disk_bytes")) без второго вызова
    # disk_usage(). Отсутствует в merged (диск недоступен) -- секция просто не рендерится,
    # не считается ошибкой.
    try:
        merged["free_disk_bytes"] = shutil.disk_usage(winlong(target)).free
    except OSError:
        pass
    # 2026-08-04: перенесено из удалённого CLI-режима analyze-full (RULES.md, "поместятся ли
    # новые на TARGET") -- та была единственной его частью, не дублирующей то, что уже точнее
    # считает сам dry-run (сверка с TARGET здесь -- через реальный decide()/Pool, а не только
    # по SHA). Тот же запас (free_space_margin_gb), который реальная сборка проверяет перед
    # копированием (см. atomic_copy()) -- если бы это была настоящая сборка, хватило бы места.
    if "free_disk_bytes" in merged:
        margin_gb = load_yaml_config(CONFIG_YAML_PATH, log=log).get("free_space_margin_gb", 10.0)
        margin_bytes = int(margin_gb * 1024**3)
        merged["fits_after_dryrun"] = (
            merged["free_disk_bytes"] - merged.get("bytes_appended", 0)) >= margin_bytes
    write_dryrun_report_csv(os.path.join(WORKDIR, "dryrun_report.csv"), merged)
    out_path = os.path.join(WORKDIR, "report.html")
    if total_processed == 0 and not any_interrupted:
        report.generate_placeholder_report(
            "Источник оказался недоступен или пуст — ни один файл не обработан.", out_path,
            suggest_other_location=True, app_version=__version__)
    else:
        # REVIEW-HANDOFF.md, Раунд 38: Target уже существует (не первый архив с нуля) --
        # читаем его настоящую историю (RunLogs, не CollectingRunLogs -- Фаза 1 пробного
        # прогона и так уже читает то же самое содержимое, чтобы сравнивать новые файлы на
        # дубликаты) и мёржим с гипотетическими строками ЭТОГО прогона -- ничего не
        # записывается на диск, только чтение уже существующего. Target пуст -- скудность
        # отчёта честна, не трогаем (см. merged_rows-only ветка ниже, поведение не изменилось).
        target_logs_dir = os.path.join(target, "__служебные_файлы", "logs")
        combined_rows = merged_rows
        if os.path.isdir(target_logs_dir):
            target_data = report.parse_target_logs(target_logs_dir)
            if any(target_data.get(name) for name in report.CSV_NAMES):
                combined_rows = {name: list(target_data.get(name, [])) for name in report.CSV_NAMES}
                for name, rows in merged_rows.items():
                    combined_rows.setdefault(name, []).extend(rows)
        # 2026-08-14, прямая просьба пользователя ("вид отчёта по dry-run всегда должен быть
        # одинаковым с реальным прогоном, независимо от существования архива"): раньше
        # run_start передавался только при full_workdir=True (TARGET уже существует) --
        # dry-run на НОВОМ TARGET получал run_start=None, из-за чего report.py:
        # _generate_from_model() уходил в старую, отдельную ветку рендера
        # (_render_dryrun_structure_recommendations()/_render_sheet3_single(), без Разделов
        # 1-3). run_start захвачен безусловно в начале функции (см. выше) -- корректен и для
        # пустого TARGET (_split_rows_by_time() тогда просто не находит "старой" истории,
        # все строки уходят в "этот прогон", что и требуется).
        report.generate_report(combined_rows, out_path, level="workdir", run_stats=merged,
                                run_start=run_start,
                                interrupted=any_interrupted,
                                app_version=__version__, target_path=target,
                                source_paths=expanded)
    log(f"Отчёт: {out_path}")
    if any_interrupted:
        # Ctrl+C-пакет: тот же приём, что и _bare_launch_run_build() -- отчёт с баннером
        # прерывания уже на диске, заново возбуждаем KeyboardInterrupt для main().
        log(f"\n  Отчёт (данные на момент остановки): {_display_path(out_path)}")
        raise _InterruptedRunReport(out_path)
    global _last_bare_launch_object_count
    _last_bare_launch_object_count = total_processed
    return out_path


def _bare_launch_run_build(sources: list, target: str, input_fn=input, log=print) -> str:
    """Шаг [3] меню -- раздел 6 ТЗ. Единственное подтверждение (_confirm_build_summary,
    развилка 4 раздела 11) перед реальной записью. Возвращает путь к отчёту, или None, если
    пользователь отказался (или сборка вообще не состоялась) -- вызывающий код
    (run_bare_launch()) в обоих случаях просто возвращается в главное меню, отличие только в
    том, печатать ли "Возвращаемся в главное меню" (report_path всегда truthy при успехе --
    _finalize_target_report() с any_succeeded=True и open_browser=True никогда не вернёт None).

    2026-07-21, по прямой просьбе пользователя: раньше [3] был единственным пунктом меню, не
    возвращавшимся в главное меню после успеха -- вместо этого ждал явный Enter
    ("_pause_before_exit(report_path=...)") и завершал всю программу, тогда как [1]/[2] уже
    молча отрабатывали и возвращались в меню без вопросов. Асимметрия стала заметна именно
    после того, как [1]/[2] лишились своих "Что дальше?"-развилок (см. ниже) -- пользователь
    прямо указал привести [3] к тому же поведению, а после общего обсуждения -- единой паузой
    "Работа окончена..." (_pause_for_report(), см. run_bare_launch()) перед открытием браузера
    и возвратом в меню, а не молча/без паузы. Эта функция сама браузер больше не открывает --
    только возвращает путь, открытие и пауза общие для всех трёх пунктов меню."""
    target = resolve_drive_root_conflict(sources, target, interactive=True, input_fn=input_fn, log=log)
    if not _confirm_build_summary(sources, target, input_fn=input_fn, log=log):
        return None
    expanded = expand_sources(sources, target)
    # 2026-07-20: момент ДО начала обработки -- report._split_rows_by_time() отбирает для
    # Листа 3 отчёта только "новое в этом пополнении" (timestamp >= run_start). Тот же
    # формат, что RunLogs._ts() пишет в каждую строку CSV-лога.
    run_start = time.strftime("%Y-%m-%d %H:%M:%S")
    # 2026-07-12, живой репорт пользователя (запустил вторую копию программы в другом окне,
    # пока первая уже собирала архив в тот же TARGET): run_for_source() возвращает
    # RunResult(failed=True) и печатает "ОШИБКА: ..." при TargetLocked (LOCK-файл уже занят
    # другим процессом), но раньше возврат ИГНОРИРОВАЛСЯ -- ниже безусловно печаталось
    # "Готово. Архив собран", даже если ни один файл фактически не скопировался.
    # any_succeeded отслеживает это.
    any_succeeded = False
    total_processed = 0
    any_stopped_for_space = False  # 4.2 (PROMPT_report_marketing.md): триада исхода --
                                    # RunResult.stopped_for_space живёт ВНЕ result.stats
                                    # (см. run_for_source()), _sum_stats(results) ниже его
                                    # не увидит -- отслеживаем отдельно, тем же приёмом, что
                                    # free_disk_bytes у [2] (_bare_launch_run_dryrun()).
    any_interrupted = False  # Ctrl+C-пакет: тем же приёмом, что и any_stopped_for_space выше
    results = []  # RunResult.stats по каждому успешному SOURCE -- для секции "Этот прогон"
                  # (report._render_this_run()), тот же принцип суммирования, что и
                  # _bare_launch_run_dryrun() уже делает для консольной сводки [2].
    shared_pool = None  # раунд 5 ревью, вариант A: не пересканировать TARGET на каждый SOURCE
                        # этого batch'а -- см. _run_impl/run_for_source
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                log(f"\n########## SOURCE = {s} ##########")
            result = run_for_source(s, target, dry_run=False, sample_limit=0, log=log,
                                     shared_pool=shared_pool, print_summary=False)
            if not result.failed:
                any_succeeded = True
                total_processed += result.processed_count
                shared_pool = result.pool
                results.append(result.stats)
                any_stopped_for_space = any_stopped_for_space or result.stopped_for_space
            if result.interrupted:
                any_interrupted = True
                break
    if any_interrupted:
        # Ctrl+C-пакет: НЕ проглатывается -- отчёт с баннером прерывания формируется здесь
        # (пока есть target/results в области видимости), затем KeyboardInterrupt возбуждается
        # заново, чтобы main() отработал как обычно (сообщение "Прервано пользователем.",
        # sys.exit(130)) -- эта функция не меняет то, что Ctrl+C останавливает программу,
        # только добавляет отчёт перед остановкой.
        merged = _sum_stats(results)
        merged["stopped_for_space"] = any_stopped_for_space
        report_path = _finalize_target_report(target, "target", any_succeeded, total_processed,
                                               open_browser=True, log=log, run_stats=merged,
                                               run_start=run_start, interrupted=True,
                                               source_paths=expanded)
        if report_path:
            log(f"\n  Отчёт (данные на момент остановки): {_display_path(report_path)}")
        raise _InterruptedRunReport(report_path)
    if not any_succeeded:
        log("")
        log("  Сборка не выполнена — см. сообщение об ошибке выше.")
        return None
    merged = _sum_stats(results)
    merged["stopped_for_space"] = any_stopped_for_space
    report_path = _finalize_target_report(target, "target", any_succeeded, total_processed,
                                           open_browser=True, log=log,
                                           run_stats=merged, run_start=run_start,
                                           source_paths=expanded)
    log("")
    log(f"  Готово. Архив собран в {_display_path(target)}")
    log("")
    # 2026-07-12, user feedback: старая формулировка ("не спешите удалять, пользуйтесь
    # архивом сколько нужно, чтобы убедиться, что всё на месте") звучала как намёк на
    # ненадёжность результата -- будто программа сама не уверена, что архив собрался
    # правильно. Исходные файлы остаются нетронутыми независимо от качества архива (это
    # свойство программы, а не оговорка про корректность), архив описываем уверенно, без
    # "чтобы убедиться".
    log("  Ваши исходные фотографии остались на месте — программа их не трогает.")
    log("  Архив — их полная копия, готовая к использованию.")
    global _last_bare_launch_object_count
    _last_bare_launch_object_count = total_processed
    return report_path


def run_bare_launch(input_fn=input, log=print):
    """Полностью голый запуск (sys.argv[1:] целиком пуст -- типично двойной клик по exe без
    единого аргумента, см. _main()) -- меню вместо мгновенного archive-прогона. ТЗ-меню
    2026-07-10 (PROMPT_interactive_menu.md) -- решения записаны в RULES.md, здесь --
    реализация.

    2026-07-12, возврат назад: сначала была отдельная стек-based версия (именованные экраны,
    возврат на один уровень назад ИЛИ на именованный уровень выше), отменена в тот же день по
    прямой просьбе пользователя -- "меню перегружено", один универсальный `[0] Главное меню`
    вместо разных вариантов "назад" достаточен. Реализовано максимально просто: весь голый
    запуск -- один `while True` вокруг лестницы `mode -> source -> [view] -> target ->
    [dry_run] -> build`, и `continue` (сброс на mode) -- единственный способ вернуться назад,
    какой бы глубины ни достигла лестница. `check_bundled_tools()` вызывается один раз за
    весь запуск (`tools_checked`), не при каждом возврате в главное меню.

    2026-07-15, живая находка: баннер печатается ПЕРВЫМ, а не после технической строки про
    photoarchive_config.yaml -- баннер специально задуман (см. print_welcome_banner()) как
    первое тёплое впечатление вместо строки-ошибки; печать служебного сообщения о
    только что созданном конфиге раньше баннера сводила этот эффект на нет. Пустая строка
    между закрывающей рамкой баннера и сообщением -- чтобы оно не липло к рамке.

    2026-07-21, по прямой просьбе пользователя: раньше после [1]/[2] показывался
    промежуточный вопрос "Что дальше?" (перейти к следующему шагу лестницы / главное меню),
    а [3] был единственным пунктом, вообще не возвращавшимся сюда -- завершал программу целиком
    после паузы "Нажмите Enter для выхода". Пользователь указал на обе несостыковки отдельно
    (после того, как отчёт после [1] оказался устроен как отчёт по архиву, который ещё не
    собран -- см. report.py) и попросил единообразия: каждый пункт меню -- самостоятельное,
    самодостаточное действие (отработал, показал отчёт, вернулся в главное меню), без
    предложений продолжить и без асимметрии между read-only-режимами и сборкой. Функция теперь
    вообще не имеет обычного return -- как и раньше, единственный выход отсюда --
    KeyboardInterrupt/EOFError, всплывающие в main().

    2026-07-21, тем же заходом, сразу следом: первая реализация открывала браузер сразу по
    завершении каждого пункта, без паузы вообще -- пользователь тут же уточнил, что хочет
    паузу, просто НЕ формулировку "для выхода" (раз выхода из программы здесь больше нет).
    Единая `_pause_for_report()` ("Работа окончена. Нажмите Enter, чтобы открыть отчёт и
    вернуться в главное меню") вызывается после [1]/[2]/[3] одинаково -- разница с
    `_pause_before_exit()` только в том, что после неё программа не закрывается.

    2026-07-31: [4] Паспорт архива ветвится СРАЗУ после mode, ДО prompt_source_submenu() --
    единственный пункт меню, которому SOURCE вообще не нужен (см. prompt_bare_launch_menu())."""
    print_welcome_banner(log=log)
    log("")
    _ensure_config_yaml_exists(CONFIG_YAML_PATH, log=log)

    tools_checked = False
    while True:
        # 2026-07-12: prompt_bare_launch_menu() больше не возвращает "exit" -- главное меню
        # не показывает [0] (возвращаться в него, будучи уже там, бессмысленно), выход --
        # только Ctrl+C/закрытие окна, см. её докстринг.
        mode = prompt_bare_launch_menu(input_fn=input_fn, log=log)
        mode_label = _BARE_LAUNCH_MODE_LABELS[mode]

        if mode == "passport":
            target = prompt_passport_target_submenu(input_fn=input_fn, log=log, allow_back=True,
                                                     mode_label=mode_label)
            if target is _MENU_BACK:
                continue
            target = _normalize_bare_drive_letter(target)
            if not tools_checked:
                check_bundled_tools(log=print)
                tools_checked = True
            report_path = _bare_launch_run_passport(target, log=log)
            _pause_for_report(report_path, input_fn=input_fn, log=log)
            continue

        source = prompt_source_submenu(input_fn=input_fn, log=log, allow_back=True,
                                        mode_label=mode_label)
        if source is _MENU_BACK:
            continue
        sources = [_normalize_bare_drive_letter(source)]

        if not tools_checked:
            check_bundled_tools(log=print)
            tools_checked = True

        if mode == "view":
            report_path = _bare_launch_run_view(sources, log=log)
            _pause_for_report(report_path, input_fn=input_fn, log=log)
            continue

        target = prompt_target_submenu(sources, input_fn=input_fn, log=log, allow_back=True,
                                        dry_run=mode == "dry_run", mode_label=mode_label)
        if target is _MENU_BACK:
            continue
        target = _normalize_bare_drive_letter(target)

        if mode == "dry_run":
            report_path = _bare_launch_run_dryrun(sources, target, input_fn=input_fn, log=log)
            _pause_for_report(report_path, input_fn=input_fn, log=log)
            continue

        report_path = _bare_launch_run_build(sources, target, input_fn=input_fn, log=log)
        if report_path is None:
            log("  Возвращаемся в главное меню.")
        else:
            _pause_for_report(report_path, input_fn=input_fn, log=log)
        continue


def _log_unexpected_crash(log=print) -> None:
    """2026-07-11, live user report ("удалил архив во время работы с ним... программа
    срубилась"): the SOURCE-scanning fixes elsewhere this session (see _handle_archive()'s two
    new try/except OSError guards, and the equivalent one in Phase 1's archive_hash_cache path)
    close the specific race that was found -- but the user's underlying requirement is broader:
    "ни одно действие пользователя с файлами параллельно с работой программы не должно
    приводить к вылету". Targeted guards can only close races that were found; this is the
    last-resort backstop for whatever wasn't. main() previously caught only
    KeyboardInterrupt/EOFError -- anything else propagated as a raw traceback. Full traceback
    goes to crash.log next to the .exe (best-effort write -- must itself never raise and take
    down the crash handler); a short Russian message goes to the console."""
    try:
        crash_log_path = os.path.join(_app_dir(), "crash.log")
        _rotate_log_if_needed(crash_log_path)
        with open(crash_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            f.write(traceback.format_exc())
    except OSError:
        pass
    # 2026-07-19 (REVIEW-HANDOFF.md Раунд 15): префикс "ОШИБКА" -- только на первой строке,
    # это единственная реально пугающая часть; две следующие reassurance-строки нарочно
    # остаются без подсветки, чтобы не выглядеть частью самой ошибки.
    log("\nОШИБКА: Произошла непредвиденная ошибка -- программа остановлена.")
    log("Ваши исходные файлы программа не изменяет и не удаляет ни при каких обстоятельствах "
        "-- эта ошибка их не затронула.")
    log(f"Подробности сохранены в {os.path.join(_app_dir(), 'crash.log')} -- приложите этот "
        f"файл, если сообщаете о проблеме.")


class _GuiExplicitExit(KeyboardInterrupt):
    """2026-08-22, Раунд 123 ревью (замечание) -- заведён, когда main() ещё различал explicit-
    выход и настоящий Ctrl-C через состояние консоли (_should_pause_before_exit()); с 2026-08-23
    (см. её докстринг) GUI-консоль вообще никогда не паузит на выходе, ни для этого типа, ни для
    голого KeyboardInterrupt -- различие де-факто перестало влиять на поведение main(). Класс
    оставлен как есть (не голый KeyboardInterrupt) -- документирует НАМЕРЕНИЕ ("пользователь сам
    попросил выйти", не "процесс прерван извне") в трассировке/логах, дешёвая семантическая
    информация, даже когда main() сейчас её не читает.

    Поднимается из явного клика "Выход"/"Выход из программы" в GUI (gui_menu.py,
    _run_wizard()/_ok_input_fn()) -- и, 2026-08-24, из крестика на нотисе "Работа окончена"
    (_notice_window(), тот же смысл -- полный выход, не "продолжить")."""


def _should_pause_before_exit(bare_launch: bool) -> bool:
    """2026-08-23, переписано по прямой просьбе пользователя ("рабочая консоль GUI-мастера --
    чистая приборная панель, не диалог; не предусматривает работу в ней с клавиатуры").
    Раньше (см. историю в git/REVIEW-HANDOFF.md, раунды 114/123/128) эта функция различала три
    состояния консоли GUI-мастера (никогда не отсоединялась / отсоединена без переоткрытия /
    переоткрыта реальной обработкой) -- вся эта логика опиралась на то, ЧТО именно видно на
    экране в момент исключения, и раз за разом ломалась, когда появлялся новый способ спрятать/
    показать окно консоли (Раунд 128 -- ровно такой случай). Новая модель проще: GUI-консоль
    ВООБЩЕ никогда не паузит на выходе (ни Ctrl-C, ни explicit-выход, ни краш -- краш идёт через
    отдельное GUI-окно, см. main()'s except Exception и gui_menu._show_crash_notice()) --
    единственный оставшийся случай паузы -- текстовое меню (не-Windows dev-сессия), где консоль
    остаётся интерактивной с самого начала и Enter реально что-то значит для пользователя.

    _console_freed_for_gui -- True для ЛЮБОГО реального Windows голого запуска (и GUI, и
    fallback на _fatal_messagebox(), см. _configure_windows_stdio_at_startup()) -- достаточно
    самой по себе, состояние _work_console_allocated (была ли консоль вообще переоткрыта под
    реальную обработку) больше не читается этой функцией."""
    return bare_launch and not _console_freed_for_gui


def main():
    # Every subprocess.run() call in this file (exiftool/7z/ffmpeg/ffprobe/UnRAR) is spawned
    # without CREATE_NEW_PROCESS_GROUP, so Ctrl-C's CTRL_C_EVENT/SIGINT already reaches those
    # children together with this process -- no separate Popen+kill needed here.
    # bare_launch: единый признак голого запуска, переиспользуется ниже для паузы перед
    # выходом (_pause_before_exit(), через _should_pause_before_exit() -- раньше пересчитывался
    # как len(sys.argv) <= 1 в 4 местах по отдельности. (2026-07-19: раньше тем же флагом ещё
    # включался белый фон консоли -- см. _console_red_text() докстрока, откачено в этой же
    # сессии.)
    bare_launch = len(sys.argv) <= 1
    try:
        sys.exit(_main())
    except KeyboardInterrupt as e:
        # 2026-07-19: через console_log(), не print() напрямую -- та же обёртка (перенос
        # длинных строк, bar-safe печать через log_line()), что и у остального вывода.
        # НЕ красная -- это штатное прерывание пользователем, не ошибка (см. console_log()
        # -- красным идёт только текст с префиксом "ОШИБКА").
        console_log("\nПрервано пользователем.")
        # 2026-07-11, live user report: on a bare double-click launch, Ctrl-C during
        # analyze/view mode printed this and closed the console window so fast it couldn't be
        # read -- this except block never called _pause_before_exit() at all, unlike every
        # normal-completion path in run_bare_launch(). Same fix, same reasoning as the
        # Exception handler below.
        # 2026-07-28, живой баг-репорт: report_path -- getattr(), не e.report_path напрямую,
        # потому что сюда долетает и обычный "безымянный" KeyboardInterrupt (Ctrl+C ДО того,
        # как что-либо успело сформировать отчёт, см. _InterruptedRunReport) -- у него этого
        # атрибута нет вовсе.
        #
        # 2026-08-23, по прямой просьбе пользователя ("Ctrl-C -- только способ убить
        # программу, без дополнительных подтверждений"): рабочая консоль GUI-мастера --
        # чистая приборная панель, не диалог (см. _hide_work_console()/_ensure_work_console()
        # ниже -- теперь minimize/restore, не hide/show) -- пользователь никогда не должен
        # нажимать Enter, чтобы её закрыть, ни при обычном Ctrl-C, ни при explicit-выходе из
        # GUI (_GuiExplicitExit, поднимается из gui_menu.py). _should_pause_before_exit()
        # теперь сама возвращает False для ЛЮБОГО Windows-голого-запуска (см. её докстринг) --
        # отдельная проверка isinstance(e, _GuiExplicitExit), нужная раньше (Раунд 123 ревью)
        # именно чтобы отличить explicit-выход от настоящего Ctrl-C, больше не нужна -- оба
        # случая уже не паузят одинаково.
        # 2026-07-28, живой баг-репорт: report_path -- getattr(), не e.report_path напрямую,
        # потому что сюда долетает и обычный "безымянный" KeyboardInterrupt (Ctrl+C ДО того,
        # как что-либо успело сформировать отчёт, см. _InterruptedRunReport) -- у него этого
        # атрибута нет вовсе. Пауза здесь по-прежнему актуальна для текстового меню
        # (не-Windows dev-сессия) -- там консоль остаётся интерактивной, Enter имеет смысл.
        if _should_pause_before_exit(bare_launch):
            _pause_before_exit(True, report_path=getattr(e, "report_path", None))
        # См. _hide_work_console_for_exit()'s докстринг -- прячет рабочую консоль немедленно,
        # не дожидаясь, пока её закроет сама ОС вместе с процессом (у onefile-сборки это не
        # мгновенно). No-op, если консоль не создавалась/не Windows.
        _hide_work_console_for_exit()
        # 2026-08-24, живая просьба пользователя: голый Windows-запуск (крестик/"Выход" в GUI,
        # Ctrl-C в рабочей консоли -- всё это KeyboardInterrupt/_GuiExplicitExit, приходят сюда
        # одинаково) -- ненулевой код 130 не давал терминальному хосту (Windows Terminal,
        # дефолтная настройка "закрывать по завершении: изящно" -- закрывает автоматически
        # ТОЛЬКО код 0) закрыть саму вкладку/окно, даже когда наш процесс и Tk-окна уже
        # полностью завершились -- выглядело как "не полный выход". Для голого запуска это не
        # ошибка, а штатное завершение по воле пользователя, код 0 честен. Настоящий CLI-запуск
        # (--source/--target и т.п., bare_launch=False) код не меняем -- 130 там осмысленный
        # конвенциональный код для скриптов/автоматизации, которые могут на него полагаться.
        sys.exit(0 if bare_launch else 130)
    except EOFError:
        # Found on real Windows hardware while testing the new bare-launch menu
        # (run_bare_launch() -- multiple new input() prompts aimed exactly at
        # non-technical users): Ctrl-Z+Enter (Windows' EOF keystroke) or a closed/redirected
        # stdin at ANY interactive prompt (menu, "Откуда брать фото", risk confirmations)
        # raises EOFError -- previously an unhandled traceback + PyInstaller's
        # "Failed to execute script" banner, same bad experience Ctrl-C already avoids.
        console_log("\nВвод прерван (нет данных на входе).")
        # Same reasoning as the KeyboardInterrupt branch above -- this can itself raise
        # EOFError again if stdin is genuinely closed/redirected (not just Ctrl-Z), which
        # _pause_before_exit() already swallows internally. Only reachable from the text
        # menu now -- GUI mode has no console input() calls at all, and
        # _should_pause_before_exit() below is False unconditionally on a Windows GUI bare
        # launch anyway (see its docstring).
        if _should_pause_before_exit(bare_launch):
            _pause_before_exit(True)
        _hide_work_console_for_exit()
        sys.exit(130)
    except Exception:
        # 2026-07-19: log=console_log (was the print() default) -- крэш-текст теперь идёт
        # через ту же обёртку log_line()/перенос строк/подсветку "ОШИБКА", что и обычные
        # ошибки пайплайна, вместо того чтобы единственный по-настоящему пугающий момент
        # оставался неокрашенным и без переноса длинных строк (REVIEW-HANDOFF.md Раунд 15).
        _log_unexpected_crash(log=console_log)
        # 2026-08-23, по прямой просьбе пользователя: рабочая консоль GUI-мастера -- чистая
        # приборная панель (см. KeyboardInterrupt-ветку выше), краш там сообщается отдельным
        # GUI-окном (gui_menu._show_crash_notice(), тот же стиль, что и нотис "Работа
        # окончена"), не консольным input(). Раньше (Раунд 128 ревью) здесь была попытка
        # показать/поднять СПРЯТАННОЕ окно консоли перед паузой -- решение оказалось временным
        # костылём: правильный фикс -- вообще не полагаться на видимость/фокус консоли для
        # краш-уведомления, GUI-окно не зависит от того, свёрнута консоль (см.
        # _hide_work_console()) или нет. bare_launch + _console_freed_for_gui==True однозначно
        # значит "это Windows голый запуск, gui_menu уже успешно импортирован и мастер уже
        # открывался" (иначе исключение сюда не долетело бы -- см. _main(), путь
        # _fatal_messagebox() возвращает 1 напрямую, не поднимает исключение).
        shown_via_gui = False
        if bare_launch and _console_freed_for_gui:
            try:
                import gui_menu
                gui_menu._show_crash_notice(
                    "ОШИБКА: Произошла непредвиденная ошибка -- программа остановлена.\n\n"
                    "Ваши исходные файлы программа не изменяет и не удаляет ни при каких "
                    "обстоятельствах -- эта ошибка их не затронула.\n\n"
                    f"Подробности сохранены в {os.path.join(_app_dir(), 'crash.log')} -- "
                    "приложите этот файл, если сообщаете о проблеме.")
                shown_via_gui = True
            except Exception:
                # best-effort, как и _fatal_messagebox() -- crash.log выше уже написан
                # независимо от того, получилось ли показать GUI-окно.
                pass
        if not shown_via_gui and _should_pause_before_exit(bare_launch):
            # Текстовое меню (не-Windows dev-сессия) -- единственный оставшийся случай, см.
            # _should_pause_before_exit()'s докстринг.
            _pause_before_exit(True)
        _hide_work_console_for_exit()
        sys.exit(1)


def _main():
    argv = sys.argv[1:]
    # По прямой просьбе пользователя 2026-08-22 ("чёрный экран до появления меню не должен
    # выскакивать совсем") -- вызывается БЕЗУСЛОВНО, первой же строкой после разбора argv, для
    # ОБЕИХ веток (голый запуск и CLI) -- см. _configure_windows_stdio_at_startup()'s докстринг:
    # build.bat теперь собирает windowed .exe, самой ОС нечего создавать/показывать, ждать
    # подтверждения GUI (как раньше) больше незачем.
    if os.name == "nt":
        _configure_windows_stdio_at_startup(bool(argv))
    # 2026-08-24, живая просьба пользователя ("что-то осталось -- не должно копиться, каждый
    # новый запуск должен подчищать всё, что было до него") -- безусловно, для ЛЮБОГО запуска
    # (голого и CLI, любого режима), не только реальной сборки: _MEIxxxxxx -- распакованная
    # PyInstaller-бутлоадером папка (exiftool/ffmpeg/7z/рантайм), которую сам бутлоадер убирает
    # только при обычном graceful-выходе -- см. _mark_own_mei_extraction_dir()'s докстринг.
    # Sweep -- ДО собственной пометки (нечего сопоставлять с самим собой, own_name уже исключён
    # явно, но порядок "сначала прибраться за старым, потом отметиться" яснее читается).
    _sweep_stale_mei_extraction_dirs()
    _mark_own_mei_extraction_dir()
    if not argv:
        # Полностью голый запуск -- НИ ОДНОГО аргумента командной строки (типично двойной
        # клик по exe). Единственный случай, который заменяется меню (RULES.md, "ЗАПУСК"
        # п.3) -- любой хотя бы один аргумент (флаг, подкоманда, даже неполный набор вроде
        # одного --source без --target) идёт по обычной ветке ниже без изменений.
        # 2026-07-21: run_bare_launch() больше не возвращается обычным путём (каждый пункт
        # меню, включая [3], сам возвращается в главное меню и открывает свой отчёт) --
        # выйти отсюда можно только через KeyboardInterrupt/EOFError, которые ловит main().
        #
        # 2026-08-22, по прямой просьбе пользователя ("не нужно текстовое дублирование GUI"):
        # текстовое меню (run_bare_launch() ниже) больше НЕ является интерактивным фоллбэком
        # для голого запуска на Windows -- GUI-мастер (gui_menu.py) теперь ЕДИНСТВЕННЫЙ
        # интерфейс для реального пользователя. Если GUI физически не может открыться (нет
        # дисплея, tkinter не установлен и т.п.) -- _fatal_messagebox() и явный ненулевой выход,
        # без попытки показать текстовое меню -- на windowed-сборке (см. build.bat) консоли для
        # него всё равно нет. Сама функция run_bare_launch() (текстовая) НЕ удалена --
        # ci/windows_ci_test.py и tests/ по-прежнему зовут её НАПРЯМУЮ, минуя _main() целиком,
        # как единственный способ прогнать всю логику dispatch (mode -> _bare_launch_run_* ->
        # pause -> continue) без реального tkinter -- см. её же докстринг. На НЕ-Windows (dev-
        # сессии) голый запуск по-прежнему идёт через неё напрямую -- это internal dev path, не
        # реальный сценарий конечного пользователя, которого касается это решение.
        #
        # 2026-08-22 (продолжение, тот же день) -- раньше здесь ещё стоял вызов
        # _free_console_for_gui_bare_launch() (переименована в _configure_windows_stdio_at_
        # startup(), см. её докстринг), гейтованный на probe_display_available()==True, чтобы
        # НЕ гасить автосозданную консоль, пока GUI не подтверждён -- та консоль была
        # единственным резервным каналом для _fatal_messagebox()'s stderr-дубля. При windowed-
        # сборке гейтовать больше нечего -- вызов ушёл на самый верх _main() (см. выше), консоли
        # не появляется ни в одном из двух исходов. Реальная консоль GUI-пути появляется только
        # когда работа (scan/dry_run/build/passport) реально стартует -- см.
        # _ensure_work_console(), вызывается из gui_menu.run_bare_launch().
        #
        # 2026-08-21 (Раунд 114 ревью, замечание 2): except Exception раньше оборачивал ВЕСЬ
        # gui_menu.run_bare_launch() -- то есть не только запуск Tk, но и реальную сборку
        # архива внутри мастера, из-за чего настоящий сбой посреди GUI-сессии терялся без
        # crash.log и выдавал вводящее в заблуждение "GUI-меню недоступно". Узкий except
        # (ImportError -- tkinter не установлен) держит только import; отдельный gui_menu.
        # probe_display_available() (создаёт и сразу уничтожает пробный tk.Tk()) решает, может
        # ли GUI открыться вообще -- если да, run_bare_launch() вызывается БЕЗ обёртки, и любое
        # исключение из середины сессии долетает до main()'s _log_unexpected_crash() как обычно.
        if os.name == "nt":
            try:
                import gui_menu
            except ImportError:
                gui_menu = None
            if gui_menu is not None and gui_menu.probe_display_available():
                gui_menu.run_bare_launch(log=console_log)
                return 0
            _fatal_messagebox(
                "PhotoArchive не смог открыть графическое меню (нет дисплея или tkinter "
                "недоступен). Программой всё ещё можно пользоваться из командной строки -- "
                "список команд: PhotoArchive --help."
            )
            return 1
        try:
            run_bare_launch(log=console_log)
        except (KeyboardInterrupt, EOFError):
            # _InterruptedRunReport (Ctrl+C mid-processing) is a KeyboardInterrupt subclass --
            # already covered here, must reach main()'s handler untouched, same as before.
            raise
        return 0
    if argv and argv[0] in ("--version", "-V", "--help", "-h", "--formats"):
        # Глобальные флаги идут напрямую в верхний парсер -- НЕ подставлять "archive" перед
        # ними, иначе верхний --help показывал бы справку только по archive (скрывая
        # analyze-*), а --version/--formats падали бы с "unrecognized arguments" (эти флаги
        # не входят в p_archive). Работает только когда argv[0] -- САМ этот флаг: "archive
        # --help" (справка конкретно по archive) и "--source X --help" (частный случай ниже)
        # сюда не попадают и разбираются как раньше.
        pass
    elif not argv or argv[0] not in CLI_MODES:
        # Обратная совместимость: без подкоманды -- поведение как раньше (сборка архива).
        # "PhotoArchive.exe --source X --target Y" продолжает работать один в один.
        argv = ["archive"] + argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.mode == "analyze":
        # 2026-08-04, по прямому предложению пользователя: раньше "проверка источника" и
        # "проверка уже собранного архива" были двумя отдельными подкомандами (analyze/
        # analyze-passport) -- объединены в одну, "analyze" ветвится по тому, какой из
        # --source/--target реально дан. argparse сам по себе не умеет "ровно один из двух
        # флагов, не оба и не ни одного" для этой комбинации (--source-list -- отдельный от
        # --source флаг, складывается с ним же в resolve_sources(), не участвует в
        # взаимоисключении с --target как таковой) -- проверяется вручную здесь.
        has_source = bool(args.source) or bool(args.source_list)
        has_target = bool(args.target)
        if has_source and has_target:
            print("ОШИБКА: analyze принимает либо --source (диагностика источника), либо "
                  "--target (проверка целостности уже собранного архива) -- не оба сразу.")
            return EXIT_CONFIG_ERROR
        if not has_source and not has_target:
            print("ОШИБКА: analyze требует --source (диагностика источника) или --target "
                  "(проверка целостности уже собранного архива).")
            return EXIT_CONFIG_ERROR
        if has_target:
            # Паспорт: self-scan уже собранного архива (TARGET), не диагностика SOURCE --
            # нет понятия SOURCE вообще в этой ветке, поэтому обрабатывается отдельно, ДО
            # resolve_sources()/interactive_mode ниже (та инфраструктура рассчитана на
            # SOURCE+TARGET у archive/analyze-с-источником). Интерактивного доспрашивания
            # для этого CLI-пути нет -- интерактив для Паспорта уже есть отдельно, [4] в
            # run_bare_launch().
            check_bundled_tools(log=print)
            report_path = _bare_launch_run_passport(args.target, log=console_log)
            return EXIT_CONFIG_ERROR if report_path is None else 0
        # else has_source: обычная диагностика источника, падает в общий поток ниже.

    sources = resolve_sources(args)
    target = args.target

    if len(sources) > 1 and any(s.strip().lower() == "all" for s in sources):
        print("ОШИБКА: нельзя сочетать SOURCE=all с другими --source/--source-list. "
              "Укажите либо один SOURCE=all, либо список конкретных путей.")
        return EXIT_CONFIG_ERROR

    # ТЗ-меню 2026-07-10, раздел 9а + развилка 1 раздела 11: ЕДИНЫЙ признак "интерактивный
    # режим" -- определяется один раз и переиспользуется для (а) решения, что именно
    # доспросить подменю, (б) паузы "Нажмите Enter для выхода" в конце, (в) подтверждения-
    # «да» перед archive (_confirm_build_summary). Полный CLI (все нужные пути заданы явно)
    # -- ни меню, ни паузы, ни подтверждения; автоматизация/скрипты не должны спотыкаться.
    #
    # 2026-08-04, живой вопрос пользователя: "analyze" (в отличие от "archive") не читает
    # cfg.target вообще при диагностике источника (read-only, см. run_analyze()/
    # _NO_TARGET_PLACEHOLDER ниже -- та же причина, по которой [1] меню его даже не
    # спрашивает) -- если мы уже здесь с mode=="analyze", target гарантированно None (ветка
    # has_target выше уже вернула управление раньше). Требовать --target только ради того,
    # чтобы не провалиться в интерактивный вопрос "Куда сложить архив?" (бессмысленный для
    # диагностики источника), было чистой шероховатостью CLI, не реальной потребностью.
    interactive_mode = not sources or (not target and args.mode != "analyze")
    if interactive_mode:
        # Частичный CLI: доспрашиваем ТОЛЬКО то, что не задано флагами (не весь набор
        # вопросов бare-launch меню -- режим уже известен из явной подкоманды/флагов).
        _ensure_config_yaml_exists(CONFIG_YAML_PATH, log=console_log)
        if not sources:
            sources = [prompt_source_submenu()]
        if not target:
            target = prompt_target_submenu(sources)
    if not target and args.mode == "analyze":
        target = _NO_TARGET_PLACEHOLDER

    # Голая буква диска без слеша ("C:") -- удобный короткий ввод ("источник C:, архив D:"),
    # но неоднозначный сам по себе в терминах Windows (p.5.9 отклонил бы его как "не полный
    # путь") -- нормализуем в однозначный корень ("C:\") ОДИНАКОВО для CLI-флагов и
    # интерактивного ввода, см. _normalize_bare_drive_letter().
    sources = [_normalize_bare_drive_letter(s) for s in sources]
    target = _normalize_bare_drive_letter(target)

    # Голый корень диска как TARGET -- либо вынужденное разрешение конфликта с одним из
    # SOURCE (без вопроса, одинаково для CLI/photoarchive_config.yaml и интерактива), либо, если конфликта
    # нет, настоящий выбор (создать PhotoArchive\ или писать прямо в корень) -- только в
    # интерактиве. См. resolve_drive_root_conflict().
    if args.mode == "archive":
        target = resolve_drive_root_conflict(sources, target, interactive=interactive_mode)

    # ТЗ-меню 2026-07-10, разделы 6/9 (развилка 4 раздела 11): единое подтверждение перед
    # реальной записью -- только интерактивный путь (полный CLI никогда его не показывает,
    # раздел 9а) и только archive (analyze-* ничего не пишет в TARGET, спрашивать не о чем).
    # _confirm_build_summary() уже включает в себя случай "TARGET похож на чужую папку"
    # (бывший confirm_target_interactively()) -- одним вопросом, не двумя подряд.
    if interactive_mode and args.mode == "archive" and not _confirm_build_summary(sources, target):
        _pause_before_exit(interactive_mode)
        return 0

    check_bundled_tools(log=print)

    expanded = expand_sources(sources, target)

    # ROADMAP.md "Коды возврата... не отражают неудачу": один exit code на весь прогон, даже
    # с несколькими SOURCE (--source all/повторяемый --source). InsufficientSpace всегда
    # выигрывает у любого другого кода, увиденного раньше в этом же цикле -- TARGET физически
    # некуда писать дальше, продолжать бессмысленно, это самая весомая причина остановиться.
    # TargetLocked/ошибка конфигурации на практике одинаковы для всех источников одного
    # прогона (один и тот же TARGET/photoarchive_config.yaml) -- порядок между ними самими не
    # важен, только чтобы 0 никогда не перекрывал уже увиденную ошибку.
    exit_code = 0
    any_succeeded = False
    total_processed = 0
    any_interrupted = False  # Ctrl+C-пакет: тем же приёмом, что и _bare_launch_run_build()
    # PROMPT_report_run_redesign.md, Фаза 0 (2026-08-14): в отличие от _bare_launch_run_build()/
    # _bare_launch_run_dryrun() (см. any_stopped_for_space там), этот CLI-путь раньше НЕ собирал
    # RunResult.stopped_for_space по источникам вообще -- merged["stopped_for_space"] никогда не
    # выставлялся, run_stats.get("stopped_for_space") в report.py всегда читал False, даже когда
    # сборка реально останавливалась по нехватке места (EXIT_INSUFFICIENT_SPACE в exit_code при
    # этом выставлялся корректно -- расхождение было именно в человекочитаемом отчёте).
    any_stopped_for_space = False
    run_start = time.strftime("%Y-%m-%d %H:%M:%S")  # см. _bare_launch_run_build() -- та же
                                                      # граница для report._split_rows_by_time()
    results = []  # RunResult.stats по успешным SOURCE (archive-режим) -- "Этот прогон" в
                  # отчёте, тот же принцип, что и в _bare_launch_run_build()/
                  # _bare_launch_run_dryrun() (_sum_stats() ниже).
    # Речь пользователя, 2026-08-18: CLI --dry-run раньше писал настоящие CSV/archive_cache.db/
    # ensure_target_layout() (Albums/ByDate/RAW/_Unsorted, __служебные_файлы) прямо в TARGET,
    # оставляя пустой скелет архива на диске после завершения -- suppress_logs там всегда было
    # False (см. build_arg_parser()/старый докстринг _finalize_target_report()). Теперь
    # suppress_logs=args.dry_run -- тот же механизм, что уже безопасно использует интерактивный
    # [2] (_bare_launch_run_dryrun()): _run_impl() собирает строки в памяти
    # (CollectingRunLogs), ensure_target_layout()/check_rules_version()/archive_cache-соединение/
    # TargetLock пропускаются целиком (все уже гейтятся `not cfg.suppress_logs`, см. run()) --
    # TARGET вообще не трогается физически. merged_rows ниже -- тот же приём слияния с уже
    # существующей историей TARGET, что и там (см. combined_rows перед вызовом
    # _finalize_target_report()).
    merged_rows = {name: [] for name in report.CSV_NAMES}
    shared_pool = None  # раунд 5 ревью, вариант A: не пересканировать TARGET на каждый SOURCE
                        # этого batch'а (archive-режим) -- см. _run_impl/run_for_source
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                print(f"\n########## SOURCE = {s} ##########")
            if args.mode == "archive":
                result = run_for_source(s, target, args.dry_run, args.sample_limit, log=console_log,
                                         suppress_logs=args.dry_run, shared_pool=shared_pool)
                source_exit_code = result.exit_code
                if not result.failed:
                    any_succeeded = True
                    total_processed += result.processed_count
                    shared_pool = result.pool
                    results.append(result.stats)
                    if args.dry_run:
                        for name, rows in (result.collected_rows or {}).items():
                            merged_rows.setdefault(name, []).extend(rows)
                    any_stopped_for_space = any_stopped_for_space or result.stopped_for_space
                if result.interrupted:
                    any_interrupted = True
                    break
            else:
                # args.mode здесь всегда "analyze" с has_source=True (target-ветка Паспорта
                # ушла в отдельный return выше, archive -- в ветку if) -- _CLI_ANALYZE_MODE_MAP
                # переводит CLI-имя в внутреннее значение mode, см. её же комментарий у CLI_MODES.
                internal_mode = _CLI_ANALYZE_MODE_MAP.get(args.mode, args.mode)
                stats = run_analyze_for_source(s, target, args.sample_limit, internal_mode, log=console_log)
                source_exit_code = EXIT_CONFIG_ERROR if stats is None else 0
                # PROMPT_archive_report.md, раздел 1.2: analyze-* -- "один слот, не
                # персистентно per-источник", каждый анализ перезаписывает WORKDIR\report.html
                # -- внутри цикла, не после (в отличие от archive ниже).
                report_path = _finalize_analyze_report(stats, open_browser=interactive_mode,
                                                        log=console_log, source_path=s)
                if stats is not None and stats.interrupted:
                    # Ctrl+C-пакет (2026-08-07, распространено с archive-ветки выше на CLI
                    # analyze --source): отчёт уже записан (баннер прерывания внутри) --
                    # архивная ветка ниже (any_interrupted) для analyze не подходит
                    # (_finalize_target_report()/_sum_stats() рассчитаны на TARGET-уровень,
                    # analyze пишет "один слот" в WORKDIR прямо здесь) -- заново возбуждаем
                    # KeyboardInterrupt сразу, тем же способом, что и она.
                    if report_path:
                        console_log(f"\n  Отчёт (данные на момент остановки): "
                                     f"{_display_path(report_path)}")
                    raise _InterruptedRunReport(report_path)
            if source_exit_code == EXIT_INSUFFICIENT_SPACE:
                exit_code = EXIT_INSUFFICIENT_SPACE
            elif source_exit_code and not exit_code:
                exit_code = source_exit_code

    # combined_rows -- тот же приём слияния in-memory строк ЭТОГО прогона с уже существующей
    # историей TARGET, что и в _bare_launch_run_dryrun() (см. её же комментарий у target_logs_dir)
    # -- ничего не читает с диска, если args.dry_run=False (тогда _finalize_target_report()
    # получает data=None и читает настоящие CSV, как раньше для реальной сборки).
    combined_rows = None
    if args.mode == "archive" and args.dry_run:
        target_logs_dir = os.path.join(target, "__служебные_файлы", "logs")
        combined_rows = merged_rows
        if os.path.isdir(target_logs_dir):
            target_data = report.parse_target_logs(target_logs_dir)
            if any(target_data.get(name) for name in report.CSV_NAMES):
                combined_rows = {name: list(target_data.get(name, [])) for name in report.CSV_NAMES}
                for name, rows in merged_rows.items():
                    combined_rows.setdefault(name, []).extend(rows)

    if any_interrupted:
        # Ctrl+C-пакет: тот же приём, что и _bare_launch_run_build() -- отчёт формируется
        # здесь, затем KeyboardInterrupt возбуждается заново для main() (сообщение "Прервано
        # пользователем.", sys.exit(130), как и раньше).
        level = "workdir" if args.dry_run else "target"
        merged = _sum_stats(results)
        merged["stopped_for_space"] = any_stopped_for_space
        report_path = _finalize_target_report(target, level, any_succeeded, total_processed,
                                               open_browser=interactive_mode, log=console_log,
                                               run_stats=merged, run_start=run_start,
                                               interrupted=True, source_paths=expanded,
                                               data=combined_rows)
        if report_path:
            console_log(f"\n  Отчёт (данные на момент остановки): {_display_path(report_path)}")
        raise KeyboardInterrupt

    report_path = None
    if args.mode == "archive":
        level = "workdir" if args.dry_run else "target"
        merged = _sum_stats(results)
        merged["stopped_for_space"] = any_stopped_for_space
        report_path = _finalize_target_report(target, level, any_succeeded, total_processed,
                                               open_browser=interactive_mode, log=console_log,
                                               run_stats=merged, run_start=run_start,
                                               source_paths=expanded, data=combined_rows)

    _pause_before_exit(interactive_mode, report_path=report_path)
    return exit_code


if __name__ == "__main__":
    # Раунд 142 ревью [БЛОКЕР], живая находка: этот файл исполняется как `__main__`
    # (sys.modules["__main__"]) -- но gui_menu.py делает `import photosort_win as m` на своей
    # первой строке, а `sys.modules` не считает "__main__" и "photosort_win" одним и тем же
    # модулем по ключу словаря, поэтому этот `import` заново ИСПОЛНЯЕТ весь файл с нуля,
    # порождая ВТОРОЙ, независимый экземпляр модуля со своими копиями ВСЕХ module-level
    # глобалов (_work_console_allocated, _console_freed_for_gui и т.д.), не совпадающими по
    # identity с копией "__main__". Тот же класс бага, что сессия уже нашла и точечно обошла
    # для _ACTIVE_BARS (см. её докстринг в log_line()) -- здесь исправляется в корне: явно
    # регистрируем ЭТОТ ЖЕ объект модуля под его настоящим именем ДО того, как что-либо (в т.ч.
    # gui_menu.py, импортируемый глубоко внутри main()/_main()) успеет импортировать его заново
    # -- `import photosort_win` дальше находит уже существующую запись в sys.modules и
    # переиспользует ровно этот экземпляр, вторая копия больше не создаётся. setdefault(), не
    # безусловное присваивание -- если модуль КОГДА-ЛИБО уже был по-настоящему импортирован под
    # этим именем раньше этой точки (на практике не происходит -- ci/windows_ci_test.py и
    # тесты запускают файл только через `python -m`/`pytest`, никогда оба пути в одном
    # процессе), не перетирает существующую запись.
    sys.modules.setdefault("photosort_win", sys.modules["__main__"])

    # Must run before anything else: some bundled dependency (reverse_geocoder) spawns
    # multiprocessing workers, and under a frozen PyInstaller exe each spawned worker
    # re-execs this very exe -- without freeze_support() it lands back in argparse with
    # multiprocessing's internal bootstrap args ("--multiprocessing-fork ...") and errors
    # out instead of running the worker payload. No-op on non-frozen/non-spawned runs.
    multiprocessing.freeze_support()

    # Console output is Russian throughout (log messages, RULES.md terminology). Windows'
    # default console codepage depends on system locale (e.g. cp1252 on an English-locale
    # install, cp866/1251 on a Russian one) and is NOT guaranteed to be able to encode
    # Cyrillic -- without this, the very first log() call crashes the whole run with
    # UnicodeEncodeError before any file is even touched. Force UTF-8 with a replace
    # fallback so a mismatched console codepage degrades to mojibake, not a crash. Log
    # files themselves are opened with explicit encoding="utf-8" elsewhere and are
    # unaffected either way.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
