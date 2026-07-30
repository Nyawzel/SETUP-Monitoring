"""
dashboard/urls.py — PROJECT-LEVEL urls (the one ROOT_URLCONF points to).

This is separate from monitoring/urls.py (your app-level urls, which stays
exactly as-is and gets included here). Save this content as dashboard/urls.py
in your project, replacing whatever is there now.

Why this file matters for the "photo doesn't show" bug:
Django's dev server does NOT serve files under MEDIA_ROOT automatically.
It only serves STATIC files automatically-ish via staticfiles app. For
MEDIA (user-uploaded files: project photos, receipts, requirement docs,
report files, etc.) you must explicitly add the `static(...)` helper below,
and only while DEBUG=True. Without this, {{ project.photo.url }} renders a
correct-looking <img src="/media/project_photos/2026/07/xyz.jpg"> tag, the
browser requests it, and Django 404s — so the upload itself succeeds (the
file really is saved to disk and the DB row really does point at it), but
nothing ever serves it back to the browser. That matches exactly what
you're seeing: upload "works" (no error, record saved) but the picture
never appears.
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('monitoring.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Not strictly needed (django.contrib.staticfiles already serves STATIC_URL
    # in DEBUG via the app's runserver override), but harmless if you ever
    # disable that app's autoserving:
    # urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
