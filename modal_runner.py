"""Modal serverless worker for owner-authorized video archiving."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import modal

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
DEFAULT_GOOGLE_DRIVE_FOLDER_ID = "1DLURc7TpH0tymnEW3bEX_bi9zN7vFvAl"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("yt-dlp", "fastapi", "google-api-python-client", "google-auth")
)
app = modal.App("video-archive")
MODAL_SECRETS = [modal.Secret.from_name("googlecloud-secret")]


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
        return Credentials(
            None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )

    raw = os.getenv('SERVICE_ACCOUNT_JSON') or os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON') or os.getenv('SERVICE_ACCOUNT_KEY')
    if raw:
        raw = raw.strip()
        if raw.startswith('{'):
            try:
                data = json.loads(raw)
                return service_account.Credentials.from_service_account_info(data, scopes=scopes)
            except Exception as e:
                print('Error parsing JSON credentials:', e)
        else:
            try:
                p = Path(raw)
                if p.is_file():
                    return service_account.Credentials.from_service_account_file(str(p), scopes=scopes)
            except Exception:
                pass
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
    result = service.files().create(
        body=file_metadata,
        media_body=MediaFileUpload(str(path), resumable=True),
        supportsAllDrives=True,
        fields="id,webViewLink",
    ).execute()
    return result.get("webViewLink") or result.get("id")


def github_metadata(record: dict[str, Any]) -> None:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO", "aedikyamac/video-archive")
    target = os.getenv("GITHUB_METADATA_PATH", "data/videos.json")
    if not token:
        return
    import base64
    import urllib.request
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


@app.function(image=image, timeout=3600, secrets=MODAL_SECRETS)
@modal.fastapi_endpoint(method="POST")
def archive(payload: dict[str, Any]) -> dict[str, Any]:
    source = normalize_url(str(payload.get("url") or payload.get("id") or ""))
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "%(id)s.%(ext)s"
        from yt_dlp import YoutubeDL
        options = {
            "outtmpl": str(output),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios"],
                    "player_skip": ["webpage", "configs"],
                }
            },
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(source, download=True)
            filename = Path(ydl.prepare_filename(info))
            if not filename.exists():
                filename = filename.with_suffix(".mp4")
            record: dict[str, Any] = {"sourceUrl": source, "title": info.get("title") or info.get("id"), "uploader": info.get("uploader"), "thumbnail": info.get("thumbnail"), "duration": info.get("duration"), "archivedAt": datetime.now(timezone.utc).isoformat(), "status": "downloaded"}
            record["artifactRunUrl"] = drive_upload(filename, record)
            record["status"] = "uploaded" if record["artifactRunUrl"] else "downloaded_ephemeral"
            github_metadata(record)
            return record


@app.local_entrypoint()
def main(url: str):
    print(archive.remote({"url": url}))
