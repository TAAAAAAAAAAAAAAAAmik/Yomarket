# Lean image for free hosting (Koyeb/Render, 512 MB). No Chromium — the bot
# runs fully; panel login via email (Playwright) is unavailable here, use the
# "🍪 Вставить cookies" button instead. To enable the browser on a bigger host
# (e.g. Oracle Cloud), build with:  --build-arg WITH_CHROMIUM=1
FROM python:3.11-slim

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG WITH_CHROMIUM=0
RUN if [ "$WITH_CHROMIUM" = "1" ]; then \
        playwright install-deps chromium && playwright install chromium; \
    fi

COPY bot/ .

CMD ["python", "main.py"]
