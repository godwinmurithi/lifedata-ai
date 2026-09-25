"""WSGI entry point for the LifeData orchestrator."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lifedata.settings")

application = get_wsgi_application()
