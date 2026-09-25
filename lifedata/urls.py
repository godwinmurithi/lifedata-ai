"""Root URL configuration.

Everything is mounted under /lifedata-ai/ because nginx routes that prefix to
this app and everything else to Superset (spec §3).
"""

from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path("lifedata-ai/", include("lifedata_ai.urls")),
]

if settings.DEBUG:
    # Serves the panel bundle at /lifedata-ai/static/panel/panel.js.
    urlpatterns += staticfiles_urlpatterns()
