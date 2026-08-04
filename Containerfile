FROM node:22-bookworm-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/var/lib/elastic-control \
    APP_CONFIG_DIR=/config \
    ANSIBLE_CONFIG=/opt/elastic-control/ansible/ansible.cfg

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates sshpass tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/elastic-control
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && ansible-galaxy collection install containers.podman ansible.posix
COPY app ./app
COPY --from=frontend-build /frontend/dist ./static
COPY ansible ./ansible
COPY tests ./tests
COPY tools ./tools
COPY reference ./reference

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
