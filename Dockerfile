FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8741 \
    DB_PATH=/data/trader.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py ./
COPY trader ./trader

EXPOSE 8741
# Ét proces og workers=1 i run.py, så motortråden og SQLite ikke deles.
CMD ["python", "run.py"]
