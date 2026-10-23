from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch

from haystack_enterprise_sdk._console import _CI_MARKERS, _PlainStatus, animations_enabled, in_ci, status_spinner


class TestAnimationsEnabled:
    @pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes"])
    def test_disabled_when_ci_is_set(self, monkeypatch: MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CI", value)
        assert animations_enabled() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "   "])
    def test_enabled_when_ci_is_unset_or_off(self, monkeypatch: MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CI", value)
        assert animations_enabled() is True

    def test_enabled_without_ci(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        assert animations_enabled() is True

    def test_status_spinner_is_plain_in_ci(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        with status_spinner() as spinner:
            assert isinstance(spinner, _PlainStatus)


class TestInCi:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: MonkeyPatch) -> None:
        for name in ("CI", *_CI_MARKERS):
            monkeypatch.delenv(name, raising=False)

    def test_false_without_any_marker(self) -> None:
        assert in_ci() is False

    @pytest.mark.parametrize("name", _CI_MARKERS)
    def test_vendor_marker_without_ci(self, monkeypatch: MonkeyPatch, name: str) -> None:
        monkeypatch.setenv(name, "x")
        assert in_ci() is True

    def test_ci_false_overrides_vendor_marker(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("JENKINS_URL", "https://jenkins.example")
        monkeypatch.setenv("CI", "false")
        assert in_ci() is False


class TestPlainStatus:
    def test_prints_each_distinct_status_once(self, capsys: Any) -> None:
        with _PlainStatus() as status:
            status.text = "Deploying."
            status.text = "Rolling out (STARTING)."
            status.text = "Rolling out (STARTING)."  # the poller repeats itself; the log must not
            status.text = "Rolling out (DEPLOYED)."

        assert capsys.readouterr().err.splitlines() == [
            "Deploying.",
            "Rolling out (STARTING).",
            "Rolling out (DEPLOYED).",
        ]

    def test_prints_nothing_to_stdout(self, capsys: Any) -> None:
        # `run` writes its JSON payload to stdout; progress chatter must stay out of it.
        with _PlainStatus() as status:
            status.text = "Working."

        assert capsys.readouterr().out == ""

    def test_hidden_is_a_no_op(self) -> None:
        with _PlainStatus() as status, status.hidden():
            status.text = "Still fine."
        assert status.text == "Still fine."
