---
paths:
  - "test_watch_tui.py"
---

# test_watch_tui.py

Reruns a Python project's tests whenever a `.py` file (or `pyproject.toml`, `setup.cfg`,
`pytest.ini`, `tox.ini`) below it changes: failures first with their last traceback line inline,
Enter for the full traceback, Tab for the raw output, `f` to rerun only what failed, `e` to open
`$EDITOR` at the deepest traceback frame inside the project. `--once` runs once and prints.

- **Results come from a side channel, not the console.** The tests run in a subprocess, and an
  embedded driver (`UNITTEST_DRIVER`, run with `python -c`) or pytest plugin (`PYTEST_PLUGIN`,
  written to a temp dir on `PYTHONPATH` and loaded with `-p _test_watch_plugin`) writes one JSON
  line per test to an extra pipe whose fd is in `TEST_WATCH_FD`. Both emit the same shape
  (`collected`, then `test` events with status, detail, file/line, `rerun`). Scraping
  `unittest -v` was rejected: docstrings split the result line and a test's prints land between
  `...` and `ok`.
- unittest cases that need care, all in the driver: `_FailedTest` (a module that didn't import),
  `_ErrorHolder` (setUpClass/setUpModule raised; reported without a `startTest`), and failing
  subtests, which never call `addFailure` on the parent — `stopTest` marks it failed. pytest 9
  sends a failed subtest as a report of its own followed by a passing one for the test, so the
  plugin never lets a status go back from fail/error to pass.
- The interpreter is the project's `.venv`/`venv`/`env` python when one exists, else
  `sys.executable`; `--runner auto` picks pytest if that interpreter can import it. The no-deps
  rule is about this script: pytest is only used when the project already has it.
- Runs start in a new session so `os.killpg` takes everything down: a change mid-run cancels it
  (SIGTERM, SIGKILL 2 s later), and when the test process exits whatever it left running in its
  group is terminated too — otherwise an orphan holds the output pipe and stacks up per rerun.
  `RUN_TIMEOUT` kills a hung run.
- Watching is a poll (`scan()`, every `POLL_SECONDS`) with a `DEBOUNCE_SECONDS` wait for editors'
  bursts of writes. Hidden folders, `__pycache__`, venvs, `build`/`dist` are skipped, which is
  what stops a run's own `.pyc` and pytest cache writes from triggering another run.
- `location()` reads both traceback shapes through `FRAME`: unittest's `File "…", line N` and
  pytest's `--tb=short` `path.py:N:` (relative to the root). Terminal editors get
  `+N file` between `endwin()` and `reset_prog_mode()`; `GUI_EDITORS` get `file:N` and no handoff.
- `f` passes the failed ids to the runner; when a failure can't be rerun alone (import error,
  setUpClass) it does a full run instead. The selected test is kept by id across runs.

Testing: a scratch project under `$CLAUDE_JOB_DIR/tmp` with pass/fail/error/skip/xfail, a failing
subtest, a broken import and a raising setUpClass covers the driver via `--once`. pytest isn't
installed system-wide; a throwaway venv in the job tmp dir (`pip install pytest`, then
`--python …/bin/python`) tests the plugin, with parametrize, a failing fixture and xfail/xpass.
The curses half runs in tmux (`capture-pane -p`) at 80×24 and 50×10; check that one save gives
exactly one new dot in the runs strip, and with a test that `Popen`s `sleep 30` that no `sleep`
outlives its run.
