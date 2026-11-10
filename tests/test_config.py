"""Configuration tests pass explicit mappings and never read credentials."""

import pytest
from pydantic import ValidationError

from northstar_codereview import config


def test_default_settings_need_no_provider_or_credentials():
    settings = config.load_settings({})
    assert settings.llm_provider is None
    assert settings.llm_model is None


def test_configuration_reads_only_the_explicit_provider_and_model():
    settings = config.load_settings({
        "NORTHSTAR_LLM_PROVIDER": " example-provider ",
        "NORTHSTAR_LLM_MODEL": " example-model ",
        "UNRELATED_SETTING": "ignored",
    })
    assert settings.llm_provider == "example-provider"
    assert settings.llm_model == "example-model"
    assert "UNRELATED_SETTING" not in settings.model_dump()


def test_blank_example_values_leave_provider_disabled():
    settings = config.load_settings({"NORTHSTAR_LLM_PROVIDER": " ", "NORTHSTAR_LLM_MODEL": ""})
    assert settings.llm_provider is None
    assert settings.llm_model is None


@pytest.mark.parametrize("environ", [
    {"NORTHSTAR_LLM_PROVIDER": "example-provider"},
    {"NORTHSTAR_LLM_MODEL": "example-model"},
])
def test_partial_provider_configuration_is_rejected(environ):
    with pytest.raises(ValidationError):
        config.load_settings(environ)


def test_environment_loading_is_explicit_and_does_not_mutate_environment(monkeypatch):
    monkeypatch.setenv("NORTHSTAR_LLM_PROVIDER", "example-provider")
    monkeypatch.setenv("NORTHSTAR_LLM_MODEL", "example-model")
    settings = config.load_settings()
    assert settings.llm_provider == "example-provider"
    assert config.load_settings({}).llm_provider is None


def test_settings_reject_credential_fields():
    with pytest.raises(ValidationError):
        config.Settings(api_key="synthetic-placeholder")
