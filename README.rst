.. image:: https://s.weblate.org/cdn/Logo-Darktext-borders.png
   :alt: Weblate
   :target: https://weblate.org/
   :height: 80px

**Weblate is libre software web-based continuous localization system,
used by over 2500 libre projects and companies in more than 165 countries.**


Django based website for Weblate, running at <https://weblate.org/>.

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


If you are looking for Weblate itself, go to <https://github.com/WeblateOrg/weblate>.

Running locally
---------------

Create virtual env and install dependencies:

.. code-block:: sh

   uv venv .venv
   source .venv/bin/activate
   uv pip install -r requirements-dev.txt

Create ``weblate_web/settings_local.py`` which adjust your settings:

.. code-block:: py

   # Disable SAML login, use local
   LOGIN_URL = "/admin/login/"

   # You can also configure API keys and other things, see weblate_web/settings.py

Create admin:

.. code-block:: sh

   ./manage.py createsuperuser --username admin --email noreply@weblate.org

Migrate the database:

.. code-block:: sh

   ./manage.py migrate

Run the developemnt server:

.. code-block:: sh

   ./manage.py runserver
