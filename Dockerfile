# Image ships Chromium so the panel login by email works (it is driven with a
# real browser — the site's login cannot be reproduced with plain requests).
#
# Chromium needs roughly 300-500 MB while a login is in progress, so on a
# 512 MB free instance memory is tight; the browser is launched in a lean
# single-process mode and closed as soon as the login finishes. To build a
# smaller image without it (cookie login only), pass --build-arg WITH_CHROMIUM=0
FROM python:3.11-slim

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG WITH_CHROMIUM=1
RUN if [ "$WITH_CHROMIUM" = "1" ]; then \
        playwright install-deps chromium && playwright install chromium; \
    fi

COPY bot/ .

CMD ["python", "main.py"]
