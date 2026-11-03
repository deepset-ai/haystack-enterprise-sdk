"""CLI tests for the `deploy-index` command."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

from ruamel.yaml import YAML
from typer.testing import CliRunner

from haystack_enterprise_sdk.cli import cli_app

runner = CliRunner()

INDEX_FIXTURE = "tests/test_data/deploy_index/indexing.py"


def _deploy_index(*args: str) -> Any:
    return runner.invoke(cli_app, ["deploy-index", INDEX_FIXTURE, "my-index", "--python", sys.executable, *args])


class TestDeployIndexCommand:
    def test_dry_run_renders_index_yaml_with_files_input(self, tmp_path: Path) -> None:
        out = tmp_path / "index.yaml"
        result = _deploy_index("--dry-run", "--output", str(out))

        assert result.exit_code == 0, result.output
        config = YAML(typ="safe").load(out.read_text())
        assert config["inputs"] == {"files": ["converter.sources"]}
        assert config["components"]["writer"]["type"].endswith("DocumentWriter")
        # Indexes run on the platform image, so no auto-detected haystack-ai pin is shipped.
        assert "dependencies" not in config

    def test_deploy_still_rejects_an_indexing_pipeline(self) -> None:
        result = runner.invoke(cli_app, ["deploy", INDEX_FIXTURE, "svc", "--dry-run", "--python", sys.executable])

        assert result.exit_code == 1
        assert "deploy-index" in result.output

    @patch("haystack_enterprise_sdk.cli.HaystackEnterpriseAPI")
    @patch("haystack_enterprise_sdk.cli.PipelineService")
    def test_deploy_saves_and_deploys_the_index(self, service_cls: Mock, api_cls: Mock) -> None:
        @asynccontextmanager
        async def _factory(_config: Any) -> AsyncIterator[Mock]:
            yield Mock()

        api_cls.factory = _factory
        service_cls.return_value.deploy_index_yaml = AsyncMock()

        result = _deploy_index("--workspace-name", "ws", "--api-key", "key")

        assert result.exit_code == 0, result.output
        call = service_cls.return_value.deploy_index_yaml.await_args
        assert call.args[0] == "my-index"
        assert "converter.sources" in call.args[1]
        assert call.kwargs == {"validate": True, "activate": True}
        assert "Deployed index 'my-index'" in result.output
