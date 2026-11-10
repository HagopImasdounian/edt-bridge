#!/usr/bin/env python3
"""
edt-bridge: take the files The Edge sends through Edge Data Transfer (EDT)
and pass them on to wherever you need them.

One file, Python 3.9+, no packages to install.

What it does
  1. Watches a folder for the JSON files EDT uploads
     (*ItemList.json and *CustomerList.json, full or incremental).
  2. Keeps a small local database (SQLite) of items, customers and sales,
     so it knows what actually changed since the last file.
  3. Turns the changes into four kinds of event:
       sale      one per sale or layaway, items grouped, customer attached
       stock     one per SKU: how many pieces are available, per store
       item      one per changed item
       customer  one per changed customer
  4. Delivers the events to the destinations in your config, with retries.

Destinations
  webhook  POSTs {"events": [...]} as JSON to any URL (optional bearer token)
  plugin   your own sender, for a system that needs its own format or signing:
           {"name": "crm", "type": "plugin", "module": "/srv/edt/crm.py"}. The file
           defines send(dest, event, bridge) and returns (status, text).

Setup
  python3 edt_bridge.py init                 # writes config.json next to this file
  edit config.json                           # set the inbox folder and destinations
  python3 edt_bridge.py run --dry-run        # show what would be sent, change nothing
  python3 edt_bridge.py run                  # one pass (use this from cron)
  python3 edt_bridge.py watch                # or keep it running
  python3 edt_bridge.py status               # what is queued, sent, failed
  python3 edt_bridge.py retry                # requeue deliveries that gave up
  python3 edt_bridge.py fields               # which Edge field works best as your SKU
  python3 edt_bridge.py show IW371605        # look up a SKU, an item key or a serial number
  python3 edt_bridge.py serve                # the web interface: inventory, sales, customers
  python3 edt_bridge.py api                  # the data API, for other systems to read from
  python3 edt_bridge.py apikey create --name website --scopes inventory,sales

Web interface: `serve` opens on http://127.0.0.1:8787 and only this machine can
reach it; it will not listen on any other address. To open it to others, point
a Cloudflare Tunnel at it, put Cloudflare Access in front of the hostname, and
set ui.cloudflare_access (team_domain, aud). A request that arrives through a
tunnel or proxy without a valid Access token is refused. ui.password_env adds a
password on top.

Data API: `api` answers read-only JSON on http://127.0.0.1:8788 for other systems.
  GET /v1/inventory                 one row per SKU: quantity, serial numbers, per store
  GET /v1/inventory?group=piece     one row per physical piece (serialized)
  GET /v1/inventory/<sku>           one SKU with every piece under it
  GET /v1/sales, /v1/sales/<key>    sales and layaways, with their pieces
  GET /v1/customers, /v1/customers/<key>
  GET /v1/issues                    every problem found in the feed, with counts
  GET /v1/ping                      checks a key; returns its scopes
  Three layers, picked with layer=bronze|silver|gold. Silver is the default
  (api.default_layer changes that).
    bronze  exactly what Edge sent, every field, one raw record per piece or
            customer. Needs a key with the bronze scope. A raw piece carries its
            sale price, sale number and customer number, so treat a bronze key
            as able to see everything in the record types it can reach.
    silver  cleaned and checked: emails lower-cased and validated, phones as
            +14165550100, names and spacing tidied, postal codes formatted,
            prices as numbers, and an "issues" list on every record.
    gold    silver plus the business's own rules (gold_rules, edited on the
            Settings page): "when vendor is Omgea, change vendor to Omega", or
            "when category is Repairs, leave it out". With no rules it matches silver.
  GET /v1 describes every endpoint, parameter and layer, so an agent handed
  nothing but a key can work out how to use the API.
  Lists take limit, cursor (pass back next_cursor) and updated_since. Inventory
  takes state, sku, store, q (and serial, item_key with group=piece). Sales take
  since, until (YYYY-MM-DD), status, type, customer_key, sku. Customers take q, email.
  To keep another system in step: state=all&updated_since=<time of your last call>.

API security. Every layer has to pass; none of them can be switched off in config.
  1. It only listens on 127.0.0.1. There is no setting to open it to the network.
     The way in is a Cloudflare Tunnel (cloudflared) pointed at 127.0.0.1:8788.
  2. Cloudflare Access sits in front of the tunnel hostname. Every request must
     carry the token Access signs (Cf-Access-Jwt-Assertion); the API checks the
     signature, audience, issuer and expiry itself. Set api.cloudflare_access
     (team_domain, aud). Without it `api` will not start. `api --local` is for
     testing on this machine: it skips Access and refuses anything that arrived
     through Cloudflare or a proxy.
  3. An API key (Authorization: Bearer edt_...). Made with `apikey create`, shown
     once, stored only as a hash. `apikey list`, `apikey revoke <id>`.
  4. Scopes per key: inventory, sales, customers, bronze. Customer names, emails
     and phones are only returned to a key that holds the customers scope. Raw
     Edge records (layer=bronze) only to a key that also holds bronze.
  5. Rate limit per key, lockout after repeated bad keys, GET only, read-only
     database connection, and an audit line per request in data_dir/api_audit.log
     (who, what, how many rows; never the search text).
  Tunnel setup, once, on the machine that runs the bridge:
     cloudflared tunnel create edt-bridge
     cloudflared tunnel route dns edt-bridge edt-api.example.com
     cloudflared tunnel run --url http://127.0.0.1:8788 edt-bridge
  Then in Cloudflare Zero Trust: add a self-hosted Access application for that
  hostname with one policy, action "Service Auth", that includes only your
  service tokens. Copy its Application Audience (AUD) tag into the config.
  Callers send CF-Access-Client-Id, CF-Access-Client-Secret and the bearer key.

The database (data_dir/edt.db, SQLite) is yours to query. Ready-made views:
  pieces         one row per physical piece (serial number, status, price, sale)
  skus           one row per SKU: pieces, available, sold, price range
  unreferenced   pieces that have no SKU, so they cannot be grouped
  sale_lines     one row per piece sold or on layaway
  customer_list  one row per customer, with number of sales and total spent
  changes        history: what changed on which piece, and when

SKU or piece: Edge tracks every physical piece under its own item key. The SKU
is what groups identical pieces together. `sku_fields` in the config lists the
Edge fields to use for it, in order; the first one that is filled in wins.
Run `fields` after your first full import to see which field fits your store.

Cron example (every 5 minutes)
  */5 * * * * /usr/bin/python3 /opt/edt/edt_bridge.py run >> /var/log/edt-bridge.log 2>&1

Secrets: put them in environment variables and name the variable in the
config ("token_env": "MY_TOKEN"), or put the value straight in ("token": "...").
Your config, database and Edge files are yours and never belong in git: keep
them outside the code folder and point at the config with --config or
EDT_BRIDGE_CONFIG. The .gitignore refuses them if they land here anyway.

Privacy: these files hold customer names, emails and phone numbers. Processed
files are kept for `retention_days` and then deleted. Set it to 0 to delete
them as soon as they are processed.
"""

import argparse
import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, unquote, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("edt-bridge")

EXAMPLE_CONFIG = {
    "inbox": "/srv/edt/inbox",
    "data_dir": "/srv/edt/data",
    "timezone": "America/Toronto",
    "currency": "CAD",
    "retention_days": 30,
    "sales_lookback_days": 30,
    "settle_seconds": 30,
    "poll_seconds": 60,
    "exclude_item_prefixes": [],
    "sku_fields": ["ItemMetaBaseSKU", "ItemVendStyleCode", "ItemStyle"],
    "heartbeat_url": "",
    "phone_country_code": "1",
    "gold_rules": [],
    "ui": {"host": "127.0.0.1", "port": 8787, "password_env": "EDT_UI_PASSWORD",
           "cloudflare_access": {"team_domain": "yourteam.cloudflareaccess.com", "aud": ""}},
    "api": {
        "port": 8788,
        "default_layer": "silver",
        "requests_per_minute": 120,
        "max_limit": 500,
        "cloudflare_access": {"team_domain": "yourteam.cloudflareaccess.com", "aud": ""},
    },
    "destinations": [
        {
            "name": "my-webhook",
            "type": "webhook",
            "url": "https://example.com/edt",
            "token_env": "EDT_WEBHOOK_TOKEN",
            "events": ["sale", "stock", "item", "customer"],
            "batch_size": 100,
            "include_raw": False,
        },
    ],
}

# Edge item status codes. I = in stock, S = sold, L = layaway.
REMOVED_STATUSES = {"-", "X", "M", "V", "G", "N"}
BACKOFF_MINUTES = [1, 5, 30, 120, 360, 720, 1440]  # then give up


# --------------------------------------------------------------------------
# Config and database
# --------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = {k: v for k, v in EXAMPLE_CONFIG.items() if k != "destinations"}
    merged.update(cfg)
    merged.setdefault("destinations", [])
    names = [d.get("name") for d in merged["destinations"]]
    if len(names) != len(set(names)) or not all(names):
        sys.exit("config: every destination needs a unique \"name\"")
    return merged


def secret(dest, field):
    """Read dest[field], or the environment variable named in dest[field + '_env']."""
    if dest.get(field):
        return dest[field]
    env = dest.get(field + "_env")
    return os.environ.get(env, "") if env else ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    hash TEXT PRIMARY KEY, name TEXT, kind TEXT, records INTEGER, processed_at TEXT);
CREATE TABLE IF NOT EXISTS items (
    item_key TEXT PRIMARY KEY, state TEXT, status TEXT, sale_key TEXT, sku TEXT,
    store_id TEXT, digest TEXT, data TEXT, updated_at TEXT, sku_source TEXT);
CREATE INDEX IF NOT EXISTS items_sale ON items(sale_key);
CREATE INDEX IF NOT EXISTS items_sku ON items(sku, state);
CREATE TABLE IF NOT EXISTS customers (
    customer_key TEXT PRIMARY KEY, digest TEXT, data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sales (
    sale_key TEXT PRIMARY KEY, digest TEXT, payload TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dest TEXT, event TEXT, key TEXT,
    payload TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    next_try REAL DEFAULT 0, last_error TEXT, created_at TEXT, sent_at TEXT);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(dest, state, next_try);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, item_key TEXT, field TEXT,
    old TEXT, new TEXT);
CREATE INDEX IF NOT EXISTS changes_item ON changes(item_key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, name TEXT, hash TEXT, scopes TEXT, created_at TEXT,
    expires_at TEXT, revoked_at TEXT);
"""

VIEWS_VERSION = 2
VIEWS = """
CREATE VIEW pieces AS SELECT
    item_key, sku, sku_source, state, status, store_id,
    json_extract(data, '$.ItemSerialNumber') AS serial_number,
    json_extract(data, '$.ItemDesc') AS name,
    json_extract(data, '$.Vendors[0].PairValue.VendorName') AS vendor,
    json_extract(data, '$.CategoryName') AS category,
    json_extract(data, '$.ItemVendStyleCode') AS vendor_style_code,
    json_extract(data, '$.ItemStyle') AS style,
    json_extract(data, '$.ItemRetailPrice') AS retail_price,
    json_extract(data, '$.ItemCurrentPrice') AS current_price,
    json_extract(data, '$.ItemLocation') AS location,
    sale_key,
    json_extract(data, '$.ItemSaleDate') AS sale_date,
    json_extract(data, '$.ItemSalePrice') AS sale_price,
    json_extract(data, '$.ItemSaleCustKey') AS customer_key,
    updated_at
  FROM items;
CREATE VIEW skus AS SELECT
    sku, MIN(name) AS name, MIN(vendor) AS vendor, MIN(category) AS category,
    COUNT(*) AS pieces,
    SUM(state = 'available') AS available,
    SUM(state = 'sold') AS sold,
    SUM(state = 'layaway') AS layaway,
    MIN(CASE WHEN state = 'available' THEN current_price END) AS price_from,
    MAX(CASE WHEN state = 'available' THEN current_price END) AS price_to
  FROM pieces WHERE sku <> '' AND state <> 'removed' GROUP BY sku;
CREATE VIEW unreferenced AS
  SELECT * FROM pieces WHERE (sku IS NULL OR sku = '') AND state <> 'removed';
CREATE VIEW sale_lines AS SELECT
    sale_key, sale_date, customer_key, item_key, sku, serial_number, name, sale_price, state
  FROM pieces WHERE state IN ('sold', 'layaway');
CREATE VIEW customer_list AS SELECT
    c.customer_key,
    json_extract(c.data, '$.CustomerFirstName') AS first_name,
    json_extract(c.data, '$.CustomerLastName') AS last_name,
    json_extract(c.data, '$.Emails[0].PairValue.EmailEmail') AS email,
    json_extract(c.data, '$.Phones[0].PairValue.PhonePhone') AS phone,
    (SELECT COUNT(DISTINCT sale_key) FROM sale_lines s WHERE s.customer_key = c.customer_key) AS sales,
    (SELECT ROUND(SUM(sale_price), 2) FROM sale_lines s WHERE s.customer_key = c.customer_key) AS total_spent
  FROM customers c;
"""


def open_db(cfg, dry_run=False):
    if dry_run:
        db = sqlite3.connect(":memory:")
        src_path = os.path.join(cfg["data_dir"], "edt.db")
        if os.path.exists(src_path):  # start from the real state, change nothing
            src = sqlite3.connect(src_path)
            src.backup(db)
            src.close()
    else:
        os.makedirs(cfg["data_dir"], exist_ok=True)
        db_path = os.path.join(cfg["data_dir"], "edt.db")
        fresh = not os.path.exists(db_path)
        db = sqlite3.connect(db_path)
        if fresh:  # customer details live here: keep other users on the machine out
            try:
                os.chmod(cfg["data_dir"], 0o700)
                os.chmod(db_path, 0o600)
            except OSError:
                pass
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if db.execute("PRAGMA user_version").fetchone()[0] != VIEWS_VERSION:
        for name in ("customer_list", "sale_lines", "unreferenced", "skus", "pieces"):
            db.execute(f"DROP VIEW IF EXISTS {name}")
        db.executescript(VIEWS + f"PRAGMA user_version = {VIEWS_VERSION};")
    return db


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest_of(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------
# Reading Edge records
# --------------------------------------------------------------------------

def unwrap(rec):
    """Edge wraps each record as {"Key": ..., "PairValue": {...}}."""
    if isinstance(rec, dict) and isinstance(rec.get("PairValue"), dict):
        return rec["PairValue"]
    return rec if isinstance(rec, dict) else {}


def edge_date(value, tz_name):
    """Edge sends local time with no offset. Return ISO 8601 with the store's offset."""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^/Date\((-?\d+)", s)  # .NET style
    try:
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).isoformat()
        s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None and ZoneInfo:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt.isoformat()
    except (ValueError, OSError):
        return None


def item_state(data, key, cfg):
    status = str(data.get("ItemStatus") or "")
    transferred = bool(data.get("ItemNewItKey"))
    if status in REMOVED_STATUSES or transferred or data.get("ItemInventoryType") == "O":
        return "removed"
    if status == "S":
        return "sold"
    if status == "L":
        return "layaway"
    if status == "I" and not data.get("ItemSaleCustKey"):
        if any(key.startswith(p) for p in cfg["exclude_item_prefixes"]):
            return "excluded"
        return "available"
    return "removed"


def item_sku(data, cfg):
    """The SKU groups identical pieces. First filled-in field wins. Returns (sku, field)."""
    for field in cfg["sku_fields"]:
        value = str(data.get(field) or "").strip()
        if value:
            return value, field
    return "", None


# Fields worth a line in the history when they change on a piece.
TRACKED = ["ItemStatus", "ItemStoreId", "ItemLocation", "ItemRetailPrice", "ItemCurrentPrice",
           "ItemSpecialPrice", "ItemSaleKey", "ItemSalePrice", "ItemSerialNumber"]


def record_changes(db, key, old, new, old_sku, new_sku):
    at = now_iso()
    if old is None:
        db.execute("INSERT INTO changes (at, item_key, field, old, new) VALUES (?,?,?,?,?)",
                   (at, key, "first seen", None, str(new.get("ItemStatus") or "")))
        return
    pairs = [(f, old.get(f), new.get(f)) for f in TRACKED] + [("sku", old_sku, new_sku)]
    for field, before, after in pairs:
        if (before or None) != (after or None):
            db.execute("INSERT INTO changes (at, item_key, field, old, new) VALUES (?,?,?,?,?)",
                       (at, key, field, None if before is None else str(before),
                        None if after is None else str(after)))


def vendor_name(data):
    vendors = data.get("Vendors") or []
    return unwrap(vendors[0]).get("VendorName") if vendors else None


def item_event(key, data, state, sku, cfg):
    return {
        "item_key": key,
        "state": state,
        "status": data.get("ItemStatus"),
        "sku": sku,
        "store_id": data.get("ItemStoreId"),
        "stock_no": data.get("ItemStockNo"),
        "name": data.get("ItemDesc"),
        "style": data.get("ItemStyle"),
        "category": data.get("CategoryName"),
        "vendor": vendor_name(data),
        "vendor_style_code": data.get("ItemVendStyleCode"),
        "barcode": data.get("ItemVendBarCode"),
        "serial_number": data.get("ItemSerialNumber"),
        "retail_price": data.get("ItemRetailPrice"),
        "current_price": data.get("ItemCurrentPrice"),
        "special_price": data.get("ItemSpecialPrice"),
        "metal_type": data.get("ItemMetalType"),
        "metal_color": data.get("ItemMetalColor"),
        "size": data.get("ItemSize"),
        "length": data.get("ItemLength"),
        "image": data.get("ItemImage"),
        "location": data.get("ItemLocation"),
        "sale_key": data.get("ItemSaleKey") or None,
        "date_entered": edge_date(data.get("ItemDateEntered"), cfg["timezone"]),
    }


def customer_event(key, data):
    emails = [unwrap(e).get("EmailEmail") for e in data.get("Emails") or []]
    phones = [unwrap(p).get("PhonePhone") for p in data.get("Phones") or []]
    addresses = []
    for a in data.get("Addresses") or []:
        a = unwrap(a)
        addresses.append({
            "type": a.get("AddressType"), "street": a.get("AddressStreet"),
            "street2": a.get("AddressStreet2"), "city": a.get("AddressCity"),
            "state": a.get("AddressState"), "zip": a.get("AddressZip"),
            "country": a.get("AddressCountry"),
        })
    return {
        "customer_key": key,
        "first_name": data.get("CustomerFirstName"),
        "last_name": data.get("CustomerLastName"),
        "company": data.get("CustomerCompany"),
        "emails": [e for e in emails if e],
        "phones": [p for p in phones if p],
        "addresses": addresses,
        "inactive": bool(data.get("CustomerInactive")),
        "store_id": data.get("CustomerStoreId"),
        "date_entered": data.get("CustomerDateEntered"),
    }


# --------------------------------------------------------------------------
# Queueing events
# --------------------------------------------------------------------------

def wants(dest, event):
    if dest.get("type") == "plugin" and not dest.get("events"):
        try:
            return event in getattr(load_plugin(dest), "EVENTS", ["sale", "stock", "item", "customer"])
        except Exception as e:
            log.error("%s: could not load %s: %s", dest.get("name"), dest.get("module"), e)
            return False
    return event in dest.get("events", ["sale", "stock", "item", "customer"])


def enqueue(db, cfg, event, key, payload, raw=None):
    for dest in cfg["destinations"]:
        if not wants(dest, event):
            continue
        body = dict(payload)
        if raw is not None and dest.get("include_raw"):
            body["raw"] = raw
        # A newer event for the same thing replaces one that has not gone out yet.
        db.execute("DELETE FROM outbox WHERE dest=? AND event=? AND key=? AND state='pending'",
                   (dest["name"], event, key))
        db.execute("INSERT INTO outbox (dest, event, key, payload, created_at) VALUES (?,?,?,?,?)",
                   (dest["name"], event, key, json.dumps(body, default=str), now_iso()))


def build_sale(db, cfg, sale_key):
    """Group every sold or layaway item that shares a sale key into one sale."""
    rows = db.execute("SELECT item_key, state, sku, data FROM items "
                      "WHERE sale_key=? AND state IN ('sold','layaway') ORDER BY item_key",
                      (sale_key,)).fetchall()
    if not rows:
        return None
    items, total, tax, first = [], 0.0, 0.0, json.loads(rows[0]["data"])
    for r in rows:
        d = json.loads(r["data"])
        price = float(d.get("ItemSalePrice") or 0)
        total += price
        tax += float(d.get("ItemSaleTax") or 0)
        items.append({
            "item_key": r["item_key"], "sku": r["sku"], "name": d.get("ItemDesc"),
            "price": price, "serial_number": d.get("ItemSerialNumber"),
            "category": d.get("CategoryName"), "vendor": vendor_name(d),
        })
    customer = None
    cust_key = first.get("ItemSaleCustKey")
    if cust_key:
        customer = {"key": cust_key}
        row = db.execute("SELECT data FROM customers WHERE customer_key=?", (cust_key,)).fetchone()
        if row:
            c = customer_event(cust_key, json.loads(row["data"]))
            customer.update({
                "first_name": c["first_name"], "last_name": c["last_name"],
                "email": c["emails"][0] if c["emails"] else None,
                "phone": c["phones"][0] if c["phones"] else None,
            })
    return {
        "sale_key": sale_key,
        "status": "completed",
        "type": "layaway" if any(r["state"] == "layaway" for r in rows) else "sale",
        "date": edge_date(first.get("ItemSaleDate"), cfg["timezone"]),
        "total": round(total, 2),  # before tax
        "tax": round(tax, 2),
        "currency": cfg["currency"],
        "store_id": first.get("ItemStoreId"),
        "orig_sale_key": first.get("ItemOrigSaleKey") or None,
        "customer": customer,
        "items": items,
    }


def too_old(sale, cfg):
    days = cfg.get("sales_lookback_days")
    if not days or not sale.get("date"):
        return False
    try:
        return datetime.fromisoformat(sale["date"]) < datetime.now(timezone.utc) - timedelta(days=days)
    except (ValueError, TypeError):
        return False


def emit_sale(db, cfg, sale_key):
    sale = build_sale(db, cfg, sale_key)
    prev = db.execute("SELECT digest, payload FROM sales WHERE sale_key=?", (sale_key,)).fetchone()
    if sale is None:
        # Every item left the sale. If we had sent it, send a cancellation.
        if prev and prev["payload"]:
            cancelled = json.loads(prev["payload"])
            if cancelled.get("status") != "cancelled":
                cancelled["status"] = "cancelled"
                enqueue(db, cfg, "sale", sale_key, cancelled)
                db.execute("UPDATE sales SET digest=?, payload=?, updated_at=? WHERE sale_key=?",
                           (digest_of(cancelled), json.dumps(cancelled), now_iso(), sale_key))
        return
    d = digest_of(sale)
    if prev and prev["digest"] == d:
        return
    db.execute("INSERT OR REPLACE INTO sales (sale_key, digest, payload, updated_at) VALUES (?,?,?,?)",
               (sale_key, d, json.dumps(sale), now_iso()))
    if not too_old(sale, cfg):
        enqueue(db, cfg, "sale", sale_key, sale)


def emit_stock(db, cfg, sku):
    rows = db.execute("SELECT store_id, COUNT(*) AS n FROM items WHERE sku=? AND state='available' "
                      "GROUP BY store_id", (sku,)).fetchall()
    by_store = {str(r["store_id"]): r["n"] for r in rows}
    enqueue(db, cfg, "stock", sku,
            {"sku": sku, "available": sum(by_store.values()), "by_store": by_store})


# --------------------------------------------------------------------------
# Processing files
# --------------------------------------------------------------------------

def process_items(db, cfg, records):
    sale_keys, skus, changed = set(), set(), 0
    for rec in records:
        data = unwrap(rec)
        key = str(data.get("ItemKey") or (rec.get("Key") if isinstance(rec, dict) else "") or "")
        if not key:
            continue
        d = digest_of(data)
        prev = db.execute("SELECT state, sale_key, sku, digest, data FROM items WHERE item_key=?",
                          (key,)).fetchone()
        if prev and prev["digest"] == d:
            continue
        changed += 1
        state = item_state(data, key, cfg)
        sku, sku_source = item_sku(data, cfg)
        record_changes(db, key, json.loads(prev["data"]) if prev else None, data,
                       prev["sku"] if prev else None, sku)
        sale_key = str(data.get("ItemSaleKey") or "") or None
        db.execute("INSERT OR REPLACE INTO items (item_key, state, status, sale_key, sku, store_id, "
                   "digest, data, updated_at, sku_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (key, state, data.get("ItemStatus"), sale_key, sku,
                    str(data.get("ItemStoreId")), d, json.dumps(data, default=str), now_iso(),
                    sku_source))
        enqueue(db, cfg, "item", key, item_event(key, data, state, sku, cfg), raw=data)
        if state in ("sold", "layaway") and sale_key:
            sale_keys.add(sale_key)
        # A layaway that went back into stock cancels the sale it belonged to.
        if prev and prev["state"] == "layaway" and prev["sale_key"] and state == "available":
            sale_keys.add(prev["sale_key"])
        for s in (sku, prev["sku"] if prev else None):
            if s:
                skus.add(s)
    for sale_key in sorted(sale_keys):
        emit_sale(db, cfg, sale_key)
    for sku in sorted(skus):
        emit_stock(db, cfg, sku)
    return changed


def process_customers(db, cfg, records):
    changed = 0
    for rec in records:
        data = dict(unwrap(rec))
        data.pop("CustomerSSNO", None)  # never keep a social security number
        key = str(data.get("CustomerKey") or (rec.get("Key") if isinstance(rec, dict) else "") or "")
        if not key:
            continue
        d = digest_of(data)
        prev = db.execute("SELECT digest FROM customers WHERE customer_key=?", (key,)).fetchone()
        if prev and prev["digest"] == d:
            continue
        changed += 1
        db.execute("INSERT OR REPLACE INTO customers (customer_key, digest, data, updated_at) "
                   "VALUES (?,?,?,?)", (key, d, json.dumps(data, default=str), now_iso()))
        enqueue(db, cfg, "customer", key, customer_event(key, data), raw=data)
    return changed


def put_away(path, folder, file_hash):
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.basename(path))
    if os.path.exists(target):
        base, ext = os.path.splitext(target)
        target = f"{base}.{file_hash[:8]}{ext}"
    shutil.move(path, target)


def resku(db, cfg):
    """If sku_fields changed since the SKUs were stored, work every SKU out again."""
    want = json.dumps(cfg["sku_fields"])
    have = db.execute("SELECT value FROM meta WHERE key='sku_fields'").fetchone()
    if have and have[0] != want:
        touched = set()
        for r in db.execute("SELECT item_key, sku, data FROM items").fetchall():
            sku, source = item_sku(json.loads(r["data"]), cfg)
            if sku != (r["sku"] or ""):
                db.execute("UPDATE items SET sku=?, sku_source=?, updated_at=? WHERE item_key=?",
                           (sku, source, now_iso(), r["item_key"]))
                touched.update(x for x in (sku, r["sku"]) if x)
        for sku in sorted(touched):
            emit_stock(db, cfg, sku)
        log.info("sku_fields changed: %d SKUs regrouped", len(touched))
    if not have or have[0] != want:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sku_fields', ?)", (want,))
        db.commit()


def process_inbox(db, cfg, dry_run=False):
    inbox = cfg["inbox"]
    done_dir, failed_dir = os.path.join(inbox, "completed"), os.path.join(inbox, "failed")
    # Customers first, so a sale in the same batch can pick up its customer.
    jobs = [("customer", p) for p in sorted(glob.glob(os.path.join(inbox, "*CustomerList.json")))]
    jobs += [("item", p) for p in sorted(glob.glob(os.path.join(inbox, "*ItemList.json")))]
    for kind, path in jobs:
        name = os.path.basename(path)
        if time.time() - os.path.getmtime(path) < cfg["settle_seconds"]:
            log.info("%s is still uploading, will pick it up next pass", name)
            continue
        with open(path, "rb") as f:
            raw = f.read()
        file_hash = hashlib.sha256(raw).hexdigest()
        if db.execute("SELECT 1 FROM files WHERE hash=?", (file_hash,)).fetchone():
            log.info("%s was already processed, skipping", name)
            if not dry_run:
                put_away(path, done_dir, file_hash)
            continue
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
            if isinstance(doc, list):
                records = doc
            else:
                top = "Customers" if kind == "customer" else "Items"
                records = doc.get(top) or doc.get(top.lower()) or []
        except (ValueError, UnicodeDecodeError) as e:
            log.error("%s is not readable JSON (%s), moved to failed/", name, e)
            if not dry_run:
                put_away(path, failed_dir, file_hash)
            continue
        handler = process_customers if kind == "customer" else process_items
        changed = handler(db, cfg, records)
        db.execute("INSERT INTO files (hash, name, kind, records, processed_at) VALUES (?,?,?,?,?)",
                   (file_hash, name, kind, len(records), now_iso()))
        db.commit()  # the file's changes and its events land together
        log.info("%s: %d records, %d changed", name, len(records), changed)
        if not dry_run:
            if cfg["retention_days"] <= 0:
                os.remove(path)
            else:
                put_away(path, done_dir, file_hash)
    if not dry_run and cfg["retention_days"] > 0:
        cutoff = time.time() - cfg["retention_days"] * 86400
        for folder in (done_dir, failed_dir):
            for old in glob.glob(os.path.join(folder, "*.json")):
                if os.path.getmtime(old) < cutoff:
                    os.remove(old)
    if not dry_run:
        # Edge can send other files too (item photos). Nothing here reads them, so they
        # go once they are older than the retention period (a day at the least).
        cutoff = time.time() - max(cfg["retention_days"], 1) * 86400
        for other in glob.glob(os.path.join(inbox, "*")):
            if os.path.isfile(other) and not other.endswith(("ItemList.json", "CustomerList.json")) \
                    and os.path.getmtime(other) < cutoff:
                os.remove(other)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would carry the token to wherever it points


_opener = urllib.request.build_opener(NoRedirect)


def http_post(url, body, headers):
    """Returns (status, text). status 0 means the request never got an answer."""
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"edt-bridge/{VERSION}", **headers})
    try:
        with _opener.open(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


_plugins = {}


def load_plugin(dest):
    """A destination of your own: a Python file with send(dest, event, bridge) -> (status, text).

    `event` is {"id", "event", "key", "data"}; `bridge` gives it bridge.http_post,
    bridge.secret, bridge.log and bridge.VERSION. The file may set EVENTS = ["sale"] and DELAY_SECONDS = 1.0.
    It runs with the bridge's rights, so only the user that runs the bridge may be able to edit it.
    """
    path = os.path.abspath(str(dest.get("module") or ""))
    if path not in _plugins:
        import importlib.util
        st = os.stat(path)
        if st.st_mode & 0o022 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            raise PermissionError(f"{path} must belong to this user and not be writable by others")
        spec = importlib.util.spec_from_file_location("edt_plugin_" + hashlib.sha1(path.encode()).hexdigest()[:8], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _plugins[path] = module
    return _plugins[path]


def send_webhook(dest, rows):
    events = [{"id": r["id"], "event": r["event"], "key": r["key"], "data": json.loads(r["payload"])}
              for r in rows]
    token = secret(dest, "token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http_post(dest["url"], json.dumps({"source": "edt-bridge", "events": events}), headers)


def settle(db, rows, status, text):
    """Mark a delivery attempt. 2xx = sent, 4xx (not 429) = give up, anything else = retry."""
    for r in rows:
        if 200 <= status < 300:
            db.execute("UPDATE outbox SET state='sent', sent_at=?, last_error=NULL WHERE id=?",
                       (now_iso(), r["id"]))
            continue
        attempts = r["attempts"] + 1
        permanent = 400 <= status < 500 and status not in (408, 429)
        if permanent or attempts > len(BACKOFF_MINUTES):
            state, next_try = "failed", 0
        else:
            state, next_try = "pending", time.time() + BACKOFF_MINUTES[attempts - 1] * 60
        db.execute("UPDATE outbox SET state=?, attempts=?, next_try=?, last_error=? WHERE id=?",
                   (state, attempts, next_try, f"{status}: {text}"[:500], r["id"]))
    db.commit()


def deliver(db, cfg):
    ok = True
    for dest in cfg["destinations"]:
        rows = db.execute("SELECT * FROM outbox WHERE dest=? AND state='pending' AND next_try<=? "
                          "ORDER BY id", (dest["name"], time.time())).fetchall()
        if not rows:
            continue
        sent = 0
        if dest.get("type") == "plugin":
            try:
                plugin = load_plugin(dest)
            except Exception as e:
                log.error("%s: could not load %s: %s", dest["name"], dest.get("module"), e)
                ok = False
                continue
            import types
            bridge = types.SimpleNamespace(http_post=http_post, secret=secret, VERSION=VERSION, log=log)
            for r in rows:
                try:
                    status, text = plugin.send(dest, {"id": r["id"], "event": r["event"], "key": r["key"],
                                                      "data": json.loads(r["payload"])}, bridge)
                except Exception as e:  # a broken add-on must not stop the other destinations
                    status, text = 0, f"{type(e).__name__}: {e}"
                settle(db, [r], status, text)
                if not 200 <= status < 300:
                    log.warning("%s: %s %s not accepted (%s %s)", dest["name"], r["event"], r["key"], status, text)
                    ok = False
                    if status in (0, 429) or status >= 500:
                        break  # destination is down or throttling, try again next pass
                else:
                    sent += 1
                time.sleep(float(dest.get("delay_seconds", getattr(plugin, "DELAY_SECONDS", 0))))
        else:
            size = int(dest.get("batch_size", 100))
            for i in range(0, len(rows), size):
                batch = rows[i:i + size]
                status, text = send_webhook(dest, batch)
                settle(db, batch, status, text)
                if not 200 <= status < 300:
                    log.warning("%s: batch not accepted (%s %s)", dest["name"], status, text)
                    ok = False
                    break
                sent += len(batch)
        log.info("%s: delivered %d of %d", dest["name"], sent, len(rows))
    return ok


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def run_once(cfg, dry_run=False):
    db = open_db(cfg, dry_run)
    try:
        resku(db, cfg)
        process_inbox(db, cfg, dry_run)
        if dry_run:
            rows = db.execute("SELECT dest, event, key, payload FROM outbox WHERE state='pending' "
                              "ORDER BY id").fetchall()
            for r in rows:
                print(f"[{r['dest']}] {r['event']} {r['key']}: {r['payload']}")
            print(f"\n{len(rows)} deliveries would be queued. Nothing was sent or changed.")
            return True
        ok = deliver(db, cfg)
        if ok and cfg.get("heartbeat_url"):
            try:
                urllib.request.urlopen(cfg["heartbeat_url"], timeout=15).close()
            except (urllib.error.URLError, OSError) as e:
                log.warning("heartbeat failed: %s", e)
        return ok
    finally:
        db.close()


def single_instance(cfg):
    """Stop two runs overlapping (cron firing while a big file is still going)."""
    try:
        import fcntl
    except ImportError:  # Windows
        return None
    os.makedirs(cfg["data_dir"], exist_ok=True)
    handle = open(os.path.join(cfg["data_dir"], "lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another edt-bridge run is in progress")
    return handle


def cmd_status(cfg):
    db = open_db(cfg)
    print("Records:  %d items, %d customers, %d sales" % tuple(
        db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("items", "customers", "sales")))
    print("\nDeliveries:")
    for r in db.execute("SELECT dest, state, COUNT(*) AS n FROM outbox GROUP BY dest, state "
                        "ORDER BY dest, state"):
        print(f"  {r['dest']:<20} {r['state']:<8} {r['n']}")
    print("\nLast files:")
    for r in db.execute("SELECT name, records, processed_at FROM files ORDER BY processed_at DESC LIMIT 5"):
        print(f"  {r['processed_at']}  {r['records']:>6}  {r['name']}")
    print("\nLast failures:")
    for r in db.execute("SELECT dest, event, key, last_error FROM outbox WHERE state='failed' "
                        "ORDER BY id DESC LIMIT 5"):
        print(f"  {r['dest']} {r['event']} {r['key']}: {r['last_error']}")


# --------------------------------------------------------------------------
# Web interface (python3 edt_bridge.py serve)
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Edge Bridge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600&family=Newsreader:ital,opsz,wght@0,6..72,400;1,6..72,400&display=swap" rel="stylesheet">
<style>
:root{--paper:#fafaf8;--tint:#f5f5f3;--card:#fff;--ink:#141414;--body:#555;--muted:#666;--forest:#123524;
--sage:#c3d2bc;--rule:rgba(18,53,36,.16);--rule-soft:rgba(18,53,36,.08);
--shadow:0 0 0 1px rgba(20,20,20,.05),0 1px 0 rgba(20,20,20,.05),0 2px 0 rgba(20,20,20,.025);
--display:"Newsreader",Georgia,serif;--sans:"Hanken Grotesk","Helvetica Neue",Arial,sans-serif;--ease:cubic-bezier(.22,.8,.24,1)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:400 15px/1.5 var(--sans);font-kerning:normal}
a{color:var(--forest);text-decoration:underline;text-decoration-color:var(--rule);text-underline-offset:3px}
a:hover{text-decoration-color:var(--forest)}
.wrap{max-width:1240px;margin:0 auto;padding:0 16px}
@media(min-width:800px){.wrap{padding:0 40px}}
header{position:sticky;top:0;z-index:5;background:var(--paper);border-bottom:1px solid var(--rule)}
header .wrap{display:flex;align-items:center;gap:24px;min-height:64px;flex-wrap:wrap}
.mark{display:block;line-height:0}
.mark img{height:30px;width:auto;display:block}
.product{font:400 19px/1 var(--display);color:var(--ink);padding-left:18px;border-left:1px solid var(--rule)}
nav{display:flex;gap:24px;margin-left:auto}
nav a{font-weight:500;color:var(--body);text-decoration:none;padding:21px 0;border-bottom:2px solid transparent}
nav a.on{color:var(--forest);border-bottom-color:var(--forest)}
@media(max-width:640px){header .wrap{padding-top:16px;gap:14px}nav{margin-left:0;width:100%;gap:20px}nav a{padding:0 0 12px}.product{display:none}}
main{padding:40px 0 80px}
h1{font:400 44px/1 var(--display);letter-spacing:-.015em;margin:0 0 24px}
h2{font:400 26px/1.1 var(--display);letter-spacing:-.01em;margin:40px 0 16px}
.stats{display:flex;gap:40px;flex-wrap:wrap;margin:0 0 32px}
.stat b{display:block;font:400 32px/1 var(--display);font-variant-numeric:tabular-nums}
.stat span{font-size:13px;color:var(--muted)}
.bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
input[type=search]{flex:1;min-width:220px;font:inherit;color:var(--ink);background:var(--card);border:0;
box-shadow:var(--shadow);border-radius:4px;padding:11px 14px;outline:0}
input[type=search]:focus{box-shadow:0 0 0 1.5px var(--forest)}
.seg{display:flex;box-shadow:var(--shadow);border-radius:4px;overflow:hidden;background:var(--card)}
.seg button{font:500 14px var(--sans);border:0;background:none;color:var(--body);padding:11px 16px;cursor:pointer}
.seg button.on{background:var(--forest);color:var(--paper)}
.card{background:var(--card);box-shadow:var(--shadow);border-radius:4px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font:500 12px var(--sans);letter-spacing:.04em;color:var(--muted);text-align:left;padding:12px 16px;
border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:13px 16px;border-bottom:1px solid var(--rule-soft);vertical-align:top}
tr:last-child>td{border-bottom:0}
.num{text-align:right;white-space:nowrap}
tr.open-able{cursor:pointer;transition:background .2s var(--ease)}
tr.open-able:hover{background:var(--tint)}
tr.open-able:focus-visible{outline:1.5px solid var(--forest);outline-offset:-1.5px}
.caret{width:28px;padding-right:0;color:var(--muted);font-size:12px}
tr.is-open .caret{color:var(--forest)}
.sku{font-weight:600;white-space:nowrap}
.sub{color:var(--muted);font-size:13px}
tr.detail>td{background:var(--tint);padding:8px 16px 20px 44px}
tr.detail table{font-size:14px}
tr.detail th{padding:10px 12px}
tr.detail td{padding:10px 12px;border-bottom-color:var(--rule)}
.in{color:var(--forest);font-weight:500}
.off{color:var(--muted)}
.cancelled{text-decoration:line-through;color:var(--muted)}
.pager{display:flex;align-items:center;gap:16px;justify-content:flex-end;margin-top:16px;font-size:14px;color:var(--muted)}
.pager button{font:500 14px var(--sans);background:var(--card);border:0;box-shadow:var(--shadow);border-radius:4px;
padding:8px 14px;color:var(--ink);cursor:pointer}
.pager button:disabled{color:#bbb;cursor:default}
.empty{padding:40px 16px;color:var(--muted)}
.cols{display:grid;gap:24px}
@media(min-width:800px){.cols{grid-template-columns:1fr 1fr}}
.panel{background:var(--card);box-shadow:var(--shadow);border-radius:4px;padding:8px 20px}
dl{margin:0}
.row{display:grid;grid-template-columns:150px 1fr;gap:16px;padding:11px 0;border-bottom:1px solid var(--rule-soft)}
.row:last-child{border-bottom:0}
dt{color:var(--muted);font-size:13px;padding-top:1px}
dd{margin:0;overflow-wrap:anywhere;white-space:pre-line}
.back{display:inline-block;margin-bottom:24px;font-size:14px}
.note{margin:0 0 24px;padding:12px 16px;background:var(--tint);border-left:2px solid var(--forest);font-size:14px;color:var(--body)}
.form{max-width:640px}
.field{padding:16px 0;border-bottom:1px solid var(--rule-soft)}
.field:last-of-type{border-bottom:0}
.field label{display:block;font-weight:500;margin-bottom:2px}
.field .sub{margin-bottom:8px}
.field input,.field select{width:100%;font:inherit;color:var(--ink);background:var(--paper);border:0;
box-shadow:var(--shadow);border-radius:4px;padding:10px 12px;outline:0}
.field input:focus,.field select:focus{box-shadow:0 0 0 1.5px var(--forest)}
.field .err{color:#9b2c1f;font-size:13px;margin-top:6px}
.rule{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:14px 0;border-bottom:1px solid var(--rule-soft)}
.rule select,.rule input{flex:1 1 130px;width:auto;min-width:0;font:inherit;font-size:14px;color:var(--ink);background:var(--paper);border:0;
box-shadow:var(--shadow);border-radius:4px;padding:9px 10px;outline:0}
.rule select:focus,.rule input:focus{box-shadow:0 0 0 1.5px var(--forest)}
.rule .word{color:var(--muted);font-size:14px}
.rule .n{font:400 20px/1 var(--display);color:var(--forest);width:22px;font-variant-numeric:tabular-nums}
button.plain{font:500 14px var(--sans);background:none;border:0;color:var(--forest);padding:8px 0;cursor:pointer;
text-decoration:underline;text-decoration-color:var(--rule);text-underline-offset:3px}
button.plain:hover{text-decoration-color:var(--forest)}
.wide{grid-column:1/-1}
.actions{display:flex;align-items:center;gap:16px;padding:16px 0 8px}
button.primary{font:500 15px var(--sans);background:var(--forest);color:var(--paper);border:0;border-radius:4px;padding:11px 20px;cursor:pointer}
button.primary:disabled{opacity:.5;cursor:default}
code{font:13px ui-monospace,Menlo,monospace;background:var(--tint);padding:1px 5px;border-radius:3px}
footer{border-top:1px solid var(--rule);padding:24px 0 40px;font-size:13px;color:var(--muted)}
@media(max-width:640px){h1{font-size:34px}.hide-sm{display:none}th,td{padding:11px 8px}.caret{width:20px;padding-left:10px}.sku{white-space:normal;overflow-wrap:anywhere}tr.detail>td{padding:4px 4px 14px}tr.detail th,tr.detail td{padding:9px 6px}.stats{gap:24px}.row{grid-template-columns:110px 1fr}}
</style>
</head>
<body>
<header><div class="wrap">
  <a class="mark" href="#/inventory" aria-label="H&amp;CO"><img alt="H&amp;CO" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAABJCAYAAAD2QuLaAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAeKADAAQAAAABAAAASQAAAACUo8TYAAASt0lEQVR4Ae2cCZBUxRnHd9mDPYDlPhaE5RQ5tMBABC8QAkarNKloTETFI0DKIyqKoNEyiWWhUQJGjXibo6KExFSpRNCAYNSIlAS5i3NZYHdhD2723s3vP7xZ3rzt92bezAKFM13V0/2+q/t9X59f95vkjh07vtzQ0DAqOTm5Psk7pIJ+rbS0dJ432anB5ubmZtXU1Pyzvr6+W5i6JlODqrq6uusOHDhQcGpqE5HUFKjS27Vrl05dUtCx6pWUkpJSd/DgwRqywdgg+KkKqRTcF4UNDVcANEnQ5oajO1X46upqKWwI9QhbB+pZnZqa2vJU1SWM3MxWrVq1Tk9P74MxB9Mg+7do0aIr9c6Er4a6ldOpdvG8Cfgm3quchngUXF0YuVGh1SsjFkylwvXyqCrhg6k2ElrqqXc6pT3DUI8Memt/GtY14K4mqtO0woghpNQt8Iyh6zB+AfQrMPi7wFeVlJQUhxA3w4MMnAixayCrS5cu4zHYTAw3WkYkDSdVw3ZvaBXVIF5v27btywzf+eEY/eBDm5cfzgRtUAMpnTt3nsg8+yKA0QJGYNwgb4AW+k4AZqWlpf2K3tytEdkMmYSBY1Rihw4dBtBzn6IX9vBjWGexFu9k0rvBNdv6IWFgp6b9PUt/d2LcAW7GBV4NzV5otpHuJlYSjUEymLPvotGcbySIApiYg6NQWpCFebcjQ/MP3YwLXQWGXU78kFhI7AT99zDiOHhygnIcaQ50NwBb5YBH9ZgwcFRqO8HEFqc/WyHXORMjft6yZctphYWF6rmB0L1794XwzcGIt4A/saQOIq0U3MVkNTrEvGtJDNEO5fp5ZIvTHWMYdQhcTo35duNK9t69e8tqa2vnYNwKU1nAtfDqjGMnw4T3CzNWzq+QeKXHEHK+GAM41l71O01Ihum9wI+YcBYs1XLseJBEhkoYODI9GakwYBmGdNvwtqAX9zcx0vPl2co24QRD5CHm9yo3vB94wsB+tOWgZd+6FdBBBzjwqCEaQ01u3759Dwc+laH7WmCtHPBGPnjXb9iwQavvmEPCwDGocN++fQUYcamM6QxWx56AMWex7RkIXnNqCgYfQTrVwjvZ1Cjkr/5XE0SUgMQqOkrFWWx1DLcvMVRfzrO8Uc4gd+RdbIv64u16lXwpBLOJeU5C2/NaRoYltueYsokeHJP6kpLoxSsw3HzEGIdqS/yVNILXyL9JHA29BW6SlDMaPFNcXFzSBBMlINGDo1ScjU0nV89gmLakN2C8zjZcIGsZtB1pOycu+Az/YfBvcaL0bhDWHGnCwGYtpjNXZjC0unY1Lj7IsMfFTv5Ip06dHiG7j3gTMY/oax+LcdfRy+fCp4sAzRYSBjaokmO7bhh3Aii3KSyZUx/tY9+TcSWCnqdD+yeZa1dhrCnkNRTnChdJoAdnsSD7AQuyf5eVle2Ep1m2SQkDG7SfmZmZxPWgW0DJZWgMGOQ4PU7L578SG12K+/fv/wgv1OqqqqpnoZlsZDYDh9Eg5tGwPqbxvIi36zPOhr3mdbMUB9SthTrI4uuxqKhoF6vjx1B4AW8uI5piNgZ8GGP0c2gnDcMPxVBhr0HZ+ShLj/KMXYncl+nNNzGSaF6PKSQM7KI+VrLLUfJvQRt7kQyCIQYRZzD/Bp0WaQyxl9L7nodvuIto7XVdr0lJLjGXBjJbRkaGr7ncWWbCwE6N2J4xnLY2C4hGr5KMQZiEb1lXbjLpzZdgmHkYfbCFE94ZKqD5AvxGEMYDBzGA132ux7t27XqRU4Cf54SBPbQld2FGRsYTkPyXGLCmgTyTnvYIjeFqDDsP/FA344KvBKez4dtwZtxNfhH0hwwyg6COrAVm8ZAVBPhNEwYOo7E9e/bspSf9ErJ8E6mMSTyfqPvlSk1kGs4rmZuXcD58Pwux7UwBn2DkOyH+G7HWxCRZ8I2l8VxowkcCSxg4Ai1hkM8xzjSUvd2DvL0bDr7D4J5j4TaV8+GCIJ08VowQs8BruDYGcOmU/SMjMgKgXwObm2cEBcVKwouesbJV93Hjxi0j0fwqY0UcoD9CT3wFhnm4Nfc7GRkhysGvh86JanwGZzx2bCTwyPjaB1OQ6Jvtxp9HvZqg+FKgJXtLdy004WhewMKFC+vy8vL+cuTIkQFI/jm6SAtXAjRyP74B3VyvS+3Q1EDrJc717NiLSThfBmaoGMF8cB98nrUJV2g0eIyrhtU6Gt7m4snPzz/I+y/EIHKCuF2aaywOusXobE55eXlhI9CR4WA/m1W4jhNdA8aP2qsVsYGprCb8i0gvoCan3cBWmcH9pqsyTiUCv0MvzYcsutKlD69g9cgWGM+rp6ewZ74OWq9tlRZxjfO2V5kmXMQGFjMFqRedkSHaVPnTCcO4eax6H6TMW9GDPiTzDFYDmMDC6jAHFwvgXcMcfAAmrZjTkdeN7dXlNJYHeG5l0Rtl0qi+NCIiAPoycATyvpUkOTk5fTDUDF5OV1397Enb0DtvxogD6amrcYTsRMYxZOQAG0T+EmLfMMbdQ9nLoYsqJAwcRm0Yty+97yEZCkOE7bkGcRqi9UHaSOIhZFSTZpC2I9WoaGA5AQJfD/6PbNPyXYnCIBIG9lAQC6r+KHgmip5E6uUT1uco2gL1NImzjKhvsTvY8RbcDgrJMzR/TO99HaCr7zqEwfDgdx9sEPHtBDGcnouCH4nAuOqROjJ8knR1M2mjHjnLmKOfYN7WsB51SPRgg+pYFA0G/DDz5HX0Mq9FpW5fLIDm6SFDhuxYv359MTy30TDGAI/2qE+3Qj6msbwiDxr5mELCwA71aSuEke4BfBVR+0/THlTbRK2GP8S4s7mBsWX58uU8Jr3HsF6AcXSr4wqedSas25bhtpVV8OjjtLXwLSUuwTGyBb6Yg28DU4mYC41WAC8eLWvEfMx5WgDtwMjPwtTg8r76aqGalfEinBib7MIxzBqcF1v59ORTZOmWxhDw+uOY9uSzSLXo0uJJ87bcnvvJ7yRuocy18G8cPHhwEg2Nc4mWDZxoaZSI+sWTOaBeQqETEOIZoBF+BxXZTP5MWFm3HS6l/EhWsvps83yUtc3zpcxIDclam4RTquZJ4zmxTWwqc7luWXal3u2pUxbDdxppA8aspIEcYp4toSGkkldP70aUh6w19KnQybhHSbV/LoKsgOmjyM9XD3578KcU/BKR8k5vQDHpKGMhpUZi4FgqV4US22AA9bouvGtb0kzSZGBS+BHqUkpaxLxbwtCsodot1HIpT25KxZCgMnifYciewJnvcGTnQdBN5RFbAm9BWkeqM+SD4IsZFXax6FrHCLEOD9nK4IW/EMGOB78GLmS++coh47Q85uXlZRw9ejRcj4mlLimMZgNQ6AVE/V1TH4R1IeoucwbPySg6YGCUXYqRC+lJm7hFuQnUN6aTIlNlxowZk7px48aR8E+A7zJkDiXtSNpIznMgH0x5yIVe14O0by4jr1F0sUZf7KGVu+s2yq+BNUyekYBxvXy6MdWJYXQAAsYqorgLUGIvoutIAU7laR4tI91K/DNxPtEz8PF3BxrFtRD9hDgKft++derXAb6L4R9CQxuFkRdkZ2e/X1BQoGG8SfBr4CYCzmaANSqM5x1+iuLUm/z8kYrm6U7wdaJH7STvaWCG5HMqKyunYRQ5TfKgjzXkIOcq5A04fvx4bwz9Gr15r1No3BqYPy3L4Wz3RhR0B4q6kOjUTUTPFp8nMyNELnQPUJYOKrSIaraAvH40snsR2IZynmVeLrILVyuMu4AiWrPAmUqchYKivu8UieJUFgb4Bcb9WXMbN1g+cuXXnkq8R+UF4UrjzsBa5KDsm1HGdIZWo+/YrqAY8xSTfD0GmEIMeysDWp25q0gtmgKrcxvMsyrI15Gj7o39GMLGbWzcDdFr164dS899AGV09dSYtHRC2Vq5artSS6oOoX2s5ylQUC7z4kB4dN7reiFPtJa8Omi3kNeqvIi8rvG0IdXfPZwLWR9SreTF4hbkTJnOXeovudC3QURxZWC2NF3otQ+hhD5uGhIcPcqA2pLJSyWlF8N3jDSdqFVsL1J5qDoS3YIcFVOIg6B3owmWtYdRRW7PpTg8NnI8uZ8FWR1pJvxykgwEP55UWys9u8oDMYh99TRGqunao8eVgVHM/SjgCi/toEAZ9yviH4gruQtWxhbtODwaMlvIhUjallGgD0qfRH4yscn2kcY0En4NzaDNQWURdVtjJoZdg9vTdGNzN/hVnEt/wBXbYTQ0/W3iCC+51Ov2zZs3/wO+FXFjYL7464kn6EZe2nXdIYUTFtMDHkWZm3FeHBPAHvjgT5+b6Hul3Rh7O7c996JsuRhDAobQXSvPfS58u2CagUtVRvbyiCUdOnToAPE/HGZoetG+u1dIgaEP2byD5uIVri8bSn/2P2Fc+du7u72JZVzdT56JIlebjOvg1T+372IP+gI95g07jsakQwUNqXZwSB68kM9FYlwbY43o4Z0LzFW4ylX5AZerjflbneWFryS6NmiUUkuve4Yz2PVeynMqieEblv1f2+E0pnOQ19cOc+bBb6+oqHgbuGfPdfLxXGPxbTHg7CDdAO3t+sJ2yrM9z7CmLYRckMZXwfBq8etx4L8HgU6J/IYQHzmytOLNchOi8uj1y44dO6bDfd+BRlWCjI8kxy2Aa8n00S8uDIwyA0d2bsoQHOMvbY4v6iWLhtJkThbcHuhd6vXmFmcnNOepbsNKM+okFJrz4sLAbDl0IhTO0fC/k6qJLccK27MsFK8CQlyKfktERrElx8gqHPHCuDCwrkagBdfxTMqg15UbNRUFkN7pyWUNra718WS2kMjwLMQqoyYuDMweM6wy6HXNdhyJLG2jPAONynVF78l4EtnFMuJJiCMH/pu4MDAdWF/RhyyEHLrQYx8DLCoQo4H2xp7zK+uCi6ISbjExSowMx4+BN8WFgbWVQRmeQzDKuAyamIbNoMJZvW4nb/JKBUhke+JYLgD0CPL4SXv27NmOBjIxTBuq5J22xIWBOffVkCmfslGPUhQ94jLuOsm/HHPgb5gKKWuDW3lWAbm4QadGUxj7YHnk+rnxqlzeZwfOmp1xYWAUoeO35W4KEZwe0R6lPMiV1XQvOhOuR48ezus9NchaZKK1weS/nsL57TU2WNgsPm79g5586p71ZB2wBJpj8WJgKU5OjCa+ZSEUUJqO4q7lmG06jxH76FH4KHri7QEhth/m/Xd5LLGBTFkdWT6FI+Z60rC2YIS5gjrOgbaPSZgNdpR1wDt6DivUxnRWZ7nKsp5epU9CvN5Dn3Xei8Kf0OGEFyHKzsa4k5D5ArHJgoc/W9kGfEGY8lTEQOJsZD1Fb/5OHrdHHeUmU5/+xFnsBp5D5gjwri+h8mgEi7iftVZyIm6pjkLPxscqzld/T8seT+VdT3lQjg7Yp+BPHonCFzHUfc6iaTewCnqqtlKdoRmGnInARhPzgG0mOkMtuBcA6v+zesPjxAefNXLoG+E7AExkQbiOcrfDoz9n0Zlyb+DDib3Jh72kAF0JfL8jrSTGlYGTGH6/5JbFm/TSe1CC3t8YwOlQ/zLozqPX3IoxyyA8TtS81wFFyzMmQ+vUiKzZf82osQ1jzQYvQ3vOmeDbW7E/Mo8iu5pUl9+zibrX5dpr4QsGvdQ8eu+aICCeerDeuQKDzaVHDkdfF6O4oB6apOD1Pa++btDXBlqkyVmib5L0N/1N6F0AdZS3kJFjKHjPRmXj11cUgUVbsBzKtKHNWdEQ3+H48lUoGvf8cTMHB9XCWW8+ed2m3BiJ4sQHXQpRd7G08vUVdIDBvPk0TG9FWp6vAiCWXN7nA3zuj+ukyc4fdwbm5Rs4NF/J8Hsf+W9OldLtStaFdG5Y/BqYhmoNvXZ0zHnkvY2BHzp8+PA2p7B4NLB0UMMh/QqGz7tQzPsCNLfSJdMe6Mj59GTdp5oBfFus5YmfqA/Of8Pi71EakS4INpk74tXA0n31gQMHdBfqQRT1GIbeailNuIiDeAgRDd1WT/4T9LdT3nx4D/kpM0grPmTonwWmMOe+yLSzQ5UwhXhbZDl1UMdKd0vr1q3n8zf+n6Cw70Ogq6lyWTq9UyG80GrRVUivXE3+7yFIjwfrUsEX3JfKZwu2DF5ttb5L7E9eK/Qm4zdw9dYjiJWP+zPKXMbosxbD7ua5cUFlKjbeDRzQCb7qUqK2Qmv4Uu/5rKysc8nris8g0h4otw2pRrvjwPYRt5DXBb2N7JdLmfuO8uwn1HFFVsYpYhu1GN9yJivtzgy3fZGp+1ydwGk1rb10Ofk9GHUHi6hdeMgq4D0GTCv7sCFh4JMq0vzFNalAkLG/5i5yOntgbZcCUxnK1pf5tWyzauj5VdDUEJvMe8AiDfpAXD1TsZSohpNG79ZWjOIC5dWzKNTFPJVVR+1IIg//B27PYS4GZcjQAAAAAElFTkSuQmCC"></a><span class="product">Edge Bridge</span>
  <nav><a href="#/inventory" data-v="inventory">Inventory</a><a href="#/sales" data-v="sales">Sales</a><a href="#/customers" data-v="customers">Customers</a><a href="#/settings" data-v="settings">Settings</a></nav>
</div></header>
<main><div class="wrap" id="app"></div></main>
<footer><div class="wrap">Edge Bridge, built by <a href="https://www.itshco.com">H&amp;CO.</a> <span id="foot"></span></div></footer>
<script>
"use strict";
var CUR = "CAD", SIZE = 50, timer = null;
function h(tag, attrs) {
  var e = document.createElement(tag), k, v;
  for (k in (attrs || {})) {
    v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  [].slice.call(arguments, 2).forEach(function add(c) {
    if (Array.isArray(c)) return c.forEach(add);
    if (c == null || c === false) return;
    e.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  });
  return e;
}
// replaceChildren, minus the empty slots (it would print them as the word "null")
function put(box) {
  box.replaceChildren.apply(box, [].slice.call(arguments, 1).filter(function (c) { return c != null && c !== false; }));
}
function money(v) {
  if (v == null || v === "") return "";
  v = Number(v);
  return new Intl.NumberFormat(undefined, {style: "currency", currency: CUR,
    maximumFractionDigits: v % 1 ? 2 : 0}).format(v);
}
function day(s) {
  if (!s) return "";
  var d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"});
}
function count(n) { return Number(n || 0).toLocaleString(); }
function api(path) {
  return fetch("api/" + path, {headers: {"Accept": "application/json"}}).then(function (r) {
    if (!r.ok) throw new Error("The server answered " + r.status);
    return r.json();
  });
}
function fail(box) { return function (e) { put(box, h("div", {class: "empty"}, "Could not load this. " + e.message)); }; }
function stat(value, label) { return h("div", {class: "stat"}, h("b", null, value), h("span", null, label)); }
var STATES = {available: "In stock", sold: "Sold", layaway: "Layaway", excluded: "Excluded", removed: "Removed"};
function stateCell(s) { return h("td", {class: s === "available" ? "in" : "off"}, STATES[s] || s); }

// A table whose rows open to show more underneath.
function openable(cells, cols, fill) {
  var tr = h("tr", {class: "open-able", tabindex: "0"}, h("td", {class: "caret"}, "+"), cells), detail = null;
  function toggle() {
    if (detail) { detail.remove(); detail = null; tr.classList.remove("is-open"); tr.firstChild.textContent = "+"; return; }
    var box = h("td", {colspan: cols}, h("div", {class: "sub"}, "Loading"));
    detail = h("tr", {class: "detail"}, box);
    tr.after(detail); tr.classList.add("is-open"); tr.firstChild.textContent = "−";
    fill(box);
  }
  tr.addEventListener("click", function (e) { if (e.target.tagName !== "A") toggle(); });
  tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  return tr;
}
function pager(res, page, go) {
  if (res.total <= SIZE) return null;
  var from = page * SIZE + 1, to = Math.min(res.total, (page + 1) * SIZE);
  return h("div", {class: "pager"}, count(from) + " to " + count(to) + " of " + count(res.total),
    h("button", {disabled: page === 0, onclick: function () { go(page - 1); }}, "Previous"),
    h("button", {disabled: to >= res.total, onclick: function () { go(page + 1); }}, "Next"));
}
function searchBox(placeholder, value, onchange) {
  return h("input", {type: "search", placeholder: placeholder, value: value || "", "aria-label": placeholder,
    oninput: function (e) { clearTimeout(timer); var v = e.target.value; timer = setTimeout(function () { onchange(v); }, 250); }});
}

// ---- Inventory: one row per SKU, opens to the pieces under it
function piecesTable(box, sku) {
  api("pieces?sku=" + encodeURIComponent(sku)).then(function (rows) {
    put(box, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Serial number"), h("th", {class: "hide-sm"}, "Item key"), h("th", null, "Status"),
        h("th", {class: "hide-sm"}, "Store"), h("th", {class: "hide-sm"}, "Location"), h("th", {class: "num"}, "Price"), h("th", null, "Sale"))),
      h("tbody", null, rows.map(function (p) {
        var sold = p.state === "sold" || p.state === "layaway";
        return h("tr", null, h("td", null, p.serial_number || h("span", {class: "sub"}, "none")), h("td", {class: "hide-sm"}, p.item_key), stateCell(p.state),
          h("td", {class: "hide-sm"}, p.store_id), h("td", {class: "hide-sm"}, p.location || ""),
          h("td", {class: "num"}, money(sold ? p.sale_price : p.current_price != null ? p.current_price : p.retail_price)),
          h("td", null, p.sale_key ? h("a", {href: "#/sales?q=" + encodeURIComponent(p.sale_key)}, p.sale_key) : "",
            sold && p.sale_date ? h("div", {class: "sub"}, day(p.sale_date)) : null));
      }))));
  }).catch(fail(box));
}
function inventory(params) {
  var app = document.getElementById("app"), q = params.get("q") || "", stock = "in", page = 0;
  var stats = h("div", {class: "stats"}), list = h("div");
  var seg = h("div", {class: "seg"}, [["in", "In stock"], ["all", "All"]].map(function (o) {
    return h("button", {class: o[0] === stock ? "on" : "", onclick: function (e) {
      stock = o[0]; page = 0; [].forEach.call(seg.children, function (b) { b.className = ""; }); e.target.className = "on"; load();
    }}, o[1]);
  }));
  put(app, h("h1", null, "Inventory"), stats,
    h("div", {class: "bar"}, searchBox("Search SKU, name, vendor or serial number", q, function (v) { q = v; page = 0; load(); }), seg), list);
  api("summary").then(function (s) {
    put(stats, stat(count(s.skus_in_stock), "SKUs in stock"), stat(count(s.pieces_available), "pieces in stock"), stat(count(s.skus), "SKUs on record"));
  }).catch(function () {});
  function load() {
    api("skus?stock=" + stock + "&page=" + page + "&q=" + encodeURIComponent(q)).then(function (res) {
      var rows = res.rows.map(function (r) {
        var price = r.price_from == null ? "" : r.price_from === r.price_to ? money(r.price_from) : money(r.price_from) + " to " + money(r.price_to);
        return openable([h("td", {class: "sku"}, r.sku), h("td", null, r.name || "", h("div", {class: "sub"}, [r.vendor, r.category].filter(Boolean).join(", "))),
          h("td", {class: "num " + (r.available ? "in" : "off")}, r.available), h("td", {class: "num hide-sm"}, r.sold + r.layaway),
          h("td", {class: "num"}, price)], 6, function (box) { piecesTable(box, r.sku); });
      });
      if (res.unreferenced && !q && page === 0) rows.push(openable([h("td", {class: "sku off"}, "No SKU"),
        h("td", {class: "sub"}, "Pieces Edge sent without a reference, so they cannot be grouped"), h("td", {class: "num"}, res.unreferenced),
        h("td", {class: "num hide-sm"}, ""), h("td", null, "")], 6, function (box) { piecesTable(box, ""); }));
      put(list, rows.length ? h("div", {class: "card"}, h("table", null,
        h("thead", null, h("tr", null, h("th", null, ""), h("th", null, "SKU"), h("th", null, "Name"), h("th", {class: "num"}, "In stock"),
          h("th", {class: "num hide-sm"}, "Sold"), h("th", {class: "num"}, "Price"))), h("tbody", null, rows)))
        : h("div", {class: "card empty"}, q ? "Nothing matches that search." : "No inventory yet. It appears here after the first file from Edge is processed."),
        pager(res, page, function (p) { page = p; load(); window.scrollTo(0, 0); }));
    }).catch(fail(list));
  }
  load();
}

// ---- Sales
function saleLines(box, s) {
  put(box, h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Piece"), h("th", null, "SKU"), h("th", null, "Serial number"), h("th", {class: "hide-sm"}, "Item key"), h("th", {class: "num"}, "Price"))),
    h("tbody", null, s.items.map(function (i) {
      return h("tr", null, h("td", null, i.name || ""), h("td", null, i.sku ? h("a", {href: "#/inventory?q=" + encodeURIComponent(i.sku)}, i.sku) : ""),
        h("td", null, i.serial_number || ""), h("td", {class: "hide-sm"}, i.item_key), h("td", {class: "num"}, money(i.price)));
    }), h("tr", null, h("td", {colspan: "4", class: "sub"}, "Tax"), h("td", {class: "num sub"}, money(s.tax))))));
}
function salesTable(rows, withCustomer) {
  return h("div", {class: "card"}, h("table", null,
    h("thead", null, h("tr", null, h("th", null, ""), h("th", null, "Date"), h("th", null, "Sale"), withCustomer ? h("th", null, "Customer") : null,
      h("th", {class: "num hide-sm"}, "Pieces"), h("th", {class: "num"}, "Total before tax"), h("th", {class: "hide-sm"}, "Type"))),
    h("tbody", null, rows.map(function (s) {
      var c = s.customer, gone = s.status === "cancelled";
      var name = c ? ([c.first_name, c.last_name].filter(Boolean).join(" ") || c.key) : "";
      return openable([h("td", null, day(s.date)), h("td", {class: "sku"}, s.sale_key),
        withCustomer ? h("td", null, c ? h("a", {href: "#/customer/" + encodeURIComponent(c.key)}, name) : h("span", {class: "sub"}, "No customer on the sale")) : null,
        h("td", {class: "num hide-sm"}, s.items.length), h("td", {class: "num" + (gone ? " cancelled" : "")}, money(s.total)),
        h("td", {class: "hide-sm " + (gone ? "off" : "")}, gone ? "Cancelled" : s.type === "layaway" ? "Layaway" : "Sale")],
        withCustomer ? 7 : 6, function (box) { saleLines(box, s); });
    }))));
}
function sales(params) {
  var app = document.getElementById("app"), q = params.get("q") || "", page = 0, list = h("div"), stats = h("div", {class: "stats"});
  put(app, h("h1", null, "Sales"), stats,
    h("div", {class: "bar"}, searchBox("Search sale number, customer, SKU or serial number", q, function (v) { q = v; page = 0; load(); })), list);
  api("summary").then(function (s) {
    put(stats, stat(count(s.sales), "sales on record"), stat(money(s.sales_total), "total before tax"), stat(count(s.layaways), "open layaways"));
  }).catch(function () {});
  function load() {
    api("sales?page=" + page + "&q=" + encodeURIComponent(q)).then(function (res) {
      put(list, res.rows.length ? salesTable(res.rows, true)
        : h("div", {class: "card empty"}, q ? "Nothing matches that search." : "No sales yet."),
        pager(res, page, function (p) { page = p; load(); window.scrollTo(0, 0); }));
    }).catch(fail(list));
  }
  load();
}

// ---- Customers
function customers(params) {
  var app = document.getElementById("app"), q = params.get("q") || "", page = 0, list = h("div"), stats = h("div", {class: "stats"});
  put(app, h("h1", null, "Customers"), stats,
    h("div", {class: "bar"}, searchBox("Search name, email, phone or customer number", q, function (v) { q = v; page = 0; load(); })), list);
  api("summary").then(function (s) {
    put(stats, stat(count(s.customers), "customers on record"), stat(count(s.buyers), "have bought"));
  }).catch(function () {});
  function load() {
    api("customers?page=" + page + "&q=" + encodeURIComponent(q)).then(function (res) {
      put(list, res.rows.length ? h("div", {class: "card"}, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Email"), h("th", {class: "hide-sm"}, "Phone"),
          h("th", {class: "num"}, "Sales"), h("th", {class: "num hide-sm"}, "Total spent"))),
        h("tbody", null, res.rows.map(function (c) {
          var go = function () { location.hash = "#/customer/" + encodeURIComponent(c.customer_key); };
          var name = [c.first_name, c.last_name].filter(Boolean).join(" ") || c.company || c.customer_key;
          return h("tr", {class: "open-able", tabindex: "0", onclick: go, onkeydown: function (e) { if (e.key === "Enter") go(); }},
            h("td", null, h("span", {class: "sku"}, name), h("div", {class: "sub"}, c.customer_key)), h("td", null, c.email || ""),
            h("td", {class: "hide-sm"}, c.phone || ""), h("td", {class: "num " + (c.sales ? "in" : "off")}, c.sales || 0),
            h("td", {class: "num hide-sm"}, c.sales ? money(c.total_spent) : ""));
        })))) : h("div", {class: "card empty"}, q ? "Nothing matches that search." : "No customers yet."),
        pager(res, page, function (p) { page = p; load(); window.scrollTo(0, 0); }));
    }).catch(fail(list));
  }
  load();
}
function rows(pairs) {
  var kept = pairs.filter(function (p) { return p[1] != null && p[1] !== "" && !(Array.isArray(p[1]) && !p[1].length); });
  if (!kept.length) return h("div", {class: "panel"}, h("div", {class: "row"}, h("dt", null, "Nothing on file"), h("dd", null, "")));
  return h("div", {class: "panel"}, h("dl", null, kept.map(function (p) {
    return h("div", {class: "row"}, h("dt", null, p[0]), h("dd", null, Array.isArray(p[1]) ? p[1].join("\n") : p[1]));
  })));
}
function customer(key) {
  var app = document.getElementById("app");
  put(app, h("div", {class: "sub"}, "Loading"));
  api("customer?key=" + encodeURIComponent(key)).then(function (d) {
    var c = d.customer, live = d.sales.filter(function (s) { return s.status !== "cancelled"; });
    var spent = live.reduce(function (t, s) { return t + s.total; }, 0);
    var name = [c.first_name, c.last_name].filter(Boolean).join(" ") || c.company || c.customer_key;
    var isDate = /^\d{4}-\d{2}-\d{2}T/;
    put(app, h("a", {class: "back", href: "#/customers"}, "All customers"), h("h1", null, name),
      h("div", {class: "stats"}, stat(count(live.length), live.length === 1 ? "sale" : "sales"), stat(money(spent), "spent before tax"),
        c.date_entered ? stat(day(c.date_entered), "customer since") : null),
      c.inactive ? h("p", {class: "note"}, "Edge marks this customer as inactive.") : null,
      h("div", {class: "cols"},
        h("div", null, h("h2", {style: "margin-top:0"}, "Contact"), rows([["Customer number", c.customer_key], ["Company", c.company], ["Email", c.emails], ["Phone", c.phones],
          ["Address", c.addresses.map(function (a) {
            return (a.type ? a.type + ": " : "") + [a.street, a.street2, [a.city, a.state, a.zip].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ");
          })]])),
        h("div", null, h("h2", {style: "margin-top:0"}, "Everything else Edge holds"), rows(d.extra.map(function (p) {
          return [p[0], typeof p[1] === "string" && isDate.test(p[1]) ? day(p[1]) : p[1] === true ? "Yes" : p[1] === false ? "No" : p[1]];
        })))),
      h("h2", null, "Purchases"),
      d.sales.length ? salesTable(d.sales, false) : h("div", {class: "card empty"}, "No sales on record for this customer."));
  }).catch(fail(app));
}

// ---- Settings
var FIELDS = [
  ["timezone", "Time zone", "Edge sends times with no zone. This is the store's zone, like America/Toronto.", "text"],
  ["currency", "Currency", "Three-letter code, like CAD.", "text"],
  ["sku_fields", "SKU fields", "The Edge fields that group identical pieces, in order. The first one filled in wins. Changing this regroups the inventory on the next run.", "list"],
  ["exclude_item_prefixes", "Leave out item numbers starting with", "Pieces whose item number starts with any of these are not counted as stock. Separate with commas.", "list"],
  ["phone_country_code", "Country calling code", "Used to clean phone numbers that have no country code. 1 for Canada and the US.", "text"],
  ["retention_days", "Keep processed files for (days)", "Files from Edge hold customer details. 0 deletes each file as soon as it is processed.", "number"],
  ["sales_lookback_days", "Send sales from the last (days)", "Older sales are stored but not sent to your destinations. 0 sends everything.", "number"],
  ["default_layer", "Layer the API answers with", "Bronze is exactly what Edge sent. Silver is cleaned and checked. Gold is silver plus your own rules, above.", ["bronze", "silver", "gold"]],
  ["requests_per_minute", "API requests per minute, per key", "", "number"],
  ["max_limit", "Most records in one API answer", "Up to 900.", "number"]
];
function settings() {
  var app = document.getElementById("app");
  put(app, h("div", {class: "sub"}, "Loading"));
  api("settings").then(function (d) {
    var inputs = {}, errs = {}, status = h("span", {class: "sub"}), save = {disabled: false};
    var fields = FIELDS.map(function (f) {
      var v = d.editable[f[0]], input;
      if (Array.isArray(f[3])) input = h("select", {id: "f-" + f[0]}, f[3].map(function (o) { return h("option", {value: o, selected: o === v}, o.charAt(0).toUpperCase() + o.slice(1)); }));
      else input = h("input", {id: "f-" + f[0], type: f[3] === "number" ? "number" : "text", min: f[3] === "number" ? "0" : null,
        value: Array.isArray(v) ? v.join(", ") : v, autocomplete: "off", spellcheck: "false"});
      inputs[f[0]] = input; errs[f[0]] = h("div", {class: "err"});
      return h("div", {class: "field"}, h("label", {for: "f-" + f[0]}, f[1]), f[2] ? h("div", {class: "sub"}, f[2]) : null, input, errs[f[0]]);
    });
    // Gold rules: one sentence per rule.
    var G = d.gold, rules = (d.editable.gold_rules || []).map(function (r) { return Object.assign({}, r); });
    var KIND = {inventory: "Inventory", customers: "Customers", sales: "Sales"};
    var OPS = {is: "is", is_not: "is not", contains: "contains", starts_with: "starts with", ends_with: "ends with", is_empty: "is empty", is_not_empty: "is filled in"};
    function nice(f) { return f.replace(/_/g, " "); }
    function pick(options, value, onchange, label) {
      return h("select", {"aria-label": label, onchange: function (e) { onchange(e.target.value); }},
        options.map(function (o) { return h("option", {value: o[0], selected: o[0] === value}, o[1]); }));
    }
    var ruleBox = h("div"), ruleErr = h("div", {class: "err"});
    function drawRules() {
      put.apply(null, [ruleBox].concat(rules.length ? rules.map(function (r, n) {
        var f = G.fields[r.on], canSet = f.set.length > 0, noValue = r.op === "is_empty" || r.op === "is_not_empty";
        function redraw(fn) { return function (v) { fn(v); drawRules(); }; }
        return h("div", {class: "rule"}, h("span", {class: "n"}, n + 1),
          pick(Object.keys(KIND).map(function (k) { return [k, KIND[k]]; }), r.on, redraw(function (v) {
            r.on = v; r.field = G.fields[v].match[0]; r.action = G.fields[v].set.length ? r.action : "exclude"; r.target = G.fields[v].set[0]; }), "Record"),
          h("span", {class: "word"}, "when"),
          pick(f.match.map(function (x) { return [x, nice(x)]; }), r.field, redraw(function (v) { r.field = v; if (f.set.indexOf(v) >= 0) r.target = v; }), "Field"),
          pick(G.ops.map(function (o) { return [o, OPS[o]]; }), r.op, redraw(function (v) { r.op = v; }), "Test"),
          noValue ? null : h("input", {type: "text", value: r.value || "", placeholder: "what Edge has", "aria-label": "Value", oninput: function (e) { r.value = e.target.value; }}),
          pick(canSet ? [["set", "change"], ["exclude", "leave it out"]] : [["exclude", "leave it out"]], r.action, redraw(function (v) { r.action = v; if (v === "set" && !r.target) r.target = f.set[0]; }), "Action"),
          r.action === "set" ? pick(f.set.map(function (x) { return [x, nice(x)]; }), r.target, redraw(function (v) { r.target = v; }), "Field to change") : null,
          r.action === "set" ? h("span", {class: "word"}, "to") : null,
          r.action === "set" ? h("input", {type: "text", value: r.to || "", placeholder: "what it should be", "aria-label": "New value", oninput: function (e) { r.to = e.target.value; }}) : null,
          h("button", {class: "plain", type: "button", onclick: function () { rules.splice(n, 1); drawRules(); }}, "Remove"));
      }) : [h("div", {class: "sub", style: "padding:14px 0"}, "No rules yet, so gold matches silver.")]));
    }
    function addRule() { rules.push({on: "inventory", field: "vendor", op: "is", value: "", action: "set", target: "vendor", to: ""}); drawRules(); }
    drawRules();
    function submit(e) {
      e.preventDefault();
      var body = {gold_rules: rules};
      ruleErr.textContent = "";
      FIELDS.forEach(function (f) {
        var v = inputs[f[0]].value;
        body[f[0]] = f[3] === "number" ? (/^\d+$/.test(v.trim()) ? Number(v) : v) : v;
        errs[f[0]].textContent = "";
      });
      save.disabled = true; status.textContent = "Saving";
      fetch("api/settings", {method: "POST", headers: {"Content-Type": "application/json", "X-EDT-Settings": "1"}, body: JSON.stringify(body)})
        .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
        .then(function (res) {
          save.disabled = false;
          if (res.ok) { status.textContent = "Saved. The API and the next run use the new settings."; CUR = body.currency.toUpperCase(); return; }
          var es = res.j.errors || {}, n = 0;
          Object.keys(es).forEach(function (k) { if (errs[k]) { errs[k].textContent = es[k]; n++; } });
          if (es.gold_rules) { ruleErr.textContent = es.gold_rules; n++; }
          status.textContent = n ? "Not saved. Fix the fields marked above." : "Not saved. " + (es._ || res.j.error || "");
        }).catch(function () { save.disabled = false; status.textContent = "Not saved. The server did not answer."; });
    }
    var i = d.info, access = function (v) { return v ? "On, through " + v : "Off: reachable from this machine only"; };
    put(app, h("h1", null, "Settings"),
      h("div", {class: "cols"},
        h("form", {class: "wide", id: "settings-form", onsubmit: submit},
          h("h2", {style: "margin-top:0"}, "Gold rules"),
          h("p", {class: "sub", style: "margin:0 0 12px;max-width:640px"}, "Your own corrections, applied on top of the cleaned data, in order. Use them for the spellings and labels your team gets wrong in Edge, or to leave records out. They only change what the gold layer answers with."),
          h("div", {class: "panel"}, ruleBox, ruleErr, h("div", {class: "actions"}, h("button", {class: "plain", type: "button", onclick: addRule}, "Add a rule")))),
        h("div", {class: "form"}, h("h2", {style: "margin-top:0"}, "Store and API"), h("div", {class: "panel"}, fields)),
        h("div", null,
          h("h2", {style: "margin-top:0"}, "Access"),
          rows([["This interface", access(i.ui_access)], ["Data API", access(i.api_access)], ["API port", i.api_port]]),
          h("h2", null, "API keys"),
          i.keys.length ? h("div", {class: "card"}, h("table", null,
            h("thead", null, h("tr", null, h("th", null, "For"), h("th", null, "Can read"), h("th", null, "Status"))),
            h("tbody", null, i.keys.map(function (k) {
              return h("tr", null, h("td", null, k.name, h("div", {class: "sub"}, k.id)), h("td", null, k.scopes.split(",").join(", ")),
                h("td", {class: k.state === "active" ? "in" : "off"}, k.state.charAt(0).toUpperCase() + k.state.slice(1)));
            })))) : h("div", {class: "card empty"}, "No API keys yet."),
          h("h2", null, "Destinations"),
          i.destinations.length ? rows(i.destinations.map(function (x) { return [x.name, x.type + ": " + x.events.join(", ")]; }))
            : h("div", {class: "card empty"}, "Nothing is being sent anywhere."),
          h("h2", null, "Folders"),
          rows([["Files from Edge", i.inbox], ["Database", i.data_dir]]),
          h("p", {class: "note", style: "margin-top:24px"}, "Folders, destinations, API keys, passwords and Cloudflare Access are set on the server, in ",
            h("code", null, "config.json"), " and with the ", h("code", null, "apikey"), " command. They cannot be changed from a browser.")),
        h("div", {class: "wide actions"}, h("button", {class: "primary", type: "submit", form: "settings-form"}, "Save settings"), status)));
  }).catch(fail(app));
}

function route() {
  var parts = location.hash.replace(/^#\/?/, "").split("?"), path = parts[0].split("/"), params = new URLSearchParams(parts[1] || "");
  var view = path[0] || "inventory";
  [].forEach.call(document.querySelectorAll("nav a"), function (a) {
    a.className = (a.dataset.v === view || (view === "customer" && a.dataset.v === "customers")) ? "on" : "";
  });
  window.scrollTo(0, 0);
  if (view === "settings") settings();
  else if (view === "sales") sales(params);
  else if (view === "customers") customers(params);
  else if (view === "customer") customer(decodeURIComponent(path.slice(1).join("/")));
  else inventory(params);
}
window.addEventListener("hashchange", route);
api("summary").then(function (s) {
  CUR = s.currency || CUR;
  var bits = [];
  if (s.last_file) bits.push("Last file from Edge: " + new Date(s.last_file).toLocaleString());
  if (s.failed) bits.push(s.failed + " deliveries failed, run the retry command");
  document.getElementById("foot").textContent = bits.length ? " · " + bits.join(" · ") : "";
}).catch(function () {}).then(route);
</script>
</body>
</html>
"""

PAGE_SIZE = 50
CUSTOMER_SHOWN = {"CustomerKey", "CustomerFirstName", "CustomerLastName", "CustomerCompany",
                  "CustomerInactive", "CustomerDateEntered", "CustomerImage", "CustomerTransfer"}


def label_of(field):
    """CustomerWeddingAnniv -> Wedding anniv"""
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"^Customer", "", field)).split()
    return " ".join(words).capitalize()


def ui_query(db, cfg, path, q):
    """Answer one /api/ request. `q` is a dict of query-string values."""
    page = min(max(0, int(q.get("page", "0") or 0)), 1000000)
    like = f"%{q.get('q', '').strip()}%"
    searching = bool(q.get("q", "").strip())
    one = lambda sql, args=(): db.execute(sql, args).fetchone()[0]

    if path == "summary":
        last = db.execute("SELECT processed_at FROM files ORDER BY processed_at DESC LIMIT 1").fetchone()
        live = "json_extract(payload, '$.status') <> 'cancelled'"
        return {
            "currency": cfg["currency"],
            "skus": one("SELECT COUNT(*) FROM skus"),
            "skus_in_stock": one("SELECT COUNT(*) FROM skus WHERE available > 0"),
            "pieces_available": one("SELECT COUNT(*) FROM items WHERE state='available'"),
            "sales": one(f"SELECT COUNT(*) FROM sales WHERE {live}"),
            "sales_total": one(f"SELECT ROUND(COALESCE(SUM(json_extract(payload, '$.total')), 0), 2) "
                               f"FROM sales WHERE {live}"),
            "layaways": one(f"SELECT COUNT(*) FROM sales WHERE {live} "
                            "AND json_extract(payload, '$.type') = 'layaway'"),
            "customers": one("SELECT COUNT(*) FROM customers"),
            "buyers": one("SELECT COUNT(DISTINCT customer_key) FROM sale_lines WHERE customer_key <> ''"),
            "last_file": last[0] if last else None,
            "failed": one("SELECT COUNT(*) FROM outbox WHERE state='failed'"),
        }

    if path == "skus":
        where, args = [], []
        if searching:
            where.append("(sku LIKE ? OR name LIKE ? OR vendor LIKE ? OR sku IN "
                         "(SELECT sku FROM pieces WHERE serial_number LIKE ? OR item_key LIKE ?))")
            args += [like] * 5
        if q.get("stock") == "in":
            where.append("available > 0")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = db.execute(f"SELECT * FROM skus{clause} ORDER BY sku LIMIT ? OFFSET ?",
                          args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
        return {"total": one(f"SELECT COUNT(*) FROM skus{clause}", args),
                "unreferenced": one("SELECT COUNT(*) FROM unreferenced"),
                "rows": [dict(r) for r in rows]}

    if path == "pieces":
        sku = q.get("sku", "")
        match = "sku = ?" if sku else "(sku IS NULL OR sku = ?)"
        rows = db.execute(
            f"SELECT * FROM pieces WHERE {match} AND state <> 'removed' ORDER BY CASE state "
            "WHEN 'available' THEN 0 WHEN 'layaway' THEN 1 WHEN 'sold' THEN 2 ELSE 3 END, item_key "
            "LIMIT 500", (sku,)).fetchall()
        return [dict(r) for r in rows]

    if path == "sales":
        clause, args = "", []
        if searching:
            clause, args = " WHERE sale_key LIKE ? OR payload LIKE ?", [like, like]
        rows = db.execute(f"SELECT payload FROM sales{clause} ORDER BY json_extract(payload, '$.date') "
                          "DESC LIMIT ? OFFSET ?", args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
        return {"total": one(f"SELECT COUNT(*) FROM sales{clause}", args),
                "rows": [json.loads(r["payload"]) for r in rows]}

    if path == "customers":
        clause, args = "", []
        if searching:
            clause, args = " WHERE c.customer_key LIKE ? OR c.data LIKE ?", [like, like]
        rows = db.execute(
            "SELECT c.customer_key, json_extract(c.data, '$.CustomerFirstName') AS first_name, "
            "json_extract(c.data, '$.CustomerLastName') AS last_name, "
            "json_extract(c.data, '$.CustomerCompany') AS company, "
            "json_extract(c.data, '$.Emails[0].PairValue.EmailEmail') AS email, "
            "json_extract(c.data, '$.Phones[0].PairValue.PhonePhone') AS phone, "
            "a.sales, a.total_spent FROM customers c LEFT JOIN ("
            "  SELECT customer_key AS ck, COUNT(DISTINCT sale_key) AS sales, "
            "  ROUND(SUM(sale_price), 2) AS total_spent FROM sale_lines GROUP BY customer_key) a "
            f"ON a.ck = c.customer_key{clause} "
            "ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE LIMIT ? OFFSET ?",
            args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
        return {"total": one(f"SELECT COUNT(*) FROM customers c{clause}", args),
                "rows": [dict(r) for r in rows]}

    if path == "customer":
        row = db.execute("SELECT data FROM customers WHERE customer_key=?", (q.get("key", ""),)).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        extra = [[label_of(k), v] for k, v in data.items()
                 if k not in CUSTOMER_SHOWN and not isinstance(v, (list, dict)) and v not in (None, "")]
        sales = db.execute("SELECT payload FROM sales WHERE json_extract(payload, '$.customer.key') = ? "
                           "ORDER BY json_extract(payload, '$.date') DESC", (q["key"],)).fetchall()
        return {"customer": customer_event(q["key"], data), "extra": extra,
                "sales": [json.loads(r["payload"]) for r in sales]}
    return None


def settings_view(cfg, db):
    """What the settings page shows. Secrets are never sent, only whether they are set."""
    api = dict(EXAMPLE_CONFIG["api"])
    api.update(cfg.get("api") or {})

    def access(block):
        a = (block or {}).get("cloudflare_access") or {}
        on = bool(a.get("aud")) and not str(a.get("team_domain") or "").startswith("yourteam.")
        return a.get("team_domain") if on else None
    return {
        "editable": {
            "timezone": cfg["timezone"], "currency": cfg["currency"],
            "sku_fields": cfg["sku_fields"], "exclude_item_prefixes": cfg["exclude_item_prefixes"],
            "retention_days": cfg["retention_days"], "sales_lookback_days": cfg["sales_lookback_days"],
            "phone_country_code": str(cfg.get("phone_country_code") or "1"),
            "default_layer": api.get("default_layer") or "silver",
            "requests_per_minute": api["requests_per_minute"], "max_limit": api["max_limit"],
            "gold_rules": gold_rules(cfg),
        },
        "gold": {"fields": GOLD_FIELDS, "ops": GOLD_OPS},
        "info": {
            "inbox": cfg["inbox"], "data_dir": cfg["data_dir"], "api_port": api["port"],
            "ui_access": access(cfg.get("ui")), "api_access": access(api),
            "destinations": [{"name": d.get("name"), "type": d.get("type") or "webhook",
                              "events": d.get("events") or ["all changes"]} for d in cfg["destinations"]],
            "keys": [{"id": r["id"], "name": r["name"], "scopes": r["scopes"],
                      "state": "revoked" if r["revoked_at"] else "expired" if r["expires_at"]
                      and r["expires_at"] <= now_iso() else "active"}
                     for r in db.execute("SELECT * FROM api_keys ORDER BY created_at")],
        },
    }


def settings_clean(body):
    """Check what the settings page sent. Returns (changes, errors). Only these fields can change:
    never a folder, a destination, a secret or the Cloudflare settings."""
    changes, errors = {"api": {}}, {}

    def words(value, pattern, most):
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return None
        out = [str(v).strip() for v in value if str(v).strip()]
        return out if len(out) <= most and all(re.match(pattern, v) for v in out) else None

    def whole(value, low, high):
        ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        return value if ok else None

    if "timezone" in body:
        tz = str(body["timezone"]).strip()
        try:
            if not ZoneInfo or not re.match(r"^[A-Za-z0-9_+/-]{1,64}$", tz):
                raise ValueError
            ZoneInfo(tz)
            changes["timezone"] = tz
        except Exception:
            errors["timezone"] = "Not a time zone. Use a name like America/Toronto."
    if "currency" in body:
        cur = str(body["currency"]).strip().upper()
        if re.match(r"^[A-Z]{3}$", cur):
            changes["currency"] = cur
        else:
            errors["currency"] = "Use a three-letter code like CAD."
    if "sku_fields" in body:
        fields = words(body["sku_fields"], r"^Item[A-Za-z0-9]{1,40}$", 6)
        if fields:
            changes["sku_fields"] = fields
        else:
            errors["sku_fields"] = "One to six Edge item fields, like ItemVendStyleCode."
    if "exclude_item_prefixes" in body:
        prefixes = words(body["exclude_item_prefixes"], r"^[A-Za-z0-9 ._/-]{1,30}$", 50)
        if prefixes is None:
            errors["exclude_item_prefixes"] = "Letters, numbers and dashes only, separated by commas."
        else:
            changes["exclude_item_prefixes"] = prefixes
    for name, low, high, where in (("retention_days", 0, 3650, changes),
                                   ("sales_lookback_days", 0, 3650, changes),
                                   ("requests_per_minute", 1, 6000, changes["api"]),
                                   ("max_limit", 1, 900, changes["api"])):
        if name in body:
            value = whole(body[name], low, high)
            if value is None:
                errors[name] = f"A whole number from {low} to {high}."
            else:
                where[name] = value
    if "phone_country_code" in body:
        cc = str(body["phone_country_code"]).strip().lstrip("+")
        if re.match(r"^[1-9]\d{0,2}$", cc):
            changes["phone_country_code"] = cc
        else:
            errors["phone_country_code"] = "The country calling code, like 1 or 44."
    if "default_layer" in body:
        if body["default_layer"] in LAYERS:
            changes["api"]["default_layer"] = body["default_layer"]
        else:
            errors["default_layer"] = "bronze, silver or gold."
    if "gold_rules" in body:
        rules, error = rules_clean(body["gold_rules"])
        if error:
            errors["gold_rules"] = error
        else:
            changes["gold_rules"] = rules
    unknown = set(body) - set(EXAMPLE_CONFIG) - {"default_layer", "requests_per_minute", "max_limit"}
    locked = set(body) & {"inbox", "data_dir", "destinations", "ui", "api", "heartbeat_url",
                          "settle_seconds", "poll_seconds"}
    if unknown or locked:
        errors["_"] = "That setting cannot be changed from this page."
    return changes, errors


_config_lock = threading.Lock()


def settings_write(path, changes):
    """Merge the changes into the config file. Written whole, so a crash cannot leave half a file."""
    with _config_lock:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        api = changes.pop("api", {})
        doc.update(changes)
        if api:
            doc["api"] = dict(doc.get("api") or {}, **api)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return load_config(path)


def cmd_serve(cfg, config_path=None):
    import base64
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qsl

    os.umask(0o077)  # anything this process creates is for this user only
    ui = cfg["ui"]
    host, port, password = ui.get("host", "127.0.0.1"), int(ui.get("port", 8787)), secret(ui, "password")
    if host not in LOOPBACK:
        sys.exit("The interface shows customer details, so it only listens on this machine "
                 "(ui.host 127.0.0.1).\nTo reach it from elsewhere, point a Cloudflare Tunnel at it "
                 "and set ui.cloudflare_access.")
    verifier = make_verifier(ui.get("cloudflare_access"), "ui")
    bad = Window(10, 300)  # wrong passwords per caller, then locked out
    open_db(cfg).close()  # make sure the database and its views exist
    db_uri = "file:" + quote(os.path.join(cfg["data_dir"], "edt.db")) + "?mode=ro"
    live = {"cfg": cfg}  # replaced when the settings page saves

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "edt-bridge", ""
        timeout = 15  # a caller that stalls is dropped

        def reply(self, status, body, ctype):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; frame-ancestors 'none'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Edge Bridge"')
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            if not password:
                return True
            try:
                kind, token = self.headers.get("Authorization", "").split(" ", 1)
                given = base64.b64decode(token).decode("utf-8").split(":", 1)[1]
            except (ValueError, IndexError, UnicodeDecodeError):
                return False
            return kind == "Basic" and hmac.compare_digest(given.encode(), password.encode())

        def gate(self):
            """True when the caller may come in. Otherwise the refusal has been sent."""
            caller = "local"
            if not came_through_proxy(self.headers) and not local_host(self.headers, port):
                self.reply(403, "Forbidden", "text/plain; charset=utf-8")
                return False
            if came_through_proxy(self.headers):
                # Through a tunnel: only with the token Cloudflare Access signs.
                if not verifier or not verifier.verify(self.headers.get("Cf-Access-Jwt-Assertion", "")):
                    self.reply(403, "Forbidden", "text/plain; charset=utf-8")
                    return False
                ip = self.headers.get("Cf-Connecting-Ip", "")
                caller = ip if re.match(r"^[0-9a-fA-F:.]{2,45}$", ip) else "unknown"
            if password:
                if bad.wait(caller):
                    self.reply(429, "Too many wrong passwords. Try later.", "text/plain; charset=utf-8")
                    return False
                if not self.allowed():
                    if self.headers.get("Authorization"):
                        bad.hit(caller)
                    self.reply(401, "Password needed", "text/plain; charset=utf-8")
                    return False
            return True

        def do_POST(self):
            """Only one thing can be changed from the browser: the settings."""
            self.close_connection = True
            if not self.gate():
                return
            js = "application/json"
            if urlparse(self.path).path != "/api/settings" or not config_path:
                return self.reply(404, json.dumps({"error": "not found"}), js)
            # A page on another site cannot send these: the header forces a browser check
            # we never approve, and the Origin has to be this interface.
            origin, host_header = self.headers.get("Origin"), self.headers.get("Host", "")
            if self.headers.get("X-EDT-Settings") != "1" \
                    or not self.headers.get("Content-Type", "").startswith("application/json") \
                    or (origin and urlparse(origin).netloc != host_header):
                return self.reply(403, json.dumps({"error": "forbidden"}), js)
            try:
                size = int(self.headers.get("Content-Length", ""))
                if not 0 < size <= 20000:
                    raise ValueError
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, json.dumps({"error": "bad request"}), js)
            changes, errors = settings_clean(body)
            if errors:
                return self.reply(400, json.dumps({"errors": errors}), js)
            try:
                live["cfg"] = settings_write(config_path, changes)
            except (OSError, ValueError, SystemExit) as e:
                log.error("could not save settings: %s", e)
                return self.reply(500, json.dumps({"error": "could not save"}), js)
            log.info("settings changed from the interface: %s",
                     ", ".join(sorted(k for k in body)))
            self.reply(200, json.dumps({"saved": True}), js)

        def do_GET(self):
            if not self.gate():
                return
            cfg = live["cfg"]
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self.reply(200, PAGE, "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self.reply(404, "Not found", "text/plain; charset=utf-8")
            db = sqlite3.connect(db_uri, uri=True)
            db.row_factory = sqlite3.Row
            try:
                if url.path == "/api/settings":
                    result = settings_view(cfg, db)
                else:
                    result = ui_query(db, cfg, url.path[5:], dict(parse_qsl(url.query)))
            except Exception as e:
                log.error("interface query failed: %s: %s", type(e).__name__, e)
                return self.reply(500, json.dumps({"error": "query failed"}), "application/json")
            finally:
                db.close()
            if result is None:
                return self.reply(404, json.dumps({"error": "not found"}), "application/json")
            self.reply(200, json.dumps(result, default=str), "application/json")

        def log_message(self, fmt, *args):  # searches can hold names; keep them out of the log
            pass

    log.info("Interface on http://%s:%d (%s; %s)", host, port,
             "password on" if password else "no password",
             "tunnel allowed with Cloudflare Access" if verifier else "this machine only, tunnels refused")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


# --------------------------------------------------------------------------
# Data API (python3 edt_bridge.py api)
# --------------------------------------------------------------------------

API_SCOPES = ("inventory", "sales", "customers", "bronze")
LAYERS = ("bronze", "silver", "gold")
API_KEY_RE = re.compile(r"^edt_([a-f0-9]{12})_([A-Za-z0-9_-]{43})$")
TEAM_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
PIECE_COLS = ("item_key, sku, serial_number, state, store_id, location, name, vendor, category, "
              "vendor_style_code, style, retail_price, current_price, sale_key, sale_date, "
              "sale_price, updated_at")


PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip",
                 "forwarded", "cdn-loop", "via")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class BadRequest(Exception):
    pass


def came_through_proxy(headers):
    """True when a request arrived through Cloudflare or any other proxy or tunnel."""
    names = [h.lower() for h in headers.keys()]
    return any(h.startswith("cf-") or h in PROXY_HEADERS for h in names)


def local_host(headers, port):
    """True when the Host header is this machine. A page on another site whose name was
    re-pointed at 127.0.0.1 (DNS rebinding) arrives with its own name here, and is refused."""
    host = headers.get("Host", "").strip().lower()
    return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")


def make_verifier(access, what):
    """An AccessVerifier from a cloudflare_access config block. None when it is not filled in."""
    access = access or {}
    team, aud = str(access.get("team_domain") or "").strip().lower(), str(access.get("aud") or "").strip()
    if not aud and (not team or team.startswith("yourteam.")):
        return None
    if not TEAM_DOMAIN_RE.match(team) or not re.match(r"^[a-f0-9]{64}$", aud):
        sys.exit(f"{what}.cloudflare_access needs team_domain (yourteam.cloudflareaccess.com) and "
                 "aud (the 64-character Application Audience tag).")
    verifier = AccessVerifier(team, aud)
    try:
        verifier.fetch()
    except Exception as e:
        sys.exit(f"Could not load Cloudflare Access signing keys from {team}: {e}")
    return verifier


def key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def cmd_apikey(cfg, action, arg, opts):
    db = open_db(cfg)
    if action == "create":
        scopes = [s.strip() for s in (opts.scopes or "").split(",") if s.strip()]
        if not re.match(r"^[\w .-]{1,60}$", opts.name or ""):
            sys.exit("apikey create needs --name (letters, numbers, spaces, dots, dashes)")
        if not scopes or any(s not in API_SCOPES for s in scopes):
            sys.exit("apikey create needs --scopes, any of: " + ",".join(API_SCOPES))
        if opts.expires_days is not None and opts.expires_days < 1:
            sys.exit("--expires-days must be 1 or more")
        expires = None
        if opts.expires_days:
            expires = (datetime.now(timezone.utc) + timedelta(days=opts.expires_days)).isoformat(
                timespec="seconds")
        kid = secrets.token_hex(6)
        key = f"edt_{kid}_{secrets.token_urlsafe(32)}"
        db.execute("INSERT INTO api_keys (id, name, hash, scopes, created_at, expires_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (kid, opts.name, key_hash(key), ",".join(sorted(set(scopes))), now_iso(), expires))
        db.commit()
        print(f"Key for {opts.name} ({', '.join(sorted(set(scopes)))})"
              + (f", expires {expires}" if expires else "") + ":\n")
        print(f"  {key}\n")
        print("This is the only time it is shown. Only a hash is stored. "
              f"To cancel it: apikey revoke {kid}")
    elif action == "list":
        rows = db.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        if not rows:
            print("No API keys yet. Make one: apikey create --name NAME --scopes inventory,sales")
        for r in rows:
            state = "revoked " + r["revoked_at"] if r["revoked_at"] else (
                "expired" if r["expires_at"] and r["expires_at"] <= now_iso() else "active")
            print(f"  {r['id']}  {r['name']:<24}{r['scopes']:<28}{state}"
                  + (f"  (expires {r['expires_at']})" if state == "active" and r["expires_at"] else ""))
    elif action == "revoke":
        n = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                       (now_iso(), arg or "")).rowcount
        db.commit()
        print("Revoked. It stops working on the next request." if n else "No active key with that id.")
    else:
        sys.exit("usage: edt_bridge.py apikey create --name NAME --scopes inventory,sales,customers\n"
                 "       edt_bridge.py apikey list\n"
                 "       edt_bridge.py apikey revoke <id>")


def b64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AccessVerifier:
    """Checks the token Cloudflare Access signs for every request it lets through.

    A request that did not come through Access (someone on this machine, a
    misrouted tunnel, a second hostname without a policy) has no valid token.
    RS256 is verified here with plain integer maths, so no packages are needed.
    """

    def __init__(self, team_domain, aud):
        self.issuer = "https://" + team_domain
        self.aud = aud
        self.keys, self.fetched, self.tried = {}, 0.0, 0.0
        self.lock = threading.Lock()

    def fetch(self):
        self.tried = time.time()
        with urllib.request.urlopen(self.issuer + "/cdn-cgi/access/certs", timeout=10) as resp:
            doc = json.loads(resp.read(500000))
        keys = {}
        for k in doc.get("keys") or []:
            if k.get("kty") != "RSA" or not k.get("kid"):
                continue
            n = int.from_bytes(b64url(k["n"]), "big")
            e = int.from_bytes(b64url(k["e"]), "big")
            if n.bit_length() >= 2048 and e >= 3 and e % 2:
                keys[str(k["kid"])] = (n, e)
        if not keys:
            raise ValueError("Cloudflare returned no usable signing keys")
        self.keys, self.fetched = keys, time.time()

    def key(self, kid):
        with self.lock:
            now = time.time()
            stale = now - self.fetched > 3600
            if (stale or kid not in self.keys) and now - self.tried > 60:
                try:
                    self.fetch()
                except Exception as e:  # keep the keys we have; they rotate every few weeks
                    log.error("could not refresh Cloudflare Access keys: %s", e)
            if now - self.fetched > 86400:
                return None  # a day without Cloudflare's keys: stop trusting the old ones
            return self.keys.get(kid)

    def verify(self, token):
        """Return the claims if the token is genuine, for this application, and current."""
        try:
            if not token or len(token) > 8192:
                return None
            head, body, sig = token.split(".")
            header = json.loads(b64url(head))
            if header.get("alg") != "RS256":
                return None
            pub = self.key(str(header.get("kid") or ""))
            if not pub:
                return None
            n, e = pub
            size = (n.bit_length() + 7) // 8
            raw = b64url(sig)
            s = int.from_bytes(raw, "big")
            if len(raw) != size or s >= n:
                return None
            digest = hashlib.sha256(f"{head}.{body}".encode("ascii")).digest()
            want = (b"\x00\x01" + b"\xff" * (size - len(SHA256_DIGEST_INFO) - 35) + b"\x00"
                    + SHA256_DIGEST_INFO + digest)
            if not hmac.compare_digest(pow(s, e, n).to_bytes(size, "big"), want):
                return None
            claims = json.loads(b64url(body))
            aud = claims.get("aud")
            aud = aud if isinstance(aud, list) else [aud]
            now = time.time()
            if self.aud not in aud or claims.get("iss") != self.issuer:
                return None
            exp, nbf = claims.get("exp"), claims.get("nbf", 0)
            if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= now:
                return None
            if not isinstance(nbf, (int, float)) or nbf > now + 60:
                return None
            return claims
        except Exception:
            return None


class Window:
    """Counts hits per name over a sliding window."""

    def __init__(self, limit, seconds):
        self.limit, self.seconds, self.hits, self.lock = limit, seconds, {}, threading.Lock()

    def _live(self, name, now):
        hits = [t for t in self.hits.get(name, ()) if now - t < self.seconds]
        if hits:
            self.hits[name] = hits
        else:
            self.hits.pop(name, None)
        return hits

    def wait(self, name):
        """Seconds until `name` may go again. 0 means it is under the limit."""
        with self.lock:
            now = time.time()
            hits = self._live(name, now)
            return 0 if len(hits) < self.limit else max(1, int(self.seconds - (now - hits[0])) + 1)

    def hit(self, name):
        with self.lock:
            now = time.time()
            if len(self.hits) > 5000:  # do not let a flood of names eat memory
                for other in list(self.hits):
                    self._live(other, now)
            self._live(name, now)
            self.hits.setdefault(name, []).append(now)


def api_params(query, allowed):
    """The query string as a dict. Unknown, repeated or oversized values are refused."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=20) if query else []
    except ValueError:
        raise BadRequest("the query string is not valid")
    q = {}
    for name, value in pairs:
        if name not in allowed:
            raise BadRequest("unknown parameter; allowed: " + ", ".join(sorted(allowed)))
        if name in q:
            raise BadRequest(f"{name} was given twice")
        if len(value) > 200 or "\x00" in value:
            raise BadRequest(f"{name} is too long")
        q[name] = value
    return q


def p_choice(q, name, choices, default):
    value = q.get(name, default)
    if value not in choices:
        raise BadRequest(f"{name} must be one of: " + ", ".join(choices))
    return value


def p_limit(q, max_limit):
    try:
        limit = int(q.get("limit", min(100, max_limit)))
    except ValueError:
        raise BadRequest("limit must be a number")
    if not 1 <= limit <= max_limit:
        raise BadRequest(f"limit must be between 1 and {max_limit}")
    return limit


def p_time(q, name):
    """An ISO 8601 time (or a plain date), as the UTC text the database stores."""
    if not q.get(name):
        return None
    try:
        dt = datetime.fromisoformat(q[name].replace("Z", "+00:00").replace(" ", "+"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        raise BadRequest(f"{name} must be a time like 2026-01-31T14:00:00Z")


def p_day(q, name):
    if not q.get(name):
        return None
    try:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", q[name]):
            raise ValueError
        datetime.strptime(q[name], "%Y-%m-%d")
    except ValueError:
        raise BadRequest(f"{name} must be a date like 2026-01-31")
    return q[name]


def like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def marks(values):
    return ",".join("?" * len(values))


def page_of(rows, limit, key):
    """rows were fetched with LIMIT limit+1; the extra one says there is more."""
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (key(rows[-1]) if more and rows else None)


# ---- Layers. bronze: what Edge sent, untouched. silver: cleaned and checked.
# gold: silver plus the store's own rules (none yet, so it matches silver).

def clean_text(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def clean_name(value):
    """Tidy spacing; fix names typed all in capitals or all in lower case."""
    text = clean_text(value)
    if not text or not (text.isupper() or text.islower()):
        return text

    def cap(m):
        word = m.group().capitalize()
        return "Mc" + word[2:].capitalize() if word.startswith("Mc") and len(word) > 3 else word
    return re.sub(r"[^\W\d_]+", cap, text)


def clean_email(value):
    text = (clean_text(value) or "").strip("<>\"' ,;").lower().replace("mailto:", "")
    return text if re.match(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$", text) else None


def clean_phone(value, country_code="1"):
    """E.164 (+14165550100), or None when it cannot be a real number."""
    text = re.sub(r"(?i)\s*(x|ext\.?|#)\s*\d+\s*$", "", clean_text(value) or "")
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        pass
    elif digits.startswith("011"):
        digits = digits[3:]
    elif country_code == "1":
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        digits = "1" + digits
    else:
        digits = country_code + digits.lstrip("0")
    if digits.startswith("1"):  # North America: area code and exchange never start with 0 or 1
        return "+" + digits if re.match(r"^1[2-9]\d{2}[2-9]\d{6}$", digits) else None
    return "+" + digits if 8 <= len(digits) <= 15 else None


def to_number(value):
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").strip()
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def issue(code, message):
    return {"code": code, "message": message}


def db_stamp(cfg):
    path = os.path.join(cfg["data_dir"], "edt.db")
    return tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in (path, path + "-wal"))


_dupes = {"stamp": None, "value": None}
_dupes_lock = threading.Lock()


def duplicates(db, cfg):
    """Serial numbers and emails that appear on more than one record. Recounted when the data changes."""
    with _dupes_lock:
        stamp = db_stamp(cfg)
        if _dupes["stamp"] != stamp:
            serials = {r[0] for r in db.execute(
                "SELECT upper(trim(serial_number)) AS s FROM pieces WHERE state IN ('available', 'layaway') "
                "AND s <> '' GROUP BY s HAVING COUNT(*) > 1")}
            seen, emails = {}, set()
            for r in db.execute("SELECT customer_key, data FROM customers"):
                for e in json.loads(r["data"]).get("Emails") or []:
                    good = clean_email(unwrap(e).get("EmailEmail"))
                    if good and seen.setdefault(good, r["customer_key"]) != r["customer_key"]:
                        emails.add(good)
            _dupes.update(stamp=stamp, value={"serials": serials, "emails": emails})
        return _dupes["value"]


def silver_piece(p, dup):
    for field in ("sku", "name", "vendor", "category", "vendor_style_code", "style", "location"):
        if field in p:
            p[field] = clean_text(p[field])
    serial = (clean_text(p.get("serial_number")) or "").upper() or None
    p["serial_number"] = serial
    for field in ("retail_price", "current_price", "sale_price"):
        if field in p:
            p[field] = to_number(p[field])
    issues = []
    if not p.get("sku"):
        issues.append(issue("no_sku", "Edge sent this piece without a reference, so it cannot be grouped"))
    if p.get("state") == "available" and not (p.get("current_price") or p.get("retail_price")):
        issues.append(issue("no_price", "in stock with no price"))
    if p.get("state") in ("sold", "layaway") and "sale_key" in p and not p["sale_key"]:
        issues.append(issue("sold_without_sale", "marked sold or on layaway with no sale number"))
    if serial and serial in dup["serials"] and p.get("state") in ("available", "layaway"):
        issues.append(issue("duplicate_serial", f"serial number {serial} is on more than one piece in stock"))
    p["issues"] = issues
    return p


def silver_customer(c, cfg, dup):
    cc, issues = str(cfg.get("phone_country_code") or "1"), []
    c["first_name"], c["last_name"] = clean_name(c.get("first_name")), clean_name(c.get("last_name"))
    c["company"] = clean_text(c.get("company"))
    emails, phones = [], []
    for raw in c.get("emails") or []:
        good = clean_email(raw)
        if not good:
            issues.append(issue("invalid_email", f"not a usable email address: {raw}"))
        elif good not in emails:
            emails.append(good)
    for raw in c.get("phones") or []:
        good = clean_phone(raw, cc)
        if not good:
            issues.append(issue("invalid_phone", f"not a usable phone number: {raw}"))
        elif good not in phones:
            phones.append(good)
    c["emails"], c["phones"] = emails, phones
    for a in c.get("addresses") or []:
        for field in a:
            a[field] = clean_text(a[field])
        if a.get("state") and len(a["state"]) == 2:
            a["state"] = a["state"].upper()
        m = re.match(r"^([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)$", a.get("zip") or "")
        if m:  # Canadian postal code
            a["zip"] = f"{m.group(1)} {m.group(2)}".upper()
    if not (c["first_name"] or c["last_name"] or c["company"]):
        issues.append(issue("no_name", "no name or company on the record"))
    if not emails and not phones:
        issues.append(issue("no_contact", "no usable email address or phone number"))
    for e in emails:
        if e in dup["emails"]:
            issues.append(issue("duplicate_email", f"{e} is on more than one customer"))
    c["issues"] = issues
    return c


def silver_sale(sale, cfg):
    issues, c = [], sale.get("customer")
    if not c or not c.get("key"):
        issues.append(issue("no_customer", "no customer on the sale"))
    else:
        for field in ("first_name", "last_name"):
            if field in c:
                c[field] = clean_name(c[field])
        if "email" in c:
            c["email"] = clean_email(c["email"])
        if "phone" in c:
            c["phone"] = clean_phone(c["phone"], str(cfg.get("phone_country_code") or "1"))
        if not (c.get("first_name") or c.get("last_name")):
            issues.append(issue("customer_not_in_feed", "the sale names a customer Edge has not sent"))
    if not sale.get("date"):
        issues.append(issue("no_date", "no sale date"))
    if sale.get("status") == "completed" and not sale.get("total"):
        issues.append(issue("zero_total", "the sale adds up to zero"))
    for i in sale.get("items") or []:
        i["name"] = clean_text(i.get("name"))
        i["serial_number"] = (clean_text(i.get("serial_number")) or "").upper() or None
        if not i.get("sku"):
            issues.append(issue("item_without_sku", f"piece {i.get('item_key')} has no reference"))
    sale["issues"] = issues
    return sale


# Gold rules: each business's own corrections on top of silver. A rule reads
# "for inventory, when vendor is Omgea, change vendor to Omega" or "...leave it out".
GOLD_OPS = ("is", "is_not", "contains", "starts_with", "ends_with", "is_empty", "is_not_empty")
GOLD_FIELDS = {
    "inventory": {"match": ["sku", "name", "vendor", "category", "style", "vendor_style_code",
                            "location", "store_id", "serial_number", "state"],
                  "set": ["name", "vendor", "category", "style", "location", "store_id"]},
    "customers": {"match": ["first_name", "last_name", "company", "email", "store_id"],
                  "set": ["first_name", "last_name", "company"]},
    "sales": {"match": ["type", "status", "store_id"], "set": []},
}


def rules_clean(value):
    """Check a list of gold rules. Returns (rules, error)."""
    if not isinstance(value, list) or len(value) > 200:
        return None, "Up to 200 rules."
    out = []
    for n, r in enumerate(value, 1):
        bad = None, f"Rule {n} is not complete."
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ("on", "field", "op", "action")) \
                or r["on"] not in GOLD_FIELDS:
            return bad
        fields = GOLD_FIELDS[r["on"]]
        if r.get("field") not in fields["match"] or r.get("op") not in GOLD_OPS \
                or r.get("action") not in ("set", "exclude"):
            return bad
        rule = {"on": r["on"], "field": r["field"], "op": r["op"], "action": r["action"]}
        if r["op"] not in ("is_empty", "is_not_empty"):
            rule["value"] = clean_text(r.get("value")) or ""
            if not rule["value"] or len(rule["value"]) > 120:
                return None, f"Rule {n} needs something to look for (120 characters at most)."
        if r["action"] == "set":
            rule["target"] = r.get("target") or r["field"]
            rule["to"] = clean_text(r.get("to")) or ""
            if rule["target"] not in fields["set"]:
                return None, f"Rule {n}: that field cannot be changed, only matched."
            if not rule["to"] or len(rule["to"]) > 120:
                return None, f"Rule {n} needs something to change it to (120 characters at most)."
        out.append(rule)
    return out, None


def gold_rules(cfg):
    rules, _ = rules_clean(cfg.get("gold_rules") or [])
    return rules or []


def rule_hits(rule, rec):
    if rule["field"] == "email":
        values = rec.get("emails") or [rec.get("email")]
    else:
        values = [rec.get(rule["field"])]
    values = [str(v).strip().lower() for v in values if v not in (None, "")]
    want, op = str(rule.get("value") or "").strip().lower(), rule["op"]
    if op == "is_empty":
        return not values
    if op == "is_not_empty":
        return bool(values)
    if op == "is_not":
        return all(v != want for v in values)
    test = {"is": lambda v: v == want, "contains": lambda v: want in v,
            "starts_with": lambda v: v.startswith(want), "ends_with": lambda v: v.endswith(want)}[op]
    return any(test(v) for v in values)


def apply_gold(kind, rec, rules, only_set=False):
    """Run the rules for one kind of record, in order. None means a rule left the record out."""
    applied = []
    for n, rule in enumerate(rules, 1):
        if rule["on"] != kind or rule["field"] not in rec and rule["field"] != "email":
            continue
        if only_set and (rule["action"] != "set" or rule["target"] not in rec):
            continue
        if not rule_hits(rule, rec):
            continue
        if rule["action"] == "exclude":
            return None
        rec[rule["target"]] = rule["to"]
        applied.append(n)
    rec["gold_rules"] = applied  # which rules changed this record, by position
    return rec


def raw_items(db, keys):
    if not keys:
        return {}
    rows = db.execute(f"SELECT item_key, data FROM items WHERE item_key IN ({marks(keys)})", list(keys))
    return {r["item_key"]: json.loads(r["data"]) for r in rows}


def pieces_out(db, cfg, scopes, layer, rows):
    if layer == "bronze":
        raw = raw_items(db, [r["item_key"] for r in rows])
        return [{"item_key": r["item_key"], "updated_at": r["updated_at"], "raw": raw.get(r["item_key"])}
                for r in rows]
    dup = duplicates(db, cfg)
    out = [silver_piece(piece_out(r, cfg, scopes), dup) for r in rows]
    if layer == "gold":
        rules = gold_rules(cfg)
        out = [p for p in (apply_gold("inventory", p, rules) for p in out) if p]
    return out


def customer_out(db, cfg, layer, key, data, updated_at):
    if layer == "bronze":
        return {"customer_key": key, "updated_at": updated_at, "raw": data}
    c = silver_customer(customer_event(key, data), cfg, duplicates(db, cfg))
    c["updated_at"] = updated_at
    return apply_gold("customers", c, gold_rules(cfg)) if layer == "gold" else c


_issues = {"stamp": None, "value": None}


def all_issues(db, cfg):
    """Every problem the silver layer found, across the whole feed."""
    with _dupes_lock:
        fresh = _issues["stamp"] == db_stamp(cfg)
    if fresh:
        return _issues["value"]
    dup, found = duplicates(db, cfg), []
    for r in db.execute(f"SELECT {PIECE_COLS} FROM pieces WHERE state <> 'removed'"):
        for i in silver_piece(dict(r), dup)["issues"]:
            found.append(dict(i, type="inventory", key=r["item_key"]))
    for r in db.execute("SELECT sale_key, payload FROM sales"):
        for i in silver_sale(json.loads(r["payload"]), cfg)["issues"]:
            found.append(dict(i, type="sales", key=r["sale_key"]))
    for r in db.execute("SELECT customer_key, data FROM customers"):
        for i in silver_customer(customer_event(r["customer_key"], json.loads(r["data"])), cfg, dup)["issues"]:
            found.append(dict(i, type="customers", key=r["customer_key"]))
    with _dupes_lock:
        _issues.update(stamp=db_stamp(cfg), value=found)
    return found


def piece_out(row, cfg, scopes):
    p = dict(row)
    if "sales" in scopes:
        p["sale_date"] = edge_date(p.get("sale_date"), cfg["timezone"])
    else:  # an inventory-only key sees that a piece is sold, not for how much or to which sale
        for field in ("sale_key", "sale_date", "sale_price"):
            p.pop(field, None)
    return p


def sku_rows(db, skus, store=None, rules=None):
    """One summary per SKU: how many are available, where, and their serial numbers."""
    if not skus:
        return []
    sql = f"SELECT {PIECE_COLS} FROM pieces WHERE sku IN ({marks(skus)})"
    args = list(skus)
    if store:
        sql, args = sql + " AND store_id = ?", args + [store]
    out = {s: {"sku": s, "name": None, "vendor": None, "category": None, "quantity": 0,
               "by_store": {}, "serial_numbers": [], "sold": 0, "layaway": 0,
               "price_from": None, "price_to": None, "updated_at": None} for s in skus}
    kept = set()
    for r in db.execute(sql + " ORDER BY sku, item_key", args):
        if rules is not None:
            sku, r = r["sku"], apply_gold("inventory", dict(r), rules)
            if r is None:
                continue  # a gold rule leaves this piece out
            r["sku"] = sku
        kept.add(r["sku"])
        o = out[r["sku"]]
        for field in ("name", "vendor", "category"):
            if not o[field] and r[field]:
                o[field] = r[field]
        if not o["updated_at"] or r["updated_at"] > o["updated_at"]:
            o["updated_at"] = r["updated_at"]
        if r["state"] == "available":
            o["quantity"] += 1
            o["by_store"][str(r["store_id"])] = o["by_store"].get(str(r["store_id"]), 0) + 1
            if clean_text(r["serial_number"]):
                o["serial_numbers"].append(clean_text(r["serial_number"]).upper())
            price = to_number(r["current_price"])
            price = price if price is not None else to_number(r["retail_price"])
            if price is not None:
                o["price_from"] = price if o["price_from"] is None else min(o["price_from"], price)
                o["price_to"] = price if o["price_to"] is None else max(o["price_to"], price)
        elif r["state"] in ("sold", "layaway"):
            o[r["state"]] += 1
    return [out[s] for s in skus if rules is None or s in kept]


def sale_out(row, scopes, cfg=None, layer="silver", db=None):
    sale = json.loads(row["payload"])
    sale["updated_at"] = row["updated_at"]
    if layer == "bronze":  # the sale as built, plus every Edge record it was built from
        raw = raw_items(db, [i["item_key"] for i in sale.get("items") or []])
        sale["raw_items"] = [raw.get(i["item_key"]) for i in sale.get("items") or []]
    elif cfg is not None:
        silver_sale(sale, cfg)
        if layer == "gold":
            rules = gold_rules(cfg)
            if apply_gold("sales", sale, rules) is None:
                return None
            for i in sale.get("items") or []:  # the same corrections the inventory gets
                apply_gold("inventory", i, rules, only_set=True)
                i.pop("gold_rules", None)
    if "customers" not in scopes and sale.get("customer"):
        sale["customer"] = {"key": sale["customer"].get("key")}  # no name, email or phone
    return sale


def api_query(db, cfg, scopes, parts, query, max_limit, default_layer="silver"):
    """Answer one request. Returns (status, body). Raises BadRequest on bad input."""
    asked = dict(parse_qsl(query, keep_blank_values=True, max_num_fields=50)) if query else {}
    layer = asked.get("layer") or (default_layer if default_layer in LAYERS else "silver")
    if layer not in LAYERS:
        raise BadRequest("layer must be one of: " + ", ".join(LAYERS))
    if layer == "bronze" and "bronze" not in scopes:
        # Raw records carry everything Edge holds (cost, sale and customer fields on a piece),
        # so they need their own permission on top of inventory, sales or customers.
        return 403, {"error": "this key does not have the bronze scope"}
    status, body = layer_query(db, cfg, scopes, parts, query, max_limit, layer)
    if status == 200 and parts[0] not in ("ping", "index"):
        body["layer"] = layer
    return status, body


def layer_query(db, cfg, scopes, parts, query, max_limit, layer):
    resource, ident = parts[0], "/".join(parts[1:])
    lists = {"limit", "cursor", "updated_since", "layer"}

    def need(scope):
        return scope in scopes

    if resource == "index" and not ident:
        api_params(query, {"layer"})
        paging = "limit, cursor (pass back next_cursor), updated_since (ISO time)"
        return 200, {
            "name": "Edge Bridge data API", "version": VERSION, "read_only": True,
            "auth": "Authorization: Bearer <key>, sent through Cloudflare Access",
            "your_scopes": sorted(scopes),
            "layers": {
                "default": layer,
                "bronze": "exactly what Edge sent, every field; needs the bronze scope",
                "silver": "cleaned and checked, with an issues list on every record",
                "gold": f"silver plus this business's own rules ({len(gold_rules(cfg))} set)",
                "choose": "add layer=bronze|silver|gold to any request",
            },
            "endpoints": [
                {"path": "/v1/inventory", "scope": "inventory",
                 "returns": "one row per SKU: quantity, serial_numbers, by_store, price range",
                 "params": "state=available|all, sku, store, q, " + paging},
                {"path": "/v1/inventory?group=piece", "scope": "inventory",
                 "returns": "one row per physical piece",
                 "params": "state=available|sold|layaway|removed|excluded|all, sku, serial, item_key, "
                           "store, q, " + paging},
                {"path": "/v1/inventory/{sku}", "scope": "inventory", "returns": "one SKU with its pieces"},
                {"path": "/v1/sales", "scope": "sales", "returns": "sales and layaways with their pieces",
                 "params": "since, until (YYYY-MM-DD), status=completed|cancelled, type=sale|layaway, "
                           "customer_key, sku, " + paging},
                {"path": "/v1/sales/{sale_key}", "scope": "sales", "returns": "one sale"},
                {"path": "/v1/customers", "scope": "customers", "returns": "customers",
                 "params": "q, email, " + paging},
                {"path": "/v1/customers/{customer_key}", "scope": "customers",
                 "returns": "one customer, with their sales if the key has the sales scope"},
                {"path": "/v1/issues", "scope": "any", "returns": "problems found in the feed, with counts",
                 "params": "type=inventory|sales|customers, code, limit"},
                {"path": "/v1/ping", "scope": "any", "returns": "key check and time of the last Edge file"},
            ],
            "sync": "to stay in step, call a list with state=all&updated_since=<time of your last call>",
            "errors": "400 bad parameter, 401 bad key, 403 scope or access, 404 not found, 429 slow down "
                      "(see Retry-After)",
        }

    if resource == "ping" and not ident:
        api_params(query, {"layer"})
        last = db.execute("SELECT processed_at FROM files ORDER BY processed_at DESC LIMIT 1").fetchone()
        return 200, {"ok": True, "scopes": sorted(scopes), "last_file": last[0] if last else None}

    if resource == "issues" and not ident:
        q = api_params(query, {"layer", "type", "code", "limit"})
        kinds = [k for k in ("inventory", "sales", "customers") if need(k)]
        if "type" in q:
            kinds = [p_choice(q, "type", tuple(kinds) or ("none",), None)]
        found = [i for i in all_issues(db, cfg) if i["type"] in kinds
                 and (not q.get("code") or i["code"] == q["code"])]
        counts = {}
        for i in found:
            counts[i["code"]] = counts.get(i["code"], 0) + 1
        return 200, {"total": len(found), "counts": counts, "data": found[:p_limit(q, max_limit)]}

    if resource == "inventory":
        if not need("inventory"):
            return 403, {"error": "this key does not have the inventory scope"}
        if ident:
            api_params(query, {"layer"})
            pieces = db.execute(f"SELECT {PIECE_COLS} FROM pieces WHERE sku = ? ORDER BY item_key "
                                "LIMIT 5000", (ident,)).fetchall()
            if not pieces:
                return 404, {"error": "not found"}
            found = sku_rows(db, [ident], rules=gold_rules(cfg) if layer == "gold" else None)
            if not found:
                return 404, {"error": "not found"}
            body = found[0]
            body["pieces"] = pieces_out(db, cfg, scopes, layer, pieces)
            return 200, {"data": body}
        group = dict(parse_qsl(query)).get("group")
        group = group or ("piece" if layer == "bronze" else "sku")
        if layer == "bronze" and group != "piece":
            raise BadRequest("bronze is what Edge sent, one record per piece: use group=piece")
        if group == "piece":
            q = api_params(query, lists | {"group", "state", "sku", "serial", "item_key", "store", "q"})
            state = p_choice(q, "state", ("available", "sold", "layaway", "removed", "excluded", "all"),
                             "available")
            where, args = ["1=1"], []
            if state != "all":
                where.append("state = ?"); args.append(state)
            for name, col in (("sku", "sku"), ("serial", "serial_number"), ("item_key", "item_key"),
                              ("store", "store_id")):
                if q.get(name):
                    where.append(f"{col} = ?"); args.append(q[name])
            if q.get("q"):
                where.append("(sku LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR vendor LIKE ? "
                             "ESCAPE '\\' OR serial_number LIKE ? ESCAPE '\\')")
                args += [like(q["q"])] * 4
            if p_time(q, "updated_since"):
                where.append("updated_at >= ?"); args.append(p_time(q, "updated_since"))
            if q.get("cursor"):
                where.append("item_key > ?"); args.append(q["cursor"])
            limit = p_limit(q, max_limit)
            rows = db.execute(f"SELECT {PIECE_COLS} FROM pieces WHERE {' AND '.join(where)} "
                              "ORDER BY item_key LIMIT ?", args + [limit + 1]).fetchall()
            rows, nxt = page_of(rows, limit, lambda r: r["item_key"])
            return 200, {"data": pieces_out(db, cfg, scopes, layer, rows), "next_cursor": nxt}
        q = api_params(query, lists | {"group", "state", "sku", "store", "q"})
        p_choice(q, "group", ("sku", "piece"), "sku")
        state = p_choice(q, "state", ("available", "all"), "available")
        where, args = ["sku <> ''"], []
        if state == "available":
            where.append("state = 'available'")
        if q.get("sku"):
            where.append("sku = ?"); args.append(q["sku"])
        if q.get("store"):
            where.append("store_id = ?"); args.append(q["store"])
        if q.get("q"):
            where.append("(sku LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR vendor LIKE ? ESCAPE '\\')")
            args += [like(q["q"])] * 3
        if p_time(q, "updated_since"):
            where.append("sku IN (SELECT sku FROM items WHERE updated_at >= ?)")
            args.append(p_time(q, "updated_since"))
        if q.get("cursor"):
            where.append("sku > ?"); args.append(q["cursor"])
        limit = p_limit(q, max_limit)
        skus = [r[0] for r in db.execute(
            f"SELECT DISTINCT sku FROM pieces WHERE {' AND '.join(where)} ORDER BY sku LIMIT ?",
            args + [limit + 1])]
        skus, nxt = page_of(skus, limit, lambda s: s)
        rules = gold_rules(cfg) if layer == "gold" else None
        return 200, {"data": sku_rows(db, skus, q.get("store"), rules), "next_cursor": nxt}

    if resource == "sales":
        if not need("sales"):
            return 403, {"error": "this key does not have the sales scope"}
        if ident:
            api_params(query, {"layer"})
            row = db.execute("SELECT payload, updated_at FROM sales WHERE sale_key = ?", (ident,)).fetchone()
            sale = sale_out(row, scopes, cfg, layer, db) if row else None
            return (200, {"data": sale}) if sale else (404, {"error": "not found"})
        q = api_params(query, lists | {"since", "until", "status", "type", "customer_key", "sku"})
        where, args = ["1=1"], []
        day = "substr(json_extract(payload, '$.date'), 1, 10)"
        if p_day(q, "since"):
            where.append(f"{day} >= ?"); args.append(q["since"])
        if p_day(q, "until"):
            where.append(f"{day} <= ?"); args.append(q["until"])
        if "status" in q:
            where.append("json_extract(payload, '$.status') = ?")
            args.append(p_choice(q, "status", ("completed", "cancelled"), None))
        if "type" in q:
            where.append("json_extract(payload, '$.type') = ?")
            args.append(p_choice(q, "type", ("sale", "layaway"), None))
        if q.get("customer_key"):
            where.append("json_extract(payload, '$.customer.key') = ?"); args.append(q["customer_key"])
        if q.get("sku"):
            where.append("sale_key IN (SELECT sale_key FROM items WHERE sku = ?)"); args.append(q["sku"])
        if p_time(q, "updated_since"):
            where.append("updated_at >= ?"); args.append(p_time(q, "updated_since"))
        if q.get("cursor"):
            where.append("sale_key > ?"); args.append(q["cursor"])
        limit = p_limit(q, max_limit)
        rows = db.execute(f"SELECT sale_key, payload, updated_at FROM sales WHERE {' AND '.join(where)} "
                          "ORDER BY sale_key LIMIT ?", args + [limit + 1]).fetchall()
        rows, nxt = page_of(rows, limit, lambda r: r["sale_key"])
        data = [s for s in (sale_out(r, scopes, cfg, layer, db) for r in rows) if s]
        return 200, {"data": data, "next_cursor": nxt}

    if resource == "customers":
        if not need("customers"):
            return 403, {"error": "this key does not have the customers scope"}
        if ident:
            api_params(query, {"layer"})
            row = db.execute("SELECT data, updated_at FROM customers WHERE customer_key = ?",
                             (ident,)).fetchone()
            if not row:
                return 404, {"error": "not found"}
            body = customer_out(db, cfg, layer, ident, json.loads(row["data"]), row["updated_at"])
            if body is None:
                return 404, {"error": "not found"}
            if need("sales"):
                sales = db.execute(
                    "SELECT payload, updated_at FROM sales WHERE json_extract(payload, '$.customer.key') "
                    "= ? ORDER BY sale_key LIMIT 1000", (ident,)).fetchall()
                body["sales"] = [x for x in (sale_out(s, scopes, cfg, layer, db) for s in sales) if x]
            return 200, {"data": body}
        q = api_params(query, lists | {"q", "email"})
        where, args = ["1=1"], []
        if q.get("q"):  # only the fields the API returns are searchable
            fields = ["customer_key", "json_extract(data, '$.CustomerFirstName')",
                      "json_extract(data, '$.CustomerLastName')",
                      "json_extract(data, '$.CustomerCompany')",
                      "json_extract(data, '$.Emails[0].PairValue.EmailEmail')",
                      "json_extract(data, '$.Phones[0].PairValue.PhonePhone')"]
            where.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")")
            args += [like(q["q"])] * len(fields)
        if q.get("email"):
            where.append("EXISTS (SELECT 1 FROM json_each(customers.data, '$.Emails') e WHERE lower("
                         "COALESCE(json_extract(e.value, '$.PairValue.EmailEmail'), "
                         "json_extract(e.value, '$.EmailEmail'))) = ?)")
            args.append(q["email"].strip().lower())
        if p_time(q, "updated_since"):
            where.append("updated_at >= ?"); args.append(p_time(q, "updated_since"))
        if q.get("cursor"):
            where.append("customer_key > ?"); args.append(q["cursor"])
        limit = p_limit(q, max_limit)
        rows = db.execute(f"SELECT customer_key, data, updated_at FROM customers WHERE "
                          f"{' AND '.join(where)} ORDER BY customer_key LIMIT ?",
                          args + [limit + 1]).fetchall()
        rows, nxt = page_of(rows, limit, lambda r: r["customer_key"])
        data = [customer_out(db, cfg, layer, r["customer_key"], json.loads(r["data"]), r["updated_at"])
                for r in rows]
        return 200, {"data": [c for c in data if c], "next_cursor": nxt}

    return 404, {"error": "not found"}


def cmd_api(cfg, local=False, config_path=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from logging.handlers import RotatingFileHandler

    os.umask(0o077)  # the audit log and its rotations are for this user only
    api = dict(EXAMPLE_CONFIG["api"])
    api.update(cfg.get("api") or {})
    port = int(api["port"])
    live = {"cfg": cfg, "api": api, "mtime": os.path.getmtime(config_path) if config_path else None}

    def settings():
        """The current config. Picks up what the settings page saved, without a restart."""
        if config_path:
            try:
                mtime = os.path.getmtime(config_path)
                if mtime != live["mtime"]:
                    fresh = load_config(config_path)
                    merged = dict(EXAMPLE_CONFIG["api"])
                    merged.update(fresh.get("api") or {})
                    live.update(cfg=fresh, api=merged, mtime=mtime)
            except (OSError, ValueError, SystemExit) as e:
                log.error("could not reload the config, keeping the last good one: %s", e)
        return live["cfg"], live["api"]
    verifier = None if local else make_verifier(api.get("cloudflare_access"), "api")
    if not local and not verifier:
        sys.exit("The API only answers requests that came through Cloudflare Access.\n"
                 "Set api.cloudflare_access.team_domain (yourteam.cloudflareaccess.com) and\n"
                 "api.cloudflare_access.aud (the 64-character Application Audience tag).\n"
                 "To test on this machine only: edt_bridge.py api --local")

    open_db(cfg).close()  # make sure the database, views and key table exist
    db_uri = "file:" + quote(os.path.join(cfg["data_dir"], "edt.db")) + "?mode=ro"
    if not sqlite3.connect(db_uri, uri=True).execute(
            "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL").fetchone()[0]:
        log.warning("No API keys yet, so every request will be refused. Make one with: apikey create")

    audit_path = os.path.join(cfg["data_dir"], "api_audit.log")
    audit = logging.getLogger("edt-bridge.audit")
    audit.propagate = False
    audit.setLevel(logging.INFO)
    handler = RotatingFileHandler(audit_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(handler)
    try:
        os.chmod(audit_path, 0o600)
    except OSError:
        pass

    rate = Window(int(api["requests_per_minute"]), 60)   # per key
    bad = Window(10, 300)                                # bad keys per caller, then locked out
    slots = threading.BoundedSemaphore(16)               # requests in flight

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "edt-bridge", ""
        timeout = 15  # a caller that stalls is dropped

        def reply(self, status, body, retry=None):
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex")
            if retry:
                self.send_header("Retry-After", str(retry))
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(data)

        def answer(self, note):
            """Every check, in order. Returns (status, body, retry_after)."""
            if len(self.path) > 2000:
                return 414, {"error": "address too long"}, None
            url = urlparse(self.path)
            note["path"] = url.path[:200]
            # 1. Did it come through Cloudflare Access?
            if verifier:
                claims = verifier.verify(self.headers.get("Cf-Access-Jwt-Assertion", ""))
                if not claims:
                    return 403, {"error": "forbidden"}, None
                note["who"] = str(claims.get("common_name") or claims.get("email") or "")[:100]
                ip = self.headers.get("Cf-Connecting-Ip", "")
                caller = ip if re.match(r"^[0-9a-fA-F:.]{2,45}$", ip) else "unknown"
            else:
                # Local testing: nothing that arrived through Cloudflare or a proxy gets in.
                if came_through_proxy(self.headers) or self.client_address[0] not in LOOPBACK \
                        or not local_host(self.headers, port):
                    return 403, {"error": "forbidden"}, None
                caller = "local"
            note["ip"] = caller
            # 2. Is the key real, current and not locked out?
            wait = bad.wait(caller)
            if wait:
                return 429, {"error": "too many bad keys, try later"}, wait
            scheme, _, given = self.headers.get("Authorization", "").partition(" ")
            m = API_KEY_RE.match(given.strip()) if scheme.lower() == "bearer" else None
            db = sqlite3.connect(db_uri, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA query_only = ON")
                row = db.execute("SELECT * FROM api_keys WHERE id = ?", (m.group(1),)).fetchone() if m else None
                good = hmac.compare_digest(row["hash"] if row else "0" * 64, key_hash(given.strip()))
                if not (row and good) or row["revoked_at"] or (
                        row["expires_at"] and row["expires_at"] <= now_iso()):
                    bad.hit(caller)
                    return 401, {"error": "a valid API key is needed"}, None
                note["key"] = f"{row['id']} {row['name']}"
                # 3. Is the key within its rate limit?
                now_cfg, now_api = settings()
                rate.limit = max(1, int(now_api["requests_per_minute"]))
                max_limit = max(1, min(int(now_api["max_limit"]), 900))
                wait = rate.wait(row["id"])
                if wait:
                    return 429, {"error": "rate limit reached"}, wait
                rate.hit(row["id"])
                # 4. Route, check the key's scope, answer.
                parts = [unquote(p) for p in url.path.split("/") if p]
                if not parts or parts[0] != "v1" or any(len(p) > 120 or "\x00" in p for p in parts):
                    return 404, {"error": "not found"}, None
                note["params"] = sorted({k for k, _ in parse_qsl(url.query, keep_blank_values=True,
                                                                 max_num_fields=50)})[:20]
                scopes = set(row["scopes"].split(",")) & set(API_SCOPES)
                status, body = api_query(db, now_cfg, scopes, parts[1:] or ["index"], url.query, max_limit,
                                         str(now_api.get("default_layer") or "silver"))
                if isinstance(body.get("data"), list):
                    note["rows"] = len(body["data"])
                return status, body, None
            finally:
                db.close()

        def do_GET(self):
            note = {"at": now_iso(), "ip": self.client_address[0], "who": "", "key": "", "path": "",
                    "params": [], "rows": None}
            if not slots.acquire(blocking=False):
                return self.reply(503, {"error": "busy, try again"}, 2)
            try:
                try:
                    status, body, retry = self.answer(note)
                except BadRequest as e:
                    status, body, retry = 400, {"error": str(e)}, None
                except Exception as e:  # never show the caller what went wrong inside
                    log.error("api request failed: %s: %s", type(e).__name__, e)
                    status, body, retry = 500, {"error": "the request could not be answered"}, None
                note["status"] = status
                audit.info(json.dumps(note))
                self.reply(status, body, retry)
            finally:
                slots.release()

        def refuse(self):
            self.reply(405, {"error": "this API is read-only: GET only"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = refuse

        def log_message(self, fmt, *args):  # the audit log is the record
            pass

    log.info("API on http://127.0.0.1:%d %s", port,
             "LOCAL TEST MODE: no Cloudflare Access check, this machine only" if local
             else f"behind Cloudflare Access ({verifier.issuer})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def cmd_fields(cfg):
    """Show how each candidate Edge field would work as the SKU, from the real data."""
    db = open_db(cfg)
    candidates = list(dict.fromkeys(cfg["sku_fields"] + [
        "ItemMetaBaseSKU", "ItemVendStyleCode", "ItemStyle", "ItemVendBarCode", "ItemSerialNumber"]))
    rows = [json.loads(r["data"]) for r in db.execute("SELECT data FROM items WHERE state <> 'removed'")]
    if not rows:
        sys.exit("No items yet. Process a full ItemList first.")
    print(f"{len(rows)} live pieces\n")
    print(f"{'field':<22}{'filled':>8}{'values':>9}{'pieces per value':>18}{'largest group':>15}")
    for field in candidates:
        groups = {}
        for d in rows:
            v = str(d.get(field) or "").strip()
            if v:
                groups[v] = groups.get(v, 0) + 1
        filled = sum(groups.values())
        if not groups:
            print(f"{field:<22}{'0%':>8}")
            continue
        print(f"{field:<22}{filled * 100 // len(rows):>7}%{len(groups):>9}"
              f"{filled / len(groups):>18.1f}{max(groups.values()):>15}")
    using = db.execute("SELECT COALESCE(sku_source, 'none') AS f, COUNT(*) AS n FROM items "
                       "WHERE state <> 'removed' GROUP BY 1 ORDER BY n DESC").fetchall()
    print("\nWith the current sku_fields order, pieces get their SKU from:")
    for r in using:
        print(f"  {r['f']:<22}{r['n']}")
    print("\nA good SKU field is filled on nearly every piece and groups identical pieces "
          "(more than 1 piece per value).\nA field with exactly 1 piece per value is a serial "
          "number, not a SKU.")


def cmd_show(cfg, query):
    """Look something up by SKU, item key or serial number."""
    db = open_db(cfg)
    piece = db.execute("SELECT * FROM pieces WHERE item_key=? OR serial_number=? LIMIT 1",
                       (query, query)).fetchone()
    if piece:
        print(f"PIECE {piece['item_key']}")
        for k in piece.keys()[1:]:
            if piece[k] not in (None, ""):
                print(f"  {k:<18}{piece[k]}")
        print("\n  History:")
        for c in db.execute("SELECT at, field, old, new FROM changes WHERE item_key=? ORDER BY id",
                            (piece["item_key"],)):
            print(f"    {c['at']}  {c['field']}: {c['old']} -> {c['new']}")
        if piece["sku"]:
            n = db.execute("SELECT pieces, available FROM skus WHERE sku=?", (piece["sku"],)).fetchone()
            print(f"\n  SKU {piece['sku']}: {n['pieces']} pieces, {n['available']} available")
        return
    sku = db.execute("SELECT * FROM skus WHERE sku=?", (query,)).fetchone()
    if not sku:
        sys.exit(f"Nothing found for {query!r} as a SKU, item key or serial number.")
    print(f"SKU {sku['sku']}  {sku['name'] or ''}  ({sku['vendor'] or 'no vendor'})")
    print(f"  {sku['pieces']} pieces: {sku['available']} available, {sku['sold']} sold, "
          f"{sku['layaway']} layaway\n")
    print(f"  {'item key':<18}{'serial':<18}{'state':<11}{'store':<7}{'price':>10}  sale")
    for r in db.execute("SELECT * FROM pieces WHERE sku=? ORDER BY state, item_key", (query,)):
        price = r["sale_price"] if r["state"] in ("sold", "layaway") else r["current_price"]
        print(f"  {r['item_key']:<18}{(r['serial_number'] or ''):<18}{r['state']:<11}"
              f"{str(r['store_id']):<7}{str(price if price is not None else ''):>10}  {r['sale_key'] or ''}")


def main():
    p = argparse.ArgumentParser(description="Pass Edge Data Transfer files on to your own systems.")
    p.add_argument("command", choices=["init", "run", "watch", "status", "retry", "fields", "show", "serve",
                                      "api", "apikey"])
    p.add_argument("query", nargs="?", help="for show: a SKU, item key or serial number. "
                                            "for apikey: create, list or revoke")
    p.add_argument("arg", nargs="?", help="for apikey revoke: the key id")
    p.add_argument("--local", action="store_true",
                   help="for api: test on this machine without Cloudflare Access")
    p.add_argument("--name", help="for apikey create: who the key is for")
    p.add_argument("--scopes", help="for apikey create: inventory,sales,customers")
    p.add_argument("--expires-days", type=int, help="for apikey create: stop working after this many days")
    p.add_argument("--config", default=os.environ.get("EDT_BRIDGE_CONFIG") or os.path.join(HERE, "config.json"),
                   help="your config file. Default: $EDT_BRIDGE_CONFIG, or config.json next to this file")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent, change nothing")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "init":
        if os.path.exists(args.config):
            sys.exit(f"{args.config} already exists")
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(EXAMPLE_CONFIG, f, indent=2)
        try:
            os.chmod(args.config, 0o600)  # it can hold tokens
        except OSError:
            pass
        print(f"Wrote {args.config}. Edit it, then run: python3 {os.path.basename(__file__)} run --dry-run")
        return

    cfg = load_config(args.config)
    if args.command == "status":
        cmd_status(cfg)
    elif args.command == "serve":
        cmd_serve(cfg, args.config)
    elif args.command == "api":
        cmd_api(cfg, args.local, args.config)
    elif args.command == "apikey":
        cmd_apikey(cfg, args.query, args.arg, args)
    elif args.command == "fields":
        cmd_fields(cfg)
    elif args.command == "show":
        if not args.query:
            sys.exit("usage: edt_bridge.py show <SKU, item key or serial number>")
        cmd_show(cfg, args.query)
    elif args.command == "retry":
        db = open_db(cfg)
        n = db.execute("UPDATE outbox SET state='pending', attempts=0, next_try=0 "
                       "WHERE state='failed'").rowcount
        db.commit()
        print(f"{n} deliveries requeued")
    elif args.command == "run":
        lock = None if args.dry_run else single_instance(cfg)
        sys.exit(0 if run_once(cfg, args.dry_run) else 1)
    elif args.command == "watch":
        lock = single_instance(cfg)
        while True:
            try:
                cfg = load_config(args.config)  # picks up what the settings page saved
                run_once(cfg)
            except Exception:  # keep the loop alive; the next pass retries
                log.exception("pass failed")
            time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
