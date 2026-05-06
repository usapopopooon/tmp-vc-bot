FROM python:3.12-slim AS base

WORKDIR /app

# Force Python stdout/stderr to be unbuffered so logs appear in real time
# under Docker/Railway (otherwise stdout is block-buffered when not a TTY,
# and our app logs only flush when the process exits or the buffer fills).
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy source code
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .

# Default: run migrations, then start bot.
# echo マーカーで「どの段階まで進んだか」をシェルレベルで残す
# (Python の logging やバッファリングに左右されない)。
CMD ["sh", "-c", "echo '>>> CMD: container start' && \
echo '>>> CMD: running alembic upgrade head' && \
alembic upgrade head && \
echo '>>> CMD: alembic done; launching python -m src.main' && \
exec python -m src.main"]
