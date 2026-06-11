from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.urls import include, path, re_path


FLUTTER_WEB_ROOT = settings.BASE_DIR.parent / "jsTube-fe" / "build" / "web"


def serve_flutter_web(request, path=""):
    web_root = FLUTTER_WEB_ROOT.resolve()
    target = (web_root / (path or "index.html")).resolve()

    if web_root not in target.parents and target != web_root:
        raise Http404("Not found")

    if not target.is_file():
        target = web_root / "index.html"

    if not target.is_file():
        raise Http404("Flutter web build not found")

    return FileResponse(target.open("rb"))


urlpatterns = [
    path("api/", include("media_api.urls")),
    path("", serve_flutter_web),
    re_path(r"^(?P<path>.*)$", serve_flutter_web),
]
