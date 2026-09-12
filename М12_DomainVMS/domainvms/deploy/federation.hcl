# Several clusters, one domain: each cluster is a Nomad REGION; regions
# share no state and gossip-couple. `nomad server join` across regions
# federates them; a request to any region is forwarded to the right one.
# Nothing here replicates a worker, a Variable or an object between clusters.
#
# Servers of the south cluster (north is the domain cluster and the
# authoritative region for ACL policies):
region     = "south"
datacenter = "room-b"
data_dir   = "/opt/nomad/data"

server {
  enabled          = true
  bootstrap_expect = 3
  authoritative_region = "north"
  server_join {
    retry_join = ["nomad-1.north:4648", "nomad-1.south:4648"]   # WAN gossip to the other region's servers
  }
}

acl { enabled = true }
