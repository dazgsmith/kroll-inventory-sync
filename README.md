# Kroll → Shopify Inventory Sync (WCUniforms)

This script copies Kroll's stock counts into your Shopify store. Each run:

1. Downloads `Kroll_Stock_Inventory.csv` from Kroll's SFTP server (`/ProductData` folder).
2. Matches Kroll SKUs to the SKUs on your Shopify products.
3. Sets the **available** quantity at one Shopify location to Kroll's number.

It replaces the hourly "Run now" click in Bulk Product Editor.

## Built-in safety

- **Practice mode is on by default** (`DRY_RUN=true`). The script only writes a report of what it *would* change.
- **Only matching SKUs are touched.** Products that aren't in the Kroll file are left alone.
- **It stops on a bad file.** If the file has too few rows, or would drop more than 30% of matched items to zero at once, nothing is changed.
- **Every run saves a report** in the `reports` folder listing each SKU, the old number, the new number, and what happened.

---

## Step 1: Choose where it runs

It has to run somewhere that can reach Kroll's SFTP server. Pick one:

| Option | Good for | Needs |
|---|---|---|
| **A. GitHub Actions** (recommended) | Runs in the cloud every 15 minutes; no computer needs to stay on | A free GitHub account |
| **B. Your office Windows PC** | Simple, if a PC is always on | Python installed |

> **Note:** If Kroll only allows logins from approved IP addresses, use option B and give Kroll your office IP. GitHub's IP addresses change.

## Step 2: Give the script access to Shopify

Shopify no longer lets you make "custom apps" from the store admin, so you create a small app in the free **Dev Dashboard**:

1. Go to **https://dev.shopify.com/dashboard** and sign in with your Shopify store-owner login.
2. Click **Apps → Create app**. Name it `Kroll Inventory Sync`.
3. Create a version with these **access scopes**: `read_products`, `read_inventory`, `write_inventory`, `read_locations`.
4. **Install** the app on your store (WCUniforms).
5. Under the app's **Settings**, copy the **Client ID** and **Client secret**. Those go in `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`.

If you already have an older custom app with an Admin API access token (starts with `shpat_`), you can put that in `SHOPIFY_ACCESS_TOKEN` instead.

The app and your store must be in the same Shopify organization. If you get a `shop_not_permitted` error, that's the cause.

## Step 3: Shopify location

The script updates your **Shop location**. This is already the default, so you don't need to set anything. If you ever want a different location, set `SHOPIFY_LOCATION_NAME` to its exact name.

---

## Option A: GitHub Actions (every 15 minutes, in the cloud)

1. Create a **private** repository on GitHub and upload all of these files, including the `.github` folder. Don't upload a `.env` file.
2. In the repository, go to **Settings → Secrets and variables → Actions**.
3. Under **Secrets**, add:
   - `KROLL_SFTP_USER` – your Kroll username
   - `KROLL_SFTP_PASSWORD` – your Kroll password
   - `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` (or `SHOPIFY_ACCESS_TOKEN`)
4. Under **Variables**, add:
   - `DRY_RUN` – `true` for now
5. Go to the **Actions** tab → **Kroll inventory sync** → **Run workflow**. That's your new "Run now" button.
6. When it finishes, open the run and download the report under **Artifacts**. Check that the numbers look right.
7. Change the `DRY_RUN` variable to `false`. From then on it runs every 15 minutes (10, 25, 40, and 55 past each hour), 24 hours a day.

## Option B: Office Windows PC

1. Install Python from **https://www.python.org/downloads/** and check **"Add Python to PATH"** during setup.
2. Put this folder somewhere permanent, like `C:\KrollSync`.
3. Open Command Prompt in that folder and run:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`, open it in Notepad, and fill it in.
5. Double-click `run_sync.bat`. Check `sync_log.txt` and the newest file in `reports`.
6. When the report looks right, change `DRY_RUN=true` to `DRY_RUN=false` in `.env`.
7. Schedule it: open **Task Scheduler → Create Basic Task**, set the trigger to **Daily**, and set the action to start `C:\KrollSync\run_sync.bat`. Then open the task's **Triggers** tab, edit the trigger, and check **Repeat task every 1 hour** for **Indefinitely**. Also check **Run whether user is logged on or not**.

---

## First run checklist

- **Columns:** the log shows the Kroll file's column names and which ones it picked for SKU and quantity. If it picked wrong, set `KROLL_SKU_COLUMN` and `KROLL_QTY_COLUMN`.
- **Matches:** the log says "Matched X Shopify variants." If X is 0 or very low, your Shopify SKUs may differ from Kroll's. For example, if yours are Kroll's SKU with a prefix like `KR-`, set `SHOPIFY_SKU_PREFIX=KR-`.
- **Not stocked at this location:** in the report, these items aren't set up at the chosen location. In Shopify, open the product's inventory and add the location, or tell me and I can help switch them on in bulk.
- **Not tracked:** these variants have "Track quantity" turned off in Shopify, so they're skipped.

## Other settings

- `ONLY_VENDORS` – only update certain brands, for example `5.11 Tactical,Smith & Wesson`.
- `SAFETY_BUFFER` – show fewer than Kroll has, to avoid overselling. For example, `2` shows 8 when Kroll has 10.
- `MIN_KROLL_ROWS` and `MAX_ZERO_PERCENT` – the safety limits described above.

## Turning off Bulk Product Editor

Once this runs well, turn off the old Bulk Product Editor automation so the two don't overwrite each other.
