FROM ghcr.io/astral-sh/uv:0.13.6-python3.12-trixie-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["daeri", "--host", "0.0.0.0", "--port", "8000"]
