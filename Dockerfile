# No browser: every panel operation runs over plain HTTP now — login via
# /token + /code, product creation through the Nova API, bump/restore/withdraw
# through the marketplace API. Dropping Chromium takes ~1.5 GB off the image
# and cuts the build from minutes to seconds.
FROM python:3.11-slim

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ .

CMD ["python", "main.py"]
