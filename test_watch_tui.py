#!/usr/bin/env python3
"""Test watch — reruns a Python project's tests whenever a .py file in it changes, and shows which
fail, with each failure's traceback a key away (unittest or pytest).

The tests run in a subprocess with the project's own interpreter (.venv/bin/python when there is
one). Results don't come from scraping the console: a small driver (unittest) or plugin (pytest),
embedded below, reports every test as a JSON line on a pipe of its own, so the tests' output is
left alone and shown as it is under Tab. A change during a run cancels it and starts again.
"""

import argparse
import atexit
import curses
import json
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque

POLL_SECONDS = 0.5             # how often the project is checked for changed files
DEBOUNCE_SECONDS = 0.3         # editors write in bursts: wait for quiet before a run
RUN_TIMEOUT = 600              # a run that takes longer is killed
MAX_FILES = 20_000             # watched files; past this the project is too big to poll
MAX_OUTPUT = 20_000            # lines of test output kept per run
HISTORY = 40                   # runs in the header strip
TICK_MS = 125
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 10

CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"}
SKIP_DIRS = {"__pycache__", "venv", "env", "node_modules", "build", "dist", "site-packages"}   # and hidden ones
VENVS = (".venv", "venv", "env")
GUI_EDITORS = {"code", "cursor", "codium", "windsurf", "subl", "zed"}   # take file:line and return at once

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# status -> (mark, colour pair, label, rank in the list: problems first)
STATUS = {"error": ("!", 4, "error", 0), "fail": ("✗", 4, "failed", 0), "xpass": ("?", 3, "unexpected pass", 1),
          "pass": ("✓", 2, "passed", 2), "skip": ("-", 5, "skipped", 3), "xfail": ("x", 5, "expected failure", 3)}
PROBLEMS = {"error", "fail", "xpass"}
# run outcome -> (text, colour pair)
OUTCOME = {"passed": ("passed", 2), "failed": ("failed", 4), "crashed": ("didn't run", 4),
           "no tests": ("no tests found", 3), "cancelled": ("cancelled", 5), "timed out": ("timed out", 4)}
# a traceback frame: unittest's `File "x.py", line 3`, or pytest's --tb=short `tests/x.py:3: in f`
FRAME = re.compile(r'File "([^"]+)", line (\d+)|^(\S+\.py):(\d+):', re.M)

# Runs in the project's interpreter: `python -c UNITTEST_DRIVER [-k pattern] [test names]`.
UNITTEST_DRIVER = r'''
import inspect, json, os, sys, time, unittest

out = os.fdopen(int(os.environ["TEST_WATCH_FD"]), "w", buffering=1)


def emit(**kw):
    out.write(json.dumps(kw) + "\n")


class Result(unittest.TextTestResult):
    cur = None

    def startTest(self, test):
        super().startTest(test)
        self.cur = {"status": "pass", "detail": "", "subs": [], "t0": time.perf_counter()}

    def stopTest(self, test):
        super().stopTest(test)
        cur, self.cur = self.cur, None
        status, detail = cur["status"], cur["detail"]
        if cur["subs"]:
            if status == "pass":
                status = "error" if any(kind == "error" for kind, _ in cur["subs"]) else "fail"
            detail = "\n".join([text for _, text in cur["subs"]] + ([detail] if detail else []))
        self.send(test, status, detail, time.perf_counter() - cur["t0"])

    def send(self, test, status, detail, secs=0.0):
        kind = type(test).__name__
        file = line = None
        if kind == "_FailedTest":          # a module that failed to import
            group, name, rerun = test._testMethodName, "(import failed)", False
        elif kind == "_ErrorHolder":       # setUpClass or setUpModule raised
            group, name, rerun = "", str(test), False
        else:
            module = type(test).__module__
            tid = test.id()
            group, name, rerun = module, tid[len(module) + 1:] if tid.startswith(module + ".") else tid, True
            fn = getattr(test, getattr(test, "_testMethodName", ""), None)
            code = getattr(inspect.unwrap(fn), "__code__", None) if fn else None
            if code:
                file, line = code.co_filename, code.co_firstlineno
        emit(event="test", id=test.id(), group=group, name=name, status=status, secs=secs,
             detail=detail, file=file, line=line, rerun=rerun)

    def note(self, test, status, detail):
        if self.cur is None:               # reported outside startTest/stopTest
            self.send(test, status, detail)
        else:
            self.cur["status"], self.cur["detail"] = status, detail

    def addSuccess(self, test):
        super().addSuccess(test)
        self.note(test, "pass", "")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.note(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.note(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if self.cur is None or not isinstance(test, unittest.case._SubTest):
            self.note(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.note(test, "xfail", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.note(test, "xpass", "passed, but is marked as an expected failure")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None and self.cur is not None:
            kind = "fail" if issubclass(err[0], test.failureException) else "error"
            self.cur["subs"].append((kind, subtest._subDescription() + "\n" + self._exc_info_to_string(err, test)))


args, patterns, names = sys.argv[1:], [], []
while args:
    arg = args.pop(0)
    if arg == "-k" and args:
        pattern = args.pop(0)
        patterns.append(pattern if "*" in pattern else f"*{pattern}*")
    else:
        names.append(arg)
sys.path.insert(0, os.getcwd())
loader = unittest.TestLoader()
if patterns:
    loader.testNamePatterns = patterns
try:
    suite = loader.loadTestsFromNames(names) if names else loader.discover(".", top_level_dir=".")
except Exception as e:
    import traceback
    emit(event="test", id="(loading)", group="", name="(couldn't load the tests)", status="error", secs=0,
         detail=traceback.format_exc(), file=None, line=None, rerun=False)
    sys.exit(1)
emit(event="collected", count=suite.countTestCases())
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=Result).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun else 5 if not result.testsRun else 1)
'''

# Loaded into pytest with `-p _test_watch_plugin` from a temporary folder on PYTHONPATH.
PYTEST_PLUGIN = r'''
import json, os

_out = os.fdopen(int(os.environ["TEST_WATCH_FD"]), "w", buffering=1)
_cur = {}
_root = [os.getcwd()]


def _emit(**kw):
    _out.write(json.dumps(kw) + "\n")


def pytest_configure(config):
    _root[0] = str(config.rootpath)


def pytest_collection_finish(session):
    _emit(event="collected", count=len(session.items))


def pytest_collectreport(report):
    if report.failed:
        path = report.nodeid.split("::")[0]
        _emit(event="test", id=report.nodeid or "(collection)", group=path, name="(collection failed)",
              status="error", secs=0, detail=report.longreprtext,
              file=os.path.join(_root[0], path) if path else None, line=None, rerun=False)


def pytest_runtest_logreport(report):
    c = _cur.setdefault(report.nodeid, {"status": "pass", "detail": [], "secs": 0.0, "loc": report.location})
    c["secs"] += report.duration
    xfail = hasattr(report, "wasxfail")
    if report.when == "call":
        if report.passed:
            status = "xpass" if xfail else "pass"
        elif report.failed:
            status = "fail"
        else:
            status = "xfail" if xfail else "skip"
    elif report.failed:
        status = "error"                   # in a fixture's setup or teardown
    elif report.skipped:
        status = "xfail" if xfail else "skip"
    else:
        status = c["status"]
    # a failed subtest arrives as a report of its own before the test's passing one
    if c["status"] not in ("fail", "error") or status == "error":
        c["status"] = status
    if xfail and report.when == "call" and not report.failed:
        c["detail"].append(report.wasxfail or "marked xfail")
    if report.failed or (report.skipped and not xfail):
        if isinstance(report.longrepr, tuple):   # a skip: (file, line, reason)
            c["detail"].append(str(report.longrepr[2]))
        else:
            c["detail"].append(report.longreprtext)


def pytest_runtest_logfinish(nodeid, location):
    c = _cur.pop(nodeid, None)
    if c is None:
        return
    path, lineno, _ = c["loc"]
    group, _, name = nodeid.partition("::")
    _emit(event="test", id=nodeid, group=group, name=name or nodeid, status=c["status"], secs=c["secs"],
          detail="\n".join(c["detail"]), file=os.path.join(_root[0], path),
          line=None if lineno is None else lineno + 1, rerun=True)
'''

_plugin_dir = []


def plugin_dir():
    """A temporary folder holding the pytest plugin, removed at exit."""
    if not _plugin_dir:
        d = tempfile.mkdtemp(prefix="test_watch_")
        with open(os.path.join(d, "_test_watch_plugin.py"), "w") as f:
            f.write(PYTEST_PLUGIN)
        atexit.register(shutil.rmtree, d, True)
        _plugin_dir.append(d)
    return _plugin_dir[0]


def find_python(root):
    for venv in VENVS:
        path = os.path.join(root, venv, "bin", "python")
        if os.access(path, os.X_OK):
            return path
    return sys.executable


def has_pytest(python, root):
    try:
        return subprocess.run([python, "-c", "import pytest"], cwd=root, capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scan(root):
    """{path: (mtime, size)} of the .py and config files below root, skipping caches, venvs and
    hidden folders."""
    files = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith(".") and e.name not in SKIP_DIRS:
                                stack.append(e.path)
                        elif e.name.endswith(".py") or e.name in CONFIG_FILES:
                            st = e.stat(follow_symlinks=False)
                            files[e.path] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
        if len(files) > MAX_FILES:
            break
    return files


class Run:
    """One run of the tests in a subprocess, filled in by reader threads as results arrive."""

    def __init__(self, cmd, cwd, env, trigger, ids=None):
        self.cmd, self.cwd, self.env, self.trigger, self.ids = cmd, cwd, env, trigger, ids
        self.tests = []
        self.output = deque(maxlen=MAX_OUTPUT)
        self.total = None                  # from the collected event
        self.state = "running"             # or done, cancelled, timed out
        self.code = None
        self.error = None
        self.when = time.time()
        self.started = time.monotonic()
        self.secs = 0.0
        self.proc = None
        self.finished = False              # the process is gone and its output read

    def start(self):
        rfd, wfd = os.pipe()
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, env=dict(self.env, TEST_WATCH_FD=str(wfd)), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, pass_fds=(wfd,), start_new_session=True)
        except OSError as e:
            os.close(rfd)
            self.error, self.state, self.finished = f"couldn't start {self.cmd[0]}: {e.strerror or e}", "done", True
            return
        finally:
            os.close(wfd)
        readers = [threading.Thread(target=self.read_events, args=(rfd,), daemon=True),
                   threading.Thread(target=self.read_output, daemon=True)]
        for t in readers:
            t.start()
        threading.Thread(target=self.wait, args=(readers,), daemon=True).start()

    def read_events(self, fd):
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "collected":
                    self.total = ev["count"]
                elif ev.get("event") == "test":
                    self.tests.append(ev)

    def read_output(self):
        for raw in self.proc.stdout:
            self.output.append(raw.decode("utf-8", "replace").rstrip("\n").expandtabs())

    def wait(self, readers):
        self.code = self.proc.wait()
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)   # whatever the tests started and left running
        except OSError:
            pass
        for t in readers:
            t.join(2)                      # a grandchild may still hold the pipes open
        if self.state == "running":
            self.secs = time.monotonic() - self.started
            self.state = "done"
        self.finished = True

    def kill(self, why):
        if self.state != "running":
            return
        self.state = why
        self.secs = time.monotonic() - self.started
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            return
        timer = threading.Timer(2, self.force)
        timer.daemon = True                # quitting mid-run mustn't wait for it
        timer.start()

    def force(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass

    @property
    def running(self):
        return self.state == "running"

    def elapsed(self):
        return time.monotonic() - self.started if self.running else self.secs

    def counts(self):
        counts = dict.fromkeys(STATUS, 0)
        for t in list(self.tests):
            counts[t["status"]] += 1
        return counts

    def outcome(self):
        if self.state in ("cancelled", "timed out"):
            return self.state
        if self.running:
            return None
        counts = self.counts()
        if any(counts[s] for s in PROBLEMS):
            return "failed"
        if self.error or self.code not in (0, 5):
            return "crashed"
        if not self.tests:
            return "no tests"
        return "passed"


class Session:
    """Watches the project and starts a run after a change; the UI only reads it and asks."""

    def __init__(self, root, python, runner, extra):
        self.root, self.python, self.runner, self.extra = root, python, runner, extra
        self.lock = threading.Lock()
        self.run = None                    # the newest run, running or not
        self.last = None                   # the newest finished run
        self.history = deque(maxlen=HISTORY)
        self.files = {}
        self.paused = False
        self.pending = None                # (trigger, ids) of the run to start
        self.changed_at = 0.0
        self.wake = threading.Event()
        self.listeners = []                # called with each finished run

    def command(self, ids=None):
        args = ids if ids is not None else self.extra
        if self.runner == "pytest":
            return [self.python, "-m", "pytest", "-p", "_test_watch_plugin", "-v", "--tb=short", "--color=no", *args]
        return [self.python, "-c", UNITTEST_DRIVER, *args]

    def environment(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        if self.runner == "pytest":
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [plugin_dir(), env.get("PYTHONPATH")]))
        return env

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def ask(self, trigger, ids=None):
        with self.lock:
            self.pending, self.changed_at = (trigger, ids), 0.0
        self.wake.set()

    def loop(self):
        self.files = scan(self.root)
        self.ask("first run")
        while True:
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
            new = scan(self.root)
            changed = sorted(p for p in new.keys() | self.files.keys() if new.get(p) != self.files.get(p))
            self.files = new
            with self.lock:
                if changed:
                    name = os.path.relpath(changed[0], self.root)
                    more = f" and {len(changed) - 1} more" if len(changed) > 1 else ""
                    self.pending, self.changed_at = (f"{name}{more} changed", None), time.monotonic()
                start = (self.pending and not self.paused
                         and time.monotonic() - self.changed_at >= DEBOUNCE_SECONDS)
                pending = self.pending if start else None
                if start:
                    self.pending = None
            if pending:
                self.launch(*pending)
            self.check()

    def launch(self, trigger, ids=None):
        if self.run and self.run.running:
            self.run.kill("cancelled")
            self.finish(self.run)
        run = Run(self.command(ids), self.root, self.environment(), trigger, ids)
        run.start()
        self.run = run

    def check(self):
        run = self.run
        if run is None or run in self.history:
            return
        if run.running and run.elapsed() > RUN_TIMEOUT:
            run.kill("timed out")
        if run.finished:
            self.finish(run)

    def finish(self, run):
        with self.lock:
            if run in self.history:
                return
            self.history.append(run)
            if run.state == "done":
                self.last = run
        for listener in self.listeners:
            listener(run)

    def failed_ids(self):
        """The ids to rerun for `f`, or None when a failure can't be rerun on its own."""
        run = self.shown()
        if run is None:
            return None
        bad = [t for t in run.tests if t["status"] in PROBLEMS]
        if not bad or not all(t["rerun"] for t in bad):
            return None
        return [t["id"] for t in bad]

    def shown(self):
        """The run whose tests to list: the newest, or the last finished one until it has results."""
        run = self.run
        if run is not None and run.running and not run.tests and self.last is not None:
            return self.last
        return run


def fmt_secs(s):
    if s < 1:
        return f"{s * 1000:.0f} ms"
    if s < 60:
        return f"{s:.2f} s"
    return f"{int(s) // 60}m {int(s) % 60:02d}s"


def tally(n, status):
    label = STATUS[status][2]
    if n != 1 and status in ("error", "xpass", "xfail"):
        label += "es" if label.endswith("s") else "s"
    return f"{n} {label}"


def cwidth(text):
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = cwidth(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def wrap(lines, width):
    """Hard-wraps long lines, continuing under a two-space indent: tracebacks don't reflow well."""
    out = []
    for line in lines:
        line = line.expandtabs()
        while cwidth(line) > width:
            cut, used = 0, 0
            for i, ch in enumerate(line):
                used += cwidth(ch)
                if used > width:
                    break
                cut = i + 1
            out.append(line[:cut])
            line = "  " + line[cut:]
        out.append(line)
    return out


def headline(detail):
    """The line that says what went wrong: the last one of a traceback, or a skip's reason."""
    for line in reversed(detail.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def location(test, root):
    """Where to open the editor: the deepest traceback frame in the project, else the test itself."""
    for m in reversed(list(FRAME.finditer(test.get("detail") or ""))):
        path, line = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        path = os.path.realpath(os.path.join(root, path))
        rel = os.path.relpath(path, root)
        if not rel.startswith("..") and not any(part in SKIP_DIRS or part in VENVS for part in rel.split(os.sep)):
            return path, int(line)
    if test.get("file"):
        return test["file"], test.get("line") or 1
    return None


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


class App:
    def __init__(self, stdscr, session):
        self.stdscr, self.s = stdscr, session
        self.view = "tests"                # tests, detail, output
        self.only_problems = False
        self.sel_id = None                 # the selected test's id, kept across runs
        self.top = 0
        self.scroll = 0
        self.follow = True                 # the output view sticks to the end while running
        self.bell = False
        self.notify = False
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.last_outcome = None
        session.listeners.append(self.finished)

    def finished(self, run):
        outcome = run.outcome()
        if outcome not in ("passed", "failed", "crashed"):
            return
        if self.bell and outcome != "passed":
            curses.beep()
        if self.notify and outcome != self.last_outcome and self.last_outcome is not None:
            counts = run.counts()
            text = ("all tests pass again" if outcome == "passed"
                    else f"{counts['fail'] + counts['error']} failing" if outcome == "failed" else "the tests didn't run")
            script = f'display notification "{text}" with title "Test watch" subtitle "{os.path.basename(self.s.root)}"'
            try:
                subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        self.last_outcome = outcome

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    def rows(self):
        run = self.s.shown()
        if run is None:
            return []
        tests = [t for t in list(run.tests) if not self.only_problems or t["status"] in PROBLEMS]
        return sorted(tests, key=lambda t: STATUS[t["status"]][3])   # stable: run order within a rank

    def index(self, rows):
        for i, t in enumerate(rows):
            if t["id"] == self.sel_id:
                return i
        return 0

    def selected(self):
        rows = self.rows()
        return rows[self.index(rows)] if rows else None

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.s.check()
        self.draw_header(w)
        if self.view == "detail":
            self.draw_detail(4, h - 5, w)
        elif self.view == "output":
            self.draw_output(4, h - 5, w)
        else:
            self.draw_tests(4, h - 5, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        sess, run = self.s, self.s.run
        self.put(0, 0, " Test watch ", curses.color_pair(1) | curses.A_BOLD)
        if run is None:
            state, pair = "starting", 3
        elif run.running:
            spin = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
            done = len(run.tests)
            state, pair = f"{spin} running {done}/{run.total}" if run.total else f"{spin} running", 3
        else:
            outcome = run.outcome()
            state, pair = OUTCOME[outcome]
            counts = run.counts()
            bad = counts["fail"] + counts["error"]
            if outcome == "failed" and bad:
                state = f"✗ {bad} failed"
            elif outcome == "passed":
                state = f"● {counts['pass']} passed"
        if sess.paused:
            state, pair = "‖ paused", 3
        self.put(0, w - cwidth(state) - 1, state, curses.color_pair(pair) | curses.A_BOLD)
        label = short_path(sess.root)
        self.put(0, 13, label, curses.A_BOLD, w - 15 - cwidth(state))

        shown = sess.shown()
        if shown is not None:
            counts = shown.counts()
            x = 1
            for status, (mark, p, _, _) in STATUS.items():
                if counts[status] or status in ("pass", "fail"):
                    text = f"{mark} {counts[status]}"
                    self.put(1, x, text, curses.color_pair(p) | (curses.A_BOLD if counts[status] else 0), len(text))
                    x += len(text) + 2
            when = time.strftime("%H:%M:%S", time.localtime(shown.when))
            took = "" if shown.running else f"{fmt_secs(shown.secs)} · "
            self.put(1, x + 1, f"{took}{when} · {shown.trigger}", curses.color_pair(5))

        runs = list(sess.history)[-(w // 3):]
        self.put(2, 1, "runs ", curses.color_pair(5))
        x = 6
        for r in runs:
            pair = OUTCOME[r.outcome()][1]
            self.put(2, x, "●" if r.outcome() in ("passed", "failed") else "○", curses.color_pair(pair), 1)
            x += 1
        if len(sess.files) > MAX_FILES:
            self.put(2, x + 2, f"over {MAX_FILES:,} files — only some are watched", curses.color_pair(4))
        else:
            info = f"{sess.runner} · {short_path(sess.python)} · {len(sess.files):,} files watched"
            self.put(2, x + 2, info, curses.color_pair(5))

        if run is not None and run.running and run.total:
            width = w - 2
            filled = min(width, round(width * len(run.tests) / run.total))
            self.put(3, 1, "━" * filled, curses.color_pair(1), filled)
            self.put(3, 1 + filled, "─" * (width - filled), curses.color_pair(5), width - filled)

    def draw_tests(self, y0, height, w):
        rows = self.rows()
        self.shown_range = ""
        shown = self.s.shown()
        if not rows:
            if shown is None or shown.running:
                return
            outcome = shown.outcome()
            if outcome == "crashed":
                self.put(y0, 1, shown.error or f"the test runner exited with code {shown.code} — Tab shows its output",
                         curses.color_pair(4))
                tail = [line for line in shown.output if line.strip()][-(height - 2):]
                for i, line in enumerate(tail):
                    self.put(y0 + 2 + i, 2, line, curses.color_pair(5))
            elif outcome == "passed" and self.only_problems:
                self.put(y0 + 1, 2, "nothing failing — a shows every test", curses.color_pair(2))
            elif outcome == "no tests":
                what = ("no test_*.py files found" if self.s.runner == "unittest" else "pytest collected nothing")
                self.put(y0 + 1, 2, f"{what} — waiting for a change", curses.color_pair(3))
            return
        index = self.index(rows)
        if index < self.top:
            self.top = index
        elif index >= self.top + height:
            self.top = index - height + 1
        self.top = max(0, min(self.top, len(rows) - height))
        secs_w = 8
        for i, t in enumerate(rows[self.top:self.top + height]):
            y = y0 + i
            mark, pair, _, _ = STATUS[t["status"]]
            chosen = self.top + i == index
            base = curses.A_REVERSE if chosen else 0
            bad = t["status"] in PROBLEMS
            self.put(y, 0, " " * w, base)
            self.put(y, 1, mark, base | curses.color_pair(pair) | curses.A_BOLD, 1)
            room = w - 3 - secs_w - 1
            name = t["name"]
            self.put(y, 3, name, base | (curses.A_BOLD if bad else 0), room)
            x = 3 + min(cwidth(name), room) + 2
            if t["group"] and x < 3 + room:
                self.put(y, x, t["group"], base | curses.color_pair(5), 3 + room - x)
                x += min(cwidth(t["group"]), 3 + room - x) + 2
            if (bad or t["status"] == "skip") and x < 3 + room:
                self.put(y, x, headline(t["detail"]), base | curses.color_pair(pair), 3 + room - x)
            if t["secs"] >= 0.01:
                self.put(y, w - secs_w - 1, fmt_secs(t["secs"]).rjust(secs_w), base | curses.color_pair(5), secs_w)
        if len(rows) > height:
            self.shown_range = f"{self.top + 1}–{min(len(rows), self.top + height)} of {len(rows):,}"

    def detail_lines(self, t, w):
        mark, pair, label, _ = STATUS[t["status"]]
        where = location(t, self.s.root)
        lines = [(f"{mark} {t['name']}", curses.color_pair(pair) | curses.A_BOLD)]
        sub = "  ".join(filter(None, [t["group"], label,
                                      fmt_secs(t["secs"]) if t["secs"] >= 0.01 else ""]))
        lines.append((sub, curses.color_pair(5)))
        if where:
            lines.append((f"{short_path(where[0])}:{where[1]}  (e opens it)", curses.color_pair(5)))
        lines.append(("", 0))
        detail = t["detail"] or ("passed" if t["status"] == "pass" else "")
        last = headline(detail)
        for line in wrap(detail.splitlines(), w - 4):
            strong = line.strip() == last and bool(last)
            lines.append((line, curses.color_pair(pair) | curses.A_BOLD if strong else 0))
        return lines

    def draw_detail(self, y0, height, w):
        t = self.selected()
        self.shown_range = ""
        if t is None:
            self.view = "tests"
            return
        lines = self.detail_lines(t, w)
        self.scroll = max(0, min(self.scroll, len(lines) - height))
        for i, (text, attr) in enumerate(lines[self.scroll:self.scroll + height]):
            self.put(y0 + i, 2, text, attr, w - 3)
        if len(lines) > height:
            self.shown_range = f"{self.scroll + 1}–{min(len(lines), self.scroll + height)} of {len(lines)}"

    def draw_output(self, y0, height, w):
        run = self.s.run
        self.shown_range = ""
        if run is None:
            return
        lines = wrap(list(run.output), w - 3)
        if not lines:
            self.put(y0 + 1, 2, "no output yet", curses.color_pair(5))
            return
        last = max(0, len(lines) - height)
        if self.follow:
            self.scroll = last
        self.scroll = max(0, min(self.scroll, last))
        for i, line in enumerate(lines[self.scroll:self.scroll + height]):
            attr = 0
            if re.match(r"^(FAIL|ERROR)\b|.* (FAIL|ERROR|FAILED)( |$)", line):
                attr = curses.color_pair(4)
            elif line.endswith((" ok", " PASSED")) or " PASSED " in line:
                attr = curses.color_pair(2)
            self.put(y0 + i, 1, line, attr, w - 2)
        if len(lines) > height:
            self.shown_range = f"{self.scroll + 1}–{min(len(lines), self.scroll + height)} of {len(lines):,}"

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        if self.view == "tests":
            keys = ["↑↓ move", "⏎ traceback", "r run", "f failed", "Tab output", "e edit",
                    f"a {'problems' if self.only_problems else 'all'}", "␣ pause",
                    f"b bell{' on' if self.bell else ''}", f"n notify{' on' if self.notify else ''}"]
        elif self.view == "detail":
            keys = ["Esc back", "↑↓ scroll", "n/p next/prev", "e edit", "r run", "Tab output"]
        else:
            keys = ["Esc back", "↑↓ scroll", "G end", "r run"]
        shown = getattr(self, "shown_range", "")
        room = w - 2 - (len(shown) + 2 if shown else 0)
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > room:
            keys.pop()                     # the least needed go first in a narrow window
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))
        if shown:
            self.put(y, w - len(shown) - 1, shown, curses.color_pair(1))

    # -- actions --

    def move(self, delta):
        rows = self.rows()
        if rows:
            self.sel_id = rows[max(0, min(len(rows) - 1, self.index(rows) + delta))]["id"]

    def edit(self, t):
        where = location(t, self.s.root) if t else None
        if not where:
            self.say("no file to open for this one", curses.color_pair(4))
            return
        path, line = where
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        try:
            if not editor:
                subprocess.run(["open", "-t", path], check=True, capture_output=True)
                self.say(f"opened {short_path(path)} — set $EDITOR to land on line {line}")
                return
            argv = shlex.split(editor)
            base = os.path.basename(argv[0])
            if base in GUI_EDITORS:
                argv += ["-g", f"{path}:{line}"] if base in ("code", "cursor", "codium", "windsurf") else [f"{path}:{line}"]
                subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.say(f"opened {short_path(path)}:{line}")
                return
            curses.def_prog_mode()
            curses.endwin()
            try:
                subprocess.call(argv + [f"+{line}", path])
            finally:
                curses.reset_prog_mode()
                self.stdscr.clear()
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            self.say(f"couldn't open the editor: {e}", curses.color_pair(4))

    def rerun_failed(self):
        ids = self.s.failed_ids()
        if ids:
            self.s.ask(f"rerunning {len(ids)} failed", ids)
        elif self.selected() is None or not any(t["status"] in PROBLEMS for t in self.rows()):
            self.say("nothing failing to rerun")
        else:
            self.s.ask("full run (a failure can't be rerun on its own)")

    def handle(self, ch):
        h = self.stdscr.getmaxyx()[0]
        page = max(1, h - 6)
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("r"):
            self.s.ask("rerun")
        elif ch == ord("f"):
            self.rerun_failed()
        elif ch == ord(" "):
            self.s.paused = not self.s.paused
            if not self.s.paused:
                self.s.wake.set()
            self.say("paused — a change made meanwhile runs the tests on resume" if self.s.paused else "watching again")
        elif ch == ord("\t"):
            self.view, self.follow = ("tests", True) if self.view == "output" else ("output", True)
        elif ch == ord("e") and self.view != "output":
            self.edit(self.selected())
        elif self.view == "tests":
            self.tests_key(ch, page)
        else:
            self.scroll_key(ch, page)
        return True

    def tests_key(self, ch, page):
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in steps:
            self.move(steps[ch])
        elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
            t = self.selected()
            if t:
                self.sel_id, self.view, self.scroll = t["id"], "detail", 0
        elif ch == ord("a"):
            self.only_problems = not self.only_problems
            self.top = 0
        elif ch == ord("b"):
            self.bell = not self.bell
            self.say("bell on: it beeps when a run fails" if self.bell else "bell off")
        elif ch == ord("n"):
            self.notify = not self.notify
            self.say("notifications on: one when the tests start failing or pass again" if self.notify
                     else "notifications off")

    def scroll_key(self, ch, page):
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in steps:
            self.scroll = max(0, self.scroll + steps[ch])
            self.follow = self.view == "output" and steps[ch] > 0 and ch in (curses.KEY_END, ord("G"))
        elif ch in (27, curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127, 8):
            self.view = "tests"
        elif self.view == "detail" and ch in (ord("n"), ord("p")):
            self.move(1 if ch == ord("n") else -1)
            self.scroll = 0


def run(stdscr, session):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, progress
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # passed
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # running, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # failed, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, session)
    session.start()
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            break
    if session.run and session.run.running:
        session.run.kill("cancelled")


def print_once(session):
    tty = sys.stdout.isatty()
    color = {2: "\033[32m", 3: "\033[33m", 4: "\033[31m", 5: "\033[2m"} if tty else {}
    bold, reset = ("\033[1m", "\033[0m") if tty else ("", "")
    print(f"{color.get(5, '')}{session.runner} · {short_path(session.python)} · {short_path(session.root)}{reset}")
    r = Run(session.command(), session.root, session.environment(), "once")
    r.start()
    try:
        while r.running and r.elapsed() < RUN_TIMEOUT:
            time.sleep(0.1)
    except KeyboardInterrupt:
        r.kill("cancelled")
        return 130
    if r.running:
        r.kill("timed out")
    while not r.finished:
        time.sleep(0.05)
    for t in sorted(r.tests, key=lambda t: STATUS[t["status"]][3]):
        if t["status"] not in PROBLEMS:
            continue
        mark, pair, _, _ = STATUS[t["status"]]
        print(f"\n{color.get(pair, '')}{mark}{reset} {bold}{t['name']}{reset}  {color.get(5, '')}{t['group']}{reset}")
        for line in t["detail"].rstrip().splitlines():
            print("    " + line)
    outcome = r.outcome()
    if outcome == "crashed":
        print(f"\n{color.get(4, '')}{r.error or f'the test runner exited with code {r.code}'}{reset}")
        for line in list(r.output)[-20:]:
            print("    " + line)
    counts = r.counts()
    parts = ", ".join(tally(counts[s], s) for s in STATUS if counts[s])
    text, pair = OUTCOME[outcome]
    print(f"\n{color.get(pair, '')}{bold}{text}{reset}  {parts}{'  ' if parts else ''}in {fmt_secs(r.secs)}")
    return 0 if outcome == "passed" else 1


def main():
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    parser = argparse.ArgumentParser(
        description="Rerun a Python project's tests whenever a .py file in it changes.",
        epilog="Anything after -- goes to the runner: pytest arguments, or test names and -k PATTERN "
               "for unittest. Example: test_watch_tui.py ~/src/project -- -k parser")
    parser.add_argument("path", nargs="?", default=".", help="the project folder (default: the current one)")
    parser.add_argument("--runner", choices=("auto", "unittest", "pytest"), default="auto",
                        help="auto (the default) uses pytest when the interpreter has it, else unittest")
    parser.add_argument("--python", metavar="PATH",
                        help="interpreter to run the tests with (default: the project's .venv or venv, else this one)")
    parser.add_argument("--once", action="store_true", help="run the tests once, print the failures and exit")
    args = parser.parse_args(argv)
    root = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        parser.error(f"not a folder: {args.path}")
    python = os.path.expanduser(args.python) if args.python else find_python(root)
    if args.python and not shutil.which(python):
        parser.error(f"no such interpreter: {args.python}")
    runner = args.runner
    if runner == "auto":
        runner = "pytest" if has_pytest(python, root) else "unittest"
    elif runner == "pytest" and not has_pytest(python, root):
        parser.error(f"pytest isn't installed for {python}")
    session = Session(root, python, runner, extra)
    if args.once:
        raise SystemExit(print_once(session))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, session)
    except KeyboardInterrupt:
        if session.run and session.run.running:
            session.run.kill("cancelled")


if __name__ == "__main__":
    main()
