"""Renew the Gmail refresh token (OAuth consent screen in Testing expires it every 7 days).

Opens a browser consent page, then updates .env and env.yaml, resets the reminder date in the
sheet State tab, and optionally pushes the new token to Cloud Run.
"""

import argparse
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from gmail_service import SCOPES, TOKEN_URI

TOKEN_KEY = "GMAIL_REFRESH_TOKEN"
REFRESHED_KEY = "gmail_token_refreshed_at"
BACKUP_DIR = ".bak_logs"


def replace_env_value(text: str, key: str, value: str, separator: str, quoted: bool) -> str:
    """Set key to value in dotenv ("=") or YAML (": ") text, appending the key if absent."""
    rendered = f'"{value}"' if quoted else value
    new_line = f"{key}{separator}{rendered}"
    pattern = re.compile(rf"^{re.escape(key)}\s*{re.escape(separator.strip())}.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: new_line, text, count=1)
    prefix = text if text.endswith("\n") or not text else text + "\n"
    return f"{prefix}{new_line}\n"


def backup_file(path: Path, backup_dir: Path, now: datetime) -> Path:
    backup_dir.mkdir(exist_ok=True)
    target = backup_dir / f"{path.name}.{now:%Y-%m-%d_%H%M%S}.bak"
    shutil.copy2(path, target)
    return target


def update_file(
    path: Path, value: str, separator: str, quoted: bool, backup_dir: Path = Path(BACKUP_DIR)
) -> Path | None:
    """Rewrite the token in path after backing it up. Returns the backup path, or None if absent."""
    if not path.exists():
        return None
    backup = backup_file(path, backup_dir, datetime.now())
    original = path.read_text()
    path.write_text(replace_env_value(original, TOKEN_KEY, value, separator, quoted))
    return backup


def authorize(client_id: str, client_secret: str, port: int) -> str:
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
                "redirect_uris": [f"http://localhost:{port}/"],
            }
        },
        SCOPES,
    )
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise SystemExit("Google did not return a refresh token. Try again.")
    return creds.refresh_token


def verify(client_id: str, client_secret: str, refresh_token: str) -> str:
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def record_refresh_date() -> None:
    from sheets_service import SheetsService

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    service_account = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not (sheet_id and service_account):
        raise RuntimeError("GOOGLE_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON not set")
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Singapore").strip())
    SheetsService(sheet_id, service_account).save_state(
        REFRESHED_KEY, datetime.now(tz).date().isoformat()
    )


def push_to_cloud_run(token: str, service: str, region: str) -> None:
    subprocess.run(
        [
            "gcloud", "run", "services", "update", service,
            "--region", region,
            "--update-env-vars", f"{TOKEN_KEY}={token}",
        ],
        check=True,
    )  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    deploy = parser.add_mutually_exclusive_group()
    deploy.add_argument("--deploy", action="store_true", help="push to Cloud Run without asking")
    deploy.add_argument("--no-deploy", action="store_true", help="never push to Cloud Run")
    parser.add_argument("--port", type=int, default=8080, help="local callback port")
    parser.add_argument("--service", default="finance-agent")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--yaml-file", default="env.yaml")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        raise SystemExit("GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in the env file.")

    print(f"Redirect URI in use: http://localhost:{args.port}/")
    token = authorize(client_id, client_secret, args.port)
    print(f"Authorized Gmail account: {verify(client_id, client_secret, token)}")

    for path, separator, quoted in ((args.env_file, "=", False), (args.yaml_file, ": ", True)):
        backup = update_file(Path(path), token, separator, quoted)
        if backup:
            print(f"Updated {path} (backup: {backup})")

    try:
        record_refresh_date()
        print("Recorded refresh date in the sheet State tab.")
    except Exception as exc:  # token files are already saved; don't fail the run
        print(f"Warning: could not record refresh date ({exc}). Send /gmailrefreshed instead.")

    if args.no_deploy:
        return
    if args.deploy or input("Push to Cloud Run now? [y/N] ").strip().lower() == "y":
        push_to_cloud_run(token, args.service, args.region)
        print("Cloud Run updated.")


if __name__ == "__main__":
    main()
