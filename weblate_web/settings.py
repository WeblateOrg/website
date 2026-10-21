#
# Copyright © Michal Čihař <michal@weblate.org>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

#
# Django settings for weblate website project.
#

from __future__ import annotations

import os
from logging.handlers import SysLogHandler
from pathlib import Path

import django_stubs_ext

django_stubs_ext.monkeypatch()

DEBUG = True

ADMINS = ()

MANAGERS = ADMINS

DATABASES = {
    "default": {
        # Use 'postgresql_psycopg2', 'mysql', 'sqlite3' or 'oracle'.
        "ENGINE": "django.db.backends.sqlite3",
        # Database name or path to database file if using sqlite3.
        "NAME": "weblate-web.sqlite3",
        # Database user, not used with sqlite3.
        "USER": "",
        # Database password, not used with sqlite3.
        "PASSWORD": "",
        # Set to empty string for localhost. Not used with sqlite3.
        "HOST": "",
        # Set to empty string for default. Not used with sqlite3.
        "PORT": "",
    },
}

# Configure CI database
if (ci_database := os.environ.get("CI_DATABASE")) and ci_database != "sqlite":
    DATABASES["default"]["NAME"] = "weblate"
    DATABASES["default"]["PASSWORD"] = "weblate"  # ruff:ignore[hardcoded-password-string]
    DATABASES["default"]["HOST"] = "127.0.0.1"
    if ci_database == "mariadb":
        DATABASES["default"]["ENGINE"] = "django.db.backends.mysql"
        DATABASES["default"]["USER"] = "root"
    else:
        DATABASES["default"]["ENGINE"] = "django.db.backends.postgresql"
        DATABASES["default"]["USER"] = "postgres"


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Local time zone for this installation. Choices can be found here:
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# although not all choices may be available on all operating systems.
# In a Windows environment this must be set to your system time zone.
TIME_ZONE = "Europe/Prague"

# Language code for this installation. All choices can be found here:
# http://www.i18nguy.com/unicode/language-identifiers.html
LANGUAGE_CODE = "en-us"

LANGUAGES = (
    ("ar", "العربية"),
    ("az", "Azərbaycan"),
    ("ba", "башҡорт теле"),
    ("be-latn", "Biełaruskaja"),
    ("be", "Беларуская"),
    ("bg", "Български"),
    ("br", "Brezhoneg"),
    ("ca", "Català"),
    ("crh", "Qırımtatar tili"),
    ("cs", "Čeština"),
    ("cy", "Cymraeg"),
    ("da", "Dansk"),
    ("de", "Deutsch"),
    ("el", "Ελληνικά"),
    ("en", "English"),
    ("en-gb", "English (United Kingdom)"),
    ("es", "Español"),
    ("et", "Eesti"),
    ("fil", "Filipino"),
    ("fi", "Suomi"),
    ("fr", "Français"),
    ("fur", "Furlan"),
    ("ga", "Gaeilge"),
    ("gl", "Galego"),
    ("he", "עברית"),
    ("hr", "Hrvatski"),
    ("hu", "Magyar"),
    ("id", "Indonesia"),
    ("is", "Íslenska"),
    ("it", "Italiano"),
    ("ja", "日本語"),
    ("kab", "Taqbaylit"),
    ("kk", "Қазақ тілі"),
    ("ko", "한국어"),
    ("lo", "ລາວ"),
    ("lt", "Lietuvių"),
    ("lv", "Latviešu"),
    ("nb", "Norsk bokmål"),
    ("nl", "Nederlands"),
    ("nn", "Norsk nynorsk"),
    ("pl", "Polski"),
    ("pt-br", "Português brasileiro"),
    ("pt", "Português"),
    ("ro", "Română"),
    ("ru", "Русский"),
    ("sk", "Slovenčina"),
    ("sl", "Slovenščina"),
    ("sq", "Shqip"),
    ("sr-latn", "Srpski"),
    ("sr", "Српски"),
    ("sv", "Svenska"),
    ("ta", "தமிழ்"),
    ("th", "ไทย"),
    ("tr", "Türkçe"),
    ("ug", "ئۇيغۇرچە"),
    ("uk", "Українська"),
    ("vi", "Tiếng việt"),
    ("zh-hans", "简体中文"),
    ("zh-hant", "正體中文"),
)

SITE_ID = 1
SITE_URL = "http://localhost:1234"

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery.
USE_I18N = True

# If you set this to False, Django will not use timezone-aware datetimes.
USE_TZ = True

# Absolute filesystem path to the directory that will hold user-uploaded files.
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# CRM file storage
CRM_ROOT = os.path.join(BASE_DIR, "crm")

# URL that handles the media served from MEDIA_ROOT. Make sure to use a
# trailing slash.
# Examples: "http://media.lawrence.com/media/", "http://example.com/media/"
MEDIA_URL = "/media/"

# Absolute path to the directory static files should be collected to.
# Don't put anything in this directory yourself; store your static files
# in apps' "static/" subdirectories and in STATICFILES_DIRS.
STATIC_ROOT = os.path.join(BASE_DIR, "static")

# URL prefix for static files.
STATIC_URL = "/static/"

# Additional locations of static files
STATICFILES_DIRS = (
    # Put strings here, like "/home/html/static" or "C:/www/django/static".
    # Always use forward slashes, even on Windows.
    # Don't forget to use absolute paths, not relative paths.
)

# List of finder classes that know how to find static files in
# various locations.
STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
    "compressor.finders.CompressorFinder",
)

# Make this unique, and don't share it with anybody.
SECRET_KEY = "secret key used for tests only"  # ruff:ignore[hardcoded-password-string]

# Templates settings
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.messages.context_processors.messages",
                "weblate_web.context_processors.weblate_web",
            ],
        },
        "APP_DIRS": True,
    }
]

# Middleware
MIDDLEWARE = [
    "weblate_web.middleware.CSRFSecurityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "weblate_web.middleware.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "weblate_web.middleware.LocalizedDocumentationMiddleware",
    "social_django.middleware.SocialAuthExceptionMiddleware",
]

ROOT_URLCONF = "weblate_web.urls"

INSTALLED_APPS = (
    "weblate_web",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "django.contrib.messages",
    "weblate_web.admin_app.CustomAdminConfig",
    "django.contrib.humanize",
    "weblate_web.payments",
    "weblate_web.invoices",
    "weblate_web.legal",
    "weblate_web.crm",
    "weblate_fonts",
    "wllegal",
    "django_countries",
    "macros",
    "social_django",
    "compressor",
)

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "social_core.backends.weblate.WeblateOpenIdConnect",
)

# Some security headers
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# Optionally enable HSTS
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_PRELOAD = False
SECURE_HSTS_INCLUDE_SUBDOMAINS = False

# A sample logging configuration. The only tangible logging
# performed by this configuration is to send an email to
# the site admins on every HTTP 500 error when DEBUG=False.
# See http://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
DEFAULT_LOG = "console" if DEBUG else "syslog"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"require_debug_false": {"()": "django.utils.log.RequireDebugFalse"}},
    "formatters": {
        "syslog": {"format": "weblate[%(process)d]: %(levelname)s %(message)s"},
        "simple": {"format": "%(levelname)s %(message)s"},
    },
    "handlers": {
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "syslog": {
            "level": "DEBUG",
            "class": "logging.handlers.SysLogHandler",
            "formatter": "syslog",
            "address": "/dev/log",
            "facility": SysLogHandler.LOG_LOCAL2,
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["mail_admins"],
            "level": "ERROR",
            "propagate": True,
        },
    },
}

LOCALE_PATHS = (os.path.join(BASE_DIR, "locale"),)

ALLOWED_HOSTS = ("weblate.org", "127.0.0.1", "localhost")

EMAIL_SUBJECT_PREFIX = "[weblate.org] "

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 3600
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

PAYMENT_DEBUG = True

PAYMENT_FAKTURACE = (Path.home() / "weblate" / "tmp-fakturace").as_posix()
INVOICES_PATH = Path(BASE_DIR) / "invoices"
INVOICES_COPY_PATH: Path | None = None
AGREEMENTS_PATH = Path(BASE_DIR) / "agreements"
AGREEMENTS_COPY_PATH: Path | None = None
AGREEMENTS_SIGNATURE_PATH: Path | None = None

LOGIN_URL = "/oidc/"
if ci_database:
    LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/user/"

REGISTRATION_EMAIL_MATCH = ".*"

CHANGES_API = "https://hosted.weblate.org/api/"
CHANGES_KEY = ""

STORAGE_SSH_HOSTNAME = "u164666-sub5.your-storagebox.de"
STORAGE_SSH_PORT = 23
STORAGE_SSH_USER = "u164666-sub4"
STORAGE_BOX = 153391
HETZNER_API = ""

NOTIFY_SUBSCRIPTION: list[str] = []

FIO_TOKEN: str | list[str] = []

COMPRESS_OFFLINE = True
COMPRESS_OFFLINE_CONTEXT = [
    {"LANGUAGE_BIDI": True},
    {"LANGUAGE_BIDI": False},
]

# Keep the persistent sync namespace used by existing Hosted identities.
HOSTED_IDENTITY_PROVIDER = "https://hosted.weblate.org/idp/metadata"
HOSTED_USER_SYNC_API = "https://hosted.weblate.org/hosted/api/users/"
HOSTED_USER_CREATE_API = "https://hosted.weblate.org/hosted/api/users/ensure/"

# Hosted user IDs are shared by OIDC and account synchronization.
SOCIAL_AUTH_WEBLATE_OIDC_ENDPOINT = "https://hosted.weblate.org/oidc"
SOCIAL_AUTH_WEBLATE_KEY = ""
SOCIAL_AUTH_WEBLATE_SECRET = ""
SOCIAL_AUTH_RAISE_EXCEPTIONS = False
SOCIAL_AUTH_WEBLATE_PIPELINE = (
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "weblate_web.oidc.sync_hosted_user",
    "social_core.pipeline.social_auth.load_extra_data",
)

# Legacy The Pay
PAYMENT_THEPAY_MERCHANTID: str
PAYMENT_THEPAY_ACCOUNTID: str
PAYMENT_THEPAY_PASSWORD: str
PAYMENT_THEPAY_DATAAPI: str

# The Pay 2.0
THEPAY_MERCHANT_ID: str
THEPAY_PASSWORD: str
THEPAY_SERVER: str
THEPAY_PROJECT_ID: str

# API authentication
PAYMENT_SECRET: str
ZAMMAD_TOKEN: str

# IMAP
IMAP_SERVER: str
IMAP_USER: str
IMAP_PASSWORD: str
IMAP_SPAM_FOLDER: str

# django-countries disable sanctioned countries
COUNTRIES_OVERRIDE = {
    "IR": None,
    "KP": None,
    "RU": None,
    "BY": None,
}

LOCAL = Path(BASE_DIR) / "weblate_web" / "settings_local.py"
if LOCAL.exists():
    local_settings = LOCAL.read_text()
    # pylint: disable-next=exec-used
    exec(local_settings)  # ruff:ignore[exec-builtin]
