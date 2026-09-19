# Урок 29 — Уборка: единственное место, где платформа удаляет объект

**Модуль:** М10A — Платформа (ServerVMS, часть первая)
**Вы напишете:** `delete` у объектного хранилища; `sweep_blobs` в два прохода с отсрочкой и CAS; снятие пометки в `put_blob`; две метрики; и тест, который ловит гонку изнутри одного прохода.
**Время:** ~55 минут.

## Зачем этот урок

Урок 26 закончился честным остатком: блобы копятся, уборки нет. Вот чем это отличается от всего остального в объектном хранилище.

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

Воркер на старте читает heartbeat предыдущего экземпляра своего слота — из этого считается измеренное переключение (урок 13). Подметёте их — уберёте измерение. Так что уборка касается **только** `blobs/`, и это не осторожность, а требование.

## Что нужно знать заранее

- **Урок 4** — объектное хранилище и записанное там «No delete».
- **Урок 26** — `blob`: байты в сторе, дайджест в строке, объект пишется **первым**.
- **Урок 27** — три каталога-сиблинга с одним писателем у каждого; `blobs/` принадлежит консоли.
- **Урок 17** — `IdempotencyKeys.prune`: уже существующая в платформе уборка по TTL, под CAS.

## Чему научитесь

- Видеть, почему «удалить всё, на что никто не ссылается» — неверно, и во сколько приёмов это чинится.
- Получать отсрочку из структуры, а не из меток времени, которых у хранилища нет.
- Ставить необратимое действие **после** обратимого, чтобы проигранный CAS ничего не стоил.
- Писать тест, который ловит гонку внутри одного прохода, а не рядом с ним.

## Шаги

### 1. `delete` — у стора, политика — у вызывающего

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

### 2. Почему очевидная реализация неверна

Объект пишется **раньше** строки, которая его называет (урок 26). Уборка, попавшая между этими двумя записями, видит блоб без ссылок и удаляет байты, на которые строка сейчас сошлётся. Оператор загрузил маску, всё прошло без ошибок, а единица не стартует.

Поэтому **замечать и удалять — разные проходы**:

```python
    #   mark   nothing is deleted. The digests that no row names are written to `<name>/sweep` with the
    #          time. A blob created after this moment is not on the list, which is where the grace period
    #          comes from — no timestamps on objects required, and `variables://` has none to offer.
    #   sweep  one pass later, and only after `grace`: the marked digests are checked AGAIN, the row is
    #          cleared by CAS on the index just read, and only then are the objects removed.
```

Блоб, появившийся между проходами, **в списке не лежит** — отсрочка получается из структуры. Метки времени на объектах не нужны, и это существенно: у `variables://objects` их нет.

### 3. Порядок двух последних действий — и есть всё доказательство

```python
        self.vars.put(key, {"at": str(now), "digests": "[]"}, cas=idx)   # Conflict here deletes nothing
        deleted = sum(1 for d in doomed if self.objects.delete(self.sub.blob_key(d)))
```

Сначала **обратимое** — очистить строку под CAS. Проигранный CAS означает «пока мы думали, решение кто-то опроверг», и на этот момент **не удалено ни одного объекта**. Переставьте две строки местами — и то же самое стечение обстоятельств уничтожит байты.

### 4. Гонка, которую закрывает `put_blob`

Тот же дайджест можно загрузить для второй единицы, пока копия первой помечена. Тогда помеченный дайджест — ровно тот, который строка сейчас назовёт:

```python
        # These exact bytes may be sitting on the sweep's list right now — the same mask uploaded again
        # for a second unit, while the copy the first unit stopped naming is marked for collection.
        # Taking it off the list makes the sweep's own CAS fail, and a sweep that loses that CAS deletes
        # nothing at all. The alternative is a lock, for a window two store calls wide.
```

Обратите внимание, что здесь не понадобилось ничего нового: **это история согласованности самой платформы**, применённая к её собственной бухгалтерии.

### 5. Уборка ограничена, потому что её бухгалтерия — строка

```python
    SWEEP_LIMIT = 64
    SWEEP_GRACE = 300.0
```

`<name>/sweep` — Variable, а над Variable стоит потолок из урока 26. Дайджест весит 71 байт, и на девятистах кандидатах строка упрётся в 64 КиБ. Значит, уборка обязана брать не больше `limit` за проход — как `ensure_home(1)`. Правило, написанное два урока назад, применяется к тому, что написано под ним.

### 6. Тест, который ловит гонку изнутри прохода

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

### 7. Кто это запускает

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

Свой цикл и своя строка лога — урок 28, применённый **до** того, как ошибка сделана второй раз.

И две метрики, устроенные так, чтобы их можно было опрашивать раз в пятнадцать секунд:

```python
        # The sweep's backlog, for subsystems that have blobs to collect. Two cheap reads — a prefix
        # listing and one row — deliberately NOT `blobs_referenced()`, which walks every unit's row: a
        # gauge scraped every fifteen seconds must not cost a full scan of the configuration.
```

### 8. Грант, и что нашлось, когда его проверили

```hcl
    # `destroy` — the only grant in this cluster that lets anything remove an object, and it is bounded to
    # the one prefix whose contents can be proved unreferenced (М10A Lesson 29). Heartbeats and snapshot
    # shards are deliberately NOT here: a worker reads the heartbeat its previous instance left to measure
    # its own failover, so collecting them would collect the measurement.
    path "objects/vms/blobs/*" { capabilities = ["write", "read", "list", "destroy"] }   # the bytes of a blob field, beside the row that names them
```

Тест политик из урока 27 проверял только **объектные** ключи. Добавив проверку для строк, он немедленно нашёл два расхождения, которые там лежали:

```
console-policy.hcl does not let it write vms/sweep
console-policy.hcl does not let it write platform/drain
```

Первое — моё, свежее: `acl_console()` получил `<sub>/sweep`, а политика нет. Второе жило там с урока 22: **`platform/drain` объявлен в `acl_console()` с самого начала, а в файле его не было**, и плановая остановка на кластере получала бы отказ — машина, которую оператор собирался вывести аккуратно, ушла бы молчанием.

Вывод, который стоит унести: проверка, написанная наполовину, находит половину. Направление «всё, что код пишет, разрешено» надо применять ко **всем** ключам, а не к тем, ради которых тест писали.

## Что может пойти не так

- **Удалили в том же проходе, где заметили.** Это гонка с `put_blob`, и выглядит она как «оператор загрузил маску, ошибок нет, единица не стартует».
- **Переставили очистку и удаление.** Проигранный CAS начинает стоить байтов.
- **Забыли снять пометку в `put_blob`.** Остаётся узкое окно шириной в два вызова, и закрывать его придётся блокировкой.
- **Сделали уборку неограниченной.** Её собственная строка упрётся в потолок, и уборка сломается на том, что убирает.
- **Подмели heartbeat'ы «заодно».** Измеренное переключение исчезает, и `builds()` перестаёт отвечать, что здесь вообще работает.
- **Написали тест на гонку снаружи прохода.** Он пройдёт при любом порядке, и вы узнаете об этом только снятием — если повезёт.

## Итог

Уборка понадобилась одному классу объектов из трёх, и разница не в том, что блобы «мусорнее», а в том, что их **ключ адресуется содержимым**: новый набор байтов — новый вечный ключ. У heartbeat'а ключ переиспользуется и его устаревшая версия работает на измерение; у снапшота ключ на воркера. Поэтому `delete` появился у стора, а право им пользоваться — ровно у одного вызывающего и ровно на одном префиксе.

Само же решение оказалось не про удаление, а про **разнесение во времени**: заметить и убрать нельзя в один приём, потому что объект пишется раньше строки. Отсрочка вышла из структуры — кандидат, которого нет в списке, уже поэтому в безопасности, — а не из меток времени, которых у хранилища и нет. Обратимое действие поставлено перед необратимым, чтобы проигранный CAS ничего не стоил, и `put_blob` этот CAS умеет проиграть нарочно.

И два побочных урока, оба про тесты. Утверждение, которое проверяет тест, не проверяющий его, — хуже отсутствия теста: оно выглядит проверенным. А проверка, написанная для одной половины ключей, найдёт дефекты только в этой половине — вторая половина ждала с урока 22.

## Упражнения

1. **Уберите отсрочку** (`grace=0`) и попробуйте воспроизвести потерю блоба. Сколько попыток нужно и от чего это зависит?
2. **Переставьте очистку и удаление** и посмотрите, какой тест падает. Потом уберите из него `other.put_blob` — и объясните, почему он снова проходит.
3. **Посчитайте потолок строки `sweep`** при дайджесте в 71 байт. Какой `limit` вы бы поставили и почему не больше?
4. **Напишите уборку для `platform/resources/<server>/heartbeat`** — единственного ключа, который действительно растёт с оборотом железа. Что должно быть верно, чтобы её можно было запустить?
5. **Перенесите уборку в контроллер** и посмотрите, на чём она сломается. Что именно об этом говорит политика?
6. **Придумайте порог** на `<sub>_blobs_total` и `<sub>_blobs_marked`. Что означает растущий `marked` при неподвижном `total`?

## Что дальше

Модуль закончен, и на этот раз без долгов: последний записанный остаток стал кодом. Дальше — **М11**, где `variables://objects` перестаёт быть каталогом на диске, а `destroy` из шага 8 становится настоящим правом, которое выдаёт планировщик.
