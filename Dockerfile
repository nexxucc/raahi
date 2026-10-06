FROM node:22-alpine AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

COPY --from=frontend /build/web/dist ./web/dist

ENV TRACKING_MODE=simulated \
    DATABASE_PATH=/tmp/raahi/transit.sqlite3

EXPOSE 10000
CMD ["sh", "-c", "python -m app.replay --scenario weekday-rush && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
