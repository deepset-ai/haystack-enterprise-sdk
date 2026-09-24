"""Shared fixtures for unit tests."""

from pathlib import Path

import pytest
import structlog

from haystack_enterprise_sdk._api import config

# Route structlog through stdlib logging so ``caplog`` sees SDK warnings. haystack-ai < 3 did this as an
# import side effect; haystack-ai 3 no longer does.
structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())


@pytest.fixture(autouse=True)
def _isolate_global_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop unit tests from reading the developer's real ``~/.haystack-enterprise/.env``.

    ``CommonConfig.__post_init__`` calls ``load_environment()``, which loads the global ``.env`` from
    disk. On a machine where ``haystack-enterprise login`` has run, that repopulates ``API_KEY``/``API_URL``
    after a test clears the process env vars, so tests asserting the "no credentials configured"
    behaviour fail locally while passing on CI (where the file does not exist). Point the loader at a
    path that never exists so unit tests only ever see env vars they set explicitly.
    """
    monkeypatch.setattr(config, "ENV_FILE_PATH", Path("/nonexistent/haystack-enterprise/.env"))
