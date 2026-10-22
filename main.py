import re
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


BASE_URL = "https://findmyshots.com"
EVENTS_URL = f"{BASE_URL}/events"

SEARCH_TERM = ""

UPLOAD_IMAGE_PATH = Path("")
DOWNLOAD_DIR = Path("")

FIREBASE_BASE_URL = (
    "https://firebasestorage.googleapis.com/v0/b/proj-lamatsapitik-458606.firebasestorage.app/o/events%2F3fd1e949-d871-4e82-a45a-66ef79ebc562%2Fphotos%2F"
)


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

        # Change .webp extension to .jpg
        if filename.lower().endswith(".webp"):
            filename = filename[:-5] + ".JPG"

        return filename

    return None


def get_original_filename(
    context: BrowserContext,
    image_url: str,
) -> str | None:
    """
    Open the image URL in a new tab and capture the network response
    containing the Content-Disposition header.
    """

    image_page = context.new_page()

    try:
        filename = None

        def handle_response(response):
            nonlocal filename

            content_disposition = response.headers.get(
                "content-disposition"
            )

            extracted = extract_filename(content_disposition)

            if extracted:
                filename = extracted

        image_page.on("response", handle_response)

        image_page.goto(
            image_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        # Give the image/network request a little time to finish.
        image_page.wait_for_timeout(2_000)

        return filename

    finally:
        image_page.close()


def download_original_image(
    context: BrowserContext,
    filename: str,
) -> Path:
    """Download the original image from Firebase Storage."""

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # Check if the file already exists
    # ------------------------------------------------------------

    output_path = DOWNLOAD_DIR / filename

    if output_path.exists():
        print(f"Already downloaded, skipping: {filename}")
        return output_path

    # ------------------------------------------------------------
    # Try the original filename
    # ------------------------------------------------------------

    firebase_url = f"{FIREBASE_BASE_URL}{filename}?alt=media"

    print(f"Trying Firebase URL: {firebase_url}")

    response = context.request.get(
        firebase_url,
        timeout=60_000,
    )

    # ------------------------------------------------------------
    # If Firebase fails, try the other .jpg capitalization
    # ------------------------------------------------------------

    if not response.ok and filename.lower().endswith(".jpg"):
        alternate_filename = (
            filename[:-4] + ".JPG"
            if filename.endswith(".jpg")
            else filename[:-4] + ".jpg"
        )

        alternate_output_path = DOWNLOAD_DIR / alternate_filename

        # Check if alternate filename already exists
        if alternate_output_path.exists():
            print(
                f"Already downloaded, skipping: "
                f"{alternate_filename}"
            )
            return alternate_output_path

        firebase_url = (
            f"{FIREBASE_BASE_URL}{alternate_filename}?alt=media"
        )

        print(
            f"Original Firebase URL failed. "
            f"Retrying with: {firebase_url}"
        )

        response = context.request.get(
            firebase_url,
            timeout=60_000,
        )

        if response.ok:
            filename = alternate_filename
            output_path = alternate_output_path

    # ------------------------------------------------------------
    # Download failed
    # ------------------------------------------------------------

    if not response.ok:
        raise RuntimeError(
            f"Failed to download {filename}: "
            f"HTTP {response.status}"
        )

    # ------------------------------------------------------------
    # Save file
    # ------------------------------------------------------------

    output_path.write_bytes(response.body())

    return output_path

def upload_image(page: Page) -> None:
    """Upload aaron.png and agree to the upload."""

    page.locator("#accept-terms-selfie").check()

    with page.expect_file_chooser() as file_chooser_info:
        page.get_by_role("button", name="Select an image").click()

    file_chooser = file_chooser_info.value
    file_chooser.set_files(UPLOAD_IMAGE_PATH)

    agree_button = page.get_by_role(
        "button",
        name="Agree and Upload",
    )

    agree_button.wait_for(
        state="visible",
        timeout=30_000,
    )

    agree_button.click()

    # Wait until the upload modal/action has finished.
    page.wait_for_timeout(2_000)


def process_event(
    context: BrowserContext,
    event_url: str,
    event_index: int,
) -> None:
    """Process one event."""

    page = context.new_page()

    try:
        print(f"\n{'=' * 70}")
        print(f"EVENT {event_index}")
        print(event_url)
        print(f"{'=' * 70}")

        page.goto(
            event_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        # ------------------------------------------------------------
        # Click the + button
        # ------------------------------------------------------------

        plus_button = page.locator(
            'button:has(svg path[d*="M12 3.75"])'
        ).first

        plus_button.wait_for(
            state="visible",
            timeout=30_000,
        )

        plus_button.click()

        # ------------------------------------------------------------
        # Accept terms
        # ------------------------------------------------------------

        checkbox = page.locator("#accept-terms-selfie")

        checkbox.wait_for(
            state="visible",
            timeout=30_000,
        )

        checkbox.check()

        # ------------------------------------------------------------
        # Upload aaron.png
        # ------------------------------------------------------------

        with page.expect_file_chooser() as file_chooser_info:
            page.get_by_role(
                "button",
                name="Select an image",
            ).click()

        file_chooser_info.value.set_files(UPLOAD_IMAGE_PATH)

        # ------------------------------------------------------------
        # Agree and Upload
        # ------------------------------------------------------------

        agree_button = page.get_by_role(
            "button",
            name="Agree and Upload",
        )

        agree_button.wait_for(
            state="visible",
            timeout=30_000,
        )

        agree_button.click()

        # Wait for the photo browser to appear.
        page.locator("#folder-content-main").wait_for(
            state="visible",
            timeout=60_000,
        )

        # ------------------------------------------------------------
        # Scroll and collect all photo URLs as they load
        # ------------------------------------------------------------

        all_photos_message = page.locator("p").filter(
            has_text="You've seen all photos!"
        )

        loading_more_message = page.locator("span.text-sm").filter(
            has_text="Loading more photos..."
        )

        image_locator = page.locator("#folder-content-main img")

        print("Checking if more photos need to be loaded...")

        # Check whether the end message is already visible.
        if all_photos_message.is_visible():
            print("All photos are already loaded.")

        else:
            # Scroll once to trigger lazy loading.
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

            # Wait up to 3 seconds for the loading message.
            try:
                loading_more_message.wait_for(
                    state="visible",
                    timeout=3_000
                )
                has_more_photos = True
                print("Loading more photos detected.")

            except PlaywrightTimeoutError:
                has_more_photos = False
                print("No loading message detected. Skipping further scrolling.")

            # Scroll only if more photos are being loaded.
            if has_more_photos:
                max_scroll_duration = 10  # seconds
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

        # ------------------------------------------------------------
        # Collect photo URLs after scrolling has finished
        # ------------------------------------------------------------

        image_urls = image_locator.evaluate_all(
            """images => [...new Set(
                images
                    .map(img => img.getAttribute('src'))
                    .filter(Boolean)
            )]"""
        )

        print(f"Collected {len(image_urls)} unique image URLs.")

        # ------------------------------------------------------------
        # Process each image
        # ------------------------------------------------------------

        for image_index, image_url in enumerate(
            image_urls,
            start=1,
        ):
            print(
                f"\nImage {image_index}/{len(image_urls)}"
            )

            print(f"Source URL: {image_url[:150]}...")

            try:
                # Open image in a new tab and inspect the
                # network response for Content-Disposition.
                filename = get_original_filename(
                    context,
                    image_url,
                )

                if not filename:
                    print(
                        "Could not find Content-Disposition filename."
                    )
                    continue

                print(f"Original filename: {filename}")

                # Build:
                #
                # https://firebasestorage.googleapis.com/...
                # /photos/<filename>?alt=media
                #
                firebase_url = (
                    f"{FIREBASE_BASE_URL}"
                    f"{filename}?alt=media"
                )

                print(f"Firebase URL: {firebase_url}")

                # Download the original.
                output_path = download_original_image(
                    context,
                    filename,
                )

                print(f"Downloaded: {output_path}")

            except Exception as exc:
                print(
                    f"ERROR processing image "
                    f"{image_index}: {exc}"
                )

    finally:
        page.close()


def main() -> None:
    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
        )

        context = browser.new_context(
            accept_downloads=True,
        )

        page = context.new_page()

        # ------------------------------------------------------------
        # 1. Open events page
        # ------------------------------------------------------------

        page.goto(
            EVENTS_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        # ------------------------------------------------------------
        # 2. Search for "Meowshie Run 2"
        # ------------------------------------------------------------

        search_input = page.locator(
            'input[placeholder="Search Event..."]'
        )

        search_input.wait_for(
            state="visible",
            timeout=30_000,
        )

        search_input.fill(SEARCH_TERM)

        # Search input uses inputmode="search".
        search_input.press("Enter")

        # Give the filtered event list time to update.
        page.wait_for_timeout(2_000)

        # ------------------------------------------------------------
        # 3. Collect View Photos links
        # ------------------------------------------------------------

        view_photos_links = page.locator(
            "#browse-events a",
            has_text="View Photos",
        )

        count = view_photos_links.count()

        print(f"Found {count} matching event(s)")

        event_urls = []

        for index in range(count):
            href = view_photos_links.nth(index).get_attribute(
                "href"
            )

            if not href:
                continue

            if href.startswith("/"):
                href = f"{BASE_URL}{href}"

            event_urls.append(href)

        print("\nEvents to process:")

        for event_url in event_urls:
            print(f"  {event_url}")

        page.close()

        # ------------------------------------------------------------
        # 4. Process each event
        # ------------------------------------------------------------

        for event_index, event_url in enumerate(
            event_urls,
            start=1,
        ):
            process_event(
                context,
                event_url,
                event_index,
            )

        browser.close()

    print("\nAll events processed.")


if __name__ == "__main__":
    main()