#!/usr/bin/env python3
"""Собрать базу знаний в один markdown и в PDF.

Документы лежат россыпью — `docs/product_audit/` и `docs/go_to_market/`, — и
это правильно для работы: каждый правится отдельно, история видна по файлам.
Но переслать сорок четыре файла нельзя, а именно это и требуется чаще всего:
отдать подрядчику, положить в языковую модель, прочитать с телефона.

Скрипт, а не разовая сборка руками: документы правятся, и собранное устареет
на первой же правке. Пересобрать надо уметь одной командой.

    python3 scripts/build_book.py            # только markdown
    python3 scripts/build_book.py --pdf      # и PDF

PDF печатает Chromium. Шрифты в него **вшиты** файлом `docs/assets/fonts.css`:
страница печати сети не видит, и подключённые ссылкой шрифты молча заменились
бы на системные — а системного шрифта с кириллицей в контейнере может не быть
вовсе.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "build"
FONTS = ROOT / "docs" / "assets" / "fonts.css"
NAME = "Юмаркет-Менеджер-база-знаний"

# Markdown кладётся в `docs/` и КОММИТИТСЯ — его скачивают с github.com, в
# том числе с телефона, где склеивать сорок четыре файла нечем. PDF и HTML
# остаются в `build/`: они тяжёлые (семь мегабайт против восьмисот
# килобайт) и выводятся из этого же файла.
#
# Имя латиницей не из вредности: в адресе github.com кириллица уезжает
# процентными кодами, и ссылку становится нечем передать голосом или
# записать от руки. Название на русском стоит внутри, первой строкой.
BOOK = ROOT / "docs" / "KNOWLEDGE_BASE.md"

CHROME = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
]

PARTS = [
    ("Часть I. Продуктовый аудит",
     "Что продукт делает на самом деле. Составлено по исходному коду: "
     "прослежена цепочка «кнопка или команда → обработчик → бизнес-логика → "
     "результат». У каждого существенного утверждения стоит доказательство — "
     "путь к файлу и имя функции.",
     "docs/product_audit"),
    ("Часть II. Система выхода на рынок",
     "Как об этом рассказывать. Позиционирование, сайт, контент, воронка, "
     "активация, удержание. Ни одно обещание здесь не выходит за рамки того, "
     "что доказано в части I.",
     "docs/go_to_market"),
]


def ordered(folder: pathlib.Path) -> list[pathlib.Path]:
    """README первым, дальше по номеру в имени."""
    files = sorted(folder.glob("*.md"))
    readme = [f for f in files if f.name == "README.md"]
    rest = sorted((f for f in files if f.name != "README.md"),
                  key=lambda f: int(f.name[:2]))
    return readme + rest


def demote(text: str) -> str:
    """Каждый заголовок на уровень ниже — освободить `#` под части.

    Внутри ``` блоков решётка означает комментарий, а не заголовок: строка
    `# комментарий` в примере кода превратилась бы в раздел документа.
    """
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and re.match(r"^#{1,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def build_markdown() -> str:
    body: list[str] = []
    toc: list[str] = []
    counts: list[int] = []

    for name, note, rel in PARTS:
        folder = ROOT / rel
        if not folder.is_dir():
            sys.exit(f"нет каталога {rel}")
        files = ordered(folder)
        counts.append(len(files))
        toc.append(f"\n### {name}\n")
        body.append(f'\n\n<div class="part-break"></div>\n\n# {name}\n\n{note}\n')
        for f in files:
            raw = f.read_text(encoding="utf-8")
            title = title_of(raw, f.stem)
            toc.append(f"* {title}")
            body.append(f'\n\n<div class="doc-break"></div>\n\n## {title}\n')
            # свой H1 уже вставлен выше — второй раз он не нужен
            rest = raw.split("\n", 1)[1] if raw.startswith("# ") else raw
            body.append(demote(rest).lstrip("\n"))

    today = datetime.date.today().strftime("%d.%m.%Y")
    head = f"""# Юмаркет Менеджер

## Полная база знаний: продукт и система продаж

Документ собран из двух наборов, лежащих в репозитории продукта:
`docs/product_audit/` и `docs/go_to_market/`. Первый отвечает на вопрос
**что продукт делает**, второй — **как об этом рассказывать**.

**Правило, связывающее обе части.** Ни одно маркетинговое утверждение не
выходит за рамки того, что доказано кодом. Формулировка допустима, если её
можно закончить фразой «потому что в коде есть…» и назвать файл. Нельзя —
она идёт в запрещённые.

**Про PlayerOK.** Интеграции с ним в продукте нет: ни API-вызовов, ни учётных
данных, ни упоминаний в коде. Продукт работает с маркетплейсом **Юмаркет**;
название площадки не является названием продукта.

**Собрано {today}** из {counts[0]} документов аудита и {counts[1]} документов
системы продаж.

---

## Содержание
"""
    return head + "\n".join(toc) + "\n" + "".join(body) + "\n"


CSS = """
:root{--ink:#16181d;--muted:#5b6270;--faint:#8b93a3;--rule:#dfe3ea;
--rule-soft:#eceff4;--accent:#1f4ed8;--code-bg:#f6f7f9;}
*{box-sizing:border-box}
@page{size:A4;margin:17mm 15mm 16mm 15mm;}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
body{font-family:'Golos Text',system-ui,sans-serif;font-size:9.6pt;
line-height:1.52;color:var(--ink);margin:0;hyphens:auto;-webkit-hyphens:auto;}
.cover{height:262mm;display:flex;flex-direction:column;justify-content:center;
page-break-after:always;}
.cover .kicker{font-family:'JetBrains Mono',monospace;font-size:8pt;
letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin-bottom:14mm;}
.cover h1{font-family:'Unbounded',sans-serif;font-size:34pt;line-height:1.04;
font-weight:700;margin:0 0 6mm;letter-spacing:-.02em;border:0;padding:0;}
.cover .sub{font-size:13pt;color:var(--muted);line-height:1.42;max-width:120mm;margin:0 0 16mm;}
.cover .meta{border-top:1.5px solid var(--ink);padding-top:5mm;
font-family:'JetBrains Mono',monospace;font-size:8.2pt;color:var(--muted);
display:grid;grid-template-columns:1fr 1fr;gap:3mm 8mm;max-width:140mm;}
.cover .meta b{color:var(--ink);font-weight:500;}
.part-break,.doc-break{page-break-before:always;height:0;}
h1,h2,h3,h4,h5{font-family:'Unbounded',sans-serif;font-weight:600;line-height:1.18;
letter-spacing:-.012em;break-after:avoid;page-break-after:avoid;}
h1{font-size:23pt;margin:0 0 7mm;padding-bottom:4mm;border-bottom:2.5px solid var(--ink);}
h2{font-size:15.5pt;margin:0 0 5mm;padding-bottom:2.5mm;border-bottom:1px solid var(--rule);}
h3{font-size:11.4pt;margin:7mm 0 2.5mm;}
h4{font-size:9.9pt;margin:5mm 0 2mm;color:var(--muted);font-family:'Golos Text',sans-serif;
font-weight:600;text-transform:uppercase;letter-spacing:.055em;}
h5{font-size:9.4pt;margin:4mm 0 1.5mm;color:var(--muted);}
.part-break + h1{font-size:27pt;margin-top:8mm;}
p{margin:0 0 3.2mm;orphans:2;widows:2;}
strong{font-weight:600;} em{color:var(--muted);}
ul,ol{margin:0 0 3.4mm;padding-left:5.2mm;} li{margin-bottom:1.3mm;}
li>ul,li>ol{margin-top:1.3mm;}
code{font-family:'JetBrains Mono',monospace;font-size:8.3pt;background:var(--code-bg);
padding:.6mm 1.1mm;border-radius:2px;color:#0f172a;word-break:break-word;}
pre{background:var(--code-bg);border:1px solid var(--rule-soft);
border-left:2.5px solid var(--accent);border-radius:3px;padding:3mm 3.5mm;
margin:0 0 4mm;overflow:hidden;page-break-inside:avoid;}
pre code{background:none;padding:0;font-size:7.9pt;line-height:1.45;
white-space:pre-wrap;word-break:break-word;}
table{width:100%;border-collapse:collapse;margin:0 0 4.5mm;font-size:8.4pt;}
thead{display:table-header-group;} tr{page-break-inside:avoid;}
th{text-align:left;font-weight:600;background:#f4f6f9;border-bottom:1.5px solid var(--ink);
padding:1.8mm 2.2mm;line-height:1.35;}
td{border-bottom:1px solid var(--rule-soft);padding:1.7mm 2.2mm;vertical-align:top;
line-height:1.4;word-break:break-word;}
td code,th code{font-size:7.6pt;}
blockquote{margin:0 0 4mm;padding:2.5mm 0 2.5mm 4mm;border-left:2.5px solid var(--accent);
font-size:10.4pt;line-height:1.45;page-break-inside:avoid;}
blockquote p{margin:0 0 2mm;} blockquote p:last-child{margin:0;}
hr{border:0;border-top:1px solid var(--rule);margin:6mm 0;}
a{color:var(--accent);text-decoration:none;}
.toc-wrap ul{list-style:none;padding-left:0;column-count:2;column-gap:8mm;}
.toc-wrap li{font-size:8.6pt;margin-bottom:1.4mm;break-inside:avoid;color:var(--muted);}
.toc-wrap h3{font-size:10pt;margin:4mm 0 2mm;column-span:all;color:var(--ink);}
.toc-wrap h3:first-child{margin-top:0;}
"""


def build_html(md_text: str) -> str:
    try:
        import markdown
    except ImportError:
        sys.exit("нужен python-markdown: pip install markdown")
    if not FONTS.exists():
        sys.exit(f"нет {FONTS.relative_to(ROOT)} — без него в PDF не будет кириллицы")

    body = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"]
    ).convert(md_text)

    # оглавление — в две колонки, до первой части
    body = body.replace('<h2>Содержание</h2>',
                        '<h2>Содержание</h2>\n<div class="toc-wrap">', 1)
    body = body.replace('<div class="part-break"></div>',
                        '</div>\n<div class="part-break"></div>', 1)

    today = datetime.date.today().strftime("%d.%m.%Y")
    n_audit = len(ordered(ROOT / "docs" / "product_audit"))
    n_gtm = len(ordered(ROOT / "docs" / "go_to_market"))
    cover = f"""<div class="cover">
  <div class="kicker">База знаний · продукт и система продаж</div>
  <h1>Юмаркет&nbsp;Менеджер</h1>
  <div class="sub">Что продукт делает на самом деле — и как об этом
  рассказывать, не обещая того, чего он не умеет.</div>
  <div class="meta">
    <div><b>Часть I</b> · Продуктовый аудит, {n_audit} документов</div>
    <div><b>Часть II</b> · Система выхода на рынок, {n_gtm} документов</div>
    <div><b>Источник</b> · исходный код продукта</div>
    <div><b>Собрано</b> · {today}</div>
  </div>
</div>"""

    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<title>Юмаркет Менеджер — база знаний</title>'
            f'<style>{FONTS.read_text(encoding="utf-8")}</style>'
            f'<style>{CSS}</style></head><body>{cover}{body}</body></html>')


def find_chrome() -> str:
    for path in CHROME:
        if pathlib.Path(path).exists():
            return path
    sys.exit("Chromium не найден — PDF собрать нечем. Markdown уже готов.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", action="store_true", help="собрать ещё и PDF")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    md_text = build_markdown()
    BOOK.write_text(md_text, encoding="utf-8")
    print(f"✅ {BOOK.relative_to(ROOT)} — "
          f"{md_text.count(chr(10)):,} строк, {len(md_text):,} знаков"
          .replace(",", " "))

    if not args.pdf:
        return

    html_path = OUT / f"{NAME}.html"
    html_path.write_text(build_html(md_text), encoding="utf-8")
    pdf_path = OUT / f"{NAME}.pdf"
    subprocess.run([
        find_chrome(), "--headless", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=60000",
        f"--print-to-pdf={pdf_path}", f"file://{html_path}",
    ], check=True, capture_output=True)

    # «Chromium отработал» — не доказательство: файла может не быть.
    if not pdf_path.exists():
        sys.exit("Chromium вышел без ошибки, а PDF не появился")
    data = pdf_path.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    if not pages:
        sys.exit("PDF собран, но в нём ноль страниц")
    print(f"✅ {pdf_path.relative_to(ROOT)} — {pages} страниц, "
          f"{len(data) / 1024 / 1024:.1f} МБ")


if __name__ == "__main__":
    main()
