import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError


load_dotenv()

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
BACKUP_OUTPUT_DIR = Path(os.getenv("BACKUP_OUTPUT_DIR"))
MAX_BACKUP = int(os.getenv("MAX_BACKUP", 0))

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

    def backup_page(self, page: dict) -> dict:
        """Fetch a page's full metadata + all its block content."""
        page_id = page["id"]
        log.info("  → page  %s  (%s)", page_id, safe_title(page))
        blocks = self.fetch_blocks(page_id)
        return {
            "metadata": page,
            "content": blocks,
        }

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
