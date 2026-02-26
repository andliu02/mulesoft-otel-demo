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
  | gpg --batch --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
ARCH=$(dpkg --print-architecture)
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $CODENAME stable" \
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

# ── Clone repo and copy service source files ─────────────────────────────────
echo "Cloning service source code from GitHub..."
git clone https://github.com/andliu02/mulesoft-otel-demo.git /tmp/repo

# Copy portal files
cp -r /tmp/repo/fnb-portal/* "$APP_DIR/fnb-portal/"

# Copy backend service files
for SVC in core-banking-svc fraud-detection-svc aml-screening-svc customer-profile-svc notification-svc; do
  cp -r /tmp/repo/backend-services/$SVC/* "$APP_DIR/backend-services/$SVC/"
done

# Copy OTel collector config from repo (overwrite the inline one)
cp /tmp/repo/otel-collector/otel-collector-config.yml "$APP_DIR/otel-collector/otel-collector-config.yml"

rm -rf /tmp/repo
echo "Service source files copied from repo."

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
