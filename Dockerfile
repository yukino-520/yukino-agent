FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YUKINO_HOST=0.0.0.0 \
    YUKINO_PORT=8082 \
    YUKINO_DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml README.md VERSION agent.py ./
COPY service_club ./service_club
ARG YUKINO_INSTALL_EXTRAS=storage
RUN pip install --no-cache-dir ".[${YUKINO_INSTALL_EXTRAS}]"

RUN mkdir -p /app/data /app/assets/stickers /app/assets/voice_refs
EXPOSE 8082
CMD ["agi-yukino", "serve", "--host", "0.0.0.0", "--port", "8082"]
