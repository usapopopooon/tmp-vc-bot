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

# Default: run migrations, then start bot
CMD ["sh", "-c", "alembic upgrade head && python -m src.main"]
