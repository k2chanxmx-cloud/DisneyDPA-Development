import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

# このプロジェクト直下の .env を読み込む
load_dotenv(BASE_DIR / ".env")


APP_VERSION = "4.1.1"
APP_BUILD = "complete-ui-environment-ready"

APP_ENV = (
    os.getenv("APP_ENV", "development").strip().lower()
    or "development"
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY",
    "",
).strip()

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

YOSOCAL_URL = "https://yosocal.com/"
YOSOCAL_CACHE_SECONDS = 60 * 60 * 6
OFFICIAL_CACHE_SECONDS = 60 * 60 * 3
