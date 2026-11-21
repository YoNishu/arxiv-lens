"""Test harness: homes, the stand-in, the server and the played small corpus, all offline.

Commands run as Python processes of their own (or several in one, through tests/probes/batch.py)
on a home under tmp_path, with an environment built here and nowhere else; the stand-in is served
from a thread of the test process, the web server is uvicorn as a sub-process.
ARXIVLENS_TEST_DATABASE_URL (an empty PostgreSQL database) runs the ingestion cases there instead,
emptied before every case; cases that read or copy a home's SQLite file are skipped then.
"""
import gzip
import importlib.util
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMALL = ROOT / "dev" / "corpus_small"
BIG = ROOT / "dev" / "corpus"
EXTERNAL_DATABASE_URL = os.environ.get("ARXIVLENS_TEST_DATABASE_URL", "")

# The test files whose homes go to PostgreSQL when ARXIVLENS_TEST_DATABASE_URL is set.
POSTGRES_MODULES = ("test_ingestion", "test_processing", "test_cycles", "test_local_run")

# Settings of the caller that must never reach a command of a test.
_CLEARED = ("DATABASE_URL", "PYTHONHASHSEED", "SCHEDULER_ENABLED", "POLL_INTERVAL_HOURS",
            "PROCESSING_LEASE_SECONDS", "CATEGORIES_FILE")
_TIMEOUT = 900
_started: list[subprocess.Popen] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(condition, timeout: float = 60, interval: float = 0.1, what: str = "condition"):
    """Ask condition() again and again until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"gave up after {timeout}s waiting for {what}")
        time.sleep(interval)


def feed(*items) -> list[list]:
    """feed(("2401.90001", 1, "new"), ...) -> the list form a scenario file wants."""
    return [list(item) for item in items]


def totals(line: str) -> dict[str, int]:
    """The counters of a `run <id>: new=.. updated=..` line, with the run id under "run"."""
    head, _, rest = line.partition(": ")
    values = {"run": int(head.split()[-1])}
    for part in rest.split():
        name, _, number = part.partition("=")
        values[name] = int(number)
    return values


class Result:
    """What one command did."""

    def __init__(self, status: int, stdout: str, stderr: str, command=None):
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.lines = stdout.splitlines()
        self.command = command

    def __repr__(self) -> str:
        return (f"Result(command={self.command!r}, status={self.status},\n"
                f"stdout={self.stdout[-3000:]!r},\nstderr={self.stderr[-3000:]!r})")


def _fail_unless_done(result: Result) -> Result:
    if result.status != 0:
        pytest.fail(f"command ended with status {result.status}: {result!r}", pytrace=False)
    return result


class Home:
    """A home folder and the way commands are run on it."""

    def __init__(self, path, standin_url: str | None = None, database_url: str = ""):
        self.path = Path(path)
        self.standin_url = standin_url
        self.database_url = database_url

    @property
    def insights(self) -> Path:
        return self.path / "insights"

    @property
    def seeds_file(self) -> Path:
        # Beside the home, not in it: the home holds only what the application writes.
        return self.path.parent / f"{self.path.name}.categories.yaml"

    def seeds(self, *codes: str) -> "Home":
        """Write the categories file the home's cycles seed from (none at all by default)."""
        lines = ["categories:"] if codes else ["categories: []"]
        for code in codes:
            lines += [f'  - name: "Category {code}"', f'    arxiv_code: "{code}"']
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seeds_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self

    def join(self, standin) -> "Home":
        """Point the four outside services at a stand-in."""
        self.standin_url = standin.url
        return self

    def environment(self, extra=None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ARXIVLENS_", "AZURE_", "ARXIV_")) and key not in _CLEARED}
        env.update(
            ARXIVLENS_HOME=str(self.path),
            PYTHONPATH=str(ROOT),
            SCHEDULER_ENABLED="false",
            ARXIV_API_DELAY_SECONDS="0",
            AZURE_OPENAI_API_KEY="x",
            AZURE_OPENAI_ENDPOINT=self.standin_url or "",
            CATEGORIES_FILE=str(self.seeds_file),
        )
        if self.standin_url:
            env.update(
                ARXIV_RSS_BASE=f"{self.standin_url}/rss",
                ARXIV_API_URL=f"{self.standin_url}/api/query",
                ARXIV_PDF_BASE=f"{self.standin_url}/pdf",
            )
        if self.database_url:
            env["DATABASE_URL"] = self.database_url
        for key, value in (extra or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def _exec(self, argv: list[str], env=None) -> Result:
        try:
            done = subprocess.run(argv, cwd=ROOT, env=self.environment(env), capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            pytest.fail(f"{argv} did not end within {_TIMEOUT}s: {e.stdout!r} {e.stderr!r}", pytrace=False)
        return Result(done.returncode, done.stdout, done.stderr, argv[1:])

    def run(self, *args, env=None, check: bool = True) -> Result:
        """python -m app ARGS, in a process of its own."""
        result = self._exec([sys.executable, "-m", "app", *map(str, args)], env)
        return _fail_unless_done(result) if check else result

    def batch(self, *commands, env=None) -> list[Result]:
        """Several command lines one after the other in one process (tests/probes/batch.py)."""
        argv = [[str(part) for part in command] for command in commands]
        result = self._exec([sys.executable, str(ROOT / "tests" / "probes" / "batch.py"), json.dumps(argv)], env)
        _fail_unless_done(result)
        answers = json.loads(result.lines[-1])
        return [Result(a["status"], a["stdout"], a["stderr"], command)
                for a, command in zip(answers, argv)]

    def start(self, *args, env=None) -> subprocess.Popen:
        """The same command as run(), not waited for; ended at the end of the test at the latest."""
        proc = subprocess.Popen([sys.executable, "-m", "app", *map(str, args)], cwd=ROOT,
                                env=self.environment(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace")
        _started.append(proc)
        return proc

    def tool(self, name: str, *args, check: bool = True) -> Result:
        result = self._exec([sys.executable, str(ROOT / "tools" / f"{name}.py"), *map(str, args)])
        return _fail_unless_done(result) if check else result

    def probe(self, name: str, *args, env=None, check: bool = True) -> Result:
        result = self._exec([sys.executable, str(ROOT / "tests" / "probes" / f"{name}.py"), *map(str, args)], env)
        return _fail_unless_done(result) if check else result

    def sql(self, query: str, params=()) -> list[tuple]:
        """Rows of a query on lens.db, opened read-only; for assertions after the commands ran."""
        if self.database_url:
            pytest.skip("reads the SQLite file of the home")
        uri = "file:" + urllib.parse.quote(str(self.path / "lens.db")) + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            return [tuple(row) for row in connection.execute(query, params).fetchall()]
        finally:
            connection.close()

    def copy(self, to) -> "Home":
        """The whole folder, as cp -a copies it, with the categories file that goes with it."""
        if self.database_url:
            pytest.skip("copies the SQLite file of the home")
        twin = Home(to, self.standin_url)
        shutil.copytree(self.path, twin.path, symlinks=True)
        if self.seeds_file.exists():
            shutil.copy2(self.seeds_file, twin.seeds_file)
        return twin


def new_home(path) -> Home:
    return Home(path)


_standin_module = None


def standin_module():
    """dev/standin.py loaded by its path, as the application loads it."""
    global _standin_module
    if _standin_module is None:
        found = importlib.util.spec_from_file_location("lens_standin", ROOT / "dev" / "standin.py")
        module = importlib.util.module_from_spec(found)
        found.loader.exec_module(module)
        _standin_module = module
    return _standin_module


def _write_json(path: Path, value) -> None:
    # Whole files only: the stand-in reads them again at every request.
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(value), encoding="utf-8")
    os.replace(scratch, path)


_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Standin:
    """dev/standin.py served from a thread of the test process."""

    def __init__(self, folder, corpus=None, scenario=None, home=None, control=True):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.scenario_file = folder / "scenario.json" if scenario is not None else None
        self.control_file = folder / "control.json" if control else None
        if self.scenario_file is not None:
            self.scenario(**(scenario if isinstance(scenario, dict) else {}))
        if self.control_file is not None:
            self.control()
        home_path = home.path if isinstance(home, Home) else home
        self._server = standin_module().make_server(
            0,
            corpus=str(corpus) if corpus is not None else None,
            scenario=str(self.scenario_file) if self.scenario_file is not None else None,
            home=str(home_path) if home_path is not None else None,
            control=str(self.control_file) if self.control_file is not None else None,
        )
        # A request still sleeping in a slow answer must not hold up the end of a test.
        self._server.block_on_close = False
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True)
        self._thread.start()

    def counts(self) -> dict[str, int]:
        with _no_proxy.open(f"{self.url}/_n", timeout=30) as answer:
            return json.loads(answer.read())

    def count(self, prefix: str) -> int:
        return sum(value for key, value in self.counts().items() if key.startswith(prefix))  # ints

    def scenario(self, **keys) -> None:
        """Rewrite what the scenario world answers from now on."""
        keys.setdefault("feeds", {})
        _write_json(self.scenario_file, keys)

    def control(self, **keys) -> None:
        """Rewrite the control file (faults, slowness) the stand-in reads at every request."""
        _write_json(self.control_file, keys)

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        self._thread.join(10)


def new_standin(folder, corpus=None, scenario=None, home=None, control=True) -> Standin:
    return Standin(folder, corpus=corpus, scenario=scenario, home=home, control=control)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_http = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class Server:
    """uvicorn app.main:app on the home, as a sub-process."""

    def __init__(self, home: Home, **env):
        self.home = home
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.headers = {}
        home.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = home.path.parent / f"{home.path.name}.server-{self.port}.log"
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=home.environment(env), stdout=self._log, stderr=subprocess.STDOUT,
        )
        try:
            self._wait_ready()
        except BaseException:
            self.kill()
            raise

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"the server ended with {self.proc.returncode}:\n{self.log()}")
            try:
                if self.get("/status")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"the server did not answer GET /status within 60s:\n{self.log()}")

    def log(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def request(self, method: str, path: str, json_body=None, form=None) -> tuple[int, str]:
        data, headers = None, {}
        if json_body is not None:
            data, headers = json.dumps(json_body).encode("utf-8"), {"Content-Type": "application/json"}
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        elif method in ("POST", "PUT"):
            data = b""
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with _http.open(req, timeout=120) as answer:
                self.headers = dict(answer.headers)
                return answer.status, answer.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            self.headers = dict(e.headers or {})
            return e.code, e.read().decode("utf-8", errors="replace")

    def get(self, path: str) -> tuple[int, str]:
        return self.request("GET", path)

    def post(self, path: str, json=None, form=None) -> tuple[int, str]:
        return self.request("POST", path, json_body=json, form=form)

    def stop(self) -> None:
        """An ordinary shutdown: SIGTERM, then wait for the exit."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(30)
        self._log.close()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(30)
        self._log.close()


def new_server(home: Home, **env) -> Server:
    return Server(home, **env)


def run_all(jobs: list, at_once: int = 4) -> list:
    """Call each job (each runs one sub-process) with at most at_once of them at the same time."""
    with ThreadPoolExecutor(max_workers=at_once) as pool:
        return list(pool.map(lambda job: job(), jobs))


def use_test_database(home: Home) -> Home:
    """On a PostgreSQL run, give the home the external database, emptied first."""
    if EXTERNAL_DATABASE_URL:
        home.database_url = EXTERNAL_DATABASE_URL
        _reset_external_database(EXTERNAL_DATABASE_URL)
    return home


def _reset_external_database(url: str) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def reset():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(reset())


def _on_postgres(request) -> bool:
    return bool(EXTERNAL_DATABASE_URL) and request.module.__name__.rsplit(".", 1)[-1] in POSTGRES_MODULES


@pytest.fixture(autouse=True)
def _end_started_processes():
    before = len(_started)
    yield
    for proc in _started[before:]:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=30)
    del _started[before:]


@pytest.fixture
def home(tmp_path, request) -> Home:
    made = new_home(tmp_path / "home")
    return use_test_database(made) if _on_postgres(request) else made


@pytest.fixture
def make_home(tmp_path, request):
    reset = [_on_postgres(request)]

    def make(name: str = "h2") -> Home:
        made = new_home(tmp_path / name)
        if reset[0]:
            use_test_database(made)
            reset[0] = False
        elif _on_postgres(request):
            made.database_url = EXTERNAL_DATABASE_URL
        return made

    return make


@pytest.fixture
def standin(tmp_path):
    made: list[Standin] = []

    def start(corpus=None, scenario=None, home=None, control=True) -> Standin:
        one = new_standin(tmp_path / f"standin-{len(made) + 1}", corpus=corpus, scenario=scenario,
                          home=home, control=control)
        made.append(one)
        return one

    yield start
    for one in made:
        one.close()


@pytest.fixture
def server():
    made: list[Server] = []

    def start(home: Home, **env) -> Server:
        one = new_server(home, **env)
        made.append(one)
        return one

    yield start
    for one in made:
        one.stop()


# -- the played small corpus -------------------------------------------------------------------

def corpus_days(manifest: dict) -> dict[str, str]:
    """The named days of a corpus: B1.. before the first month turn, A0.. from it, and END."""
    start = date.fromisoformat(manifest["start"])
    end = date.fromisoformat(manifest["end"])
    turn = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    days = {"END": end.isoformat()}
    day, number = turn - timedelta(days=1), 1
    while day >= start:
        if day.weekday() < 5:
            days[f"B{number}"] = day.isoformat()
            number += 1
        day -= timedelta(days=1)
    day, number = turn, 0
    while day <= end:
        if day.weekday() < 5:
            days[f"A{number}"] = day.isoformat()
            number += 1
        day += timedelta(days=1)
    return days


def corpus_papers(folder: Path) -> dict[str, dict]:
    with gzip.open(folder / "papers.jsonl.gz", "rb") as f:
        papers = [json.loads(line) for line in f if line.strip()]
    return {paper["id"]: paper for paper in papers}


class Played:
    """The small corpus played forward once, with a copy of the home after six of its days."""

    STEPS = ("B1", "A1", "A2", "A3", "A5", "END")

    def __init__(self, planted: dict, days: dict[str, str], home_at: dict[str, Home], versions: dict[str, dict]):
        self.planted = planted
        self.days = days
        self.home_at = home_at
        self.versions = versions


class Small:
    """Played, with one insights build in each of its six homes."""

    def __init__(self, played: Played, build: dict[str, Path]):
        self.build = build
        self.home_at = played.home_at
        self.planted = played.planted
        self.days = played.days
        self.versions = played.versions


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


@pytest.fixture(scope="session")
def labels(tmp_path_factory):
    one = new_standin(tmp_path_factory.mktemp("labels"))
    yield one
    one.close()


@pytest.fixture(scope="session")
def played(tmp_path_factory) -> Played:
    folder = tmp_path_factory.mktemp("played")
    manifest = json.loads((SMALL / "corpus.json").read_text(encoding="utf-8"))
    days = corpus_days(manifest)
    playing = new_home(folder / "playing")
    home_at, last = {}, None
    for name in Played.STEPS:
        args = ["play", "--corpus", SMALL, "--fast", "--until", days[name]]
        if last is not None:
            args += ["--from", _next_day(last)]
        playing.run(*args)
        home_at[name] = playing.copy(folder / name)
        last = days[name]
    return Played(manifest["planted"], days, home_at, corpus_papers(SMALL))


@pytest.fixture(scope="session")
def small(played, labels) -> Small:
    endpoint = {"AZURE_OPENAI_ENDPOINT": labels.url}
    homes = [played.home_at[name] for name in Played.STEPS]
    run_all([lambda h=h: h.run("insights", "build", env=endpoint) for h in homes], at_once=4)
    return Small(played, {name: played.home_at[name].insights for name in Played.STEPS})


@pytest.fixture(scope="session")
def tiny(tmp_path_factory, labels) -> Home:
    manifest = json.loads((SMALL / "corpus.json").read_text(encoding="utf-8"))
    start = date.fromisoformat(manifest["start"])
    friday = start + timedelta(days=(4 - start.weekday()) % 7)
    made = new_home(tmp_path_factory.mktemp("tiny") / "home")
    made.run("play", "--corpus", SMALL, "--fast", "--until", friday.isoformat())
    made.run("insights", "build", env={"AZURE_OPENAI_ENDPOINT": labels.url})
    return made
