terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# ── Service Account ─────────────────────────────────────────────────────────
resource "google_service_account" "demo" {
  account_id   = "aliu-fnb-demo"
  display_name = "FNB MuleSoft OTel Demo"
}

resource "google_project_iam_member" "demo_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.demo.email}"
}

# ── VPC Network ─────────────────────────────────────────────────────────────
resource "google_compute_network" "demo" {
  name                    = "aliu-fnb-demo-network"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "demo" {
  name          = "aliu-fnb-demo-subnet"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.demo.id
}

# ── Firewall Rules ───────────────────────────────────────────────────────────

# SSH from your IP
resource "google_compute_firewall" "ssh" {
  name    = "aliu-fnb-demo-ssh"
  network = google_compute_network.demo.name
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = [var.allowed_ssh_cidr]
  target_tags   = ["aliu-fnb-demo"]
}

# Public-facing demo ports (portal UI + MuleSoft API)
resource "google_compute_firewall" "public_ports" {
  name    = "aliu-fnb-demo-public-ports"
  network = google_compute_network.demo.name
  allow {
    protocol = "tcp"
    ports    = ["8080", "8081"]
  }
  source_ranges = [var.allowed_demo_cidr]
  target_tags   = ["aliu-fnb-demo"]
}

# Internal VM-to-VM traffic — app-vm backend services
# integration-vm (MuleSoft) → app-vm (core-banking, fraud, aml, crm, notification)
resource "google_compute_firewall" "internal" {
  name    = "aliu-fnb-demo-internal"
  network = google_compute_network.demo.name
  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }
  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }
  allow {
    protocol = "icmp"
  }
  source_ranges = ["10.10.0.0/24"]
  target_tags   = ["aliu-fnb-demo"]
}

# ── Integration VM (MuleSoft + OTel Collector) ───────────────────────────────
resource "google_compute_instance" "integration_vm" {
  name         = "aliu-fnb-integration-vm"
  machine_type = "e2-standard-2"
  zone         = var.zone
  tags         = ["aliu-fnb-demo"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.demo.id
    access_config {}    # ephemeral public IP
  }

  service_account {
    email  = google_service_account.demo.email
    scopes = ["cloud-platform"]
  }

  scheduling {
    preemptible         = var.use_preemptible
    on_host_maintenance = var.use_preemptible ? "TERMINATE" : "MIGRATE"
    automatic_restart   = var.use_preemptible ? false : true
  }

  labels = {
    env     = "demo"
    project = "aliu-fnb-mulesoft-otel"
    role    = "integration"
    owner   = var.owner_label
  }

  metadata_startup_script = templatefile("${path.module}/startup-integration-vm.sh.tpl", {
    elastic_otlp_endpoint = var.elastic_otlp_endpoint
    elastic_api_key       = var.elastic_api_key
    app_vm_internal_ip    = google_compute_instance.app_vm.network_interface[0].network_ip
  })

  # integration-vm must know app-vm's internal IP → depends on app-vm being created first
  depends_on = [google_compute_instance.app_vm]
}

# ── App VM (FNB Portal + Backend Services) ────────────────────────────────────
resource "google_compute_instance" "app_vm" {
  name         = "aliu-fnb-app-vm"
  machine_type = "e2-standard-4"
  zone         = var.zone
  tags         = ["aliu-fnb-demo"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 40
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.demo.id
    access_config {}    # ephemeral public IP
  }

  service_account {
    email  = google_service_account.demo.email
    scopes = ["cloud-platform"]
  }

  scheduling {
    preemptible         = var.use_preemptible
    on_host_maintenance = var.use_preemptible ? "TERMINATE" : "MIGRATE"
    automatic_restart   = var.use_preemptible ? false : true
  }

  labels = {
    env     = "demo"
    project = "aliu-fnb-mulesoft-otel"
    role    = "app"
    owner   = var.owner_label
  }

  metadata_startup_script = templatefile("${path.module}/startup-app-vm.sh.tpl", {
    elastic_otlp_endpoint = var.elastic_otlp_endpoint
    elastic_api_key       = var.elastic_api_key
    integration_vm_ip     = ""   # populated post-boot via internal DNS
  })
}
