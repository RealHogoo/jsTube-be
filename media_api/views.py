import json
import logging
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bson import ObjectId
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from pymongo.errors import PyMongoError

from .auth import CurrentUser, auth_token, require_user
from .mongo import karaoke_remote_collection, media_collection, media_user_state_collection, mongo_client
from .webhard import stream_webhard_file, sync_from_webhard, sync_one_from_webhard
from .youtube import check_download_tools, import_youtube_item, preview_youtube

try:
    YOUTUBE_IMPORT_CONCURRENCY = max(int(os.getenv("YOUTUBE_IMPORT_CONCURRENCY", "1")), 1)
except ValueError:
    YOUTUBE_IMPORT_CONCURRENCY = 1
YOUTUBE_IMPORT_SEMAPHORE = threading.Semaphore(YOUTUBE_IMPORT_CONCURRENCY)
LOGGER = logging.getLogger(__name__)


def ok(data: dict[str, Any] | list[Any]) -> JsonResponse:
    return JsonResponse({"ok": True, "code": "OK", "message": "success", "data": data}, json_dumps_params={"ensure_ascii": False})


def bad_request(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "code": "BAD_REQUEST", "message": message}, status=400)


def mongo_unavailable(message: str = "MongoDB connection is unavailable") -> JsonResponse:
    return JsonResponse({"ok": False, "code": "MONGO_UNAVAILABLE", "message": message}, status=503)


def mongo_safe_view(view):
    def wrapped(request: HttpRequest, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PyMongoError as error:
            LOGGER.warning("MongoDB unavailable while handling %s", request.path, exc_info=error)
            return mongo_unavailable()
    return wrapped


def health(_request: HttpRequest) -> JsonResponse:
    mongo_status = "UP"
    try:
        mongo_client().admin.command("ping")
    except Exception:
        mongo_status = "DOWN"
    return ok({"status": "UP" if mongo_status == "UP" else "DEGRADED", "service": "media-service", "mongo": mongo_status})


def version(_request: HttpRequest) -> JsonResponse:
    return ok({
        "service": "media-service",
        "git_commit": git_commit(),
    })


@csrf_exempt
def options_or_view(request: HttpRequest, view):
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    return view(request)


def me(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    return ok({
        "user_id": user.user_id,
        "roles": user.roles,
        "is_admin": user.is_admin,
        "permissions": {
            "write": user.has_permission("WRITE"),
            "share": user.has_permission("SHARE"),
            "delete": user.has_permission("DELETE"),
        },
    })


def sync(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    limit = int_param(request, "limit", 0)
    return ok(sync_from_webhard(user, limit if limit > 0 else None))


@csrf_exempt
def youtube_preview(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    body = json_body(request)
    url = str(body.get("url") or "").strip()
    if not is_youtube_url(url):
        return bad_request("youtube url is required")
    try:
        return ok(preview_youtube(url))
    except Exception as exc:
        return JsonResponse({"ok": False, "code": "YOUTUBE_ANALYZE_FAILED", "message": str(exc)}, status=502)


@csrf_exempt
def youtube_tools_check(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    return ok(check_download_tools())


@csrf_exempt
def youtube_import_status(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    raw_ids = body.get("youtube_video_ids") or []
    if not isinstance(raw_ids, list):
        return bad_request("youtube_video_ids must be a list")
    video_ids = []
    for item in raw_ids:
        video_id = str(item or "").strip()
        if video_id and video_id not in video_ids:
            video_ids.append(video_id[:80])
    items = []
    if video_ids:
        query: dict[str, Any] = {
            "source_type": "YOUTUBE_DOWNLOAD",
            "youtube_video_id": {"$in": video_ids[:200]},
        }
        if not user.is_admin:
            query["owner_user_id"] = user.user_id
        items = [serialize_media(item) for item in media_collection().find(query, media_list_projection()).limit(200)]
    return ok({"items": items, "saved_count": len(items), "job": youtube_import_job(job_id, user)})


@csrf_exempt
def youtube_import_view(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    url = str(body.get("url") or "").strip()
    if not is_youtube_url(url):
        return bad_request("youtube url is required")
    tool_status = check_download_tools()
    if not tool_status.get("ok_to_download"):
        return JsonResponse({"ok": False, "code": "YOUTUBE_TOOL_CHECK_FAILED", "message": "download environment or webhard check failed", "data": tool_status}, status=422)
    return ok(create_youtube_import_job(url, user, normalize_tags(body.get("tags") or "")))


@csrf_exempt
def youtube_import_item_start(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    video_id = str(body.get("youtube_video_id") or "").strip()
    if not job_id or not video_id:
        return bad_request("job_id and youtube_video_id are required")
    if not is_safe_youtube_video_id(video_id):
        return bad_request("youtube_video_id is invalid")
    job = youtube_job_collection().find_one({"job_id": job_id, "owner_user_id": user.user_id})
    if not job:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "youtube import job not found"}, status=404)
    try:
        started = start_youtube_import_item(job, video_id, user)
    except RuntimeError as exc:
        return bad_request(str(exc))
    return ok({"job": serialize_youtube_job(started), "message": "youtube item started"})


@csrf_exempt
def youtube_import_start_all(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "admin permission is required"}, status=403)
    if not user.has_permission("WRITE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "write permission is required"}, status=403)
    body = json_body(request)
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        return bad_request("job_id is required")
    job = youtube_job_collection().find_one({"job_id": job_id, "owner_user_id": user.user_id})
    if not job:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "youtube import job not found"}, status=404)
    started = start_youtube_import_job(job, user)
    refreshed = youtube_job_collection().find_one({"job_id": job_id}) or job
    return ok({"job": serialize_youtube_job(refreshed), "started_count": started})


def media_list(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    limit = min(max(int_param(request, "limit", 40), 1), 100)
    offset = max(int_param(request, "offset", 0), 0)
    query: dict[str, Any] = readable_media_query(user)
    if user.is_admin and request.GET.get("owner_user_id"):
        query["owner_user_id"] = request.GET["owner_user_id"].strip()

    content_kind = request.GET.get("content_kind", "").strip().upper()
    if content_kind in {"IMAGE", "VIDEO"}:
        query["content_kind"] = content_kind
        if content_kind == "VIDEO" and request.GET.get("tag", "").strip() != "노래방":
            query["tags"] = {"$ne": "노래방"}
    elif content_kind == "KARAOKE":
        query["content_kind"] = "VIDEO"
        query["tags"] = "노래방"
    if request.GET.get("tag"):
        query["tags"] = request.GET["tag"].strip()
    if request.GET.get("album"):
        query["album"] = request.GET["album"].strip()
    if request.GET.get("favorite") in {"true", "1", "Y"}:
        favorite_ids = favorite_media_ids(user)
        query["webhard_file_id"] = {"$in": favorite_ids}
    if request.GET.get("q"):
        keyword = request.GET["q"].strip()[:80]
    if request.GET.get("q") and keyword:
        search_terms = karaoke_search_terms(keyword) if content_kind == "KARAOKE" else [keyword]
        search_patterns = [re.escape(term) for term in search_terms]
        search_fields = [
            "title",
            "display_name",
            "file_name",
            "tags",
            "webhard_tags",
            "album",
            "description",
            "webhard_memo",
            "channel_name",
            "owner_user_id",
        ]
        search_query = {"$or": []}
        for field_name in search_fields:
            search_query["$or"].extend(
                {field_name: {"$regex": term_pattern, "$options": "i"}}
                for term_pattern in search_patterns
            )
        if "$or" in query:
            access_query = {"$or": query.pop("$or")}
            query["$and"] = [access_query, search_query]
        else:
            query.update(search_query)

    sort_key = request.GET.get("sort", "recent").strip().lower()
    if sort_key == "popular":
        sort = [("view_count", -1), ("like_count", -1), ("original_created_at", -1), ("webhard_file_id", -1)]
    elif sort_key == "liked":
        sort = [("like_count", -1), ("view_count", -1), ("original_created_at", -1), ("webhard_file_id", -1)]
    else:
        sort = [("original_created_at", -1), ("webhard_file_id", -1)]

    collection = media_collection()
    count_base_query = dict(query)
    count_base_query.pop("content_kind", None)
    count_base_query.pop("tags", None)
    counts = None
    if offset == 0 or request.GET.get("include_counts") in {"true", "1", "Y"}:
        counts = media_counts(collection, count_base_query)
    cursor = collection.find(query, media_list_projection()).sort(sort).skip(offset).limit(limit + 1)
    raw_items = list(cursor)
    state_map = user_state_map(user, [int(item.get("webhard_file_id") or 0) for item in raw_items])
    fetched_items = [serialize_media(item, user_state=state_map.get(int(item.get("webhard_file_id") or 0))) for item in raw_items]
    has_more = len(fetched_items) > limit
    items = fetched_items[:limit]
    return ok({
        "items": items,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "counts": counts,
    })


@csrf_exempt
def media_detail(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method not in {"GET", "PATCH"}:
        return bad_request("GET or PATCH is required")

    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    query: dict[str, Any] = readable_media_query(user, webhard_file_id)

    if request.method == "GET":
        item = media_collection().find_one(query)
        if not item:
            return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
        return ok({"item": serialize_media(item, user_state=user_state(user, webhard_file_id))})

    body = json_body(request)
    item = media_collection().find_one(query)
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    edit_fields = {"tags", "album", "title", "description", "channel_name", "subscribed"}
    if edit_fields.intersection(body.keys()) and not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    update: dict[str, Any] = {}
    increments: dict[str, int] = {}
    if "tags" in body:
        update["tags"] = normalize_tags(body["tags"])
    if "album" in body:
        update["album"] = str(body.get("album") or "").strip()[:100]
    if "title" in body:
        update["title"] = str(body.get("title") or "").strip()[:180]
    if "description" in body:
        update["description"] = str(body.get("description") or "").strip()[:2000]
    if "channel_name" in body:
        update["channel_name"] = str(body.get("channel_name") or "").strip()[:120]
    if "subscribed" in body:
        update["subscribed"] = bool(body.get("subscribed"))
    if "favorite" in body:
        set_user_state(user, webhard_file_id, {"favorite": bool(body.get("favorite"))})
    if "liked" in body:
        liked = bool(body.get("liked"))
        previous = user_state(user, webhard_file_id)
        previous_liked = bool(previous.get("liked")) if previous else False
        set_user_state(user, webhard_file_id, {"liked": liked})
        if liked != previous_liked:
            increments["like_count"] = 1 if liked else -1
    if body.get("increment_view"):
        increments["view_count"] = 1

    if update or increments:
        update["updated_at"] = datetime.utcnow()
        patch: dict[str, Any] = {"$set": update}
        if increments:
            patch["$inc"] = increments
        result = media_collection().update_one(query, patch)
        if increments.get("like_count", 0) < 0:
            media_collection().update_one(query, {"$max": {"like_count": 0}})
        if result.matched_count == 0:
            return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    item = media_collection().find_one(query)
    return ok({"item": serialize_media(item, user_state=user_state(user, webhard_file_id))})


@csrf_exempt
def media_delete(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")

    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    if not user.has_permission("DELETE"):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "delete permission is required"}, status=403)

    item = media_collection().find_one({"webhard_file_id": webhard_file_id})
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    if not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    token = auth_token(request)
    try:
        response = requests.post(
            f"{settings.MEDIA_CONFIG['WEBHARD_PUBLIC_BASE_URL']}/file/delete.json",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"file_id": webhard_file_id},
            timeout=15,
        )
        body = response.json()
    except requests.RequestException:
        return JsonResponse({"ok": False, "code": "WEBHARD_UNAVAILABLE", "message": "webhard delete request failed"}, status=502)
    except ValueError:
        return JsonResponse({"ok": False, "code": "WEBHARD_INVALID_RESPONSE", "message": "webhard delete response is invalid"}, status=502)

    if not response.ok or body.get("ok") is not True:
        return JsonResponse(
            {
                "ok": False,
                "code": body.get("code") or "WEBHARD_DELETE_FAILED",
                "message": body.get("message") or "delete failed",
            },
            status=response.status_code if response.status_code >= 400 else 502,
        )

    media_collection().delete_one({"webhard_file_id": webhard_file_id})
    media_user_state_collection().delete_many({"webhard_file_id": webhard_file_id})
    return ok({"file_id": webhard_file_id})


@csrf_exempt
def media_thumbnail(request: HttpRequest, webhard_file_id: int) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")

    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    item = media_collection().find_one({"webhard_file_id": webhard_file_id})
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media not found"}, status=404)
    if not can_manage_media(user, item):
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "owner or admin permission is required"}, status=403)

    body = json_body(request)
    seek_seconds = optional_seconds(body.get("seek_seconds"))
    if seek_seconds is False:
        return bad_request("seek_seconds must be numeric")

    payload: dict[str, Any] = {"file_id": webhard_file_id, "limit": 1}
    if isinstance(seek_seconds, (int, float)):
        payload["seek_seconds"] = seek_seconds

    token = auth_token(request)
    try:
        response = requests.post(
            f"{settings.MEDIA_CONFIG['WEBHARD_PUBLIC_BASE_URL']}/thumbnail/rebuild.json",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        body = response.json()
    except requests.RequestException:
        return JsonResponse({"ok": False, "code": "WEBHARD_UNAVAILABLE", "message": "webhard thumbnail request failed"}, status=502)
    except ValueError:
        return JsonResponse({"ok": False, "code": "WEBHARD_INVALID_RESPONSE", "message": "webhard thumbnail response is invalid"}, status=502)

    if not response.ok or body.get("ok") is not True:
        return JsonResponse(
            {
                "ok": False,
                "code": body.get("code") or "WEBHARD_THUMBNAIL_FAILED",
                "message": body.get("message") or "thumbnail creation failed",
            },
            status=response.status_code if response.status_code >= 400 else 502,
        )

    synced = sync_one_from_webhard(user, webhard_file_id)
    return ok({"thumbnail": body.get("data") or {}, "item": serialize_media(synced.get("item"))})


@csrf_exempt
def karaoke_remote_session(request: HttpRequest) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    now = datetime.utcnow()
    session_id = uuid.uuid4().hex
    item = {
        "session_id": session_id,
        "owner_user_id": user.user_id,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=12),
        "commands": [],
        "next_sequence": 1,
    }
    karaoke_remote_collection().insert_one(item)
    return ok({"session_id": session_id, "expires_at": item["expires_at"].isoformat()})


@csrf_exempt
def karaoke_remote_command(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    body = json_body(request)
    command_type = str(body.get("type") or "").strip().upper()
    if command_type not in {"PLAY_ITEM", "RESERVE_ITEM", "NEXT", "PREV_TAG", "NEXT_TAG", "TOGGLE_PLAY", "CLEAR_QUEUE"}:
        return bad_request("invalid remote command")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

    collection = karaoke_remote_collection()
    session = collection.find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if str(session.get("owner_user_id") or "") != user.user_id and not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)
    if is_remote_rate_limited(session):
        return JsonResponse({"ok": False, "code": "RATE_LIMITED", "message": "remote command rate limit exceeded"}, status=429)
    sequence = int(session.get("next_sequence") or 1)
    command = {
        "sequence": sequence,
        "type": command_type,
        "payload": sanitize_remote_payload(user, payload),
        "created_by": user.user_id,
        "created_at": datetime.utcnow(),
    }
    commands = list(session.get("commands") or [])[-49:] + [command]
    collection.update_one(
        {"session_id": session_id},
        {"$set": {"commands": commands, "updated_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(hours=12), "next_sequence": sequence + 1}},
    )
    return ok({"sequence": sequence})


@csrf_exempt
def karaoke_remote_heartbeat(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method == "OPTIONS":
        return HttpResponse(status=204)
    if request.method != "POST":
        return bad_request("POST is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    session = karaoke_remote_collection().find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if str(session.get("owner_user_id") or "") != user.user_id and not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)
    expires_at = datetime.utcnow() + timedelta(hours=12)
    karaoke_remote_collection().update_one(
        {"session_id": session_id},
        {"$set": {"updated_at": datetime.utcnow(), "expires_at": expires_at}},
    )
    return ok({"session_id": session_id, "expires_at": expires_at.isoformat()})


def karaoke_remote_commands(request: HttpRequest, session_id: str) -> JsonResponse | HttpResponse:
    if request.method != "GET":
        return bad_request("GET is required")
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    session = karaoke_remote_collection().find_one({"session_id": session_id})
    if not session:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "remote session not found"}, status=404)
    if str(session.get("owner_user_id") or "") != user.user_id and not user.is_admin:
        return JsonResponse({"ok": False, "code": "FORBIDDEN", "message": "remote session owner permission is required"}, status=403)

    after = int_param(request, "after", 0)
    commands = [
        serialize_remote_command(command)
        for command in session.get("commands") or []
        if int(command.get("sequence") or 0) > after
    ]
    return ok({"session_id": session_id, "commands": commands, "latest_sequence": int(session.get("next_sequence") or 1) - 1})


def media_file_proxy(request: HttpRequest, webhard_file_id: int, file_kind: str) -> JsonResponse | HttpResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user

    item = media_collection().find_one(readable_media_query(user, webhard_file_id))
    if not item:
        return JsonResponse({"ok": False, "code": "NOT_FOUND", "message": "media file not found"}, status=404)

    if file_kind not in {"thumbnail", "content", "download"}:
        return bad_request("invalid file kind")
    range_header = ""
    if file_kind == "content":
        range_header = normalized_range_header(request.headers.get("Range", ""))
        if range_header is None:
            return JsonResponse({"ok": False, "code": "INVALID_RANGE", "message": "range header is invalid"}, status=416)
    try:
        upstream = stream_webhard_file(
            user,
            webhard_file_id,
            file_kind,
            allow_public=is_public_media(item),
            range_header=range_header,
        )
    except RuntimeError as exc:
        LOGGER.warning("webhard stream failed for file_id=%s kind=%s: %s", webhard_file_id, file_kind, exc)
        return JsonResponse({"ok": False, "code": "WEBHARD_STREAM_FAILED", "message": "media stream failed"}, status=502)

    response = StreamingHttpResponse(
        stream_response_chunks(upstream),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type") or "application/octet-stream",
    )
    for header in [
        "Content-Disposition",
        "X-Content-Type-Options",
        "Content-Security-Policy",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
    ]:
        value = upstream.headers.get(header)
        if value:
            response[header] = value
    return response


def albums(request: HttpRequest) -> JsonResponse:
    user = require_user(request)
    if not isinstance(user, CurrentUser):
        return user
    query = readable_media_query(user)
    values = [item for item in media_collection().distinct("album", query) if item]
    values.sort()
    return ok({"items": values})


def serialize_media(item: dict[str, Any] | None, user_state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not item:
        return {}
    result = dict(item)
    result["_id"] = str(result.get("_id", ""))
    result["title"] = result.get("title") or result.get("display_name") or result.get("file_name") or "Untitled"
    result["description"] = result.get("description") or ""
    result["channel_name"] = result.get("channel_name") or channel_name(result)
    result["view_count"] = max(int(result.get("view_count") or 0), 0)
    result["like_count"] = max(int(result.get("like_count") or 0), 0)
    state = user_state or {}
    result["favorite"] = bool(state.get("favorite"))
    result["liked"] = bool(state.get("liked"))
    result["subscribed"] = bool(result.get("subscribed"))
    result["karaoke_number"] = karaoke_number(result)
    result["karaoke_artist"] = karaoke_artist(result)
    result["time_markers"] = karaoke_time_markers(result.get("tags") or [])
    thumbnail_url = str(result.get("thumbnail_url") or "")
    content_url = str(result.get("content_url") or "")
    if result.get("content_kind") == "VIDEO" and (thumbnail_url == content_url or "/file/content/" in thumbnail_url):
        result["thumbnail_url"] = ""
    for key, value in list(result.items()):
        if isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def media_list_projection() -> dict[str, int]:
    return {
        "_id": 1,
        "webhard_file_id": 1,
        "owner_user_id": 1,
        "owner_is_admin": 1,
        "file_name": 1,
        "display_name": 1,
        "file_size": 1,
        "content_type": 1,
        "content_kind": 1,
        "thumbnail_url": 1,
        "content_url": 1,
        "download_url": 1,
        "original_created_at": 1,
        "uploaded_at": 1,
        "webhard_updated_at": 1,
        "source_type": 1,
        "youtube_video_id": 1,
        "youtube_url": 1,
        "youtube_playlist_id": 1,
        "youtube_playlist_title": 1,
        "title": 1,
        "description": 1,
        "channel_name": 1,
        "album": 1,
        "tags": 1,
        "webhard_tags": 1,
        "webhard_memo": 1,
        "favorite": 1,
        "view_count": 1,
        "like_count": 1,
        "liked": 1,
        "subscribed": 1,
    }


def favorite_media_ids(user: CurrentUser) -> list[int]:
    return [
        int(item)
        for item in media_user_state_collection().distinct("webhard_file_id", {"user_id": user.user_id, "favorite": True})
        if item
    ]


def user_state(user: CurrentUser, webhard_file_id: int) -> dict[str, Any] | None:
    return media_user_state_collection().find_one({"user_id": user.user_id, "webhard_file_id": int(webhard_file_id)})


def user_state_map(user: CurrentUser, webhard_file_ids: list[int]) -> dict[int, dict[str, Any]]:
    ids = [int(item) for item in webhard_file_ids if item]
    if not ids:
        return {}
    states = media_user_state_collection().find({"user_id": user.user_id, "webhard_file_id": {"$in": ids}})
    return {int(state.get("webhard_file_id")): state for state in states if state.get("webhard_file_id")}


def set_user_state(user: CurrentUser, webhard_file_id: int, patch: dict[str, Any]) -> None:
    update = {"updated_at": datetime.utcnow(), **patch}
    media_user_state_collection().update_one(
        {"user_id": user.user_id, "webhard_file_id": int(webhard_file_id)},
        {
            "$set": update,
            "$setOnInsert": {
                "user_id": user.user_id,
                "webhard_file_id": int(webhard_file_id),
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )


def readable_media_query(user: CurrentUser, webhard_file_id: int | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if webhard_file_id is not None:
        query["webhard_file_id"] = webhard_file_id
    if user.is_admin:
        return query
    query["$or"] = [
        {"owner_user_id": user.user_id},
        {"owner_is_admin": True},
    ]
    return query


def is_public_media(item: dict[str, Any]) -> bool:
    return bool(item.get("owner_is_admin"))


def stream_response_chunks(upstream: requests.Response):
    try:
        for chunk in upstream.iter_content(chunk_size=1024 * 1024):
            if chunk:
                yield chunk
    finally:
        upstream.close()


def normalized_range_header(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"bytes=(\d{0,16})-(\d{0,16})", text)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    return text


def media_counts(collection, query: dict[str, Any]) -> dict[str, int]:
    tags_array = {"$cond": [{"$isArray": "$tags"}, "$tags", []]}
    karaoke_tag = {"$in": ["노래방", tags_array]}
    row = next(collection.aggregate([
        {"$match": query},
        {
            "$group": {
                "_id": None,
                "image": {"$sum": {"$cond": [{"$eq": ["$content_kind", "IMAGE"]}, 1, 0]}},
                "video": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$content_kind", "VIDEO"]}, {"$not": [karaoke_tag]}]},
                            1,
                            0,
                        ]
                    }
                },
                "karaoke": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$content_kind", "VIDEO"]}, karaoke_tag]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]), None) or {}
    return {
        "image": int(row.get("image") or 0),
        "video": int(row.get("video") or 0),
        "karaoke": int(row.get("karaoke") or 0),
    }


def youtube_job_collection():
    db = mongo_client()[settings.MEDIA_CONFIG["MEDIA_MONGO_DATABASE"]]
    collection = db["youtube_import_jobs"]
    collection.create_index([("job_id", 1)], unique=True)
    collection.create_index([("owner_user_id", 1), ("updated_at", -1)])
    return collection


def create_youtube_import_job(url: str, user: CurrentUser, tags: list[str]) -> dict[str, Any]:
    preview = preview_youtube(url)
    job_id = uuid.uuid4().hex
    now = datetime.utcnow()
    items = []
    max_items = youtube_import_max_items()
    for index, item in enumerate((preview.get("items") or [])[:max_items]):
        video_id = str(item.get("youtube_video_id") or "").strip()
        if not is_safe_youtube_video_id(video_id):
            continue
        items.append({
            **item,
            "order_no": index + 1,
            "status": "QUEUED",
            "file_id": None,
            "message": "",
            "started_at": None,
            "finished_at": None,
        })
    if not items:
        raise RuntimeError("youtube import items were not found")
    doc = {
        "job_id": job_id,
        "owner_user_id": user.user_id,
        "status": "QUEUED",
        "message": "youtube import job created",
        "url": url,
        "tags": tags,
        "playlist_id": preview.get("playlist_id") or "",
        "playlist_title": preview.get("playlist_title") or "",
        "title": preview.get("playlist_title") or preview.get("title") or "",
        "source_type": "YOUTUBE_DOWNLOAD",
        "items": items,
        "item_count": len(items),
        "source_item_count": int(preview.get("item_count") or len(items)),
        "truncated": int(preview.get("item_count") or len(items)) > len(items),
        "started_count": 0,
        "downloaded_count": 0,
        "failed_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    youtube_job_collection().insert_one(doc)
    return serialize_youtube_job(doc)


def youtube_import_job(job_id: str, user: CurrentUser) -> dict[str, Any] | None:
    if not job_id:
        return None
    query = {"job_id": job_id}
    if not user.is_admin:
        query["owner_user_id"] = user.user_id
    job = youtube_job_collection().find_one(query)
    return serialize_youtube_job(job) if job else None


def start_youtube_import_item(job: dict[str, Any], video_id: str, user: CurrentUser) -> dict[str, Any]:
    if not is_safe_youtube_video_id(video_id):
        raise RuntimeError("youtube video id is invalid")
    target = next((item for item in job.get("items") or [] if str(item.get("youtube_video_id") or "") == video_id), None)
    if not target:
        raise RuntimeError("youtube import item not found")
    if target.get("status") in {"RUNNING", "SAVED"}:
        return job
    if youtube_running_count(job) >= YOUTUBE_IMPORT_CONCURRENCY:
        raise RuntimeError("youtube import concurrency limit exceeded")
    mark_youtube_import_item_running(job["job_id"], video_id)
    thread = threading.Thread(target=run_youtube_import_item, args=(job["job_id"], video_id, user), daemon=True)
    thread.start()
    return youtube_job_collection().find_one({"job_id": job["job_id"]}) or job


def start_youtube_import_job(job: dict[str, Any], user: CurrentUser) -> int:
    if job.get("dispatcher_running"):
        return 0
    pending = [
        str(item.get("youtube_video_id") or "")
        for item in job.get("items") or []
        if item.get("status") in {"QUEUED", "FAILED"} and is_safe_youtube_video_id(str(item.get("youtube_video_id") or ""))
    ]
    if not pending:
        return 0
    now = datetime.utcnow()
    updated = youtube_job_collection().update_one(
        {"job_id": job["job_id"], "dispatcher_running": {"$ne": True}},
        {"$set": {"dispatcher_running": True, "status": "RUNNING", "message": "youtube import running", "updated_at": now}},
    )
    if updated.modified_count == 0:
        return 0
    thread = threading.Thread(target=run_youtube_import_job, args=(job["job_id"], pending, user), daemon=True)
    thread.start()
    return len(pending)


def run_youtube_import_job(job_id: str, pending_ids: list[str], user: CurrentUser) -> None:
    try:
        for video_id in pending_ids:
            job = youtube_job_collection().find_one({"job_id": job_id})
            if not job:
                return
            item = next((entry for entry in job.get("items") or [] if str(entry.get("youtube_video_id") or "") == video_id), None)
            if not item:
                continue
            if item.get("status") not in {"QUEUED", "FAILED"}:
                continue
            mark_youtube_import_item_running(job_id, video_id)
            run_youtube_import_item(job_id, video_id, user)
    finally:
        youtube_job_collection().update_one(
            {"job_id": job_id},
            {"$set": {"dispatcher_running": False, "updated_at": datetime.utcnow()}},
        )
        refresh_youtube_job_status(job_id)


def run_youtube_import_item(job_id: str, video_id: str, user: CurrentUser) -> None:
    with YOUTUBE_IMPORT_SEMAPHORE:
        job = youtube_job_collection().find_one({"job_id": job_id})
        if not job:
            return
        item = next((entry for entry in job.get("items") or [] if str(entry.get("youtube_video_id") or "") == video_id), None)
        if not item:
            return
        try:
            result = import_youtube_item(
                item,
                user,
                normalize_import_tags(job.get("tags") or []),
                str(job.get("playlist_id") or ""),
                str(job.get("playlist_title") or ""),
            )
            update_youtube_import_item(job_id, video_id, "SAVED", "saved", result.get("file_id"), result)
        except Exception as exc:
            update_youtube_import_item(job_id, video_id, "FAILED", str(exc)[:500], None, None)


def mark_youtube_import_item_running(job_id: str, video_id: str) -> None:
    now = datetime.utcnow()
    youtube_job_collection().update_one(
        {"job_id": job_id, "items.youtube_video_id": video_id},
        {
            "$set": {
                "status": "RUNNING",
                "message": "youtube import running",
                "updated_at": now,
                "items.$.status": "RUNNING",
                "items.$.message": "download started",
                "items.$.started_at": now,
                "items.$.finished_at": None,
            }
        },
    )


def update_youtube_import_item(
    job_id: str,
    video_id: str,
    status: str,
    message: str,
    file_id: int | None,
    result: dict[str, Any] | None,
) -> None:
    now = datetime.utcnow()
    update = {
        "updated_at": now,
        "items.$.status": status,
        "items.$.message": message,
        "items.$.file_id": file_id,
        "items.$.finished_at": now,
    }
    if result:
        update["items.$.result"] = result
    youtube_job_collection().update_one(
        {"job_id": job_id, "items.youtube_video_id": video_id},
        {"$set": update},
    )
    refresh_youtube_job_status(job_id)


def refresh_youtube_job_status(job_id: str) -> None:
    job = youtube_job_collection().find_one({"job_id": job_id})
    if not job:
        return
    items = job.get("items") or []
    saved = sum(1 for item in items if item.get("status") == "SAVED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    running = sum(1 for item in items if item.get("status") == "RUNNING")
    queued = sum(1 for item in items if item.get("status") == "QUEUED")
    if running > 0:
        status = "RUNNING"
    elif queued > 0 and saved + failed == 0:
        status = "QUEUED"
    elif queued > 0:
        status = "PARTIAL"
    elif failed > 0 and saved == 0:
        status = "FAILED"
    elif failed > 0:
        status = "PARTIAL"
    else:
        status = "DONE"
    youtube_job_collection().update_one(
        {"job_id": job_id},
        {
            "$set": {
                "status": status,
                "message": youtube_job_message(status, saved, failed, queued, running),
                "downloaded_count": saved,
                "failed_count": failed,
                "started_count": saved + failed + running,
                "updated_at": datetime.utcnow(),
            }
        },
    )


def youtube_job_message(status: str, saved: int, failed: int, queued: int, running: int) -> str:
    if status == "DONE":
        return "youtube import completed"
    if status == "FAILED":
        return "youtube import failed"
    return f"saved {saved}, failed {failed}, running {running}, queued {queued}"


def serialize_youtube_job(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    items = [serialize_youtube_job_item(item) for item in job.get("items") or []]
    item_count = int(job.get("item_count") or len(items))
    downloaded_count = sum(1 for item in items if item.get("status") == "SAVED")
    failed_count = sum(1 for item in items if item.get("status") == "FAILED")
    running_count = sum(1 for item in items if item.get("status") == "RUNNING")
    queued_count = sum(1 for item in items if item.get("status") == "QUEUED")
    finished_count = downloaded_count + failed_count
    active_item = next((item for item in items if item.get("status") == "RUNNING"), None)
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": job.get("message"),
        "title": job.get("title") or "",
        "playlist_id": job.get("playlist_id") or "",
        "playlist_title": job.get("playlist_title") or "",
        "item_count": item_count,
        "source_item_count": int(job.get("source_item_count") or item_count),
        "truncated": bool(job.get("truncated")),
        "dispatcher_running": bool(job.get("dispatcher_running")),
        "downloaded_count": downloaded_count,
        "failed_count": failed_count,
        "running_count": running_count,
        "queued_count": queued_count,
        "finished_count": finished_count,
        "progress_percent": round((finished_count / item_count) * 100, 1) if item_count else 0,
        "active_item": active_item,
        "items": items,
        "result": youtube_job_result(job, items),
        "created_at": job.get("created_at").isoformat() if isinstance(job.get("created_at"), datetime) else "",
        "updated_at": job.get("updated_at").isoformat() if isinstance(job.get("updated_at"), datetime) else "",
    }


def serialize_youtube_job_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_no": int(item.get("order_no") or 0),
        "youtube_video_id": item.get("youtube_video_id") or "",
        "title": item.get("title") or item.get("youtube_video_id") or "",
        "thumbnail_url": item.get("thumbnail_url") or "",
        "duration": item.get("duration"),
        "channel_name": item.get("channel_name") or "",
        "status": item.get("status") or "QUEUED",
        "file_id": item.get("file_id"),
        "webhard_file_id": item.get("file_id"),
        "message": item.get("message") or "",
        "started_at": item.get("started_at").isoformat() if isinstance(item.get("started_at"), datetime) else "",
        "finished_at": item.get("finished_at").isoformat() if isinstance(item.get("finished_at"), datetime) else "",
    }


def youtube_job_result(job: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for item in items:
        if item.get("status") == "SAVED":
            results.append({
                "youtube_video_id": item.get("youtube_video_id"),
                "file_id": item.get("file_id"),
                "title": item.get("title"),
                "status": "DOWNLOADED",
            })
        elif item.get("status") == "FAILED":
            results.append({
                "youtube_video_id": item.get("youtube_video_id"),
                "title": item.get("title"),
                "status": "FAILED",
                "message": item.get("message") or "저장 실패",
            })
    downloaded = sum(1 for item in items if item.get("status") == "SAVED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    return {
        "source_type": "YOUTUBE_DOWNLOAD",
        "scanned_count": int(job.get("item_count") or len(items)),
        "downloaded_count": downloaded,
        "upserted_count": downloaded,
        "skipped_count": 0,
        "failed_count": failed,
        "results": results,
    }


def can_manage_media(user: CurrentUser, item: dict[str, Any]) -> bool:
    return user.is_admin or str(item.get("owner_user_id") or "") == user.user_id


def channel_name(item: dict[str, Any]) -> str:
    owner = str(item.get("owner_user_id") or "creator").strip()
    return f"{owner} 채널"


def karaoke_number(item: dict[str, Any]) -> str:
    values = list(item.get("tags") or [])
    values.extend(item.get("webhard_tags") or [])
    values.extend([item.get("title"), item.get("display_name"), item.get("file_name")])
    for value in values:
        match = re.search(r"KY\.?(\d{3,7})", str(value or ""), flags=re.IGNORECASE)
        if match:
            return f"KY.{match.group(1)}"
        numeric_match = re.fullmatch(r"\d{3,7}", str(value or "").strip())
        if numeric_match:
            return f"KY.{numeric_match.group(0)}"
    return ""


def karaoke_artist(item: dict[str, Any]) -> str:
    if item.get("channel_name"):
        return str(item.get("channel_name"))
    if item.get("album"):
        return str(item.get("album"))
    for tag in list(item.get("tags") or []) + list(item.get("webhard_tags") or []):
        text = str(tag or "").strip()
        if text and not re.match(r"KY\.?\d+", text, flags=re.IGNORECASE) and not re.fullmatch(r"\d{3,7}", text) and not parse_time_marker(text):
            return text
    return ""


def karaoke_time_markers(tags: list[Any]) -> list[dict[str, Any]]:
    markers = []
    seen = set()
    for tag in tags:
        marker = parse_time_marker(str(tag or ""))
        if not marker:
            continue
        key = marker["seconds"]
        if key in seen:
            continue
        seen.add(key)
        markers.append(marker)
    markers.sort(key=lambda item: item["seconds"])
    return markers


def parse_time_marker(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    match = re.search(r"(?:^|[^\d])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d(?:\.\d{1,3})?)(?!\d)", text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    total_seconds = round(hours * 3600 + minutes * 60 + seconds, 3)
    label = re.sub(re.escape(match.group(0)), " ", text, count=1).strip() or text
    return {"seconds": total_seconds, "label": label, "raw": text}


def git_commit() -> str:
    env_commit = os.getenv("GIT_COMMIT") or os.getenv("VITE_GIT_COMMIT")
    if env_commit:
        return env_commit[:12]

    repo_dir = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def int_param(request: HttpRequest, name: str, default: int) -> int:
    try:
        return int(request.GET.get(name) or default)
    except ValueError:
        return default


def karaoke_search_terms(keyword: str) -> list[str]:
    text = str(keyword or "").strip()
    terms = [text] if text else []
    match = re.fullmatch(r"(?:KY\.?)?(\d{1,7})", text, flags=re.IGNORECASE)
    if match:
        number = match.group(1)
        padded_numbers = [number]
        for width in (4, 5, 6, 7):
            if len(number) < width:
                padded_numbers.append(number.zfill(width))
        for value in padded_numbers:
            terms.extend([value, f"KY.{value}", f"KY{value}"])
    result = []
    for term in terms:
        if term and term not in result:
            result.append(term)
    return result


def is_remote_rate_limited(session: dict[str, Any]) -> bool:
    now = datetime.utcnow()
    recent_count = 0
    for command in session.get("commands") or []:
        created_at = command.get("created_at")
        if isinstance(created_at, datetime) and (now - created_at).total_seconds() <= 10:
            recent_count += 1
    return recent_count >= 20


def sanitize_remote_payload(user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else None
    result: dict[str, Any] = {}
    if item:
        try:
            webhard_file_id = int(item.get("webhard_file_id") or 0)
        except (TypeError, ValueError):
            webhard_file_id = 0
        media_item = media_collection().find_one(readable_media_query(user, webhard_file_id)) if webhard_file_id else None
        if media_item:
            result["item"] = serialize_media(media_item)
    return result


def serialize_remote_command(command: dict[str, Any]) -> dict[str, Any]:
    result = dict(command)
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
    return result


def optional_seconds(value: Any) -> float | bool | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    if not 0 <= parsed <= 24 * 60 * 60:
        return False
    return parsed


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def normalize_tags(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").split(",")
    result = []
    for item in items:
        tag = str(item).strip()
        if tag and tag not in result:
            result.append(tag[:40])
    return result[:30]


def normalize_import_tags(value: Any) -> list[str]:
    tags = ["youtube"]
    for tag in normalize_tags(value):
        if tag not in tags:
            tags.append(tag)
    return tags[:30]


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    allowed_hosts = {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed_hosts


def is_safe_youtube_video_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,80}", str(value or "").strip()))


def youtube_import_max_items() -> int:
    try:
        return min(max(int(settings.MEDIA_CONFIG.get("YOUTUBE_IMPORT_MAX_ITEMS") or 100), 1), 500)
    except (TypeError, ValueError):
        return 100


def youtube_running_count(job: dict[str, Any]) -> int:
    return sum(1 for item in job.get("items") or [] if item.get("status") == "RUNNING")
