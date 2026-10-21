# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import subprocess,sys,tomllib; deps=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; subprocess.check_call([sys.executable,'-m','pip','install','setuptools>=68','wheel',*deps])"
COPY aura ./aura
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-build-isolation --no-deps .

EXPOSE 8080
CMD ["uvicorn", "aura.app:app", "--host", "0.0.0.0", "--port", "8080"]
