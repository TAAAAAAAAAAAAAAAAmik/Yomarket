#!/usr/bin/env bash
#
# PostgreSQL для бота БЕЗ прав администратора — рядом с ботом, в домашнем
# каталоге. Продолжение scripts/setup-user.sh: тот ставит бота, этот
# переводит его с файлов на базу.
#
# Зачем база, если файлы и так не пропадают. Затем, что запись в файл
# обрывается вместе с процессом: перезагрузка в неудачную секунду оставляла
# обрезанный JSON, и продавец терял токен и настройки разом. В коде это
# закрыто (запись стала атомарной), но у базы есть то, чего у файлов нет
# совсем, — снимок состояния, который можно унести с сервера.
#
# Откуда берётся PostgreSQL без root. Готовыми сборками с Maven Central,
# теми же, на которых работают встраиваемые тесты у java-разработчиков.
# Они урезаны: `initdb`, `pg_ctl`, `postgres` — и всё. Ни `psql`, ни
# `pg_dump`. Поэтому копии снимает scripts/db_tool.py через драйвер, уже
# стоящий у бота, и снимает их читаемым JSON — тем же, каким хранилище
# лежит в файлах. Такую копию можно и залить обратно, и разложить файлами,
# если база однажды не поднимется.
#
# Как устроен доступ. Сервер не слушает сеть ВООБЩЕ (`listen_addresses=''`),
# общение идёт через unix-сокет, а вход разрешён только тому пользователю
# ОС, чьё имя совпало с именем в базе (`peer`). Пароля нет — потому что его
# негде было бы хранить безопаснее, чем сам доступ.
#
# Запускать ОТ ОБЫЧНОГО пользователя. Повторный запуск обновляет службу и
# не трогает уже созданный кластер.
#
# Переменные:
#   DEPLOY_PATH  где стоит бот (по умолчанию ~/yomarket)
#   PG_HOME      куда положить бинарники (по умолчанию ~/pgsql)
#   PG_DATA      где держать базу (по умолчанию ~/yomarket-db)
#   PG_VERSION   версия PostgreSQL (по умолчанию 17.11.0)

set -Eeuo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "этот скрипт для ОБЫЧНОГО пользователя, не для root.
База будет работать от него же, и вход в неё разрешён только ему."

REPO="${DEPLOY_PATH:-$HOME/yomarket}"
PGROOT="${PG_HOME:-$HOME/pgsql}"
PGDATA="${PG_DATA:-$HOME/yomarket-db}"
SOCK="$PGDATA/sock"
PGVER="${PG_VERSION:-17.11.0}"
DBNAME="${PG_DB:-yomarket}"
ENV_FILE="$REPO/.env"
PY="$REPO/.venv/bin/python"
export REPO_PATH="$REPO"
UNITDIR="$HOME/.config/systemd/user"
BACKUPS="${BACKUP_DIR:-$HOME/yomarket-backups}"

# --- 0. Есть ли к чему подключать ------------------------------------------
step "Смотрю, стоит ли бот"
[ -f "$ENV_FILE" ] || die "не нашёл $ENV_FILE.
Сначала поставьте бота:  bash setup-user.sh"
[ -x "$PY" ] || die "не нашёл питон бота: $PY
Сначала поставьте бота:  bash setup-user.sh"
"$PY" -c "import psycopg2" 2>/dev/null || die "у бота нет psycopg2 — без него
он не сможет говорить с базой. Обновите зависимости:  bash setup-user.sh"
say "бот в $REPO, драйвер базы на месте"

# --- 1. Бинарники ----------------------------------------------------------
step "Ставлю PostgreSQL $PGVER"
if [ -x "$PGROOT/bin/postgres" ]; then
    say "уже стоит: $("$PGROOT/bin/postgres" --version)"
else
    case "$(uname -m)" in
        x86_64)  PGARCH=linux-amd64 ;;
        aarch64) PGARCH=linux-arm64v8 ;;
        *) die "неизвестная архитектура $(uname -m) — готовой сборки под неё нет.
Посмотреть, какие есть: https://repo1.maven.org/maven2/io/zonky/test/postgres/" ;;
    esac
    BASE="https://repo1.maven.org/maven2/io/zonky/test/postgres"
    JAR="$BASE/embedded-postgres-binaries-$PGARCH/$PGVER/embedded-postgres-binaries-$PGARCH-$PGVER.jar"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "$JAR" -o "$TMP/pg.jar" || die "не скачался PostgreSQL.
Проверьте доступность:  curl -I $JAR
Если Maven Central закрыт у провайдера — базу придётся взять внешнюю."
    # jar — это zip. `unzip` на сервере может не быть; распаковываем питоном
    # бота, он есть заведомо.
    "$PY" - "$TMP" <<'PYEOF' || die "архив PostgreSQL не распаковался"
import sys, zipfile, os
tmp = sys.argv[1]
z = zipfile.ZipFile(os.path.join(tmp, "pg.jar"))
inner = [n for n in z.namelist() if n.endswith(".txz")]
if not inner:
    raise SystemExit("в архиве нет сборки PostgreSQL")
z.extract(inner[0], tmp)
print(os.path.join(tmp, inner[0]))
PYEOF
    mkdir -p "$PGROOT"
    tar xJf "$TMP"/postgres-*.txz -C "$PGROOT" || die "не распаковалась сборка PostgreSQL"
    [ -x "$PGROOT/bin/postgres" ] || die "в сборке нет postgres — распаковалось не то"
    say "поставлен: $("$PGROOT/bin/postgres" --version)"
    trap - EXIT
    rm -rf "$TMP"
fi

# --- 2. Кластер ------------------------------------------------------------
#
# Уже созданный кластер НЕ трогаем ни при каких условиях: в нём данные.
step "Готовлю базу"
if [ -f "$PGDATA/data/PG_VERSION" ]; then
    say "кластер уже есть в $PGDATA/data — не трогаю"
else
    mkdir -p "$PGDATA"
    # peer — вход только своему пользователю ОС; host=reject — по сети
    # не пускать вообще. Пароля нет и не нужно: снаружи не достучаться.
    # locale=C, чтобы initdb не спотыкался о ненастроенные локали сервера;
    # на хранение JSON по точному ключу сортировка не влияет.
    "$PGROOT/bin/initdb" -D "$PGDATA/data" -U "$USER" \
        --auth-local=peer --auth-host=reject \
        -E UTF8 --locale=C >"$PGDATA/initdb.log" 2>&1 \
        || die "initdb не отработал. Что случилось: $PGDATA/initdb.log"
    say "кластер создан: вход только для $USER, по сети — никак"
fi
mkdir -p "$SOCK" "$BACKUPS"
chmod 700 "$PGDATA" "$BACKUPS"

# --- 3. Служба базы --------------------------------------------------------
step "Автозапуск базы"
PGOPTS="-c listen_addresses='' -k $SOCK"
DB_MODE=""
if systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$UNITDIR"
    cat > "$UNITDIR/yomarket-db.service" <<UNITEOF
[Unit]
Description=YooMarket BOT — PostgreSQL

[Service]
# Не Type=notify: эта сборка PostgreSQL собрана БЕЗ поддержки systemd
# (проверено 30.08 — в двоичном файле нет ни libsystemd, ни sd_notify).
# С notify служба ждала бы уведомления о готовности, которого никогда не
# будет, через полторы минуты считалась бы упавшей — и бот крутился бы в
# перезапусках при живой, работающей базе.
Type=simple
ExecStart=${PGROOT}/bin/postgres -D ${PGDATA}/data ${PGOPTS}
# Без этого «служба запущена» значило бы только «процесс порождён»: база
# ещё не принимает подключения, а бот по After= уже стартует и падает.
# Ждём сокет — тогда After= означает «база готова», а не «файл запущен».
ExecStartPost=/bin/sh -c 'for _ in \$(seq 1 60); do [ -S "${SOCK}/.s.PGSQL.5432" ] && exit 0; sleep 1; done; exit 1'
ExecReload=/bin/kill -HUP \$MAINPID
# SIGINT — это «быстрое выключение» PostgreSQL: оборвать подключения, но
# закрыть файлы по-человечески. SIGTERM у него означает «ждать, пока все
# отключатся», и служба висела бы до таймаута.
KillSignal=SIGINT
TimeoutStopSec=60
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF
    # Бот обязан стартовать ПОСЛЕ базы. Правим не его файл, а надстройку:
    # setup-user.sh перезапишет свой при следующем запуске, а эта уцелеет.
    mkdir -p "$UNITDIR/yomarket.service.d"
    cat > "$UNITDIR/yomarket.service.d/db.conf" <<DROPEOF
[Unit]
After=yomarket-db.service
Wants=yomarket-db.service
DROPEOF
    systemctl --user daemon-reload
    systemctl --user enable yomarket-db >/dev/null 2>&1 || true
    systemctl --user restart yomarket-db
    DB_MODE="systemd"
    say "служба yomarket-db запущена, бот будет стартовать после неё"
else
    # Без systemd порядок запуска не гарантируется ничем, и врать об этом
    # нельзя: бот, стартовавший раньше базы, упадёт.
    "$PGROOT/bin/pg_ctl" -D "$PGDATA/data" -l "$PGDATA/pg.log" \
        -o "$PGOPTS" start >/dev/null 2>&1 || true
    DB_MODE="вручную"
    say "⚠️  systemd для пользователя недоступен — база запущена напрямую."
    say "    После перезагрузки её придётся поднимать самому:"
    say "        $PGROOT/bin/pg_ctl -D $PGDATA/data -l $PGDATA/pg.log -o \"$PGOPTS\" start"
    say "    И ВСЕГДА до бота: бот, стартовавший раньше базы, упадёт."
fi

# Ждём сокет, а не «спим и надеемся».
for _ in $(seq 1 30); do
    [ -S "$SOCK"/.s.PGSQL.5432 ] && break
    sleep 1
done
[ -S "$SOCK"/.s.PGSQL.5432 ] || die "база не поднялась за 30 секунд.
Логи: $( [ "$DB_MODE" = systemd ] && echo 'journalctl --user -u yomarket-db -n 50' \
                                  || echo "tail -50 $PGDATA/pg.log" )"
say "база отвечает"

# --- 4. Собственно база ----------------------------------------------------
step "Завожу базу $DBNAME"
URL="postgresql:///$DBNAME?host=$SOCK"
"$PY" - "$SOCK" "$DBNAME" <<'PYEOF' || die "не удалось завести базу"
import sys
import psycopg2
sock, name = sys.argv[1], sys.argv[2]
c = psycopg2.connect(f"postgresql:///postgres?host={sock}")
c.autocommit = True
with c.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
    if cur.fetchone():
        print(f"база {name} уже есть")
    else:
        cur.execute(f'CREATE DATABASE "{name}"')
        print(f"база {name} создана")
c.close()
PYEOF

# --- 5. Перенос ------------------------------------------------------------
#
# ДО подмены DATABASE_URL: иначе бот перезапустится на пустую базу, и
# продавец увидит бота, потерявшего всё, — а данные будут целы в файлах,
# о чём он не догадается.
step "Переношу то, что уже накоплено"
TOOL="$REPO/scripts/db_tool.py"
[ -f "$TOOL" ] || TOOL="$(cd "$(dirname "$0")" && pwd)/db_tool.py"
[ -f "$TOOL" ] || die "не нашёл db_tool.py — переносить нечем"

# Окружение бота целиком, а не один DATABASE_URL. Каталог данных задаётся
# переменной DATA_DIR из .env, и утилита спрашивает его у самого хранилища.
# Без неё она смотрит в каталог по умолчанию, не находит там ничего и
# честно докладывает «пусто в файлах» — а данные продавца остаются лежать
# в стороне. Бот при этом переезжает на ПУСТУЮ базу: снаружи это выглядит
# как бот, потерявший всех продавцов, при живых и целых файлах.
# Поймано живым прогоном 30.08: перенос отчитался «нечего» при заведённом
# продавце.
( set -a; . "$ENV_FILE"; set +a
  DATABASE_URL="$URL" "$PY" "$TOOL" migrate ) \
  || die "перенос не удался — DATABASE_URL в .env НЕ трогаю, бот работает по-старому"

# Сверяем перенос, а не верим ему: у каждого непустого файла обязана быть
# строка в базе. «Перенёс» — не доказательство, как и «HTTP 200».
( set -a; . "$ENV_FILE"; set +a
  DATABASE_URL="$URL" "$PY" - <<'PYEOF'
import json
import os
import sys
sys.path.insert(0, os.path.join(os.environ.get("REPO_PATH", ""), "bot"))
import psycopg2
import storage

conn = psycopg2.connect(os.environ["DATABASE_URL"])
with conn.cursor() as cur:
    cur.execute("SELECT k FROM kv_store")
    in_db = {row[0] for row in cur.fetchall()}
conn.close()

lost = []
for key, path in storage._BLOBS.items():
    if not os.path.exists(path):
        continue
    with open(path) as fh:
        if json.load(fh) and key not in in_db:
            lost.append(key)
if lost:
    raise SystemExit("НЕ доехали до базы разделы: " + ", ".join(lost))
print("сверено: всё, что было в файлах, лежит в базе")
PYEOF
) || die "перенос не сошёлся — DATABASE_URL в .env НЕ трогаю, бот работает по-старому.
Файлы целы. Посмотреть, что в базе:  $PY $TOOL show"

# --- 6. Переключаем бота ---------------------------------------------------
step "Переключаю бота на базу"
if grep -q '^DATABASE_URL=' "$ENV_FILE"; then
    OLD="$(sed -n 's/^DATABASE_URL=//p' "$ENV_FILE" | head -1)"
    if [ "$OLD" != "$URL" ]; then
        say "в .env уже был другой адрес базы:"
        say "    $OLD"
        die "не подменяю его молча — там могут быть чужие данные.
Если тот адрес больше не нужен, уберите строку DATABASE_URL из
$ENV_FILE и запустите скрипт снова."
    fi
    say "адрес базы в .env уже правильный"
else
    printf 'DATABASE_URL=%s\n' "$URL" >> "$ENV_FILE"
    say "адрес базы записан в .env"
fi

if [ "$DB_MODE" = systemd ]; then
    systemctl --user restart yomarket
else
    pkill -f "$REPO/.venv/bin/python main.py" 2>/dev/null || true
    ( cd "$REPO/bot" && set -a && . "$ENV_FILE" && set +a && \
      nohup "$PY" main.py >> "${DATA_DIR:-$HOME/yomarket-data}/bot.log" 2>&1 & )
fi
sleep 6

# --- 7. Ежедневная копия ---------------------------------------------------
step "Ежедневная копия"
BACKUP_SH="$PGDATA/backup.sh"
cat > "$BACKUP_SH" <<BKEOF
#!/usr/bin/env bash
# Снимок базы в JSON + уборка старых. Копий держим 14.
set -Eeuo pipefail
set -a; . "${ENV_FILE}"; set +a
"${PY}" "${REPO}/scripts/db_tool.py" dump "${BACKUPS}/yomarket-\$(date +%F).json"
ls -1t "${BACKUPS}"/yomarket-*.json 2>/dev/null | tail -n +15 | xargs -r rm --
BKEOF
chmod +x "$BACKUP_SH"
if [ "$DB_MODE" = systemd ]; then
    cat > "$UNITDIR/yomarket-backup.service" <<UNITEOF
[Unit]
Description=YooMarket BOT — копия базы

[Service]
Type=oneshot
ExecStart=${BACKUP_SH}
UNITEOF
    cat > "$UNITDIR/yomarket-backup.timer" <<UNITEOF
[Unit]
Description=YooMarket BOT — копия базы раз в сутки

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
UNITEOF
    systemctl --user daemon-reload
    systemctl --user enable --now yomarket-backup.timer >/dev/null 2>&1 || true
    say "копия снимается раз в сутки, хранится 14 штук: $BACKUPS"
elif command -v crontab >/dev/null; then
    ( crontab -l 2>/dev/null | grep -v 'yomarket-db/backup.sh'
      echo "17 4 * * * $BACKUP_SH" ) | crontab - 2>/dev/null \
        && say "копия раз в сутки через cron: $BACKUPS" \
        || say "⚠️  не вышло завести расписание — снимайте копию сами: $BACKUP_SH"
else
    say "⚠️  РАСПИСАНИЯ НЕТ: ни systemd, ни cron. Копию снимать самому:"
    say "        $BACKUP_SH"
fi
# Первую копию снимаем сразу: расписание, ни разу не сработавшее, — это
# обещание, а не копия.
"$BACKUP_SH" || say "⚠️  первая копия не снялась — проверьте $BACKUP_SH"

# --- 8. Проверка -----------------------------------------------------------
#
# «Переключил» — не доказательство. Спрашиваем у самой базы, пишет ли в неё
# работающий бот, и сверяем версию через /health.
step "Проверяю"
pgrep -f "$REPO/.venv/bin/python main.py" >/dev/null || die "бот не поднялся.
Логи: $( [ "$DB_MODE" = systemd ] && echo 'journalctl --user -u yomarket -n 50' \
                                  || echo "tail -50 ${DATA_DIR:-$HOME/yomarket-data}/bot.log" )"
say "процесс бота жив"

DATABASE_URL="$URL" "$PY" - <<'PYEOF' || die "бот не работает с базой"
import os
import psycopg2
c = psycopg2.connect(os.environ["DATABASE_URL"])
with c.cursor() as cur:
    cur.execute("SELECT to_regclass('kv_store')")
    if not cur.fetchone()[0]:
        raise SystemExit("таблицы kv_store нет — бот в базу не писал")
    cur.execute("SELECT count(*) FROM kv_store")
    print(f"в базе разделов: {cur.fetchone()[0]}")
PYEOF

PORT_VALUE="$(sed -n 's/^PORT=//p' "$ENV_FILE" | head -1)"
if [ -n "$PORT_VALUE" ]; then
    BODY="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT_VALUE}/health" 2>/dev/null || true)"
    GOT="$(printf '%s' "$BODY" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    [ -n "$GOT" ] || die "health на порту ${PORT_VALUE} не ответил — бот запущен, но нездоров."
    say "версия на ходу: $GOT"
fi

say ""
say "✅ Готово. Бот работает на PostgreSQL."
say ""
say "   база:     $PGDATA (сокет, без сетевого порта и без пароля)"
say "   копии:    $BACKUPS, раз в сутки, хранится 14"
say "   что там:  $PY $REPO/scripts/db_tool.py show"
say ""
say "Проверьте в боте /version — хранилище должно стать 🟢 PostgreSQL."
[ "$DB_MODE" = systemd ] \
    && say "Логи базы:  journalctl --user -u yomarket-db -f" \
    || say "Логи базы:  tail -f $PGDATA/pg.log"
