#!/usr/bin/env bash
#
# Установка бота БЕЗ прав администратора. Всё живёт в домашнем каталоге.
#
# Зачем: на сервере может не быть ни sudo, ни пароля root — так вышло при
# переезде 29.08. Обычный `setup-server.sh` там неприменим: он ставит пакеты
# и заводит системную службу, а на это прав нет.
#
# Чем этот путь отличается:
#
#   * **Python берётся свой.** `uv` скачивает готовую сборку CPython в
#     домашний каталог. Системный Python не нужен вовсе — а с ним отпадает и
#     вопрос, есть ли под него сборки зависимостей: версию выбираем мы.
#   * **git не нужен.** Если его нет, код скачивается архивом с GitHub.
#   * **Автозапуск — служба пользователя** (`systemctl --user`) с включённым
#     linger, чтобы она пережила выход и перезагрузку. Если systemd для
#     пользователя недоступен, ставится задание `@reboot` в cron, и об этом
#     говорится вслух: разные способы чинятся по-разному.
#
# Запускать ОТ ОБЫЧНОГО пользователя, без sudo. Скрипт повторно запускаемый:
# второй запуск обновляет код и перезапускает бота.
#
# Переменные:
#   DEPLOY_PATH   куда положить код (по умолчанию ~/yomarket)
#   DATA_DIR      где хранить данные (по умолчанию ~/yomarket-data)
#   REPO_URL      откуда брать (по умолчанию — репозиторий проекта)
#   DEPLOY_BRANCH ветка
#   PYTHON_VER    версия Python (по умолчанию 3.12 — под неё сборки есть у всего)

set -Eeuo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "этот скрипт для ОБЫЧНОГО пользователя.
От root пользуйтесь scripts/setup-server.sh — он заведёт системную службу."

REPO="${DEPLOY_PATH:-$HOME/yomarket}"
# Данные лежат ОТДЕЛЬНО от кода: повторный запуск перекачивает код заново, и
# данные внутри него были бы стёрты вместе со старой версией.
DATA="${DATA_DIR:-$HOME/yomarket-data}"
BRANCH="${DEPLOY_BRANCH:-claude/where-we-left-off-mul8tu}"
OWNER_REPO="${REPO_SLUG:-TAAAAAAAAAAAAAAAAmik/Yomarket}"
URL="${REPO_URL:-https://github.com/${OWNER_REPO}.git}"
PYVER="${PYTHON_VER:-3.12}"
UNIT="$HOME/.config/systemd/user/yomarket.service"

mkdir -p "$DATA"

# --- 1. uv ----------------------------------------------------------------
step "Ставлю uv"
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null; then
    say "uv уже есть: $(uv --version)"
else
    # Два источника. Официальный установщик живёт на astral.sh, но он может
    # быть недоступен — у провайдера свои маршруты, и проверять это на
    # середине установки поздно. Запасной путь идёт через GitHub, до
    # которого сервер точно достаёт: без этого код всё равно не забрать.
    if ! curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1; then
        say "astral.sh недоступен — беру uv с GitHub"
        case "$(uname -m)" in
            x86_64)  ARCH=x86_64-unknown-linux-gnu ;;
            aarch64) ARCH=aarch64-unknown-linux-gnu ;;
            *) die "неизвестная архитектура $(uname -m).
Посмотрите список сборок: https://github.com/astral-sh/uv/releases/latest" ;;
        esac
        mkdir -p "$HOME/.local/bin"
        curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-${ARCH}.tar.gz" \
            | tar xz -C "$HOME/.local/bin" --strip-components=1 --wildcards '*/uv' '*/uvx' \
            || die "не удалось скачать uv ни с astral.sh, ни с GitHub.
Проверьте интернет: curl -I https://github.com"
    fi
    hash -r
    command -v uv >/dev/null || die "uv поставился, но не нашёлся в PATH.
Выполните:  export PATH=\"\$HOME/.local/bin:\$PATH\"  и запустите снова."
    say "uv: $(uv --version)"
fi

# --- 2. Свой Python -------------------------------------------------------
step "Ставлю Python $PYVER"
uv python install "$PYVER" >/dev/null 2>&1 \
    || die "не удалось поставить Python $PYVER.
Посмотрите, что доступно: uv python list"
say "Python $PYVER на месте (сборка uv, системный не трогаем)"

# --- 3. Код ---------------------------------------------------------------
step "Забираю код"
if command -v git >/dev/null; then
    if [ -d "$REPO/.git" ]; then
        git -C "$REPO" fetch --prune origin "$BRANCH"
        git -C "$REPO" checkout -q "$BRANCH"
        git -C "$REPO" merge --ff-only "origin/$BRANCH"
    else
        git clone -q -b "$BRANCH" "$URL" "$REPO"
    fi
    say "код через git, ветка $BRANCH"
else
    # git нет и поставить его нечем — берём архивом. Обновление устроено так
    # же: скачали заново, распаковали поверх.
    TARBALL="https://codeload.github.com/${OWNER_REPO}/tar.gz/refs/heads/${BRANCH}"
    TMP="$(mktemp -d)"
    curl -fsSL "$TARBALL" | tar xz -C "$TMP" \
        || die "не удалось скачать код архивом.
Проверьте, что репозиторий доступен: curl -I $TARBALL"
    SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
    mkdir -p "$REPO"
    # Копируем поверх, не удаляя каталог целиком: .env и .venv остаются.
    cp -a "$SRC"/. "$REPO"/
    rm -rf "$TMP"
    say "код скачан архивом (git на сервере нет), ветка $BRANCH"
fi

# --- 4. Зависимости -------------------------------------------------------
step "Окружение и зависимости"
uv venv --python "$PYVER" "$REPO/.venv" >/dev/null \
    || die "не создалось окружение под Python $PYVER"
if ! uv pip install --python "$REPO/.venv/bin/python" \
        -q -r "$REPO/bot/requirements.txt"; then
    die "зависимости не поставились.

Посмотрите выше, какой пакет упал. Если под Python $PYVER для него нет
готовой сборки — возьмите другую версию:
    PYTHON_VER=3.11 bash $0"
fi
say "зависимости поставлены"

# --- 5. Секреты -----------------------------------------------------------
#
# `.env` не перезаписывается: в нём ключ, которым зашифрованы seed-фразы
# кошельков. Перезаписать его — потерять доступ к чужим деньгам, молча.
step "Секреты"
ENV_FILE="$REPO/.env"
if [ -f "$ENV_FILE" ]; then
    say "$ENV_FILE уже есть — не трогаю."
else
    ask() {
        local var="$1" hint="$2" required="${3:-}" value=""
        printf '\n%s\n> ' "$hint"
        read -r value </dev/tty
        if [ -z "$value" ] && [ -n "$required" ]; then
            die "$var обязательна — без неё бот не запустится"
        fi
        # Не `[ ... ] && printf`: это последняя строка функции, и при пустом
        # необязательном ответе она вернула бы 1 — а `set -e` убил бы скрипт
        # ровно там, где мы сами предложили ответить пусто.
        if [ -n "$value" ]; then
            printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
        fi
    }
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    ask BOT_TOKEN "Токен бота от @BotFather (обязательно):" yes
    ask SECRET_KEY "Ключ шифрования seed-фраз и ключей поставщиков.
ОБЯЗАТЕЛЕН: без него seed-фразы лежат в базе открытым текстом.
Сгенерировать: openssl rand -hex 32
При переезде вставьте ТОТ ЖЕ ключ, что был на старом сервере, иначе
зашифрованные фразы не расшифруются:" yes
    ask DATABASE_URL "Адрес PostgreSQL (postgresql://...).
Пусто — данные лягут в файлы в $DATA:"
    ask PORT "Порт для /health, например 8080.
Пусто — health не поднимется, и проверить версию будет нечем:"

    printf 'DATA_DIR=%s\n' "$DATA" >> "$ENV_FILE"
    say ""
    say ".env создан, права 600."
fi

# --- 6. Автозапуск --------------------------------------------------------
step "Автозапуск"
START="$REPO/.venv/bin/python"
MODE=""

if systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=YooMarket BOT
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO}/bot
EnvironmentFile=${ENV_FILE}
ExecStart=${START} main.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF
    systemctl --user daemon-reload
    systemctl --user enable yomarket >/dev/null 2>&1 || true
    systemctl --user restart yomarket
    MODE="systemd"

    # Без linger служба пользователя гаснет при выходе из SSH и не встаёт
    # после перезагрузки. Права на это есть не всегда — если нет, говорим.
    if loginctl enable-linger "$USER" >/dev/null 2>&1; then
        say "служба пользователя запущена, linger включён"
    else
        say "⚠️  служба запущена, но linger включить не удалось."
        say "    Бот остановится при выходе из SSH и не поднимется после"
        say "    перезагрузки сервера. Попросите включить:"
        say "        loginctl enable-linger $USER"
    fi
else
    # systemd для пользователя недоступен — остаётся cron. Он поднимает бота
    # после перезагрузки, но не перезапускает при падении: это хуже, и
    # молчать об этом нельзя.
    RUNNER="$REPO/run-bot.sh"
    cat > "$RUNNER" <<RUNEOF
#!/usr/bin/env bash
cd "${REPO}/bot" || exit 1
set -a; . "${ENV_FILE}"; set +a
exec "${START}" main.py >> "${DATA}/bot.log" 2>&1
RUNEOF
    chmod +x "$RUNNER"
    pkill -f "${START} main.py" 2>/dev/null || true
    nohup "$RUNNER" >/dev/null 2>&1 &
    ( crontab -l 2>/dev/null | grep -v 'run-bot.sh'; echo "@reboot $RUNNER" ) | crontab -
    MODE="cron"
    say "⚠️  systemd для пользователя недоступен — бот запущен через cron."
    say "    После перезагрузки поднимется, но при падении сам не"
    say "    перезапустится. Логи: ${DATA}/bot.log"
fi

# --- 7. Проверка ----------------------------------------------------------
#
# «Запустил» — не доказательство. Сверяем версию и слышит ли бот Telegram.
step "Проверяю"
sleep 6
pgrep -f "${START} main.py" >/dev/null \
    || die "процесс не поднялся.
Логи: $( [ "$MODE" = systemd ] && echo "journalctl --user -u yomarket -n 50" \
                               || echo "tail -50 ${DATA}/bot.log" )"
say "процесс жив"

WANT="$(sed -n 's/^BOT_VERSION *= *"\(.*\)".*/\1/p' "$REPO/bot/handlers/start.py" | head -1)"
PORT_VALUE="$(sed -n 's/^PORT=//p' "$ENV_FILE" | head -1)"
if [ -z "$PORT_VALUE" ]; then
    say ""
    say "⚠️  PORT не задан — сверить версию нечем. Бот при этом работает."
    exit 0
fi

BODY="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT_VALUE}/health" 2>/dev/null || true)"
GOT="$(printf '%s' "$BODY" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
[ -n "$GOT" ] || die "health на порту ${PORT_VALUE} не ответил."
[ "$GOT" = "$WANT" ] || die "поднялась версия «$GOT», а в коде «$WANT»."

POLLING="$(printf '%s' "$BODY" | sed -n 's/.*"polling" *: *"\([^"]*\)".*/\1/p')"
if [ -n "$POLLING" ] && [ "$POLLING" != "ok" ]; then
    say ""
    say "⚠️  Бот запущен, но НЕ получает сообщения: $POLLING"
    say "    Либо он работает где-то ещё с тем же токеном, либо на токене"
    say "    остался вебхук. Лечится это по-разному."
    exit 1
fi

say ""
say "✅ Готово. Версия $GOT, Telegram слышим, запуск через $MODE."
say ""
say "Обновление — этот же скрипт ещё раз:  bash $0"
[ "$MODE" = systemd ] \
    && say "Логи:  journalctl --user -u yomarket -f" \
    || say "Логи:  tail -f ${DATA}/bot.log"
