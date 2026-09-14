"""The event index is the platform's, one per resource: psimplatform.eventindex.ResourceIndex, run inside
the resource job (`python3 -m cluster resource`). The console holds none — see `cluster.console.MergedIndex`.
Kept here as the name М11 used."""
from psimplatform.eventindex import EventIndex, ResourceIndex  # noqa: F401
from .console import MergedIndex  # noqa: F401
