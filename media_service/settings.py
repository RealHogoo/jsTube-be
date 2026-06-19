import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

APP_ENV = os.environ.get("APP_ENV", os.environ.get("MEDIA_SERVICE_ENV", "local")).strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
DEBUG = os.environ.get("MEDIA_SERVICE_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get("MEDIA_SERVICE_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("MEDIA_SERVICE_SECRET_KEY is required in production")
    SECRET_KEY = "dev-media-secret"
if IS_PRODUCTION and SECRET_KEY == "dev-media-secret":
    raise RuntimeError("MEDIA_SERVICE_SECRET_KEY must not use the development default in production")
if IS_PRODUCTION and DEBUG:
    raise RuntimeError("MEDIA_SERVICE_DEBUG must be false in production")
if IS_PRODUCTION and not os.environ.get("WEBHARD_STORAGE_ROOT"):
    raise RuntimeError("WEBHARD_STORAGE_ROOT is required in production")
if IS_PRODUCTION and not (os.environ.get("ADMIN_INTERNAL_API_TOKEN") or os.environ.get("MEDIA_INTERNAL_API_TOKEN")):
    raise RuntimeError("ADMIN_INTERNAL_API_TOKEN is required in production")
if IS_PRODUCTION and not os.environ.get("ADMIN_SERVICE_BASE_URL"):
    raise RuntimeError("ADMIN_SERVICE_BASE_URL is required in production")
if IS_PRODUCTION and not os.environ.get("WEBHARD_PUBLIC_BASE_URL"):
    raise RuntimeError("WEBHARD_PUBLIC_BASE_URL is required in production")
if IS_PRODUCTION and not os.environ.get("WEBHARD_INTERNAL_BASE_URL"):
    raise RuntimeError("WEBHARD_INTERNAL_BASE_URL is required in production")
ALLOWED_HOSTS = [item.strip() for item in os.environ.get("MEDIA_SERVICE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if item.strip()]
if IS_PRODUCTION and any(item in {"*", "0.0.0.0"} for item in ALLOWED_HOSTS):
    raise RuntimeError("MEDIA_SERVICE_ALLOWED_HOSTS must not use wildcard hosts in production")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "media_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "media_api.middleware.SecurityHeaderMiddleware",
]

ROOT_URLCONF = "media_service.urls"
WSGI_APPLICATION = "media_service.wsgi.application"
ASGI_APPLICATION = "media_service.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "media-service.sqlite3",
    }
}

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_CONFIG = {
    "ADMIN_SERVICE_BASE_URL": os.environ.get("ADMIN_SERVICE_BASE_URL", "http://localhost:8081").rstrip("/"),
    "WEBHARD_PUBLIC_BASE_URL": os.environ.get("WEBHARD_PUBLIC_BASE_URL", "http://localhost:8083").rstrip("/"),
    "WEBHARD_INTERNAL_BASE_URL": os.environ.get("WEBHARD_INTERNAL_BASE_URL", os.environ.get("WEBHARD_PUBLIC_BASE_URL", "http://localhost:8083")).rstrip("/"),
    "ADMIN_INTERNAL_API_TOKEN": os.environ.get("ADMIN_INTERNAL_API_TOKEN", os.environ.get("MEDIA_INTERNAL_API_TOKEN", "" if IS_PRODUCTION else "dev-media-internal-token")),
    "MEDIA_INTERNAL_API_TOKEN": os.environ.get("MEDIA_INTERNAL_API_TOKEN", "" if IS_PRODUCTION else "dev-media-internal-token"),
    "WEBHARD_STORAGE_ROOT": os.environ.get("WEBHARD_STORAGE_ROOT", str(BASE_DIR.parent / "webhard-service" / "storage")),
    "MEDIA_MONGO_URI": os.environ.get("MEDIA_MONGO_URI", "mongodb://localhost:27017"),
    "MEDIA_MONGO_DATABASE": os.environ.get("MEDIA_MONGO_DATABASE", "media_service"),
    "MEDIA_SYNC_LIMIT": int(os.environ.get("MEDIA_SYNC_LIMIT", "500")),
    "AUTH_CACHE_SECONDS": float(os.environ.get("MEDIA_AUTH_CACHE_SECONDS", "5")),
    "SERVICE_STATUS_CACHE_SECONDS": float(os.environ.get("MEDIA_SERVICE_STATUS_CACHE_SECONDS", os.environ.get("MEDIA_AUTH_CACHE_SECONDS", "5"))),
    "FILE_TOKEN_SECONDS": int(os.environ.get("MEDIA_FILE_TOKEN_SECONDS", "7200")),
    "YOUTUBE_YTDLP_PATH": os.environ.get("YOUTUBE_YTDLP_PATH", ""),
    "YOUTUBE_FFMPEG_PATH": os.environ.get("YOUTUBE_FFMPEG_PATH", ""),
    "YOUTUBE_AUTO_INSTALL_FFMPEG": os.environ.get("YOUTUBE_AUTO_INSTALL_FFMPEG", "false" if IS_PRODUCTION else "true").lower() == "true",
    "YOUTUBE_TOOL_DIR": os.environ.get("YOUTUBE_TOOL_DIR", str(BASE_DIR / ".runtime" / "tools")),
    "YOUTUBE_IMPORT_LIMIT": int(os.environ.get("YOUTUBE_IMPORT_LIMIT", "100")),
    "YOUTUBE_IMPORT_MAX_ITEMS": int(os.environ.get("YOUTUBE_IMPORT_MAX_ITEMS", "100")),
}
if IS_PRODUCTION and MEDIA_CONFIG["ADMIN_INTERNAL_API_TOKEN"] == "dev-media-internal-token":
    raise RuntimeError("ADMIN_INTERNAL_API_TOKEN must not use the development default in production")
if IS_PRODUCTION:
    for name in ("WEBHARD_PUBLIC_BASE_URL",):
        value = MEDIA_CONFIG[name]
        if "://localhost" in value or "://127.0.0.1" in value:
            raise RuntimeError(f"{name} must not use localhost in production")

DEFAULT_CORS_ORIGINS = "http://localhost:8084,http://localhost:8085,http://127.0.0.1:8084,http://127.0.0.1:8085"
CORS_ORIGINS = [
    item.strip()
    for item in os.environ.get("MEDIA_SERVICE_CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
    if item.strip()
]
