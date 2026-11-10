"""Explicit, credential-free configuration; importing does not read the environment."""

import os
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    """Optional provider selection; creating settings never contacts a provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    llm_provider: str | None = Field(default=None, max_length=100, description="Future provider integration identifier.")
    llm_model: str | None = Field(default=None, max_length=200, description="Future model identifier, supplied by the user.")

    @field_validator("llm_provider", "llm_model", mode="before")
    @classmethod
    def normalize_optional_value(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_provider_and_model_together(self) -> Self:
        if (self.llm_provider is None) != (self.llm_model is None):
            raise ValueError("Configure both NORTHSTAR_LLM_PROVIDER and NORTHSTAR_LLM_MODEL, or neither")
        return self


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read only two named settings; never load dotenv files or mutate variables."""
    values = os.environ if environ is None else environ
    return Settings(
        llm_provider=values.get("NORTHSTAR_LLM_PROVIDER"),
        llm_model=values.get("NORTHSTAR_LLM_MODEL"),
    )
