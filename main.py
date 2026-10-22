import io
import mimetypes
import re
import time
from pathlib import Path

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
FIREBASE_BASE_URL = (
    "https://firebasestorage.googleapis.com/v0/b/"
    "proj-lamatsapitik-458606.firebasestorage.app/o/"
    "events%2F3fd1e949-d871-4e82-a45a-66ef79ebc562%2Fphotos%2F"
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yml"
TOKEN_FILE = BASE_DIR / "token.json"
WORK_DIR = BASE_DIR / "work"
DOWNLOADS_DIR = BASE_DIR / "downloads"

# Google Drive folder IDs: configure these here instead of in config.yml.
INPUT_FOLDER_ID = "YOUR_INPUT_FOLDER_ID"
OUTPUT_FOLDER_ID = "YOUR_OUTPUT_FOLDER_ID"

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

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_drive_files(service, folder_id: str) -> list[dict]:
    """List all non-trashed files directly inside a Drive folder."""
    files = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000,
                pageToken=page_token,
                orderBy="name",
                fields="nextPageToken, files(id, name, mimeType, size)",
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def download_drive_file(service, file_id: str, destination: Path) -> Path:
    """Download a Drive file to the given local path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)

    with destination.open("wb") as file_handle:
        downloader = MediaIoBaseDownload(file_handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return destination


def get_or_create_drive_folder(service, parent_id: str, folder_name: str) -> str:
    """Find a child folder by name, or create it if it doesn't exist."""
    escaped_name = folder_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and "
        f"mimeType = '{DRIVE_FOLDER_MIME}' and "
        f"'{parent_id}' in parents and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            pageSize=100,
            fields="files(id, name)",
        )
        .execute()
    )
    folders = response.get("files", [])
    if folders:
        return folders[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": DRIVE_FOLDER_MIME,
        "parents": [parent_id],
    }
    created = service.files().create(
        body=metadata,
        fields="id",
    ).execute()
    print(f"Created Google Drive folder: {folder_name}")
    return created["id"]


def upload_file_to_drive(service, local_path: Path, folder_id: str) -> None:
    """Upload a file, replacing a same-named file in the destination folder."""
    escaped_name = local_path.name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and "
        f"'{folder_id}' in parents and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            pageSize=100,
            fields="files(id, name)",
        )
        .execute()
    )
    existing = response.get("files", [])
    mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(
        str(local_path),
        mimetype=mime_type,
        resumable=True,
    )

    if existing:
        service.files().update(
            fileId=existing[0]["id"],
            media_body=media,
        ).execute()
        print(f"Updated Drive file: {local_path.name}")
    else:
        service.files().create(
            body={"name": local_path.name, "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
        print(f"Uploaded to Drive: {local_path.name}")


def upload_folder_contents(service, local_folder: Path, drive_folder_id: str) -> None:
    """Upload the files in one runner's local result folder to its Drive folder."""
    if not local_folder.exists():
        return

    for path in sorted(local_folder.iterdir()):
        if path.is_file():
            upload_file_to_drive(service, path, drive_folder_id)


def extract_filename(content_disposition: str | None) -> str | None:
    """Extract filename from Content-Disposition header."""
    if not content_disposition:
        return None

    match = re.search(
        r'filename="([^"]+)"',
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        filename = match.group(1)
        if filename.lower().endswith(".webp"):
            filename = filename[:-5] + ".JPG"
        return filename

    # Also handle unquoted filename values.
    match = re.search(
        r"filename=([^;]+)",
        content_disposition,
        re.IGNORECASE,
    )
    if match:
        filename = match.group(1).strip().strip('"')
        if filename.lower().endswith(".webp"):
            filename = filename[:-5] + ".JPG"
        return filename

    return None


def get_original_filename(context: BrowserContext, image_url: str) -> str | None:
    """Open an image URL and capture its Content-Disposition filename."""
    image_page = context.new_page()

    try:
        filename = None

        def handle_response(response):
            nonlocal filename
            extracted = extract_filename(
                response.headers.get("content-disposition")
            )
            if extracted:
                filename = extracted

        image_page.on("response", handle_response)
        image_page.goto(
            image_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        image_page.wait_for_timeout(2_000)
        return filename
    finally:
        image_page.close()


def download_original_image(
    context: BrowserContext,
    filename: str,
    download_dir: Path,
) -> Path:
    """Download the original image from Firebase Storage."""
    download_dir.mkdir(parents=True, exist_ok=True)
    output_path = download_dir / filename

    if output_path.exists():
        print(f"Already downloaded, skipping: {filename}")
        return output_path

    firebase_url = f"{FIREBASE_BASE_URL}{filename}?alt=media"
    print(f"Trying Firebase URL for: {filename}")
    response = context.request.get(firebase_url, timeout=60_000)

    if not response.ok and filename.lower().endswith(".jpg"):
        alternate_filename = (
            filename[:-4] + ".JPG"
            if filename.endswith(".jpg")
            else filename[:-4] + ".jpg"
        )
        alternate_output_path = download_dir / alternate_filename

        if alternate_output_path.exists():
            print(f"Already downloaded, skipping: {alternate_filename}")
            return alternate_output_path

        alternate_url = f"{FIREBASE_BASE_URL}{alternate_filename}?alt=media"
        print(f"Original URL failed. Retrying with alternate capitalization.")
        response = context.request.get(alternate_url, timeout=60_000)

        if response.ok:
            filename = alternate_filename
            output_path = alternate_output_path

    if not response.ok:
        raise RuntimeError(
            f"Failed to download {filename}: HTTP {response.status}"
        )

    output_path.write_bytes(response.body())
    return output_path


def process_event(
    context: BrowserContext,
    event_url: str,
    event_index: int,
    upload_image_path: Path,
    download_dir: Path,
) -> None:
    """Upload one reference image to one event and download matching photos."""
    page = context.new_page()

    try:
        print(f"\n{'=' * 70}")
        print(f"EVENT {event_index}")
        print(event_url)
        print(f"Reference image: {upload_image_path.name}")
        print(f"{'=' * 70}")

        page.goto(
            event_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        plus_button = page.locator(
            'button:has(svg path[d*="M12 3.75"])'
        ).first
        plus_button.wait_for(state="visible", timeout=30_000)
        plus_button.click()

        checkbox = page.locator("#accept-terms-selfie")
        checkbox.wait_for(state="visible", timeout=30_000)
        checkbox.check()

        with page.expect_file_chooser() as file_chooser_info:
            page.get_by_role("button", name="Select an image").click()
        file_chooser_info.value.set_files(str(upload_image_path))

        agree_button = page.get_by_role(
            "button",
            name="Agree and Upload",
        )
        agree_button.wait_for(state="visible", timeout=30_000)
        agree_button.click()

        page.locator("#folder-content-main").wait_for(
            state="visible",
            timeout=60_000,
        )

        all_photos_message = page.locator("p").filter(
            has_text="You've seen all photos!"
        )
        loading_more_message = page.locator("span.text-sm").filter(
            has_text="Loading more photos..."
        )
        image_locator = page.locator("#folder-content-main img")

        print("Checking if more photos need to be loaded...")

        if all_photos_message.is_visible():
            print("All photos are already loaded.")
        else:
            page.evaluate("""
                () => {
                    window.scrollTo(0, document.body.scrollHeight);
                    const container = document.querySelector(
                        "#folder-content-main"
                    );
                    if (container) {
                        container.scrollTop = container.scrollHeight;
                    }
                }
            """)

            try:
                loading_more_message.wait_for(state="visible", timeout=3_000)
                has_more_photos = True
                print("Loading more photos detected.")
            except PlaywrightTimeoutError:
                has_more_photos = False
                print("No loading message detected. Skipping further scrolling.")

            if has_more_photos:
                max_scroll_duration = 10
                start_time = time.monotonic()

                while time.monotonic() - start_time < max_scroll_duration:
                    if all_photos_message.is_visible():
                        print("Reached end of photos message.")
                        break

                    page.evaluate("""
                        () => {
                            window.scrollTo(0, document.body.scrollHeight);
                            const container = document.querySelector(
                                "#folder-content-main"
                            );
                            if (container) {
                                container.scrollTop = container.scrollHeight;
                            }
                        }
                    """)
                    page.wait_for_timeout(1_000)
                else:
                    print(
                        "Maximum scrolling duration reached. "
                        "Collecting currently loaded photos."
                    )

        image_urls = image_locator.evaluate_all(
            """images => [...new Set(
                images
                    .map(img => img.getAttribute('src'))
                    .filter(Boolean)
            )]"""
        )
        print(f"Collected {len(image_urls)} unique image URLs.")

        for image_index, image_url in enumerate(image_urls, start=1):
            print(f"\nImage {image_index}/{len(image_urls)}")
            print(f"Source URL: {image_url[:150]}...")

            try:
                filename = get_original_filename(context, image_url)
                if not filename:
                    print("Could not find Content-Disposition filename.")
                    continue

                print(f"Original filename: {filename}")
                output_path = download_original_image(
                    context,
                    filename,
                    download_dir,
                )
                print(f"Downloaded: {output_path}")
            except Exception as exc:
                print(f"ERROR processing image {image_index}: {exc}")
    finally:
        page.close()


def find_event_urls(context: BrowserContext, search_term: str) -> list[str]:
    """Search FindMyShots and return all matching View Photos links."""
    page = context.new_page()

    try:
        page.goto(
            EVENTS_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        search_input = page.locator('input[placeholder="Search Event..."]')
        search_input.wait_for(state="visible", timeout=30_000)
        search_input.fill(search_term)
        search_input.press("Enter")
        page.wait_for_timeout(2_000)

        view_photos_links = page.locator(
            "#browse-events a",
            has_text="View Photos",
        )
        count = view_photos_links.count()
        print(f"Found {count} matching event(s) for {search_term!r}")

        event_urls = []
        for index in range(count):
            href = view_photos_links.nth(index).get_attribute("href")
            if not href:
                continue
            if href.startswith("/"):
                href = f"{BASE_URL}{href}"
            event_urls.append(href)

        print("Events to process:")
        for event_url in event_urls:
            print(f"  {event_url}")

        return event_urls
    finally:
        page.close()


def main() -> None:
    config = load_config()
    search_term = config["search_term"]
    selected_filenames = config["upload_images"]
    input_folder_id = INPUT_FOLDER_ID
    output_folder_id = OUTPUT_FOLDER_ID

    service = get_drive_service()

    print("Reading reference photos from Google Drive...")
    drive_files = list_drive_files(service, input_folder_id)
    image_files = {
        file["name"]: file
        for file in drive_files
        if file.get("mimeType", "").startswith("image/")
    }

    missing = [name for name in selected_filenames if name not in image_files]
    if missing:
        raise FileNotFoundError(
            "These upload_images filenames were not found in the configured "
            f"Google Drive input folder: {', '.join(missing)}"
        )

    local_input_dir = WORK_DIR / "input"
    local_input_dir.mkdir(parents=True, exist_ok=True)
    local_search_dir = DOWNLOADS_DIR / search_term
    local_search_dir.mkdir(parents=True, exist_ok=True)

    selected_paths = []
    for filename in selected_filenames:
        file_info = image_files[filename]
        local_path = download_drive_file(
            service,
            file_info["id"],
            local_input_dir / filename,
        )
        selected_paths.append(local_path)
        print(f"Downloaded reference image: {filename}")

    # Create/reuse the search-term folder under the configured Drive output folder.
    search_drive_folder_id = get_or_create_drive_folder(
        service,
        output_folder_id,
        search_term,
    )

    failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)

        try:
            for runner_index, upload_image_path in enumerate(selected_paths, start=1):
                runner_name = upload_image_path.stem
                runner_download_dir = local_search_dir / runner_name
                runner_download_dir.mkdir(parents=True, exist_ok=True)

                runner_drive_folder_id = get_or_create_drive_folder(
                    service,
                    search_drive_folder_id,
                    runner_name,
                )

                print(f"\n{'#' * 70}")
                print(
                    f"RUNNER {runner_index}/{len(selected_paths)}: "
                    f"{upload_image_path.name}"
                )
                print(f"Local output: {runner_download_dir}")
                print(f"{'#' * 70}")

                try:
                    event_urls = find_event_urls(context, search_term)
                    if not event_urls:
                        print(
                            f"No matching events found for search term "
                            f"{search_term!r}."
                        )
                    for event_index, event_url in enumerate(event_urls, start=1):
                        process_event(
                            context,
                            event_url,
                            event_index,
                            upload_image_path,
                            runner_download_dir,
                        )
                except Exception as exc:
                    failures.append((upload_image_path.name, str(exc)))
                    print(f"ERROR processing runner {upload_image_path.name}: {exc}")
                finally:
                    # Upload whatever was successfully downloaded, even if a later
                    # event/photo failed for this runner.
                    try:
                        upload_folder_contents(
                            service,
                            runner_download_dir,
                            runner_drive_folder_id,
                        )
                    except Exception as exc:
                        failures.append(
                            (upload_image_path.name, f"Drive upload failed: {exc}")
                        )
                        print(
                            f"ERROR uploading results for "
                            f"{upload_image_path.name}: {exc}"
                        )
        finally:
            context.close()
            browser.close()

    if failures:
        print("\nCompleted with errors:")
        for filename, error in failures:
            print(f"- {filename}: {error}")
        raise RuntimeError(
            f"Automation finished with {len(failures)} error(s). "
            "Review the workflow logs above."
        )

    print("\nAll selected runners processed successfully.")


if __name__ == "__main__":
    main()
