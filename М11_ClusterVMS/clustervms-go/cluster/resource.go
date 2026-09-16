package cluster

// The resource job on a cluster is М10's resource process (vms.NewVmsResource)
// run as a `system` job on every server with meta.archive: the platform's
// Resource with the VMS registered on it — serving buckets, taking mirrors
// from its peers, retaining every subsystem's buckets by that subsystem's
// policy, and keeping the event database over its own tree. Nothing here is
// new; the names say so: platform/resources/<server>/heartbeat,
// platform/mirror, job "resource".
//
//	ClusterResource = vms.NewVmsResource: ArchivePolicy registered as the "rec" hook (the recorder's: manifest repair, media retention), an EventDatabase attached
//	VmsRoutes       = vms.ResourceRoutes:  GET /manifest/<cam>, GET /segment/<path> (Range honoured)

import (
	p "vmsserver/w2cplatform"
	"vmsserver/vms"
)

// VmsRoutes: the VMS's reads on the resource, plugged into the platform's server.
func VmsRoutes(archive *vms.ArchiveResource) p.Extra { return vms.ResourceRoutes(archive) }

// ClusterResource: the platform's resource for this server with the VMS registered on it, and the event database over its tree.
func ClusterResource(archive *vms.ArchiveResource, server, url string, vars Variables, objects ObjectStore, wall p.Clock, peers p.PeerClient) *p.Resource {
	return vms.NewVmsResource(archive, server, url, vars, objects, wall, peers)
}
