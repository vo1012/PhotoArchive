# -*- coding: utf-8 -*-
"""Генерирует faq.html/quickstart.html/letter.html из FAQ.md/QUICKSTART.md/
PhotoArchive_ot_avtora.md публичного репозитория -- переиспользует markdown-конвертацию
build/md_to_pdf.py (markdown.markdown(...)), но без шага печати в PDF, обёрнуто в стили
сайта. См. SITE_DESIGN_PROMPT.md, §7."""
import html
import os
import re
import sys

import markdown

DOCS = [
    ("FAQ.md", "faq.html", "Частые вопросы", "FAQ.pdf"),
    ("QUICKSTART.md", "quickstart.html", "Быстрый старт", "QUICKSTART.pdf"),
    ("PhotoArchive_ot_avtora.md", "letter.html", "Письмо автора", "PhotoArchive_ot_avtora.pdf"),
]

DOC_TO_HTML = {src: html_name for src, html_name, _, _ in DOCS}

_CI_BADGE_RE = re.compile(r"^\[!\[CI\]\([^)]+\)\]\([^)]+\)\n\n?", re.MULTILINE)
_LINK_RE = re.compile(r"\]\(\./([A-Za-z0-9_]+\.md)(#[^)]*)?\)")


def strip_ci_badge(text):
    return _CI_BADGE_RE.sub("", text)


_CONTACTS_PHRASE_RE = re.compile(r"разд\w* «Контакты» на сайте проекта")


def link_contacts_phrase(text):
    def repl(m):
        return f"[{m.group(0)}](./index.html#contacts)"

    return _CONTACTS_PHRASE_RE.sub(repl, text)


def rewrite_links(text):
    def repl(m):
        name = m.group(1)
        anchor = m.group(2) or ""
        if name in DOC_TO_HTML:
            return f"](./{DOC_TO_HTML[name]}{anchor})"
        if name == "README.md":
            return f"](https://github.com/vo1012/PhotoArchive/blob/main/README.md{anchor})"
        return m.group(0)

    return _LINK_RE.sub(repl, text)


PAGE_CSS = """
:root{
  --bg:#F0F2EC; --paper:#F7F8F4; --ink:#20261F; --muted:#4E574A;
  --accent:#24544A; --accent2:#A85A2A; --line:#B9C2B2; --radius:12px; --prose:42em;
}
*,*::before,*::after{ box-sizing:border-box; }
html{ font-size:18px; }
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  line-height:1.6; -webkit-text-size-adjust:100%;
}
a{ color:var(--accent); text-decoration-thickness:1.5px; text-underline-offset:3px; }
a:hover{ color:var(--accent2); }
:focus-visible{ outline:3px solid var(--accent2); outline-offset:3px; border-radius:4px; }
.topbar{
  background:var(--paper); border-bottom:1px solid var(--line);
  padding:18px 24px; margin-bottom:8px;
}
.topbar a.brand{ font-weight:800; font-size:1.15rem; color:var(--ink); text-decoration:none; }
.page-head{ max-width:var(--prose); margin:40px auto 0; padding:0 24px; }
.page-head .eyebrow{ display:block; color:var(--accent); font-weight:600; margin-bottom:0.4em; }
.page-head h1{ font-size:clamp(1.7rem,4vw,2.4rem); margin:0 0 0.3em; }
.pdf-link{
  display:inline-flex; align-items:center; gap:8px; margin:0.6em 0 0;
  font-size:0.95rem; color:var(--muted);
}
main.doc{
  max-width:var(--prose); margin:0 auto; padding:24px 24px 80px;
}
main.doc h1{ font-size:1.7rem; border-bottom:2px solid var(--line); padding-bottom:0.3em; }
main.doc h2{ font-size:1.35rem; border-bottom:1px solid var(--line); padding-bottom:0.25em; margin-top:1.6em; }
main.doc h3{ font-size:1.1rem; margin-top:1.3em; }
main.doc code{
  background:var(--paper); padding:0.1em 0.35em; border-radius:4px;
  font-family:Consolas,"SFMono-Regular",monospace; font-size:0.9em;
}
main.doc pre{ background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:1em; overflow-x:auto; }
main.doc pre code{ background:none; padding:0; }
main.doc table{ border-collapse:collapse; width:100%; margin:1em 0; }
main.doc th, main.doc td{ border:1px solid var(--line); padding:0.5em 0.7em; text-align:left; }
main.doc th{ background:var(--paper); }
main.doc blockquote{ border-left:3px solid var(--accent2); margin:1em 0; padding:0.2em 1.2em; color:var(--muted); }
main.doc sub{ font-size:80%; color:var(--muted); }
main.doc img{ max-width:100%; }
.back-link{ max-width:var(--prose); margin:0 auto; padding:0 24px 40px; }
"""


def build_page(root, src_name, out_name, title, pdf_name):
    src_path = os.path.join(root, src_name)
    with open(src_path, encoding="utf-8") as f:
        text = f.read()
    text = strip_ci_badge(text)
    text = rewrite_links(text)
    text = link_contacts_phrase(text)
    body = markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists", "toc"],
        extension_configs={"toc": {"permalink": False}},
    )
    doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — PhotoArchive</title>
<meta name="description" content="{html.escape(title)} — PhotoArchive, программа для Windows, которая никогда не изменяет исходные фотографии и видео.">
<meta name="robots" content="noindex">
<style>{PAGE_CSS}</style>
</head>
<body>
<header class="topbar"><a class="brand" href="./index.html">← PhotoArchive</a></header>
<div class="page-head">
  <span class="eyebrow">Документация</span>
  <h1>{html.escape(title)}</h1>
  <p class="pdf-link">Также доступно как <a href="./{pdf_name}">PDF-версия для печати или пересылки →</a></p>
</div>
<main class="doc">
{body}
</main>
<p class="back-link"><a href="./index.html">← Вернуться на главную страницу PhotoArchive</a></p>
</body>
</html>
"""
    out_path = os.path.join(root, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"  {src_name} -> {out_name} ({os.path.getsize(out_path)} bytes)")


def main():
    if len(sys.argv) != 2:
        sys.exit("Использование: python gen_doc_pages.py <корень репозитория (=папка вывода)>")
    root = sys.argv[1]
    for src, out, title, pdf_name in DOCS:
        build_page(root, src, out, title, pdf_name)


if __name__ == "__main__":
    main()
