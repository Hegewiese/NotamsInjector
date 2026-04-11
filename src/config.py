from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource


class Settings(BaseSettings):
    # SimConnect / MSFS
    simconnect_enabled: bool = True
    position_poll_interval_s: int = 30
    notam_radius_nm: float = 50.0
    min_move_nm: float = 5.0

    # NOTAM sources
    notams_online_enabled: bool = True
    checkwx_api_key: str = ""

    # Behaviour
    auto_apply_notams: bool = True
    notam_refresh_interval_min: int = 15
    max_notam_age_h: int = 24
    obstacle_placement_radius_nm: float = 10.0   # only place objects within this range

    # Logging
    log_level: str = "INFO"
    log_file: str = "notam_injector.log"

    model_config = SettingsConfigDict(yaml_file="config.yaml", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
        )


# Singleton — import and use settings anywhere
settings = Settings()
