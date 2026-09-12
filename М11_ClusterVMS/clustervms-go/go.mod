module clustervms

go 1.22

// М10's platform and VMS, imported not copied — the same relationship
// clustervms/ has to vmsserver/ in Python.
require vmsserver v0.0.0

replace vmsserver => ../../М10_ServerVMS/vmsserver-go
