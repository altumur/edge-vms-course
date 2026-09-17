package vms

// vmscontroller — the only writer of vms/*. It is the platform's
// SpecController run from vms.subsystem.yaml; this file is the VMS's
// vocabulary over it (camera, not unit) and nothing
// else. On one box it is a cluster of one: the snapshot still says which
// server, and it is the hostname.

import (
	p "vmsserver/w2cplatform"
)

var VMS = Spec.Sub()

// The VMS's names for the platform's things.
type Refused = p.Refused

var ErrNoSuchCamera = p.ErrNoSuchUnit

type Placement struct {
	Camera string
	Worker string
	Reason string
	At     float64
	Rev    int
}

type Move struct {
	Camera   string
	From, To string
}

type Unplaceable struct {
	ID          string   `json:"id"`
	Labels      []string `json:"labels"`
	WorkersLive int      `json:"workers_live"`
}

func placementOf(pl *p.Placement) *Placement {
	if pl == nil {
		return nil
	}
	return &Placement{pl.Unit, pl.Worker, pl.Reason, pl.At, pl.Rev}
}

func movesOf(ms []p.Move) []Move {
	out := []Move{}
	for _, m := range ms {
		out = append(out, Move{m.Unit, m.From, m.To})
	}
	return out
}

type VmsController struct{ *p.SpecController }

func NewVmsController(vars p.Variables, objects p.ObjectStore, capacity int, wall p.Clock) *VmsController {
	return &VmsController{p.NewSpecController(Spec, vars, objects, capacity, wall, "")}
}

// NewVmsControllerIn names the cluster the snapshot carries.
func NewVmsControllerIn(cluster string, vars p.Variables, objects p.ObjectStore, capacity int, wall p.Clock) *VmsController {
	return &VmsController{p.NewSpecController(Spec, vars, objects, capacity, wall, cluster)}
}

func (c *VmsController) CreateCamera(fields map[string]any) (Camera, error) {
	r, err := c.Create(fields)
	if err != nil {
		return Camera{}, err
	}
	return CameraOf(r), nil
}

func (c *VmsController) UpdateCamera(cid string, fields map[string]any) (Camera, error) {
	r, err := c.Update(cid, fields)
	if err != nil {
		return Camera{}, err
	}
	return CameraOf(r), nil
}

func (c *VmsController) DeleteCamera(cid string) error { return c.Delete(cid) }

func (c *VmsController) Camera(cid string) *Camera {
	r := c.Unit(cid)
	if r == nil {
		return nil
	}
	cam := CameraOf(r)
	return &cam
}

func (c *VmsController) Cameras() []Camera {
	out := []Camera{}
	for _, r := range c.Units() {
		out = append(out, CameraOf(r))
	}
	return out
}

func (c *VmsController) Placement(cid string) *Placement {
	return placementOf(c.SpecController.Placement(cid))
}

func (c *VmsController) Eligible(cam Camera, workers []string) []string {
	return c.SpecController.Eligible(RowOf(cam), workers)
}

func (c *VmsController) Place(cid string, workers []string) (*Placement, error) {
	pl, err := c.SpecController.Place(cid, workers)
	return placementOf(pl), err
}

func (c *VmsController) EnsurePlaced(workers []string) ([]Placement, error) {
	pls, err := c.SpecController.EnsurePlaced(workers)
	out := []Placement{}
	for i := range pls {
		out = append(out, *placementOf(&pls[i]))
	}
	return out, err
}

func (c *VmsController) Unplaceable() []Unplaceable {
	out := []Unplaceable{}
	for _, u := range c.SpecController.Unplaceable() {
		out = append(out, Unplaceable{p.Str(u.ID), u.Labels, u.WorkersLive})
	}
	return out
}

func (c *VmsController) Where(cid string) string { return c.SpecController.Where(cid) }

func (c *VmsController) UnplaceDeleted() []string {
	return c.SpecController.UnplaceDeleted()
}

func (c *VmsController) MoveTo(cid string, to, reason string) (Placement, error) {
	pl, err := c.SpecController.MoveTo(cid, to, reason)
	return *placementOf(&pl), err
}

func (c *VmsController) Redistribute(workers []string) []Move {
	return movesOf(c.SpecController.Redistribute(workers))
}

func (c *VmsController) Rebalance(budget int, deadBand float64, workers []string) []Move {
	return movesOf(c.SpecController.Rebalance(budget, deadBand, workers))
}
