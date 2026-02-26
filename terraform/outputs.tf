output "app_vm_public_ip" {
  description = "Public IP of the app VM (portal + backend services)"
  value       = google_compute_instance.app_vm.network_interface[0].access_config[0].nat_ip
}

output "integration_vm_public_ip" {
  description = "Public IP of the integration VM (MuleSoft + OTel Collector)"
  value       = google_compute_instance.integration_vm.network_interface[0].access_config[0].nat_ip
}

output "portal_url" {
  description = "FNB Portal URL"
  value       = "http://${google_compute_instance.app_vm.network_interface[0].access_config[0].nat_ip}:8080"
}

output "mulesoft_api_url" {
  description = "MuleSoft API URL"
  value       = "http://${google_compute_instance.integration_vm.network_interface[0].access_config[0].nat_ip}:8081"
}

output "ssh_app_vm" {
  description = "SSH command for app VM"
  value       = "gcloud compute ssh aliu-fnb-app-vm --zone=${var.zone}"
}

output "ssh_integration_vm" {
  description = "SSH command for integration VM"
  value       = "gcloud compute ssh aliu-fnb-integration-vm --zone=${var.zone}"
}
