"""Test harness: everything runs offline against dev/standin.py.

Each test gets its own scratch directory, its own stand-in (arXiv feeds, arXiv API, PDF host and
model endpoint in one small server) and its own database, and starts the real app with uvicorn
as a subprocess, so restarts, kills and several processes on one database are the real thing.

The database is a SQLite file by default. Set ARXIVLENS_TEST_DATABASE_URL to an empty PostgreSQL
database (postgresql+asyncpg://user:password@host/dbname) to run the same tests on PostgreSQL;
its public schema is dropped and recreated before every test.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_DATABASE_URL = os.environ.get("ARXIVLENS_TEST_DATABASE_URL", "")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(condition, timeout: float = 60, interval: float = 0.1, what: str = "condition"):
    """Poll until condition() returns something truthy and return it."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def db_run(url: str, fn):
    """Run fn(sync_connection) in one transaction on the database and return its result."""

    async def main():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class App:
    """One running app process."""

    def __init__(self, proc: subprocess.Popen, port: int, log_path: Path):
        self.proc = proc
        self.port = port
        self.log_path = log_path
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False)

    # -- plumbing ----------------------------------------------------------
    def log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def wait_ready(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"app exited with {self.proc.returncode}:\n{self.log_tail()}")
            try:
                if self.http.get("/status").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise AssertionError(f"app did not come up in {timeout}s:\n{self.log_tail()}")

    def stop(self, timeout: float = 40) -> None:
        """Ordinary shutdown (SIGTERM)."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.http.close()

    def kill(self) -> None:
        """The process dies on the spot (SIGKILL), with no chance to clean up."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self.http.close()

    # -- the API -----------------------------------------------------------
    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.http.get(path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.http.post(path, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self.http.delete(path, **kwargs)

    def status(self) -> dict:
        r = self.get("/status")
        assert r.status_code == 200, r.text
        return r.json()

    def wait_idle(self, timeout: float = 120) -> dict:
        """Wait until no cycle is running in this process; returns the status then."""
        return wait_until(
            lambda: (lambda s: s if not s["running"] else None)(self.status()),
            timeout=timeout, what="the cycle to finish",
        )

    def start_cycle(self) -> dict:
        r = self.post("/process")
        assert r.status_code == 202, r.text
        return r.json()

    def cycle(self, timeout: float = 120) -> dict:
        """Run one full cycle to its end and return its record (the `run` of GET /status)."""
        self.wait_idle(timeout)
        started = self.start_cycle()
        assert started["started"] is True, started
        run = self.wait_idle(timeout)["run"]
        assert run["id"] == started["run_id"], (run, started)
        assert run["finished_at"] is not None, run
        return run

    def papers(self, **params) -> list[dict]:
        r = self.get("/papers", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def paper(self, arxiv_id: str) -> dict | None:
        for p in self.papers():
            if p["arxiv_id"] == arxiv_id:
                return p
        return None

    def by_arxiv_id(self, **params) -> dict[str, dict]:
        return {p["arxiv_id"]: p for p in self.papers(**params)}

    def categories(self) -> dict[str, dict]:
        r = self.get("/categories")
        assert r.status_code == 200, r.text
        return {c["arxiv_code"]: c for c in r.json()}

    def add_category(self, code: str) -> dict:
        r = self.post("/categories", json={"arxiv_code": code})
        assert r.status_code == 201, r.text
        return r.json()


class Env:
    """The world of one test: scratch directory, stand-in, database, app processes."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.scenario_path = tmp / "scenario.json"
        self.categories_file = tmp / "categories.yaml"
        self.standin_port = free_port()
        self.standin_url = f"http://127.0.0.1:{self.standin_port}"
        self.database_url = EXTERNAL_DATABASE_URL or f"sqlite+aiosqlite:///{tmp / 'app.db'}"
        self.apps: list[App] = []
        self._standin: subprocess.Popen | None = None
        self._http = httpx.Client(timeout=30, trust_env=False)
        self.set_scenario()
        self.set_yaml_categories([])

    # -- stand-in ------------------------------------------------------------
    def set_scenario(self, **scenario) -> None:
        """Rewrite what the outside world answers. The stand-in re-reads it on every request."""
        scenario.setdefault("feeds", {})
        tmp_file = self.scenario_path.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(scenario), encoding="utf-8")
        os.replace(tmp_file, self.scenario_path)

    def set_yaml_categories(self, codes: list[str]) -> None:
        lines = ["categories:"] if codes else ["categories: []"]
        for code in codes:
            lines += [f'  - name: "Category {code}"', f'    arxiv_code: "{code}"']
        self.categories_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def start_standin(self) -> None:
        self._standin = subprocess.Popen(
            [sys.executable, str(ROOT / "dev" / "standin.py"), str(self.scenario_path), str(self.standin_port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )

        def up():
            assert self._standin.poll() is None, "stand-in exited"
            try:
                return self._http.get(f"{self.standin_url}/_n").status_code == 200
            except httpx.TransportError:
                return False

        wait_until(up, timeout=60, what="the stand-in")

    def counts(self) -> dict[str, int]:
        """How often each stand-in service was asked (rss:<cat>, api, pdf:<id>, llm:<id>)."""
        return self._http.get(f"{self.standin_url}/_n").json()

    def pdf_url(self, arxiv_id: str, version: int) -> str:
        return f"{self.standin_url}/pdf/{arxiv_id}v{version}"

    # -- app -----------------------------------------------------------------
    def app_environ(self, **overrides) -> dict[str, str]:
        environ = dict(os.environ)
        environ.update(
            DATABASE_URL=self.database_url,
            ARXIV_RSS_BASE=f"{self.standin_url}/rss",
            ARXIV_API_URL=f"{self.standin_url}/api/query",
            ARXIV_API_DELAY_SECONDS="0",
            AZURE_OPENAI_ENDPOINT=self.standin_url,
            AZURE_OPENAI_API_KEY="local",
            AZURE_BLOB_CONNECTION_STRING="",
            AZURE_STORAGE_ACCOUNT_NAME="",
            SCHEDULER_ENABLED="false",
            CATEGORIES_FILE=str(self.categories_file),
            DATA_DIR=str(self.tmp / "data"),
            PYTHONUNBUFFERED="1",
        )
        for key, value in overrides.items():
            if value is None:  # leave the variable unset, so the app's default applies
                environ.pop(key, None)
            else:
                environ[key] = str(value)
        return environ

    def start_app(self, wait: bool = True, **overrides) -> App:
        port = free_port()
        log_path = self.tmp / f"app-{len(self.apps) + 1}.log"
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=self.app_environ(**overrides),
            stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
        )
        app = App(proc, port, log_path)
        self.apps.append(app)
        if wait:
            app.wait_ready()
        return app

    def run_python(self, code: str, **overrides) -> str:
        """Run a snippet with the app's settings, from the project root; returns its stdout."""
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=self.app_environ(**overrides),
            capture_output=True, text=True, timeout=180,
        )
        assert done.returncode == 0, done.stderr[-3000:]
        return done.stdout

    def db(self, fn):
        return db_run(self.database_url, fn)

    def close(self) -> None:
        for app in self.apps:
            app.stop()
        if self._standin and self._standin.poll() is None:
            self._standin.terminate()
            try:
                self._standin.wait(10)
            except subprocess.TimeoutExpired:
                self._standin.kill()
        self._http.close()


def _reset_external_database(url: str) -> None:
    def reset(conn):
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    db_run(url, reset)


@pytest.fixture
def env(tmp_path, request):
    if EXTERNAL_DATABASE_URL:
        _reset_external_database(EXTERNAL_DATABASE_URL)
    world = Env(tmp_path)
    world.start_standin()
    try:
        yield world
    finally:
        world.close()
        failed = getattr(request.node, "rep_call", None)
        if failed is not None and failed.failed:
            for app in world.apps:
                print(f"\n----- {app.log_path.name} (tail) -----\n{app.log_tail(80)}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def feed(*items):
    """feed(("2401.00001", 1, "new"), ...) -> the list form the scenario file wants."""
    return [list(item) for item in items]
