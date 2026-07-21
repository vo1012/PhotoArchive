"""Конвертирует пользовательскую markdown-документацию в PDF для комплекта dist\\PhotoArchive\\,
который CI зашивает в ZIP-ассет релиза (см. .github/workflows/ci.yml, job build) -- пользователь
скачивает его с сайта проекта как самодостаточный набор для распространения (флешка, оффлайн),
альтернативу голому `.exe`-ассету. Причина конвертации та же, что и у одноимённого скрипта в
dev-репозитории (портирован оттуда 2026-07-21, см. RELEASING.md): получатель ZIP чаще всего
откроет `.md` голым Блокнотом -- markdown-разметка (и raw-HTML вроде `<sub>`/`<table width=N>`,
уже использованного в README.md/PhotoArchive_ot_avtora.md) не рендерится. `.pdf` открывается
штатной программой Windows (Edge) без установки чего-либо.

Конвейер: .md -> HTML (python-markdown, raw HTML сохраняется как есть) -> PDF (headless
Edge, --print-to-pdf) -> пост-обработка ссылок (pypdf). Только для этой цели, не часть самой
программы -- вызывается из build.bat, требует `pip install markdown pypdf` (requirements.txt)
и установленный Edge на машине, где собирается релиз (GitHub-hosted windows-latest runner несёт
Edge предустановленным -- НЕ нужен на машине конечного пользователя).

Перекрёстные ссылки между документами (`](./FAQ.md#якорь)`) переписываются на `.pdf` БЕЗ
якоря (`](./FAQ.pdf)`) -- открытие конкретного заголовка в чужом PDF ненадёжно зависит от
того, какой читалкой откроют файл (Edge/Acrobat/SumatraPDF и т.д.), в отличие от самого факта
открытия правильного файла. Абсолютные `file:///...`-ссылки, оставшиеся после печати PDF на
СБОРОЧНОЙ машине (CI-раннер), пост-обрабатываются через pypdf на голые имена файлов-соседей --
подробное обоснование см. в dev-репозитории, build/md_to_pdf.py (идентичный механизм, скопирован
дословно, комментарии там не дублируются здесь ради краткости).

Отличие от dev-репозитория (сознательное, не путать с расхождением по невнимательности):
- `DOCS` не включает `CHANGELOG.md` -- этого файла в публичном репозитории нет (внутренняя
  история разработки, находки аудитов и т.п., см. dev-репозиторий, RELEASING.md/CHANGELOG.md).
- `_rewrite_identity_links`/`_inject_donation_details` в публичном репозитории тоже нужны и
  тоже переписывают ссылку-визитку/донат-клаузу с github.com на сайт проекта -- PDF в ZIP
  собирается для оффлайн-аудитории точно так же, как и в dev-репозитории; сам факт, что ZIP
  скачивается С сайта, не отменяет того, что получатель может потом смотреть эти PDF уже без
  доступа к интернету. `DONATE.txt` в этом репозитории никогда не коммитится и не появляется на
  CI-раннере (см. .gitignore) -- автоматическая сборка всегда берёт ветку "нет DONATE.txt"."""

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import markdown
except ImportError:
    sys.exit("Нужен пакет 'markdown' (pip install -r requirements.txt), см. RELEASING.md")

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject
except ImportError:
    sys.exit("Нужен пакет 'pypdf' (pip install -r requirements.txt), см. RELEASING.md")

DOCS = ["README.md", "QUICKSTART.md", "FAQ.md", "PhotoArchive_ot_avtora.md",
        "THIRD_PARTY_LICENSES.md"]

OTHER_DIST_FILES = {"LICENSE", "NOTICE", "photoarchive_config.yaml.example",
                     "PhotoArchive_buklet.pdf"}

CSS = """
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 11pt; line-height: 1.5;
       color: #1a1a1a; max-width: 900px; margin: 2em auto; padding: 0 1em; }
h1, h2, h3 { color: #111; }
h1 { border-bottom: 2px solid #ccc; padding-bottom: .3em; }
h2 { border-bottom: 1px solid #ddd; padding-bottom: .2em; margin-top: 1.6em; }
code { background: #f2f2f2; padding: .1em .3em; border-radius: 3px; font-family: Consolas, monospace; }
pre code { display: block; padding: .8em; white-space: pre-wrap; word-break: break-word;
           font-size: 9.5pt; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #ccc; padding: .4em .6em; text-align: left; }
th { background: #f5f5f5; }
blockquote { border-left: 3px solid #ccc; margin: 1em 0; padding: .2em 1em; color: #555; }
a { color: #0645ad; }
sub { font-size: 80%; color: #444; }
"""

_LINK_RE = re.compile(r"\]\(\./([A-Za-z0-9_]+\.md)(#[^)]*)?\)")
_FENCE_RE = re.compile(r"(```.*?```)", re.DOTALL)
_CI_BADGE_RE = re.compile(r"^\[!\[CI\]\([^)]+\)\]\([^)]+\)\n\n?", re.MULTILINE)


def _strip_ci_badge(text: str) -> str:
    return _CI_BADGE_RE.sub("", text)


def _rewrite_links(text: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in DOCS:
            return f"](./{name[:-3]}.pdf)"
        return m.group(0)

    return _LINK_RE.sub(repl, text)


def _rewrite_plain_mentions(text: str) -> str:
    """Голые упоминания ".md" вне ссылок (текст/code-спаны) -- см. dev-репозиторий для
    полного обоснования. НЕ трогает ``` fenced code blocks ``` -- дословные примеры
    реального вывода программы не должны переписываться."""
    names_by_len = sorted(DOCS, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(n) for n in names_by_len))

    def repl(m: "re.Match[str]") -> str:
        return m.group(0)[:-3] + ".pdf"

    parts = _FENCE_RE.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(repl, parts[i])
    return "".join(parts)


_SITE_URL = "https://vo1012.github.io/PhotoArchive"

_IDENTITY_LINK_RE = re.compile(
    r"\[github\.com/vo1012/PhotoArchive\]\(https://github\.com/vo1012/PhotoArchive\) —\n"
    r"\s*репозиторий проекта: исходный код, обновления, вопросы и сообщения об ошибках\.")
_IDENTITY_LINK_REPL = f"[vo1012.github.io/PhotoArchive]({_SITE_URL}) — сайт проекта."

_AUTHOR_LETTER_IDENTITY_RE = re.compile(
    r"Исходный код, обновления и обратная связь — на GitHub:\n"
    r"\[github\.com/vo1012/PhotoArchive\]\(https://github\.com/vo1012/PhotoArchive\)\.")
_AUTHOR_LETTER_IDENTITY_REPL = f"Сайт проекта:\n[vo1012.github.io/PhotoArchive]({_SITE_URL})."


def _rewrite_identity_links(text: str) -> str:
    """Ссылка-визитка на проект (не Releases/не Issues -- те остаются на GitHub, там реально
    лежат .exe/.zip и баг-трекер) в PDF ведёт на сайт, не на github.com -- та же логика, что и
    в dev-репозитории (см. там за полным обоснованием)."""
    text = _IDENTITY_LINK_RE.sub(_IDENTITY_LINK_REPL, text)
    text = _AUTHOR_LETTER_IDENTITY_RE.sub(_AUTHOR_LETTER_IDENTITY_REPL, text)
    return text


_DONATE_INJECT_DOCS = {"PhotoArchive_ot_avtora.md", "FAQ.md"}
_GITHUB_DONATION_CLAUSE_RE = re.compile(
    r"актуальные\s+способы\s+сделать\s+это\s+указаны\s+на\s+странице\s+проекта\s+на\s+"
    r"GitHub:\s*github\.com/vo1012/PhotoArchive\.")
_SITE_DONATION_CLAUSE = f"актуальные способы сделать это указаны на сайте проекта:\n{_SITE_URL}."


def _inject_donation_details(text: str, src_path: str) -> str:
    """`DONATE.txt` никогда не коммитится (.gitignore) и не появляется на CI-раннере --
    автоматическая публичная сборка всегда идёт по ветке "нет DONATE.txt", заменяя
    GitHub-отсылку на сайт (та же причина, что и у _rewrite_identity_links выше)."""
    if os.path.basename(src_path) not in _DONATE_INJECT_DOCS:
        return text
    donate_path = os.path.join(os.path.dirname(src_path), "DONATE.txt")
    if not os.path.isfile(donate_path):
        return _GITHUB_DONATION_CLAUSE_RE.sub(_SITE_DONATION_CLAUSE, text)
    with open(donate_path, encoding="utf-8") as f:
        real_details = f.read().strip()
    return _GITHUB_DONATION_CLAUSE_RE.sub(real_details, text)


def _find_edge() -> str:
    candidates = [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    found = shutil.which("msedge")
    if found:
        return found
    sys.exit("msedge.exe не найден (проверены Program Files и PATH) -- нужен для "
              "конвертации HTML -> PDF, см. build/md_to_pdf.py")


def _relativize_links(pdf_path: str, known_names: set) -> None:
    """Заменяет /URI-действия вида "file:///абсолютный/путь/Имя.pdf" на "Имя.pdf" для всех
    ссылок на файлы из known_names -- см. dev-репозиторий, build/md_to_pdf.py за полным
    обоснованием (тот же механизм, скопирован дословно)."""
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    writer.append(reader)

    changed = False
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for a in annots:
            obj = a.get_object()
            action = obj.get("/A")
            if not action:
                continue
            uri = action.get("/URI")
            if not uri or not str(uri).startswith("file:///"):
                continue
            name = os.path.basename(str(uri))
            if name in known_names:
                obj[NameObject("/A")] = DictionaryObject({
                    NameObject("/Type"): NameObject("/Action"),
                    NameObject("/S"): NameObject("/URI"),
                    NameObject("/URI"): TextStringObject(name),
                })
                changed = True

    if changed:
        with open(pdf_path, "wb") as f:
            writer.write(f)


def convert_one(src_path: str, out_dir: str, edge: str, known_names: set) -> str:
    with open(src_path, encoding="utf-8") as f:
        text = _strip_ci_badge(_rewrite_plain_mentions(_rewrite_links(f.read())))
    text = _rewrite_identity_links(text)
    text = _inject_donation_details(text, src_path)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    title = os.path.splitext(os.path.basename(src_path))[0]
    doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
           f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
           f'<body>{body}</body></html>')

    html_path = os.path.join(os.path.abspath(out_dir), title + ".tmp.html")
    profile_dir = tempfile.mkdtemp(prefix="edge_pdf_profile_")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(doc)
        pdf_path = os.path.abspath(os.path.join(out_dir, title + ".pdf"))
        uri = "file:///" + html_path.replace("\\", "/")
        subprocess.run(
            [edge, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile_dir}",
             f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer", uri],
            check=True, timeout=60,
        )
    finally:
        os.unlink(html_path)
        shutil.rmtree(profile_dir, ignore_errors=True)

    size = os.path.getsize(pdf_path)
    if size < 2000:
        sys.exit(f"[ERROR] {pdf_path} подозрительно маленький ({size} байт) -- "
                  f"конвертация, вероятно, не сработала (проверьте вручную)")
    _relativize_links(pdf_path, known_names)
    return pdf_path


def main() -> None:
    # Живая находка 2026-07-21 (первый реальный прогон в CI публичного репозитория): скрипт
    # печатает кириллицу ("байт") -- на windows-latest-раннере (cp1252) это падает с
    # UnicodeEncodeError на первом же print(), тот же класс бага, что уже чинился для
    # photosort_win.py/ci/windows_ci_test.py (см. там), просто не был перенесён сюда при
    # портировании скрипта.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) != 3:
        sys.exit("Использование: python md_to_pdf.py <корень репозитория> <папка вывода>")
    root, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    edge = _find_edge()
    known_names = {d[:-3] + ".pdf" for d in DOCS} | OTHER_DIST_FILES
    for doc in DOCS:
        src = os.path.join(root, doc)
        pdf_path = convert_one(src, out_dir, edge, known_names)
        print(f"  {doc} -> {os.path.relpath(pdf_path, out_dir)} "
              f"({os.path.getsize(pdf_path)} байт)")


if __name__ == "__main__":
    main()
