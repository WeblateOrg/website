Weblate website
===============

.. image:: https://img.shields.io/badge/website-weblate.org-blue.svg
    :alt: Website
    :target: https://weblate.org/

.. image:: https://codecov.io/github/WeblateOrg/website/coverage.svg?branch=main
    :alt: Coverage Status
    :target: https://codecov.io/github/WeblateOrg/website?branch=main

.. image:: https://hosted.weblate.org/widget/weblate/website/status-badge.png
    :alt: Translation status
    :target: https://hosted.weblate.org/engage/weblate/

.. image:: https://img.shields.io/github/license/WeblateOrg/website.svg
    :alt: License
    :target: https://github.com/WeblateOrg/website/blob/main/LICENSE

.. image:: https://argos-ci.com/badge.svg
    :alt: Covered by Argos Visual Testing
    :target: https://app.argos-ci.com/weblate/website/reference?utm_source=weblate&utm_campaign=oss

Website and supporting services for `Weblate <https://weblate.org/>`_, built with Django.

.. image:: https://s.weblate.org/cdn/Logo-Darktext-borders.png
   :target: https://weblate.org/
   :alt: Weblate
   :height: 55px

Part of `Weblate <https://weblate.org/>`_ — a privacy-respecting localization platform built on open-source foundations.

Weblate
-------

Find Weblate at https://github.com/WeblateOrg/weblate.

Running locally
---------------

Create a virtual environment and install dependencies:

.. code-block:: sh

   uv sync --dev
   source .venv/bin/activate

Create ``weblate_web/settings_local.py`` to adjust your settings:

.. code-block:: py

   # Disable SAML login, use local
   LOGIN_URL = "/admin/login/"

   # Required signing key shared with the hosted infrastructure
   PAYMENT_SECRET = os.environ["PAYMENT_SECRET"]

   # You can also configure API keys and other things, see weblate_web/settings.py

   # Do not send real e-mails, log them to the console
   EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

Create admin:

.. code-block:: sh

   ./manage.py createsuperuser --username admin --email noreply@weblate.org

Migrate the database:

.. code-block:: sh

   ./manage.py migrate

Import subscription packages to the database:

.. code-block:: sh

   ./manage.py sync_packages

Compile MO files so that translations can be used:

.. code-block:: sh

   ./manage.py compilemessages

Run the development server:

.. code-block:: sh

   ./manage.py runserver

Testing Hosted Weblate OIDC login
--------------------------------

OIDC is available at ``/oidc/`` for manual testing. The normal login links and
admin login continue to use SAML. Visiting this page displays a button which
starts authentication using a CSRF-protected POST.

Install the released python-social-auth dependencies from the committed lockfile
and run the OIDC integration tests:

.. code-block:: sh

   uv sync --dev
   uv run pytest weblate_web/test_oidc.py

Both staging and production websites authenticate against Hosted Weblate.
Keep the committed ``SOCIAL_AUTH_WEBLATE_OIDC_ENDPOINT`` and
``HOSTED_SAML_PROVIDER`` defaults. SAML NameID, OIDC ``sub``, and account-sync
``external_id`` all contain the same Hosted Weblate user primary key.

On Hosted Weblate, enable the provider as described in Weblate's OIDC provider
documentation and register separate confidential clients for the staging and
production websites. Use authorization code flow and RSA with SHA-256. Register
each website's exact HTTPS callback URL ending in ``/oidc/complete/weblate/``;
the production callback is ``https://weblate.org/oidc/complete/weblate/``.
The Weblate social-auth backend enables S256 PKCE and requests the scopes
``openid profile email``.

Configure credentials in ``settings_local.py`` without committing secrets:

.. code-block:: py

   SOCIAL_AUTH_WEBLATE_KEY = os.environ["WEBLATE_OIDC_CLIENT_ID"]
   SOCIAL_AUTH_WEBLATE_SECRET = os.environ["WEBLATE_OIDC_CLIENT_SECRET"]

Run ``uv run ./manage.py migrate`` before starting the application to
create python-social-auth's tables. Existing ``SamlIdentity`` records remain
intact; each first successful OIDC login adds a social-auth association to the
same local user. New OIDC users receive both identity records atomically.
Email addresses and usernames never establish account associations. A conflict
rejects login without reassigning either identity.

The hosted account-sync payload already uses this stable identity, so push
updates, ``sync_hosted_users``, and CRM account linking retain their existing
format. OIDC users remain available to all three paths and SAML authentication.

Validate in staging first, then production: sign in with SAML, record the local
user ID and customer ownership, sign out, use ``/oidc/``, and confirm both are
unchanged. Repeat after hosted username/email changes and account synchronization,
and verify SAML login still reaches the same account. Also test a new OIDC user,
inactive users, and logout. Watch authentication errors for association conflicts;
investigate them rather than deleting or reassigning identities. If testing
fails, continue using SAML while retaining the OIDC tables and associations.
Switching the default login and removing SAML are separate follow-up changes.
