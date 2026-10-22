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
            workflows=[{"name": "w1", "namespace": "argo"}],
            incidents=[INC],
        )
    )
    assert [s["name"] for s in c.get("/api/services").json()] == ["api"]  # only demo namespaces
    assert c.get("/api/argocd").json() == [{"name": "demo-app"}]
    assert c.get("/api/workflows").json() == [{"name": "w1", "namespace": "argo"}]
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
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing"))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    from repair_dashboard.kube import KubeUnavailable

    with pytest.raises(KubeUnavailable):
        KubeClientReader("kind-repair").argocd_apps()
