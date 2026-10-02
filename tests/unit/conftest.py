"""Shared fixtures for unit tests."""

from pathlib import Path

import pytest

from haystack_enterprise_sdk import cli
from haystack_enterprise_sdk._api import config


@pytest.fixture(autouse=True)
def _isolate_global_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop unit tests from reading the developer's real ``~/.haystack-enterprise/.env`` or ``~/.deepset-cloud/.env``.

    ``CommonConfig.__post_init__`` calls ``load_environment()``, which loads the global ``.env`` from
    disk. On a machine where ``haystack-enterprise login`` has run, that repopulates ``API_KEY``/``API_URL``
    after a test clears the process env vars, so tests asserting the "no credentials configured"
    behaviour fail locally while passing on CI (where the file does not exist). Point the loader at a
    path that never exists so unit tests only ever see env vars they set explicitly.
    """
    monkeypatch.setattr(config, "ENV_FILE_PATH", Path("/nonexistent/haystack-enterprise/.env"))
    # `logout` deletes the legacy file, so a test must never point at the real one.
    monkeypatch.setattr(config, "LEGACY_ENV_FILE_PATH", Path("/nonexistent/deepset-cloud/.env"))
    monkeypatch.setattr(cli, "LEGACY_ENV_FILE_PATH", Path("/nonexistent/deepset-cloud/.env"))
