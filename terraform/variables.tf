variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "us-central1-a"
}

variable "elastic_otlp_endpoint" {
  description = "Elastic OTLP ingest endpoint (e.g. https://xxxx.ingest.us-central1.gcp.elastic-cloud.com:443)"
  type        = string
  sensitive   = true
}

variable "elastic_api_key" {
  description = "Elastic API key for OTLP ingest (base64-encoded)"
  type        = string
  sensitive   = true
}

variable "allowed_ssh_cidr" {
  description = "CIDR range allowed to SSH into demo VMs (your IP)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "allowed_demo_cidr" {
  description = "CIDR range allowed to access portal (8080) and MuleSoft API (8081)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "use_preemptible" {
  description = "Use preemptible (spot) VMs to save cost"
  type        = bool
  default     = true
}

variable "owner_label" {
  description = "Owner label for GCP resources"
  type        = string
  default     = "aliu"
}
