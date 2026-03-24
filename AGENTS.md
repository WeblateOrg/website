# Agents guidance for Weblate website

This file captures agent-specific guidance for working in the Weblate website codebase.

## Project overview

- This is a Django-based website.
- The primary stack is Python, Django, JavaScript, and HTML/CSS.
- Uses vaniall JavaScript without third-party dependencies.

## Code expectations

- Follow existing Django patterns and project conventions.
- Prefer the repository's configured Ruff-based formatting and linting rules.
- Prefer type hints and use `from __future__ import annotations` in Python
  modules.
- Use `TYPE_CHECKING` imports for type-only dependencies when that avoids
  runtime import cycles.
- All user-facing strings must be translatable using Django i18n helpers.
- In templates, use `{% translate %}` / `{% blocktranslate %}` for translatable
  text.
- Preserve accessibility and the existing Bootstrap/jQuery-based frontend
  patterns.
- Write commit messages using the Conventional Commits format
  `<type>(<optional scope>): <description>`. Common types include `feat`,
  `fix`, `docs`, `refactor`, `test`, `ci`, and `chore`. Example:
  `fix(translations): handle empty component slug`.
- Include the GPL-3.0-or-later license header in new Python files.

## Testing and linting instructions

- Install the development dependencies first using
  `uv venv .venv; uv pip install -r requirements-dev.txt`.
- After syncing, you can also activate it with `source .venv/bin/activate` or invoke tools from `.venv/bin/`.
- Prefer `prek run --all-files` as the primary linting/formatting command because
  it runs the repository's configured pre-commit framework checks.
- `prek` is a third-party reimplementation of the `pre-commit` tool.
- Use `pytest` to run the test suite: `pytest weblate_web`.
- Use `pylint` to lint the Python code: `pylint weblate_web/`
- Use `mypy` to type check the code: `mypy weblate_web/`
- All mentioned linting tools MUST pass.
