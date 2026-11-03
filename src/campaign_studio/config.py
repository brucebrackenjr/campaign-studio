"""Predictable data locations and explicit local credentials, kept separate."""

import logging
import os
import sys
import tomllib
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from campaign_studio.local_files import has_link, local_root


class ConfigurationError(ValueError):
    pass


def distribution_directory() -> Path | None:
    """An installed Motif environment belongs to the source folder beside its .venv."""
    environment = Path(sys.prefix)
    launcher = Path(sys.argv[0]).absolute()
    if (
        launcher.name.casefold() in {"campaign-studio", "campaign-studio.exe"}
        and launcher.parent.name.casefold() in {"bin", "scripts"}
        and launcher.parent.parent.name == ".venv"
        and launcher.parent.parent != environment
    ):
        raise ConfigurationError(
            "This launcher points to another Motif environment. Recreate .venv in this "
            "installation folder with the README installation commands."
        )
    root = environment.parent
    if environment.name != ".venv" or not (root / "pyproject.toml").is_file():
        return None
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
    except (OSError, ValueError) as error:
        raise ConfigurationError(
            "Motif's installation files cannot be read. Extract the ZIP again."
        ) from error
    if (
        project.get("name") != "campaign-studio"
        or not (root / "src/campaign_studio/cli.py").is_file()
    ):
        return None
    if has_link(environment):
        raise ConfigurationError(
            "Extract Motif into a regular folder without symbolic links or junctions."
        )
    return root


def default_data_directory() -> tuple[Path, str]:
    root = distribution_directory()
    if root is None:
        raise ConfigurationError(
            "Motif could not identify its installation folder. Start with the extracted "
            "Motif folder's .venv, or supply --data-dir with an explicit workspace path."
        )
    return root / "Workspace", "Using the Workspace folder inside your Motif installation."


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
        distribution = distribution_directory()
        local_file = distribution / ".env" if distribution else None
        environment_key = "OPENAI_API_KEY" in os.environ
        local = {}
        if local_file is not None:
            if has_link(local_file):
                raise ConfigurationError(
                    "The local .env file must not be a symbolic link or junction."
                )
            if local_file.is_file():
                local = dotenv_values(local_file, interpolate=False)
        environment = {**local, **os.environ}
        values = {}
        for field, variable in (
            ("data_dir", "CAMPAIGN_STUDIO_DATA_DIR"),
            ("port", "CAMPAIGN_STUDIO_PORT"),
            ("storage_budget_bytes", "CAMPAIGN_STUDIO_STORAGE_BUDGET_BYTES"),
            ("disk_reserve_bytes", "CAMPAIGN_STUDIO_DISK_RESERVE_BYTES"),
            ("product_limit", "CAMPAIGN_STUDIO_PRODUCT_LIMIT"),
        ):
            if variable in environment:
                values[field] = environment[variable]
        if data_dir is not None:
            values["data_dir"] = data_dir
        if "data_dir" not in values:
            values["data_dir"], values["location_notice"] = default_data_directory()
        elif (
            data_dir is None and "CAMPAIGN_STUDIO_DATA_DIR" not in os.environ and values["data_dir"]
        ):
            values["data_dir"] = distribution / values["data_dir"]
        settings = cls.model_validate(values)
        try:
            workspace = local_root(settings.data_dir)
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        if os.name == "nt":
            local_key = workspace / "private/openai-key.dpapi"
        else:
            local_key = local_file or workspace / "private/.env"
        if not environment_key:
            supplied = local.get("OPENAI_API_KEY")
            if supplied and os.name == "nt":
                raise ConfigurationError(
                    "Remove OPENAI_API_KEY from Motif/.env on Windows. Enter it in Motif "
                    "for encrypted storage, or set the process environment variable."
                )
            if has_link(local_key):
                raise ConfigurationError(
                    "The credential path must not contain a symbolic link or junction."
                )
            if local_key.is_file():
                from campaign_studio.credentials import UnreadableProtectedKey, read_saved_key

                try:
                    saved = read_saved_key(local_key)
                except UnreadableProtectedKey:
                    saved = None
                    logging.getLogger(__name__).warning(
                        "Windows could not decrypt the saved key for this account. "
                        "Your campaigns remain available. Enter a key in Production to replace it."
                    )
                except (OSError, ValueError) as error:
                    raise ConfigurationError(
                        "The saved key could not be read. Check the private key file and account."
                    ) from error
                if saved:
                    os.environ["OPENAI_API_KEY"] = saved
        # Store source metadata only. The secret stays in the process environment.
        settings._credential_file = local_key
        settings._credential_environment = environment_key
        return settings
