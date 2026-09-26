---
genre: записки
kind: разбор кода
subject: М11_ClusterVMS
source-commit: e3b8f57
date: 2026-09-26
status: draft
---

# Часть 6. Подтверждение: как оператор убеждается, что всё встало

> [!note] Записки поверх репозитория, а не его документация
> Это разбор: я читал код и восстанавливал по нему, как всё устроено, максимально простыми словами.
> **Источник истины — код.** Где записки расходятся с кодом, прав код.
> Проект описывает себя сам: [`README.md`](../../README.md) и указатели модулей.
> Состояние: коммит `e3b8f57`, 26 сентября 2026.

[← карта разбора](README.md) · назад: [05-workers-and-epochs.md](05-workers-and-epochs.md) · вперёд: [07-edit-and-delete.md](07-edit-and-delete.md)


Три разных вопроса — три разных ответа, и источники у них разные.

## 6.1. «Покажи мне все камеры» — `GET /cameras`

Консоль отвечает из **двух** источников сразу, и это принципиально:

```
GET /cameras HTTP/1.1
Host: srv-1:8080
```

```json
{
  "rows": [
    {
      "id": 1,
      "ref": "north-gate",
      "name": "Ворота",
      "enabled": true,
      "phase": "running",
      "position": "converged",
      "revision": 1,
      "observed_revision": 1,
      "epoch": 1,
      "live_url": "rtsp://srv-1:8554/1",
      "live_shm": "shm:///run/vms/1.shm",
      "worker": "w-1",
      "server": "srv-1",
      "age": 2.1,
      "worker_state": "live"
    },
    {"id": 2, …, "worker": "w-2", "server": "srv-2", "age": 1.7, "worker_state": "live"},
    {"id": 3, …, "worker": "w-3", "server": "srv-3", "age": 3.4, "worker_state": "live"}
  ],
  "configured": [
    {
      "id": 1,
      "name": "Ворота",
      "source": "driverpack://acme/10.2.0.11",
      "enabled": true,
      "events_retention_days": 365,
      "priority": 100,
      "labels": ["vlan:cctv"],
      "folders": [],
      "alarms": [],
      "ref": "north-gate",
      "cred_username": "operator",
      "cred_secret": "***",
      "live": "always",
      "kind": "video",
      "revision": 1
    },
    {"id": 2, …},
    {"id": 3, …}
  ]
}
```

`configured` — из строк камер, это **желаемое**. Им заполняются формы редактирования.

`rows` — из heartbeat'ов воркеров, это **фактическое**. Отсюда берутся `phase`, `epoch`, `observed_revision` и возраст отчёта.

Смешивать их в один список нельзя, и причина простая: если сложить статус в хранилище настроек, получится кэш, который врёт — зелёная строчка в консоли над коробкой, которая ничего не пишет.

Что при этом ушло в Nomad:

```
# фактическое — дёшево, зависит от числа воркеров, а не камер
GET /v1/vars?prefix=objects/vms/heartbeats/   → 3 пути
GET /v1/var/objects/vms/heartbeats/w-1..w-3   → 3 объекта

# желаемое — дорого, по запросу на камеру
GET /v1/vars?prefix=vms/cameras/              → 3 пути
GET /v1/var/vms/cameras/1..3                  → 3 строки
```

На трёх камерах разницы не видно. На шестистах вторая половина стоит 601 чтение, а первая по-прежнему 13.

## 6.2. «Где камера 1 и почему» — `GET /where/1`

```
GET /where/1 HTTP/1.1
```

```json
{
  "worker": "w-1",
  "reason": "most free capacity (50) among 3 worker(s) reaching vlan:cctv; on srv-1, whose resource is live",
  "directory": "w-1",
  "scans": 1
}
```

В Nomad за этим ответом стоят два источника:

```
# решение контроллера
GET /v1/var/vms/placement/1                   → worker, reason

# кто камеру реально ведёт: все назначения подряд
GET /v1/vars?prefix=vms/workers/              → 3 пути
GET /v1/var/vms/workers/w-1..w-3              → units каждого
```

Здесь сверяются два независимых ответа на один вопрос.

`worker` — из строки размещения: **решение контроллера**.

`directory` — из назначений: **кто эту камеру реально ведёт**, полученное сканированием всех строк `vms/workers/*`.

В норме они совпадают. Если в `directory` окажется `"w-1+w-2"`, значит камера числится за двумя воркерами сразу — такое видно в окне переназначения и не должно застревать надолго.

`reason` — та самая причина, собранная при размещении. По ней видно и сколько было свободной ёмкости, и сколько воркеров рассматривалось, и что ресурс сервера в тот момент отвечал.

## 6.3. «Что с кластером в целом» — `GET /metrics`

```
GET /metrics HTTP/1.1
```

```
# TYPE vms_workers_live gauge
vms_workers_live 3
# TYPE vms_worker_headroom gauge
vms_worker_headroom{worker="w-1",server="srv-1"} 49
vms_worker_headroom{worker="w-2",server="srv-2"} 49
vms_worker_headroom{worker="w-3",server="srv-3"} 49
vms_headroom 147
# TYPE vms_worker_load gauge
vms_worker_load{worker="w-1"} 0.020
vms_worker_load{worker="w-2"} 0.020
vms_worker_load{worker="w-3"} 0.020
# TYPE vms_spare_workers gauge
vms_spare_workers 0
# TYPE vms_epoch_conflicts counter
vms_epoch_conflicts{worker="w-1"} 0
vms_epoch_conflicts{worker="w-2"} 0
vms_epoch_conflicts{worker="w-3"} 0
# TYPE vms_failover_seconds gauge
vms_failover_seconds{kind="worst"} 0.0
# TYPE vms_resources_live gauge
vms_resources_live 3
# TYPE vms_cameras_running gauge
vms_cameras_running 3
# TYPE vms_snapshot_age_seconds gauge
vms_snapshot_age_seconds 1.3
```

`vms_cameras_running 3` — это счёт записей в фазе `running` по живым воркерам, то есть тот же источник, что и `rows`.

`vms_spare_workers 0` — метрика общая для всех подсистем. Запасной воркер жив, но не держит ни одного места и ждёт, когда оно освободится. Места бывают у записи — это тома архива. У VMS место — это просто сервер, и запасных у неё не бывает, поэтому здесь всегда `0`. Запасной в `vms_worker_load` не попадает: его нулевая загрузка тянула бы среднее вниз и мешала бы масштабированию.

`vms_failover_seconds{kind="worst"}` — самое долгое переключение, которое видно по heartbeat'ам: разница между последним отчётом прежнего держателя имени (`previous_hb`) и стартом нового экземпляра (`started`). Здесь переключений не было, отсюда `0.0`.

`vms_snapshot_age_seconds` показывает, насколько устарела копия, которую читает доменный слой. Значение `-1` означало бы «снапшот не публиковался ни разу» — отдельный случай от «опубликован только что».

## 6.4. Что видит слой над кластером

Домен строк камер не читает: они принадлежат кластеру, и веер поштучных чтений через глобальную сеть был бы и медленным, и нарушением правила одного писателя. Он читает каталог шардов снапшота:

```
GET /v1/vars?prefix=objects/vms/snapshot/
→ ["objects/vms/snapshot/w-1", "objects/vms/snapshot/w-2", "objects/vms/snapshot/w-3"]

GET /v1/var/objects/vms/snapshot/w-1   → шард с камерой 1
GET /v1/var/objects/vms/snapshot/w-2   → шард с камерой 2
GET /v1/var/objects/vms/snapshot/w-3   → шард с камерой 3
```

Возрастом всей картины домен считает **самый старый** шард, а не самый свежий: каталог свеж настолько, насколько свежа его отставшая часть.

## 6.5. Что считать подтверждением

Три признака, и все три нужны:

| Признак | Где смотреть | Что означает |
|---|---|---|
| `vms/placement/<id>` существует и `worker` непустой | `GET /where/<id>` | контроллер решил, где камере работать |
| `phase: running` | `rows` в `GET /cameras` | воркер поднял пайплайн |
| `observed_revision == revision` | `rows` в `GET /cameras` | поднял именно на той конфигурации, что задана |

Четвёртый, необязательный, но полезный: `epoch` в статусе не равен нулю — воркер взял право писать эту камеру.

---

## 6.6. Вся временная шкала одним куском

| Время | Кто | Что сделал | Записал в хранилище |
|---|---|---|---|
| до сценария | воркеры | взяли слоты `w-1..w-3` | `vms/slots/w-1..3` |
| до сценария | воркеры | первый heartbeat | `objects/vms/heartbeats/w-1..3` |
| до сценария | ресурсы | отчитались о дисках | `objects/platform/resources/srv-1..3/heartbeat` |
| `T+0.0` | консоль | создала камеру 1 | `vms/idem/…`, `vms/next_id`, `vms/cameras/1`, `vms/retention/1` |
| `T+1.2` | консоль | создала камеру 2 | те же ключи, номер 2 |
| `T+2.0` | консоль | создала камеру 3 | те же ключи, номер 3 |
| `T+3.4` | контроллер | разместил все три | `vms/placement/1..3`, `vms/workers/w-1..3` |
| `T+3.7` | контроллер | опубликовал снапшот | `objects/vms/snapshot/w-1..3` |
| `T+4.1` | w-1 | прочитал назначение, взял эпоху, поднял поток | `vms/epoch/1` |
| `T+4.6` | w-2 | то же со своей камерой | `vms/epoch/2` |
| `T+5.0` | w-3 | то же со своей камерой | `vms/epoch/3` |
| `T+10.3` | w-1..w-3 | heartbeat с `phase: running` | `objects/vms/heartbeats/w-1..3` |
| `T+12.0` | оператор | увидел три работающие камеры | — |

**Сколько ждать в худшем случае.** Складываются четыре независимых интервала, и каждый взят из кода:

| Ожидание | Сколько | Откуда число |
|---|---|---|
| контроллер проснётся | до 5 с | `stop.wait(5)` в цикле контроллера |
| воркер проснётся | до 2 с | `poll = 2.0` в цикле воркера |
| воркер отчитается | до 10 с | heartbeat раз в 10 с |
| страница опросит консоль | до 10 с | опрос раз в 10 с в консоли |
| **итого до «зелёного» на экране** | **до 27 с** | сумма четырёх |

Типично получается около 10–14 секунд. Важно понимать, что из 27 секунд лишь 7 — это реальная задержка до запуска видео; остальные 20 уходят на то, чтобы факт дошёл до экрана.

### Сценарий: от создания камеры до подтверждения

```mermaid
sequenceDiagram
    actor OP as Оператор
    participant CON as Консоль
    participant NV as Nomad Variables
    participant CTL as Контроллер
    participant W1 as Воркер w-1

    OP->>CON: POST /cameras (камера 1)
    CON->>NV: занять ключ идемпотентности (cas=0)
    CON->>NV: выдать номер: next_id 0 → 1
    CON->>NV: записать vms/cameras/1 (cas=0), revision 1
    CON->>NV: записать vms/retention/1
    CON->>NV: положить ответ рядом с ключом
    CON-->>OP: 201, worker = null

    Note over OP,W1: Оператор свободен. Размещения ещё нет

    loop каждые 5 секунд
        CTL->>NV: прочитать камеры, heartbeat'ы, слоты, ресурсы
        NV-->>CTL: 3 камеры, 3 живых воркера по 50 свободных
    end
    CTL->>CTL: most-free-capacity: 1→w-1, 2→w-2, 3→w-3
    CTL->>NV: записать vms/placement/1 (cas=0)
    CTL->>NV: добавить "1" в vms/workers/w-1
    CTL->>NV: опубликовать шарды снапшота

    loop каждые 2 секунды
        W1->>NV: прочитать vms/workers/w-1 и строки своих камер
        NV-->>W1: камера 1, revision 1
    end
    W1->>NV: взять эпоху: vms/epoch/1 (cas=0) → 1
    W1->>W1: поднять пайплайн, rtsp://srv-1:8554/1
    W1->>NV: heartbeat: running, observed_revision 1

    OP->>CON: GET /cameras
    CON->>NV: прочитать heartbeat'ы и строки камер
    CON-->>OP: 3 камеры running, observed_revision = revision
```

---

[← карта разбора](README.md) · назад: [05-workers-and-epochs.md](05-workers-and-epochs.md) · вперёд: [07-edit-and-delete.md](07-edit-and-delete.md)
