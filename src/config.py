from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource


class Settings(BaseSettings):
    # SimConnect / MSFS
    simconnect_enabled: bool = True
    position_poll_interval_s: int = 5
    notam_radius_nm: float = 50.0
    min_move_nm: float = 5.0

    # NOTAM sources
    notams_online_enabled: bool = True
    checkwx_api_key: str = ""

    # Behaviour
    auto_apply_notams: bool = True
    notam_refresh_interval_min: int = 15
    notam_cache_ttl_min: int = 60
    max_notam_age_h: int = 24
    obstacle_placement_radius_nm: float = 10.0   # only place objects within this range
    notam_movement_refetch_cooldown_s: int = 120  # skip refetch if same airport set was fetched recently
    highlight_obstacle_objects: bool = True
    highlight_beacon_base_ft: float = 1500.0   # MSL alt of lowest beacon in column
    highlight_beacon_step_ft: float = 500.0    # vertical spacing between beacons
    highlight_beacon_count: int    = 6         # number of stacked beacons
    notam_alert_enabled: bool = True           # show in-sim NOTAM popups via SimConnect_Text
    notam_alert_radius_nm: float = 20.0        # show popup when within this range of a NOTAM
    alert_window_opacity: float = 0.7          # 0.7 = 30% transparent for NOTAM alert overlay
    msfs_status_dialog_enabled: bool = True    # show startup MSFS status dialog until valid position

    # WASM integration
    wasm_state_file: str = "navaid_overrides.json"   # WASM module reads this for override state

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
