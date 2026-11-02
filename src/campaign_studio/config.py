"""Predictable data locations and explicit local credentials, kept separate."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


class ConfigurationError(ValueError):
    pass


def platform_data_directory() -> Path:
    """New installations do not depend on the shell's current directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Motif"
    if sys.platform.startswith("linux"):
        configured = os.environ.get("XDG_DATA_HOME")
        if configured and not Path(configured).is_absolute():
            raise ConfigurationError("XDG_DATA_HOME must be an absolute directory.")
        return (Path(configured) if configured else Path.home() / ".local" / "share") / "motif"
    raise ConfigurationError(
        "Motif – Campaign Studio supports macOS and Linux. Native Windows is unsupported."
    )


def default_data_directory() -> tuple[Path, str]:
    return platform_data_directory(), "Using the standard per-user data location."


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    _credential_file: Path | None = PrivateAttr(default=None)
    _credential_environment: bool = PrivateAttr(default=False)

    data_dir: Path = Field(default_factory=lambda: default_data_directory()[0])
    location_notice: str = "Using the explicitly configured data location."
    storage_budget_bytes: int = Field(default=10_000_000_000, ge=1)
    disk_reserve_bytes: int = Field(default=1_000_000_000, ge=0)
    port: int = Field(default=8000, ge=1024, le=65535)
    product_limit: int = Field(default=5, ge=1, le=5)

    @field_validator("data_dir", mode="before")
    @classmethod
    def require_directory_value(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("storage directory must not be empty")
        return value

    @classmethod
    def from_environment(cls, *, data_dir: Path | None = None) -> Settings:
        local_file = Path.cwd() / ".env"
        environment_key = "OPENAI_API_KEY" in os.environ
        if local_file.is_symlink():
            raise ConfigurationError("The local .env file must not be a symbolic link.")
        if local_file.is_file():
            load_dotenv(local_file, override=False, interpolate=False)
        values = {}
        for field, variable in (
            ("data_dir", "CAMPAIGN_STUDIO_DATA_DIR"),
            ("port", "CAMPAIGN_STUDIO_PORT"),
            ("storage_budget_bytes", "CAMPAIGN_STUDIO_STORAGE_BUDGET_BYTES"),
            ("disk_reserve_bytes", "CAMPAIGN_STUDIO_DISK_RESERVE_BYTES"),
            ("product_limit", "CAMPAIGN_STUDIO_PRODUCT_LIMIT"),
        ):
            if variable in os.environ:
                values[field] = os.environ[variable]
        if data_dir is not None:
            values["data_dir"] = data_dir
        if "data_dir" not in values:
            values["data_dir"], values["location_notice"] = default_data_directory()
        settings = cls.model_validate(values)
        # Store source metadata only. The secret stays in the process environment.
        settings._credential_file = local_file
        settings._credential_environment = environment_key
        return settings
