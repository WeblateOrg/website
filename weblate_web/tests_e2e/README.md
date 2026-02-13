# End-to-End Tests

This directory contains end-to-end (E2E) tests for the Weblate website using [Playwright](https://playwright.dev/).

## Overview

The E2E tests cover full user flows including:

- New user visiting the website
- Navigating through key pages (hosting, features, support, donate)
- Purchasing services/subscriptions

## Prerequisites

Before running the E2E tests, you need to:

1. Install all dependencies:

   ```bash
   uv pip install -r requirements-dev.txt
   ```

1. Install Playwright browsers:

   ```bash
   playwright install chromium
   ```

1. Set up the database and static files:

   ```bash
   ./manage.py migrate
   ./manage.py sync_packages
   ./manage.py collectstatic --noinput
   ```

1. Compile translation files (optional):

   ```bash
   ./scripts/generate-locales
   ```

## Running Tests

### Run all E2E tests

```bash
pytest weblate_web/tests_e2e/
```

### Run specific test file

```bash
pytest weblate_web/tests_e2e/test_navigation.py
```

### Run specific test

```bash
pytest weblate_web/tests_e2e/test_navigation.py::TestWebsiteNavigation::test_new_user_visits_homepage
```

### Run tests with visible browser (headed mode)

```bash
pytest weblate_web/tests_e2e/ --headed
```

### Run tests with different browsers

```bash
# Chromium (default)
pytest weblate_web/tests_e2e/ --browser chromium

# Firefox
pytest weblate_web/tests_e2e/ --browser firefox

# WebKit
pytest weblate_web/tests_e2e/ --browser webkit
```

### Run tests with video recording

```bash
pytest weblate_web/tests_e2e/ --video=on
```

### Run tests with screenshots on failure

```bash
pytest weblate_web/tests_e2e/ --screenshot=only-on-failure
```

## Test Structure

```
weblate_web/tests_e2e/
├── __init__.py
├── conftest.py           # Pytest fixtures and configuration
├── test_navigation.py    # Tests for basic website navigation
└── test_purchase.py      # Tests for purchasing services
```

## Configuration

The tests use the following configuration:

- **Headless mode**: Tests run in headless mode by default (can be changed with `--headed`)
- **Browser**: Chromium is used by default
- **Timeout**: 30 seconds default timeout for page operations
- **Database**: Tests use Django's test database with `pytest-django`

## CI/CD

E2E tests run automatically in CI on every push and pull request. See `.github/workflows/e2e.yml` for the CI configuration.

## Troubleshooting

### Tests fail with "SynchronousOnlyOperation" error

Make sure the `DJANGO_ALLOW_ASYNC_UNSAFE` environment variable is set:

```bash
export DJANGO_ALLOW_ASYNC_UNSAFE=true
pytest weblate_web/tests_e2e/
```

### Tests fail to connect to live server

The tests use Django's `live_server` fixture which starts a test server automatically. Make sure port 8000+ is available.

### Browser not installed

If you get an error about missing browser, run:

```bash
playwright install chromium
```

## Writing New Tests

When adding new E2E tests:

1. Create a new test file in `weblate_web/tests_e2e/` following the naming convention `test_*.py`
1. Use the `live_server` fixture to get the URL of the test server
1. Use the `page` fixture from pytest-playwright for browser interaction
1. Mark tests with `@pytest.mark.django_db` if they require database access
1. Follow the existing test structure and patterns

Example:

```python
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db


class TestMyFeature:
    def test_something(self, page: Page, live_server):
        page.goto(f"{live_server.url}/en/")
        # ... test code ...
```

## References

- [Playwright Documentation](https://playwright.dev/python/)
- [pytest-playwright Documentation](https://playwright.dev/python/docs/test-runners)
- [pytest-django Documentation](https://pytest-django.readthedocs.io/)
