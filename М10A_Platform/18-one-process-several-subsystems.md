# Урок 18 — Один процесс, несколько подсистем

**Модуль:** М10A — Платформа (ServerVMS, часть первая)
**Вы напишете:** записи консоли (`create`, `update`, `delete`, `mark`), `/servers`, `/policy`, `/metrics`, расширения подсистемы (`_extra`), ветки POST/PUT/DELETE в `dispatch` — и класс `Mount`, после которого новая подсистема становится путём.
**Время:** ~90 минут.

## Зачем этот урок

Консоль из урока 17 умеет только смотреть. Это половина: оператор создаёт единицы, администратор крутит политику, страница просит метрики. Все эти записи короткие, и почти все они — один и тот же жест: позвать метод контроллера, поймать `Refused`, вернуть код.

Интересно в них не это, а то, **чего в них нет**. Ни одна запись консоли не размещает. `create` возвращает строку с `worker: None` — не потому, что ещё не успели посчитать, а потому что консоли нечем: у её токена нет прав на `*/assignment/*` (урок 3). Размещение случится на следующем проходе контроллера, и это видно прямо в ответе.

Вторая половина урока — `Mount`, сорок строк, ради которых написано всё остальное. К концу М10B подсистем будет четыре: `vms`, `rec`, `det`, `an`. Вариантов было три: четыре процесса на четырёх портах (оператору четыре вкладки), одна консоль, знающая про все четыре (обратно к монолиту), или **один процесс, в котором каждая подсистема — путь**. Третий вариант стоит сорок строк, и после него новая подсистема добавляется строкой `mount("det", console)` — без единой правки ни в консоли, ни в странице.

> **Проверка без железа.** Вся эта глава тестируется на настоящем сокете с `port=0`: поднять `Mount`, сходить `GET /mounts`, создать единицу с `Idempotency-Key`, повторить тот же POST, сравнить ответы. Ни камеры, ни GStreamer.

## Что нужно знать заранее

- **Урок 17** — `SpecConsole`, `describe`, `IdempotencyKeys`, чтения, `dispatch`.
- **Урок 13** — `read_model`, `policy`/`set_policy`, `POLICY_CHOICES`, `resource_state`, `idle_by_policy`.
- **Урок 10** — `Refused` и `refuse`: почему ошибка поля приходит именно оттуда.
- **Урок 4** — heartbeat и `extra`: `/servers` и `/metrics` целиком построены на нём.

## Чему вы научитесь

1. Писать запись, которая ничего не решает, и объяснять, почему `worker: None` — это ответ, а не заглушка.
2. Отличать 400 от 404 и 503 по тому, кто виноват, а не по тому, что удобнее.
3. Собирать вид «по серверам» из одних heartbeat'ов — без реестра машин.
4. Отдавать метрики в формате Prometheus, не притащив Prometheus.
5. Открывать подсистеме дверь в консоль (`extra`) так, чтобы платформа не узнала, что за ней.
6. Монтировать несколько подсистем в один процесс и один порт.

---

## Шаг 1 — Четыре записи

```python
    def create(self, body: dict) -> tuple[int, dict]:
        try:
            r = self.ctl.create(body)
            return 201, {**r, "worker": None}                        # placed by the controller's next pass, never by the console
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}
```

Пять строк, и в них три решения.

`201`, а не `200`: создано нечто новое, у него есть идентификатор, и он в теле. Обычный HTTP.

`{**r, "worker": None}` — та самая честность. Контроллер вернул строку такой, какой она легла в хранилище: с `id`, `revision`, полями по умолчанию. Поля `worker` в ней нет вовсе — размещение живёт в другом ключе (урок 5). Консоль дописывает его явным `None`, чтобы страница не гадала: единица есть, воркера у неё пока нет, приходите через секунду.

`Refused` → `400`. Исключение приходит из `refuse` (урок 10): оператор прислал поле, которого нет в спецификации, или значение не того типа, или нарушил ограничение. Это ошибка клиента, и текст в ней уже человеческий — его не надо переводить, его надо передать.

Почему `detail` и `error` — одно и то же дважды? Потому что страница читает `detail`, а тесты и внешние клиенты М12 читают `error`. Дублирование в четыре символа дешевле, чем согласовывать два потребителя.

```python
    def update(self, uid, body: dict) -> tuple[int, dict]:
        try:
            return 200, self.ctl.update(uid, body)
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}
        except KeyError:
            return 404, {"detail": "no such unit", "error": "no such unit"}
```

То же самое плюс одна ветка. `KeyError` — единицы нет — это `404`, не `400`: клиент ничего не написал неправильно, он обратился к тому, чего нет. Разница видна оператору: при `400` надо исправить форму, при `404` — обновить страницу.

`update` не возвращает `worker: None`. Единица уже жила, размещение у неё могло быть, и затирать его выдумкой было бы хуже, чем не упомянуть.

```python
    def delete(self, uid) -> tuple[int, dict]:
        if self.ctl.unit(uid) is None:
            return 404, {"detail": "no such unit", "error": "no such unit"}
        self.ctl.delete(uid)
        return 200, {"deleted": uid}
```

Проверка **до** удаления, а не `try/except` вокруг него. `ctl.delete` из урока 11 идемпотентен по построению: он снимает строку, размещение, эпоху и слот, и ему всё равно, было ли что снимать. Если бы консоль просто звала его, второй `DELETE` вернул бы `200` — и оператор, дважды нажавший на крестик, не узнал бы, что второй раз был вхолостую.

Здесь курс сознательно выбирает **не** идемпотентность: у `DELETE` нет ключа идемпотентности (дальше в `dispatch` это видно), и второй удар честно отвечает `404`. Формула, которой стоит держаться: «пропало — значит пропало». Между гонкой двух операторов и молчаливым «ок» на удаление несуществующего выбрано второе как более вредное.

## Шаг 2 — Отметка оператора

```python
    def mark(self, body: dict, user: str) -> tuple[int, dict]:
        if self.marks is None:
            return 503, {"detail": "no resource on this server to write marks into", "error": "no resource on this server to write marks into"}
        if "cam" not in body and "unit" not in body:
            return 400, {"detail": "a mark names a unit", "error": "a mark names a unit"}
```

Третий код состояния в уроке, и снова по вине, а не по удобству. `503` — «не я и не ты»: консоль поднята на сервере, где нет ресурса, писать некуда, и это состояние машины, а не запроса. Клиенту имеет смысл повторить позже; на `400` повторять бессмысленно.

`400` — отметка без единицы. Отметка оператора — это «здесь что-то было» **про что-то**; без ссылки она не событие, а запись в дневнике.

```python
        fields = {"user": user, "note": str(body.get("note", ""))}
        if "cam" in body:
            fields["cam"] = int(body["cam"])                          # the field the index joins on
        else:
            fields["unit"] = str(body["unit"])
        path = self.marks.append(self.wall(), "mark", **fields)
        return 201, {"subsystem": "console", "unit": self.instance, "bucket": os.path.relpath(path, self.marks_root)}
```

`int(body["cam"])` — не придирка к типу. Это поле, **по которому база событий соединяет** (урок 15): отметка оператора должна лечь на таймлайн камеры рядом с детекциями, а соединение идёт по числу. Строка `"7"` и число `7` не встретятся.

`unit` — для всего остального: подсистема, у которой единица не камера. Тогда соединения по камере нет, и отметка видна только в общем списке.

Пишет консоль **в свой собственный бакет**: `console/<instance>/e1/…`, обоснованное в уроке 17. Ответ возвращает путь относительно корня ресурса — то, что тест сравнивает, и то, что можно показать оператору без утечки абсолютных путей.

## Шаг 3 — Вид по серверам

Страница показывает не только единицы, но и машины: где есть воркеры, где живой ресурс, куда контроллер сейчас поставил бы, а куда нет и почему. Этого нет ни в одном ключе — это собирается из heartbeat'ов.

```python
    def servers(self) -> dict:
        ctl, now = self.ctl, self.wall()
        out: dict[str, dict] = {}
        for w, hb in heartbeats(ctl.objects, ctl.sub.name + "/").items():
            s = out.setdefault(hb.extra.get("server", "?"), {"archive": None, "resource": "unknown", "workers": []})
            if hb.extra.get("archive"):
                s["archive"] = hb.extra["archive"]
            s["workers"].append({"worker": w, "load": ctl.load(w), "capacity": ctl.capacity_of(w), "labels": hb.extra.get("labels", ""),
                                 "state": "live" if now - hb.ts <= self.lost_after else "stale", "idle_by_policy": False})
```

**Реестра машин нет.** Список серверов получается перечислением того, что билось: `hb.extra["server"]` — строка, которую воркер положил в свой heartbeat (урок 4). Сервер, с которого никто не бился, в ответе не появится, и это правильно: с точки зрения системы его нет.

`"?"` для воркера без поля `server` — не заглушка ради красоты, а видимая аномалия: если в ответе появился сервер `?`, значит кто-то бьётся, не сказав откуда.

`archive` — путь, куда воркеры этого сервера пишут архив; на кластере это `meta.archive` из Nomad, метка, по которой планировщик и разместил. Консоль её не вычисляет, она её пересказывает.

`state` — `live` или `stale` по `lost_after`. Не «мёртв»: консоль не выносит приговоров, это дело контроллера и его `failover_seconds`.

```python
        for w in ctl.idle_by_policy(list(heartbeats(ctl.objects, ctl.sub.name + "/"))):    # servers: distinct — one worker per server carries units
            for s in out.values():
                for row in s["workers"]:
                    if row["worker"] == w:
                        row["idle_by_policy"] = True
```

`idle_by_policy` из урока 13: при `servers: distinct` единицы несёт один воркер на сервер, остальные живы и пусты. Без этого флага оператор видит воркер с нулевой нагрузкой и решает, что тот сломан. Флаг переводит «пустой» в «пустой по правилу».

Три вложенных цикла на десятке воркеров — не проблема, и переписывать их в словарь ради красоты не стоит; но если вы поднимете счёт до сотен, это первое место, куда смотреть.

```python
        for server in resources_seen(ctl.objects):
            out.setdefault(server, {"archive": None, "resource": "unknown", "workers": []})
        for server, s in out.items():
            s["resource"] = ctl.resource_state(server, self.lost_after)
            s["requires_resource"] = ctl.spec.requires == "resource"
            s["placeable"] = not (s["requires_resource"] and s["resource"] == "silent")
            s["why"] = f"resource on {server} silent" if not s["placeable"] else None
            s["workers"].sort(key=lambda x: x["worker"])
        return {"policy": ctl.policy(), "servers": dict(sorted(out.items()))}
```

Первый цикл добавляет серверы, у которых **бьётся ресурс, но нет воркеров**. Такой сервер существует для системы — на нём лежит архив — и должен быть на экране, пусть и с пустым списком.

`placeable`/`why` — то же правило, по которому решает `_pool` (урок 12), пересчитанное для показа. Дублирование логики? Да, и намеренное: контроллер отвечает решением, консоль — объяснением, и объяснение должно существовать до того, как решение понадобится. Если правило меняется, оно меняется в двух местах — и в уроке 13 у него один тест на оба.

Сортировка — чтобы экран не дрожал между обновлениями.

## Шаг 4 — Метрики

```python
    def metrics_text(self) -> str:
        p = self.spec.name
        hbs = heartbeats(self.ctl.objects, p + "/"); now = self.wall()
        live = {w: hb for w, hb in hbs.items() if now - hb.ts <= self.lost_after}
        res = resources_seen(self.ctl.objects)
```

Формат Prometheus — это текст. Никакой библиотеки, никакого реестра, никакого клиента: `# TYPE`, имя, метки в фигурных скобках, число. Двадцать строк кода вместо зависимости, которую пришлось бы тащить на коробку.

Каждое имя начинается с `p = spec.name`: `ticks_workers_live`, `vms_workers_live`, `det_workers_live`. **Имена метрик выводятся из спецификации**, как и всё остальное в этой консоли; добавив подсистему, вы получаете её метрики бесплатно.

```python
        lines = [f"# TYPE {p}_workers_live gauge", f"{p}_workers_live {len(live)}",
                 f"# TYPE {p}_worker_headroom gauge",
                 *[f'{p}_worker_headroom{{worker="{w}",server="{hb.extra.get("server", "?")}"}} {hb.extra.get(self.spec.headroom_from, 0)}' for w, hb in live.items()],
                 f"{p}_headroom {sum(int(hb.extra.get(self.spec.headroom_from, 0)) for hb in live.values())}",
```

`headroom_from` — имя поля heartbeat'а, взятое из YAML (урок 9). Платформа не знает, что у камер запас считается в мегапикселях, а у счётчика в тиках; она знает, что поле называется так, как сказано в спецификации.

Сумма запаса по живым воркерам — единственная метрика без меток: именно её читает автомасштабирование в М11. Метрика с метками — для глаз, метрика без меток — для решения.

```python
                 f"# TYPE {p}_worker_load gauge",              # assigned / capacity: what a target-value policy scales on
                 *[f'{p}_worker_load{{worker="{w}"}} {1 - int(hb.extra.get(self.spec.headroom_from, 0)) / max(1, int(hb.extra.get(self.spec.capacity_from, 1))):.3f}' for w, hb in live.items()],
```

Нагрузка как `1 - запас/ёмкость`. `max(1, …)` — защита от деления на ноль, которая случается ровно один раз: у воркера, успевшего ударить heartbeat'ом до того, как он посчитал свою ёмкость.

```python
                 f"# TYPE {p}_epoch_conflicts counter",
                 *[f'{p}_epoch_conflicts{{worker="{w}"}} {hb.extra.get("conflicts", 0)}' for w, hb in hbs.items()],
```

Единственная метрика, которая считается по **всем** heartbeat'ам, а не только по живым: конфликты эпох — это история, и умерший воркер свой счёт уже не поправит, но рассказать о нём успел. Ненулевые конфликты означают, что двое держали одну единицу и отсечение сработало — то, ради чего написан урок 6.

```python
                 f"# TYPE {p}_failover_seconds gauge", f'{p}_failover_seconds{{kind="worst"}} {self.worst_failover}',
                 f"# TYPE {p}_resources_live gauge", f"{p}_resources_live {sum(1 for hb in res.values() if now - float(hb['ts']) <= self.lost_after)}",
                 f"# TYPE {p}_{self.spec.running_gauge} gauge",
                 f"{p}_{self.spec.running_gauge} {sum(1 for hb in live.values() for s in hb.status if s.get('phase') == 'running')}"]
        return "\n".join(lines) + "\n"
```

`worst_failover` — арифметика из урока 13, посчитанная один раз при сборке консоли: худший разрыв между смертью воркера и подхватом его единиц. Величина проектная, а не наблюдаемая, и на графике она — линия, с которой сравнивают наблюдаемое.

`running_gauge` — снова имя из YAML: у камер это `cameras_streaming`, у счётчика `ticks_ticking`. Считается перечислением статусов живых воркеров с `phase == running` — то есть **не** «сколько настроено», а «сколько на самом деле идёт».

Ответ заканчивается переводом строки: Prometheus на это не жалуется, а `curl` без него печатает промпт впритык.

## Шаг 5 — Дверь подсистемы

Платформа не знает про видео — но `/live/7.m3u8` и `/rec/7/clip.mp4` кому-то отдавать надо. Дверь одна, и она в четырнадцати строках:

```python
    def _extra(self, h, method, path, q):
        r = self.extra(h, method, path, q) if self.extra else None
        if r is None:
            return False
        if r == ():                                                  # the extra wrote the reply itself (send_file)
            return True
        if len(r) == 2 and isinstance(r[1], (dict, list)):
            h._send(*r)
        elif len(r) == 2:
            h.send_response(r[0]); h.send_header("Content-Length", str(len(r[1]))); h.end_headers(); h.wfile.write(r[1])
        else:
            status, data, headers = r
            h.send_response(status); h.send_header("Content-Length", str(len(data)))
            for k, v in headers: h.send_header(k, v)
            h.end_headers(); h.wfile.write(data)
        return True
```

`extra` — функция, переданная в конструктор консоли. Платформа не знает о ней ничего, кроме протокола ответа, и протокол здесь — четыре формы:

| Вернула | Что это значит |
|---|---|
| `None` | «не мой маршрут» — `dispatch` идёт дальше и в конце отвечает 404 |
| `()` | «я уже ответила сама» — так отдаётся файл через `send_file` |
| `(status, dict\|list)` | JSON |
| `(status, bytes)` | сырые байты |
| `(status, bytes, headers)` | сырые байты со своими заголовками |

Последняя форма — ради диапазонных запросов: плеер просит кусок файла, и `Content-Range` с `206` приходит именно так. Это единственное место, где платформа уступает подсистеме управление заголовками, и оно стоило одной ветки `else`.

Обратите внимание, чего у `extra` нет: доступа к контроллеру, к ACL, к размещению. Ей дают обработчик, метод, путь и запрос. Всё, что она может, — ответить.

## Шаг 6 — Ключ идемпотентности в диспетчере

```python
    def _idem(self, h):
        key = h.headers.get("Idempotency-Key")
        if not key:
            h._send(400, {"detail": "Idempotency-Key header is required", "error": "Idempotency-Key required"}); return None
        try:
            prior = self.seen.claim(key)
        except Refused as e:
            h._send(400, {"detail": str(e), "error": str(e)}); return None
        if prior is not None:
            h._send(*prior); return None
        return key
```

Ключ **обязателен** для POST. Не «поддерживается» — обязателен: клиент без ключа получает 400 и не создаёт ничего. Это невежливо к тем, кто пробует систему из `curl`, и это осознанно: создание без ключа при повторе даёт вторую единицу, а вторая камера с тем же адресом — худшая ошибка, чем неудобный `curl`.

Возврат `None` в трёх случаях из четырёх, и во всех трёх ответ уже отправлен. Идиома неприятная, но её альтернатива — исключение на управляющий поток; здесь вызывающий — три строки ниже, и разглядеть их можно целиком.

```python
        if method == "POST":
            if path not in (rows_path, "/marks"):
                if self._extra(h, "POST", path, q):
                    return
                return h._send(404, {"detail": "no such route", "error": "no such path"})
            key = self._idem(h)
            if key is None:
                return
            if path == "/marks":
                resp = con.mark(h._body(), h.headers.get("X-User", "operator"))
            else:
                resp = con.create(h._body())
            con.seen.store(key, resp); return h._send(*resp)
```

Порядок: сначала маршрут, потом ключ. Запрос на несуществующий путь получает 404, не 400 про заголовок — иначе опечатка в URL выглядела бы как проблема с идемпотентностью.

`store` **до** отправки. Если процесс умрёт между ними, клиент не получит ответа, повторит — и получит сохранённый. Обратный порядок в той же аварии создал бы вторую единицу.

`X-User` со значением по умолчанию `operator` — единственное место, где консоль знает про человека. Аутентификации в курсе нет: она разбирается в М12 вместе с прокси, а здесь заголовок ставит тот же прокси, и консоль ему верит, потому что она за ним.

```python
        if method == "PUT":
            if path == "/policy":                                        # the administrator's knobs: one row, no idempotency needed (a PUT is)
                try:
                    return h._send(200, ctl.set_policy(h._body()))
                except (Refused, Forbidden) as e:
                    return h._send(400 if isinstance(e, Refused) else 403, {"detail": str(e), "error": str(e)})
```

Комментарий договаривает мысль за код: **PUT идемпотентен сам по себе.** Поставить `servers: distinct` дважды — это то же состояние. Ключ нужен там, где повтор создаёт, а не там, где он перезаписывает.

Две ошибки, два кода: `Refused` — 400 (администратор выбрал значение не из `POLICY_CHOICES`), `Forbidden` — 403 (токен не имеет права на `<sub>/policy`, урок 3). Первое — «вы не так написали», второе — «вам нельзя». Свалить их в один код означало бы скрыть от администратора, что дело в правах.

Парный ему GET отдаёт и значения, и варианты:

```python
            if path == "/policy":
                return h._send(200, {**ctl.policy(), "choices": ctl.POLICY_CHOICES})
```

Страница строит из `choices` выпадающий список, ничего не зная о политиках. Тот же приём, что и с `/spec` в уроке 17: **вариант выбора — это данные**, а не разметка.

```python
            key = h.headers.get("Idempotency-Key")
            if key:
                try:
                    prior = con.seen.claim(key)
                except Refused as e:
                    return h._send(400, {"detail": str(e), "error": str(e)})
                if prior is not None:
                    return h._send(*prior)
            resp = con.update(self._uid(path), h._body())
            if key:
                con.seen.store(key, resp)
            return h._send(*resp)
```

Для `PUT /<rows>/<id>` ключ **необязателен**: PUT и так идемпотентен. Но если клиент его прислал — та же заявка и то же сохранение. Зачем? Ради ответа: повтор без ключа пройдёт по контроллеру заново и вернёт новую `revision`, а клиент, который сравнивает ответы, решит, что кто-то ещё правил строку. С ключом он получит тот же ответ, что и в первый раз.

```python
        if method == "DELETE":
            if not path.startswith(rows_path + "/"):
                if self._extra(h, "DELETE", path, q):
                    return
                return h._send(404, {"detail": "no such route", "error": "no such path"})
            return h._send(*con.delete(self._uid(path)))
        h._send(405, {"detail": "method", "error": "method"})
```

У `DELETE` ключа нет вовсе — решение из шага 1, теперь видное в коде. И последняя строка: `405` для всего, что не GET/POST/PUT/DELETE.

## Шаг 7 — `Mount`

```python
class Mount:
    """One console process, several subsystems. The root console answers at `/`
    (the page, `/<rows>`, its extras); every other subsystem is a path: `/live/spec`,
    `/det/units`, `/det/where/7-motion` — the same SpecConsole class, its routes
    under its name, its own token-scoped controller. A person opens one page;
    the machines (the autoscaler, М12's read model) find every subsystem on one
    port; a new subsystem is a YAML, a worker, and a path."""

    def __init__(self, root: SpecConsole, mounts: dict[str, SpecConsole] | None = None):
        self.root, self.mounts = root, dict(mounts or {})

    def mount(self, name: str, console: SpecConsole) -> "Mount":
        self.mounts[name] = console
        return self
```

Корневая консоль и словарь остальных. `mount` возвращает `self`, чтобы сборка читалась одной цепочкой:

```python
Mount(cameras).mount("rec", rec).mount("det", det).mount("an", an).serve(port=8080)
```

Асимметрия — корень отдельно, остальные в словаре — не случайна. У корня есть страница, и `/` должен вести к ней, а не к списку подсистем. Оператор открывает камеры; всё остальное — под своими именами.

```python
    def resolve(self, path: str) -> tuple[SpecConsole, str]:
        head = path.split("/", 2)
        if len(head) >= 2 and head[1] in self.mounts:
            return self.mounts[head[1]], "/" + (head[2] if len(head) > 2 else "")
        return self.root, path
```

Вся маршрутизация — семь строк. `split("/", 2)` с ограничением: `/det/where/7-motion` даёт `["", "det", "where/7-motion"]`. Если второй элемент — имя смонтированной подсистемы, отдаём её консоль и остаток пути, начатый со слэша; иначе — корень и путь целиком.

`/det` без остатка даёт `"/"` — то есть страницу подсистемы `det`. Та же страница: она строит себя из `/spec`, и `spec` у неё свой.

И главное: **консоль не знает, что она смонтирована.** Её `dispatch` получает путь уже без префикса и работает так же, как если бы сидела на своём порту. Ради этого `dispatch` в уроке 17 принимает обработчик аргументом, а не является его методом.

Коллизия имён возможна: подсистема с именем `spec` или `metrics` перехватила бы маршрут корня. Проверки на это нет — есть соглашение, что имя подсистемы совпадает с её префиксом в хранилище, а префиксы и так должны быть различны.

```python
    def describe(self) -> dict:
        return {"root": self.root.spec.name, "mounts": {n: c.describe() for n, c in self.mounts.items()}}
```

`GET /mounts` — единственный маршрут, принадлежащий самому `Mount`. Ответ содержит **полное описание каждой подсистемы** (урок 17), а не только имена: клиент, пришедший на порт впервые, за один запрос узнаёт, что здесь живёт и какие у каждого поля. Так читающая модель М12 открывает узел, ничего не зная о нём заранее.

```python
    def handler(self):
        mnt = self

        class H(SendMixin, BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _route(self, method):
                u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/mounts":
                    return self._send(200, mnt.describe())
                con, path = mnt.resolve(u.path)
                con.dispatch(self, method, path, q)

            def do_GET(self): self._route("GET")
            def do_POST(self): self._route("POST")
            def do_PUT(self): self._route("PUT")
            def do_DELETE(self): self._route("DELETE")

        return H
```

Класс обработчика **один на все подсистемы** — он и не может быть другим, `ThreadingHTTPServer` принимает ровно один. Отсюда и форма `dispatch`: обработчик общий, диспетчер у каждой свой.

`mnt = self` — замыкание вместо атрибута класса: обработчик создаётся заново для каждого `Mount`, и связь идёт через область видимости.

`q` схлопывает повторяющиеся параметры к первому: `?cam=1&cam=2` даёт `1`. Ни один маршрут курса не принимает списков, а разбираться в них в четырёх местах не хочется.

`log_message` замолчан: `BaseHTTPRequestHandler` по умолчанию печатает каждую строку запроса в `stderr`, и на коробке с камерами это десятки строк в секунду в журнал, где нужны совсем другие.

```python
    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
```

И зеркальный ему метод одиночной консоли, ради которого всё сошлось:

```python
    def handler(self):
        """The request handler class for this console alone — a Mount with no other subsystems."""
        return Mount(self).handler()
```

**Одиночная консоль — это `Mount` без смонтированных подсистем.** Один путь кода вместо двух: то, что работает для четырёх, работает для одной, и тест, поднимающий одну консоль, проверяет ту же маршрутизацию, что и рабочий процесс.

`daemon=True` — чтобы тест, забывший остановить сервер, не подвесил прогон. `port=0` в тестах: ядро выдаёт свободный порт, и он читается из `srv.server_address[1]`.

## Результат

```python
from w2cplatform.console import SpecConsole, Mount

root = SpecConsole(SpecController(ticks_spec, vars_, objects), marks_root="/srv/archive")
srv  = Mount(root).serve(port=0)
port = srv.server_address[1]
```

```
GET  /mounts            → {"root": "ticks", "mounts": {}}
GET  /spec              → поля, строки, политики
GET  /ticks             → {"rows": [...], "configured": {...}}
POST /ticks             + Idempotency-Key: a1   → 201 {"id": 1, ..., "worker": null}
POST /ticks             + Idempotency-Key: a1   → 201 тот же ответ, вторая единица не создана
PUT  /policy            {"servers": "distinct"} → 200
GET  /metrics           → ticks_workers_live 2 …
DELETE /ticks/1         → 200 {"deleted": 1}
DELETE /ticks/1         → 404
```

Прогон тестов зелёный. М10A написан целиком: платформа, которая ничего не знает о видео, — и она работает.

## Что может пойти не так

- **`store` после отправки ответа.** Кажется естественнее — сначала ответить, потом записать. При падении процесса между ними повтор создаст вторую единицу. Записывайте до.
- **Ключ идемпотентности у `DELETE`.** Соблазнительно ради единообразия. Результат: второй `DELETE` возвращает `200` из сохранённого ответа, и оператор не узнаёт, что удалять было нечего.
- **`Forbidden` через тот же 400, что и `Refused`.** Администратор будет искать ошибку в значении, а проблема в токене.
- **`extra`, которой дали контроллер.** Один раз это удобно; после этого подсистема пишет в хранилище мимо контроллера, и правило «один писатель на префикс» держится только на честном слове.
- **Подсистема, смонтированная под именем существующего маршрута** (`spec`, `metrics`, `mounts`, `policy`). Маршрут корня исчезает молча. Имена подсистем — те же, что префиксы хранилища; держитесь этого.
- **Реестр серверов «чтобы было надёжнее».** Появится вторая правда о машинах, и она разойдётся с heartbeat'ами ровно тогда, когда правда нужнее всего.
- **Регистрация метрик через клиентскую библиотеку.** Зависимость, процесс-коллектор, формат, который всё равно текст. Двадцать строк f-строк делают то же и читаются.

## Итог

- Запись консоли ничего не решает: `create` отвечает `worker: None`, потому что размещает контроллер, и у консоли нет на это прав.
- Код ответа выбирается по виновнику: 400 — форма запроса, 403 — права, 404 — нет такого, 503 — нет ресурса на этой машине.
- Отметка оператора хранит `cam` числом, потому что это поле соединения базы событий.
- Вид по серверам собран из одних heartbeat'ов: реестра машин в системе нет, и сервер, с которого никто не бился, не существует.
- Метрики — текст, а имена метрик выведены из спецификации: новая подсистема получает свои даром.
- `extra` — единственная дверь подсистемы в консоль, и через неё проходят только ответы, никогда не права.
- `Mount` — сорок строк, после которых новая подсистема становится путём; одиночная консоль реализована как `Mount` без смонтированных.

## Упражнения

1. Поменяйте местами `store` и `_send` в ветке POST. Убейте процесс между ними (`os._exit` во временной строке) и повторите запрос. Сколько единиц в хранилище?
2. Сделайте ключ идемпотентности необязательным для POST. Отправьте один и тот же запрос дважды из двух вкладок. Что увидит оператор на странице?
3. Уберите из `servers()` цикл по `resources_seen`. Опишите, что перестанет быть видно на сервере, где лежит архив и нет воркеров.
4. Снимите флаг `idle_by_policy`. Поставьте `servers: distinct` на трёх воркерах и опишите экран глазами оператора, который не читал урок 13.
5. Замените в `metrics_text` `self.spec.headroom_from` на строковый литерал. Какие подсистемы М10B перестанут отдавать осмысленные метрики?
6. Смонтируйте подсистему под именем `spec`. Какой запрос сломается и почему его не поймает ни один тест?
7. Верните из `extra` кортеж `(200, "текст")` вместо байтов. Найдите в `_extra` ветку, в которую он попадёт, и скажите, чем закончится запрос.
8. Напишите `GET /mounts` для четырёх подсистем и посчитайте размер ответа. Что стоит из него убрать, если консолей на узле станет двадцать?

## Что дальше

Порт отвечает, маршруты есть, JSON правильный — а смотреть не на что. [**Урок 19**](19-the-page.md), последний в модуле, разбирает 666 строк `console.html`: список и формы, собранные из `/spec`; таймлайн на канве с метками событий; плеер, просящий файл диапазонами; переключатели оператора и администратора — страница, которая ничего не знает ни об одной подсистеме и показывает любую.
