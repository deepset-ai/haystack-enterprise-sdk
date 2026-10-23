import datetime
import logging
import threading
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Dict, Generator, List
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
import respx
import structlog
import typer
from typer.testing import CliRunner

__version__ = version("haystack-enterprise-sdk")
from haystack_enterprise_sdk._api.files import File
from haystack_enterprise_sdk._api.oauth import CliOAuthConfig, DeviceLoginUnsupported, OAuthCredentials
from haystack_enterprise_sdk._api.upload_sessions import (
    UploadSessionDetail,
    UploadSessionIngestionStatus,
    UploadSessionStatus,
    UploadSessionStatusEnum,
    UploadSessionWriteModeEnum,
    WriteMode,
)
from haystack_enterprise_sdk.cli import (
    _PYPI_URL,
    _browser_login,
    _configure_cli_logging,
    _maybe_print_update_hint,
    _ui_url_for,
    cli_app,
    run_packaged,
)
from haystack_enterprise_sdk.models import UserInfo
from haystack_enterprise_sdk.workflows.sync_client.files import download as sync_download

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep login/logout off the real ~/.haystack-enterprise and the network; OAuth is off unless a test enables it."""
    credentials_path = tmp_path / "credentials.json"
    monkeypatch.setattr("haystack_enterprise_sdk._api.oauth.CREDENTIALS_PATH", credentials_path)
    monkeypatch.setattr("haystack_enterprise_sdk.cli.CREDENTIALS_PATH", credentials_path)
    monkeypatch.setattr("haystack_enterprise_sdk.cli.fetch_cli_config", lambda api_url: None)
    return credentials_path


class TestCLIMethods:
    @patch("haystack_enterprise_sdk.workflows.sync_client.files.async_upload")
    def test_uploading(self, async_upload_mock: AsyncMock) -> None:
        def log_upload_folder_mock(
            *args: Any,
            **kwargs: Any,
        ) -> None:
            print("Fake log line")

        async_upload_mock.side_effect = log_upload_folder_mock
        result = runner.invoke(cli_app, ["upload", "./test/data/upload_folder/example.txt"])
        assert result.exit_code == 0
        assert "Fake log line" in result.stdout

    @patch("haystack_enterprise_sdk.workflows.sync_client.files.async_upload")
    def test_raising_exception_during_cli_run(self, async_upload_mock: AsyncMock) -> None:
        async_upload_mock.side_effect = ValueError(
            "API_KEY environment variable must be set. Please visit https://cloud.deepset.ai/settings/connections to get an API key."
        )
        result = runner.invoke(cli_app, ["upload", "./test/data/upload_folder/example.txt"])
        assert result.exit_code == 1

    @patch("haystack_enterprise_sdk.workflows.sync_client.files.async_upload")
    def test_upload_only_desired_file_types_defaults_to_text(self, async_upload_mock: AsyncMock) -> None:
        result = runner.invoke(
            cli_app,
            [
                "upload",
                "./test/data/upload_folder/example.txt",
                "--workspace-name",
                "default",
                "--enable-parallel-processing",
            ],
        )
        async_upload_mock.assert_called_once_with(
            paths=[Path("test/data/upload_folder/example.txt")],
            api_key=None,
            api_url=None,
            workspace_name="default",
            write_mode=WriteMode.KEEP,
            blocking=True,
            timeout_s=None,
            show_progress=True,
            recursive=False,
            desired_file_types=None,
            enable_parallel_processing=True,
            safe_mode=False,
        )
        assert result.exit_code == 0

    @patch("haystack_enterprise_sdk.workflows.sync_client.files.async_upload")
    def test_upload_only_desired_file_types_with_desired_file_types(self, async_upload_mock: AsyncMock) -> None:
        result = runner.invoke(
            cli_app,
            [
                "upload",
                "./test/data/upload_folder/example.txt",
                "--workspace-name",
                "default",
                "--use-type",
                ".csv",
                "--use-type",
                ".pdf",
                "--use-type",
                ".json",
                "--use-type",
                ".xml",
            ],
        )
        async_upload_mock.assert_called_once_with(
            paths=[Path("test/data/upload_folder/example.txt")],
            api_key=None,
            api_url=None,
            workspace_name="default",
            write_mode=WriteMode.KEEP,
            blocking=True,
            timeout_s=None,
            show_progress=True,
            recursive=False,
            desired_file_types=[".csv", ".pdf", ".json", ".xml"],
            enable_parallel_processing=False,
            safe_mode=False,
        )
        assert result.exit_code == 0

    @patch("haystack_enterprise_sdk.workflows.sync_client.files.async_upload")
    def test_upload_safe_mode(self, async_upload_mock: AsyncMock) -> None:
        result = runner.invoke(
            cli_app,
            [
                "upload",
                "./test/data/upload_folder/example.txt",
                "--workspace-name",
                "default",
                "--safe-mode",
            ],
        )
        async_upload_mock.assert_called_once_with(
            paths=[Path("test/data/upload_folder/example.txt")],
            api_key=None,
            api_url=None,
            workspace_name="default",
            write_mode=WriteMode.KEEP,
            blocking=True,
            timeout_s=None,
            show_progress=True,
            recursive=False,
            desired_file_types=None,
            enable_parallel_processing=False,
            safe_mode=True,
        )
        assert result.exit_code == 0

    class TestDownloadFiles:
        @patch("haystack_enterprise_sdk.cli.sync_download")
        def test_download_files(self, sync_download_mock: AsyncMock) -> None:
            sync_download_mock.side_effect = Mock(spec=sync_download)
            result = runner.invoke(cli_app, ["download", "--workspace-name", "default"])
            assert result.exit_code == 0
            sync_download_mock.assert_called_once_with(
                workspace_name="default",
                file_dir=None,
                name=None,
                odata_filter=None,
                include_meta=True,
                batch_size=50,
                api_key=None,
                api_url=None,
                show_progress=True,
                safe_mode=False,
            )

        @patch("haystack_enterprise_sdk.cli.sync_download")
        def test_download_files_safe_mode(self, sync_download_mock: AsyncMock) -> None:
            sync_download_mock.side_effect = Mock(spec=sync_download)
            result = runner.invoke(cli_app, ["download", "--workspace-name", "default", "--safe-mode"])
            assert result.exit_code == 0
            sync_download_mock.assert_called_once_with(
                workspace_name="default",
                file_dir=None,
                name=None,
                odata_filter=None,
                include_meta=True,
                batch_size=50,
                api_key=None,
                api_url=None,
                show_progress=True,
                safe_mode=True,
            )

    class TestListFiles:
        @patch("haystack_enterprise_sdk.cli.sync_list_files")
        def test_listing_files(self, sync_list_files_mock: Mock) -> None:
            def mocked_list_files(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[File], None, None]:
                yield [
                    File(
                        file_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        url="/api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10",
                        name="silly_things_1.txt",
                        size=611,
                        meta={},
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    )
                ]

            sync_list_files_mock.side_effect = mocked_list_files
            result = runner.invoke(cli_app, ["list-files"], input="y\n")
            assert result.exit_code == 0
            assert (
                " cd16435f-f6eb-423f-bf6f-994dc8a36a10 | /api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10 | silly_things_1.txt |    611 | 2022-06-21 16:40:00.634653+00:00 | {}  "
                in result.stdout
            )

        @patch("haystack_enterprise_sdk.cli.sync_list_files")
        def test_listing_files_with_timeout(self, sync_list_files_mock: Mock) -> None:
            sync_list_files_mock.side_effect = TimeoutError()
            result = runner.invoke(cli_app, ["list-files"])
            assert result.exit_code == 0
            assert "Command timed out." in result.stdout

        @patch("haystack_enterprise_sdk.cli.sync_list_files")
        def test_listing_files_with_no_found_files(self, sync_list_files_mock: Mock) -> None:
            def mocked_list_files(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[File], None, None]:
                yield []

            sync_list_files_mock.side_effect = mocked_list_files
            result = runner.invoke(cli_app, ["list-files"])
            assert result.exit_code == 0
            assert (
                "+-----------+-------+--------+--------+--------------+--------+\n| file_id   | url   | name   | size   | created_at   | meta   |\n+===========+=======+========+========+==============+========+\n+-----------+-------+--------+--------+--------------+--------+\n"
                in result.stdout
            )

        @patch("haystack_enterprise_sdk.cli.sync_list_files")
        def test_listing_files_with_cut_off(self, sync_list_files_mock: AsyncMock) -> None:
            def mocked_list_files(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[File], None, None]:
                yield [
                    File(
                        file_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        url="/api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10",
                        name="silly_things_1.txt",
                        size=611,
                        meta={},
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    )
                ]
                yield [
                    File(
                        file_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        url="/api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10",
                        name="silly_things_1.txt",
                        size=611,
                        meta={},
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    )
                ]

            sync_list_files_mock.side_effect = mocked_list_files
            result = runner.invoke(cli_app, ["list-files", "--batch-size", "1"], input="y\n\n")
            assert result.exit_code == 0
            # check that two batches are printed
            assert (
                "+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\n| file_id                              | url                                                                        | name               |   size | created_at                       | meta   |\n+======================================+============================================================================+====================+========+==================================+========+\n| cd16435f-f6eb-423f-bf6f-994dc8a36a10 | /api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10 | silly_things_1.txt |    611 | 2022-06-21 16:40:00.634653+00:00 | {}     |\n+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\nPrint more results ? [y]: y\n+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\n| file_id                              | url                                                                        | name               |   size | created_at                       | meta   |\n+======================================+============================================================================+====================+========+==================================+========+\n| cd16435f-f6eb-423f-bf6f-994dc8a36a10 | /api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10 | silly_things_1.txt |    611 | 2022-06-21 16:40:00.634653+00:00 | {}     |\n+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\nPrint more results ? [y]: \n"
                == result.stdout
            )

        @patch("haystack_enterprise_sdk.cli.sync_list_files")
        def test_listing_files_with_break_showing_more_results(self, sync_list_files_mock: AsyncMock) -> None:
            def mocked_list_files(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[File], None, None]:
                yield [
                    File(
                        file_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        url="/api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10",
                        name="silly_things_1.txt",
                        size=611,
                        meta={},
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    )
                ]
                yield [
                    File(
                        file_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        url="/api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10",
                        name="silly_things_1.txt",
                        size=611,
                        meta={},
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    )
                ]

            sync_list_files_mock.side_effect = mocked_list_files
            result = runner.invoke(cli_app, ["list-files", "--batch-size", "1"], input="n")
            assert result.exit_code == 0
            # check that two batches are printed
            assert (
                "+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\n| file_id                              | url                                                                        | name               |   size | created_at                       | meta   |\n+======================================+============================================================================+====================+========+==================================+========+\n| cd16435f-f6eb-423f-bf6f-994dc8a36a10 | /api/v1/workspaces/search tests/files/cd16435f-f6eb-423f-bf6f-994dc8a36a10 | silly_things_1.txt |    611 | 2022-06-21 16:40:00.634653+00:00 | {}     |\n+--------------------------------------+----------------------------------------------------------------------------+--------------------+--------+----------------------------------+--------+\nPrint more results ? [y]: n\n"
                == result.stdout
            )

    class TestListUploadSessions:
        @patch("haystack_enterprise_sdk.cli.sync_list_upload_sessions")
        def test_listing_upload_sessions(self, sync_list_upload_sessions: Mock) -> None:
            def mocked_list_upload_sessions(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[UploadSessionDetail], None, None]:
                yield [
                    UploadSessionDetail(
                        session_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        created_by=UserInfo(
                            user_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                            given_name="Fake",
                            family_name="User",
                        ),
                        expires_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:10:00.634653+00:00"),
                        write_mode=UploadSessionWriteModeEnum.KEEP,
                        status=UploadSessionStatusEnum.OPEN,
                    )
                ]

            sync_list_upload_sessions.side_effect = mocked_list_upload_sessions
            result = runner.invoke(cli_app, ["list-upload-sessions"], input="y\n")
            assert result.exit_code == 0
            assert (
                "cd16435f-f6eb-423f-bf6f-994dc8a36a10 | Fake User    | 2022-06-21 16:10:00.634653+00:00 | 2022-06-21 16:40:00.634653+00:00 | KEEP         | OPEN"
                in result.stdout
            )

        @patch("haystack_enterprise_sdk.cli.sync_list_upload_sessions")
        def test_listing_upload_sessions_with_break(self, sync_list_upload_sessions: Mock) -> None:
            def mocked_list_upload_sessions(
                *args: Any,
                **kwargs: Any,
            ) -> Generator[List[UploadSessionDetail], None, None]:
                yield [
                    UploadSessionDetail(
                        session_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        created_by=UserInfo(
                            user_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                            given_name="Fake",
                            family_name="User",
                        ),
                        expires_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:10:00.634653+00:00"),
                        write_mode=UploadSessionWriteModeEnum.KEEP,
                        status=UploadSessionStatusEnum.OPEN,
                    )
                ]
                yield [
                    UploadSessionDetail(
                        session_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                        created_by=UserInfo(
                            user_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                            given_name="Not In There",
                            family_name="Not In There",
                        ),
                        expires_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                        created_at=datetime.datetime.fromisoformat("2022-06-21T16:10:00.634653+00:00"),
                        write_mode=UploadSessionWriteModeEnum.KEEP,
                        status=UploadSessionStatusEnum.OPEN,
                    ),
                ]

            sync_list_upload_sessions.side_effect = mocked_list_upload_sessions
            result = runner.invoke(cli_app, ["list-upload-sessions", "--batch-size", "1"], input="n")
            assert result.exit_code == 0
            assert "Not In There" not in result.stdout

        @patch("haystack_enterprise_sdk.cli.sync_list_upload_sessions")
        def test_listing_files_with_timeout(self, sync_list_upload_sessions: Mock) -> None:
            sync_list_upload_sessions.side_effect = TimeoutError()
            result = runner.invoke(cli_app, ["list-upload-sessions"])
            assert result.exit_code == 0
            assert "Command timed out." in result.stdout

    class TestGetUploadSession:
        @patch("haystack_enterprise_sdk.cli.sync_get_upload_session")
        def test_get_upload_session(self, sync_get_upload_session: AsyncMock) -> None:
            def mocked_get_upload_session(
                *args: Any,
                **kwargs: Any,
            ) -> UploadSessionStatus:
                return UploadSessionStatus(
                    session_id=UUID("cd16435f-f6eb-423f-bf6f-994dc8a36a10"),
                    expires_at=datetime.datetime.fromisoformat("2022-06-21T16:40:00.634653+00:00"),
                    documentation_url="https://docs.deepset.ai",
                    ingestion_status=UploadSessionIngestionStatus(
                        failed_files=0,
                        finished_files=1,
                    ),
                )

            sync_get_upload_session.side_effect = mocked_get_upload_session
            result = runner.invoke(cli_app, ["get-upload-session", "cd16435f-f6eb-423f-bf6f-994dc8a36a10"])
            assert result.exit_code == 0
            assert (
                result.stdout
                == '{\n    "session_id": "cd16435f-f6eb-423f-bf6f-994dc8a36a10",\n    "expires_at": "2022-06-21 16:40:00.634653+00:00",\n    "documentation_url": "https://docs.deepset.ai",\n    "ingestion_status": {\n        "failed_files": 0,\n        "finished_files": 1\n    }\n}\n'
            )


class TestCLIUtils:
    def test_login_with_minimal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test login command with minimal input."""
        # Create a temporary directory for the global .env file
        global_env_dir = tmp_path / ".haystack-enterprise"
        global_env_dir.mkdir()
        global_env_path = global_env_dir / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)

        # Accept the platform URL (empty confirm defaults to yes), default workspace.
        result = runner.invoke(cli_app, ["login", "--no-browser"], input="\ntest_api_key\n\n")
        assert result.exit_code == 0
        assert "Logged in." in result.stdout
        assert (
            "API_KEY=test_api_key\nAPI_URL=https://api.cloud.deepset.ai\nDEFAULT_WORKSPACE_NAME=default"
            == global_env_path.read_text()
        )

    def test_login_removes_legacy_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        legacy_file = tmp_path / "deepset-cloud" / ".env"
        legacy_file.parent.mkdir()
        legacy_file.write_text("API_KEY=old_key")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", tmp_path / "haystack-enterprise" / ".env")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.LEGACY_ENV_FILE_PATH", legacy_file)

        result = runner.invoke(cli_app, ["login", "--no-browser"], input="\ntest_api_key\n\n")
        assert result.exit_code == 0
        assert not legacy_file.exists()

    def test_login_with_custom_base_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create a temporary directory for the global .env file
        global_env_dir = tmp_path / ".haystack-enterprise"
        global_env_dir.mkdir()
        global_env_path = global_env_dir / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)

        # Decline the platform URL, then provide a custom base URL.
        result = runner.invoke(
            cli_app,
            ["login", "--no-browser"],
            input="n\nhttps://custom-api.example.com\ntest_api_key\nmy_workspace\n",
        )
        assert result.exit_code == 0
        assert "Logged in." in result.stdout
        assert (
            "API_KEY=test_api_key\nAPI_URL=https://custom-api.example.com\nDEFAULT_WORKSPACE_NAME=my_workspace"
            == global_env_path.read_text()
        )

    def test_login_custom_url_with_version_is_normalized(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create a temporary directory for the global .env file
        global_env_dir = tmp_path / ".haystack-enterprise"
        global_env_dir.mkdir()
        global_env_path = global_env_dir / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)

        # A pasted URL that still includes the version suffix is normalized to the base.
        result = runner.invoke(
            cli_app,
            ["login", "--no-browser"],
            input="n\nhttps://custom-api.example.com/api/v1\ntest_api_key\nmy_workspace\n",
        )
        assert result.exit_code == 0
        assert (
            "API_KEY=test_api_key\nAPI_URL=https://custom-api.example.com\nDEFAULT_WORKSPACE_NAME=my_workspace"
            == global_env_path.read_text()
        )

    def test_login_with_flags_skips_prompts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        global_env_path = tmp_path / ".haystack-enterprise" / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)

        with patch("haystack_enterprise_sdk.cli.webbrowser.open") as open_mock:
            result = runner.invoke(cli_app, ["login", "--api-key", "test_api_key", "--workspace-name", "my_workspace"])

        assert result.exit_code == 0
        open_mock.assert_not_called()
        assert (
            "API_KEY=test_api_key\nAPI_URL=https://api.cloud.deepset.ai\nDEFAULT_WORKSPACE_NAME=my_workspace"
            == global_env_path.read_text()
        )
        assert global_env_path.stat().st_mode & 0o777 == 0o600

    def test_login_through_browser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        global_env_path = tmp_path / ".haystack-enterprise" / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)
        responses: List[int] = []

        def fake_browser(url: str) -> bool:
            # Stand in for the platform page: post a forged callback first, then the real one.
            parsed = urlsplit(url)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            assert parsed.netloc == "cloud.deepset.ai" and parsed.path == "/cli-login"
            callback = f"http://127.0.0.1:{query['port']}/callback"

            def post() -> None:
                forged = {"state": "wrong", "api_key": "evil_key", "workspace": "evil"}
                responses.append(httpx.post(callback, data=forged).status_code)
                real = {"state": query["state"], "api_key": "browser_key", "workspace": "my_workspace"}
                responses.append(httpx.post(callback, data=real).status_code)

            threading.Thread(target=post).start()
            return True

        with patch("haystack_enterprise_sdk.cli.webbrowser.open", side_effect=fake_browser):
            result = runner.invoke(cli_app, ["login"])

        assert result.exit_code == 0, result.stdout
        assert responses == [400, 200]
        assert (
            "API_KEY=browser_key\nAPI_URL=https://api.cloud.deepset.ai\nDEFAULT_WORKSPACE_NAME=my_workspace"
            == global_env_path.read_text()
        )
        assert global_env_path.stat().st_mode & 0o777 == 0o600

    def test_browser_login_times_out(self) -> None:
        with patch("haystack_enterprise_sdk.cli.webbrowser.open"), pytest.raises(typer.Exit):
            _browser_login("https://cloud.deepset.ai", timeout=0.2)

    @pytest.mark.parametrize(
        "api_url, ui_url",
        [
            ("https://api.cloud.deepset.ai", "https://cloud.deepset.ai"),
            ("https://api.us.deepset.ai", "https://us.deepset.ai"),
            ("http://localhost:8000", "http://localhost:8000"),
        ],
    )
    def test_ui_url_for(self, api_url: str, ui_url: str) -> None:
        assert _ui_url_for(api_url) == ui_url

    @pytest.mark.parametrize(
        "args, device_supported, used",
        [
            ([], True, "device_login"),
            ([], False, "authorization_code_login"),
            (["--no-device"], True, "authorization_code_login"),
        ],
    )
    def test_login_with_oauth(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_login: Path,
        args: List[str],
        device_supported: bool,
        used: str,
    ) -> None:
        global_env_path = tmp_path / ".haystack-enterprise" / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)
        config = CliOAuthConfig(issuer="https://auth.example.com/", client_id="cli", scopes=["openid"])
        monkeypatch.setattr("haystack_enterprise_sdk.cli.fetch_cli_config", lambda api_url: config)
        credentials = OAuthCredentials(
            api_url="https://api.cloud.deepset.ai",
            client_id="cli",
            token_endpoint="https://auth.example.com/token",
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 600,
        )
        responses = {
            "https://api.cloud.deepset.ai/api/v1/sso/organizations": {
                "organizations": [
                    {"organization_id": "org-a", "name": "Acme"},
                    {"organization_id": "org-b", "name": "Beta"},
                ]
            },
            "https://api.cloud.deepset.ai/api/v1/me": {"organization": {"workspaces": [{"name": "default"}]}},
        }

        def fake_get(url: str, headers: Dict[str, str], timeout: int) -> httpx.Response:
            assert headers["Authorization"] == "Bearer access"
            return httpx.Response(200, json=responses[url], request=httpx.Request("GET", url))

        device = Mock(return_value=credentials) if device_supported else Mock(side_effect=DeviceLoginUnsupported())
        logins = {"device_login": device, "authorization_code_login": Mock(return_value=credentials)}
        with (
            patch("haystack_enterprise_sdk.cli.device_login", logins["device_login"]),
            patch("haystack_enterprise_sdk.cli.authorization_code_login", logins["authorization_code_login"]),
            patch("haystack_enterprise_sdk.cli.httpx.get", side_effect=fake_get),
        ):
            result = runner.invoke(cli_app, ["login", *args], input="2\n")

        assert result.exit_code == 0, result.stdout
        assert logins[used].called
        assert logins["device_login"].called == ("--no-device" not in args)
        assert "API_URL=https://api.cloud.deepset.ai\nDEFAULT_WORKSPACE_NAME=default" == global_env_path.read_text()
        stored = OAuthCredentials.load()
        assert stored is not None and stored.organization_id == "org-b"
        assert _isolate_login.stat().st_mode & 0o777 == 0o600

    def test_login_with_oauth_picks_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_login: Path
    ) -> None:
        global_env_path = tmp_path / ".haystack-enterprise" / ".env"
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", global_env_path)
        config = CliOAuthConfig(issuer="https://auth.example.com/", client_id="cli", scopes=["openid"])
        monkeypatch.setattr("haystack_enterprise_sdk.cli.fetch_cli_config", lambda api_url: config)
        credentials = OAuthCredentials(
            api_url="https://api.cloud.deepset.ai",
            client_id="cli",
            token_endpoint="https://auth.example.com/token",
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 600,
        )
        responses = {
            "https://api.cloud.deepset.ai/api/v1/sso/organizations": {
                "organizations": [
                    {"organization_id": "org-a", "name": "Acme"},
                    {"organization_id": "org-b", "name": "Beta"},
                ]
            },
            "https://api.cloud.deepset.ai/api/v1/me": {
                "organization": {"workspaces": [{"name": "default"}, {"name": "prod"}]}
            },
        }

        def fake_get(url: str, headers: Dict[str, str], timeout: int) -> httpx.Response:
            return httpx.Response(200, json=responses[url], request=httpx.Request("GET", url))

        with (
            patch("haystack_enterprise_sdk.cli.authorization_code_login", return_value=credentials),
            patch("haystack_enterprise_sdk.cli.httpx.get", side_effect=fake_get),
        ):
            result = runner.invoke(cli_app, ["login", "--no-device"], input="nope\nBeta\n3\nprod\n")

        assert result.exit_code == 0, result.output
        assert "'nope' is not one of the options above." in result.output
        assert "'3' is not one of the options above." in result.output
        assert global_env_path.read_text().endswith("DEFAULT_WORKSPACE_NAME=prod")
        stored = OAuthCredentials.load()
        assert stored is not None and stored.organization_id == "org-b"

    def test_logout_revokes_oauth_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_login: Path
    ) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", tmp_path / ".env")
        OAuthCredentials(
            api_url="https://api.cloud.deepset.ai",
            client_id="cli",
            token_endpoint="https://auth.example.com/token",
            access_token="access",
            refresh_token="refresh",
            expires_at=0,
        ).save()

        with patch("haystack_enterprise_sdk.cli.revoke") as revoke_mock:
            result = runner.invoke(cli_app, ["logout"])

        assert result.exit_code == 0
        assert revoke_mock.call_args.args[0].refresh_token == "refresh"
        assert not _isolate_login.exists()

    def test_whoami(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "key")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.DEFAULT_WORKSPACE_NAME", "default")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer key"
            return httpx.Response(200, json=[{"name": "default"}])

        async_client = httpx.AsyncClient
        with patch(
            "haystack_enterprise_sdk.cli.httpx.AsyncClient",
            lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            result = runner.invoke(cli_app, ["whoami"])
        assert result.exit_code == 0, result.output
        assert "with an API key" in result.stdout
        assert "Default workspace: default" in result.stdout

    def test_whoami_with_login_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.setattr("haystack_enterprise_sdk._api.config.API_KEY", "")
        OAuthCredentials(
            api_url="https://api.cloud.deepset.ai",
            client_id="cli",
            token_endpoint="https://auth.example.com/token",
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
        ).save()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer access"
            return httpx.Response(200, json=[{"name": "default"}])

        async_client = httpx.AsyncClient
        with patch(
            "haystack_enterprise_sdk.cli.httpx.AsyncClient",
            lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            result = runner.invoke(cli_app, ["whoami"])
        assert result.exit_code == 0, result.output
        assert "with a login session" in result.stdout

    def test_whoami_warns_about_unknown_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "key")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.DEFAULT_WORKSPACE_NAME", "gone")
        response = httpx.Response(200, json=[{"name": "default"}], request=httpx.Request("GET", "https://x"))
        with patch("haystack_enterprise_sdk.cli._list_workspaces", AsyncMock(return_value=response)):
            result = runner.invoke(cli_app, ["whoami"])
        assert result.exit_code == 0
        assert "'gone' not found. Available: default" in result.stdout

    def test_whoami_rejected_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "bogus")
        response = httpx.Response(401, request=httpx.Request("GET", "https://x"))
        with patch("haystack_enterprise_sdk.cli._list_workspaces", AsyncMock(return_value=response)):
            result = runner.invoke(cli_app, ["whoami"])
        assert result.exit_code == 1
        assert "Authentication failed" in result.output

    def test_whoami_not_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("haystack_enterprise_sdk.cli.CommonConfig", side_effect=ValueError("API key is required.")):
            result = runner.invoke(cli_app, ["whoami"])
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_logout_if_not_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", Path("/nonexistent/path/.env"))
        result = runner.invoke(cli_app, ["logout"])
        assert result.exit_code == 0
        assert "No global configuration file found. Nothing to do!" in result.stdout

    def test_logout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create a temporary .env file
        env_file = tmp_path / ".env"
        env_file.touch()
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", env_file)

        result = runner.invoke(cli_app, ["logout"])
        assert result.exit_code == 0
        assert "removed successfully" in result.stdout
        assert not env_file.exists()

    def test_logout_removes_legacy_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / "haystack-enterprise" / ".env"
        legacy_file = tmp_path / "deepset-cloud" / ".env"
        for path in (env_file, legacy_file):
            path.parent.mkdir()
            path.touch()
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", env_file)
        monkeypatch.setattr("haystack_enterprise_sdk.cli.LEGACY_ENV_FILE_PATH", legacy_file)

        result = runner.invoke(cli_app, ["logout"])
        assert result.exit_code == 0
        assert not env_file.exists()
        assert not legacy_file.exists()

    def test_logout_with_only_legacy_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        legacy_file = tmp_path / ".env"
        legacy_file.touch()
        monkeypatch.setattr("haystack_enterprise_sdk.cli.ENV_FILE_PATH", tmp_path / "missing" / ".env")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.LEGACY_ENV_FILE_PATH", legacy_file)

        result = runner.invoke(cli_app, ["logout"])
        assert result.exit_code == 0
        assert f"{legacy_file} removed successfully" in result.stdout
        assert not legacy_file.exists()

    def test_get_version(self) -> None:
        result = runner.invoke(cli_app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout


class TestCLILogging:
    @pytest.fixture(autouse=True)
    def _restore_logging(self) -> Generator[None, None, None]:
        """Restore the library's default structlog config after each test."""
        yield
        _configure_cli_logging(False)
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    def test_quiet_by_default_suppresses_info(self, capsys: pytest.CaptureFixture) -> None:
        _configure_cli_logging(False)
        structlog.get_logger("test").info("should-not-appear")
        assert "should-not-appear" not in capsys.readouterr().err

    def test_quiet_still_shows_warning(self, capsys: pytest.CaptureFixture) -> None:
        _configure_cli_logging(False)
        structlog.get_logger("test").warning("should-appear")
        captured = capsys.readouterr()
        assert "should-appear" in (captured.out + captured.err)

    def test_verbose_shows_info_and_debug(self, capsys: pytest.CaptureFixture) -> None:
        _configure_cli_logging(True)
        structlog.get_logger("test").info("info-line")
        structlog.get_logger("test").debug("debug-line")
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "info-line" in combined
        assert "debug-line" in combined


class TestUpdateHint:
    @pytest.fixture(autouse=True)
    def _interactive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk.cli.VERSION_CHECK_PATH", tmp_path / "version_check.json")
        monkeypatch.setattr("haystack_enterprise_sdk.cli.__version__", "1.0.2")
        monkeypatch.setattr("haystack_enterprise_sdk.cli._stderr_is_tty", lambda: True)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("HAYSTACK_ENTERPRISE_NO_UPDATE_CHECK", raising=False)

    @respx.mock
    def test_newer_version_prints_hint_and_caches(self, capsys: pytest.CaptureFixture) -> None:
        route = respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "1.10.0"}}))
        _maybe_print_update_hint()
        _maybe_print_update_hint()
        assert capsys.readouterr().err.count("1.0.2 -> 1.10.0") == 2
        assert route.call_count == 1

    @pytest.mark.parametrize(
        "prefix,command",
        [
            ("/home/u/.local/share/uv/tools/haystack-enterprise-sdk", "uv tool upgrade haystack-enterprise-sdk"),
            ("/home/u/project/.venv", "pip install -U haystack-enterprise-sdk"),
        ],
    )
    @respx.mock
    def test_upgrade_command_matches_install(
        self, prefix: str, command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.prefix", prefix)
        respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "1.1.0"}}))
        _maybe_print_update_hint()
        assert capsys.readouterr().err.strip().endswith(f"Upgrade with: {command}")

    @pytest.mark.parametrize("latest", ["1.0.2", "0.9.9", "2.0.0rc1"])
    @respx.mock
    def test_no_hint_when_not_newer(self, latest: str, capsys: pytest.CaptureFixture) -> None:
        respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": latest}}))
        _maybe_print_update_hint()
        assert capsys.readouterr().err == ""

    @respx.mock
    def test_network_error_is_silent(self, capsys: pytest.CaptureFixture) -> None:
        respx.get(_PYPI_URL).mock(side_effect=httpx.ConnectError("offline"))
        _maybe_print_update_hint()
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("env", ["CI", "HAYSTACK_ENTERPRISE_NO_UPDATE_CHECK"])
    @respx.mock
    def test_disabled_skips_network(self, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(env, "1")
        route = respx.get(_PYPI_URL)
        _maybe_print_update_hint()
        assert not route.called


def test_run_packaged_uses_os_trust_store() -> None:
    with (
        patch("haystack_enterprise_sdk.cli.truststore.inject_into_ssl") as inject,
        patch("haystack_enterprise_sdk.cli.cli_app"),
    ):
        run_packaged()
    inject.assert_called_once_with()
