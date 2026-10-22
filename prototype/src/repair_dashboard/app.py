"""Read-only dashboard API + static UI. Run: uvicorn repair_dashboard.app:app --port 8090"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .kube import DEFAULT_NAMESPACES, WORKFLOW_NAMESPACES, KubeClientReader, KubeReader, KubeUnavailable

STATIC = Path(__file__).parent / "static"
CONTEXT = os.environ.get("REPAIR_KUBE_CONTEXT", "kind-repair")

app = FastAPI(title="Repair loop dashboard")


@lru_cache
def _reader() -> KubeReader:
    return KubeClientReader(CONTEXT)


def get_reader() -> KubeReader:
    return _reader()


@app.exception_handler(KubeUnavailable)
async def _unavailable(_request, exc: KubeUnavailable):
    return JSONResponse(status_code=503, content={"error": "cluster unavailable", "detail": str(exc)})


Reader = Depends(get_reader)


@app.get("/api/health")
def health(reader: KubeReader = Reader):
    try:
        reader.argocd_apps()
        return {"status": "ok", "cluster": True, "context": CONTEXT}
    except KubeUnavailable as exc:
        return {"status": "ok", "cluster": False, "context": CONTEXT, "detail": str(exc)}


@app.get("/api/services")
def services(reader: KubeReader = Reader):
    return reader.services(list(DEFAULT_NAMESPACES))


@app.get("/api/argocd")
def argocd(reader: KubeReader = Reader):
    return reader.argocd_apps()


@app.get("/api/workflows")
def workflows(reader: KubeReader = Reader):
    return reader.workflows(list(WORKFLOW_NAMESPACES))


@app.get("/api/incidents")
def incidents(reader: KubeReader = Reader):
    return reader.incidents()


@app.get("/api/incidents/{ns}/{name}")
def incident(ns: str, name: str, reader: KubeReader = Reader):
    found = reader.incident(ns, name)
    if found is None:
        raise HTTPException(404, "incident not found")
    return found


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
