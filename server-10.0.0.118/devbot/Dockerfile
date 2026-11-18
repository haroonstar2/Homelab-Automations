FROM node:22-bookworm-slim AS node_runtime
FROM golang:1.26.4-bookworm AS go_runtime

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Use the project-compatible official Node runtime rather than Debian's older
# nodejs/npm packages. The target repository currently requires Node >=22.
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=go_runtime /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version \
    && go version

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock && pip check

COPY devbot ./devbot

# uid 1000 matches the host user that owns /mnt/Storage1/devbot
RUN useradd --create-home --uid 1000 devbot
USER devbot

CMD ["python", "-m", "devbot"]
