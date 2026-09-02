#!/usr/bin/env python3
"""Выложить правовые документы на Telegraph — с нужной подписью и одной
командой.

Зачем это скриптом, а не руками. Документы правятся вместе с кодом: тесты
сверяют, что оферта перечисляет те же тарифы, что продаёт бот, и обещает тот
же срок пробы. Но сверяют они ФАЙЛЫ РЕПОЗИТОРИЯ, а продавец читает
опубликованную страницу — и разъехаться они могут молча. Публикация одной
командой из тех же файлов эту щель закрывает.

Второе, ради чего он написан: подпись. Страница, созданная руками в
браузере, берёт автором ник того, кто её создал, да ещё и ссылкой на его
профиль. Здесь автор задан явно — «YooMarket Manager», без ссылки, — и
после публикации ПЕРЕЧИТЫВАЕТСЯ: «опубликовал» не доказательство.

━━━ Чего скрипт НЕ может ━━━

**Он не исправит страницы, созданные до него.** Telegraph разрешает править
страницу только тому ключу, которым она создана; страницу из браузера наш
ключ не тронет. Поэтому первый запуск создаёт НОВЫЕ страницы с правильной
подписью, а новые адреса надо вписать в бота:

    👑 Админ-панель → 📄 Правовые документы

Со второго запуска адреса больше не меняются: скрипт помнит, что где лежит
(`docs/legal/published.json`), и правит те же страницы. Старые остаются
висеть на Telegraph — удалить их оттуда нельзя ни руками, ни ключом.

**В Telegraph нет таблиц.** Таблица «кому передаются данные» разворачивается
списком — содержимое сохраняется целиком, сетка теряется. Скрипт об этом
говорит вслух, а не молча.

━━━ Как пользоваться ━━━

    python scripts/publish_legal.py --dry-run     что получится, без сети
    python scripts/publish_legal.py --new-account завести ключ (один раз)
    export TELEGRAPH_TOKEN=…
    python scripts/publish_legal.py               выложить или обновить

Ключ живёт в переменной окружения и в репозиторий не попадает: он и есть
единственное право править эти страницы. Печатается он ровно один раз — при
создании; потеряешь — прежние страницы станут неправимыми навсегда.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegra.ph"
AUTHOR = "YooMarket Manager"

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "legal"
RECORD = SRC / "published.json"

# Ключ здесь тот же, что у `storage.POLICY_DOCS`: адреса вписываются в бота
# по одному на документ, и перепутать их значит показать продавцу оферту под
# видом политики.
DOCS: tuple[tuple[str, str], ...] = (
    ("terms", "terms.md"),
    ("offer", "offer.md"),
    ("privacy", "privacy.md"),
)

# Предел Telegraph на содержимое страницы. Проверяем сами: отказ по размеру
# приходит с сервера в виде, по которому не понять, насколько перебрали.
CONTENT_LIMIT = 64 * 1024


class TelegraphError(RuntimeError):
    """Отказ Telegraph. В тексте — его собственные слова, а не наш пересказ."""


# ---------------------------------------------------------------------------
# Разметка → узлы Telegraph
# ---------------------------------------------------------------------------

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


# Внутри моноширинного разметки нет — там всё буквально; внутри жирного и
# ссылки она есть, поэтому их содержимое разбирается снова.
_RULES = (
    (_CODE, lambda m: {"tag": "code", "children": [m.group(1)]}),
    (_BOLD, lambda m: {"tag": "b", "children": inline(m.group(1))}),
    (_LINK, lambda m: {"tag": "a", "attrs": {"href": m.group(2)},
                       "children": inline(m.group(1))}),
)


def inline(text: str):
    """Строка → список узлов: жирное, моноширинное, ссылки.

    Побеждает САМОЕ ЛЕВОЕ совпадение, а не правило, стоящее раньше в списке.
    Проход правилами по очереди резал строку на куски, и `**Fragment
    (`fragment.com`)**` разваливался: моноширинное забирало середину, а
    жирное больше не видело пары `**` целиком — звёздочки уезжали продавцу
    буквами.
    """
    out: list = []
    while text:
        best = None
        for rule, build in _RULES:
            m = rule.search(text)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, build)
        if not best:
            out.append(text)
            break
        m, build = best
        if m.start():
            out.append(text[:m.start()])
        out.append(build(m))
        text = text[m.end():]
    return out


def _unwrap(lines) -> str:
    """Жёсткие переносы исходника — вёрстка файла, а не текста договора."""
    return " ".join(ln.strip() for ln in lines)


def _blocks(raw: str):
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


def _list_nodes(block, tag: str):
    """Список: пункт может занимать несколько строк исходника."""
    items, cur = [], None
    mark = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*)$")
    for line in block:
        m = mark.match(line)
        if m:
            if cur:
                items.append(cur)
            cur = [m.group(1)]
        elif cur is not None:
            cur.append(line)
    if cur:
        items.append(cur)
    return {"tag": tag,
            "children": [{"tag": "li", "children": inline(_unwrap(i))}
                         for i in items]}


def _table_nodes(block):
    """Таблица → список. Таблиц в Telegraph нет, а содержимое юридическое.

    Каждая строка становится пунктом: первая ячейка жирным, остальные через
    тире, с названием столбца. Заголовок вписывается в пункт, а не теряется:
    без него «всегда» в третьей колонке ничего не значит.
    """
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in block if ln.strip().startswith("|")]
    rows = [r for r in rows if not all(set(c) <= set("-: ") for c in r)]
    if not rows:
        return None, 0
    head, body = rows[0], rows[1:]
    items = []
    for row in body:
        kids = inline(f"**{row[0]}**") if row else []
        for title, cell in zip(head[1:], row[1:]):
            kids.append(f" — {title.lower()}: ")
            kids += inline(cell)
        items.append({"tag": "li", "children": kids})
    return {"tag": "ul", "children": items}, len(body)


def md_to_nodes(raw: str):
    """Файл → (заголовок, узлы, замечания).

    Замечания — это то, что скрипт сделал не буквально: развёрнутая таблица.
    Молчаливое преобразование в юридическом тексте недопустимо: читающий
    вывод должен знать, чем опубликованное отличается от исходника.
    """
    title, nodes, notes = "", [], []
    for block in _blocks(raw):
        first = block[0].strip()
        if first.startswith("# "):
            title = first[2:].strip()
            continue
        if first.startswith("### "):
            # В Telegraph заголовков ровно два: h3 и h4. `##` — крупный,
            # `###` — мелкий; h1/h2 он не принимает вовсе.
            nodes.append({"tag": "h4", "children": inline(first[4:].strip())})
            continue
        if first.startswith("## "):
            nodes.append({"tag": "h3", "children": inline(first[3:].strip())})
            continue
        if all(ln.strip() == "---" for ln in block):
            nodes.append({"tag": "hr"})
            continue
        if first.startswith("|"):
            node, rows = _table_nodes(block)
            if node:
                nodes.append(node)
                notes.append(f"таблица ({rows} строк) развёрнута списком — "
                             f"таблиц в Telegraph нет")
            continue
        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", first):
            tag = "ol" if re.match(r"^\s*\d+\.", first) else "ul"
            nodes.append(_list_nodes(block, tag))
            continue
        nodes.append({"tag": "p", "children": inline(_unwrap(block))})
    return title, nodes, notes


# ---------------------------------------------------------------------------
# Telegraph
# ---------------------------------------------------------------------------

def call(method: str, **params) -> dict:
    """Вызов Telegraph. Отдаёт `result`, на отказе кидает его же словами.

    HTTP 200 здесь ничего не решает: у Telegraph всё существенное лежит в
    конверте `{"ok": …}`. Конверт незнакомого вида — тоже отказ, а не повод
    упасть с KeyError на чужом поле.
    """
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise TelegraphError(
            f"{method}: HTTP {e.code}, ответ: "
            f"{e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise TelegraphError(f"{method}: до Telegraph не достучались — "
                             f"{e.reason}") from e
    try:
        data = json.loads(raw)
    except ValueError:
        raise TelegraphError(f"{method}: ответ не разобрался как JSON: "
                             f"{raw[:300]}") from None
    if not isinstance(data, dict) or "ok" not in data:
        raise TelegraphError(f"{method}: конверт не тот, что ожидался: "
                             f"{raw[:300]}")
    if not data.get("ok"):
        raise TelegraphError(f"{method}: {data.get('error') or raw[:300]}")
    return data.get("result") or {}


def new_account() -> dict:
    return call("createAccount", short_name="YooMarket",
                author_name=AUTHOR, author_url="")


def _payload(token: str, title: str, nodes) -> dict:
    # `author_url` шлём пустым НАМЕРЕННО, а не опускаем: подпись должна быть
    # именем без ссылки, и «поля нет» с «поле пустое» Telegraph понимает
    # по-разному только в нашей голове — проверяем перечитыванием.
    return {"access_token": token, "title": title, "author_name": AUTHOR,
            "author_url": "", "return_content": "false",
            "content": json.dumps(nodes, ensure_ascii=False)}


def create_page(token: str, title: str, nodes) -> dict:
    return call("createPage", **_payload(token, title, nodes))


def edit_page(token: str, path: str, title: str, nodes) -> dict:
    return call("editPage", path=path, **_payload(token, title, nodes))


def read_back(path: str) -> dict:
    return call("getPage", path=path, return_content="false")


def verify(path: str, title: str) -> list[str]:
    """Перечитать страницу и сравнить. Список расхождений — пустой значит
    сошлось.

    Отдельным шагом, потому что «опубликовал» — не доказательство: ответ на
    публикацию собирает наш же запрос, а показывают продавцу то, что лежит
    на сервере.
    """
    page = read_back(path)
    bad = []
    if page.get("title") != title:
        bad.append(f"заголовок на странице «{page.get('title')}», "
                   f"а отправляли «{title}»")
    if page.get("author_name") != AUTHOR:
        bad.append(f"автор на странице «{page.get('author_name')}», "
                   f"а должен быть «{AUTHOR}»")
    if (page.get("author_url") or "").strip():
        bad.append(f"имя автора осталось ссылкой: {page.get('author_url')}")
    return bad


# ---------------------------------------------------------------------------
# Память о выложенном
# ---------------------------------------------------------------------------

def load_record() -> dict:
    try:
        return json.loads(RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_record(rec: dict) -> None:
    RECORD.write_text(json.dumps(rec, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------

def prepare(fname: str):
    """Файл → всё, что нужно для публикации. Отдельно от сети, чтобы
    `--dry-run` проверял ровно то же, что уходит на сервер."""
    raw = (SRC / fname).read_text(encoding="utf-8")
    title, nodes, notes = md_to_nodes(raw)
    if not title:
        raise SystemExit(f"{fname}: нет строки «# Заголовок» — Telegraph "
                         f"требует заголовок, а выдумывать его нельзя")
    size = len(json.dumps(nodes, ensure_ascii=False).encode("utf-8"))
    if size > CONTENT_LIMIT:
        raise SystemExit(f"{fname}: {size} байт содержимого при пределе "
                         f"{CONTENT_LIMIT} — Telegraph столько не примет")
    return raw, title, nodes, notes, size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Выложить документы на Telegraph")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что получится, и не ходить в сеть")
    ap.add_argument("--new-account", action="store_true",
                    help="завести ключ Telegraph и напечатать его")
    args = ap.parse_args(argv)

    if args.new_account:
        acc = new_account()
        print("Ключ создан. Сохрани обе строки — второй раз их не покажут:\n")
        print(f"  TELEGRAPH_TOKEN={acc.get('access_token')}")
        print(f"  вход в браузере: {acc.get('auth_url')}\n")
        print("Ключ — единственное право править эти страницы. В репозиторий\n"
              "он не кладётся: держи его в переменной окружения.")
        return 0

    ready = [(key, *prepare(fname)) for key, fname in DOCS]

    if args.dry_run:
        for key, _raw, title, nodes, notes, size in ready:
            tags: dict[str, int] = {}
            for n in nodes:
                tags[n.get("tag", "?")] = tags.get(n.get("tag", "?"), 0) + 1
            print(f"\n📄 {key}: «{title}»")
            print(f"   автор: {AUTHOR} (без ссылки)")
            print(f"   {size} байт, блоков {len(nodes)}: "
                  + ", ".join(f"{t}×{c}" for t, c in sorted(tags.items())))
            for note in notes:
                print(f"   ⚠️  {note}")
        print("\nЭто сухой прогон: в сеть не ходили, ничего не опубликовано.")
        return 0

    token = os.environ.get("TELEGRAPH_TOKEN", "").strip()
    if not token:
        print("Нет TELEGRAPH_TOKEN. Заведи ключ:\n"
              "  python scripts/publish_legal.py --new-account", file=sys.stderr)
        return 2

    rec, failed, fresh = load_record(), [], []
    for key, raw, title, nodes, notes, _size in ready:
        known = rec.get(key) or {}
        path = known.get("path")
        created = False
        try:
            if path and known.get("sha") == digest(raw):
                print(f"📄 {key}: не менялся, перечитываю…")
                page = {"path": path, "url": known.get("url")}
            elif path:
                page = edit_page(token, path, title, nodes)
                print(f"📄 {key}: обновлён")
            else:
                page = create_page(token, title, nodes)
                created = True
                print(f"📄 {key}: создан")
            bad = verify(page["path"], title)
        except TelegraphError as e:
            print(f"❌ {key}: {e}", file=sys.stderr)
            failed.append(key)
            continue
        except KeyError:
            print(f"❌ {key}: Telegraph не назвал адрес страницы",
                  file=sys.stderr)
            failed.append(key)
            continue
        if bad:
            print(f"❌ {key}: страница вышла не такой, как отправляли:",
                  file=sys.stderr)
            for line in bad:
                print(f"     {line}", file=sys.stderr)
            failed.append(key)
            continue
        for note in notes:
            print(f"   ⚠️  {note}")
        print(f"   {page.get('url')}")
        rec[key] = {"path": page["path"], "url": page.get("url", ""),
                    "sha": digest(raw)}
        # В список «впиши адрес в бота» документ попадает ТОЛЬКО дойдя
        # сюда: страница, не прошедшая сверку, адресом не является, и
        # называть её новым адресом значит советовать вписать в бота
        # неизвестно что.
        if created:
            fresh.append(key)

    save_record(rec)
    if fresh:
        print("\n⚠️  Адреса новые. Впиши их в бота:")
        print("   👑 Админ-панель → 📄 Правовые документы")
        for key in fresh:
            print(f"   {key}: {rec[key]['url']}")
        print("   Бот ссылку не проверяет: со старым адресом кнопка молча\n"
              "   откроет прежнюю страницу.")
    if failed:
        print(f"\nНе вышло: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
