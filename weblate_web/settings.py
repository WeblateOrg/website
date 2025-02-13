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

import saml2.saml

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
    ("be", "Беларуская"),
    ("be-latn", "Biełaruskaja"),
    ("bg", "Български"),
    ("br", "Brezhoneg"),
    ("ca", "Català"),
    ("cs", "Čeština"),
    ("da", "Dansk"),
    ("de", "Deutsch"),
    ("en", "English"),
    ("el", "Ελληνικά"),
    ("en-gb", "English (United Kingdom)"),
    ("es", "Español"),
    ("fi", "Suomi"),
    ("fr", "Français"),
    ("fur", "Furlan"),
    ("gl", "Galego"),
    ("he", "עברית"),
    ("hu", "Magyar"),
    ("hr", "Hrvatski"),
    ("id", "Indonesia"),
    ("is", "Íslenska"),
    ("it", "Italiano"),
    ("ja", "日本語"),
    ("kab", "Taqbaylit"),
    ("kk", "Қазақ тілі"),
    ("ko", "한국어"),
    ("nb", "Norsk bokmål"),
    ("nl", "Nederlands"),
    ("pl", "Polski"),
    ("pt", "Português"),
    ("pt-br", "Português brasileiro"),
    ("ro", "Română"),
    ("ru", "Русский"),
    ("sk", "Slovenčina"),
    ("sl", "Slovenščina"),
    ("sq", "Shqip"),
    ("sr", "Српски"),
    ("sr-latn", "Srpski"),
    ("sv", "Svenska"),
    ("tr", "Türkçe"),
    ("uk", "Українська"),
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
SECRET_KEY = "secret key used for tests only"  # noqa: S105

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
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "weblate_web.middleware.SecurityMiddleware",
    "djangosaml2.middleware.SamlSessionMiddleware",
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
    "wllegal",
    "django_countries",
    "macros",
    "djangosaml2",
    "compressor",
)

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "djangosaml2.backends.Saml2Backend",
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
        "djangosaml2": {
            "handlers": [DEFAULT_LOG],
            "level": "DEBUG",
        },
        "saml2": {
            "handlers": [DEFAULT_LOG],
            "level": "DEBUG",
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

LOGIN_URL = "/saml2/login/"
LOGIN_REDIRECT_URL = "/user/"

REGISTRATION_EMAIL_MATCH = ".*"

CHANGES_API = "https://hosted.weblate.org/api/"
CHANGES_KEY = ""

STORAGE_SSH_HOSTNAME = "u164666-sub5.your-storagebox.de"
STORAGE_SSH_PORT = 23
STORAGE_SSH_USER = "u164666-sub4"
STORAGE_BOX = 153391
STORAGE_USER = ""
STORAGE_PASSWORD = ""

NOTIFY_SUBSCRIPTION: list[str] = []

FIO_TOKEN: str | list[str] = []

COMPRESS_OFFLINE = True
COMPRESS_OFFLINE_CONTEXT = [
    {"LANGUAGE_BIDI": True},
    {"LANGUAGE_BIDI": False},
]

SAML_CSP_HANDLER = ""
SAML_ATTRIBUTE_MAPPING = {
    "username": ("username",),
    "email": ("email",),
    "last_name": ("last_name",),
}
SAML_DJANGO_USER_MAIN_ATTRIBUTE = "email"

SAML_CONFIG = {
    # full path to the xmlsec1 binary program
    "xmlsec_binary": "/usr/bin/xmlsec1",
    # your entity id, usually your subdomain plus the url to the metadata view
    "entityid": "http://localhost:1234/saml2/metadata/",
    # this block states what services we provide
    "service": {
        # we are just a lonely SP
        "sp": {
            "name": "Weblate.org",
            "name_id_format": saml2.saml.NAMEID_FORMAT_EMAILADDRESS,
            # For Okta add signed logout requests. Enable this:
            "endpoints": {
                # url and binding to the assetion consumer service view
                # do not change the binding or service name
                "assertion_consumer_service": [
                    ("http://localhost:1234/saml2/acs/", saml2.BINDING_HTTP_POST),
                ],
                # url and binding to the single logout service view
                # do not change the binding or service name
                "single_logout_service": [
                    # Disable next two lines for HTTP_REDIRECT for IDP's that only support HTTP_POST. Ex. Okta:
                    ("http://localhost:1234/saml2/ls/", saml2.BINDING_HTTP_REDIRECT),
                    ("http://localhost:1234/saml2/ls/post", saml2.BINDING_HTTP_POST),
                ],
            },
            # Mandates that the identity provider MUST authenticate the
            # presenter directly rather than rely on a previous security context.
            "force_authn": False,
            # Require signing
            "want_assertions_signed": True,
            "want_response_signed": True,
            "want_assertions_or_response_signed": True,
            "logout_requests_signed": True,
            # Enable AllowCreate in NameIDPolicy.
            "name_id_format_allow_create": False,
            # attributes that this project need to identify a user
            "required_attributes": ["email", "username", "last_name"],
        },
    },
    # where the remote metadata is stored, local, remote or mdq server.
    # One metadatastore or many ...
    "metadata": {
        # Needs periodic updates of the file, for example:
        # curl -sSf  https://hosted.weblate.org/idp/metadata/ > .../saml/remote_metadata.xml
        "local": [os.path.join(BASE_DIR, "saml", "remote_metadata.xml")],
        "remote": [{"url": "https://hosted.weblate.org/idp/metadata/"}],
    },
    # set to 1 to output debugging information
    "debug": 0,
    # Signing
    "key_file": os.path.join(BASE_DIR, "saml", "saml.key"),  # private part
    "cert_file": os.path.join(BASE_DIR, "saml", "saml.crt"),  # public part
    "attribute_map_dir": os.path.join(BASE_DIR, "saml", "attribute-maps"),
    # Encryption
    "encryption_keypairs": [
        {
            "key_file": os.path.join(BASE_DIR, "saml", "saml.key"),  # private part
            "cert_file": os.path.join(BASE_DIR, "saml", "saml.crt"),  # public part
        }
    ],
    # own metadata settings
    "contact_person": [
        {
            "given_name": "Michal",
            "sur_name": "Čihař",
            "company": "Weblate",
            "email_address": "michal@weblate.org",
            "contact_type": "technical",
        },
        {
            "given_name": "Michal",
            "sur_name": "Čihař",
            "company": "Weblate",
            "email_address": "michal@weblate.org",
            "contact_type": "administrative",
        },
    ],
    # you can set multilanguage information here
    "organization": {
        "name": [("Weblate", "en")],
        "display_name": [("Weblate", "es")],
        "url": [("https://weblate.org/", "en")],
    },
}

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
    "SY": None,
    "BY": None,
}

LOCAL = Path(BASE_DIR) / "weblate_web" / "settings_local.py"
if LOCAL.exists():
    local_settings = LOCAL.read_text()
    exec(local_settings)  # noqa: S102
