
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def main():
    token_file = Path("token.json")

    if not token_file.exists():
        raise FileNotFoundError("Google OAuth token was not created.")

    credentials = Credentials.from_authorized_user_file(
        str(token_file),
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    drive = build("drive", "v3", credentials=credentials)

    response = drive.files().list(
        pageSize=20,
        orderBy="name",
        fields="files(id, name, mimeType)",
        q="trashed = false",
    ).execute()

    files = response.get("files", [])

    print("Google Drive connection successful!")
    print(f"Found {len(files)} file(s) in the first page.")

    for file in files:
        print(f"- {file['name']} ({file['mimeType']})")


if __name__ == "__main__":
    main()
