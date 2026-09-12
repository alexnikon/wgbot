FROM python:3.14.7-slim-bookworm@sha256:d893452fcd120ea9a7233972c85ea868255bde289a636fe76ff090427fe8fac9 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

RUN python -m pip install "uv==0.11.29"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.14.7-slim-bookworm@sha256:d893452fcd120ea9a7233972c85ea868255bde289a636fe76ff090427fe8fac9 AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

WORKDIR /app

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && python -m pip uninstall --yes pip setuptools wheel

COPY --from=builder /build/.venv /app/.venv
COPY --chown=app:app *.py ./
COPY --chown=app:app handlers ./handlers
COPY --chown=app:app scripts/backup_runtime.py ./scripts/backup_runtime.py

USER app

CMD ["python", "app.py"]
