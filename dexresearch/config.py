"""Configuration loader.

Runtime config comes from environment variables (loaded from .env).
Study parameters (protocols, tokens, sampling) come from config/study.yaml.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DUNE_API_KEY: str
    GCP_PROJECT_ID: str
    GCS_BUCKET: str
    BQ_DATASET: str = "raw"
    DATA_DIR: Path = PROJECT_ROOT / "data"
    STUDY_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "study.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_study() -> dict[str, Any]:
    with open(get_settings().STUDY_CONFIG_PATH) as f:
        return yaml.safe_load(f)
