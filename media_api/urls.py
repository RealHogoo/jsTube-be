from django.urls import path

from . import views


urlpatterns = [
    path("health/", views.health),
    path("version/", views.version),
    path("me/", views.me),
    path("sync/", lambda request: views.options_or_view(request, views.sync)),
    path("youtube/tools/check/", views.youtube_tools_check),
    path("youtube/preview/", views.youtube_preview),
    path("youtube/import/status/", views.youtube_import_status),
    path("youtube/import/item/start/", views.youtube_import_item_start),
    path("youtube/import/start-all/", views.youtube_import_start_all),
    path("youtube/import/", views.youtube_import_view),
    path("media/", views.mongo_safe_view(views.media_list)),
    path("media/<int:webhard_file_id>/", views.mongo_safe_view(views.media_detail)),
    path("media/<int:webhard_file_id>/delete/", views.mongo_safe_view(views.media_delete)),
    path("media/<int:webhard_file_id>/thumbnail/", views.mongo_safe_view(views.media_thumbnail)),
    path("karaoke/remote/session/", views.mongo_safe_view(views.karaoke_remote_session)),
    path("karaoke/remote/<str:session_id>/command/", views.mongo_safe_view(views.karaoke_remote_command)),
    path("karaoke/remote/<str:session_id>/commands/", views.mongo_safe_view(views.karaoke_remote_commands)),
    path("karaoke/remote/<str:session_id>/heartbeat/", views.mongo_safe_view(views.karaoke_remote_heartbeat)),
    path("media/<int:webhard_file_id>/content-file/", lambda request, webhard_file_id: views.media_file_proxy(request, webhard_file_id, "content")),
    path("media/<int:webhard_file_id>/thumbnail-file/", lambda request, webhard_file_id: views.media_file_proxy(request, webhard_file_id, "thumbnail")),
    path("media/<int:webhard_file_id>/download-file/", lambda request, webhard_file_id: views.media_file_proxy(request, webhard_file_id, "download")),
    path("albums/", views.mongo_safe_view(views.albums)),
]
