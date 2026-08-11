from datetime import time

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: str = "./data/lunchshop.db"

    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_user: str = ""
    gmail_imap_password: str = ""
    menu_email_sender: str = ""

    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 587

    order_summary_recipient_email: str = ""
    order_summary_recipient_name: str = "Honza"
    order_summary_sender_name: str = "Lunchshop4you"
    order_summary_send_time: str = "11:35"

    google_service_account_json: str = "./secrets/service-account.json"
    google_sheets_spreadsheet_id: str = ""

    gemini_api_key: str = ""

    session_secret: str = "change-me"
    session_idle_timeout_hours: int = 24

    order_cutoff_time: str = "11:30"

    timezone: str = "Europe/Prague"

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))

    @property
    def order_cutoff(self) -> time:
        return self._parse_hhmm(self.order_cutoff_time)

    @property
    def order_summary_time(self) -> time:
        return self._parse_hhmm(self.order_summary_send_time)


settings = Settings()
