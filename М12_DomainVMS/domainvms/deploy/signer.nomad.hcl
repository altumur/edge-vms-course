# The domain signer: one job, two keys (the CA and the token issuer), in the
# DOMAIN CLUSTER only. count = 1 is not exactly-one during a reschedule, and
# that is harmless here: same key, same signatures. The key lives in the
# Variable domain/signer — a software key on purpose; a TPM would pin the
# job to one server and defeat the failover it just gained.
job "domain-signer" {
  region      = "north"          # the domain cluster: a stated decision, recorded where the directory can report it
  datacenters = ["*"]
  type        = "service"

  group "signer" {
    count = 1

    # What the operator may say: a CONSTRAINT, never a server.
    constraint {
      attribute = "${meta.role}"
      operator  = "!="
      value     = "worker"       # not on a server carrying fifty cameras (meta.role, set in client.hcl)
    }

    network { port "https" {} }

    service {
      name     = "domain-signer"
      port     = "https"
      provider = "nomad"
    }

    task "signer" {
      driver = "podman"
      config { image = "vms/domainvms:latest"; args = ["python3", "-m", "domain.signer_service"] }
      identity { env = true }    # NOMAD_TOKEN: may write domain/signer, identity/*, domain/keys, domain/revoked
      template {
        data        = <<-EOT
          DOMAIN_ID=acme
          NOMAD_ADDR=http://127.0.0.1:4646
          OBJECT_STORE_URL=http://minio.north:9000/domain
          TOKEN_LIFETIME=900
          IDENTITY_PUBLISH_FLOOR=60
        EOT
        destination = "local/signer.env"
        env         = true
      }
      resources { cpu = 200  memory = 128 }
    }
  }
}
