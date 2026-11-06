FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY slim_demo ./slim_demo
COPY farm ./farm
COPY notes ./notes

ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "slim_demo.agent"]
