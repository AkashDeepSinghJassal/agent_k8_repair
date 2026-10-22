import inspect

from repair_operator.fingerprint import dedupe_fingerprint, knowledge_signature, normalise_line

BASE = [
    "2026-11-03T10:15:42.123Z ERROR cannot connect to 10.244.0.17:5432 for pod api-7d9f8b6c5-x2k9z",
    "image sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 failed",
]
OTHER = [
    "2027-01-09T23:59:01.999Z ERROR cannot connect to 10.244.3.250:5432 for pod api-5c4b7f9d8-q7w8e",
    "image sha256:aaaaaaaaaaaabbbbbbbbbbbbcccccccccccc111111111111222222222222dddd failed",
]


def sig(lines, reason="CrashLoopBackOff"):
    return knowledge_signature(reason, 1, "container", lines)


def test_dedupe_is_stable_sha256_and_sensitive():
    a = dedupe_fingerprint("demo", "Deployment/api", "api", "OOMKilled")
    assert len(a) == 64 and a == dedupe_fingerprint("demo", "Deployment/api", "api", "OOMKilled")
    assert a != dedupe_fingerprint("demo-b", "Deployment/api", "api", "OOMKilled")
    assert a != dedupe_fingerprint("demo", "Deployment/api", "api", "ErrImagePull")


def test_normalise_placeholders():
    out = normalise_line(BASE[0])
    assert "<TS>" in out and "<IP>" in out and "<POD>" in out
    assert "<HASH>" in normalise_line(BASE[1])
    assert normalise_line("took 123456 ms, retry 3") == "took <N> ms, retry 3"
    assert normalise_line("secret 'db-pass' not found") == "secret <NAME> not found"


def test_volatile_parts_do_not_change_signature():
    assert sig(BASE) == sig(OTHER)
    assert len(sig(BASE)) == 12


def test_each_volatile_kind_alone():
    t = "connect to {} failed"
    for a, b in [
        ("10.0.0.1:80", "10.9.9.9:8080"),
        ("2026-01-01 10:00:00", "2027-02-02 11:11:11"),
        ("api-7d9f8b6c5-x2k9z", "web-5c4b7f9d8-q7w8e"),
        ("deadbeefcafe0123", "0123456789abcdef"),
        ("'foo'", '"bar"'),
        ("pid 123456", "pid 987654"),
    ]:
        assert sig([t.format(a)]) == sig([t.format(b)]), (a, b)


def test_different_template_changes_signature():
    assert sig(["connection refused"]) != sig(["permission denied"])
    assert sig(BASE, reason="OOMKilled") != sig(BASE)
    assert knowledge_signature("r", 1, "c", ["x"]) != knowledge_signature("r", 137, "c", ["x"])
    assert knowledge_signature("r", 1, "c", ["x"]) != knowledge_signature("r", 1, "step", ["x"])
    assert knowledge_signature("r", 1, "c", ["x"]) != knowledge_signature("r", 1, "c", ["x"], ["SCONE-42"])


def test_order_and_duplicates_ignored():
    assert sig(["a b", "c d", "a b"]) == sig(["c d", "a b"])


def test_namespace_never_affects_signature():
    assert "namespace" not in inspect.signature(knowledge_signature).parameters
    # same error template, resource names differ (quoted) as they would across namespaces
    assert sig(['configmaps "api-config" not found']) == sig(['configmaps "other-cfg" not found'])
