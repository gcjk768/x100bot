FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISABLE_AUTOUPDATER=1

# exiftool reads the Fujifilm settings; curl, ca-certificates and git are for the Claude Code installer
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git libimage-exiftool-perl \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY x100bot ./x100bot
COPY camera ./camera
COPY library ./library
COPY prompts ./prompts
COPY tests ./tests
# editable install keeps the code in /app, next to camera/, library/, prompts/ and the mounted config.yaml and data/
RUN pip install -e . && pip install numpy opencv-python-headless pillow-heif matplotlib

# UID matches the NAS user that owns ./data, so the bot can write its database and the teacher's photos
ARG UID=1000
RUN useradd -m -u ${UID} app && mkdir -p /app/data && chown -R app /app
USER app
RUN curl -fsSL https://claude.ai/install.sh | bash -s stable
ENV PATH=/home/app/.local/bin:$PATH
RUN claude --version

CMD ["x100bot", "serve"]
