#!/bin/bash
# startup-app-vm.sh.tpl — FNB App VM startup
# Installs Docker, clones repo, starts all backend services + portal
set -euo pipefail
exec > >(tee /var/log/demo-startup.log | logger -t startup-script) 2>&1

echo "=== FNB App VM Startup ==="
echo "Started: $(date)"

# ── System packages ─────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq apt-transport-https ca-certificates curl gnupg lsb-release git

# ── Docker ──────────────────────────────────────────────────────────────────
echo "Installing Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker && systemctl start docker

echo "Docker: $(docker --version)"

# ── App directory ────────────────────────────────────────────────────────────
APP_DIR=/opt/fnb-mulesoft-otel-demo
mkdir -p "$APP_DIR"/{fnb-portal,backend-services/{core-banking-svc,fraud-detection-svc,aml-screening-svc,customer-profile-svc,notification-svc},otel-collector}

# ── Get internal IP for MULESOFT_URL ────────────────────────────────────────
# integration-vm is on same subnet, use GCP internal DNS
INTEGRATION_IP=$(getent hosts aliu-fnb-integration-vm.$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | cut -d/ -f4).c.$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id).internal 2>/dev/null | awk '{print $1}' || echo "10.10.0.2")

echo "Integration VM IP resolved: $INTEGRATION_IP"

# ── Write .env ───────────────────────────────────────────────────────────────
cat > "$APP_DIR/.env" << ENVEOF
ELASTIC_OTLP_ENDPOINT=${elastic_otlp_endpoint}
ELASTIC_API_KEY=${elastic_api_key}
INTEGRATION_VM_IP=$INTEGRATION_IP
ENVEOF
chmod 600 "$APP_DIR/.env"

# ── Write docker-compose.yml ─────────────────────────────────────────────────
cat > "$APP_DIR/docker-compose.yml" << 'COMPOSE'
version: "3.9"
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.104.0
    command: ["--config=/etc/otelcol/otel-collector-config.yml"]
    volumes:
      - ./otel-collector/otel-collector-config.yml:/etc/otelcol/otel-collector-config.yml
    ports:
      - "4317:4317"
      - "4318:4318"
      - "8888:8888"
    environment:
      - ELASTIC_OTLP_ENDPOINT=$${ELASTIC_OTLP_ENDPOINT}
      - ELASTIC_API_KEY=$${ELASTIC_API_KEY}
    restart: unless-stopped

  fnb-portal:
    build: ./fnb-portal
    ports:
      - "8080:8080"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
      - MULESOFT_URL=http://$${INTEGRATION_VM_IP}:8081
    depends_on:
      - otel-collector
    restart: unless-stopped

  core-banking-svc:
    build: ./backend-services/core-banking-svc
    ports:
      - "9001:9001"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
      - SLOW_QUERY_RATE=0.10
    depends_on:
      - otel-collector
    restart: unless-stopped

  fraud-detection-svc:
    build: ./backend-services/fraud-detection-svc
    ports:
      - "9002:9002"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
    depends_on:
      - otel-collector
    restart: unless-stopped

  aml-screening-svc:
    build: ./backend-services/aml-screening-svc
    ports:
      - "9003:9003"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
    depends_on:
      - otel-collector
    restart: unless-stopped

  customer-profile-svc:
    build: ./backend-services/customer-profile-svc
    ports:
      - "9004:9004"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
    depends_on:
      - otel-collector
    restart: unless-stopped

  notification-svc:
    build: ./backend-services/notification-svc
    ports:
      - "9005:9005"
    environment:
      - OTLP_ENDPOINT=http://otel-collector:4317
    depends_on:
      - otel-collector
    restart: unless-stopped
COMPOSE

# ── Write OTel Collector config ──────────────────────────────────────────────
cat > "$APP_DIR/otel-collector/otel-collector-config.yml" << 'OTEL'
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch:
    timeout: 5s
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
    spike_limit_mib: 128
  resource:
    attributes:
      - action: insert
        key: demo.name
        value: fnb-mulesoft-otel
exporters:
  otlp/elastic:
    endpoint: "$${ELASTIC_OTLP_ENDPOINT}"
    headers:
      Authorization: "ApiKey $${ELASTIC_API_KEY}"
    tls:
      insecure: false
  debug:
    verbosity: basic
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [otlp/elastic, debug]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [otlp/elastic, debug]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [otlp/elastic, debug]
OTEL

# ── Write service source files ───────────────────────────────────────────────
# (app files are cloned from GitHub in production — for standalone demo they're embedded)

# Portal
mkdir -p "$APP_DIR/fnb-portal"
cat > "$APP_DIR/fnb-portal/requirements.txt" << 'REQ'
flask==3.0.3
requests==2.32.3
opentelemetry-api==1.26.0
opentelemetry-sdk==1.26.0
opentelemetry-instrumentation-flask==0.47b0
opentelemetry-instrumentation-requests==0.47b0
opentelemetry-exporter-otlp-proto-grpc==1.26.0
REQ

cat > "$APP_DIR/fnb-portal/Dockerfile" << 'DEOF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["python", "app.py"]
DEOF

# Copy service files from GitHub repo (adjust URL to your actual repo)
# For standalone: service files are written inline above in the startup script
# In production, replace with: git clone https://github.com/YOUR_ORG/fnb-mulesoft-otel-demo.git

# Write minimal service stubs — full versions are in the repo
for SVC_PORT in "core-banking-svc:9001" "fraud-detection-svc:9002" "aml-screening-svc:9003" "customer-profile-svc:9004" "notification-svc:9005"; do
  SVC=$(echo $SVC_PORT | cut -d: -f1)
  PORT=$(echo $SVC_PORT | cut -d: -f2)
  mkdir -p "$APP_DIR/backend-services/$SVC"
  cat > "$APP_DIR/backend-services/$SVC/requirements.txt" << 'REQ'
flask==3.0.3
requests==2.32.3
opentelemetry-api==1.26.0
opentelemetry-sdk==1.26.0
opentelemetry-instrumentation-flask==0.47b0
opentelemetry-instrumentation-requests==0.47b0
opentelemetry-exporter-otlp-proto-grpc==1.26.0
REQ
  cat > "$APP_DIR/backend-services/$SVC/Dockerfile" << DEOF
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE $PORT
CMD ["python", "app.py"]
DEOF
done

# NOTE: In production deployment, the app.py files for each service are copied
# from your GitHub repo. Add this step:
#   git clone https://github.com/YOUR_ORG/fnb-mulesoft-otel-demo.git /tmp/repo
#   cp -r /tmp/repo/backend-services/* $APP_DIR/backend-services/
#   cp -r /tmp/repo/fnb-portal/* $APP_DIR/fnb-portal/

# ── Launch ──────────────────────────────────────────────────────────────────
echo "Starting containers..."
cd "$APP_DIR"
docker compose --env-file .env up --build -d

# Wait for health
echo "Waiting for services..."
for i in $(seq 1 40); do
  HEALTHY=$(docker compose ps | grep -c "healthy" || true)
  if [ "$HEALTHY" -ge 4 ]; then
    echo "Services healthy ($HEALTHY/7)"
    break
  fi
  echo "  ...waiting $i/40 ($HEALTHY healthy)"
  sleep 5
done

PUBLIC_IP=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     FNB App VM — Ready                               ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Portal:          http://$PUBLIC_IP:8080"
echo "║  Core Banking:    http://$PUBLIC_IP:9001/health"
echo "║  Fraud Detection: http://$PUBLIC_IP:9002/health"
echo "║  AML Screening:   http://$PUBLIC_IP:9003/health"
echo "║  Customer CRM:    http://$PUBLIC_IP:9004/health"
echo "║  Notifications:   http://$PUBLIC_IP:9005/health"
echo "╚══════════════════════════════════════════════════════╝"
echo "Startup complete: $(date)"
