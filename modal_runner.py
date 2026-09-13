"""Modal serverless worker for owner-authorized video archiving.

Deploy: modal setup && modal deploy modal_runner.py
Invoke: modal run modal_runner.py --url 'https://youtu.be/VIDEO_ID'
HTTP: POST /archive with {"url": "..."} after `modal deploy`.

The function accepts either Modal secret name when available:
  googlecloud-secret (native GCP integration or user-created secret)
  video-archive-config (legacy/configuration secret)

Expected configuration can be supplied by Modal secrets or environment variables:
  GOOGLE_DRIVE_FOLDER_ID (defaults to the configured archive folder below)
  GOOGLE_SERVICE_ACCOUNT_JSON, SERVICE_ACCOUNT_KEY, or
  GOOGLE_APPLICATION_CREDENTIALS
Optional GitHub metadata publishing:
  GITHUB_TOKEN, GITHUB_REPO, GITHUB_METADATA_PATH
"""
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

# Modal injects the Google Cloud secret into the function environment.
MODAL_SECRETS = [
    modal.Secret.from_name("googlecloud-secret"),
]


def normalize_url(value: str) -> str:
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"
    if not value.startswith(("https://", "http://")):
        raise ValueError("url must be an HTTP(S) URL or a YouTube video ID")
    return value


def _service_account_credentials():
    import os, json
    from google.oauth2 import service_account
    scopes = ['https://www.googleapis.com/auth/drive']
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
                from pathlib import Path
                p = Path(raw)
                if p.is_file():
                    return service_account.Credentials.from_service_account_file(str(p), scopes=scopes)
            except Exception:
                pass
    from google.auth import default as google_auth_default
    creds, _ = google_auth_default(scopes=scopes)
    return creds


def drive_upload(path: Path, metadata: dict[str, Any]) -> str | None:
    # Upload into the configured/shared Drive folder so service accounts do not
    # attempt to write to their quota-less My Drive.
    folder_id = os.getenv("DRIVE_FOLDER_ID", DEFAULT_GOOGLE_DRIVE_FOLDER_ID)
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    service = build("drive", "v3", credentials=_service_account_credentials(), cache_discovery=False)
    file_metadata = {
        "name": path.name,
        "parents": [folder_id],
        "description": json.dumps(metadata),
    }
    media = MediaFileUpload(str(path), resumable=True)
    result = service.files().create(
        body=file_metadata,
        media_body=media,
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
    body = json.dumps(payload).encode()
    update = urllib.request.Request(api, data=body, method="PUT", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(update):
        pass


@app.function(image=image, timeout=3600, secrets=MODAL_SECRETS)
@modal.fastapi_endpoint(method="POST")
def archive(payload: dict[str, Any]) -> dict[str, Any]:
    source = normalize_url(str(payload.get("url") or payload.get("id") or ""))
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "%(id)s.%(ext)s"
        from yt_dlp import YoutubeDL
        options = {"outtmpl": str(output), "format": "bv*+ba/b", "merge_output_format": "mp4", "noplaylist": True, "quiet": True}
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
