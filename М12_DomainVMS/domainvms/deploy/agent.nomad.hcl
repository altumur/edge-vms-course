# The domain agent: one per cluster. Its only right is to write domain/* in
# THIS cluster's Variables — the signer's public key set and the revocation
# list, copied from the domain cluster. When the domain is unreachable it
# stops updating; the cluster's console and gateway keep verifying with the keys they have.
job "domain-agent" {
  datacenters = ["*"]
  type        = "service"

  group "agent" {
    count = 1
    task "agent" {
      driver = "podman"
      config { image = "vms/domainvms:latest"; args = ["python3", "-m", "domain.agent"] }
      identity { env = true }    # bound to agent-policy.hcl: domain/* and nothing else
      template {
        data        = <<-EOT
          CLUSTER={{ env "NOMAD_REGION" }}
          DOMAIN_CONFIG_URL=nomad://nomad.north:4646   # the domain cluster, read through federation forwarding; a scheme, not a vendor
          NOMAD_ADDR=http://127.0.0.1:4646              # this cluster, written
          SYNC_INTERVAL=30
        EOT
        destination = "local/agent.env"
        env         = true
      }
      resources { cpu = 50  memory = 64 }
    }
  }
}
