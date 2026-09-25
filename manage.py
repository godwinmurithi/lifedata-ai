#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lifedata.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - developer setup guidance
        raise ImportError(
            "Could not import Django. Is the virtual environment installed?\n"
            "    uv sync\n"
            "    uv run python manage.py check"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
