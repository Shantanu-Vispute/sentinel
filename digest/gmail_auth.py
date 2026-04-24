from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

TOKEN_PATH = Path(GMAIL_TOKEN_PATH)
CREDENTIALS_PATH = Path(GMAIL_CREDENTIALS_PATH)

def get_gmail_service():
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_PATH}\n"
            "Download your OAuth 2.0 Client ID JSON from Google Cloud Console\n"
            "and save it as state/credentials.json"
        )

    creds = _load_existing_credentials()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing expired token...")
            creds.refresh(Request())
        else:
            print("  Opening browser for OAuth consent...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        _save_credentials(creds)

    return build("gmail", "v1", credentials=creds)

def _load_existing_credentials():
    if TOKEN_PATH.exists():
        return Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    return None

def _save_credentials(creds):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
