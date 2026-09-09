#!/usr/bin/env python3
"""Перенос данных бота между файлами и базой, и резервные копии.

Зачем отдельная утилита, а не `pg_dump`. Готовый PostgreSQL, который
ставится без прав администратора, приходит урезанным: в нём есть `initdb`,
`pg_ctl` и `postgres` — и всё. Ни `psql`, ни `createdb`, ни `pg_dump`. Значит
снимать копию нечем, а копия и есть главная причина заводить базу.

Выручает то, что хранилище бота устроено просто: одна таблица `kv_store`
из двух столбцов и семи строк, в каждой — JSON целого раздела. Такую копию
снимает драйвер, уже стоящий в окружении бота. Больше того, копия выходит
читаемым JSON того же вида, что и файлы хранилища, — её можно и залить
обратно в базу, и разложить файлами, если база однажды не поднимется.

    db_tool.py migrate   файлы → база (ничего не затирая; --force затирает)
    db_tool.py dump ФАЙЛ база → файл копии
    db_tool.py restore ФАЙЛ файл копии → база
    db_tool.py unpack ФАЙЛ  файл копии → файлы хранилища
    db_tool.py show      что сейчас лежит в базе

Адрес базы берётся из DATABASE_URL, каталог файлов — из DATA_DIR: те же
переменные, по которым живёт сам бот, чтобы утилита не могла смотреть не
туда.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bot"))


def die(msg: str) -> None:
    print(f"\n❌ {msg}", file=sys.stderr)
    raise SystemExit(1)


def blobs() -> dict:
    """Разделы хранилища и пути их файлов — спрашиваем у самого бота.

    Не список имён файлов, переписанный сюда: он разошёлся бы с кодом при
    первом же новом разделе, и перенос молча потерял бы его целиком.
    """
    import storage
    return dict(storage._BLOBS)


def connect():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        die("DATABASE_URL не задан — не с чем работать.\n"
            "Он лежит в .env бота; запускайте утилиту так:\n"
            "    set -a; . ~/yomarket/.env; set +a; "
            "~/yomarket/.venv/bin/python scripts/db_tool.py …")
    try:
        import psycopg2
    except ImportError:
        die("нет psycopg2. Запускайте питоном бота:\n"
            "    ~/yomarket/.venv/bin/python scripts/db_tool.py …")
    try:
        conn = psycopg2.connect(url)
    except Exception as e:                                  # noqa: BLE001
        die(f"база не отвечает: {e}\n"
            "Поднята ли она:  systemctl --user status yomarket-db")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS kv_store "
                    "(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.commit()
    return conn


def read_db(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT k, v FROM kv_store")
        return {k: v for k, v in cur.fetchall()}


def write_db(conn, key: str, raw: str) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kv_store (k, v) VALUES (%s, %s) "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                    (key, raw))
    conn.commit()


def cmd_migrate(force: bool) -> int:
    """Файлы → база. Занятые разделы не трогаем без спроса.

    Затирать молча нельзя: в базе может быть свежее, а в файлах — то, что
    осталось с прошлой недели. Спутать эти две стороны значит откатить
    настройки продавца и не сказать об этом.
    """
    conn = connect()
    have = read_db(conn)
    moved, skipped, empty = [], [], []
    for key, path in blobs().items():
        if not os.path.exists(path):
            empty.append(key)
            continue
        with open(path) as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as e:
                die(f"файл {path} испорчен и не разбирается: {e}\n"
                    "Перенос прерван — целые разделы уже в базе, "
                    "починИ́те этот и запустите снова.")
        if not data:
            empty.append(key)
            continue
        if key in have and not force:
            skipped.append(key)
            continue
        write_db(conn, key, json.dumps(data, ensure_ascii=False))
        moved.append(f"{key} ({len(data)})")
    conn.close()
    print("перенесено:", ", ".join(moved) or "нечего")
    if skipped:
        print("пропущено (в базе уже есть):", ", ".join(skipped))
        print("  затереть базой из файлов:  db_tool.py migrate --force")
    if empty:
        print("пусто в файлах:", ", ".join(empty))
    return 0


def cmd_dump(path: str) -> int:
    conn = connect()
    rows = read_db(conn)
    conn.close()
    # Пишем через временный файл: копия, оборванная на середине, хуже
    # отсутствующей — на неё надеются.
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({k: json.loads(v) for k, v in rows.items()},
                  fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)          # в копии токены и шифротекст seed-фраз
    size = os.path.getsize(path)
    print(f"копия: {path} — {len(rows)} разделов, {size} байт, права 600")
    return 0


def cmd_restore(path: str) -> int:
    with open(path) as fh:
        data = json.load(fh)
    conn = connect()
    for key, value in data.items():
        write_db(conn, key, json.dumps(value, ensure_ascii=False))
    conn.close()
    print("залито в базу разделов:", len(data))
    return 0


def cmd_unpack(path: str) -> int:
    """Копия → файлы хранилища. Путь на случай, когда база не поднимается:
    бот умеет работать и без неё, и данные не должны оказаться в заложниках
    у сервера, который не стартует."""
    with open(path) as fh:
        data = json.load(fh)
    where = blobs()
    written = []
    for key, value in data.items():
        target = where.get(key)
        if not target:
            print(f"  раздел {key} этому боту неизвестен — пропускаю")
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w") as fh2:
            json.dump(value, fh2, ensure_ascii=False)
            fh2.flush()
            os.fsync(fh2.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        written.append(key)
    print("разложено файлами:", ", ".join(written) or "нечего")
    print("чтобы бот их читал, уберите DATABASE_URL из .env и перезапустите")
    return 0


def cmd_show() -> int:
    conn = connect()
    rows = read_db(conn)
    conn.close()
    if not rows:
        print("база пуста")
        return 0
    for key in sorted(rows):
        try:
            n = len(json.loads(rows[key]))
        except Exception:                                   # noqa: BLE001
            n = "?"
        # Значения не печатаем: там токены и шифротекст seed-фраз.
        print(f"  {key}: записей {n}, {len(rows[key])} байт")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "migrate":
        return cmd_migrate("--force" in rest)
    if cmd == "show":
        return cmd_show()
    if cmd in ("dump", "restore", "unpack"):
        if not rest:
            die(f"{cmd}: не сказано, какой файл")
        return {"dump": cmd_dump, "restore": cmd_restore,
                "unpack": cmd_unpack}[cmd](rest[0])
    die(f"неизвестная команда «{cmd}». Что умею — db_tool.py --help")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
