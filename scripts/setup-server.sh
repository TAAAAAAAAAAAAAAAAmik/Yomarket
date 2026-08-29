#!/usr/bin/env bash
#
# Первичная настройка сервера под бота. Запускается НА сервере один раз;
# дальше обновления идут через scripts/deploy.sh.
#
# Чего этот скрипт НЕ делает намеренно:
#
#   * не придумывает секреты. `BOT_TOKEN` и `SECRET_KEY` спрашиваются, а не
#     генерируются молча: подставленный за спиной ключ шифрования означает,
#     что seed-фразы кошельков зашифрованы неизвестно чем, и при переносе на
#     другой сервер они не расшифруются;
#   * не пишет секреты в репозиторий. `.env` лежит рядом с кодом и уже в
#     `.gitignore`;
#   * не говорит «готово», не проверив. В конце поднимается бот и сверяется
#     версия через /health — ровно как в scripts/deploy.sh.
#
# Переменные (можно задать заранее, иначе спросит):
#   DEPLOY_PATH   куда положить репозиторий (по умолчанию /opt/yomarket)
#   REPO_URL      откуда клонировать
#   DEPLOY_BRANCH ветка (по умолчанию claude/where-we-left-off-mul8tu)

set -Eeuo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

REPO="${DEPLOY_PATH:-/opt/yomarket}"
BRANCH="${DEPLOY_BRANCH:-claude/where-we-left-off-mul8tu}"
SERVICE="${SERVICE_NAME:-yomarket}"
RUN_USER="${SUDO_USER:-$(id -un)}"

[ "$(id -u)" = 0 ] || die "нужен root: запустите через sudo"

# --- 1. Что нужно системе -------------------------------------------------
step "Ставлю зависимости системы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip curl \
    build-essential libffi-dev >/dev/null
# libffi-dev и build-essential здесь не для красоты: без них ломается сборка
# `cryptography`, и падают все тесты шифрования seed-фразы с
# `ModuleNotFoundError: No module named '_cffi_backend'`.
say "git, python3, venv, curl — на месте"

# --- 2. Код ---------------------------------------------------------------
step "Забираю код"
if [ -d "$REPO/.git" ]; then
    say "репозиторий уже есть — обновляю"
    git -C "$REPO" fetch --prune origin "$BRANCH"
    git -C "$REPO" checkout "$BRANCH"
    git -C "$REPO" merge --ff-only "origin/$BRANCH"
else
    [ -n "${REPO_URL:-}" ] || die "задайте REPO_URL — откуда клонировать"
    mkdir -p "$(dirname "$REPO")"
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO"
fi
chown -R "$RUN_USER":"$RUN_USER" "$REPO"
say "код в $REPO, ветка $BRANCH"

# --- 3. Окружение ---------------------------------------------------------
step "Виртуальное окружение и зависимости"
sudo -u "$RUN_USER" python3 -m venv "$REPO/.venv"
sudo -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q -r "$REPO/bot/requirements.txt"
say "зависимости поставлены из bot/requirements.txt"

# --- 4. Секреты -----------------------------------------------------------
#
# `.env` не перезаписывается: на работающем сервере в нём лежит ключ, которым
# зашифрованы seed-фразы. Перезаписать его значит потерять доступ к чужим
# кошелькам — молча, и выяснится это на первой же выдаче звёзд.
step "Секреты"
ENV_FILE="$REPO/.env"
if [ -f "$ENV_FILE" ]; then
    say "$ENV_FILE уже есть — не трогаю."
    say "Если нужно сменить токен, правьте файл руками."
else
    ask() {  # ask ПЕРЕМЕННАЯ "пояснение" [обязательна]
        local var="$1" hint="$2" required="${3:-}" value=""
        printf '\n%s\n> ' "$hint"
        read -r value </dev/tty
        if [ -z "$value" ] && [ -n "$required" ]; then
            die "$var обязательна — без неё бот не запустится"
        fi
        [ -n "$value" ] && printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
    }
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown "$RUN_USER":"$RUN_USER" "$ENV_FILE"

    ask BOT_TOKEN "Токен бота от @BotFather (обязательно):" yes
    ask SECRET_KEY "Ключ шифрования seed-фраз и ключей поставщиков.
ОБЯЗАТЕЛЕН: без него seed-фразы лежат в базе открытым текстом.
Сгенерировать длинную строку: openssl rand -hex 32
Если переносите бота со старого сервера — вставьте ТОТ ЖЕ ключ, иначе
зашифрованные фразы не расшифруются:" yes
    ask DATABASE_URL "Адрес PostgreSQL (postgresql://...).
Пусто — данные лягут в JSON-файлы в каталоге бота. На своём сервере они
переживают перезапуск, но база надёжнее:"
    ask PORT "Порт для /health, например 8080.
Пусто — health-сервер не поднимется, и выкат не сможет сверить версию:"

    say ""
    say ".env создан, права 600, в git не попадёт."
fi

# --- 5. Автозапуск --------------------------------------------------------
step "Служба systemd"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=YooMarket BOT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO}/bot
EnvironmentFile=${REPO}/.env
ExecStart=${REPO}/.venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null
systemctl restart "${SERVICE}"
say "служба ${SERVICE} включена в автозапуск и запущена"

# --- 6. Проверка ----------------------------------------------------------
#
# «Запустил» — не доказательство. Смотрим, что процесс жив и что health
# отвечает той версией, которая лежит в исходниках.
step "Проверяю"
sleep 5
systemctl is-active --quiet "${SERVICE}" \
    || die "служба не поднялась. Что случилось: journalctl -u ${SERVICE} -n 50"
say "процесс жив"

WANT="$(sed -n 's/^BOT_VERSION *= *"\(.*\)".*/\1/p' "$REPO/bot/handlers/start.py" | head -1)"
PORT_VALUE="$(sed -n 's/^PORT=//p' "$ENV_FILE" | head -1)"
if [ -z "$PORT_VALUE" ]; then
    say ""
    say "⚠️  PORT не задан — health-сервер не поднят, и сверить версию нечем."
    say "    Бот при этом работает. Чтобы сверка была, добавьте PORT в .env"
    say "    и перезапустите: systemctl restart ${SERVICE}"
    exit 0
fi

BODY="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT_VALUE}/health" 2>/dev/null || true)"
GOT="$(printf '%s' "$BODY" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
[ -n "$GOT" ] || die "health на порту ${PORT_VALUE} не ответил.
Логи: journalctl -u ${SERVICE} -n 50"
[ "$GOT" = "$WANT" ] || die "поднялась версия «$GOT», а в коде «$WANT» —
код не доехал. Логи: journalctl -u ${SERVICE} -n 50"

# «Поднялся» и «слышит Telegram» — разные вещи, и при переезде расходятся
# они особенно часто: старый сервер ещё работает с тем же токеном, либо на
# токене остался вебхук. И то и другое приходит как 409 Conflict, а лечится
# противоположным, поэтому причина не угадывается — печатается как есть.
POLLING="$(printf '%s' "$BODY" | sed -n 's/.*"polling" *: *"\([^"]*\)".*/\1/p')"
if [ -n "$POLLING" ] && [ "$POLLING" != "ok" ]; then
    say ""
    say "⚠️  Бот запущен, но НЕ получает сообщения от Telegram:"
    say "    $POLLING"
    say ""
    say "    Причин две, и они лечатся по-разному:"
    say "    • бот ещё работает где-то ещё с тем же токеном — остановите там;"
    say "    • на токене стоит вебхук — бот снимает его сам при запуске,"
    say "      посмотрите journalctl -u ${SERVICE} -n 50"
    exit 1
fi

say ""
say "✅ Готово. Версия $GOT, служба ${SERVICE}, Telegram слышим."
say ""
say "Дальше:"
say "  • обновления —  sudo -u ${RUN_USER} DEPLOY_PATH=${REPO} bash ${REPO}/scripts/deploy.sh"
say "  • логи       —  journalctl -u ${SERVICE} -f"
say "  • проверка   —  отправьте боту /version"
