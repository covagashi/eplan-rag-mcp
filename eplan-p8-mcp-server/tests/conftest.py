"""Shared test setup: import paths, and keeping the suite out of the real log.

Two import spellings are in use:

    from api.actions import scripted          # 10 modules - needs mcp_server/
    from mcp_server.api.actions import ...    #  2 modules - needs its parent

Only the first directory used to be added here. The second happened to work
anyway when `python -m pytest` was run from eplan-p8-mcp-server/, because
that puts the current directory on sys.path - so the suite passed from there
and failed collection with `ModuleNotFoundError: No module named
'mcp_server'` from the repo root, or under a bare `pytest`. Both are added
explicitly so the working directory stops mattering.
"""

import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVER_ROOT = os.path.dirname(_TESTS_DIR)                    # eplan-p8-mcp-server/
_PACKAGE_DIR = os.path.join(_SERVER_ROOT, "mcp_server")       # its mcp_server/

for _path in (_PACKAGE_DIR, _SERVER_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def pytest_configure(config):
    """Register the `live` marker.

    There is no pytest.ini in this repo, so an unregistered marker would warn on
    every run. `live` marks tests that need a real EPLAN answering the
    remote-control channel; they skip themselves when none does, so they are
    safe to leave in the default suite. Run only those with `-m live`, or
    exclude them with `-m "not live"`.
    """
    config.addinivalue_line(
        "markers",
        "live: needs a running EPLAN on the remote-control channel; "
        "skips itself when none answers",
    )


@pytest.fixture(autouse=True)
def _isolate_action_log(tmp_path, monkeypatch):
    """Point actions.jsonl at a tmp_path for every test in the suite.

    Without this the suite appends to mcp_server/logs/actions.jsonl - the same
    file the action audit reasons from. That had already happened at scale: a
    census on 2026-09-03 found 871 of its 1,463 entries were test fixtures from
    59 separate runs, spread through the file rather than clustered at the head,
    including entries whose action strings are injection-probe payloads. Any
    statistic drawn from that file was measuring the test suite as much as
    EPLAN.

    Autouse and unconditional on purpose. An opt-in fixture only protects the
    tests that remember to ask for it, and the polluting writes come from
    _log_action deep inside execute_action, where a test author has no reason
    to be thinking about logging at all.
    """
    monkeypatch.setenv("EPLAN_MCP_LOG_DIR", str(tmp_path / "logs"))
