"""Rules read off the source text: who reads the time, how floats are added, what light commands import."""
import json
import re
from functools import partial

from conftest import ROOT, run_all

SYSTEM_TIME = re.compile(r"\b(datetime\.now|datetime\.utcnow|date\.today|time\.time|time\.monotonic)\b")
UNSTABLE_MATH = "log|log1p|log2|log10|exp|expm1|pow|sin|cos|tan|asin|acos|atan|atan2|sinh|cosh|tanh|asinh|acosh|atanh|hypot"
BANNED = [
    ("a logarithm, power or angle from libm", re.compile(rf"\bmath\.({UNSTABLE_MATH})\b")),
    ("a libm name imported from math", re.compile(rf"^\s*from\s+math\s+import\s+.*\b({UNSTABLE_MATH})\b")),
    ("the built-in pow", re.compile(r"(?<![\w.])pow\(")),
    ("a power with a fractional exponent", re.compile(r"\*\*\s*\(?\s*-?\d*\.\d")),
    ("a source of chance", re.compile(r"^\s*(import|from)\s+(random|uuid|secrets)\b|\b(random|uuid|secrets)\.[a-z_]")),
    ("bytes from the system", re.compile(r"\bos\.urandom\b")),
    ("the system time", re.compile(r"\b(time\.time|datetime\.now|date\.today)\b")),
]
# The built-in sum; math.fsum and methods called sum are other names.
BUILTIN_SUM = re.compile(r"(?<![\w.])sum\(")
HEAVY = ["pymupdf", "openai", "azure", "fastmcp", "arxiv", "feedparser", "fastapi"]


def _lines(paths):
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            yield f"{path.relative_to(ROOT).as_posix()}:{number}", line


def test_only_the_clock_module_reads_the_system_time():
    sources = [p for p in sorted((ROOT / "app").rglob("*.py")) if p != ROOT / "app" / "clock.py"]
    assert sources
    found = [(where, line.strip()) for where, line in _lines(sources) if SYSTEM_TIME.search(line)]
    assert found == []


def test_the_pipeline_adds_floats_in_loops_and_takes_logarithms_of_whole_numbers():
    sources = sorted((ROOT / "app" / "insights").rglob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
    sources.append(ROOT / "dev" / "make_corpus.py")
    assert all(path.is_file() for path in sources) and len(sources) > 2, sources
    banned = [(where, why, line.strip()) for where, line in _lines(sources)
              for why, pattern in BANNED if pattern.search(line)]
    assert banned == []
    sums = [(where, line.strip()) for where, line in _lines(sources)
            if BUILTIN_SUM.search(line) and not line.rstrip().endswith("# ints")]
    assert sums == []


def test_light_commands_import_no_heavy_package(home):
    commands = [("clock", "show"), ("db", "info"), ("insights", "config")]
    results = run_all([partial(home.probe, "light_imports", *args) for args in commands])
    for args, result in zip(commands, results):
        answer = json.loads(result.lines[-1])
        assert answer["status"] == 0, (args, answer)
        assert [name for name in answer["heavy"] if name in HEAVY] == [], (args, answer)
