FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 h3 \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin h3

WORKDIR /app
COPY requirements.lock.txt ./requirements.lock.txt
RUN python -m pip install --no-cache-dir -r requirements.lock.txt

# Deliberate allowlist: no local database/media, cloud controller, SSH keys,
# API vault, model weights, deployment records or user sessions enter the image.
COPY server.py password_auth.py comfy_workflow.py comfy-object-info.json ./
COPY tools/manage_users.py ./tools/manage_users.py
COPY web/ ./web/

USER 10001:10001
CMD ["python", "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8844", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-access-log"]
