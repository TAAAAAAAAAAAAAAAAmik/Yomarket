#!/usr/bin/env python3
"""Собрать страницу из docs/legal/*.md.

Юридический текст не переписывается руками: опечатка в нём — это не опечатка,
а другое условие договора. Разметка читается из файлов репозитория как есть.
"""
import html
import pathlib
import re

SRC = pathlib.Path("/home/user/Yomarket/docs/legal")
DOCS = [
    ("terms",   "Пользовательское соглашение", "terms.md"),
    ("offer",   "Договор-оферта",              "offer.md"),
    ("privacy", "Политика конфиденциальности", "privacy.md"),
]


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def unwrap(lines):
    """Склеить жёсткие переносы исходника обратно в абзац."""
    return " ".join(ln.strip() for ln in lines)


def blocks(raw: str):
    out, cur = [], []
    for line in raw.split("\n"):
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def render_list(block):
    items, cur = [], None
    for line in block:
        if line.lstrip().startswith("- "):
            if cur:
                items.append(cur)
            cur = [line.lstrip()[2:]]
        elif cur is not None:
            cur.append(line)
    if cur:
        items.append(cur)
    return ("<ul>" + "".join(f"<li>{inline(unwrap(i))}</li>" for i in items)
            + "</ul>")


def render_table(block):
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in block if ln.strip().startswith("|")]
    rows = [r for r in rows if not all(set(c) <= set("-: ") for c in r)]
    head, body = rows[0], rows[1:]
    th = "".join(f"<th>{inline(c)}</th>" for c in head)
    tb = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                 for r in body)
    return (f'<div class="scroll"><table><thead><tr>{th}</tr></thead>'
            f"<tbody>{tb}</tbody></table></div>")


# Пункты, где ошибка стоит денег или доступа к кошельку. Отмечаются не для
# красоты: это единственные места, читать которые обязательно.
MONEY = ("seed-фраз", "тратит деньги", "не несёт ответственности за\nсредства",
         "не несёт ответственности за средства", "возврат средств не производится",
         "блокировку аккаунта", "остаток подписки сгорает")


def render(raw: str) -> tuple[str, str]:
    parts, meta = [], []
    for block in blocks(raw):
        first = block[0].strip()
        if first.startswith("# "):
            continue                                # заголовок даём сами
        if first.startswith("## "):
            title = first[3:]
            m = re.match(r"^(\d+)\.\s+(.*)$", title)
            if m:
                parts.append(f'<h2><span class="num">{m.group(1)}</span>'
                             f"<span>{inline(m.group(2))}</span></h2>")
            else:
                parts.append(f"<h2>{inline(title)}</h2>")
            continue
        if all(ln.strip() == "---" for ln in block):
            continue                                # разделители даёт вёрстка
        if first.startswith("|"):
            parts.append(render_table(block))
            continue
        if first.startswith("- "):
            parts.append(render_list(block))
            continue
        text = unwrap(block)
        if text.startswith("**Дата вступления в силу:**") or \
           text.startswith("**Версия:**") or text.startswith("**Сервис:**"):
            meta.append(inline(text))
            continue
        cls = ""
        if any(k in text for k in MONEY):
            cls = ' class="money"'
        if text.startswith("📌"):
            cls = ' class="pin"'
            text = text.lstrip("📌 ")
        parts.append(f"<p{cls}>{inline(text)}</p>")
    return "\n".join(parts), "<br>".join(meta)


def main():
    sections, nav = [], []
    for key, title, fname in DOCS:
        raw = (SRC / fname).read_text()
        body, meta = render(raw)
        plain = re.sub(r"\n{3,}", "\n\n", raw).strip()
        nav.append(f'<button class="tab" data-doc="{key}">{title}</button>')
        sections.append(
            f'<article class="doc" id="{key}" hidden>'
            f'<header class="dochead"><h1>{title}</h1>'
            f'<p class="meta">{meta}</p>'
            f'<div class="acts">'
            f'<button class="copy" data-doc="{key}">Скопировать текст</button>'
            f'<span class="said" data-said="{key}"></span></div></header>'
            f'<div class="body">{body}</div>'
            f'<textarea class="src" id="src-{key}" readonly '
            f'aria-hidden="true" tabindex="-1">{html.escape(plain)}</textarea>'
            f"</article>")
    tpl = pathlib.Path(__file__).with_name("page.tpl").read_text()
    out = tpl.replace("<!--NAV-->", "\n".join(nav)) \
             .replace("<!--DOCS-->", "\n".join(sections))
    pathlib.Path("/home/user/Yomarket/docs/legal/yoomarket-legal.html").write_text(out)
    print("собрано, знаков:", len(out))


if __name__ == "__main__":
    main()
