# М13_Observability — Module Design

**Seeing four layers you already built, from outside the one that failed.**

Every module before this one added a scope. This one adds none — it is how you see the four that exist, and **it is domain-level**: the remote observer is one more domain service, hosted beside the signer, and the module's thesis is М12's honesty about incomplete answers applied to metrics. It comes directly after the domain, and before the vendor, because the vendor module's demo cannot be *demonstrated* without it.

It also **does not introduce observability.** М9–М12 already emit signals, each defined where the failure that needed it was introduced, because a metric chosen at the moment you watch something break has a reason and one chosen in an observability chapter has only a name. This module collects them, and adds what is genuinely cross-cutting.

> **Scope note.** Four lessons, 38–41. This module sat *after* the vendor module through three restructures, each time with a note that the vendor module leaned on instrumentation it had not taught. Recognising the remote observer as a domain service settled it: observability follows the domain, and [М14](../М14_VendorVMS/module-design.md) inherits alerting rather than presupposing it — its *vendor disappears for thirty days* demo is measured here, and its canary halt condition is Lesson 3's alert rule.

---

## The thesis

Datacentre monitoring assumes something this product cannot: that you can reach the thing you are monitoring.

> **In a datacentre, no news is bad news. At the edge, no news is *no news*.**

A target that stops responding in a rack is broken. A site that stops responding might be broken, or its uplink might be down, or the building might have lost power, or a customer's IT department might have changed a firewall rule on a Friday. **Those are four different problems for four different people, and the signal is identical in every case: nothing.**

Everything hard in this module comes out of that sentence. A monitoring system that cannot distinguish *broken* from *unreachable* will either page somebody every time a router reboots, or stay silent through a genuine outage — and in practice it does both, which is how a team learns to ignore it.

### The observer's paradox

The rule everybody knows:

> **Monitoring must not share a failure domain with the thing it monitors.**

A Prometheus running as a Nomad job inside the domain it watches dies with that domain and cannot tell you it died. Obvious, and at the edge it collides with a second fact: **Prometheus pulls**, and you cannot pull across the link you stopped trusting. To see anything at a site, the scraper has to be *at* the site — inside the failure domain the rule just forbade.

Both halves are true, so there is no single correct place, and the resolution is that **there are two observers with different jobs**:

| | **Local** — in the cluster | **Remote** — in the domain cluster |
|---|---|---|
| Sees | everything, at full resolution | whether the site is alive, and a handful of aggregates |
| Dies with | the site | nothing the site can cause |
| Answers | *what broke* | ***whether* anything broke** |
| Retention | days, on the data partition | long, cheap, small |

Neither alone is sufficient, and the failure of most edge monitoring is picking one. **The remote observer's only job is to distinguish silence from health** — everything else it could do, the local one does better and cheaper.

Which is a rule this course already has, arriving for the fourth time:

> **Detail is local; summary is domain.** [`where-the-database-lives.md`](../М12_DomainVMS/where-the-database-lives.md) named it for footage, the archive index and events, and said "the fourth one will arrive eventually." **Metrics are the fourth.**

---

## The demo it is built backwards from

A wall showing four sites. Then, in order:

1. **A camera stops recording** while every control-plane field says it is healthy — and the console goes amber on `camera_silent_seconds` alone.
2. **A server dies.** Its Node moves. **One** alert fires, naming the server, not two hundred naming cameras.
3. **A cluster's uplink is cut.** The domain shows it as **unreachable, not healthy and not broken**, with the age of its last report and what is therefore unknown.
4. **The uplink returns.** The backlog drains at a stated rate, the gap in the graphs is visible and labelled, and nothing was invented to fill it.
5. Finally, a student greps the whole monitoring stack for a camera password **and finds nothing**, because Lesson 4 made that impossible.

If a lesson does not move that wall forward, it does not belong here.

---

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Collection | **Prometheus, pull, plus agent mode at constrained sites** | Apache 2.0, the ecosystem's default, and its agent mode is built for exactly this shape. |
| Topology | **Two observers: local full-resolution, remote aliveness plus aggregates** | The observer's paradox above. Neither position alone is correct. |
| What travels | **Aggregates and alarms, never raw series** | The thin uplink is already carrying the product's own publications; monitoring must not compete with footage. |
| Cardinality | **No per-object series.** Distributions, plus the database for detail | A thousand cameras is a thousand series per metric. A metric is not a database. |
| Alerting | **On the product, not the process** | М9 Lesson 3's rule, stated once for everything above it. |
| Silence | **A first-class state, never rendered as health** | The module's thesis. `unreachable` is not `up` and not `down`. |
| Logs | **Local, structured, and never containing a secret** | М9 Lesson 5 found the password inside `rtsp_url`; the leak is a formatting bug, not a storage one. |
| Dashboards | **Ship none. See the licensing section** | Grafana is **AGPLv3**, and §6 triggers on shipping at all — modified or not. This is a sharper constraint than the BUSL one the course already settled. |

---

## Prerequisites

- **М9 Lesson 3** — the health-check ladder, and *a health check is not monitoring*. This module supplies the other half of that distinction.
- **М9 Lesson 4** — `spool_oldest_seconds`, and why age beats count.
- **М9 Lesson 5** — events are not metrics; and the credential hiding in `rtsp_url`, which Lesson 4 collects on.
- **М9 Lesson 9** — `camera_lag` and `camera_silent_seconds`, and positions versus reasons.
- **М11 Lessons 8–9** and **М12 Lesson 1** — failover duration, epoch conflicts, replica lag.
- **М12 Lesson 8** — regions, because a fleet view spans them.

---

## What is already emitted

The module opens by taking inventory rather than by installing anything:

| From | Signal | Established there because |
|---|---|---|
| М9 L3 | the health-check ladder | it decides **rollback**, on the box, offline |
| М9 L4 | `spool_oldest_seconds`, `spool_bytes_used` | the spool was the first thing whose health was a *quantity* |
| М9 L5 | `camera_lag` (distribution), `camera_silent_seconds` | the second is the only one describing the product |
| М11 L4 | `node_failover_seconds`, `node_epoch_conflicts` | RTO, and a counter that should be zero forever |
| М12 L1 | `node_replica_lag_seconds` | the RPO made visible per Node |

**Six signals for a whole VMS.** That is the point, and it is worth saying to a student who expects a hundred: each one was chosen at the moment its failure was demonstrated, and nothing was added because it was easy to measure.

---

## The licensing problem, which is worse than Nomad's

The course has one settled licensing thread — Nomad under BUSL, resolved because the Additional Use Grant permits a non-competitive product to embed it. **The monitoring stack is a harder case and it needs stating before a student builds an appliance around a dashboard.**

| Component | Licence | Shippable in an appliance? |
|---|---|---|
| **Prometheus**, incl. agent mode | Apache 2.0 | Clean |
| **VictoriaMetrics**, single *and* cluster | Apache 2.0 | Clean |
| **Thanos**, **Cortex** | Apache 2.0 | Clean |
| **OpenTelemetry Collector** (core + contrib) | Apache 2.0 | Clean |
| **Grafana Alloy** | Apache 2.0 | Clean |
| **Grafana** | **AGPLv3** | **See below** |
| **Loki**, **Tempo**, **Mimir** | **AGPLv3** | Same |

Grafana, Loki and Tempo relicensed from Apache 2.0 to AGPLv3 in **April 2021**; Mimir has been AGPLv3 from the start. Agents were deliberately left Apache — which is why Alloy is clean and Grafana is not.

**Why this is sharper than BUSL.** BUSL's grant *permits* production use and forbids only a competing offering, so a VMS was simply outside its restriction. AGPL has no such carve-out:

- **§6 triggers on conveying at all** — modified or not. Putting an AGPL binary on hardware a customer buys **is** conveying, and you owe them the Corresponding Source: *"all the source code needed to generate, install, and (for an executable work) run the object code and to modify the work."* Note **install** and **modify**.
- **§13 triggers on modification** — and every user reaching your UI over the network must be offered the source of *your modified version*. Theming it, rebranding it, wiring it to your auth: all modification.
- **Grafana's free Enterprise binary does not solve it.** It is proprietary and its licence agreement forbids you to *"distribute, sublicense… or make available"* the object code to third parties. Redistribution needs an OEM agreement.

**And the question that actually decides it is one a VMS walks straight into**: a video product wants dashboards *inside* its own console, not on a separate port. The tighter that coupling, the more arguable it becomes that the proprietary VMS is a covered work. Loose coupling — separate process, network API, no linking, no embedding — is the standard mitigation, and it is a design constraint rather than a legal footnote.

> **The course's position: teach Prometheus, and ship no dashboard.** Build the console you already have — М9 Lesson 9 built one and М12 Lesson 3 extended it — and let Grafana be something the *customer* installs and points at your Apache-2.0 endpoint. That keeps every shipped component permissive, and it is a better product decision anyway: an operator should not need two consoles.

Not legal advice, and the anti-tivoisation clause in §6 deserves its own look for a secure-boot appliance. But a course that teaches students to embed Grafana in a product they sell, without saying this, has done them harm.

---

## Lessons

*Four lessons. The system already emits; this is how you see it from outside.*

### Lesson 1 — What you already emit, and what silence means

- **Take inventory first.** The six signals above, and for each one: what it cannot tell you. That column is the lesson
- Build the exporter: a `/metrics` endpoint on the Node, exposing what М9 Lesson 9 already computes. **No new measurements** — this is plumbing over decisions already taken
- Metric types, and picking them correctly: `camera_silent_seconds` is a **gauge**, `node_epoch_conflicts` is a **counter**, and confusing them makes the second unalertable
- **The three states of a target, kept apart:** `up`, `down`, and **`unknown`**. Prometheus gives you `up` for free and *nothing* for the third — the site whose scrape failed is indistinguishable from the site that is fine but unreachable. Deriving the third is this module's whole thesis in one expression
- **Staleness, and the honest gap.** When a Node returns after an outage, its metrics have a hole. Do not interpolate it, do not carry the last value forward — **render the gap**, because a flat line through an outage is a lie an operator will act on

**Deliverable:** one scrape endpoint per Node, and a written table of every signal with what it cannot tell you.

---

### Lesson 2 — Where the observer runs

- The observer's paradox from above, and the two-observer resolution
- **Prometheus agent mode** — introduced in 2.32 behind a feature flag, now the `--agent` CLI flag. It disables the local TSDB, alerting and rule evaluation and optimises for scraping plus remote write. Built for small resource-constrained deployments, which is what a site is
- **But at a site you usually want the opposite:** a *full* local Prometheus with real retention, because the moment you most need history is the moment the uplink is down. Agent mode fits the small site with no local operator; a recording site wants local storage. **Make students pick per site and justify it**
- **What crosses the uplink**, and the arithmetic beside М13's: metrics measured in kilobits per second against footage measured in megabits. Monitoring must not compete with the product for the link
- **Remote write**: v1.0 is the stable spec (April 2023) and mandates **Snappy** compression; v2.0 is still marked experimental. Say which you are using and why
- **Federation** is for pulling *selected*, typically *aggregated*, series between servers. The widespread advice not to federate everything is community practice rather than a documented warning — **teach the reasoning, and attribute it honestly**
- Where the *remote* observer lives: **the domain cluster**, as one more domain service — a different failure domain from every other cluster, and the same one as the signer. It is allowed to be down, so it may not be the thing that decides anything; and it watches the domain cluster itself only from *inside*, which is the one blind spot the design accepts and names

**Deliverable:** kill an entire cluster and show that the domain still reports it as unreachable within a stated window — and that nothing in that cluster stopped recording.

---

### Lesson 3 — Alarms somebody will act on

- **Alarm on the product, not the process.** М9 Lesson 3's ladder, generalised: every alert names a thing the customer paid for
- **Symptom, not cause.** One alert per failure, at the level a human acts on. М12's failure-domain grouping already does the work — a dead server is **one** alert, not two hundred
- **Every alert needs an action.** An alert with no runbook is a notification, and notifications train people to filter
- **The silence problem, properly.** Three cases, three different responses: the site is broken, the *link* is broken, the *monitoring* is broken. Deriving which needs a signal that does not travel over the failed path — a heartbeat, a second route, or an honest *"unknown since 14:02"*
- **Thresholds come from earlier arithmetic, not taste.** The spool alarm from М9's partition sizing, the replica-lag alarm from М12's stated RPO. Every number in this lesson was decided in an earlier module
- **Alert fatigue as a design failure**, measured: alerts per week per operator, and what fraction were acted on

**Deliverable:** an alert set in which every alert has a written action, plus a test that a silent site produces **exactly one** alert — and that the alert says *unknown*, not *down*.

---

### Lesson 4 — Logs, secrets, and one failure through four layers

The capstone, and it is a reading exercise as much as a building one.

- journald: retention on a data partition sized for footage, rate limiting, and why an unbounded log is a disk-full incident waiting for a busy night
- **Structured logs**, so a machine can filter what a human cannot read
- **Never log a secret — and М9 Lesson 5 showed why this is a formatting bug, not a storage one.** An RTSP URL carries `user:pass@` inline, so *any* code path that logs a URL leaks a credential: the pipeline description, the bus error, the exception, the support bundle. Grep for it, find the paths, and fix the composition rather than the log line
- **What travels upward:** alarms and counts, never log bodies. A site's logs stay at the site and are fetched on demand — the same shape as footage, index, events and metrics
- **The capstone:** induce one failure at each layer — a bad OS update (М9), a stalled camera (М9), a dead server (М11), an unreachable directory (М12) — and for each, state **which signal fired first, which fired second, and what the operator saw.** Then do it again with the uplink down

**Deliverable:** a support bundle a field engineer could actually use, containing no credentials — and the four-failure table, written.

---

## Verification plan

**Track 1 — verified in the authoring sandbox.** Most of it. Prometheus is a single binary with no dependencies; exporters, recording rules, alert rules and their unit tests (`promtool test rules`) are all files. Cardinality can be demonstrated by generating series and watching memory. The credential-grep exercise is `grep`.

**Track 2 — needs the bench.** Anything with a real uplink to cut: the two-observer topology, remote write under loss, and the backlog drain. And the capstone needs М9–М12 actually running.

**A note on honesty here:** this module can *assert* alert quality more easily than it can demonstrate it, and that is a trap. The deliverables are written tables and runbooks for exactly that reason — an alert set is judged by what a human does at 3am, which no test asserts.

---

## Open questions

1. **Does this module belong before М13?** Three of М13's lessons lean on instrumentation taught here. Emitting low softened it; the argument is not dead.
2. **One Prometheus per domain, or per site?** They are the same thing when a domain is one building and different when a domain is a cloud region serving fifty small sites — which М13's mixed deployment makes routine.
3. **Who watches the domain cluster?** The remote observer lives there, so it cannot see its own cluster from outside. The vendor could host a second-level heartbeat as a support service — but the product must not *depend* on it, so the honest answer may be that the domain cluster's death is discovered by a human noticing the console is gone.
4. **Is a customer-installed Grafana an acceptable answer**, or does the product need a dashboard it ships? The licensing section takes a position; a product manager may not accept it.
5. **What is the retention for metrics at a site?** Sized against the same partition as the spool and the archive, and therefore competing with footage for disk. Nobody has costed this.

---

## Sources

- [Prometheus LICENSE](https://raw.githubusercontent.com/prometheus/prometheus/main/LICENSE) — Apache 2.0
- [Prometheus agent mode](https://prometheus.io/docs/prometheus/latest/prometheus_agent/) — disables TSDB, alerting and rule evaluation; optimises for scrape and remote write · [launch post](https://prometheus.io/blog/2021/11/16/agent/) · [3.0 migration](https://prometheus.io/docs/prometheus/latest/migration/) — the feature flag became the `--agent` flag
- [Remote-Write 1.0 spec](https://prometheus.io/docs/specs/prw/remote_write_spec/) — stable, April 2023; Snappy compression mandatory · [2.0 spec](https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/) — **still marked experimental**
- [Federation](https://prometheus.io/docs/prometheus/latest/federation/) — *selected* series; hierarchical federation collects *aggregated* series. The "don't federate everything" advice is community practice, not a documented warning
- [Grafana LICENSE](https://raw.githubusercontent.com/grafana/grafana/main/LICENSE) · [Loki](https://raw.githubusercontent.com/grafana/loki/main/LICENSE) · [Tempo](https://raw.githubusercontent.com/grafana/tempo/main/LICENSE) · [Mimir](https://raw.githubusercontent.com/grafana/mimir/main/LICENSE) — all **AGPLv3**
- [Grafana relicensing announcement](https://grafana.com/blog/2021/04/20/grafana-loki-tempo-relicensing-to-agplv3/) — April 2021; *"Plugins, agents, and certain libraries will remain Apache-licensed"* · [CEO Q&A](https://grafana.com/blog/2021/04/20/qa-with-our-ceo-on-relicensing/) · [Grafana Labs License Agreement](https://grafana.com/legal/grafana-labs-license/) — the free Enterprise binary may not be redistributed
- [Grafana Alloy LICENSE](https://raw.githubusercontent.com/grafana/alloy/main/LICENSE) · [VictoriaMetrics](https://raw.githubusercontent.com/VictoriaMetrics/VictoriaMetrics/master/LICENSE) · [Thanos](https://raw.githubusercontent.com/thanos-io/thanos/main/LICENSE) · [Cortex](https://raw.githubusercontent.com/cortexproject/cortex/master/LICENSE) · [OTel Collector](https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector/main/LICENSE) — all Apache 2.0
- [`where-the-database-lives.md`](../М12_DomainVMS/where-the-database-lives.md) — detail is local, summary is domain, and the prediction that a fourth data type would arrive

*Written 7 September 2026.*
