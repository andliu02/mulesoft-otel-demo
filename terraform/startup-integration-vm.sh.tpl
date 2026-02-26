#!/bin/bash
# startup-integration-vm.sh.tpl — FNB Integration VM startup
# Installs Java, Mule Community Runtime, EDOT Java agent, OTel Collector
set -euo pipefail
exec > >(tee /var/log/demo-startup.log | logger -t startup-script) 2>&1

echo "=== FNB Integration VM Startup ==="
echo "Started: $(date)"
APP_VM_IP="${app_vm_internal_ip}"
ELASTIC_OTLP_ENDPOINT="${elastic_otlp_endpoint}"
ELASTIC_API_KEY="${elastic_api_key}"

# ── System packages ──────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq apt-transport-https ca-certificates curl gnupg lsb-release \
  wget unzip docker.io docker-compose-plugin

systemctl enable docker && systemctl start docker

# ── Java 17 (required for Mule 4.6) ─────────────────────────────────────────
echo "Installing Java 17..."
apt-get install -y -qq openjdk-17-jdk
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
echo "Java: $(java -version 2>&1 | head -1)"

# ── Mule Community Runtime ────────────────────────────────────────────────────
echo "Installing Mule 4.6 Community Runtime..."
mkdir -p /opt/edot /opt/mule-app

MULE_VERSION="4.6.1"
MULE_DIST="mule-community-standalone-$${MULE_VERSION}"
MULE_URL="https://repository.mulesoft.org/nexus/content/repositories/releases/org/mule/distributions/mule-community-standalone/$${MULE_VERSION}/$${MULE_DIST}.tar.gz"

wget -q -O /tmp/mule.tar.gz "$MULE_URL" || {
  echo "WARNING: Could not download Mule from official repo. Using mock runtime for demo."
  # Fallback: create a minimal mock that simulates Mule responses
  MULE_MOCK=true
}

if [ "$${MULE_MOCK:-false}" != "true" ]; then
  tar -xzf /tmp/mule.tar.gz -C /opt/
  ln -sf "/opt/$${MULE_DIST}" /opt/mule
  echo "Mule installed: $(ls /opt/mule/)"
fi

# ── EDOT Java Agent ───────────────────────────────────────────────────────────
echo "Downloading EDOT Java agent..."
EDOT_URL="https://github.com/elastic/elastic-otel-java/releases/latest/download/elastic-otel-javaagent.jar"
wget -q -O /opt/edot/elastic-otel-javaagent.jar "$EDOT_URL" || {
  echo "WARNING: Could not download EDOT agent. Will use vanilla OTel Java agent."
  wget -q -O /opt/edot/elastic-otel-javaagent.jar \
    "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar" || true
}
echo "EDOT agent: $(ls -lh /opt/edot/elastic-otel-javaagent.jar)"

# ── OTel Collector (also runs on integration-vm to receive EDOT from Mule) ───
echo "Starting OTel Collector..."
mkdir -p /opt/otel-collector

cat > /opt/otel-collector/otel-collector-config.yml << OTEL
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
    limit_mib: 256
    spike_limit_mib: 64
  resource:
    attributes:
      - action: insert
        key: demo.name
        value: fnb-mulesoft-otel
exporters:
  otlp/elastic:
    endpoint: "$ELASTIC_OTLP_ENDPOINT"
    headers:
      Authorization: "ApiKey $ELASTIC_API_KEY"
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

docker run -d \
  --name otel-collector \
  --restart unless-stopped \
  -p 4317:4317 -p 4318:4318 -p 8888:8888 \
  -v /opt/otel-collector/otel-collector-config.yml:/etc/otelcol/config.yml \
  -e ELASTIC_OTLP_ENDPOINT="$ELASTIC_OTLP_ENDPOINT" \
  -e ELASTIC_API_KEY="$ELASTIC_API_KEY" \
  otel/opentelemetry-collector-contrib:0.104.0 \
  --config=/etc/otelcol/config.yml

# ── Write Mule app properties (backend service IPs) ──────────────────────────
mkdir -p /opt/mule-app
cat > /opt/mule-app/mule-app.properties << PROPS
backend.corebanking.host=$APP_VM_IP
backend.corebanking.port=9001
backend.fraud.host=$APP_VM_IP
backend.fraud.port=9002
backend.aml.host=$APP_VM_IP
backend.aml.port=9003
backend.crm.host=$APP_VM_IP
backend.crm.port=9004
backend.notification.host=$APP_VM_IP
backend.notification.port=9005
PROPS

echo "Mule properties written | app_vm=$APP_VM_IP"

# ── EDOT environment variables for Mule JVM ──────────────────────────────────
cat > /etc/profile.d/mule-edot.sh << 'EDOTENV'
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export JAVA_TOOL_OPTIONS="-javaagent:/opt/edot/elastic-otel-javaagent.jar"
export OTEL_SERVICE_NAME="mulesoft-anypoint-runtime"
export OTEL_RESOURCE_ATTRIBUTES="service.version=4.6.1,deployment.environment=production,mule.app.name=fnb-integration,mule.runtime.version=4.6.1,mule.region=us-central1"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
export ELASTIC_OTEL_INFERRED_SPANS_ENABLED="true"
export ELASTIC_OTEL_INFERRED_SPANS_SAMPLING_INTERVAL="5ms"
export OTEL_INSTRUMENTATION_HTTP_SERVER_CAPTURE_REQUEST_HEADERS="X-Correlation-ID,X-Teller-ID,X-Branch-Code"
export OTEL_INSTRUMENTATION_HTTP_CLIENT_CAPTURE_REQUEST_HEADERS="X-Correlation-ID,X-Source-System"
EDOTENV

source /etc/profile.d/mule-edot.sh

# ── Start Mule (if installed) ─────────────────────────────────────────────────
if [ -d /opt/mule ]; then
  # Copy flow XMLs and properties to Mule apps dir
  mkdir -p /opt/mule/apps/fnb-integration
  cp /opt/mule-app/mule-app.properties /opt/mule/apps/fnb-integration/

  # NOTE: In production, copy flow XMLs here:
  # cp /path/to/repo/mulesoft/flows/*.xml /opt/mule/apps/fnb-integration/

  echo "Starting Mule Community Runtime with EDOT Java agent..."
  /opt/mule/bin/mule start

  sleep 15
  echo "Mule status: $(/opt/mule/bin/mule status 2>&1 || echo 'starting')"
else
  echo "Mule runtime not installed — manual install required."
  echo "See: mulesoft/edot-setup.md"
fi

PUBLIC_IP=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     FNB Integration VM — Ready                               ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  MuleSoft API:    http://$PUBLIC_IP:8081"
echo "║  OTel Collector:  grpc://$PUBLIC_IP:4317"
echo "║  App VM IP:       $APP_VM_IP"
echo "║  EDOT agent:      /opt/edot/elastic-otel-javaagent.jar"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo "  1. SSH in and verify: source /etc/profile.d/mule-edot.sh"
echo "  2. Copy flow XMLs:    cp *.xml /opt/mule/apps/fnb-integration/"
echo "  3. Restart Mule:      /opt/mule/bin/mule restart"
echo "  4. See mulesoft/edot-setup.md for full EDOT verification steps"
echo ""
echo "Startup complete: $(date)"
