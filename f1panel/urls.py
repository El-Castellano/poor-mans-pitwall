from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path, include

urlpatterns = [
    path("", include("timing.urls")),
]

urlpatterns += staticfiles_urlpatterns()
