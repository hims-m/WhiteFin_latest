# WhiteFIN — one container that serves the API and the dashboard.
# Works locally (docker compose up) and on Hugging Face Spaces (Docker SDK).
#
# Hugging Face Spaces rules this file follows:
#   * the app must listen on port 7860 (README front matter: app_port: 7860)
#   * the container runs as a non-root user with UID 1000
#   * the disk is NOT persistent — the SQLite history/cache lives in /tmp and
#     is wiped when the Space restarts or sleeps (fine for a demo)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd -m -u 1000 user && mkdir -p /data && chown user:user /data
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

# Dependencies first, so code edits don't reinstall them on every build.
COPY --chown=user backend/requirements.txt backend/requirements.txt
RUN pip install --user -r backend/requirements.txt

COPY --chown=user backend backend
COPY --chown=user frontend frontend

WORKDIR /home/user/app/backend
ENV PORT=7860 \
    DATABASE_URL=sqlite:////tmp/whitefin.db
EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
