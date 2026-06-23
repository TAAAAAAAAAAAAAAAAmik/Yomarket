FROM python:3.11-slim

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all its system dependencies
RUN playwright install-deps chromium && playwright install chromium

COPY bot/ .

CMD ["python", "main.py"]
