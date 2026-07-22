from unittest.mock import Mock

import httpx
import pytest
from httpx import codes

from haystack_enterprise_sdk._api.config import CommonConfig, normalize_base_url
from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    WorkspaceNotDefinedError,
)


@pytest.mark.asyncio
class TestUtilitiesForHaystackEnterpriseAPI:
    async def test_haystack_enterprise_api_factory(self, unit_config: CommonConfig) -> None:
        async with HaystackEnterpriseAPI.factory(unit_config) as haystack_enterprise_api:
            assert (
                haystack_enterprise_api.base_url("test_workspace")
                == "https://fake.dc.api/api/v1/workspaces/test_workspace"
            )
            assert haystack_enterprise_api.headers == {
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            }

    async def test_haystack_enterprise_api_raises_exception_if_no_workspace_is_defined(
        self, haystack_enterprise_api: HaystackEnterpriseAPI
    ) -> None:
        with pytest.raises(WorkspaceNotDefinedError):
            await haystack_enterprise_api.get("", "endpoint")


class TestCommonConfig:
    def test_common_config_raises_exception_if_no_api_key_is_defined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear environment variables to ensure they don't interfere with the test
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("API_URL", raising=False)
        monkeypatch.delenv("DEFAULT_WORKSPACE_NAME", raising=False)

        # api key is not explicitly provided nor via env
        with pytest.raises(ValueError):
            CommonConfig(api_key="", api_url="https://fake.dc.api")

    def test_common_config_uses_default_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear environment variables to ensure they don't interfere with the test
        monkeypatch.delenv("API_URL", raising=False)

        # api url is not explicitly provided nor via env
        c = CommonConfig(api_key="something")
        assert c.api_url == "https://api.cloud.deepset.ai"

    def test_common_config_works_with_explicit_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_load_env = Mock(return_value=True)
        monkeypatch.setattr("haystack_enterprise_sdk._api.config.load_environment", mock_load_env)

        # When all parameters are provided explicitly, should not call load_environment
        config = CommonConfig(api_key="explicit-key", api_url="https://explicit.api")

        assert config.api_key == "explicit-key"
        assert config.api_url == "https://explicit.api"

        mock_load_env.assert_not_called()

    def test_common_config_uses_env_vars_when_not_explicitly_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Set environment variables
        monkeypatch.setenv("API_KEY", "env-api-key")
        monkeypatch.setenv("API_URL", "https://env.api.url")
        monkeypatch.setenv("DEFAULT_WORKSPACE_NAME", "env-workspace")

        mock_load_env = Mock(return_value=True)
        monkeypatch.setattr("haystack_enterprise_sdk._api.config.load_environment", mock_load_env)

        # When no explicit parameters are provided, should use environment variables
        config = CommonConfig()

        assert config.api_key == "env-api-key"
        assert config.api_url == "https://env.api.url"
        # Should have called load_environment with warnings enabled
        mock_load_env.assert_called_once_with(show_warnings=True)

    def test_common_config_uses_env_vars_when_partially_provided_explicit_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_KEY", "env-api-key")
        monkeypatch.setenv("API_URL", "https://env.api.url")
        monkeypatch.setenv("DEFAULT_WORKSPACE_NAME", "env-workspace")

        mock_load_env = Mock(return_value=True)
        monkeypatch.setattr("haystack_enterprise_sdk._api.config.load_environment", mock_load_env)

        # When only api_key is provided explicitly, should use env var for api_url
        config = CommonConfig(api_key="explicit-key")

        assert config.api_key == "explicit-key"  # explicit value used
        assert config.api_url == "https://env.api.url"  # env var used
        # Should have called load_environment because api_url was not provided
        mock_load_env.assert_called_once_with(show_warnings=True)

    def test_common_config_uses_env_vars_when_partially_provided_explicit_api_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set environment variables
        monkeypatch.setenv("API_KEY", "env-api-key")
        monkeypatch.setenv("API_URL", "https://env.api.url")
        monkeypatch.setenv("DEFAULT_WORKSPACE_NAME", "env-workspace")

        mock_load_env = Mock(return_value=True)
        monkeypatch.setattr("haystack_enterprise_sdk._api.config.load_environment", mock_load_env)

        # When only api_url is provided explicitly, should use env var for api_key
        config = CommonConfig(api_url="https://explicit.api")

        assert config.api_key == "env-api-key"  # env var used
        assert config.api_url == "https://explicit.api"  # explicit value used
        # Should have called load_environment because api_key was not provided
        mock_load_env.assert_called_once_with(show_warnings=True)

    def test_common_config_removes_last_backslash(self) -> None:
        assert CommonConfig(api_key="your-key", api_url="https://fake.dc.api/").api_url == "https://fake.dc.api"

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("https://fake.dc.api", "https://fake.dc.api"),
            ("https://fake.dc.api/", "https://fake.dc.api"),
            ("https://fake.dc.api/api/v1", "https://fake.dc.api"),
            ("https://fake.dc.api/api/v1/", "https://fake.dc.api"),
            ("https://fake.dc.api/v1", "https://fake.dc.api"),
            ("https://fake.dc.api/v2", "https://fake.dc.api"),
            ("https://fake.dc.api/API/V1", "https://fake.dc.api"),
        ],
    )
    def test_common_config_normalizes_api_url(self, given: str, expected: str) -> None:
        assert CommonConfig(api_key="your-key", api_url=given).api_url == expected


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize(
        "given, expected",
        [
            ("https://fake.dc.api", "https://fake.dc.api"),
            ("https://fake.dc.api/", "https://fake.dc.api"),
            ("https://fake.dc.api/api/v1", "https://fake.dc.api"),
            ("https://fake.dc.api/api/v1/", "https://fake.dc.api"),
            ("https://fake.dc.api/v1", "https://fake.dc.api"),
            ("https://fake.dc.api/v2", "https://fake.dc.api"),
            ("https://fake.dc.api/API/V1", "https://fake.dc.api"),
        ],
    )
    def test_normalize_base_url(self, given: str, expected: str) -> None:
        assert normalize_base_url(given) == expected


@pytest.mark.asyncio
class TestCRUDForHaystackEnterpriseAPI:
    async def test_get(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.get.return_value = httpx.Response(status_code=codes.OK, json={"test": "test"})

        result = await haystack_enterprise_api.get(
            "default", "endpoint", params={"param_key": "param_value"}, timeout_s=123
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        mocked_client.get.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_get_retry(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.get.side_effect = [
            httpx.ReadTimeout(message="read timeout"),
            httpx.RequestError(message="read error"),
            httpx.Response(status_code=codes.OK, json={"test": "test"}),
        ]

        result = await haystack_enterprise_api.get(
            "default", "endpoint", params={"param_key": "param_value"}, timeout_s=123
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        assert mocked_client.get.call_count == 3

        mocked_client.get.assert_called_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_get_with_not_covered_retry_exception(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        class CustomException(Exception):
            pass

        mocked_client.get.side_effect = [
            CustomException(),
        ]
        with pytest.raises(CustomException):
            await haystack_enterprise_api.get("default", "endpoint", params={"param_key": "param_value"}, timeout_s=123)

    async def test_get_retry_with_exception(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.get.side_effect = [
            httpx.ReadTimeout(message="read timeout"),
            httpx.RequestError(message="read error"),
            httpx.RequestError(message="read error"),
        ]
        with pytest.raises(httpx.RequestError):
            await haystack_enterprise_api.get("default", "endpoint", params={"param_key": "param_value"}, timeout_s=123)

    async def test_post(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.post.return_value = httpx.Response(status_code=codes.OK, json={"test": "test"})

        result = await haystack_enterprise_api.post(
            "default",
            "endpoint",
            params={"param_key": "param_value"},
            json={"data_key": "data_value"},
            data={"raw": "data_sent_as_form_data"},
            files={"file": ("my_file", "fake-file-binary", "text/csv")},
            timeout_s=123,
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        mocked_client.post.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            json={"data_key": "data_value"},
            data={"raw": "data_sent_as_form_data"},
            files={"file": ("my_file", "fake-file-binary", "text/csv")},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_delete(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.delete.return_value = httpx.Response(status_code=codes.OK, json={"test": "test"})

        result = await haystack_enterprise_api.delete(
            "default", "endpoint", params={"param_key": "param_value"}, timeout_s=123
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        mocked_client.delete.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_put(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.put.return_value = httpx.Response(status_code=codes.OK, json={"test": "test"})

        result = await haystack_enterprise_api.put(
            "default",
            "endpoint",
            params={"param_key": "param_value"},
            data={"data_key": "data_value"},
            timeout_s=123,
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        mocked_client.put.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            json={"data_key": "data_value"},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_patch(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.patch.return_value = httpx.Response(status_code=codes.OK, json={"test": "test"})

        result = await haystack_enterprise_api.patch(
            "default",
            "endpoint",
            params={"param_key": "param_value"},
            json={"data_key": "data_value"},
            timeout_s=123,
        )
        assert result.status_code == codes.OK
        assert result.json() == {"test": "test"}
        mocked_client.patch.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            json={"data_key": "data_value"},
            data=None,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )

    async def test_patch_with_data_parameter(
        self, haystack_enterprise_api: HaystackEnterpriseAPI, unit_config: CommonConfig, mocked_client: Mock
    ) -> None:
        mocked_client.patch.return_value = httpx.Response(status_code=codes.NO_CONTENT)

        result = await haystack_enterprise_api.patch(
            "default",
            "endpoint",
            params={"param_key": "param_value"},
            data={"data_key": "data_value"},
            timeout_s=123,
        )
        assert result.status_code == codes.NO_CONTENT
        mocked_client.patch.assert_called_once_with(
            "https://fake.dc.api/api/v1/workspaces/default/endpoint",
            params={"param_key": "param_value"},
            json=None,
            data={"data_key": "data_value"},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {unit_config.api_key}",
                "X-Client-Source": "haystack-enterprise-sdk",
            },
            timeout=123,
        )
