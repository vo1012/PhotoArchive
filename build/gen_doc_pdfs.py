# -*- coding: utf-8 -*-
"""Генерирует FAQ.pdf/QUICKSTART.pdf/PhotoArchive_ot_avtora.pdf отдельно (не только внутри
PhotoArchive.zip) для публикации прямо на сайте. Переиспользует большую часть
build/md_to_pdf.py (CSS, поиск Edge, печать в PDF, релативизация ссылок) как библиотеку, но
со своим правилом переписывания ссылок: README.md (не входит в набор из трёх, публикуемых
отдельно) должен вести на github.com, а не на несуществующий README.pdf рядом. См.
SITE_DESIGN_PROMPT.md §7."""
import html
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, r"C:\PhotoArchive\build")
import md_to_pdf  # noqa: E402

ROOT = r"C:\PhotoArchive"
DOCS = ["QUICKSTART.md", "FAQ.md", "PhotoArchive_ot_avtora.md"]

_LINK_RE = re.compile(r"\]\(\./([A-Za-z0-9_]+\.md)(#[^)]*)?\)")


def rewrite_links(text):
    def repl(m):
        name = m.group(1)
        anchor = m.group(2) or ""
        if name in DOCS:
            return f"](./{name[:-3]}.pdf)"
        if name == "README.md":
            return f"](https://github.com/vo1012/PhotoArchive/blob/main/README.md{anchor})"
        return m.group(0)

    return _LINK_RE.sub(repl, text)


_FENCE_RE = re.compile(r"(```.*?```)", re.DOTALL)


def rewrite_plain_mentions(text):
    """md_to_pdf._rewrite_plain_mentions rewrites README.md too (it's in the full 6-doc
    DOCS list there) -- wrong here, since rewrite_links() above deliberately sends README.md
    to a github.com URL, not a local README.pdf that doesn't exist next to these three files.
    Applying the upstream function afterwards would mangle that URL's own "README.md"
    substring into "README.pdf" (a dead link) as well as any bare `README.md` mentions in
    prose. Own restricted version: only the three docs actually published as PDFs here."""
    names_by_len = sorted(DOCS, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(n) for n in names_by_len))

    def repl(m):
        return m.group(0)[:-3] + ".pdf"

    parts = _FENCE_RE.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(repl, parts[i])
    return "".join(parts)


def convert_one(src_path, out_dir, edge, known_names):
    with open(src_path, encoding="utf-8") as f:
        text = md_to_pdf._strip_ci_badge(rewrite_plain_mentions(rewrite_links(f.read())))
    body = md_to_pdf.markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    title = os.path.splitext(os.path.basename(src_path))[0]
    doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
           f'<title>{html.escape(title)}</title><style>{md_to_pdf.CSS}</style></head>'
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
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)

    size = os.path.getsize(pdf_path)
    if size < 2000:
        sys.exit(f"[ERROR] {pdf_path} подозрительно маленький ({size} байт)")
    md_to_pdf._relativize_links(pdf_path, known_names)
    return pdf_path


def main():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    edge = md_to_pdf._find_edge()
    known_names = {d[:-3] + ".pdf" for d in DOCS}
    for doc in DOCS:
        src = os.path.join(ROOT, doc)
        pdf_path = convert_one(src, ROOT, edge, known_names)
        print(f"  {doc} -> {os.path.relpath(pdf_path, ROOT)} ({os.path.getsize(pdf_path)} bytes)")


if __name__ == "__main__":
    main()
