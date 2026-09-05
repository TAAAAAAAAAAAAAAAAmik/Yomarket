#!/usr/bin/env bash
#
# Обновление бота на сервере. Запускается НА сервере: из GitHub Actions по
# пушу, из локального Claude Code (`ssh сервер 'bash -s' < scripts/deploy.sh`)
# или руками.
#
# Главное правило этого проекта действует и здесь: **«перезапустил» — не
# доказательство**. Скрипт заканчивается не словом «готово», а сверкой:
# поднялась ли та версия, которую мы только что выкатили. Если поднялась
# старая — это провал, о нём говорится вслух и код возвращается на прежний
# коммит. Молчаливый «успешный деплой» со старой сборкой — ровно та беда,
# из-за которой в CLAUDE.md написано «после деплоя проверять /version».
#
# Ничего не угадывает: чем бот запущен (docker compose, systemd или просто
# python), выясняется осмотром сервера, и найденное печатается.
#
# Переменные (все необязательные):
#   DEPLOY_PATH   — каталог репозитория на сервере (по умолчанию — где лежит
#                   этот скрипт)
#   DEPLOY_BRANCH — ветка (по умолчанию текущая)
#   HEALTH_URL    — адрес health-эндпоинта для сверки версии
#                   (по умолчанию http://127.0.0.1:$PORT/health, если есть PORT)
#   SERVICE_NAME  — имя systemd-юнита, если автоопределение промахнулось
#   DEPLOY_RESTART_CMD — своя команда перезапуска; тогда осмотр сервера не
#                   делается вовсе (для установок, которых скрипт не знает)
#   NO_ROLLBACK=1 — не откатывать при неудачной сверке (для разбора руками)

set -Eeuo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

REPO="${DEPLOY_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || die "нет каталога $REPO"
[ -d .git ] || die "$REPO — не git-репозиторий. Укажите DEPLOY_PATH."

BRANCH="${DEPLOY_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
VERSION_FILE="bot/handlers/start.py"

read_version() {   # версия из исходников рабочего дерева
    sed -n 's/^BOT_VERSION *= *"\(.*\)".*/\1/p' "$VERSION_FILE" | head -1
}

step "Где обновляемся"
say "каталог:  $REPO"
say "ветка:    $BRANCH"
BEFORE_COMMIT="$(git rev-parse HEAD)"
BEFORE_VERSION="$(read_version)"
say "было:     ${BEFORE_COMMIT:0:8} · версия $BEFORE_VERSION"

# --- 1. Забрать код -------------------------------------------------------
# Без `reset --hard`: он молча выбрасывает правки, сделанные на сервере, и
# найти их потом негде. Если слияние не проходит — это факт, о котором надо
# сказать, а не переехать по нему.
step "Забираю код"
git fetch --prune origin "$BRANCH"
if ! git merge --ff-only "origin/$BRANCH"; then
    die "не удалось перемотать на origin/$BRANCH — на сервере есть свои
коммиты или незакоммиченные правки. Разберите их руками:
    cd $REPO && git status"
fi

AFTER_COMMIT="$(git rev-parse HEAD)"
AFTER_VERSION="$(read_version)"
say "стало:    ${AFTER_COMMIT:0:8} · версия $AFTER_VERSION"

if [ "$BEFORE_COMMIT" = "$AFTER_COMMIT" ]; then
    say ""
    say "ℹ️  Новых коммитов нет — обновлять нечего. Перезапуск всё равно делаю:"
    say "    контейнер мог упасть, и тогда это единственное, что поможет."
fi

if [ "$BEFORE_VERSION" = "$AFTER_VERSION" ] && [ "$BEFORE_COMMIT" != "$AFTER_COMMIT" ]; then
    # Не ошибка, но сверка после перезапуска станет бессмысленной: старая и
    # новая версии совпадают, и «код доехал» будет неотличимо от «не доехал».
    say ""
    say "⚠️  Код изменился, а BOT_VERSION — нет. Сверить, доехал ли он, будет"
    say "    нечем: и до, и после будет «$AFTER_VERSION». Поднимайте версию"
    say "    в $VERSION_FILE при каждом значимом изменении."
fi

# --- 2. Понять, чем бот запущен -------------------------------------------
step "Смотрю, чем бот запущен"
RUNTIME=""
COMPOSE=""
UNIT=""

if [ -n "${DEPLOY_RESTART_CMD:-}" ]; then
    # Своя команда — значит осматривать нечего: человек уже знает лучше.
    RUNTIME="custom"
    say "нашёл: своя команда перезапуска (DEPLOY_RESTART_CMD)"
fi

if [ -z "$RUNTIME" ] && { [ -f docker-compose.yml ] || [ -f compose.yml ]; }; then
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    fi
fi

UNIT="${SERVICE_NAME:-}"
if [ -z "$RUNTIME" ] && [ -z "$UNIT" ] && command -v systemctl >/dev/null 2>&1; then
    # Юнит ищем по тому, что он на самом деле запускает, а не по имени:
    # назвать службу могли как угодно.
    # `|| true` в конце обязателен. Без него неудачный `systemctl` (а он
    # падает везде, где systemd не запущен — в контейнерах, например) ронял
    # весь скрипт при `pipefail`, причём молча: ни строчки в выводе, только
    # код возврата 1. Тихая поломка выката — худшее, что тут может быть.
    UNIT="$(systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null \
            | awk '{print $1}' \
            | while read -r u; do
                  frag="$(systemctl show -p FragmentPath --value "$u" 2>/dev/null || true)"
                  [ -n "$frag" ] && [ -f "$frag" ] || continue
                  if grep -qF "$REPO" "$frag" 2>/dev/null; then printf '%s\n' "$u"; break; fi
              done | head -1 || true)"
fi

BARE_PID=""
[ -z "$RUNTIME" ] && BARE_PID="$(pgrep -f "python.*$REPO/bot/main.py|python.*main\.py" 2>/dev/null | head -1 || true)"

if [ -n "$RUNTIME" ]; then
    :                              # уже решено выше
elif [ -n "$COMPOSE" ] && $COMPOSE ps --quiet 2>/dev/null | grep -q .; then
    RUNTIME="compose"
elif [ -n "$UNIT" ]; then
    RUNTIME="systemd"
elif [ -n "$COMPOSE" ]; then
    # Compose-файл есть, но ничего не поднято — поднимем.
    RUNTIME="compose"
elif [ -n "$BARE_PID" ]; then
    RUNTIME="bare"
else
    die "не понял, чем запущен бот. Проверял: compose-файл и запущенные
контейнеры, systemd-юнит со ссылкой на $REPO, живой процесс python main.py.
Ничего не нашлось. Подскажите скрипту: SERVICE_NAME=имя.service, либо
запустите бота один раз руками — дальше он определится сам."

fi
[ "$RUNTIME" = custom ] || say "нашёл: $RUNTIME${UNIT:+ ($UNIT)}${COMPOSE:+ ($COMPOSE)}"

# --- 3. Перезапустить -----------------------------------------------------
step "Перезапускаю"
case "$RUNTIME" in
    custom)
        say "выполняю: $DEPLOY_RESTART_CMD"
        bash -c "$DEPLOY_RESTART_CMD"
        ;;
    compose)
        $COMPOSE up -d --build
        ;;
    systemd)
        if [ -f bot/requirements.txt ]; then
            PIP="pip"
            [ -x "$REPO/.venv/bin/pip" ] && PIP="$REPO/.venv/bin/pip"
            $PIP install -q -r bot/requirements.txt || \
                die "не встали зависимости — службу не трогаю, старая версия жива"
        fi
        systemctl restart "$UNIT"
        ;;
    bare)
        say "⚠️  Бот запущен вручную (pid $BARE_PID), без службы. Перезапускаю"
        say "    процессом в nohup — переживёт выход из ssh, но не перезагрузку"
        say "    сервера. Это стоит перевести на systemd или compose."
        [ -f bot/requirements.txt ] && pip install -q -r bot/requirements.txt || true
        kill "$BARE_PID" 2>/dev/null || true
        for _ in $(seq 1 10); do kill -0 "$BARE_PID" 2>/dev/null || break; sleep 1; done
        kill -9 "$BARE_PID" 2>/dev/null || true
        ( cd "$REPO/bot" && nohup python main.py >>"$REPO/bot.log" 2>&1 & )
        ;;
esac

# --- 4. Сверить, что поднялась именно новая версия ------------------------
step "Проверяю, что доехало"

HEALTH="${HEALTH_URL:-}"
if [ -z "$HEALTH" ] && [ -z "${PORT:-}" ] && [ -f .env ]; then
    # Порт бота обычно лежит в .env рядом с остальными настройками, а в
    # окружении того, кто запускает выкат, его нет. Читаем оттуда — но
    # только читаем: угадать порт нельзя, потому что промах превратил бы
    # удачный выкат в «бот не ответил», то есть в ложную тревогу.
    PORT="$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p' .env | tail -1)"
    [ -n "$PORT" ] && say "порт health взят из .env: $PORT"
fi
if [ -z "$HEALTH" ] && [ -n "${PORT:-}" ]; then
    HEALTH="http://127.0.0.1:$PORT/health"
fi

if [ -z "$HEALTH" ] || ! command -v curl >/dev/null 2>&1; then
    # Сказать «выкачено» здесь было бы обещанием, которое нечем подтвердить.
    say "⚠️  Сверить версию по сети нечем: не задан HEALTH_URL (или PORT), либо"
    say "    нет curl. Проверено только то, что перезапуск отработал без ошибки."
    say "    Откройте /version в боте и убедитесь, что там $AFTER_VERSION."
    say ""
    say "✅ Обновление выполнено, версия не сверена."
    exit 0
fi

LIVE=""
for attempt in $(seq 1 30); do
    BODY="$(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null || true)"
    LIVE="$(printf '%s' "$BODY" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    [ -n "$LIVE" ] && break
    sleep 2
done

if [ -z "$LIVE" ]; then
    say "⚠️  $HEALTH не ответил за минуту или не назвал версию."
    say "    Логи: ${RUNTIME} → $( [ "$RUNTIME" = compose ] && echo "$COMPOSE logs --tail=50 bot" \
                                   || { [ "$RUNTIME" = systemd ] && echo "journalctl -u $UNIT -n 50" \
                                   || echo "tail -50 $REPO/bot.log"; } )"
    die "не подтверждено, что бот поднялся. Старую сборку не откатываю: она
уже заменена, и откат вслепую сделал бы только хуже. Смотрите логи."
fi

say "бот отвечает, версия: $LIVE"

if [ "$LIVE" != "$AFTER_VERSION" ]; then
    say ""
    say "❌ Поднялась НЕ та версия: ждали $AFTER_VERSION, работает $LIVE."
    say "   Значит перезапуск не подхватил новый код — обычно это старый"
    say "   образ, кеш сборки или второй экземпляр бота, который и отвечает."
    if [ "${NO_ROLLBACK:-}" = "1" ]; then
        die "откат отключён (NO_ROLLBACK=1). Код остаётся на ${AFTER_COMMIT:0:8}."
    fi
    say ""
    say "   Возвращаю код на ${BEFORE_COMMIT:0:8} — чтобы репозиторий и"
    say "   работающий бот снова означали одно и то же."
    git reset --hard "$BEFORE_COMMIT" >/dev/null
    die "выкат не удался, код откачен. Разберитесь с логами и повторите."
fi

# Версия совпала — но «поднялся» и «слышит Telegram» разные вещи. Бот,
# запущенный дважды с одним токеном, отвечает на health и молчит в чате.
POLLING_STATE="$(printf '%s' "$BODY" | sed -n 's/.*"polling" *: *"\([^"]*\)".*/\1/p')"
if [ -n "$POLLING_STATE" ] && [ "$POLLING_STATE" != "ok" ]; then
    say ""
    say "⚠️  Код выкачен, но бот НЕ получает сообщения:"
    say "    $POLLING_STATE"
    say "    Проверьте, не запущен ли он где-то ещё с тем же токеном."
fi

step "Готово"
say "✅ Работает версия $LIVE (коммит ${AFTER_COMMIT:0:8})."
say "   Сверено по $HEALTH, а не по факту перезапуска."
