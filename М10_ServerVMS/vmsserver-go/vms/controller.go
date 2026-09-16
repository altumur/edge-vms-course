package vms

// vmscontroller — the only writer of vms/*. It is the platform's
// SpecController run from vms.subsystem.yaml; this file is the VMS's
// vocabulary over it (camera, not unit; an int, not a string) and nothing
// else. On one box it is a cluster of one: the snapshot still says which
// server, and it is the hostname.

import (
	"strconv"

	p "vmsserver/w2cplatform"
)

var VMS = Spec.Sub()

// The VMS's names for the platform's things.
type Refused = p.Refused

var ErrNoSuchCamera = p.ErrNoSuchUnit

type Placement struct {
	Camera int
	Worker string
	Reason string
	At     float64
	Rev    int
}

type Move struct {
	Camera   int
	From, To string
}

type Unplaceable struct {
	ID          int      `json:"id"`
	Labels      []string `json:"labels"`
	WorkersLive int      `json:"workers_live"`
}

func placementOf(pl *p.Placement) *Placement {
	if pl == nil {
		return nil
	}
	n, _ := strconv.Atoi(pl.Unit)
	return &Placement{n, pl.Worker, pl.Reason, pl.At, pl.Rev}
}

func movesOf(ms []p.Move) []Move {
	out := []Move{}
	for _, m := range ms {
		n, _ := strconv.Atoi(m.Unit)
		out = append(out, Move{n, m.From, m.To})
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

func (c *VmsController) UpdateCamera(cid int, fields map[string]any) (Camera, error) {
	r, err := c.Update(strconv.Itoa(cid), fields)
	if err != nil {
		return Camera{}, err
	}
	return CameraOf(r), nil
}

func (c *VmsController) DeleteCamera(cid int) error { return c.Delete(strconv.Itoa(cid)) }

func (c *VmsController) Camera(cid int) *Camera {
	r := c.Unit(strconv.Itoa(cid))
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

func (c *VmsController) Placement(cid int) *Placement {
	return placementOf(c.SpecController.Placement(strconv.Itoa(cid)))
}

func (c *VmsController) Eligible(cam Camera, workers []string) []string {
	return c.SpecController.Eligible(RowOf(cam), workers)
}

func (c *VmsController) Place(cid int, workers []string) (*Placement, error) {
	pl, err := c.SpecController.Place(strconv.Itoa(cid), workers)
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
		out = append(out, Unplaceable{int(p.ToFloat(u.ID)), u.Labels, u.WorkersLive})
	}
	return out
}

func (c *VmsController) Where(cid int) string { return c.SpecController.Where(strconv.Itoa(cid)) }

func (c *VmsController) UnplaceDeleted() []int {
	out := []int{}
	for _, u := range c.SpecController.UnplaceDeleted() {
		n, _ := strconv.Atoi(u)
		out = append(out, n)
	}
	return out
}

func (c *VmsController) MoveTo(cid int, to, reason string) (Placement, error) {
	pl, err := c.SpecController.MoveTo(strconv.Itoa(cid), to, reason)
	return *placementOf(&pl), err
}

func (c *VmsController) Redistribute(workers []string) []Move {
	return movesOf(c.SpecController.Redistribute(workers))
}

func (c *VmsController) Rebalance(budget int, deadBand float64, workers []string) []Move {
	return movesOf(c.SpecController.Rebalance(budget, deadBand, workers))
}
