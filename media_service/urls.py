from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.urls import include, path, re_path


FLUTTER_WEB_ROOT = settings.BASE_DIR.parent / "jsTube-fe" / "build" / "web"
DOWNLOAD_ROOT = settings.BASE_DIR.parent / "jsTube-fe" / "build" / "downloads"


def serve_flutter_web(request, path=""):
    web_root = FLUTTER_WEB_ROOT.resolve()
    target = (web_root / (path or "index.html")).resolve()

    if web_root not in target.parents and target != web_root:
        raise Http404("Not found")

    requested_index = not path or path.endswith("index.html")
    if not target.is_file():
        target = web_root / "index.html"
        requested_index = True

    if not target.is_file():
        raise Http404("Flutter web build not found")

    response = FileResponse(target.open("rb"))
    no_cache_files = {"flutter_bootstrap.js", "flutter.js", "flutter_service_worker.js"}
    if requested_index or target.name in no_cache_files:
        response["Cache-Control"] = "no-cache"
    else:
        response["Cache-Control"] = "public, max-age=86400"
    return response


def serve_download(request, filename: str):
    download_root = DOWNLOAD_ROOT.resolve()
    target = (download_root / filename).resolve()

    if download_root not in target.parents and target != download_root:
        raise Http404("Not found")
    if not target.is_file():
        raise Http404("Download not found")

    response = FileResponse(target.open("rb"), as_attachment=True, filename=filename)
    response["Cache-Control"] = "public, max-age=3600"
    return response


urlpatterns = [
    path("api/", include("media_api.urls")),
    path("downloads/<str:filename>", serve_download),
    path("", serve_flutter_web),
    re_path(r"^(?P<path>.*)$", serve_flutter_web),
]
