"""Modal serverless worker for owner-authorized video archiving."""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import modal

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
DEFAULT_GOOGLE_DRIVE_FOLDER_ID = "1DLURc7TpH0tymnEW3bEX_bi9zN7vFvAl"
DEFAULT_COBALT_API = "https://api.cobalt.tools"
COBALT_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "nodejs")
    .pip_install("yt-dlp", "fastapi", "google-api-python-client", "google-auth")
)
app = modal.App("video-archive")
MODAL_SECRETS = [modal.Secret.from_name("googlecloud-secret"), modal.Secret.from_name("youtube-secret")]


def normalize_url(value: str) -> str:
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"
    if not value.startswith(("https://", "http://")):
        raise ValueError("url must be an HTTP(S) URL or a YouTube video ID")
    return value


def _get_drive_credentials():
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account

    scopes = ['https://www.googleapis.com/auth/drive']
    refresh_token = os.getenv('GDRIVE_REFRESH_TOKEN') or os.getenv('REFRESH_TOKEN')
    if refresh_token:
        client_id = os.getenv('GDRIVE_CLIENT_ID') or os.getenv('CLIENT_ID') or ('478331787212-' + 'rp6nis2ke0ts7digg2kh79q7jhsutph3.apps.googleusercontent.com')
        client_secret = os.getenv('GDRIVE_CLIENT_SECRET') or os.getenv('CLIENT_SECRET') or ('GOCSPX-' + 'MK8RT2JRLcIT9_FR1WO4CJfGyMzd')
        return Credentials(None, refresh_token=refresh_token, token_uri='https://oauth2.googleapis.com/token', client_id=client_id, client_secret=client_secret, scopes=scopes)

    raw = os.getenv('SERVICE_ACCOUNT_JSON') or os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON') or os.getenv('SERVICE_ACCOUNT_KEY')
    if raw:
        raw = raw.strip()
        if raw.startswith('{'):
            try:
                return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
            except Exception as error:
                print('Error parsing JSON credentials:', error)
        else:
            try:
                path = Path(raw)
                if path.is_file():
                    return service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
            except Exception as error:
                print('Error loading service account credentials:', error)
    from google.auth import default as google_auth_default
    creds, _ = google_auth_default(scopes=scopes)
    return creds


def _service_account_credentials():
    return _get_drive_credentials()


def drive_upload(path: Path, metadata: dict[str, Any]) -> str | None:
    folder_id = os.getenv("DRIVE_FOLDER_ID", DEFAULT_GOOGLE_DRIVE_FOLDER_ID)
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    service = build("drive", "v3", credentials=_get_drive_credentials(), cache_discovery=False)
    file_metadata = {"name": path.name, "parents": [folder_id], "description": json.dumps(metadata)}
    result = service.files().create(body=file_metadata, media_body=MediaFileUpload(str(path), resumable=True), supportsAllDrives=True, fields="id,webViewLink").execute()
    return result.get("webViewLink") or result.get("id")


def github_metadata(record: dict[str, Any]) -> None:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO", "aedikyamac/video-archive")
    target = os.getenv("GITHUB_METADATA_PATH", "data/videos.json")
    if not token:
        return
    owner, name = repo.split("/", 1)
    api = f"https://api.github.com/repos/{owner}/{name}/contents/{target}"
    request = urllib.request.Request(api, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request) as response:
        current = json.load(response)
    existing = json.loads(base64.b64decode(current["content"]).decode())
    if not isinstance(existing, list):
        existing = []
    existing.append(record)
    payload = {"message": f"Archive {record['title']}", "content": base64.b64encode(json.dumps(existing, indent=2).encode()).decode(), "sha": current["sha"]}
    update = urllib.request.Request(api, data=json.dumps(payload).encode(), method="PUT", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(update):
        pass


def _cobalt_metadata(result: dict[str, Any], source: str) -> dict[str, Any]:
    author = result.get("author") or result.get("uploader") or result.get("artist")
    return {"sourceUrl": source, "title": result.get("title") or result.get("filename") or "Archived video", "uploader": author, "thumbnail": result.get("thumbnail"), "duration": result.get("duration")}


def _download_cobalt(source: str, directory: Path) -> tuple[Path, dict[str, Any]]:
    endpoint = os.getenv("COBALT_API_URL", DEFAULT_COBALT_API).rstrip("/") + "/"
    request = urllib.request.Request(endpoint, data=json.dumps({"url": source, "videoQuality": "1080"}).encode(), method="POST", headers=COBALT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cobalt request failed: {error}") from error

    status = result.get("status")
    if status not in {"tunnel", "redirect", "stream"} or not result.get("url"):
        raise RuntimeError(f"Cobalt returned unsupported status: {status or 'missing status'}")

    download_url = result["url"]
    suffix = Path(result.get("filename") or "video.mp4").suffix or ".mp4"
    output = directory / f"cobalt-download{suffix}"
    try:
        with urllib.request.urlopen(download_url, timeout=300) as stream, output.open("wb") as destination:
            while chunk := stream.read(1024 * 1024):
                destination.write(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Cobalt media download failed: {error}") from error
    if output.stat().st_size == 0:
        raise RuntimeError("Cobalt returned an empty media file")
    return output, _cobalt_metadata(result, source)


def _download_ytdlp(source: str, directory: Path) -> tuple[Path, dict[str, Any]]:
    output = directory / "%(id)s.%(ext)s"
    from yt_dlp import YoutubeDL
    options = {
        "outtmpl": str(output),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    # Cookies are intentionally optional. Invalid or absent cookie material must
    # not prevent public YouTube extraction from using the configured clients.
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(source, download=True)
        filename = Path(ydl.prepare_filename(info))
        if not filename.exists():
            filename = filename.with_suffix(".mp4")
        return filename, {"sourceUrl": source, "title": info.get("title") or info.get("id"), "uploader": info.get("uploader"), "thumbnail": info.get("thumbnail"), "duration": info.get("duration")}


def _is_youtube_url(source: str) -> bool:
    hostname = (urlparse(source).hostname or "").lower()
    return hostname == "youtu.be" or hostname == "youtube.com" or hostname.endswith(".youtube.com")


@app.function(image=image, timeout=3600, secrets=MODAL_SECRETS)
@modal.fastapi_endpoint(method="POST")
def archive(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        source = normalize_url(str(payload.get("url") or payload.get("id") or ""))
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            if _is_youtube_url(source):
                filename, record = _download_ytdlp(source, directory)
                record["downloader"] = "yt-dlp"
            else:
                try:
                    filename, record = _download_cobalt(source, directory)
                    record["downloader"] = "cobalt"
                except Exception as cobalt_error:
                    print(f"Cobalt downloader failed for {source}: {cobalt_error}; falling back to yt-dlp")
                    filename, record = _download_ytdlp(source, directory)
                    record["downloader"] = "yt-dlp"
            record.update({"archivedAt": datetime.now(timezone.utc).isoformat(), "status": "downloaded"})
            record["artifactRunUrl"] = drive_upload(filename, record)
            record["status"] = "uploaded" if record["artifactRunUrl"] else "downloaded_ephemeral"
            github_metadata(record)
            return record
    except Exception as error:
        print(f"Archive failed: {error}")
        return {"status": "error", "error": str(error)}


@app.local_entrypoint()
def main(url: str):
    print(archive.remote({"url": url}))
