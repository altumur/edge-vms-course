# deploy/vms-scaler.nomad.hcl — the knob for a cluster with no metrics store.
#
# The Autoscaler closes the loop "an archive was declared -> a process exists to hold it" by reading
# Prometheus. A cluster that does not run one (М13 is where Prometheus is built) still has the number:
# the console publishes it on `/rec/volumes`. This job is that loop with one fewer component — ask the
# console, scale the recorder job — and it exists so that the answer to "who starts the worker" is never
# "nobody, until somebody notices".
#
# Run THIS or the Autoscaler, not both: two agents with opinions about one `count` will fight, and the
# fight looks like a job that scales out and in every minute.
#
# It is a job of the deployment, not of the platform. It holds a Nomad token; the console does not, and
# that is the line the whole design is drawn on (М10A, Lesson 7): the platform publishes a number,
# something with the scheduler's credentials acts on it.
job "vms-scaler" {
  datacenters = ["room-a"]
  type        = "service"

  group "scaler" {
    count = 1                                    # two would race on one `count`

    task "scaler" {
      driver = "podman"

      config {
        image   = "localhost/clustervms:latest"
        command = "/bin/sh"
        args    = ["-c", <<-EOS
          set -eu
          while true; do
            # The number, never the command. `/rec/volumes` also returns `how` — a line written out for a
            # person — and running a string that arrived over HTTP with a scheduler token in hand is
            # remote code execution with extra steps.
            needed=$(curl -fsS --max-time 5 "$CONSOLE/rec/volumes" \
                     | python3 -c 'import json,sys; print(max(0,int(json.load(sys.stdin).get("needed") or 0)))' || echo 0)
            if [ "$needed" -gt 0 ]; then
              live=$(nomad job status -json recworker | python3 -c 'import json,sys; print(len([a for a in json.load(sys.stdin).get("Allocations",[]) if a.get("ClientStatus")=="running"]))')
              want=$((live + needed))
              [ "$want" -le "$MAX_RECORDERS" ] || want=$MAX_RECORDERS
              echo "vms-scaler: $needed archive(s) unheld; scaling recworker to $want"
              nomad job scale recworker "$want" || true
            fi
            sleep "$INTERVAL"
          done
        EOS
        ]
      }

      env {
        CONSOLE        = "http://127.0.0.1:8080"   # the console runs on every server (`system` job)
        INTERVAL       = "60"                      # the same order as the Autoscaler's evaluation_interval
        MAX_RECORDERS  = "12"                      # its own ceiling: a wrong number upstream costs a log line
      }

      # `scale-job` on the namespace and nothing else — it cannot submit, stop or read secrets. Nomad OSS
      # scopes ACL at the namespace, so a cluster that wants this narrower puts the recorder in a
      # namespace of its own. See `vms-scaler-policy.hcl`.
      identity { env = true }

      resources { cpu = 50  memory = 64 }
    }

    # It never scales IN. Deciding a cluster has too many recorders is a capacity judgement with a
    # person's context behind it; this thing only knows that an archive has nobody to hold it.
    restart { attempts = 10  interval = "30m"  delay = "15s"  mode = "delay" }
  }
}
