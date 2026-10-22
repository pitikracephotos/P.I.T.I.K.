# P.I.T.I.K. 
Photo Identification Tracker Instant Keeper

## Files
- `main.py`: Playwright automation and Google Drive input/output handling.
- `config.yml`: search term, selected reference image filenames, and Drive folder IDs.
- `requirements.txt`: Python dependencies.
- `.github/workflows/run-automation.yml`: manually triggered GitHub Actions workflow.

## Before running
1. Set `INPUT_FOLDER_ID` and `OUTPUT_FOLDER_ID` near the top of `main.py` to your Google Drive folder IDs.
2. Ensure the selected filenames in `upload_images` exist directly inside the Drive input folder.
3. In GitHub repository Settings → Secrets and variables → Actions, add `GOOGLE_OAUTH_TOKEN` with the complete contents of your locally generated `token.json`.
4. Keep `token.json` and `credentials.json` out of the repository. They are included in `.gitignore`.

## Run
Open the repository's Actions tab, choose `P.I.T.I.K. FindMyShots Automation`, click Run workflow, and confirm.

Results are uploaded to:
`[Drive output folder]/[search_term]/[reference image filename without extension]/`

The workflow runs Chromium in headless mode.
