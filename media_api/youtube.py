import hashlib
import json
import mimetypes
import platform
import re
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests
from django.conf import settings
from .auth import CurrentUser
from .mongo import media_collection
from .webhard import check_webhard_internal_ready, register_youtube_file, sync_one_from_webhard

def preview_youtube(url: str) -> dict[str, Any]:
    info = load_youtube_info(url)
    items = extract_video_items(info)
    return {
        "source_type": info.get("_type") or "video",
        "title": info.get("title") or "",
        "playlist_id": info.get("id") if (info.get("_type") or "").lower() == "playlist" else "",
        "playlist_title": info.get("title") if (info.get("_type") or "").lower() == "playlist" else "",
        "items": items,
        "item_count": len(items),
    }

def import_youtube(url: str, current_user: CurrentUser, token: str, tags: list[str] | None = None) -> dict[str, Any]:
    if not token:
        raise RuntimeError("login token is required for webhard upload")
    info = load_youtube_info(url)
    items = extract_video_items(info)
    playlist_title = str(info.get("title") or "") if (info.get("_type") or "").lower() == "playlist" else ""
    playlist_id = str(info.get("id") or "") if (info.get("_type") or "").lower() == "playlist" else ""
    base_tags = normalize_import_tags(tags)
    downloaded = 0
    skipped = 0
    failed = 0
    results = []
    for item in items:
        video_id = str(item.get("youtube_video_id") or "").strip()
        if not video_id:
            skipped += 1
            continue
        try:
            result = import_youtube_item(item, current_user, base_tags, playlist_id, playlist_title)
            downloaded += 1
            results.append(result)
        except Exception as exc:
            failed += 1
            results.append({
                "youtube_video_id": video_id,
                "title": item.get("title") or video_id,
                "status": "FAILED",
                "message": str(exc)[:500],
            })
    if downloaded == 0 and failed > 0:
        first_failure = next((item for item in results if item.get("status") == "FAILED"), {})
        raise RuntimeError(str(first_failure.get("message") or "youtube download failed"))
    return {
        "source_type": "YOUTUBE_DOWNLOAD",
        "scanned_count": len(items),
        "downloaded_count": downloaded,
        "upserted_count": downloaded,
        "skipped_count": skipped,
        "failed_count": failed,
        "results": results,
    }

def import_youtube_item(
    item: dict[str, Any],
    current_user: CurrentUser,
    tags: list[str],
    playlist_id: str = "",
    playlist_title: str = "",
) -> dict[str, Any]:
    video_id = str(item.get("youtube_video_id") or "").strip()
    if not video_id:
        raise RuntimeError("youtube video id is required")
    downloaded_file = download_youtube_video(item)
    try:
        upload = save_to_webhard_storage(downloaded_file, item, current_user)
        file_id = int(upload.get("file_id") or 0)
        if file_id <= 0:
            raise RuntimeError("webhard upload response does not include file_id")
        sync_one_from_webhard(current_user, file_id)
        apply_youtube_metadata(file_id, item, playlist_id, playlist_title, tags)
        return {
            "youtube_video_id": video_id,
            "file_id": file_id,
            "title": item.get("title") or video_id,
            "status": "DOWNLOADED",
        }
    finally:
        cleanup_download_dir(video_id)

def check_download_tools() -> dict[str, Any]:
    yt_dlp = check_yt_dlp()
    ffmpeg = check_ffmpeg(auto_install=True)
    webhard = check_webhard()
    required_ok = yt_dlp["installed"] and ffmpeg["installed"] and webhard["installed"]
    return {
        "ok_to_download": bool(required_ok),
        "tools": {
            "yt_dlp": yt_dlp,
            "ffmpeg": ffmpeg,
            "webhard": webhard,
        },
    }

def check_webhard() -> dict[str, Any]:
    base_url = str(settings.MEDIA_CONFIG.get("WEBHARD_INTERNAL_BASE_URL") or settings.MEDIA_CONFIG.get("WEBHARD_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base_url:
        return {
            "name": "webhard",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": "",
            "is_latest": None,
            "message": "webhard base url is not configured",
        }
    try:
        check_webhard_internal_ready()
        return {
            "name": "webhard",
            "installed": True,
            "path": base_url,
            "version": "ready",
            "latest_version": "",
            "is_latest": None,
            "message": "webhard internal api is ready",
        }
    except Exception as exc:
        return {
            "name": "webhard",
            "installed": False,
            "path": base_url,
            "version": "down",
            "latest_version": "",
            "is_latest": None,
            "message": f"webhard is not reachable: {exc}",
        }

def check_yt_dlp() -> dict[str, Any]:
    command = yt_dlp_command()
    if not command:
        return {
            "name": "yt-dlp",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": latest_yt_dlp_version(),
            "is_latest": False,
            "message": "yt-dlp is not installed",
        }
    version = command_output([command, "--version"])
    latest = latest_yt_dlp_version()
    is_latest = None if not latest else version_key(version) >= version_key(latest)
    return {
        "name": "yt-dlp",
        "installed": True,
        "path": command,
        "version": version,
        "latest_version": latest,
        "is_latest": is_latest,
        "message": "ok" if is_latest is not False else f"latest yt-dlp is {latest}",
    }

def check_ffmpeg(auto_install: bool = False) -> dict[str, Any]:
    command = ffmpeg_command()
    installed_by_service = False
    install_message = ""
    if not command and auto_install:
        installed = install_ffmpeg()
        command = installed.get("path") or ""
        installed_by_service = bool(command)
        install_message = installed.get("message") or ""
    if not command:
        return {
            "name": "ffmpeg",
            "installed": False,
            "path": "",
            "version": "",
            "latest_version": latest_ffmpeg_release(),
            "is_latest": False,
            "installed_by_service": False,
            "message": install_message or "ffmpeg is not installed",
        }
    first_line = command_output([command, "-version"]).splitlines()[0:1]
    version = first_line[0] if first_line else ""
    latest = latest_ffmpeg_release()
    return {
        "name": "ffmpeg",
        "installed": True,
        "path": command,
        "version": version,
        "latest_version": latest,
        "is_latest": None,
        "installed_by_service": installed_by_service,
        "message": install_message or ("installed; compare release manually" if latest else "installed"),
    }

def load_youtube_info(url: str) -> dict[str, Any]:
    command = yt_dlp_command()
    if not command:
        raise RuntimeError("yt-dlp is not installed")
    limit = int(settings.MEDIA_CONFIG.get("YOUTUBE_IMPORT_LIMIT") or 200)
    result = subprocess.run(
        [
            command,
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--no-warnings",
            "--ignore-errors",
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError((result.stderr or result.stdout or "youtube analysis failed").strip()[:500])
    import json
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("youtube analysis response is invalid")
    return data

def download_youtube_video(item: dict[str, Any]) -> Path:
    command = yt_dlp_command()
    if not command:
        raise RuntimeError("yt-dlp is not installed")
    video_id = str(item.get("youtube_video_id") or "").strip()
    video_url = str(item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
    target_dir = youtube_download_dir(video_id)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    command_line = [
        command,
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(target_dir / "%(id)s.%(ext)s"),
    ]
    ffmpeg = ffmpeg_command()
    if ffmpeg:
        command_line.extend(["--ffmpeg-location", str(Path(ffmpeg).parent)])
    command_line.append(video_url)
    result = subprocess.run(
        command_line,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60 * 60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "youtube download failed").strip()[-1000:])
    candidates = sorted([path for path in target_dir.glob(f"{safe_glob(video_id)}.*") if path.is_file()], key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        candidates = sorted([path for path in target_dir.iterdir() if path.is_file()], key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        raise RuntimeError("downloaded file was not found")
    return candidates[0]

def upload_to_webhard(path: Path, item: dict[str, Any], token: str) -> dict[str, Any]:
    base_url = str(settings.MEDIA_CONFIG.get("WEBHARD_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("webhard base url is not configured")
    mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    upload_name = safe_file_name(str(item.get("title") or path.stem), str(item.get("youtube_video_id") or path.stem), path.suffix or ".mp4")
    with open(path, "rb") as handle:
        response = requests.post(
            f"{base_url}/file/upload.json",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (upload_name, handle, mime_type)},
            data={"original_created_at": datetime.now(timezone.utc).isoformat()},
            timeout=60 * 30,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"webhard upload response is invalid: {response.status_code}") from exc
    if not response.ok or body.get("ok") is not True:
        raise RuntimeError(str(body.get("message") or f"webhard upload failed: HTTP {response.status_code}"))
    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("webhard upload data is invalid")
    return data

def save_to_webhard_storage(path: Path, item: dict[str, Any], current_user: CurrentUser) -> dict[str, Any]:
    storage_root = webhard_storage_root()
    owner_dir = safe_path_segment(current_user.user_id)
    now = datetime.now(timezone.utc)
    relative_dir = Path(owner_dir) / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
    target_dir = storage_root / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix if path.suffix else ".mp4"
    stored_name = f"{uuid.uuid4()}{suffix.lower()}"
    storage_path = target_dir / stored_name
    assert_path_under_root(storage_path, storage_root)
    thumbnail_path = None
    try:
        shutil.copy2(path, storage_path)
        file_name = safe_file_name(str(item.get("title") or path.stem), str(item.get("youtube_video_id") or path.stem), suffix)
        file_size = storage_path.stat().st_size
        content_type = mimetypes.guess_type(file_name)[0] or "video/mp4"
        content_sha256 = sha256_file(storage_path)
        thumbnail_path = create_direct_video_thumbnail(storage_path, current_user.user_id, now)
        file_id = insert_webhard_file(
            current_user=current_user,
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            storage_path=storage_path,
            public_path=f"/storage/{relative_dir.as_posix()}/{stored_name}",
            thumbnail_path=thumbnail_path,
            original_created_at=now,
            content_sha256=content_sha256,
        )
        return {
            "file_id": file_id,
            "public_path": f"/storage/{relative_dir.as_posix()}/{stored_name}",
            "original_created_at": now.isoformat(),
            "content_kind": "VIDEO",
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
            "content_sha256": content_sha256,
            "duplicate_count": 0,
            "duplicate_files": [],
        }
    except Exception:
        if storage_path.exists():
            storage_path.unlink(missing_ok=True)
        if thumbnail_path and Path(thumbnail_path).exists():
            Path(thumbnail_path).unlink(missing_ok=True)
        raise

def insert_webhard_file(
    current_user: CurrentUser,
    file_name: str,
    file_size: int,
    content_type: str,
    storage_path: Path,
    public_path: str,
    thumbnail_path: Path | None,
    original_created_at: datetime,
    content_sha256: str,
) -> int:
    return register_youtube_file(
        current_user,
        {
            "file_name": file_name,
            "file_size": file_size,
            "content_type": content_type,
            "storage_path": str(storage_path),
            "public_path": public_path,
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
            "media_public_yn": current_user.is_admin,
            "original_created_at": original_created_at.isoformat(),
            "content_sha256": content_sha256,
        },
    )

def create_direct_video_thumbnail(storage_path: Path, owner_user_id: str, created_at: datetime) -> Path | None:
    ffmpeg = ffmpeg_command()
    if not ffmpeg:
        return None
    owner_dir = safe_path_segment(owner_user_id)
    storage_root = webhard_storage_root()
    thumbnail_dir = storage_root / owner_dir / ".thumbs" / str(created_at.year) / f"{created_at.month:02d}" / f"{created_at.day:02d}"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    target = thumbnail_dir / f"{storage_path.stem}.webp"
    assert_path_under_root(target, storage_root)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(storage_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=420:315:force_original_aspect_ratio=increase,crop=420:315",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return target if result.returncode == 0 and target.exists() else None

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def safe_path_segment(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "").strip())
    return normalized or "unknown"

def webhard_storage_root() -> Path:
    configured = str(settings.MEDIA_CONFIG.get("WEBHARD_STORAGE_ROOT") or "").strip()
    if not configured:
        raise RuntimeError("webhard storage root is not configured")
    return Path(configured).resolve()

def assert_path_under_root(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise RuntimeError("resolved file path is outside webhard storage root")

def apply_youtube_metadata(file_id: int, item: dict[str, Any], playlist_id: str, playlist_title: str, tags: list[str]) -> None:
    video_id = str(item.get("youtube_video_id") or "")
    title = str(item.get("title") or video_id)
    media_tags = youtube_media_tags(title, tags)
    media_collection().update_one(
        {"webhard_file_id": file_id},
        {
            "$set": {
                "source_type": "YOUTUBE_DOWNLOAD",
                "owner_is_admin": True,
                "youtube_video_id": video_id,
                "youtube_url": item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                "youtube_playlist_id": playlist_id,
                "youtube_playlist_title": playlist_title,
                "title": title,
                "display_name": title,
                "description": item.get("description") or "",
                "channel_name": item.get("channel_name") or "YouTube",
                "album": playlist_title,
                "tags": media_tags,
                "synced_at": datetime.now(timezone.utc),
            }
        },
    )

def normalize_import_tags(tags: list[str] | None) -> list[str]:
    result = ["youtube"]
    for tag in tags or []:
        normalized = str(tag or "").strip()
        if normalized and normalized not in result:
            result.append(normalized[:40])
    return result[:30]

def youtube_media_tags(title: str, tags: list[str]) -> list[str]:
    result = list(tags)
    if "노래방" not in result:
        return result
    match = re.search(r"\bKY[.\-_ ]?(\d{4,5})\b", title, re.IGNORECASE)
    ky_tag = f"KY.{match.group(1)}" if match else "0000"
    if ky_tag not in result:
        result.append(ky_tag)
    return result[:30]

def youtube_download_dir(video_id: str) -> Path:
    base_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve().parent / "youtube-downloads"
    digest = hashlib.sha1(video_id.encode("utf-8")).hexdigest()[:12]
    safe_id = safe_path_segment(video_id)[:48]
    return base_dir / f"{safe_id}-{digest}"

def cleanup_download_dir(video_id: str) -> None:
    target_dir = youtube_download_dir(video_id)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)

def safe_file_name(title: str, video_id: str, suffix: str) -> str:
    base = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" .")[:160]
    if not base:
        base = video_id or "youtube-video"
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{base} [{video_id}]{ext}" if video_id else f"{base}{ext}"

def safe_glob(value: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", value)

def yt_dlp_command() -> str | None:
    configured = str(settings.MEDIA_CONFIG.get("YOUTUBE_YTDLP_PATH") or "").strip()
    if configured:
        return existing_command(configured)
    return shutil.which("yt-dlp")

def ffmpeg_command() -> str | None:
    configured = str(settings.MEDIA_CONFIG.get("YOUTUBE_FFMPEG_PATH") or "").strip()
    if configured:
        return existing_command(configured)
    bundled = bundled_ffmpeg_command()
    if bundled:
        return bundled
    return shutil.which("ffmpeg")

def existing_command(value: str) -> str | None:
    path = Path(value)
    if path.is_absolute() or "\\" in value or "/" in value:
        return str(path) if path.exists() else None
    return shutil.which(value)

def bundled_ffmpeg_command() -> str | None:
    tool_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve()
    candidates = list(tool_dir.glob("ffmpeg/**/bin/ffmpeg.exe")) + list(tool_dir.glob("ffmpeg/**/ffmpeg.exe"))
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return str(existing[0])

def install_ffmpeg() -> dict[str, str]:
    if platform.system().lower() != "windows":
        return {"path": "", "message": "automatic ffmpeg install is only supported on Windows in this environment"}
    try:
        asset = latest_ffmpeg_windows_asset()
        if not asset:
            return {"path": "", "message": "ffmpeg download asset was not found"}
        tool_dir = Path(str(settings.MEDIA_CONFIG.get("YOUTUBE_TOOL_DIR") or "")).resolve()
        install_dir = tool_dir / "ffmpeg"
        install_dir.mkdir(parents=True, exist_ok=True)
        archive_path = install_dir / "ffmpeg-latest.zip"
        download_file(asset["browser_download_url"], archive_path)
        verify_download_digest(archive_path, str(asset.get("digest") or ""))
        extract_dir = install_dir / "latest"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "r") as archive:
            safe_extract_zip(archive, extract_dir)
        command = bundled_ffmpeg_command()
        if not command:
            return {"path": "", "message": "ffmpeg archive was extracted but ffmpeg.exe was not found"}
        return {"path": command, "message": "ffmpeg was installed automatically"}
    except Exception as exc:
        return {"path": "", "message": f"ffmpeg auto install failed: {exc}"}

def latest_ffmpeg_windows_asset() -> dict[str, Any] | None:
    response = requests.get("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest", timeout=12)
    response.raise_for_status()
    assets = response.json().get("assets") or []
    candidates = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip") and "win64" in name and "gpl" in name and "shared" not in name:
            candidates.append(asset)
    if not candidates:
        return None
    candidates.sort(key=lambda asset: str(asset.get("name") or ""))
    return candidates[0]

def download_file(url: str, target: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(target, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

def verify_download_digest(path: Path, expected_digest: str) -> None:
    prefix = "sha256:"
    if not expected_digest.startswith(prefix):
        raise RuntimeError("ffmpeg download digest is missing")
    expected_sha256 = expected_digest[len(prefix):].lower()
    actual_sha256 = sha256_file(path).lower()
    if actual_sha256 != expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg download digest verification failed")

def safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (root / member.filename).resolve()
        if member_path != root and root not in member_path.parents:
            raise RuntimeError("ffmpeg archive contains an unsafe path")
    archive.extractall(root)

def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return ""
    return (result.stdout or result.stderr or "").strip()

def latest_yt_dlp_version() -> str:
    try:
        response = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=8)
        if response.ok:
            return str((response.json().get("info") or {}).get("version") or "")
    except Exception:
        return ""
    return ""

def latest_ffmpeg_release() -> str:
    try:
        response = requests.get("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest", timeout=8)
        if response.ok:
            return str(response.json().get("tag_name") or response.json().get("name") or "")
    except Exception:
        return ""
    return ""

def version_key(value: str) -> tuple[int, ...]:
    parts = []
    for part in str(value or "").replace("-", ".").split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)

def extract_video_items(info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = info.get("entries")
    if isinstance(entries, list):
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                item = video_item(entry)
                if item.get("youtube_video_id"):
                    result.append(item)
        return result
    item = video_item(info)
    return [item] if item.get("youtube_video_id") else []

def video_item(raw: dict[str, Any]) -> dict[str, Any]:
    video_id = str(raw.get("id") or raw.get("url") or "").strip()
    if "youtube.com/watch" in video_id or "youtu.be/" in video_id:
        video_id = video_id.rstrip("/").split("/")[-1].split("v=")[-1].split("&")[0]
    thumbnail_url = raw.get("thumbnail") or best_thumbnail(raw.get("thumbnails"))
    webpage_url = raw.get("webpage_url") or raw.get("url") or ""
    if video_id and not str(webpage_url).startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "youtube_video_id": video_id,
        "title": raw.get("title") or video_id,
        "description": raw.get("description") or "",
        "duration": raw.get("duration"),
        "channel_name": raw.get("channel") or raw.get("uploader") or "",
        "thumbnail_url": thumbnail_url,
        "webpage_url": webpage_url,
    }

def best_thumbnail(thumbnails: Any) -> str:
    if not isinstance(thumbnails, list) or not thumbnails:
        return ""
    candidates = [item for item in thumbnails if isinstance(item, dict) and item.get("url")]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0), reverse=True)
    return str(candidates[0].get("url") or "")
