# Running the Test Suite on Windows

This document describes every change that had to be made so the `ha-dominion-sc`
test suite (and VS Code test discovery) works on Windows.

Home Assistant and its testing helper `pytest-homeassistant-custom-component`
are written with the assumption that they run on Linux. On Windows there are
**four separate, stacked problems**, each hidden behind the previous one. This
guide documents all of them, why they happen, and exactly how to fix them.

> **TL;DR** — If you just want a working environment, jump to
> [Quick Setup](#quick-setup) and run the steps in order.

---

## Table of Contents

1. [Environment](#environment)
2. [Symptom you will see first](#symptom-you-will-see-first)
3. [Problem 1 — `ModuleNotFoundError: No module named 'fcntl'`](#problem-1--modulenotfounderror-no-module-named-fcntl)
4. [Problem 2 — `ModuleNotFoundError: No module named 'resource'`](#problem-2--modulenotfounderror-no-module-named-resource)
5. [Problem 3 — `pytest_socket.SocketBlockedError`](#problem-3--pytest_socketsocketblockederror)
6. [Problem 4 — `FileNotFoundError: ... __editable__...__path_hook__`](#problem-4--filenotfounderror--__editable____path_hook__)
7. [Quick Setup](#quick-setup)
8. [Verifying the setup](#verifying-the-setup)
9. [Important caveats](#important-caveats)
10. [Summary table](#summary-table)

---

## Environment

These instructions were validated with:

| Item             | Value                                                       |
| ---------------- | ----------------------------------------------------------- |
| OS               | Windows (`win32`)                                           |
| Python           | 3.13.13 (managed by `uv`)                                   |
| Virtual env      | `.venv` in the repo root                                    |
| Package manager  | `uv` / `pip`                                                |
| Test runner      | `pytest` 9.x                                                |
| HA test helper   | `pytest-homeassistant-custom-component` 0.13.x              |

All commands below are written for **PowerShell** and are run from the repo
root: `ha-dominion-sc`.

---

## Symptom you will see first

VS Code's Test Explorer shows **no tests**, and the Python Test Log contains:

```text
ModuleNotFoundError: No module named 'fcntl'
...
File ".venv\Lib\site-packages\homeassistant\runner.py", line 11, in <module>
    import fcntl
```

Discovery fails before a single test is collected. Fixing this reveals the next
problem, and so on. Work through all four problems below in order.


---

## Problem 1 — `ModuleNotFoundError: No module named 'fcntl'`

### Cause

`homeassistant/runner.py` does `import fcntl`. The `fcntl` module is part of the
Python standard library **only on Unix** — it does not exist on Windows. The HA
test helper imports `homeassistant.runner` during plugin load, so pytest crashes
immediately.

### Fix

Two parts are required:

**1a. Install the `winfcntl` shim** (already present in `pyproject.toml`
`dependencies`):

```toml
[project]
dependencies = [
    "homeassistant>=2025.6.3",
    "dominion-sc-power==0.0.1",
    "winfcntl>=1.1.9",   # Windows shim for the Unix-only fcntl module
]
```

Installing `winfcntl` alone is **not enough**. `winfcntl` installs a package
literally named `winfcntl`, but `homeassistant/runner.py` does `import fcntl` —
Python looks for a module named `fcntl`, not `winfcntl`.

**1b. Create a `fcntl` bridge module** inside the virtual environment so that
`import fcntl` resolves to `winfcntl`:

Create `.venv\Lib\site-packages\fcntl.py` with this single line:

```python
from winfcntl import *
```

PowerShell one-liner:

```powershell
Set-Content -Path ".venv\Lib\site-packages\fcntl.py" -Value "from winfcntl import *"
```

---

## Problem 2 — `ModuleNotFoundError: No module named 'resource'`

### Cause

Once `fcntl` resolves, `homeassistant/runner.py` continues loading and imports
`homeassistant/util/resource.py`, which does `import resource`. Like `fcntl`,
the `resource` module is **Unix-only** and does not exist on Windows.

`homeassistant/util/resource.py` uses only three names from `resource`:
`RLIMIT_NOFILE`, `getrlimit()`, and `setrlimit()` — all inside a
`set_open_file_descriptor_limit()` helper that just tunes the open-file limit
(irrelevant on Windows).

### Fix

Create a minimal stub module `.venv\Lib\site-packages\resource.py`:

```python
RLIMIT_NOFILE = 7


def getrlimit(resource):
    return (4096, 4096)


def setrlimit(resource, limits):
    pass
```

PowerShell one-liner:

```powershell
@"
RLIMIT_NOFILE = 7

def getrlimit(resource):
    return (4096, 4096)

def setrlimit(resource, limits):
    pass
"@ | Set-Content -Path ".venv\Lib\site-packages\resource.py"
```

This lets `set_open_file_descriptor_limit()` run harmlessly (it reads a fake

---

## Problem 3 — `pytest_socket.SocketBlockedError`

### Cause

After the imports succeed, **all** tests error during setup with:

```text
pytest_socket.SocketBlockedError: A test tried to use socket.socket.
```

`pytest-homeassistant-custom-component` calls the following in its
`pytest_runtest_setup` hook:

```python
pytest_socket.disable_socket(allow_unix_socket=True)
```

- On **Linux**, Home Assistant's `HassEventLoopPolicy` creates an event loop
  whose internal "self-pipe" uses an **`AF_UNIX`** `socket.socketpair()` — which
  is explicitly allowed by `allow_unix_socket=True`.
- On **Windows**, the `ProactorEventLoop` self-pipe falls back to an **`AF_INET`**
  `socket.socketpair()` (Windows has no `AF_UNIX` socketpair). `pytest_socket`
  blocks `AF_INET` sockets, so the event loop cannot even be created, and every
  test errors during the session-scoped runner fixture setup.

### Fix

Add a `tryfirst` `pytest_runtest_setup` hook to `tests/conftest.py` that
neutralizes `disable_socket` **on Windows only**. It must run *before* the
plugin's own hook (hence `tryfirst=True`), because the fixture that builds the
event loop runs in the same setup phase.

```python
import sys

import pytest
import pytest_socket


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup() -> None:
    """
    Keep sockets enabled on Windows so the asyncio event loop can start.

    pytest-homeassistant-custom-component disables sockets during setup with
    ``allow_unix_socket=True``. On Linux the ProactorEventLoop self-pipe uses an
    AF_UNIX socketpair (allowed), but on Windows the ProactorEventLoop falls back
    to an AF_INET socketpair, which pytest_socket blocks. Neutralizing
    ``disable_socket`` on Windows lets the session-scoped event-loop runner
    fixture create its self-pipe. This runs ``tryfirst`` so the patch is in place
    before the plugin's own setup hook executes.
    """
    if sys.platform == "win32":
        pytest_socket.disable_socket = lambda *args, **kwargs: None  # type: ignore[assignment]
        pytest_socket.enable_socket()
```

**This is the only committed source change.** It is guarded by
`sys.platform == "win32"`, so it has no effect on Linux or CI.

---

## Problem 4 — `FileNotFoundError: ... __editable__...__path_hook__`

### Cause

With sockets working, tests fail with:

```text
FileNotFoundError: [WinError 3] The system cannot find the path specified:
    '__editable__.ha_dominion_sc-0.1.0.finder.__path_hook__'
```

The project had been installed as an **editable package** (`pip install -e .`).
Modern setuptools implements editable namespace packages by injecting a **fake
placeholder path** into the namespace package's `__path__`:

```python
NAMESPACES = {"custom_components": ["C:\\...\\custom_components"]}
PATH_PLACEHOLDER = "__editable__.ha_dominion_sc-0.1.0.finder" + ".__path_hook__"
# __path__ ends up as [<real dir>, <PATH_PLACEHOLDER>]
```

Home Assistant's `loader._get_custom_components()` iterates every entry in
`custom_components.__path__` and calls `os.scandir()` on each. The placeholder
string is not a real directory, so on Windows `os.scandir()` raises
`FileNotFoundError`.

### Fix

Uninstall the editable package. It is **redundant** for testing because
`pyproject.toml` already sets:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

That makes `custom_components.dominionsc` importable directly from the repo root
without any install.

```powershell
.\.venv\Scripts\python.exe -m pip uninstall ha-dominion-sc -y
```

> **Do not** run `pip install -e .` again in this venv, or this problem returns.
> See [Important caveats](#important-caveats) for the compatible install mode if
> you truly need the package installed.


---

## Quick Setup

Run these steps in order from the repo root in PowerShell:

```powershell
# 0. (One time) Create the venv and install dependencies.
#    uv sync installs everything from pyproject.toml, including winfcntl.
uv sync --extra dev

# 1. Create the fcntl bridge module (Problem 1).
Set-Content -Path ".venv\Lib\site-packages\fcntl.py" -Value "from winfcntl import *"

# 2. Create the resource stub module (Problem 2).
@"
RLIMIT_NOFILE = 7

def getrlimit(resource):
    return (4096, 4096)

def setrlimit(resource, limits):
    pass
"@ | Set-Content -Path ".venv\Lib\site-packages\resource.py"

# 3. Problem 3 is already handled in tests/conftest.py (committed).

# 4. Make sure the project is NOT editable-installed (Problem 4).
.\.venv\Scripts\python.exe -m pip uninstall ha-dominion-sc -y
```

Steps 1, 2, and 4 must be repeated any time the virtual environment is
recreated, because they modify files inside `.venv` (see caveats).

---

## Verifying the setup

**Discovery only:**

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only tests
```

Expected tail:

```text
========================= 39 tests collected in 0.xx s =========================
```

**Full run (with coverage from `addopts` in `pyproject.toml`):**

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

Expected tail:

```text
============================= 39 passed in x.xx s ==============================
```

In VS Code, run **Test: Refresh Tests** (or reload the window) and confirm the
39 tests appear in the Test Explorer. The `.vscode/settings.json` already
enables pytest:

```json
{
    "python.testing.pytestArgs": ["tests"],
    "python.testing.unittestEnabled": false,
    "python.testing.pytestEnabled": true
}
```

Also confirm the VS Code Python interpreter is set to `.\.venv\Scripts\python.exe`
(`Python: Select Interpreter`).

> **Note about the noisy warning:** You may still see a
> `PytestUnraisableExceptionWarning` mentioning
> `'ProactorEventLoop' object has no attribute '_ssock'` during teardown. This is
> a harmless Windows asyncio loop-cleanup warning and does **not** cause test
> failures.

---

## Important caveats

- **The `fcntl.py` and `resource.py` files live inside `.venv`.** They are
  *not* committed source and will be **deleted whenever the venv is rebuilt**
  (`uv sync` from scratch, deleting `.venv`, etc.). Re-run steps 1 and 2 of
  [Quick Setup](#quick-setup) after any venv rebuild.
- **Do not re-run `pip install -e .`** (plain editable install) in this venv, or
  Problem 4 returns. If you genuinely need the package installed in editable
  mode, use setuptools' compatibility mode which writes a real `.pth` path entry
  instead of the namespace finder hook:

  ```powershell
  .\.venv\Scripts\python.exe -m pip install -e . --config-settings editable_mode=compat
  ```

- **These fixes are Windows-only.** The only committed change
  (`tests/conftest.py`) is guarded by `sys.platform == "win32"`, so Linux
  developers and CI are unaffected. The `.venv` stubs never exist on Linux
  because `fcntl` and `resource` are part of the standard library there.

---

## Summary table

| # | Error | Root cause | Fix | Persists across venv rebuild? |
| - | ----- | ---------- | --- | ----------------------------- |
| 1 | `No module named 'fcntl'` | HA imports Unix-only `fcntl` | `winfcntl` dep + `.venv\...\fcntl.py` = `from winfcntl import *` | ❌ (recreate stub) |
| 2 | `No module named 'resource'` | HA imports Unix-only `resource` | `.venv\...\resource.py` stub | ❌ (recreate stub) |
| 3 | `pytest_socket.SocketBlockedError` | Windows event-loop self-pipe uses blocked `AF_INET` socketpair | `tryfirst` hook in `tests/conftest.py` (committed) | ✅ (committed) |
| 4 | `FileNotFoundError: ...__editable__...__path_hook__` | Editable install injects fake path into `custom_components.__path__` | `pip uninstall ha-dominion-sc` (rely on `pythonpath = ["."]`) | ❌ (don't reinstall editable) |
