"""Lesson 4 — who may call it: tokens verified offline; users never reach a
worker; the agent carries keys, revocations and the cluster's grants and
nothing else; grants are cluster-local with expiry; the revocation window
is stated then measured; break-glass is one account, audited."""
from cluster.variables import Forbidden
from domain.agent import KEYS_PATH, DomainAgent, DomainPublisher, ClusterTrust
from domain.grants import GRANT_LIFETIME, ClusterAuthoriser, ClusterGrants, Grant, revocation_window
from domain.identity import TOKEN_LIFETIME, AuthError, BreakGlass, IdentityStore
from domain.signer import Signer
from domain.tokens import Expired, Revoked, RevocationList, UnknownKey, verify
from tests.conftest import Clock, make_domain


def _domain(clk):
    fed, links = make_domain({"north": (), "south": ()}, "north")
    dc = fed.domain_cluster
    signer = Signer("acme", dc.vars, now=clk)
    return fed, links, dc, signer


def test_login_ends_in_a_token_naming_the_subject_and_nothing_else():
    clk = Clock(1000.0)
    fed, _, dc, signer = _domain(clk)
    ids = IdentityStore(signer, dc.vars, dc.objects, now=clk)
    ids.create_local("alice", "correct horse", roles=["operator"])
    tok = ids.login("alice", "correct horse")
    payload = verify(tok, signer.tokens.keyset(), now=clk())
    assert payload["sub"] == "alice" and payload["exp"] == 1000.0 + TOKEN_LIFETIME
    assert "roles" not in payload and "grants" not in payload            # rights are the cluster's grants, not the token's
    try:
        ids.login("alice", "wrong"); raise AssertionError()
    except AuthError:
        pass
    ids.create_federated("bob", idp_subject="bob@corp.example", roles=["admin"])
    assert ids.get("bob").pwhash == ""                                     # an IdP user has no secret here
    assert verify(ids.login_federated({"sub": "bob@corp.example"}), signer.tokens.keyset(), now=clk())["sub"] == "bob"
    clk.advance(TOKEN_LIFETIME + 61)
    try:
        verify(tok, signer.tokens.keyset(), now=clk()); raise AssertionError()
    except Expired:
        pass


def test_nothing_about_a_user_reaches_a_worker_only_trust_does():
    clk = Clock(1000.0)
    fed, links, dc, signer = _domain(clk)
    south = fed.clusters["south"]
    ids = IdentityStore(signer, dc.vars, dc.objects, now=clk)
    ids.create_local("alice", "pw", ["operator"])
    DomainPublisher(dc.vars).publish_keys(signer.tokens.keyset())
    agent = DomainAgent("south", dc.vars, south.vars.as_writer("agent"), now=clk)
    south.vars.acl = {"agent": ["domain/*"]}
    assert agent.sync()
    assert south.vars.list("identity/") == [] and south.vars.list("domain/") == ["domain/keys"]   # keys, no people
    for path in ("vms/cameras/7", "vms/epoch/7", "vms/slots/w-0"):
        try:
            south.vars.as_writer("agent").put(path, {"x": 1}); raise AssertionError()
        except Forbidden:
            pass                                                           # the agent's only right is domain/*: not the controller's, not a worker's
    trust = ClusterTrust(south.vars)
    tok = ids.login("alice", "pw")
    assert verify(tok, trust.keyset(), trust.revoked(), now=clk())["sub"] == "alice"   # verified by south's console from south's OWN Variables


def test_domain_down_clusters_keep_verifying_nobody_new_logs_in():
    clk = Clock(1000.0)
    fed, links, dc, signer = _domain(clk)
    south = fed.clusters["south"]
    ids = IdentityStore(signer, dc.vars, dc.objects, now=clk)
    ids.create_local("alice", "pw", ["operator"])
    DomainPublisher(dc.vars).publish_keys(signer.tokens.keyset())
    agent = DomainAgent("south", dc.vars, south.vars, now=clk)
    agent.sync()
    tok = ids.login("alice", "pw")
    links["north"].up = False                                              # the domain cluster is gone
    assert agent.sync() is False and agent.last_synced == 1000.0           # the agent stops updating, writes nothing
    trust = ClusterTrust(south.vars)
    assert verify(tok, trust.keyset(), now=clk())["sub"] == "alice"        # existing tokens: fine, offline
    try:
        ids.login("alice", "pw"); raise AssertionError()
    except Exception:
        pass                                                               # nobody NEW logs in


def test_revocation_travels_by_the_agent_and_rotation_overlaps():
    clk = Clock(1000.0)
    fed, links, dc, signer = _domain(clk)
    south = fed.clusters["south"]
    pub = DomainPublisher(dc.vars)
    pub.publish_keys(signer.tokens.keyset())
    agent = DomainAgent("south", dc.vars, south.vars, now=clk); agent.sync()
    tok = signer.tokens.issue("mallory", 900, now=clk())
    rl = RevocationList(); rl.revoke(verify(tok, signer.tokens.keyset(), now=clk()))
    pub.publish_revoked(rl); agent.sync()
    trust = ClusterTrust(south.vars)
    try:
        verify(tok, trust.keyset(), trust.revoked(), now=clk()); raise AssertionError()
    except Revoked:
        pass
    old_tok = signer.tokens.issue("alice", 900, now=clk())
    signer.tokens.rotate(overlap=600, now=clk()); pub.publish_keys(signer.tokens.keyset()); agent.sync()
    trust = ClusterTrust(south.vars)
    assert verify(old_tok, trust.keyset(), now=clk())["sub"] == "alice"    # the previous key is still in the set
    new_tok = signer.tokens.issue("alice", 900, now=clk())
    assert verify(new_tok, trust.keyset(), now=clk())["sub"] == "alice"
    clk.advance(700)
    try:
        verify(old_tok, trust.keyset(), now=clk()); raise AssertionError()
    except UnknownKey:
        pass                                                               # overlap over: the old key is retired


def test_grants_are_cluster_local_carried_by_the_agent_and_expiry_is_the_revocation_mechanism():
    clk = Clock(1000.0)
    fed, links, dc, signer = _domain(clk)
    south = fed.clusters["south"]
    pub = DomainPublisher(dc.vars); pub.publish_keys(signer.tokens.keyset())
    pub.publish_grants("south", [Grant("alice", "view", None, clk() + GRANT_LIFETIME), Grant("alice", "edit", 7, clk() + GRANT_LIFETIME)])
    agent = DomainAgent("south", dc.vars, south.vars, now=clk); agent.sync()
    assert sorted(south.vars.list("domain/")) == ["domain/grants", "domain/keys"]   # what reached the cluster: trust and grants, no user
    g = ClusterGrants("south", now=clk)
    g.renew_from_domain(ClusterTrust(south.vars).grants())                            # the console loads them from ITS cluster
    auth = ClusterAuthoriser(g, signer.tokens.keyset(), now=clk)
    tok = signer.tokens.issue("alice", TOKEN_LIFETIME, now=clk())
    assert auth.authorise(tok, "view", 12) == "alice" and auth.authorise(tok, "edit", 7) == "alice"
    try:
        auth.authorise(tok, "edit", 12); raise AssertionError()
    except PermissionError as e:
        assert "no edit grant on camera 12 in south" in str(e)
    # The revoke cannot reach south (unreachable). State in advance when access ends:
    stated = g.access_ends("alice", token_exp=clk() + TOKEN_LIFETIME)
    assert stated == clk() + TOKEN_LIFETIME                                  # the token is the shorter lifetime here
    assert revocation_window(TOKEN_LIFETIME, GRANT_LIFETIME) == TOKEN_LIFETIME
    # ...then measure it: the token expires, the grant is still there, access is gone anyway.
    clk.advance(TOKEN_LIFETIME + 61)
    try:
        auth.authorise(tok, "view", 12); raise AssertionError()
    except PermissionError as e:
        assert "expired" in str(e)
    # And the other direction: a fresh token, but south's agent could not renew its grants past their expiry.
    clk.advance(GRANT_LIFETIME)
    tok2 = signer.tokens.issue("alice", TOKEN_LIFETIME, now=clk())         # the domain is back and issues a fresh token...
    try:
        auth.authorise(tok2, "view", 12); raise AssertionError()
    except PermissionError as e:
        assert "no view grant" in str(e)
    # The agent renews what the domain still grants — and drops what it does not.
    g.renew_from_domain([Grant("alice", "view", None, clk() + GRANT_LIFETIME)])
    assert g.may("alice", "view", 12) and not g.may("alice", "edit", 7)


def test_identity_publishes_object_first_then_pointer_and_restores_elsewhere():
    clk = Clock(1000.0)
    fed, links, dc, signer = _domain(clk)
    ids = IdentityStore(signer, dc.vars, dc.objects, publish_floor=0, now=clk)
    ids.create_local("alice", "pw", ["operator"]); ids.create_federated("bob", "bob@corp", ["admin"])
    assert ids.publish() and ids.publishes == 1 and not ids.publish()
    ptr, _ = dc.vars.get("identity/pointer")
    assert ptr["object"] == "identity/rev-1" and dc.objects.get("identity/rev-1") is not None
    ids.set_roles("alice", ["admin"]); assert ids.publish() and dc.vars.get("identity/pointer")[0]["revision"] == "2"
    # The domain cluster dies. Re-host in south: the backed-up key, then the identity object.
    backup = signer.backup()
    south = fed.clusters["south"]
    objs_backup = south.objects; objs_backup.put("identity/rev-2", dc.objects.get("identity/rev-2"))
    links["north"].up = False
    signer2 = Signer.restore("acme", south.vars, backup, now=clk)
    ids2 = IdentityStore.restore(signer2, south.vars, objs_backup, ptr | {"object": "identity/rev-2", "revision": "2"}, now=clk)
    assert {u.id: u.roles for u in ids2.users()} == {"alice": ["admin"], "bob": ["admin"]}
    assert verify(ids2.login("alice", "pw"), signer2.tokens.keyset(), now=clk())["sub"] == "alice"   # same key: old tokens too
    assert signer2.root.cert.subject == signer.root.cert.subject


def test_prefs_are_objects_and_a_stale_tab_is_told():
    from cluster.variables import Conflict
    clk = Clock(1000.0)
    fed, _, dc, signer = _domain(clk)
    ids = IdentityStore(signer, dc.vars, dc.objects, now=clk)
    assert ids.get_prefs("alice") == ({}, 0)
    assert ids.put_prefs("alice", {"wall": [1, 2, 3]}, base_revision=0) == 1
    assert ids.put_prefs("alice", {"wall": [1, 2]}, base_revision=1) == 2
    try:
        ids.put_prefs("alice", {"wall": []}, base_revision=1); raise AssertionError()
    except Conflict:
        pass
    assert ids.get_prefs("alice") == ({"wall": [1, 2]}, 2)
    assert dc.vars.list("users/") == []                                    # nothing of this in raft


def test_break_glass_is_one_account_audited_and_alarmed():
    clk = Clock(1000.0)
    fed, _, dc, signer = _domain(clk)
    bg = BreakGlass.create(signer, "emergency-pw")
    try:
        bg.use("nope", who="carol", why="uplink down", now=clk()); raise AssertionError()
    except AuthError:
        pass
    tok = bg.use("emergency-pw", who="carol", why="uplink down, token expired", now=clk())
    p = verify(tok, signer.tokens.keyset(), now=clk())
    assert p["sub"] == "break-glass" and p["via"] == "break-glass" and p["who"] == "carol"
    assert len(bg.audit) == 2 and bg.alarm[-1].startswith("BREAK-GLASS used by carol") and bg.used_since_rotation == 1
    bg.rotate("new-pw"); assert bg.used_since_rotation == 0
