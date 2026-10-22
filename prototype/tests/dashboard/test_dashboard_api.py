import pytest
from fastapi.testclient import TestClient

from repair_dashboard.app import app, get_reader
from repair_dashboard.kube import FakeKubeReader, KubeClientReader

SVC = {"name": "api", "namespace": "demo", "ready": 1, "desired": 1, "status": "green", "pods": []}
INC = {"name": "ri-1", "namespace": "demo", "phase": "Healed", "class": "OOM_KILLED"}


@pytest.fixture
def make_client():
    def _make(reader):
        app.dependency_overrides[get_reader] = lambda: reader
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_endpoints_return_fake_data(make_client):
    c = make_client(
        FakeKubeReader(
            services=[SVC, {**SVC, "name": "x", "namespace": "other"}],
            apps=[{"name": "demo-app"}],
            workflows=[{"name": "w1", "namespace": "demo"}],
            incidents=[INC],
        )
    )
    assert [s["name"] for s in c.get("/api/services").json()] == ["api"]  # only demo namespaces
    assert c.get("/api/argocd").json() == [{"name": "demo-app"}]
    assert c.get("/api/workflows").json() == [{"name": "w1", "namespace": "demo"}]
    assert c.get("/api/incidents").json() == [INC]
    assert c.get("/api/incidents/demo/ri-1").json() == INC
    assert c.get("/api/health").json() == {"status": "ok", "cluster": True, "context": "kind-repair"}


def test_incident_not_found(make_client):
    assert make_client(FakeKubeReader()).get("/api/incidents/demo/nope").status_code == 404


def test_empty_cluster_gives_empty_lists(make_client):
    c = make_client(FakeKubeReader())
    for url in ("/api/services", "/api/argocd", "/api/workflows", "/api/incidents"):
        assert c.get(url).json() == []


def test_unreachable_cluster(make_client):
    c = make_client(FakeKubeReader(error="boom"))
    r = c.get("/api/services")
    assert r.status_code == 503 and r.json()["error"] == "cluster unavailable"
    h = c.get("/api/health")
    assert h.status_code == 200 and h.json()["cluster"] is False


def test_static_ui_served(make_client):
    c = make_client(FakeKubeReader())
    r = c.get("/")
    assert r.status_code == 200 and "Fault injection" in r.text
    assert c.get("/app.js").status_code == 200 and c.get("/style.css").status_code == 200


def test_real_reader_without_cluster_is_unavailable(monkeypatch, tmp_path):
    from kubernetes import config

    from repair_dashboard.kube import KubeUnavailable

    def boom(*a, **kw):
        raise config.ConfigException("no config")

    monkeypatch.setattr(config, "load_kube_config", boom)
    monkeypatch.setattr(config, "load_incluster_config", boom)

    with pytest.raises(KubeUnavailable):
        KubeClientReader("kind-repair").argocd_apps()


def test_workflows_aggregated_from_demo_and_demo_b_only(make_client):
    wfs = [
        {"name": "a", "namespace": "demo", "startedAt": "2026-01-01T00:00:01Z"},
        {"name": "b", "namespace": "demo-b", "startedAt": "2026-01-01T00:00:03Z"},
        {"name": "c", "namespace": "demo", "startedAt": "2026-01-01T00:00:02Z"},
        {"name": "old", "namespace": "argo", "startedAt": "2026-01-01T00:00:09Z"},
    ]
    c = make_client(FakeKubeReader(workflows=wfs))
    assert [w["name"] for w in c.get("/api/workflows").json()] == ["b", "c", "a"]


def test_workflows_limited_to_latest_20(make_client):
    wfs = [
        {"name": f"w{i:02d}", "namespace": "demo-b" if i % 2 else "demo", "startedAt": f"2026-01-01T00:00:{i:02d}Z"}
        for i in range(30)
    ]
    c = make_client(FakeKubeReader(workflows=wfs))
    names = [w["name"] for w in c.get("/api/workflows").json()]
    assert len(names) == 20 and names[0] == "w29" and names[-1] == "w10"
