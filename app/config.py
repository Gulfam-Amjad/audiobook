"""Process-level configuration, read from the environment and ``.env``.

Only values that must be known *before* the database is open belong here
(paths, bind address, credentials). Everything a user can change at runtime
lives in the ``settings`` table and is reached through
:mod:`app.services.settings_store`.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AVM_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_root: Path = Field(default=Path("D:/AIVideoManager"))
    host: str = "127.0.0.1"
    port: int = 8765
    ui_token: str = ""

    paid_enabled: bool = False
    monthly_spend_cap_usd: float = 0.0

    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    #: Blank means "look in the data root, then PATH". Set this only to
    #: point at a piper.exe installed somewhere else.
    piper_path: str = ""

    log_level: str = "INFO"

    # Script writing. Blank keeps the pipeline on the manual paste flow.
    gemini_api_key: str = ""

    # Borrowed GPU time. Both are free tiers; absent values disable them.
    kaggle_username: str = ""
    kaggle_key: str = ""
    #: Dataset holding pinned model weights, as ``owner/dataset-slug``.
    #: Without it every remote run re-downloads them from the allowance.
    kaggle_weights_dataset: str = ""
    hf_token: str = ""
    #: Spaces are chosen by you: this app cannot know a Space's arguments.
    hf_space_i2v: str = ""
    hf_space_lipsync: str = ""
    hf_space_api_name: str = "predict"

    # Free media libraries. Wikimedia Commons needs no key. Pixabay adds
    # stock video when a free key is set; blank skips it.
    pixabay_api_key: str = ""
    # NASA api.nasa.gov key. It raises the rate limit on NASA's own data
    # endpoints. The public image search at images-api.nasa.gov needs none.
    nasa_api_key: str = ""

    # Provider credentials. Absent values keep the adapter disabled.
    alibaba_api_key: str = ""
    alibaba_region: str = "singapore"
    byteplus_api_key: str = ""
    fal_api_key: str = ""
    replicate_api_token: str = ""
    minimax_api_key: str = ""
    minimax_group_id: str = ""
    pixverse_api_key: str = ""
    runway_api_key: str = ""
    luma_api_key: str = ""
    pika_api_key: str = ""
    vidu_api_key: str = ""
    kling_access_key: str = ""
    kling_secret_key: str = ""

    # --- Derived paths ----------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_root / "app.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def sync_db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_root / "thumbs"

    @property
    def output_dir(self) -> Path:
        return self.data_root / "output"

    @property
    def inbox_dir(self) -> Path:
        return self.data_root / "inbox"

    @property
    def assemblies_dir(self) -> Path:
        return self.data_root / "assemblies"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def diagnostics_dir(self) -> Path:
        return self.data_root / "diagnostics"

    @property
    def tmp_dir(self) -> Path:
        return self.data_root / "tmp"

    @property
    def voices_dir(self) -> Path:
        return self.data_root / "voices"

    @property
    def speech_dir(self) -> Path:
        return self.data_root / "speech"

    @property
    def tools_dir(self) -> Path:
        return self.data_root / "tools"

    @property
    def remote_dir(self) -> Path:
        """Notebooks sent out and files brought back, kept for inspection."""
        return self.data_root / "remote"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.thumbs_dir,
            self.output_dir,
            self.inbox_dir,
            self.assemblies_dir,
            self.logs_dir,
            self.diagnostics_dir,
            self.tmp_dir,
            self.voices_dir,
            self.speech_dir,
            self.tools_dir,
            self.remote_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def credential_for(self, provider_key: str) -> dict[str, str]:
        """Return the non-empty credential fields configured for a provider."""
        mapping: dict[str, dict[str, str]] = {
            "alibaba": {"api_key": self.alibaba_api_key, "region": self.alibaba_region},
            "byteplus": {"api_key": self.byteplus_api_key},
            "fal": {"api_key": self.fal_api_key},
            "replicate": {"api_token": self.replicate_api_token},
            "minimax": {"api_key": self.minimax_api_key, "group_id": self.minimax_group_id},
            "pixverse": {"api_key": self.pixverse_api_key},
            "runway": {"api_key": self.runway_api_key},
            "luma": {"api_key": self.luma_api_key},
            "pika": {"api_key": self.pika_api_key},
            "vidu": {"api_key": self.vidu_api_key},
            "kling": {"access_key": self.kling_access_key, "secret_key": self.kling_secret_key},
        }
        return {k: v for k, v in mapping.get(provider_key, {}).items() if v}


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
