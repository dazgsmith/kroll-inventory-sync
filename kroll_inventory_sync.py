#!/usr/bin/env python3
"""
Kroll -> Shopify inventory sync for WCUniforms.

What it does, each time it runs:
  1. Downloads Kroll_Stock_Inventory.csv from Kroll's SFTP server.
  2. Reads the SKU and quantity columns.
  3. Looks up every product variant in Shopify and matches them by SKU.
  4. Sets the "available" quantity at ONE Shopify location to Kroll's number.

Safety features:
  - DRY_RUN=true (the default) only shows what WOULD change. Nothing is written.
  - SKUs that are not in the Kroll file are never touched.
  - Variants that don't track inventory are skipped.
  - If the Kroll file looks broken (too few rows, or it would zero out too much
    of your stock at once), the run stops without changing anything.
  - Every run writes a report CSV listing each change.

All settings come from environment variables or a ".env" file next to this
script. See README.md and .env.example.
"""

import csv
import io
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
API_VERSION = "2026-10"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file (without overriding real env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value else default


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Step 1: get the Kroll file
# --------------------------------------------------------------------------

def download_kroll_file() -> bytes:
    local = env("KROLL_LOCAL_FILE")
    if local:
        log(f"Using local Kroll file: {local}")
        return Path(local).read_bytes()

    import paramiko  # only needed when downloading

    host = env("KROLL_SFTP_HOST", "ftp.krollcorp.com")
    port = env_int("KROLL_SFTP_PORT", 22)
    user = env("KROLL_SFTP_USER")
    password = env("KROLL_SFTP_PASSWORD")
    remote_path = env("KROLL_REMOTE_PATH", "/ProductData/Kroll_Stock_Inventory.csv")
    if not user or not password:
        raise SystemExit("Missing KROLL_SFTP_USER or KROLL_SFTP_PASSWORD.")

    log(f"Connecting to sftp://{host}:{port} as {user} ...")
    transport = paramiko.Transport((host, port))
    transport.banner_timeout = 60
    try:
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        buf = io.BytesIO()
        try:
            sftp.getfo(remote_path, buf)
        except FileNotFoundError:
            # Some servers want the path without the leading slash.
            buf = io.BytesIO()
            sftp.getfo(remote_path.lstrip("/"), buf)
        sftp.close()
    finally:
        transport.close()

    data = buf.getvalue()
    log(f"Downloaded {remote_path} ({len(data):,} bytes).")
    return data


# --------------------------------------------------------------------------
# Step 2: read the Kroll file
# --------------------------------------------------------------------------

SKU_COLUMN_GUESSES = [
    "sku", "item id", "itemid", "item", "item #", "item no", "item number", "itemnumber", "item_number",
    "part", "part #", "part number", "partnumber", "stock number", "stock #",
    "kroll #", "kroll sku", "product code", "productcode", "style",
]
QTY_COLUMN_GUESSES = [
    "qty", "quantity", "qty available", "quantity available", "available",
    "qty on hand", "on hand", "onhand", "qoh", "stock", "in stock", "inventory",
    "stock qty", "stock quantity", "qtyavail", "qty_avail", "avail",
]


def normalize_header(h: str) -> str:
    return " ".join(h.replace("﻿", "").replace("_", " ").strip().lower().split())


def pick_column(headers, wanted: str, guesses, label: str) -> str:
    by_norm = {normalize_header(h): h for h in headers}
    if wanted:
        if wanted in headers:
            return wanted
        if normalize_header(wanted) in by_norm:
            return by_norm[normalize_header(wanted)]
        raise SystemExit(f"{label} column '{wanted}' not found. Columns in file: {headers}")
    for g in guesses:
        if normalize_header(g) in by_norm:
            return by_norm[normalize_header(g)]
    raise SystemExit(
        f"Couldn't figure out which column holds the {label}. "
        f"Set KROLL_{label.upper()}_COLUMN in .env. Columns in file: {headers}"
    )


def parse_qty(raw: str):
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return None


def normalize_sku(s: str) -> str:
    return (s or "").strip().upper()


def read_kroll_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    log(f"Kroll file columns: {headers}")

    sku_col = pick_column(headers, env("KROLL_SKU_COLUMN"), SKU_COLUMN_GUESSES, "sku")
    qty_col = pick_column(headers, env("KROLL_QTY_COLUMN"), QTY_COLUMN_GUESSES, "qty")
    log(f"Using SKU column '{sku_col}' and quantity column '{qty_col}'.")

    stock = {}
    bad_rows = 0
    dupes = 0
    for row in reader:
        sku = normalize_sku(row.get(sku_col, ""))
        qty = parse_qty(row.get(qty_col, ""))
        if not sku or qty is None:
            bad_rows += 1
            continue
        if sku in stock:
            dupes += 1
        stock[sku] = qty  # if a SKU repeats, the last row wins
    log(f"Read {len(stock):,} SKUs from Kroll ({bad_rows} unreadable rows, {dupes} duplicate SKUs).")
    return stock


# --------------------------------------------------------------------------
# Shopify API helpers
# --------------------------------------------------------------------------

class Shopify:
    def __init__(self):
        store = env("SHOPIFY_STORE", "96ba13-6e")
        self.shop = store if store.endswith(".myshopify.com") else f"{store}.myshopify.com"
        self.url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        self.session = requests.Session()
        self._token = env("SHOPIFY_ACCESS_TOKEN")
        self._token_expires = float("inf") if self._token else 0.0

    def token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        client_id = env("SHOPIFY_CLIENT_ID")
        client_secret = env("SHOPIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SystemExit("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET.")
        r = self.session.post(
            f"https://{self.shop}/admin/oauth/access_token",
            data={"grant_type": "client_credentials",
                  "client_id": client_id, "client_secret": client_secret},
            timeout=60,
        )
        if r.status_code != 200:
            raise SystemExit(f"Shopify login failed ({r.status_code}): {r.text[:500]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 86399))
        return self._token

    def gql(self, query: str, variables=None):
        for attempt in range(8):
            r = self.session.post(
                self.url,
                headers={"X-Shopify-Access-Token": self.token(),
                         "Content-Type": "application/json"},
                data=json.dumps({"query": query, "variables": variables or {}}),
                timeout=120,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise SystemExit(f"Shopify API error {r.status_code}: {r.text[:500]}")
            body = r.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 + 2 ** attempt)
                continue
            if errors:
                raise SystemExit(f"Shopify GraphQL errors: {json.dumps(errors)[:1000]}")
            self._pace(body)
            return body["data"]
        raise SystemExit("Shopify kept throttling or failing; giving up for this run.")

    @staticmethod
    def _pace(body):
        """Slow down a bit when the API cost bucket is running low."""
        cost = (body.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        available = status.get("currentlyAvailable")
        restore = status.get("restoreRate") or 50
        if available is not None and available < 300:
            time.sleep((300 - available) / restore)


LOCATIONS_QUERY = """
query { locations(first: 50) { nodes { id name isActive } } }
"""

VARIANTS_QUERY = """
query Variants($after: String, $loc: ID!) {
  productVariants(first: 100, after: $after) {
    nodes {
      id
      sku
      displayName
      product { vendor status }
      inventoryItem {
        id
        tracked
        inventoryLevel(locationId: $loc) {
          quantities(names: ["available"]) { name quantity }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

SET_QTY_MUTATION = """
mutation SetQty($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { id }
    userErrors { code field message }
  }
}
"""


def find_location(shop: Shopify):
    wanted = env("SHOPIFY_LOCATION_NAME", "Shop location")
    if not wanted:
        raise SystemExit("Set SHOPIFY_LOCATION_NAME to the Shopify location that holds Kroll stock.")
    nodes = shop.gql(LOCATIONS_QUERY)["locations"]["nodes"]
    for loc in nodes:
        if loc["name"].strip().lower() == wanted.lower():
            return loc["id"], loc["name"]
    raise SystemExit(f"Location '{wanted}' not found. Your locations: {[n['name'] for n in nodes]}")


def fetch_variants(shop: Shopify, location_id: str):
    after = None
    page = 0
    while True:
        data = shop.gql(VARIANTS_QUERY, {"after": after, "loc": location_id})["productVariants"]
        page += 1
        for v in data["nodes"]:
            yield v
        if page % 20 == 0:
            log(f"  ...read {page * 100:,} Shopify variants so far")
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]


# --------------------------------------------------------------------------
# Step 3 + 4: compare and update
# --------------------------------------------------------------------------

def plan_changes(stock, variants, location_id):
    prefix = normalize_sku(env("SHOPIFY_SKU_PREFIX"))
    vendors = {v.strip().lower() for v in env("ONLY_VENDORS").split(",") if v.strip()}
    buffer = env_int("SAFETY_BUFFER", 0)

    changes, report = [], []
    matched = 0
    for v in variants:
        sku = normalize_sku(v.get("sku"))
        if not sku:
            continue
        lookup = sku[len(prefix):] if prefix and sku.startswith(prefix) else sku
        if prefix and not sku.startswith(prefix):
            continue
        if lookup not in stock:
            continue
        vendor = ((v.get("product") or {}).get("vendor") or "")
        if vendors and vendor.lower() not in vendors:
            continue
        matched += 1

        item = v["inventoryItem"]
        new_qty = max(0, stock[lookup] - buffer)
        row = {"sku": v["sku"], "kroll_sku": lookup, "product": v["displayName"],
               "vendor": vendor, "kroll_qty": stock[lookup], "old_qty": "",
               "new_qty": new_qty, "action": ""}

        if not item["tracked"]:
            row["action"] = "skipped - inventory not tracked"
        elif item["inventoryLevel"] is None:
            row["action"] = "skipped - not stocked at this location"
        else:
            q = item["inventoryLevel"]["quantities"]
            old = q[0]["quantity"] if q else 0
            row["old_qty"] = old
            if old == new_qty:
                row["action"] = "no change"
            else:
                row["action"] = "update"
                changes.append({"inventoryItemId": item["id"], "locationId": location_id,
                                "quantity": new_qty, "changeFromQuantity": old,
                                "_row": row})
        report.append(row)
    return changes, report, matched


def apply_changes(shop: Shopify, changes):
    ok = failed = 0
    batch_size = 100
    for i in range(0, len(changes), batch_size):
        batch = changes[i:i + batch_size]
        quantities = [{k: c[k] for k in ("inventoryItemId", "locationId", "quantity", "changeFromQuantity")}
                      for c in batch]
        result = shop.gql(SET_QTY_MUTATION, {
            "key": str(uuid.uuid4()),
            "input": {
                "name": "available",
                "reason": "correction",
                "referenceDocumentUri": f"wcuniforms://kroll-sync/{datetime.now(timezone.utc).isoformat()}",
                "quantities": quantities,
            },
        })["inventorySetQuantities"]
        errors = result["userErrors"]
        if errors:
            # Shopify rejects the whole batch if any line fails. Retry those lines one by one
            # so one bad item doesn't block the rest.
            log(f"  Batch had errors ({errors[0].get('message')}); retrying items one at a time.")
            for c, q in zip(batch, quantities):
                single = shop.gql(SET_QTY_MUTATION, {
                    "key": str(uuid.uuid4()),
                    "input": {"name": "available", "reason": "correction", "quantities": [q]},
                })["inventorySetQuantities"]
                if single["userErrors"]:
                    failed += 1
                    c["_row"]["action"] = "FAILED - " + single["userErrors"][0]["message"]
                else:
                    ok += 1
                    c["_row"]["action"] = "updated"
        else:
            ok += len(batch)
            for c in batch:
                c["_row"]["action"] = "updated"
        log(f"  Updated {min(i + batch_size, len(changes)):,} of {len(changes):,}")
    return ok, failed


def write_report(report, dry_run: bool) -> Path:
    out_dir = SCRIPT_DIR / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"kroll_sync_{stamp}{'_DRYRUN' if dry_run else ''}.csv"
    # Only list items that changed, failed, or were skipped. Unchanged items are left out
    # so the report stays small (and doesn't publish the whole catalog's stock counts).
    report = [r for r in report if r["action"] != "no change"]
    fields = ["sku", "kroll_sku", "product", "vendor", "kroll_qty", "old_qty", "new_qty", "action"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(report)
    # Keep only the 200 newest reports.
    old = sorted(out_dir.glob("kroll_sync_*.csv"))[:-200]
    for p in old:
        p.unlink(missing_ok=True)
    return path


def main() -> int:
    load_dotenv(SCRIPT_DIR / ".env")
    dry_run = env_bool("DRY_RUN", True)
    log(f"Kroll inventory sync starting ({'DRY RUN - nothing will be changed' if dry_run else 'LIVE'}).")

    stock = read_kroll_csv(download_kroll_file())
    min_rows = env_int("MIN_KROLL_ROWS", 100)
    if len(stock) < min_rows:
        log(f"STOPPED: Kroll file has only {len(stock)} SKUs (minimum is {min_rows}). File may be incomplete.")
        return 2

    shop = Shopify()
    location_id, location_name = find_location(shop)
    log(f"Updating Shopify location: {location_name}")

    log("Reading Shopify variants (this can take a few minutes for a large store)...")
    changes, report, matched = plan_changes(stock, fetch_variants(shop, location_id), location_id)
    to_zero = sum(1 for c in changes if c["quantity"] == 0 and c["changeFromQuantity"] > 0)
    log(f"Matched {matched:,} Shopify variants to Kroll SKUs. "
        f"{len(changes):,} need a new quantity ({to_zero:,} would go to zero).")

    max_zero_pct = env_int("MAX_ZERO_PERCENT", 30)
    if matched and to_zero * 100 / matched > max_zero_pct:
        path = write_report(report, True)
        log(f"STOPPED: this would set {to_zero:,} of {matched:,} matched items to zero, "
            f"more than the {max_zero_pct}% limit. The Kroll file may be bad. Report: {path}")
        return 3

    if dry_run:
        path = write_report(report, True)
        log(f"Dry run finished. Review the report, then set DRY_RUN=false to go live. Report: {path}")
        return 0

    ok, failed = apply_changes(shop, changes) if changes else (0, 0)
    path = write_report(report, False)
    log(f"Done. Updated {ok:,} items, {failed:,} failed. Report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
