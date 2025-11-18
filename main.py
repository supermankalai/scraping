import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import requests
from playwright.async_api import async_playwright

# -------- CONFIG --------
URL_FILE = "urls.txt"
OUTPUT_ROOT = "downloads"
PARALLEL_PAGES = 2         # number of concurrent browser pages (adjust)
NAV_TIMEOUT = 60_000       # ms
DOWNLOAD_RETRY = 2         # number of retries for file download
REQUEST_TIMEOUT = 60       # seconds for requests
HEADLESS = True            # set False to watch browser
# ------------------------

os.makedirs(OUTPUT_ROOT, exist_ok=True)


def read_input_urls(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def username_from_instagram_url(url: str) -> str:
    """
    extract username from typical instagram story/post url
    examples:
      https://www.instagram.com/stories/thehughjackman/
    returns 'thehughjackman'
    """
    m = re.search(r"instagram\.com/(?:stories/|u/|p/)?([^/?#]+)/?", url)
    if m:
        return m.group(1)
    # fallback: sanitized hostname
    return re.sub(r"\W+", "_", url)[:50]


def ensure_user_dirs(username: str) -> Tuple[Path, Path, Path]:
    """
    creates:
    downloads/username/
        videos/
        images/
        downloaded.json
    returns (user_root, videos_dir, images_dir)
    """
    user_root = Path(OUTPUT_ROOT) / username
    videos = user_root / "videos"
    images = user_root / "images"
    user_root.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    return user_root, videos, images


def load_downloaded_json(user_root: Path) -> dict:
    jpath = user_root / "downloaded.json"
    if jpath.exists():
        try:
            return json.loads(jpath.read_text(encoding="utf-8"))
        except Exception:
            return {"videos": [], "images": []}
    else:
        return {"videos": [], "images": []}


def save_downloaded_json(user_root: Path, data: dict):
    jpath = user_root / "downloaded.json"
    jpath.write_text(json.dumps(data, indent=2), encoding="utf-8")


def filename_for(user_root: Path, kind: str, index: int) -> str:
    """
    Option C filename pattern: YYYY-MM-DD_story_{index}.ext
    kind: 'video' or 'image'
    """
    today = datetime.now().strftime("%Y-%m-%d")
    ext = "mp4" if kind == "video" else "jpg"
    return f"{today}_story_{index}.{ext}"


def download_with_retries(url: str, dest_path: Path) -> bool:
    """
    Download file with retries. Returns True if saved.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for attempt in range(1, DOWNLOAD_RETRY + 2):
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT, headers=headers) as r:
                r.raise_for_status()
                # write file
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as e:
            print(f"   ⚠ Download attempt {attempt} failed for {url}: {e}")
    return False


# ---------------- Playwright helpers ----------------
async def close_popup_tabs(page):
    ctx = page.context
    for p in list(ctx.pages):
        if p != page:
            try:
                print("   ⚠ Closing popup tab...")
                await p.close()
            except Exception:
                pass


async def close_modal_ads(page):
    # try several likely close selectors
    selectors = [
        "button:has-text('Close')",
        "button:has-text('close')",
        "button.close",
        ".modal-close",
        "text=X",
        "text=Skip ad",
        "text=No thanks",
    ]
    for sel in selectors:
        try:
            await page.click(sel, timeout=1500)
            print(f"   ⚠ Closed modal: {sel}")
        except Exception:
            pass


async def extract_links_from_page(page, ig_url: str) -> List[Tuple[str, str]]:
    """
    Visit SaveClip, paste URL, click and extract links.
    Returns list of tuples: (title, href)
    """
    try:
        await page.goto("https://saveclip.app/en/download-video-instagram", timeout=NAV_TIMEOUT)
        await page.wait_for_timeout(500)  # small pause
        # fill input - some pages use #s_input or input[name='q'] - try both
        try:
            await page.fill("#s_input", ig_url, timeout=3000)
        except Exception:
            try:
                await page.fill("input[name='q']", ig_url, timeout=3000)
            except Exception:
                # fallback: find first input and fill
                try:
                    await page.fill("input", ig_url, timeout=3000)
                except Exception:
                    pass

        # click the main "download" trigger
        try:
            await page.click("button[onclick*='ksearchvideo']", timeout=5000)
        except Exception:
            # fallback by text
            try:
                await page.click("button:has-text('Download')", timeout=5000)
            except Exception:
                pass

        # immediately close ad windows and modals (they usually appear)
        await page.wait_for_timeout(500)
        await close_popup_tabs(page)
        await close_modal_ads(page)

        # wait for result list selector seen in the page
        await page.wait_for_selector("#search-result .download-box li", timeout=NAV_TIMEOUT)

        anchors = page.locator("#search-result .download-items__btn a")
        count = await anchors.count()
        results = []
        for i in range(count):
            title = await anchors.nth(i).get_attribute("title")
            href = await anchors.nth(i).get_attribute("href")
            if href:
                results.append((title or "", href))
        return results
    except Exception as e:
        # debug artifacts
        try:
            await page.screenshot(path="debug_error.png")
            html = await page.content()
            Path("debug_page.html").write_text(html, encoding="utf-8")
            print("   ⚠ saved debug_error.png and debug_page.html for inspection")
        except Exception:
            pass
        print("   ⚠ extract_links_from_page error:", e)
        return []


# ---------------- Worker ----------------
async def worker(urls: List[str], worker_id: int):
    print(f"[Worker {worker_id}] starting, processing {len(urls)} urls")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        # create a single page per worker (could create multiple for concurrency)
        page = await browser.new_page()

        for ig_url in urls:
            print("\n--------------------------------------")
            print(f"[Worker {worker_id}] URL: {ig_url}")
            username = username_from_instagram_url(ig_url)
            user_root, videos_dir, images_dir = ensure_user_dirs(username)
            downloaded = load_downloaded_json(user_root)
            # ensure keys exist
            downloaded.setdefault("videos", [])
            downloaded.setdefault("images", [])

            results = await extract_links_from_page(page, ig_url)
            if not results:
                print(f"[Worker {worker_id}]   No links found for {ig_url}")
                continue

            # classify
            video_links = []
            image_links = []
            for title, href in results:
                t = (title or "").lower()
                if "video" in t:
                    video_links.append((title, href))
                elif "image" in t or "photo" in t or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    image_links.append((title, href))
                else:
                    # try inspect extension
                    if href.lower().endswith((".mp4", ".mov")):
                        video_links.append((title, href))
                    elif href.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        image_links.append((title, href))
                    else:
                        # unknown - treat as image fallback
                        image_links.append((title, href))

            print(f"[{username}] found videos={len(video_links)} images={len(image_links)} total={len(results)}")

            # download videos (only new)
            vid_index = 0
            # compute next index based on existing files for naming
            existing_video_files = sorted(videos_dir.glob(f"*story_*"))
            if existing_video_files:
                # find highest index used today to continue numbering (optional)
                # but we will just increment from 0 and skip existing URLs via downloaded.json
                pass

            for title, href in video_links:
                if href in downloaded["videos"]:
                    print(f"   - video already downloaded, skipping: {href}")
                    continue
                filename = filename_for(user_root, "video", vid_index)
                dest = videos_dir / filename
                print(f"   - downloading video -> {dest.name}")
                ok = download_with_retries(href, dest)
                if ok:
                    downloaded["videos"].append(href)
                    save_downloaded_json(user_root, downloaded)
                    vid_index += 1
                else:
                    print(f"   ⚠ failed to download video: {href}")

            # download images (only new)
            img_index = 0
            for title, href in image_links:
                if href in downloaded["images"]:
                    print(f"   - image already downloaded, skipping: {href}")
                    continue
                filename = filename_for(user_root, "image", img_index)
                dest = images_dir / filename
                print(f"   - downloading image -> {dest.name}")
                ok = download_with_retries(href, dest)
                if ok:
                    downloaded["images"].append(href)
                    save_downloaded_json(user_root, downloaded)
                    img_index += 1
                else:
                    print(f"   ⚠ failed to download image: {href}")

        await browser.close()
    print(f"[Worker {worker_id}] finished")


# ---------------- Main ----------------
async def main():
    urls = read_input_urls(URL_FILE)
    if not urls:
        print("No URLs found in", URL_FILE)
        return

    # split into groups for workers
    # simple round-robin split
    groups = [[] for _ in range(PARALLEL_PAGES)]
    for i, u in enumerate(urls):
        groups[i % PARALLEL_PAGES].append(u)

    tasks = [worker(group, idx) for idx, group in enumerate(groups)]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
