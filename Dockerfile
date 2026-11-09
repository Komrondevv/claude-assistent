FROM python:3.12-slim

# The CLI flags and builtin plugin list were verified against this version.
ARG CLAUDE_VERSION=2.1.319

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PATH="/root/.local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
    && claude --version

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Runs as root because Railway mounts the Volume with root ownership.
CMD ["python", "bot.py", "--settings-from-env"]
