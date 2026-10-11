"""Optional Google Drive upload with an "anyone with the link can view" share link.

Two auth modes:
  * Service account (GOOGLE_SERVICE_ACCOUNT_FILE) - the target GDRIVE_FOLDER_ID must
    live in a Shared Drive the service account is a member of (service accounts
    have no storage quota of their own).
  * OAuth user token (GOOGLE_OAUTH_CLIENT_FILE + `python -m paperagent drive-auth`)
    - uploads into your own My Drive.
If neither is configured, uploads are skipped and the dashboard links to the
local copy and the original paper page instead."""
import logging
import os

from . import config

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_service = None


def _credentials():
    if config.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    if os.path.exists(config.GOOGLE_OAUTH_TOKEN_FILE):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(config.GOOGLE_OAUTH_TOKEN_FILE, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(config.GOOGLE_OAUTH_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return creds
    return None


def enabled() -> bool:
    return bool(config.GDRIVE_FOLDER_ID) and (
        (config.GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE))
        or os.path.exists(config.GOOGLE_OAUTH_TOKEN_FILE)
    )


def _drive():
    global _service
    if _service is None:
        from googleapiclient.discovery import build

        _service = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    return _service


def upload_and_share(path, name=None, mime="application/pdf") -> str:
    """Upload a file into GDRIVE_FOLDER_ID and return its public view link."""
    from googleapiclient.http import MediaFileUpload

    drive = _drive()
    name = name or os.path.basename(path)
    meta = {"name": name, "parents": [config.GDRIVE_FOLDER_ID]}
    f = (
        drive.files()
        .create(body=meta, media_body=MediaFileUpload(str(path), mimetype=mime), fields="id, webViewLink",
                supportsAllDrives=True)
        .execute()
    )
    drive.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}, supportsAllDrives=True
    ).execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"


def run_oauth_flow():
    """Interactive one-time login that stores a refreshable user token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_OAUTH_CLIENT_FILE:
        raise SystemExit("Set GOOGLE_OAUTH_CLIENT_FILE to your OAuth client JSON (Desktop app) first.")
    flow = InstalledAppFlow.from_client_secrets_file(config.GOOGLE_OAUTH_CLIENT_FILE, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False)
    with open(config.GOOGLE_OAUTH_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Saved token to {config.GOOGLE_OAUTH_TOKEN_FILE}")
