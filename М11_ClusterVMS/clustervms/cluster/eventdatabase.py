"""The event database is the platform's, one per resource: psimplatform.eventdatabase.EventDatabase, run
inside the resource job (`python3 -m cluster resource`). The console holds none — MergedIndex asks and merges.
Kept here under the name the platform uses."""
from psimplatform.eventdatabase import EventDatabase, MergedIndex  # noqa: F401
