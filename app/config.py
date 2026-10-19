from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_gov_in_api_key: str = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571"
    data_gov_in_resource_id: str = "9ef84268-d588-465a-a308-a864a43d0070"
    osrm_base_url: str = "http://router.project-osrm.org"
    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    default_diesel_price: float = 92.0
    request_timeout_seconds: float = 12.0
    # data.gov.in is often slow (10-30 s per call), so it gets a longer timeout.
    data_gov_in_timeout_seconds: float = 25.0


settings = Settings()
