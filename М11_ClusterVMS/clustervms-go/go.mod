module clustervms

go 1.22

// М9's Go reconciler, imported not copied — the same relationship
// clustervms/ has to recorder/ in Python.
require recorder v0.0.0

replace recorder => ../../М9_EdgeVMS/recorder-go
