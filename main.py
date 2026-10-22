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
    """Upload a file unless a same-named file already exists in the destination."""
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

    if existing:
        print(f"Skipped existing file: {local_path.name}")
        return

    mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(
        str(local_path),
        mimetype=mime_type,
        resumable=True,
    )
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
