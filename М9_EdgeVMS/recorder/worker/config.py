"""Configuration is read from the environment (М8 Lesson 6). Credentials
never live in the image; on the appliance the EnvironmentFile is on the data
partition (М9 Lesson 4)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL", "postgresql://recorder@127.0.0.1:5432/recorder"))
    archive_dir: str = field(default_factory=lambda: _env("ARCHIVE_DIR", "/data/archive"))
    column_key_file: str = field(default_factory=lambda: _env(
        "COLUMN_KEY_FILE", "/data/config/column.key"))
    epoch: int = field(default_factory=lambda: int(_env("EPOCH", "1")))

    # Lesson 7: segment length is a product decision. It bounds what a hard
    # kill loses (Lesson 8, Step 4) and sets how many index rows you write.
    segment_seconds: int = field(default_factory=lambda: int(_env("SEGMENT_SECONDS", "600")))
    watchdog_ms: int = field(default_factory=lambda: int(_env("WATCHDOG_MS", "8000")))
    rtsp_latency_ms: int = field(default_factory=lambda: int(_env("RTSP_LATENCY_MS", "200")))

    # Lesson 6, Step 4: one task per concern.
    poll_interval: float = field(default_factory=lambda: float(_env("POLL_INTERVAL", "2")))
    bus_tick: float = field(default_factory=lambda: float(_env("BUS_TICK", "0.2")))
    report_interval: float = field(default_factory=lambda: float(_env("REPORT_INTERVAL", "5")))
    max_backoff: float = field(default_factory=lambda: float(_env("MAX_BACKOFF", "60")))
    stall_failures: int = field(default_factory=lambda: int(_env("STALL_FAILURES", "3")))

    # Lesson 8, Step 3: retention and the disk-full policy.
    retention_interval: float = field(default_factory=lambda: float(_env("RETENTION_INTERVAL", "600")))
    partitions_ahead: int = field(default_factory=lambda: int(_env("PARTITIONS_AHEAD", "2")))
    disk_high_water: float = field(default_factory=lambda: float(_env("DISK_HIGH_WATER", "0.95")))
    disk_full_policy: str = field(default_factory=lambda: _env("DISK_FULL_POLICY", "degrade_retention"))

    # Lesson 9: the console.
    console_host: str = field(default_factory=lambda: _env("CONSOLE_HOST", "127.0.0.1"))
    console_port: int = field(default_factory=lambda: int(_env("CONSOLE_PORT", "8080")))
    session_ttl: float = field(default_factory=lambda: float(_env("SESSION_TTL", "43200")))

    def __post_init__(self) -> None:
        if self.disk_full_policy not in ("stop_recording", "degrade_retention", "by_priority"):
            raise ValueError(f"DISK_FULL_POLICY={self.disk_full_policy!r}: "
                             "expected stop_recording | degrade_retention | by_priority")
