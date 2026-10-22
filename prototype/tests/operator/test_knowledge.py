from concurrent.futures import ThreadPoolExecutor

import pytest

from repair_operator.knowledge import RecipeStore


@pytest.fixture
def store(tmp_path):
    return RecipeStore(tmp_path / "state" / "knowledge.db", promotion_threshold=3)


def test_lookup_empty(store):
    assert store.lookup("abc") is None
    assert store.similar("abc") == []


def test_candidate_verified_promoted(store):
    r = store.record_outcome("s1", "OOM_KILLED", "raise_memory_limit", {"container": "api"}, "healed")
    assert r.status == "verified" and store.lookup("s1") is None
    store.record_outcome("s1", "OOM_KILLED", "raise_memory_limit", {"container": "api"}, "healed")
    assert store.lookup("s1") is None
    store.record_outcome("s1", "OOM_KILLED", "raise_memory_limit", {"container": "api"}, "healed")
    got = store.lookup("s1")
    assert got.status == "promoted" and got.params == {"container": "api"} and got.rerun_passed == 3


def test_approved_does_not_promote(store):
    for _ in range(5):
        store.record_outcome("s2", "IMAGE_PULL", "fix_image_tag", {}, "approved")
    assert store.lookup("s2") is None
    assert store.get("s2").approved == 5 and store.get("s2").status == "candidate"


def test_demotion_after_failure(store):
    for _ in range(3):
        store.record_outcome("s3", "OOM_KILLED", "raise_memory_limit", {}, "healed")
    assert store.lookup("s3")
    store.record_outcome("s3", "OOM_KILLED", "raise_memory_limit", {}, "rejected")
    assert store.lookup("s3") is None
    assert store.get("s3").status == "demoted"
    store.record_outcome("s3", "OOM_KILLED", "raise_memory_limit", {}, "healed")
    assert store.get("s3").status == "demoted"  # stays demoted


def test_failure_before_promotion_does_not_demote(store):
    store.record_outcome("s4", "OOM_KILLED", "raise_memory_limit", {}, "healed")
    store.record_outcome("s4", "OOM_KILLED", "raise_memory_limit", {}, "failed")
    r = store.get("s4")
    assert r.status == "verified" and r.rejected == 1


def test_no_template_and_unknown_outcome(store):
    assert store.record_outcome("s5", "CRASH_LOOP", None, {}, "healed") is None
    with pytest.raises(ValueError):
        store.record_outcome("s5", "X", "t", {}, "weird")


def test_threshold_setting(tmp_path):
    s = RecipeStore(tmp_path / "k.db", promotion_threshold=1)
    s.record_outcome("a", "X", "t", {}, "healed")
    assert s.lookup("a")


def test_concurrent_writes(store):
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda _: store.record_outcome("c", "OOM_KILLED", "t", {}, "approved"), range(20)))
    assert store.get("c").approved == 20
