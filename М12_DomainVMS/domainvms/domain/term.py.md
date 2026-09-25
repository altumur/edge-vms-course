# term.py — Lesson 15: the domain hosted on a camera — a term, a signed backup beyond the host, re-hosting as an ordinary operation

**Role in the module.** On a site with no server the designated domain cluster is a camera, and cameras die. Re-hosting becomes ordinary, and three things follow: a TERM (the epoch one level up — a larger term wins, a returning host steps down on its next read), STATE BEYOND THE HOST (what only the host holds — Lesson 9's kept edits for a member that is off above all — published as a signed backup that chosen members' agents carry, like the shared settings), and NOTHING LOST SILENTLY (an old host's un-backed-up changes listed for a person). The signer's key is never in the backup; it comes from where Lesson 7 put it.

## Module-level names
- `HOST = "domain/host"` (a signed `{term, host, at}` in the host and in every member), `BACKUP = "domain/backup"` (per-member pointer `domain/backup/<member>` in the host; the member's copy and document), `EXPORTED` — the prefixes the backup carries: pending, grants, crossings, sources, mirrors, the settings pointer.

## `class Deposed`, `class TwoHosts`

## Functions
### `read_host(vars_, keys, now)` — the verified host record, or `None`.
### `carry_host(domain_vars, member_vars, keys, now) -> str` — the agent's side: carry the record only if it verifies and its term is LARGER than the member's; never backwards.
### `find_host(fed, member_vars, keys, now) -> str | None` — the largest verified term among the member's own record and reachable members' claims about THEMSELVES; two hosts with one term raise `TwoHosts`. A host that is off stays the host if nobody holds a larger term.
### `rehost(fed, new, signer_backup, domain_id, objects_of, wall) -> (DomainHost, report)` — restore the signer on `new`; among reachable members, the largest term carried and the newest backup that verifies; restore its state; publish keys; flag `new` as the domain cluster; term = largest seen + 1; claim. The report names the term, the backup and its keeper, what was ignored, and says what is not there.
### `stranded(old_vars, restored_state) -> list` — every exported item on a returning old host that differs from what the new term was restored from.

## `class DomainHost`
`claim()`; `export()`; `backup(targets, objects) -> rev` (guarded: a deposed host may not publish); `check()` — False, and `deposed_by` set, if any reachable member carries a larger term; `guard()` raises `Deposed` naming the new host and term.

## Notes
- `test_every_rehost_takes_a_larger_term_than_any_member_has_seen`: the second re-host in a month takes term 3.
- `test_a_host_restored_without_the_signers_key_is_followed_by_nobody`: no backup verifies and no member switches.
