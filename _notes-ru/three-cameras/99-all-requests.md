---
genre: записки
kind: разбор кода
subject: М11_ClusterVMS
source-commit: 4099cd5
date: 2026-09-23
status: draft
---

# Приложение. Все запросы сценария одним списком

> [!note] Записки поверх репозитория, а не его документация
> Это разбор: я читал код и восстанавливал по нему, как всё устроено, максимально простыми словами.
> **Источник истины — код.** Где записки расходятся с кодом, прав код.
> Проект описывает себя сам: [`README.md`](../../README.md) и указатели модулей.
> Состояние: коммит `4099cd5`, 23 сентября 2026.

[← карта разбора](README.md) · назад: [10-limits-and-scale.md](10-limits-and-scale.md)


Порядок — временной. Ответы сокращены до существенного.

```
── подготовка (до прихода оператора) ───────────────────────────────────────
w-1  GET  /v1/vars?prefix=vms/slots/                        → []
w-1  GET  /v1/var/vms/slots/w-1                             → 404
w-1  PUT  /v1/var/vms/slots/w-1?cas=0                       → 200  holder, until, gen 1
w-1  PUT  /v1/var/objects/vms/heartbeats/w-1                → 200  status [], capacity 50
     …то же для w-2 и w-3
res  PUT  /v1/var/objects/platform/resources/srv-1/heartbeat → 200
     …то же для srv-2 и srv-3

── создание камеры 1 (T+0.0) ───────────────────────────────────────────────
OP   POST /cameras  Idempotency-Key: 8f4b21e0-…             → 201  id 1, worker null
con  PUT  /v1/var/vms/idem/8f4b21e0-…?cas=0                 → 200
con  GET  /v1/vars?prefix=vms/idem/                         → 1 путь   (подчистка)
con  GET  /v1/var/vms/next_id                               → 404
con  PUT  /v1/var/vms/next_id?cas=0                         → 200  n=1
con  PUT  /v1/var/vms/cameras/1?cas=0                       → 200  revision 1
con  GET  /v1/var/vms/retention/1                           → 404
con  PUT  /v1/var/vms/retention/1?cas=0                     → 200  days 365
con  PUT  /v1/var/vms/idem/8f4b21e0-…                       → 200  state done

── создание камер 2 и 3 (T+1.2, T+2.0) ─────────────────────────────────────
     те же семь вызовов, next_id 1→2 и 2→3

── проход контроллера (T+3.4) ──────────────────────────────────────────────
ctl  GET  /v1/vars?prefix=vms/placement/   ×2               → []   unplace_deleted, unplace_retired
ctl  GET  /v1/vars?prefix=vms/cameras/                      → 3 пути
ctl  GET  /v1/var/vms/cameras/1..3                          → 3 строки
ctl  GET  /v1/vars?prefix=objects/vms/heartbeats/           → 3 пути
ctl  GET  /v1/var/objects/vms/heartbeats/w-1..3             → server, labels, capacity
ctl  GET  /v1/vars?prefix=vms/slots/                        → 3 пути
ctl  GET  /v1/var/vms/slots/w-1..3                          → released false
ctl  GET  /v1/vars?prefix=objects/platform/resources/       → 3 пути
ctl  GET  /v1/var/objects/platform/resources/srv-1..3/…     → live
ctl  GET  /v1/var/vms/policy                                → 404 → shared
ctl  GET  /v1/var/platform/drain                            → 404 → никого
ctl  GET  /v1/var/vms/workers/w-1..3                        → 404 → load 0
ctl  PUT  /v1/var/vms/placement/1?cas=0                     → 200  w-1
ctl  PUT  /v1/var/vms/workers/w-1?cas=0                     → 200  units "1"
ctl  PUT  /v1/var/vms/placement/2?cas=0                     → 200  w-2
ctl  PUT  /v1/var/vms/workers/w-2?cas=0                     → 200  units "2"
ctl  PUT  /v1/var/vms/placement/3?cas=0                     → 200  w-3
ctl  PUT  /v1/var/vms/workers/w-3?cas=0                     → 200  units "3"
ctl  GET  /v1/vars?prefix=vms/placement/                    → 3 пути  redistribute проверяет их заново
ctl  GET  /v1/vars?prefix=vms/slots/                        → 3 пути  никто не отпускал — уходит
ctl  GET  /v1/vars?prefix=objects/rec/heartbeats/           → []      ensure_home: записи нет, дома нет
ctl  GET  /v1/vars?prefix=objects/vms/snapshot/             → []
ctl  PUT  /v1/var/objects/vms/snapshot/w-1..3               → 200  по шарду на воркера

── воркеры забирают камеры (T+4.1 … T+5.0) ─────────────────────────────────
w-1  GET  /v1/var/vms/workers/w-1                           → units "1", rev 1
w-1  GET  /v1/var/vms/cameras/1                             → revision 1
w-1  GET  /v1/var/vms/epoch/1                               → 404
w-1  PUT  /v1/var/vms/epoch/1?cas=0                         → 200  epoch 1
     …то же на w-2 (камера 2) и w-3 (камера 3)

── отчёт (T+10.3) ──────────────────────────────────────────────────────────
w-1  PUT  /v1/var/objects/vms/heartbeats/w-1                → running, observed_revision 1
     …то же на w-2 и w-3

── подтверждение (T+12.0) ──────────────────────────────────────────────────
OP   GET  /cameras                                          → rows 3 × running, configured 3
OP   GET  /where/1                                          → w-1 + причина + directory w-1
OP   GET  /metrics                                          → vms_cameras_running 3

── правка камеры 2 (T+20.0) ────────────────────────────────────────────────
OP   PUT  /cameras/2  Idempotency-Key: 3f9c1e2a-…           → 200  revision 2
con  PUT  /v1/var/vms/idem/3f9c1e2a-…?cas=0                 → 200
con  GET  /v1/var/vms/cameras/2                             → ModifyIndex 4421
con  PUT  /v1/var/vms/cameras/2?cas=4421                    → 200  revision 1→2
con  PUT  /v1/var/vms/idem/3f9c1e2a-…                       → 200  state done
w-2  GET  /v1/var/vms/workers/w-2                           → units "2"
w-2  GET  /v1/var/vms/cameras/2                             → revision 2 → применяет
w-2  PUT  /v1/var/objects/vms/heartbeats/w-2                → observed_revision 2

── удаление камеры 2 (T+30.0) ──────────────────────────────────────────────
OP   DELETE /cameras/2                                      → 200  deleted 2
con  GET  /v1/var/vms/cameras/2                             → есть и не помечена
con  GET  /v1/var/vms/cameras/2                             → ModifyIndex 5133
con  PUT  /v1/var/vms/cameras/2?cas=5133                    → 200  deleted true
con  GET  /v1/var/vms/retention/2                           → ModifyIndex 4424
con  PUT  /v1/var/vms/retention/2?cas=4424                  → 200  days 0
w-2  GET  /v1/var/vms/workers/w-2                           → units "2"  (ещё называет её)
w-2  GET  /v1/var/vms/cameras/2                             → deleted → пайплайн гаснет
w-2  PUT  /v1/var/objects/vms/heartbeats/w-2                → status []
ctl  PUT  /v1/var/vms/workers/w-2?cas=4452                  → 200  units пусто
ctl  PUT  /v1/var/vms/placement/2?cas=4449                  → 200  worker "", reason deleted
```

Всего за сценарий: **по семь запросов на каждое создание**, то есть 21 на три камеры, плюс подчистка ключей идемпотентности не чаще раза в минуту. Правка стоит консоли **четырёх** запросов, удаление — **пяти**, и ключа идемпотентности в удалении нет вовсе (разбор — в [части 7](07-edit-and-delete.md)). **Около сорока запросов контроллера** за один проход по логике — фактически заметно больше, потому что контроллер перечитывает heartbeat'ы при каждом обращении к данным воркера (см. [4.6](04-placement.md)). **По четыре запроса на воркера**, чтобы поднять камеру, и по одному heartbeat'у каждые десять секунд.

Обратите внимание на асимметрию в конце: **на удаление контроллер тратит два запроса, а воркер гасит видео раньше него** — на своём проходе, до того как размещение вообще тронули.

---

[← карта разбора](README.md) · назад: [10-limits-and-scale.md](10-limits-and-scale.md)
