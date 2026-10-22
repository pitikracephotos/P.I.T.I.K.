import io
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_URL = "https://findmyshots.com"
EVENTS_URL = f"{BASE_URL}/events"
FIREBASE_BASE_URL_TEMPLATE = (
    "https://firebasestorage.googleapis.com/v0/b/"
    "proj-lamatsapitik-458606.firebasestorage.app/o/"
    "events%2F{event_id}%2Fphotos%2F"
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yml"
TOKEN_FILE = BASE_DIR / "token.json"
WORK_DIR = BASE_DIR / "work"
DOWNLOADS_DIR = BASE_DIR / "downloads"

# Google Drive folder IDs: configure these here instead of in config.yml.
INPUT_FOLDER_ID = "11zh8F_XGE0xeRvCx9yRM4ikWtiysQTP8"
OUTPUT_FOLDER_ID = "1dveODIwUwxLT77dsGLHZbkKRC0M6I06s"

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_config() -> dict:
    """Load and validate config.yml."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Missing configuration file: {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    search_term = config.get("search_term")
    upload_images = config.get("upload_images")
    if not isinstance(search_term, str) or not search_term.strip():
        raise ValueError("config.yml must contain a non-empty search_term.")

    if not isinstance(upload_images, list) or not upload_images:
        raise ValueError(
            "config.yml must contain at least one filename under upload_images."
        )

    if not all(isinstance(name, str) and name.strip() for name in upload_images):
        raise ValueError("Every upload_images entry must be a non-empty filename.")

    if not INPUT_FOLDER_ID or "YOUR_" in INPUT_FOLDER_ID:
        raise ValueError("Set INPUT_FOLDER_ID in main.py.")
    if not OUTPUT_FOLDER_ID or "YOUR_" in OUTPUT_FOLDER_ID:
        raise ValueError("Set OUTPUT_FOLDER_ID in main.py.")

    # Preserve order but avoid processing a filename more than once.
    config["upload_images"] = list(dict.fromkeys(name.strip() for name in upload_images))
    config["search_term"] = search_term.strip()
    return config


def get_drive_service():
    """Create a Google Drive API client using the locally restored OAuth token."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            "token.json is missing. The GitHub workflow should create it from "
            "the GOOGLE_OAUTH_TOKEN repository secret."
        )

    credentials = Credentials.from_authorized_user_file(
        str(TOKEN_FILE),
        scopes=SCOPES,
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")

    if not credentials.valid:
        raise RuntimeError(
            "Google OAuth credentials are not valid. Re-authorize Google Drive "
            "and update the GOOGLE_OAUTH_TOKEN GitHub secret."
        )
