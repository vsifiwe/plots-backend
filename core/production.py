import re

from django.core.exceptions import ImproperlyConfigured

from .settings import *


DEBUG = False
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a random secret of at least 50 characters.")
API_HOSTNAME = os.environ["API_HOSTNAME"]
if any(
    not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
    for label in API_HOSTNAME.split(".")
):
    raise ImproperlyConfigured("Set API_HOSTNAME to a hostname without a scheme or port.")
ALLOWED_HOSTS = [API_HOSTNAME]
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("FRONTEND_ORIGIN", "").split(",")
    if origin.strip()
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["DJANGO_DB_PATH"],
        "OPTIONS": {"timeout": 20},
    }
}

STATIC_URL = "/static/"
MIDDLEWARE = [MIDDLEWARE[0], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[1:]]

# Caddy connects to Gunicorn over the host's loopback interface and sets this header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
