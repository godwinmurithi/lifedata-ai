"""ASGI entry point for the LifeData orchestrator."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lifedata.settings")

application = get_asgi_application()
