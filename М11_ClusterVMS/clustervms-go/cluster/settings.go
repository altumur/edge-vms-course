package cluster

// Settings are read from the environment (М8: credentials never live in
// the image). The subset М9's Settings has that the cluster host touches;
// the same variable names.

import (
	"os"
	"strconv"
)

type Settings struct {
	DatabaseURL       string
	ArchiveDir        string
	ColumnKeyFile     string
	Epoch             int64
	SegmentSeconds    int
	PollInterval      float64
	ReportInterval    float64
	MaxBackoff        float64
	StallFailures     int
	ConsoleAddr       string
	LeaseTTL          float64
	LeaseMargin       float64
	PublishFloor      float64
	HeartbeatInterval float64
	ReindexInterval   float64
	ObjectStoreURL    string
}

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func envF(name string, def float64) float64 {
	f, err := strconv.ParseFloat(env(name, ""), 64)
	if err != nil {
		return def
	}
	return f
}

func SettingsFromEnv() Settings {
	return Settings{
		DatabaseURL:       env("DATABASE_URL", "postgresql://recorder@127.0.0.1:5432/recorder"),
		ArchiveDir:        env("ARCHIVE_DIR", "/data/archive"),
		ColumnKeyFile:     env("COLUMN_KEY_FILE", "/data/config/column.key"),
		Epoch:             int64(envF("EPOCH", 1)),
		SegmentSeconds:    int(envF("SEGMENT_SECONDS", 600)),
		PollInterval:      envF("POLL_INTERVAL", 2),
		ReportInterval:    envF("REPORT_INTERVAL", 5),
		MaxBackoff:        envF("MAX_BACKOFF", 60),
		StallFailures:     int(envF("STALL_FAILURES", 3)),
		ConsoleAddr:       env("CONSOLE_HOST", "127.0.0.1") + ":" + env("CONSOLE_PORT", "8080"),
		LeaseTTL:          envF("LEASE_TTL", 30),   // Lesson 4: the numbers this course ships
		LeaseMargin:       envF("LEASE_MARGIN", 5), // stop_on_client_after = TTL − margin = 25 s
		PublishFloor:      envF("PUBLISH_FLOOR", 5),
		HeartbeatInterval: envF("HEARTBEAT_INTERVAL", 10),
		ReindexInterval:   envF("REINDEX_INTERVAL", 600),
		ObjectStoreURL:    env("OBJECT_STORE_URL", "file:///data/restore"),
	}
}
