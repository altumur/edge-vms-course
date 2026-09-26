# Урок 20 — Объектное хранилище: права и уборка

**Модуль:** М10A — Платформа (ServerVMS, часть первая)
**Вы напишете:** `heartbeats/` как каталог, а не как суффикс; три `acl_objects_*`, выведенные из спеки; тест, сверяющий `deploy/*-policy.hcl` с кодом в обе стороны; `verify` на чтении блоба. Затем `delete` у объектного хранилища; `sweep_blobs` в два прохода с отсрочкой и CAS; снятие пометки в `put_blob`; две метрики; и тест, который ловит гонку изнутри одного прохода.
**Время:** ~110 минут.

## Зачем этот урок

Спросите себя: **нужен ли объектному хранилищу ACL?**

У `Variables` он есть с урока 1, и он выводится из спеки — `acl_controller()`, `acl_console()`, `acl_worker()`. У объектного хранилища нет ничего: `FsObjectStore` не знает ни писателя, ни префиксов, и в `objects.py` про это сказано прямо — «No CAS, no index».

Соблазн — дописать `writer`/`acl` в `FsObjectStore` по образцу `FileVariables`. Не надо, и довод есть в самом курсе, в кластерном бэкенде:

```python
# `writer`/`acl` are ignored here: on a cluster the ACL is the task's own workload identity, enforced by
# the server, not by the client (which is the point of a real store).
```

Клиентская проверка — симуляция, а не защита: процесс, который хочет её обойти, просто её не вызывает. Настоящий ACL **уже есть** — это политики Nomad в `deploy/*-policy.hcl`. Проблема не в том, что его нет, а в том, что он **написан руками и ничем не сверяется с кодом**.

За два коммета он разошёлся с кодом трижды.

Права разложены — и тем самым назван последний писатель каждого каталога. Остаётся долг, записанный в уроке 19 и до сих пор не отданный.

Урок 19 закончился честным остатком: блобы копятся, уборки нет. Вот чем это отличается от всего остального в объектном хранилище.

Ключ блоба — дайджест его байтов, поэтому **каждая правка поля `blob` создаёт новый вечный объект**. У heartbeat'а ключ один на слот и переписывается следующим экземпляром; у снапшота — один на воркера. Блобы растут по числу **правок за всю жизнь системы**, и до сих пор их не забирал никто.

Прежде чем писать уборку, стоит проверить встречное подозрение: а не копятся ли и heartbeat'ы? Нет, и это важно:

```go
			order = append(append(lapsed, free...), fmt.Sprintf("w-%d", maxN+1))
```

`claim_slot` берёт сначала просроченные слоты, потом свободные, и только потом придумывает новое имя. Множество имён сходится к пику одновременно живших воркеров — двенадцать по jobspec'у.

И устаревший heartbeat **несущий**:

```go
	if raw, _ := objects.Get(w.Sub.HeartbeatKey(w.Name)); len(raw) > 0 {
		if old, err := p.HeartbeatFromBytes(raw); err == nil && old.ExtraString("instance", "") != w.Instance {
			w.PreviousHb, w.PreviousInstance = old.Ts, old.ExtraString("instance", "")
		}
	}
```

Воркер на старте читает heartbeat предыдущего экземпляра своего слота — из этого считается измеренное переключение (урок 11). Подметёте их — уберёте измерение. Так что уборка касается **только** `blobs/`, и это не осторожность, а требование.

## Три расхождения, и ни одно не тонкое

**Первое.** Грант контроллеру называл точный путь:

```hcl
path "objects/vms/snapshot" { capabilities = ["write", "read"] }
```

Урок 19 нарезал снапшот на `objects/vms/snapshot/<worker>`. Путь без `*` — это **точный путь**, поэтому каждый шард стал 403. Каждые пять секунд, с записью в логе «placement pass failed» — про единственное, что не упало.

**Второе.** Консоль получила маршрут для блобов (урок 19) и не получила гранта:

```hcl
path "objects/*"       { capabilities = ["read", "list"] }
```

Маршрут, покрытый восемью тестами, на кластере не работал вообще.

**Третье, и оно жило там с самого начала.** У `reccontroller` гранта на собственный снапшот не было никогда. Контроллер второй подсистемы вызывает тот же `publish_snapshot()` и всегда получал бы отказ.

Все три невидимы по одной причине: **набор гоняется против хранилища без ACL, а файл с ACL — не код.**
## Что нужно знать заранее

- **Урок 2** — ACL у `Variables`: писатель и префиксы, и почему их два.
- **Урок 4** — объектное хранилище и записанное там «No delete».
- **Урок 5** — раскладка ключей, и место, где `heartbeat_key` выбрал суффикс.
- **Урок 15** — `IdempotencyKeys.prune`: уже существующая в платформе уборка, с которой стоит сравнить.
- **Урок 19** — снапшот нарезан по воркеру; `blob`: байты в объектном хранилище, дайджест в строке.
- **М11, урок 5** — политики Nomad и workload identity.

## Чему научитесь

- Отличать «ACL отсутствует» от «ACL есть, но невыразим» — и видеть, что второе лечится раскладкой ключей.
- Выводить грант из спеки и проверять файл политики тестом в обе стороны.
- Понимать, чего ACL не может дать в принципе, и что ставить туда вместо него.
- Видеть, почему «удалить всё, на что никто не ссылается» — неверно, и во сколько приёмов это чинится.
- Получать отсрочку из структуры, а не из меток времени, которых у хранилища нет.
- Ставить необратимое действие **после** обратимого, чтобы проигранный CAS ничего не стоил.
- Писать тест, который ловит гонку внутри одного прохода, а не рядом с ним.

---

## Шаг 1 — Что мог воркер

```hcl
path "objects/vms/*" { capabilities = ["write", "read", "list"] }   # its heartbeat, as an object-as-Variable
```

Комментарий говорит «свой heartbeat». Грант даёт весь префикс подсистемы:

| ключ | что это значит |
|---|---|
| `objects/vms/w-9/heartbeat` | подделать чужой heartbeat: ёмкость, `headroom`, камеры как `running` |
| `objects/vms/snapshot/<любой>` | **писать каталог домена напрямую** |
| `objects/vms/blobs/<дайджест>` | подменить байты под существующим дайджестом |

Строки конфигурации при этом в безопасности — `vms/cameras/*` принадлежит консоли. Дыра именно в объектном хранилище.

## Шаг 2 — Почему это нельзя было сузить

И вот главное в уроке. Дело **не** в том, что у Nomad не хватает выразительности.

Имя воркера берётся в рантайме: `claim_slot()` выдаёт `w-1`. Политика — статический текст, написанный до того, как кластер запустился. А ключ был устроен так:

```
objects/vms/<worker>/heartbeat
            ^^^^^^^^ первый сегмент — и он неизвестен заранее
```

Написать «может писать свой heartbeat и больше ничего» было **нечем**: самый узкий выражаемый префикс — `objects/vms/*`, то есть всё.

Это не дефект ACL. Это дефект **раскладки ключей**, и урок 5 выбрал её сам, вслух, с обоснованием:

> Порядок выбран так, чтобы объекты одного воркера лежали в одном каталоге — у него их будет больше одного, когда шлюз начнёт публиковать свою статистику.

Довод был разумный. Он оказался неверным, потому что не учитывал того, чего в уроке 5 ещё не было, — что этот префикс кому-то придётся выдавать.

## Шаг 3 — Имя воркера уезжает в конец

```python
HEARTBEATS = "heartbeats"
```

```python
    def heartbeat_key(self, worker: str) -> str:
        return f"{self.name}/{HEARTBEATS}/{worker}"
```

Три каталога-сиблинга под подсистемой, **по одному писателю у каждого**:

```
vms/heartbeats/<worker>     воркеры
vms/snapshot/<worker>       контроллер
vms/blobs/<digest>          консоль
```

И грант становится выразимым:

```hcl
    path "objects/vms/heartbeats/*" { capabilities = ["write", "read", "list"] }   # its heartbeat, as an object-as-Variable
    path "objects/vms/*" { capabilities = ["read", "list"] }            # the snapshot and the blobs: read, never written by a worker
```

Подделку соседнего heartbeat это не убирает — `w-1` и `w-2` в одном каталоге, — но радиус падает с «весь кластер плюс каталог домена» до «соседний воркер», а поверх уже работают эпохи и лизы.

**Попутно исчез фильтр, который несли все читатели.** Раньше каждый перечислял `vms/` и отбирал по суффиксу:

```python
        for key in self.objects.list(self.sub.name + "/"):
            if key.endswith("/heartbeat"):
```

Теперь:

```python
        # One prefix, no filter: `<name>/heartbeats/` holds heartbeats and nothing else.
        for key in self.objects.list(self.sub.heartbeats_prefix()):
```

Фильтр никогда не был смыслом — он был **ценой раскладки**, в которой heartbeat'ы лежали вперемешку со всем остальным. То же самое в домене: `Cluster.heartbeats()` в М12 нёс `endswith("/heartbeat") and key.count("/") == 2`, и обе половины ушли.

## Шаг 4 — Грант выводится из спеки

```python
    def acl_objects_worker(self) -> list[str]:
        """The workers write their own heartbeats and nothing else."""
        return [f"{self.name}/{HEARTBEATS}/*"]

    def acl_objects_controller(self) -> list[str]:
        """The controller publishes the snapshot shards — the only thing that leaves the cluster."""
        return [f"{self.name}/snapshot/*"]

    def acl_objects_console(self) -> list[str]:
        """The console stores the bytes of a `blob` field, beside the row that names them."""
        return [f"{self.name}/{BLOBS}/*"]
```

Рядом с `acl_controller()` и `acl_console()`, из той же спеки. Источник правды один — и теперь его можно с чем-то сравнить.

## Шаг 5 — Тест, который сверяет файл с кодом

Первое, что ему нужно, — семантика пути:

```python
def matches(pattern: str, key: str) -> bool:
    """Nomad's glob: `*` stands for any run of characters, and a pattern with no
    `*` is an EXACT path. That second half is the whole of the first bug — a
    pattern naming `objects/vms/snapshot` does not match `objects/vms/snapshot/w-1`."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), key) is not None
```

Дальше — три проверки, и они разной силы.

**Направление первое: всё, что код пишет, разрешено.** Берутся не строки-префиксы, а **конкретные ключи**, которые платформа действительно пишет, — иначе `objects/vms/snapshot` и `objects/vms/snapshot/*` выглядят похоже и сравниваются как равные:

```python
def _keys(prefixes: list[str], sample: str) -> list[str]:
    """A concrete key under each granted prefix — a pattern is checked by what it
    has to match, not by comparing two strings that look alike."""
    return [OBJECTS + p.rstrip("*") + sample for p in prefixes]
```

**Направление второе: никто не пишет того, что ему не положено.**

```python
    denied = [
        # a worker writes its own heartbeat and nothing else in the subsystem
        ("vmsworker-policy.hcl", OBJECTS + "vms/snapshot/w-1"),
        ("vmsworker-policy.hcl", OBJECTS + "vms/blobs/" + digest(b"a mask")),
```

**Третья — самая сильная: в файле нет гранта, которого код не просил.**

```python
    for policy, allowed in expected.items():
        unexplained = write_patterns(policy) - allowed
        assert not unexplained, f"{policy} grants write to {sorted(unexplained)}, which no acl_* in the spec asks for"
```

Именно её не прошёл бы старый `objects/vms/*` — грант, комментарий которого говорил «its heartbeat», а шаблон означал всю подсистему.

> **Проверено снятием — все четыре раза.** Вернуть точный путь снапшота: падают две проверки из трёх. Забрать у консоли грант на блобы: падает первая. Вернуть воркеру `objects/vms/*`: падают вторая и третья. Забрать у rec-контроллера снапшот: падает первая. То есть тест ловит ровно те три расхождения, которые были в проде, и четвёртое, которое я сочинил.

## Шаг 6 — Чего ACL не даёт, и что ставить вместо него

```python
    def blob(self, d: str, check_digest: bool = True) -> bytes | None:
        data = self.objects.get(self.sub.blob_key(d))
        if data is None or not check_digest:
            return data
        return verify(d, data)
```

Дайджест в ключе делал его неизменяемым **по договорённости**. Теперь — проверяемо:

```python
def verify(d: str, data: bytes) -> bytes:
    """`data` if it hashes to `d`; otherwise `BlobMismatch`. Never a silent pass."""
    actual = digest(data)
    if actual != d:
        raise BlobMismatch(f"{d}: the bytes stored there hash to {actual} — "
                           f"the object was replaced by someone who could write that key")
    return data
```

**Адресация по содержимому — это обещание; обещание, которое никто не проверяет, — это комментарий.**

И вот в чём разница классов. ACL — утверждение о том, **кто написал** байты. Проверка дайджеста — утверждение о том, **что это за** байты. Второе сильнее и держится там, где первое не держится вовсе:

- писатель с не тем токеном — ACL мог не сработать, проверка сработает;
- хранилище, доступное шире, чем задумано (`OBJECTS=s3+…` с общим бакетом);
- объект, скопированный между сторами при миграции;
- обрезанный объект и подгнивший диск — тут ACL не при чём вообще.

Проверка стоит на **чтении**, где байты сейчас пойдут в дело. Проверять только на записи — значит снова поверить писателю, то есть ровно тому, что заменяем.
## Шаг 7 — `delete` — у стора, политика — у вызывающего

```python
    # Removes one object; `True` if it was there. A capability of the STORE — a store can either delete or
    # it cannot — and deliberately not "delete, but only under `blobs/`": that would be policy welded into
    # the seam, and policy lives with the caller that has it (`SpecController.sweep_blobs`) and with the
    # scheduler's ACL, which is the only place that can actually enforce it.
    def delete(self, key: str) -> bool: ...
```

Соблазн — сделать `delete_blob(digest)` и «обезопасить» шов. Это тот же ход, от которого курс уходил в уроках 26 и 27: потолок объявляет стор, а решает вызывающий; ACL — у планировщика, а не в клиенте. Стор умеет удалять или не умеет; **что именно** можно удалять — знание уборщика, а **кому** можно — знание политики.

Инвариант урока 4 переписан честно, а не вычеркнут:

```
# - `delete` exists, and exactly one caller uses it: the blob sweep (Lesson 29). Everything else in the
#   platform still relies on objects NEVER going away — a stale heartbeat simply ages and readers filter by
#   `ts`, and a worker restarting reads the heartbeat its previous instance left to measure its own
#   failover. That is not an accident waiting to be tidied up: sweep the heartbeats and the measurement
#   goes with them. The rule is therefore not "nothing deletes" any more but the narrower and truer one:
#   an object is deleted only by a caller that can prove nothing refers to it, and only the blob sweep can.
```

## Шаг 8 — Почему очевидная реализация неверна

Объект пишется **раньше** строки, которая его называет (урок 19). Уборка, попавшая между этими двумя записями, видит блоб без ссылок и удаляет байты, на которые строка сейчас сошлётся. Оператор загрузил маску, всё прошло без ошибок, а единица не стартует.

Поэтому **замечать и удалять — разные проходы**:

```python
    #   mark   nothing is deleted. The digests that no row names are written to `<name>/sweep` with the
    #          time. A blob created after this moment is not on the list, which is where the grace period
    #          comes from — no timestamps on objects required, and `variables://` has none to offer.
    #   sweep  one pass later, and only after `grace`: the marked digests are checked AGAIN, the row is
    #          cleared by CAS on the index just read, and only then are the objects removed.
```

Блоб, появившийся между проходами, **в списке не лежит** — отсрочка получается из структуры. Метки времени на объектах не нужны, и это существенно: у `variables://objects` их нет.

## Шаг 9 — Порядок двух последних действий — и есть всё доказательство

```python
        self.vars.put(key, {"at": str(now), "digests": "[]"}, cas=idx)   # Conflict here deletes nothing
        deleted = sum(1 for d in doomed if self.objects.delete(self.sub.blob_key(d)))
```

Сначала **обратимое** — очистить строку под CAS. Проигранный CAS означает «пока мы думали, решение кто-то опроверг», и на этот момент **не удалено ни одного объекта**. Переставьте две строки местами — и то же самое стечение обстоятельств уничтожит байты.

## Шаг 10 — Гонка, которую закрывает `put_blob`

Тот же дайджест можно загрузить для второй единицы, пока копия первой помечена. Тогда помеченный дайджест — ровно тот, который строка сейчас назовёт:

```python
        # These exact bytes may be sitting on the sweep's list right now — the same mask uploaded again
        # for a second unit, while the copy the first unit stopped naming is marked for collection.
        # Taking it off the list makes the sweep's own CAS fail, and a sweep that loses that CAS deletes
        # nothing at all. The alternative is a lock, for a window two store calls wide.
```

Обратите внимание, что здесь не понадобилось ничего нового: **это история согласованности самой платформы**, применённая к её собственной бухгалтерии.

## Шаг 11 — Уборка ограничена, потому что её бухгалтерия — строка

```python
    SWEEP_LIMIT = 64
    SWEEP_GRACE = 300.0
```

`<name>/sweep` — Variable, а над Variable стоит потолок из урока 19. Дайджест весит 71 байт, и на девятистах кандидатах строка упрётся в 64 КиБ. Значит, уборка обязана брать не больше `limit` за проход — как `ensure_home(1)`. Правило, написанное два урока назад, применяется к тому, что написано под ним.

## Шаг 12 — Тест, который ловит гонку изнутри прохода

Три первых теста пишутся легко: пометил — не удалил; появился между проходами — выжил; перезалили помеченный — уборка отменилась. Четвёртый — про порядок из шага 3 — так не пишется: снаружи прохода переставленные строки ведут себя одинаково.

Нужно вмешаться **в середине**:

```python
    real = ctl.blobs_referenced
    def racing():
        ctl.blobs_referenced = real                       # once, in the middle of the pass
        # The real interleaving: the OBJECT is written and the row naming it is still in flight, so the
        # digest is genuinely unreferenced at the re-check — and the only thing between it and deletion
        # is that `put_blob` took it off the list, which the CAS is about to notice.
        other.put_blob(MASK_A)
        return real()
    ctl.blobs_referenced = racing
```

`blobs_referenced` вызывается после чтения строки и до записи — ровно там, где живёт гонка.

> **Стоит рассказать, как я на этом ошибся.** Первая версия теста делала в `racing()` не `put_blob`, а `put_blob` + `update`. Тест проходил — и проходил **одинаково при обоих порядках**, потому что при наличии строки дайджест оказывался сослыханным на перепроверке, `doomed` пустел, и удалять было нечего. Утверждение «порядок — это всё доказательство» стояло в комментарии, а проверял его тест, который его не проверял. Поймалось только снятием: переставил строки — всё зелено. Ошибка ровно того класса, который снятие и существует ловить, и она бы уехала в курс.

## Шаг 13 — Кто это запускает

Консоль — потому что по уроку 27 `blobs/` принадлежит ей: контроллер не смог бы удалить блоб, даже если бы захотел.

```python
def _sweep_loop(controllers, every: float = 60.0) -> None:
    while not stop.is_set():
        for c in controllers:
            try:
                r = c.sweep_blobs()
                if r["deleted"]:
                    logging.info("swept %d blob(s) nothing names in %s", r["deleted"], c.spec.name)
            except Exception:                         # noqa: BLE001
                logging.exception("the blob sweep failed in %s — nothing is reclaiming its blobs", c.spec.name)
        stop.wait(every)
```

Свой цикл и своя строка лога — урок 19, применённый **до** того, как ошибка сделана второй раз.

И две метрики, устроенные так, чтобы их можно было опрашивать раз в пятнадцать секунд:

```python
        # The sweep's backlog, for subsystems that have blobs to collect. Two cheap reads — a prefix
        # listing and one row — deliberately NOT `blobs_referenced()`, which walks every unit's row: a
        # gauge scraped every fifteen seconds must not cost a full scan of the configuration.
```

## Шаг 14 — Грант, и что нашлось, когда его проверили

```hcl
    # `destroy` — the only grant in this cluster that lets anything remove an object, and it is bounded to
    # the one prefix whose contents can be proved unreferenced (М10A Lesson 29). Heartbeats and snapshot
    # shards are deliberately NOT here: a worker reads the heartbeat its previous instance left to measure
    # its own failover, so collecting them would collect the measurement.
    path "objects/vms/blobs/*" { capabilities = ["write", "read", "list", "destroy"] }   # the bytes of a blob field, beside the row that names them
```

Тест политик из шага 5 проверял только **объектные** ключи. Добавив проверку для строк, он немедленно нашёл два расхождения, которые там лежали:

```
console-policy.hcl does not let it write vms/sweep
console-policy.hcl does not let it write platform/drain
```

Первое — моё, свежее: `acl_console()` получил `<sub>/sweep`, а политика нет. Второе жило там с урока 17: **`platform/drain` объявлен в `acl_console()` с самого начала, а в файле его не было**, и плановая остановка на кластере получала бы отказ — машина, которую оператор собирался вывести аккуратно, ушла бы молчанием.

Вывод, который стоит унести: проверка, написанная наполовину, находит половину. Направление «всё, что код пишет, разрешено» надо применять ко **всем** ключам, а не к тем, ради которых тест писали.

## Что может пойти не так

- **Дописали ACL в `FsObjectStore` и успокоились.** Клиентская проверка — симуляция; и это второе место, где записано правило, которое разойдётся с политикой так же, как разошлась политика с кодом.
- **Сравнили шаблон со строкой.** `objects/vms/snapshot` и `objects/vms/snapshot/*` выглядят одинаково, пока не проверишь их на конкретном ключе.
- **Написали только первое направление.** «Всё, что нужно, разрешено» — это половина; вторая половина, «и больше ничего», и есть то, ради чего ACL существует.
- **Проверили дайджест на записи.** Записывающий и так знает, что кладёт. Смысл — на чтении.
- **Решили, что имя воркера можно подставить в политику.** Оно появляется после старта кластера; политика существует до.
- **Удалили в том же проходе, где заметили.** Это гонка с `put_blob`, и выглядит она как «оператор загрузил маску, ошибок нет, единица не стартует».
- **Переставили очистку и удаление.** Проигранный CAS начинает стоить байтов.
- **Забыли снять пометку в `put_blob`.** Остаётся узкое окно шириной в два вызова, и закрывать его придётся блокировкой.
- **Сделали уборку неограниченной.** Её собственная строка упрётся в потолок, и уборка сломается на том, что убирает.
- **Подмели heartbeat'ы «заодно».** Измеренное переключение исчезает, и `builds()` перестаёт отвечать, что здесь вообще работает.
- **Написали тест на гонку снаружи прохода.** Он пройдёт при любом порядке, и вы узнаете об этом только снятием — если повезёт.

## Итог

Вопрос «нужен ли ACL объектному хранилищу» распался на три разных, и ответы разные.

**Нужен ли механизм в платформе?** Нет. Настоящий ACL — на кластере, и он там, где ему место: у планировщика, а не у клиента.

**Нужно ли сузить то, что есть?** Да, и это оказалось не вопросом ACL. Префикс, который нельзя ограничить политикой, — это дефект раскладки ключей. Имя воркера переехало из первого сегмента в последний, три каталога получили по одному писателю, и грант стал выразимым. Попутно из каждого читателя ушёл фильтр по суффиксу, который был не смыслом, а платой за старую раскладку.

**Нужно ли что-то, чего ACL не даёт?** Да — проверка дайджеста. Она про другое утверждение и держится там, где никакой ACL не держится.

А главное — **между кодом и политикой стоит тест**. Правило выводится из спеки один раз, и файл сверяется с ним в обе стороны. Три расхождения, которые прожили в репозитории от двух коммитов до нескольких месяцев, теперь не проживут ни одного прогона.

Уборка понадобилась одному классу объектов из трёх, и разница не в том, что блобы «мусорнее», а в том, что их **ключ адресуется содержимым**: новый набор байтов — новый вечный ключ. У heartbeat'а ключ переиспользуется и его устаревшая версия работает на измерение; у снапшота ключ на воркера. Поэтому `delete` появился у стора, а право им пользоваться — ровно у одного вызывающего и ровно на одном префиксе.

Само же решение оказалось не про удаление, а про **разнесение во времени**: заметить и убрать нельзя в один приём, потому что объект пишется раньше строки. Отсрочка вышла из структуры — кандидат, которого нет в списке, уже поэтому в безопасности, — а не из меток времени, которых у хранилища и нет. Обратимое действие поставлено перед необратимым, чтобы проигранный CAS ничего не стоил, и `put_blob` этот CAS умеет проиграть нарочно.

И два побочных урока, оба про тесты. Утверждение, которое проверяет тест, не проверяющий его, — хуже отсутствия теста: оно выглядит проверенным. А проверка, написанная для одной половины ключей, найдёт дефекты только в этой половине — вторая половина ждала с урока 17.

## Упражнения

1. **Верните `objects/vms/*` воркеру** и посмотрите, какие две проверки падают и почему разные.
2. **Добавьте четвёртую подсистему** с блобами и посмотрите, сколько строк политики придётся написать руками. Что должно измениться, чтобы их не писать вовсе?
3. **Подделайте heartbeat соседа** из теста: напишите `vms/heartbeats/w-2` токеном `w-1` и проследите, куда это доедет. Что останавливает такого воркера дальше по цепочке?
4. **Уберите `verify` и подмените блоб.** Через сколько шагов это заметит человек? А с `verify`?
5. **Напишите генератор политик** из `acl_*`/`acl_objects_*` и сравните его вывод с файлами. Что из того, что есть в файлах, он не сможет сгенерировать, и почему это может быть правильно?
6. **Проверьте `platform/resources/<server>/heartbeat`** по тому же критерию: выразим ли его грант, и почему у него имя в середине никого не смущает.
7. **Уберите отсрочку** (`grace=0`) и попробуйте воспроизвести потерю блоба. Сколько попыток нужно и от чего это зависит?
8. **Переставьте очистку и удаление** и посмотрите, какой тест падает. Потом уберите из него `other.put_blob` — и объясните, почему он снова проходит.
9. **Посчитайте потолок строки `sweep`** при дайджесте в 71 байт. Какой `limit` вы бы поставили и почему не больше?
10. **Напишите уборку для `platform/resources/<server>/heartbeat`** — единственного ключа, который действительно растёт с оборотом железа. Что должно быть верно, чтобы её можно было запустить?
11. **Перенесите уборку в контроллер** и посмотрите, на чём она сломается. Что именно об этом говорит политика?
12. **Придумайте порог** на `<sub>_blobs_total` и `<sub>_blobs_marked`. Что означает растущий `marked` при неподвижном `total`?

## Что дальше

Модуль закончен, и на этот раз без долгов: последний записанный остаток стал кодом. Дальше — **М11**, где `variables://objects` перестаёт быть каталогом на диске, а `destroy` из шага 8 становится настоящим правом, которое выдаёт планировщик.
