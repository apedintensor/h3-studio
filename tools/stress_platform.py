"""Bounded, in-process HTTP control-plane load test; no GPU/cloud requests.

Uses a new explicitly named .platform-stress-* directory and two synthetic
accounts. This measures local ASGI/DB behavior, not internet or inference speed.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.repository import Repository, Scope


@contextmanager
def test_database(root, backend):
    """A dedicated local test database only; never accept a deployment DSN."""
    if backend == "sqlite":
        yield "sqlite:///" + (root / "platform.sqlite3").as_posix()
        return
    if backend != "postgres-test":
        raise ValueError("Unknown test backend")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    raw = os.environ.get("PLATFORM_TEST_DATABASE_URL", "")
    parsed = make_url(raw)
    if (parsed.drivername != "postgresql+psycopg" or parsed.host != "127.0.0.1"
            or parsed.database != "sixnine_test" or parsed.username != "sixnine_test"
            or parsed.port not in {5432, 55469} or parsed.query):
        raise ValueError("Only the explicit loopback sixnine_test fixture is permitted")
    schema = "stress_test_" + uuid.uuid4().hex
    bootstrap = create_engine(parsed, echo=False)
    created = False
    try:
        with bootstrap.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        created = True
        yield parsed.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    finally:
        if created:
            # The name comes only from the UUID above, never from a user path,
            # CLI input or an existing schema discovered by scanning.
            with bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        bootstrap.dispose()


def project(ident, owner):
    def entity(identity, kind, parent=None):
        return {"id": identity, "type": kind, "parentId": parent, "title": identity, "description": "Synthetic local load fixture",
                "version": 1, "order": 0, "status": "draft", "data": {"seconds": 5} if kind == "shot" else {}}
    return {"schemaVersion": 4, "id": ident, "title": owner+" fixture", "logline": "A bounded test, not user content",
            "entities": [entity("chapter", "chapter"), entity("scene", "scene", "chapter"), entity("shot", "shot", "scene")],
            "links": [], "jobs": [], "layout": {"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
            "journey": {"brief": {"note": "Synthetic larger document "+"x"*64000}}}


async def run(root, *, requests=600, concurrency=12, project_count=20, database="sqlite"):
    root = root.resolve()
    if root.parent != ROOT or not root.name.startswith(".platform-stress-") or root.exists():
        raise ValueError("Use a new .platform-stress-* direct child of this repository")
    if not 10 <= requests <= 2000 or not 1 <= concurrency <= 24 or not 2 <= project_count <= 50:
        raise ValueError("Load limits exceeded")
    root.mkdir(mode=0o700)
    with test_database(root, database) as url:
        repository = Repository(url)
        try:
            app = create_app(Settings(root, database_url=url, auth_mode="password"), repository=repository)
            return await measure(app, root, requests=requests, concurrency=concurrency,
                                 project_count=project_count, database=database)
        finally:
            repository.close()


async def measure(app, root, *, requests, concurrency, project_count, database):
    clients, expected_jobs = [], {}
    started = time.time()
    try:
        for owner in ("superdan", "supervan"):
            password = secrets.token_urlsafe(24)
            app.state.auth.set_password(owner, password)
        # Both accounts must exist before a real password-mode session opens.
        for owner in ("superdan", "supervan"):
            password = secrets.token_urlsafe(24)
            app.state.auth.set_password(owner, password)
            token = app.state.auth.login(owner, password, source="load-fixture-"+owner)
            del password
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8845",
                                       cookies={"sixnine_session": token}, timeout=15)
            clients.append(client)
            expected_jobs[owner] = set()
            for number in range(project_count):
                ident = "load-"+str(number)
                app.state.repository.put_document(Scope("sixnine", owner, "__projects"), "project", ident, project(ident, owner), expected_version=0)
                response = await client.post("/v1/generation-plans", json={
                    "client_ref": {"project_id": ident, "shot_id": "shot", "shot_version": 1},
                    "recipe_id": "h3-base-fl2va-v1", "prompt": "A quiet synthetic test scene.",
                    "controls": {"duration": 5, "resolution": "480P"}})
                response.raise_for_status()
                plan_id = response.json()["plan_id"]
                for variant in range(3):
                    response = await client.post("/v1/jobs", json={"plan_id": plan_id},
                        headers={"Idempotency-Key": f"load-{number}-{variant}"})
                    response.raise_for_status()
                    expected_jobs[owner].add(response.json()["id"])
                    if response.json()["status"] != "blocked":
                        raise RuntimeError("Test unexpectedly enabled generation")
        semaphore = asyncio.Semaphore(concurrency)
        durations, statuses, errors = [], Counter(), []
        total_bytes = 0
        async def request(number):
            nonlocal total_bytes
            async with semaphore:
                tick = time.perf_counter()
                try:
                    client = clients[number % 2]
                    endpoint = ("/v1/jobs?limit=100", "/v1/projects?limit=20", "/v1/activity-summary?client_project_id=load-0")[number % 3]
                    response = await client.get(endpoint)
                    statuses[str(response.status_code)] += 1
                    total_bytes += len(response.content)
                    if response.status_code != 200:
                        errors.append("non_200_"+str(response.status_code))
                    expected_owner = "superdan" if number % 2 == 0 else "supervan"
                    if response.status_code == 200 and endpoint.startswith("/v1/projects"):
                        if any(item["title"] != expected_owner+" fixture" for item in response.json()["projects"]):
                            errors.append("cross_owner_title")
                    elif response.status_code == 200 and endpoint.startswith("/v1/jobs"):
                        observed = {item["id"] for item in response.json()["jobs"]}
                        expected = expected_jobs[expected_owner]
                        if not observed.issubset(expected) or len(observed) != min(100, len(expected)):
                            errors.append("job_list_owner_or_count_mismatch")
                    elif response.status_code == 200 and response.json()["total"] != 3:
                        errors.append("activity_summary_scope_mismatch")
                except Exception:
                    errors.append("request_exception_details_suppressed")
                finally:
                    durations.append(time.perf_counter()-tick)
        measurement = time.perf_counter()
        await asyncio.gather(*(request(number) for number in range(requests)))
        seconds = time.perf_counter()-measurement
        ordered = sorted(durations)
        def percentile(fraction):
            return round(ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))]*1000, 2)
        report = {"kind": "LOCAL_ASGI_CONTROL_PLANE_NOT_NETWORK_OR_GPU_BENCHMARK", "started_at": started,
            "finished_at": time.time(), "database": "isolated_"+database, "auth_mode": "password", "owners": 2,
            "projects": project_count*2, "blocked_jobs": project_count*6, "concurrency": concurrency,
            "requests": requests, "statuses": dict(statuses), "errors": dict(Counter(errors)), "gpu_calls": 0, "cloud_calls": 0,
            "measurement_seconds": round(seconds, 3), "requests_per_second": round(requests/seconds, 2),
            "latency_ms": {"p50": percentile(.50), "p95": percentile(.95), "p99": percentile(.99), "max": round(max(durations)*1000, 2)},
            "response_bytes": total_bytes, "all_passed": not errors}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        for client in clients:
            await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=600)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--database", choices=("sqlite", "postgres-test"), default="sqlite")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output, requests=args.requests, concurrency=args.concurrency, database=args.database))
    except Exception:
        # DB exceptions can contain a DSN or SQL parameters. Do not serialize
        # them or a chained traceback, including on a failed cleanup.
        print(json.dumps({"all_passed": False, "error": "isolated_load_test_failed_details_suppressed"}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
