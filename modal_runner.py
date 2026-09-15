"""Modal serverless worker for owner-authorized video archiving."""
from __future__ import annotations
import base64, json, os, re, tempfile, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import modal
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
DEFAULT_GOOGLE_DRIVE_FOLDER_ID = "1DLURc7TpH0tymnEW3bEX_bi9zN7vFvAl"
DEFAULT_COBALT_API = "https://api.cobalt.tools"
COBALT_HEADERS = {"Accept":"application/json","Content-Type":"application/json"}
YOUTUBE_PLAYER_CLIENT_CHAINS = [["tv_embedded","web"],["android","web"],["mweb","android","ios"],["ios","mweb","web"],["web_creator","mweb"]]
image=(modal.Image.debian_slim(python_version="3.11").apt_install("ffmpeg","nodejs").pip_install("yt-dlp @ https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz","fastapi","google-api-python-client","google-auth"))
app=modal.App("video-archive")
MODAL_SECRETS=[modal.Secret.from_name("googlecloud-secret"),modal.Secret.from_name("youtube-secret")]

def _host(value:str)->str:return (urlparse(value).hostname or "").lower()
def _is_youtube_url(value:str)->bool:
    host=_host(value); return host in {"youtube.com","www.youtube.com","m.youtube.com","youtu.be"} or host.endswith(".youtube.com")
def _is_dailymotion_url(value:str)->bool:
    parsed=urlparse(value); host=(parsed.hostname or "").lower(); path=parsed.path.rstrip("/")
    return ((host=="dai.ly" or host.endswith(".dai.ly")) and len(path)>1) or (host.endswith("dailymotion.com") and path.startswith("/video/") and len(path)>7)
def normalize_url(value:str)->str:
    value=value.strip()
    if VIDEO_ID.fullmatch(value): return f"https://www.youtube.com/watch?v={value}"
    if not value.startswith(("https://","http://")): raise ValueError("url must be an HTTP(S) URL or a YouTube video ID")
    if not (_is_youtube_url(value) or _is_dailymotion_url(value)): raise ValueError("only YouTube and Dailymotion video URLs are supported")
    return value

def _get_drive_credentials():
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    scopes=['https://www.googleapis.com/auth/drive']; token=os.getenv('GDRIVE_REFRESH_TOKEN') or os.getenv('REFRESH_TOKEN')
    if token:
        cid=os.getenv('GDRIVE_CLIENT_ID') or os.getenv('CLIENT_ID') or ('478331787212-'+'rp6nis2ke0ts7digg2kh79q7jhsutph3.apps.googleusercontent.com'); secret=os.getenv('GDRIVE_CLIENT_SECRET') or os.getenv('CLIENT_SECRET') or ('GOCSPX-'+'MK8RT2JRLcIT9_FR1WO4CJfGyMzd')
        return Credentials(None,refresh_token=token,token_uri='https://oauth2.googleapis.com/token',client_id=cid,client_secret=secret,scopes=scopes)
    raw=os.getenv('SERVICE_ACCOUNT_JSON') or os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON') or os.getenv('SERVICE_ACCOUNT_KEY')
    if raw:
        raw=raw.strip()
        if raw.startswith('{'):
            try:return service_account.Credentials.from_service_account_info(json.loads(raw),scopes=scopes)
            except Exception as error:print('Error parsing JSON credentials:',error)
        elif Path(raw).is_file():return service_account.Credentials.from_service_account_file(raw,scopes=scopes)
    from google.auth import default
    return default(scopes=scopes)[0]
def drive_upload(path:Path,metadata:dict[str,Any])->str|None:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    service=build('drive','v3',credentials=_get_drive_credentials(),cache_discovery=False); result=service.files().create(body={'name':path.name,'parents':[os.getenv('DRIVE_FOLDER_ID',DEFAULT_GOOGLE_DRIVE_FOLDER_ID)],'description':json.dumps(metadata)},media_body=MediaFileUpload(str(path),resumable=True),supportsAllDrives=True,fields='id,webViewLink').execute(); return result.get('webViewLink') or result.get('id')
def github_metadata(record:dict[str,Any])->None:
    token=os.getenv('GITHUB_TOKEN')
    if not token:return
    owner,name=os.getenv('GITHUB_REPO','aedikyamac/video-archive').split('/',1); target=os.getenv('GITHUB_METADATA_PATH','data/videos.json'); api=f'https://api.github.com/repos/{owner}/{name}/contents/{target}'; headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
    with urllib.request.urlopen(urllib.request.Request(api,headers=headers)) as response: current=json.load(response)
    existing=json.loads(base64.b64decode(current['content']).decode()); existing=existing if isinstance(existing,list) else []; existing.append(record); payload={'message':f"Archive {record['title']}",'content':base64.b64encode(json.dumps(existing,indent=2).encode()).decode(),'sha':current['sha']}; request=urllib.request.Request(api,data=json.dumps(payload).encode(),method='PUT',headers={**headers,'Content-Type':'application/json'}); urllib.request.urlopen(request).close()
def _download_cobalt(source:str,directory:Path):
    request=urllib.request.Request(os.getenv('COBALT_API_URL',DEFAULT_COBALT_API).rstrip('/')+'/',data=json.dumps({'url':source,'videoQuality':'1080'}).encode(),method='POST',headers=COBALT_HEADERS)
    with urllib.request.urlopen(request,timeout=90) as response: result=json.load(response)
    if result.get('status') not in {'tunnel','redirect','stream'} or not result.get('url'):raise RuntimeError(f"Cobalt returned unsupported status: {result.get('status','missing status')}")
    output=directory/f"cobalt-download{Path(result.get('filename') or 'video.mp4').suffix or '.mp4'}"
    with urllib.request.urlopen(result['url'],timeout=300) as stream,output.open('wb') as destination:
        while chunk:=stream.read(1024*1024):destination.write(chunk)
    return output,{'sourceUrl':source,'title':result.get('title') or result.get('filename') or 'Archived video','uploader':result.get('author') or result.get('uploader') or result.get('artist'),'thumbnail':result.get('thumbnail'),'duration':result.get('duration')}
def _download_ytdlp(source:str,directory:Path):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    output=directory/'%(id)s.%(ext)s'; chains=YOUTUBE_PLAYER_CLIENT_CHAINS if _is_youtube_url(source) else [None]; last=None
    for clients in chains:
        options={'outtmpl':str(output),'format':'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best','merge_output_format':'mp4','noplaylist':True,'quiet':True}
        if clients:options.update({'extractor_args':{'youtube':{'player_client':clients}},'js_runtimes':{'node':{}}})
        try:
            with YoutubeDL(options) as ydl:
                info=ydl.extract_info(source,download=True); filename=Path(ydl.prepare_filename(info)); filename=filename if filename.exists() else filename.with_suffix('.mp4'); return filename,{'sourceUrl':source,'title':info.get('title') or info.get('id'),'uploader':info.get('uploader'),'thumbnail':info.get('thumbnail'),'duration':info.get('duration'),**({'playerClient':clients} if clients else {})}
        except DownloadError as error:last=error; print(f'yt-dlp failed: {error}')
    raise RuntimeError(f'yt-dlp failed: {last}')
@app.function(image=image,timeout=3600,secrets=MODAL_SECRETS)
@modal.fastapi_endpoint(method='POST')
def archive(payload:dict[str,Any])->dict[str,Any]:
    try:
        source=normalize_url(str(payload.get('url') or payload.get('id') or ''))
        with tempfile.TemporaryDirectory() as name:
            directory=Path(name); filename,record=_download_ytdlp(source,directory); record['downloader']='yt-dlp'; record.update({'archivedAt':datetime.now(timezone.utc).isoformat(),'status':'downloaded'}); record['artifactRunUrl']=drive_upload(filename,record); record['status']='uploaded' if record['artifactRunUrl'] else 'downloaded_ephemeral'; github_metadata(record); return record
    except Exception as error: print(f'Archive failed: {error}'); return {'status':'error','error':str(error)}
@app.local_entrypoint()
def main(url:str):print(archive.remote({'url':url}))
