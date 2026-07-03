"""Render a Dawnstrike HTML audit bundle into screenshot artifacts."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps
from playwright.async_api import async_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render copied Dawnstrike HTML pages into PNG screenshots."
    )
    parser.add_argument("--source-bundle", required=True, help="Folder containing manifest.json and site/")
    parser.add_argument("--out", required=True, help="Output folder for visual audit artifacts")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--viewport-label", default="desktop")
    parser.add_argument("--limit", type=int, default=0, help="Optional page limit for smoke tests")
    return parser.parse_args()


def safe_output_path(root: Path, audit_path: str) -> Path:
    rel = audit_path[5:] if audit_path.startswith("site/") else audit_path
    rel = re.sub(r"[^A-Za-z0-9._/\\-]+", "_", rel).replace("\\", "/")
    return (root / Path(rel)).with_suffix(".png")


def thumbnail_path(thumb_root: Path, screen_root: Path, screenshot: Path) -> Path:
    return (thumb_root / screenshot.relative_to(screen_root)).with_suffix(".jpg")


def safe_viewport_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip().lower())
    return cleaned or "desktop"


def make_thumbnail(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((420, 560), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (420, 260), (11, 15, 20))
        fitted = ImageOps.contain(image, (420, 260), Image.Resampling.LANCZOS)
        canvas.paste(fitted, ((420 - fitted.width) // 2, 0))
        canvas.save(dest, quality=82, optimize=True)


async def launch_browser(playwright):
    errors: list[str] = []
    attempts = (
        {},
        {"channel": "chrome"},
        {"executable_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe"},
        {"executable_path": r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"},
    )
    for kwargs in attempts:
        try:
            return await playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # pragma: no cover - environment dependent
            label = "bundled chromium" if not kwargs else str(kwargs)
            errors.append(f"{label}: {exc}")
    raise RuntimeError("Could not launch Chromium/Chrome:\n" + "\n".join(errors))


async def render_all(args: argparse.Namespace) -> list[dict[str, object]]:
    source_bundle = Path(args.source_bundle).resolve()
    out = Path(args.out).resolve()
    manifest = json.loads((source_bundle / "manifest.json").read_text(encoding="utf-8"))
    rows = list(manifest["rows"])
    if args.limit:
        rows = rows[: args.limit]

    viewport_label = safe_viewport_label(args.viewport_label)
    screen_root = out / "screenshots" / f"{viewport_label}_fullpage"
    thumb_root = out / "thumbnails" / f"{viewport_label}_fullpage"
    screen_root.mkdir(parents=True, exist_ok=True)
    thumb_root.mkdir(parents=True, exist_ok=True)

    handler = partial(QuietHandler, directory=str(source_bundle))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/"

    results: list[dict[str, object]] = []
    lock = asyncio.Lock()
    done = 0

    async with async_playwright() as playwright:
        browser = await launch_browser(playwright)
        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
        )
        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def render(row: dict[str, object]) -> dict[str, object]:
            nonlocal done

            audit_path = str(row["audit_path"])
            screenshot = safe_output_path(screen_root, audit_path)
            thumbnail = thumbnail_path(thumb_root, screen_root, screenshot)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            url = base_url + quote(audit_path.replace("\\", "/"), safe="/")

            result = dict(row)
            result.update(
                {
                    "status": "ok",
                    "mode": f"{viewport_label}_fullpage",
                    "url": url,
                    "screenshot": "",
                    "thumbnail": "",
                    "rendered_title": "",
                    "width": 0,
                    "height": 0,
                    "error": "",
                }
            )

            async with semaphore:
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    await page.wait_for_timeout(650)
                    result["rendered_title"] = await page.title()
                    try:
                        await page.screenshot(
                            path=str(screenshot),
                            full_page=True,
                            animations="disabled",
                            timeout=18000,
                        )
                    except Exception as exc:
                        result["mode"] = f"{viewport_label}_viewport_fallback"
                        result["error"] = "full_page_fallback: " + str(exc).splitlines()[0][:240]
                        await page.screenshot(
                            path=str(screenshot),
                            full_page=False,
                            animations="disabled",
                            timeout=12000,
                        )
                    with Image.open(screenshot) as image:
                        result["width"], result["height"] = image.size
                    make_thumbnail(screenshot, thumbnail)
                    result["screenshot"] = screenshot.relative_to(out).as_posix()
                    result["thumbnail"] = thumbnail.relative_to(out).as_posix()
                except Exception as exc:
                    result["status"] = "error"
                    result["error"] = str(exc).splitlines()[0][:500]
                finally:
                    await page.close()
                    async with lock:
                        done += 1
                        if done % 50 == 0 or done == len(rows):
                            print(f"rendered {done}/{len(rows)}", flush=True)

            return result

        tasks = [asyncio.create_task(render(row)) for row in rows]
        for task in asyncio.as_completed(tasks):
            results.append(await task)

        await context.close()
        await browser.close()
    server.shutdown()

    return sorted(results, key=lambda item: str(item["source"]))


def write_outputs(args: argparse.Namespace, results: list[dict[str, object]]) -> None:
    out = Path(args.out).resolve()
    source_bundle = Path(args.source_bundle).resolve()
    viewport_label = safe_viewport_label(args.viewport_label)
    ok_count = sum(1 for row in results if row["status"] == "ok")
    error_count = sum(1 for row in results if row["status"] != "ok")
    fallback_count = sum(1 for row in results if str(row["mode"]).endswith("fallback"))

    payload = {
        "source_bundle": str(source_bundle),
        "visual_bundle": str(out),
        "viewport": {"width": args.width, "height": args.height},
        "screenshot_mode": f"{viewport_label} full-page, viewport fallback on full-page failures",
        "ok_count": ok_count,
        "error_count": error_count,
        "fallback_count": fallback_count,
        "rows": results,
    }
    (out / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    fieldnames = [
        "status",
        "mode",
        "group",
        "source",
        "title",
        "h1",
        "rendered_title",
        "screenshot",
        "thumbnail",
        "width",
        "height",
        "error",
    ]
    with (out / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

    write_gallery(out, source_bundle, results, ok_count, error_count, fallback_count)


def write_gallery(
    out: Path,
    source_bundle: Path,
    results: list[dict[str, object]],
    ok_count: int,
    error_count: int,
    fallback_count: int,
) -> None:
    by_group: dict[str, list[dict[str, object]]] = {}
    for row in results:
        by_group.setdefault(str(row["group"]), []).append(row)

    sections: list[str] = []
    for group in sorted(by_group):
        cards: list[str] = []
        for row in by_group[group]:
            label = str(row.get("title") or row.get("h1") or row.get("rendered_title") or "untitled")
            status = str(row["status"])
            screenshot = str(row.get("screenshot") or "")
            thumbnail = str(row.get("thumbnail") or "")
            image_html = (
                f'<img loading="lazy" src="{html.escape(thumbnail, quote=True)}" '
                f'alt="{html.escape(str(row["source"]), quote=True)}">'
                if thumbnail
                else '<div class="missing">No screenshot</div>'
            )
            html_path = source_bundle / str(row["audit_path"])
            error_html = f'<small>{html.escape(str(row["error"]))}</small>' if row.get("error") else ""
            search = " ".join([group, str(row["source"]), label]).lower()
            cards.append(
                f"""
                <article class="card {html.escape(status)}" data-search="{html.escape(search, quote=True)}">
                  <a class="image" href="{html.escape(screenshot, quote=True) if screenshot else '#'}">{image_html}</a>
                  <div class="body">
                    <div class="status">{html.escape(status)}</div>
                    <h3>{html.escape(label)}</h3>
                    <p>{html.escape(str(row["source"]))}</p>
                    <div class="links">
                      <a href="{html.escape(screenshot, quote=True) if screenshot else '#'}">PNG</a>
                      <a href="{html.escape(str(html_path).replace(chr(92), '/'), quote=True)}">HTML</a>
                    </div>
                    {error_html}
                  </div>
                </article>
                """
            )
        sections.append(
            f'<section><h2>{html.escape(group)} <span>{len(by_group[group])}</span></h2>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )

    index = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike UI/UX Visual Audit</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0b0f14; color: #eef6ff; }}
    header {{ position: sticky; top: 0; z-index: 5; padding: 18px 24px; background: #101824; border-bottom: 1px solid #26384c; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; line-height: 1.15; }}
    p {{ margin: 0; color: #aebed0; line-height: 1.45; }}
    code {{ padding: 3px 6px; border-radius: 6px; border: 1px solid #26384c; background: #07101a; color: #dff6ff; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
    .meta code {{ font-size: 13px; }}
    input {{ width: min(760px, 100%); padding: 11px 12px; border: 1px solid #32465d; border-radius: 8px; background: #07101a; color: #fff; font-size: 15px; }}
    main {{ display: grid; gap: 18px; padding: 20px 24px 36px; }}
    section {{ display: grid; gap: 12px; }}
    h2 {{ margin: 0; font-size: 18px; }}
    h2 span {{ color: #7dd3fc; font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }}
    .card {{ overflow: hidden; border: 1px solid #24364b; border-radius: 8px; background: #101721; }}
    .card.error {{ border-color: #7f1d1d; }}
    .image {{ display: block; height: 260px; background: #07101a; border-bottom: 1px solid #24364b; }}
    img {{ display: block; width: 100%; height: 100%; object-fit: cover; object-position: top center; }}
    .missing {{ display: grid; height: 260px; place-items: center; color: #fca5a5; }}
    .body {{ display: grid; gap: 7px; padding: 11px 12px 12px; }}
    .status {{ width: fit-content; padding: 2px 7px; border-radius: 999px; background: #0f2e1e; color: #86efac; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
    .error .status {{ background: #3a1111; color: #fca5a5; }}
    h3 {{ margin: 0; font-size: 15px; line-height: 1.25; }}
    .body p, small {{ color: #9fb0c1; overflow-wrap: anywhere; }}
    .body p {{ margin: 0; font-size: 12px; }}
    .links {{ display: flex; gap: 10px; }}
    a {{ color: #8fd3ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <header>
    <h1>Dawnstrike UI/UX Visual Audit</h1>
    <p>Rendered screenshots of the HTML audit bundle. Thumbnails link to full PNG captures; each card also links back to its source HTML.</p>
    <div class="meta">
      <code>{len(results)} pages</code>
      <code>{ok_count} screenshots ok</code>
      <code>{error_count} errors</code>
      <code>{fallback_count} viewport fallbacks</code>
      <code>{html.escape(str(out))}</code>
    </div>
    <input id="q" type="search" placeholder="Filter by path, title, or group" aria-label="Filter screenshots">
  </header>
  <main>{"".join(sections)}</main>
  <script>
    const input = document.getElementById('q');
    input.addEventListener('input', () => {{
      const value = input.value.trim().toLowerCase();
      document.querySelectorAll('.card').forEach((card) => {{
        card.style.display = card.dataset.search.includes(value) ? 'block' : 'none';
      }});
    }});
  </script>
</body>
</html>
"""
    (out / "index.html").write_text(index, encoding="utf-8")


def main() -> None:
    args = parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    results = asyncio.run(render_all(args))
    write_outputs(args, results)
    manifest = json.loads((Path(args.out) / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({key: manifest[key] for key in ("visual_bundle", "ok_count", "error_count", "fallback_count")}, indent=2))


if __name__ == "__main__":
    main()
