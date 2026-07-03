"""Runtime QA for hardened Dawnstrike static route audit bundles."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check static route bundle console, network, and accessibility basics."
    )
    parser.add_argument(
        "--source-bundle",
        required=True,
        help="Folder containing manifest.json and site/",
    )
    parser.add_argument("--out", required=True, help="JSON report output path")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


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


async def qa_all(args: argparse.Namespace) -> list[dict[str, object]]:
    source_bundle = Path(args.source_bundle).resolve()
    manifest = json.loads((source_bundle / "manifest.json").read_text(encoding="utf-8"))
    rows = list(manifest["rows"])
    if args.limit:
        rows = rows[: args.limit]

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

        async def check(row: dict[str, object]) -> dict[str, object]:
            nonlocal done
            audit_path = str(row["audit_path"])
            url = base_url + quote(audit_path.replace("\\", "/"), safe="/")
            result: dict[str, object] = {
                "source": row.get("source", ""),
                "group": row.get("group", ""),
                "audit_path": audit_path,
                "url": url,
                "status": "ok",
                "console_errors": [],
                "page_errors": [],
                "request_failures": [],
                "bad_responses": [],
                "dom_issues": [],
            }

            async with semaphore:
                page = await context.new_page()
                console_errors: list[str] = []
                page_errors: list[str] = []
                request_failures: list[str] = []
                bad_responses: list[str] = []

                page.on(
                    "console",
                    lambda msg: console_errors.append(_trim(msg.text))
                    if msg.type == "error"
                    else None,
                )
                page.on("pageerror", lambda exc: page_errors.append(_trim(str(exc))))
                page.on(
                    "requestfailed",
                    lambda request: request_failures.append(
                        _trim(f"{request.url} :: {request.failure}")
                    ),
                )
                page.on(
                    "response",
                    lambda response: bad_responses.append(
                        _trim(f"{response.status} {response.url}")
                    )
                    if response.status >= 400
                    else None,
                )

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception as exc:
                    page_errors.append(_trim(f"navigation: {exc}"))

                try:
                    dom = await page.evaluate(DOM_QA_SCRIPT)
                except Exception as exc:
                    dom = {"issues": [f"dom_qa_exception: {_trim(str(exc))}"]}

                await page.close()

                issues = list(dom.get("issues", []))
                result["console_errors"] = console_errors
                result["page_errors"] = page_errors
                result["request_failures"] = request_failures
                result["bad_responses"] = bad_responses
                result["dom_issues"] = issues
                if console_errors or page_errors or request_failures or bad_responses or issues:
                    result["status"] = "failed"

                async with lock:
                    done += 1
                    if done % 50 == 0 or done == len(rows):
                        print(f"qa checked {done}/{len(rows)}", flush=True)
            return result

        tasks = [asyncio.create_task(check(row)) for row in rows]
        for task in asyncio.as_completed(tasks):
            results.append(await task)

        await context.close()
        await browser.close()
    server.shutdown()

    return sorted(results, key=lambda item: str(item["source"]))


DOM_QA_SCRIPT = r"""
() => {
  const issues = [];
  const text = document.body ? document.body.innerText : "";
  const title = document.title.trim();
  const body = document.body;
  const boundary = body ? body.getAttribute("data-apex-pro-boundary") : "";
  const requiredText = [
    "Freshness:",
    "Source/provider:",
    "Risk state:",
    "Next safe action:",
    "Audit trail",
  ];

  if (!document.documentElement.getAttribute("lang")) issues.push("missing html lang");
  if (!document.querySelector("meta[name='viewport']")) issues.push("missing viewport meta");
  if (!title) issues.push("missing title");
  if (title.toLowerCase() === "untitled") issues.push("generic untitled title");
  if (title.endsWith("Trade paper basket")) issues.push("generic trade basket title");
  if (!document.querySelector("h1")) issues.push("missing h1");
  if (!body || body.getAttribute("data-apex-pro-hardened") !== "true") {
    issues.push("missing apex pro hardened body marker");
  }
  if (!document.querySelector(".apex-pro-statusbar")) issues.push("missing status bar");
  if (!document.querySelector(".apex-pro-content-header")) issues.push("missing content header");
  if (!document.querySelector(".apex-pro-evidence-rail")) issues.push("missing evidence rail");
  if (!document.querySelector(".apex-pro-audit-footer")) issues.push("missing audit footer");
  if (!document.querySelector(".apex-pro-skip")) issues.push("missing skip link");
  if (boundary === "dev-audit-quarantine" && !document.querySelector(".apex-pro-dev-banner")) {
    issues.push("missing dev-only banner");
  }
  for (const phrase of requiredText) {
    if (!text.includes(phrase)) issues.push(`missing required text: ${phrase}`);
  }
  const styleText = [...document.querySelectorAll("style")]
    .map((style) => style.textContent || "")
    .join("\n");
  if (!styleText.includes(":focus-visible")) issues.push("missing focus-visible CSS");
  const focusable = document.querySelectorAll(
    "a[href], button, summary, input, select, textarea, [tabindex]:not([tabindex='-1'])"
  );
  if (!focusable.length) issues.push("no focusable controls");
  const brokenImages = [...document.images]
    .filter((img) => !img.complete || img.naturalWidth === 0)
    .map((img) => img.currentSrc || img.src || img.alt || "image");
  for (const image of brokenImages) issues.push(`broken image: ${image}`);
  const lower = text.toLowerCase();
  if (/\bundefined\b/.test(lower)) issues.push("visible undefined text");
  if (lower.includes("[object object]")) issues.push("visible object placeholder");
  if (/\bnan\b/.test(lower)) issues.push("visible NaN text");
  return { issues };
}
"""


def summarize(results: list[dict[str, object]]) -> dict[str, object]:
    failed = [row for row in results if row["status"] != "ok"]
    return {
        "route_count": len(results),
        "ok_count": len(results) - len(failed),
        "error_count": len(failed),
        "console_error_count": sum(len(row["console_errors"]) for row in results),
        "page_error_count": sum(len(row["page_errors"]) for row in results),
        "request_failure_count": sum(len(row["request_failures"]) for row in results),
        "bad_response_count": sum(len(row["bad_responses"]) for row in results),
        "dom_issue_count": sum(len(row["dom_issues"]) for row in results),
    }


def _trim(value: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def main() -> int:
    args = parse_args()
    results = asyncio.run(qa_all(args))
    payload = {"summary": summarize(results), "rows": results}
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    return 1 if payload["summary"]["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
