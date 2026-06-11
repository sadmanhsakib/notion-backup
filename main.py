import json
import logging
import os
import shutil
import time
import mimetypes
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError


load_dotenv()

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
BACKUP_OUTPUT_DIR = Path(os.getenv("BACKUP_OUTPUT_DIR"))
MAX_BACKUP = int(os.getenv("MAX_BACKUP", 0))

# Notion block types that carry downloadable binary assets.
# Each of these stores its payload under block[block_type]["file"]["url"]
# (Notion-hosted, expires ~1h) or block[block_type]["external"]["url"].
MEDIA_BLOCK_TYPES = {"image", "video", "audio", "file", "pdf"}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("notion_backup")


class NotionBackup:
    def __init__(self, notion: Client, output_dir: Path):
        self.notion = notion
        self.output_dir = output_dir

    # ── block content ──────────────────────────────────────────────────────────
    def fetch_blocks(self, block_id: str, depth: int = 0, max_depth: int = 10) -> list:
        """
        Recursively fetch all block children for a page or block.
        Notion pages are a tree of blocks; this flattens it while preserving children.
        """
        if depth > max_depth:
            log.warning(
                "Max block depth (%d) reached for block %s", max_depth, block_id
            )
            return []

        try:
            blocks = paginate(self.notion.blocks.children.list, block_id=block_id)
        except APIResponseError as e:
            log.warning("Could not fetch blocks for %s: %s", block_id, e)
            return []

        for block in blocks:
            if block.get("has_children"):
                block["children"] = self.fetch_blocks(block["id"], depth + 1, max_depth)

        return blocks

    # ── media helpers ──────────────────────────────────────────────────────────
    def _extract_media_url(self, block: dict) -> str | None:
        """Return the raw URL from an image/video/audio/file/pdf block."""
        btype = block.get("type")
        payload = block.get(btype, {})
        source = payload.get("type")  # "file" (Notion-hosted) or "external"
        if source == "file":
            return payload.get("file", {}).get("url")
        if source == "external":
            return payload.get("external", {}).get("url")
        return None

    def _download_file(
        self, url: str, media_dir: Path, block_id: str, block_type: str
    ) -> str | None:
        """
        Download a single asset to media_dir.
        Returns a relative path string (for embedding in JSON / Markdown), or None on failure.
        Notion-hosted URLs expire after ~1 hour, so downloading during the run is mandatory.
        """
        try:
            parsed = urllib.parse.urlparse(url)
            # The S3 path before the query string usually preserves the original filename.
            ext = Path(parsed.path).suffix  # e.g. ".png", ".pdf"
            if not ext:
                # Fall back to a MIME-type guess via a HEAD-like sniff.
                with urllib.request.urlopen(url) as r:
                    ctype = r.headers.get_content_type() or ""
                ext = mimetypes.guess_extension(ctype) or ".bin"
            filename = f"{block_type}_{block_id}{ext}"
            dest = media_dir / filename
            if not dest.exists():
                urllib.request.urlretrieve(url, dest)
            return f"media/{filename}"
        except Exception as e:
            log.warning("Could not download %s (%s): %s", block_type, block_id, e)
            return None

    def _download_media_from_blocks(self, blocks: list, media_dir: Path) -> int:
        """
        Walk a block tree (already fetched with children) and download every
        media asset found. Mutates each media block in-place by adding
        ``local_path`` pointing to the saved file. Returns total files saved.
        """
        count = 0
        for block in blocks:
            if block.get("type") in MEDIA_BLOCK_TYPES:
                url = self._extract_media_url(block)
                if url:
                    local = self._download_file(
                        url, media_dir, block["id"], block["type"]
                    )
                    if local:
                        block["local_path"] = local
                        count += 1
            if block.get("children"):
                count += self._download_media_from_blocks(block["children"], media_dir)
        return count

    # ── markdown export ────────────────────────────────────────────────────────
    def _blocks_to_markdown(self, blocks: list, indent: int = 0) -> str:
        """
        Convert a fetched block tree to a Markdown string.
        Handles the most common Notion block types; unknown types are skipped.
        """

        def rt(arr: list) -> str:
            """Flatten a rich_text array to plain text."""
            return "".join(t.get("plain_text", "") for t in (arr or []))

        lines = []
        pad = "  " * indent

        for block in blocks:
            btype = block.get("type", "")
            payload = block.get(btype, {})

            if btype == "paragraph":
                lines.append(f"{pad}{rt(payload.get('rich_text', []))}\n")

            elif btype in ("heading_1", "heading_2", "heading_3"):
                level = int(btype[-1])
                lines.append(f"{'#' * level} {rt(payload.get('rich_text', []))}\n")

            elif btype == "bulleted_list_item":
                lines.append(f"{pad}- {rt(payload.get('rich_text', []))}")

            elif btype == "numbered_list_item":
                lines.append(f"{pad}1. {rt(payload.get('rich_text', []))}")

            elif btype == "to_do":
                mark = "[x]" if payload.get("checked") else "[ ]"
                lines.append(f"{pad}- {mark} {rt(payload.get('rich_text', []))}")

            elif btype == "quote":
                lines.append(f"{pad}> {rt(payload.get('rich_text', []))}")

            elif btype == "callout":
                icon = (payload.get("icon") or {}).get("emoji", "💡")
                lines.append(f"{pad}> {icon} {rt(payload.get('rich_text', []))}")

            elif btype == "code":
                lang = payload.get("language", "")
                lines.append(f"```{lang}\n{rt(payload.get('rich_text', []))}\n```")

            elif btype == "divider":
                lines.append("---")

            elif btype == "toggle":
                lines.append(f"{pad}**{rt(payload.get('rich_text', []))}**")

            elif btype == "table_row":
                cells = [rt(cell) for cell in payload.get("cells", [])]
                lines.append("| " + " | ".join(cells) + " |")

            elif btype in MEDIA_BLOCK_TYPES:
                ref = block.get("local_path") or self._extract_media_url(block) or ""
                caption = rt(payload.get("caption", []))
                if btype == "image":
                    lines.append(f"![{caption}]({ref})")
                else:
                    label = caption or Path(ref).name or btype
                    lines.append(f"[{label}]({ref})")

            elif btype == "child_page":
                lines.append(f"{pad}📄 [{payload.get('title', 'Untitled')}]")

            elif btype == "child_database":
                lines.append(f"{pad}🗄️ [{payload.get('title', 'Untitled')}]")

            # Recurse into children (toggles, columns, etc.)
            if block.get("children"):
                lines.append(self._blocks_to_markdown(block["children"], indent + 1))

        return "\n".join(lines)

    # ── pages ──────────────────────────────────────────────────────────────────
    def backup_page(self, page: dict) -> dict:
        """Fetch a page's full metadata + all its block content."""
        page_id = page["id"]
        log.info("  → page  %s  (%s)", page_id, safe_title(page))
        blocks = self.fetch_blocks(page_id)
        return {
            "metadata": page,
            "content": blocks,
        }

    # ── databases ──────────────────────────────────────────────────────────────
    def backup_database(self, db: dict) -> dict:
        """Fetch a database's schema + all its rows, each row with full block content."""
        db_id = db["id"]
        log.info("  → db    %s  (%s)", db_id, safe_title(db))

        try:
            rows = paginate(self.notion.databases.query, database_id=db_id)
        except APIResponseError as e:
            log.warning("Could not query database %s: %s", db_id, e)
            rows = []

        backed_up_rows = []
        for row in rows:
            row_data = self.backup_page(row)
            backed_up_rows.append(row_data)

        return {
            "schema": db,
            "rows": backed_up_rows,
        }

    # ── orchestration ──────────────────────────────────────────────────────────
    def run(self):
        """Run a full workspace backup."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.output_dir / f"backup_{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        log.info("Starting Notion backup → %s", backup_dir)
        start = time.monotonic()

        # Search returns every page and database the integration can see.
        log.info("Fetching workspace index …")
        all_objects = paginate(self.notion.search)

        pages = [o for o in all_objects if o["object"] == "page"]
        databases = [o for o in all_objects if o["object"] == "database"]

        log.info("Found %d pages and %d databases", len(pages), len(databases))

        media_dir = backup_dir / "media"
        markdown_dir = backup_dir / "markdown"
        media_dir.mkdir(exist_ok=True)
        markdown_dir.mkdir(exist_ok=True)

        # ── back up databases ──
        db_backup = {}
        for db in databases:
            db_backup[db["id"]] = self.backup_database(db)

        db_path = backup_dir / "databases.json"
        db_path.write_text(
            json.dumps(db_backup, indent=2, default=str), encoding="utf-8"
        )
        log.info("Saved %d databases → %s", len(databases), db_path)

        # ── back up standalone pages ──
        # Skip pages that are already database rows (already captured above).
        db_row_ids = {
            row["metadata"]["id"]
            for db_data in db_backup.values()
            for row in db_data["rows"]
        }
        standalone_pages = [p for p in pages if p["id"] not in db_row_ids]

        page_backup = {}
        for page in standalone_pages:
            page_backup[page["id"]] = self.backup_page(page)

        pages_path = backup_dir / "pages.json"
        pages_path.write_text(
            json.dumps(page_backup, indent=2, default=str), encoding="utf-8"
        )
        log.info("Saved %d standalone pages → %s", len(standalone_pages), pages_path)

        # ── write markdown exports ──
        def _safe_filename(title: str, uid: str) -> str:
            clean = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[
                :60
            ].strip()
            return f"{clean or 'untitled'}_{uid[:8]}.md"

        md_count = 0
        for page_data in page_backup.values():
            md = page_data.pop("markdown", "")
            if md:
                fname = _safe_filename(
                    safe_title(page_data["metadata"]), page_data["metadata"]["id"]
                )
                (markdown_dir / fname).write_text(md, encoding="utf-8")
                md_count += 1

        for db_data in db_backup.values():
            for row in db_data["rows"]:
                md = row.pop("markdown", "")
                if md:
                    fname = _safe_filename(
                        safe_title(row["metadata"]), row["metadata"]["id"]
                    )
                    (markdown_dir / fname).write_text(md, encoding="utf-8")
                    md_count += 1

        log.info("Wrote %d markdown files → %s", md_count, markdown_dir)

        # ── write a summary manifest ──
        elapsed = round(time.monotonic() - start, 2)
        manifest = {
            "backed_up_at": timestamp,
            "duration_seconds": elapsed,
            "databases_count": len(databases),
            "standalone_pages_count": len(standalone_pages),
        }
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        log.info("Backup complete in %.1fs", elapsed)


def main():
    notion = Client(auth=NOTION_API_KEY)

    backup = NotionBackup(notion=notion, output_dir=BACKUP_OUTPUT_DIR)
    backup.run()

    backup_filenames = [f.name for f in BACKUP_OUTPUT_DIR.glob("backup_*")]
    backup_filenames = sorted(backup_filenames)

    if MAX_BACKUP > 0 and len(backup_filenames) > MAX_BACKUP:
        for filename in backup_filenames[:-MAX_BACKUP]:
            shutil.rmtree(BACKUP_OUTPUT_DIR / filename)
            log.info("Deleted old backup → %s", filename)


def paginate(fn, **kwargs) -> list:
    """Exhaust a paginated Notion API call and return all results."""
    results = []
    cursor = None
    while True:
        params = {**kwargs}
        if cursor:
            params["start_cursor"] = cursor
        resp = fn(**params)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def safe_title(obj: dict) -> str:
    """Extract a human-readable title from a page or database object."""
    title_prop = (
        obj.get("title")  # databases expose title directly
        or obj.get("properties", {}).get("title", {}).get("title")  # pages via property
        or obj.get("properties", {}).get("Name", {}).get("title")
    )
    if isinstance(title_prop, list) and title_prop:
        return "".join(t.get("plain_text", "") for t in title_prop) or "Untitled"
    if isinstance(title_prop, str):
        return title_prop or "Untitled"
    return "Untitled"


if __name__ == "__main__":
    main()
