"""Self-contained tests for edt_bridge.py. Standard library only.

Run from the repo root:   python3 -m unittest discover -s tests -v

Everything is built in a temporary directory: a config, synthetic Edge files
(fake data only), a database, API keys, and servers on free local ports.
"""

import argparse
import base64
import contextlib
import copy
import http.server
import warnings
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("edt_bridge", os.path.join(HERE, "..", "edt_bridge.py"))
eb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eb)

HAVE_OPENSSL = shutil.which("openssl") is not None
TEAM = "demo.cloudflareaccess.com"
AUD = "a" * 64


# --------------------------------------------------------------------------
# Synthetic Edge data (fake names, example.com emails, 416-555-01xx phones)
# --------------------------------------------------------------------------

def item(key, sku_field=None, sku=None, status="I", store=1, vendor="Omega", serial=None,
         retail=1000, current=900, desc=None, sale=None, **extra):
    d = {"ItemKey": key, "ItemStatus": status, "ItemStoreId": store,
         "ItemDesc": desc or f"Fake Watch {key[-4:]}", "CategoryName": "Watches",
         "ItemRetailPrice": retail, "ItemCurrentPrice": current, "ItemLocation": "Case 1",
         "ItemDateEntered": "2026-10-01T09:00:00",
         "Vendors": [{"Key": "V1", "PairValue": {"VendorName": vendor}}]}
    if sku_field:
        d[sku_field] = sku
    if serial:
        d["ItemSerialNumber"] = serial
    if sale:
        d.update({"ItemSaleKey": sale["key"], "ItemSaleDate": sale["date"],
                  "ItemSalePrice": sale["price"], "ItemSaleTax": sale["tax"]})
        if sale.get("cust"):
            d["ItemSaleCustKey"] = sale["cust"]
    d.update(extra)
    return {"Key": key, "PairValue": d}


def base_items():
    S = "ItemVendStyleCode"
    s0 = {"key": "001-S5000", "date": "2026-11-05T10:00:00", "cust": "001-1004"}
    return [
        item("001-I001", S, "OM-100", serial="OM1001", retail=5000, current=4500),
        item("001-I002", S, "OM-100", serial="om1002", retail=3000, current=2500),
        item("001-I003", S, "OM-100", store=2, serial="OM1003", retail=3500, current=3000),
        item("001-I004", "ItemStyle", "OM-200", serial="OM2001", retail=800, current=700),  # SKU from a later field
        item("001-I005", S, "IW-300", vendor="IWC", desc="  Fake   Pilot  ", retail=9000, current=9000),
        item("001-I006", S, "IW-300", vendor="IWC", retail=9000, current=9000),
        item("001-I007", vendor="Brand X", retail=100, current=100),                      # no SKU field at all
        item("001-I008", S, "MX-400", vendor="Marq", serial="dup-777", retail=None, current=None),       # no price
        item("001-I009", S, "MX-400", vendor="Marq", serial="DUP-777", retail=50, current=50),           # same serial
        item("001-I010", S, "OM-100", status="S", retail=1200, current=1200,
             sale=dict(s0, price=1000, tax=130)),
        item("001-I011", S, "OM-200", status="S", retail=600, current=600,
             sale=dict(s0, price=500, tax=65)),
        item("001-I012", S, "IW-300", vendor="IWC", status="S",
             sale={"key": "001-S5001", "date": "2026-11-06T11:00:00", "cust": "001-1001",
                   "price": 8000, "tax": 1040}),
        item("001-I013", S, "MX-400", vendor="Marq", status="L",                                          # layaway
             sale={"key": "001-S5002", "date": "2026-11-07T12:00:00", "cust": "001-1002",
                   "price": 300, "tax": 39}),
        item("001-I014", S, "OM-100", status="S",                                          # customer not in feed
             sale={"key": "001-S5003", "date": "2026-11-08T13:00:00", "cust": "001-9999",
                   "price": 700, "tax": 91}),
        item("001-I015", S, "ZZ-500", vendor="Marq", status="S"),                                         # sold, no sale key
        item("001-I016", S, "OM-100", status="X"),                                         # removed
    ]


def cust(key, first=None, last=None, emails=(), phones=(), company=None, **extra):
    d = {"CustomerKey": key, "CustomerFirstName": first, "CustomerLastName": last,
         "CustomerCompany": company, "CustomerStoreId": 1,
         "Emails": [{"Key": "e", "PairValue": {"EmailEmail": e}} for e in emails],
         "Phones": [{"Key": "p", "PairValue": {"PhonePhone": p}} for p in phones]}
    d.update(extra)
    return {"Key": key, "PairValue": d}


def base_customers():
    return [
        cust("001-1000", "NO", "CONTACT", ["not-an-email"], ["555-0100"]),
        cust("001-1001", "JANE", "DOE", ["Jane.Doe@Example.com"], ["416-555-0101"],
             Addresses=[{"Key": "a", "PairValue": {"AddressType": "H", "AddressStreet": " 1  Fake St ",
                                                   "AddressCity": "Toronto", "AddressState": "on",
                                                   "AddressZip": "m5r2e3", "AddressCountry": "CA"}}]),
        cust("001-1002", "john", "smith", ["john.smith@example.com"], ["(416) 555 0102"]),
        cust("001-1004", "  PRIYA ", "MCDONALD-O'BRIEN", ["PRIYA.patel@example.com"], ["416-555-0104"],
             CustomerSSNO="000-00-0000"),
        cust("001-1005", "Jane", "Duplicate", ["jane.doe@example.com"], ["416-555-0105"]),  # same email as 1001
        cust("001-1006", None, None, [], ["416-555-0106"]),                                # no name
    ]


class Env:
    """A temp workspace: config, inbox, data dir. Call ingest() to load the fixture."""

    def __init__(self, root, **overrides):
        self.root = root
        self.inbox = os.path.join(root, "inbox")
        self.data = os.path.join(root, "data")
        os.makedirs(self.inbox, exist_ok=True)
        self.config_path = os.path.join(root, "config.json")
        cfg = {"inbox": self.inbox, "data_dir": self.data, "timezone": "America/Toronto",
               "currency": "CAD", "retention_days": 30, "sales_lookback_days": 0,
               "settle_seconds": 0, "poll_seconds": 60, "destinations": [],
               "ui": {"host": "127.0.0.1", "port": 8787},
               "api": {"port": 8788, "default_layer": "silver", "requests_per_minute": 100000,
                       "max_limit": 500}}
        cfg.update(overrides)
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)
        self.cfg = eb.load_config(self.config_path)

    def write(self, name, doc):
        path = os.path.join(self.inbox, name)
        with open(path, "w") as f:
            json.dump(doc, f)
        return path

    def ingest(self, items=None, customers=None):
        self.write("FullCustomerList.json", customers if customers is not None else base_customers())
        self.write("FullItemList.json", {"Items": items if items is not None else base_items()})
        return eb.run_once(self.cfg)

    def db(self):
        db = sqlite3.connect(os.path.join(self.data, "edt.db"))
        db.row_factory = sqlite3.Row
        return db

    def ro_db(self):
        db = sqlite3.connect("file:" + os.path.join(self.data, "edt.db") + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def make_key(self, name, scopes, expires_days=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            eb.cmd_apikey(self.cfg, "create", None, argparse.Namespace(
                name=name, scopes=scopes, expires_days=expires_days))
        return re.search(r"edt_[a-f0-9]{12}_[A-Za-z0-9_-]{43}", out.getvalue()).group(0)

    def revoke(self, key):
        with contextlib.redirect_stdout(io.StringIO()):
            eb.cmd_apikey(self.cfg, "revoke", key.split("_")[1], argparse.Namespace())


ENV = None
KEYS = {}
_TMP = None


def reset_caches():
    eb._dupes.update(stamp=None, value=None)
    eb._issues.update(stamp=None, value=None)


def setUpModule():
    global ENV, _TMP
    warnings.simplefilter("ignore", ResourceWarning)  # edt_bridge leaves a few short-lived sqlite handles
    _TMP = tempfile.TemporaryDirectory()
    ENV = Env(_TMP.name)
    ENV.ingest()
    reset_caches()
    KEYS["full"] = ENV.make_key("full", "inventory,sales,customers")
    KEYS["invonly"] = ENV.make_key("invonly", "inventory")
    KEYS["invsales"] = ENV.make_key("invsales", "inventory,sales")
    KEYS["custonly"] = ENV.make_key("custonly", "customers")
    KEYS["raw"] = ENV.make_key("raw", "inventory,sales,customers,bronze")
    KEYS["dead"] = ENV.make_key("dead", "inventory")
    ENV.revoke(KEYS["dead"])
    kid = "abcdef012345"
    KEYS["expired"] = f"edt_{kid}_{'A' * 43}"
    db = ENV.db()
    db.execute("INSERT INTO api_keys (id, name, hash, scopes, created_at, expires_at) VALUES (?,?,?,?,?,?)",
               (kid, "expired", eb.key_hash(KEYS["expired"]), "inventory", eb.now_iso(),
                "2020-01-01T00:00:00+00:00"))
    db.commit()
    db.close()


def tearDownModule():
    _TMP.cleanup()


# --------------------------------------------------------------------------
# Helpers: ports, servers, HTTP client, JWTs
# --------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port, thread, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return
        except OSError:
            if not thread.is_alive():
                raise RuntimeError("the server thread exited before listening")
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def start_api(cfg, local=True, config_path=None, api=None):
    cfg = copy.deepcopy(cfg)
    port = free_port()
    cfg["api"] = dict(cfg["api"], port=port, **(api or {}))
    t = threading.Thread(target=eb.cmd_api, args=(cfg, local, config_path), daemon=True)
    t.start()
    wait_listening(port, t)
    return port


def start_ui(cfg, ui, config_path=None):
    cfg = copy.deepcopy(cfg)
    port = free_port()
    cfg["ui"] = dict(ui, port=port)
    t = threading.Thread(target=eb.cmd_serve, args=(cfg, config_path), daemon=True)
    t.start()
    wait_listening(port, t)
    return port


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(port, path, key=None, headers=None, method="GET", data=None):
    h = dict(headers or {})
    if key:
        h["Authorization"] = "Bearer " + key
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method=method)
    try:
        with _opener.open(req, timeout=15) as r:
            raw, status, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode("utf-8", "replace")
    return status, body, {k.lower(): v for k, v in hdrs.items()}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Rsa:
    """An RSA key made with openssl, able to sign JWTs the way Cloudflare Access does."""

    def __init__(self, folder, name):
        self.pem = os.path.join(folder, name + ".pem")
        subprocess.run(["openssl", "genrsa", "-out", self.pem, "2048"], check=True, capture_output=True)
        out = subprocess.run(["openssl", "rsa", "-in", self.pem, "-noout", "-modulus"],
                             check=True, capture_output=True, text=True).stdout
        self.public = (int(out.strip().split("=")[1], 16), 65537)

    def jwt(self, claims, alg="RS256", kid="k1"):
        head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.pem],
                             input=f"{head}.{body}".encode(), check=True, capture_output=True).stdout
        return f"{head}.{body}.{b64(sig)}"


def patch_access_keys(testcase, rsa):
    """Replace AccessVerifier.fetch so no network is touched; the verifier trusts `rsa` as kid k1."""
    def fake(self):
        self.keys = {"k1": rsa.public}
        self.fetched = self.tried = time.time()
    patcher = mock.patch.object(eb.AccessVerifier, "fetch", fake)
    patcher.start()
    testcase.addClassCleanup(patcher.stop)


def good_claims(**over):
    now = int(time.time())
    claims = {"aud": [AUD], "iss": "https://" + TEAM, "exp": now + 300, "iat": now, "nbf": now,
              "common_name": "svc-token.access", "sub": ""}
    claims.update(over)
    return claims


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------

class TestPureFunctions(unittest.TestCase):
    def test_unwrap(self):
        self.assertEqual(eb.unwrap({"Key": "k", "PairValue": {"a": 1}}), {"a": 1})
        self.assertEqual(eb.unwrap({"a": 1}), {"a": 1})
        self.assertEqual(eb.unwrap("nope"), {})

    def test_clean_phone(self):
        for raw, want in [("416-555-0104", "+14165550104"), ("1 (416) 555 0104", "+14165550104"),
                          ("+44 20 7946 0958", "+442079460958"), ("555-0104", None),
                          ("0000000000", None), ("011 44 20 7946 0958", "+442079460958"),
                          ("416-555-0104 ext 22", "+14165550104"), ("", None), (None, None)]:
            with self.subTest(raw=raw):
                self.assertEqual(eb.clean_phone(raw), want)

    def test_clean_name(self):
        for raw, want in [("JEAN-LUC  PICARD", "Jean-Luc Picard"), ("o'neil", "O'Neil"),
                          ("de la Cruz", "de la Cruz"), ("Elodie", "Elodie"), ("", None),
                          ("MCDONALD", "McDonald"), ("  ANNA  ", "Anna")]:
            with self.subTest(raw=raw):
                self.assertEqual(eb.clean_name(raw), want)

    def test_clean_email(self):
        self.assertEqual(eb.clean_email(" <Jane.Doe@Example.com> "), "jane.doe@example.com")
        self.assertEqual(eb.clean_email("mailto:a@example.com"), "a@example.com")
        for bad in ("not-an-email", "a@b", "", None, "a b@example.com"):
            with self.subTest(bad=bad):
                self.assertIsNone(eb.clean_email(bad))

    def test_edge_date(self):
        self.assertEqual(eb.edge_date("2026-11-05T10:00:00", "America/Toronto"), "2026-11-05T10:00:00-05:00")
        self.assertEqual(eb.edge_date("/Date(0)/", "UTC"), "1970-01-01T00:00:00+00:00")
        self.assertIsNone(eb.edge_date("garbage", "UTC"))
        self.assertIsNone(eb.edge_date("", "UTC"))

    def test_item_state_and_sku(self):
        cfg = {"exclude_item_prefixes": ["TMP"], "sku_fields": ["A", "B"]}
        self.assertEqual(eb.item_state({"ItemStatus": "I"}, "001-1", cfg), "available")
        self.assertEqual(eb.item_state({"ItemStatus": "I"}, "TMP-1", cfg), "excluded")
        self.assertEqual(eb.item_state({"ItemStatus": "S"}, "1", cfg), "sold")
        self.assertEqual(eb.item_state({"ItemStatus": "L"}, "1", cfg), "layaway")
        self.assertEqual(eb.item_state({"ItemStatus": "X"}, "1", cfg), "removed")
        self.assertEqual(eb.item_state({"ItemStatus": "I", "ItemNewItKey": "9"}, "1", cfg), "removed")
        self.assertEqual(eb.item_state({"ItemStatus": "I", "ItemSaleCustKey": "9"}, "1", cfg), "removed")
        self.assertEqual(eb.item_sku({"A": " ", "B": " x1 "}, cfg), ("x1", "B"))
        self.assertEqual(eb.item_sku({}, cfg), ("", None))

    def test_like_escapes_wildcards(self):
        self.assertEqual(eb.like("50%_off\\"), "%50\\%\\_off\\\\%")

    def test_api_params(self):
        allowed = {"a", "b"}
        self.assertEqual(eb.api_params("a=1&b=", allowed), {"a": "1", "b": ""})
        for bad in ("c=1", "a=1&a=2", "a=" + "x" * 201, "a=%00", "a"):
            with self.subTest(bad=bad), self.assertRaises(eb.BadRequest):
                eb.api_params(bad, allowed)

    def test_rules_clean_accepts_and_normalises(self):
        rules, err = eb.rules_clean([{"on": "inventory", "field": "vendor", "op": "is", "value": " omega ",
                                      "action": "set", "to": " OMEGA SA "},
                                     {"on": "customers", "field": "email", "op": "is_empty",
                                      "action": "exclude"}])
        self.assertIsNone(err)
        self.assertEqual(rules[0], {"on": "inventory", "field": "vendor", "op": "is", "action": "set",
                                    "value": "omega", "target": "vendor", "to": "OMEGA SA"})
        self.assertEqual(rules[1], {"on": "customers", "field": "email", "op": "is_empty",
                                    "action": "exclude"})

    def test_rules_clean_rejects_bad_rules(self):
        good = {"on": "inventory", "field": "vendor", "op": "is", "value": "x", "action": "exclude"}
        bad_lists = [
            [{"on": "inventory", "field": "sku", "op": "is", "value": "x", "action": "set",
              "target": "sku", "to": "y"}],                         # sku cannot be changed
            [{"on": "nope"}],
            [dict(good, op="matches", value=".*")],                  # no regex
            [{"on": "sales", "field": "type", "op": "is", "value": "sale", "action": "set",
              "target": "type", "to": "x"}],                         # nothing settable on sales
            [dict(good, value="")],
            [dict(good, value="x" * 121)],
            [dict(good, action="delete")],
            [{"on": "inventory", "field": "vendor", "op": "is", "value": "x", "action": "set", "to": ""}],
            ["x"], "x", None, {"on": "inventory"},
            [good] * 201,
        ]
        for bad in bad_lists:
            with self.subTest(bad=str(bad)[:60]):
                rules, err = eb.rules_clean(bad)
                self.assertIsNone(rules)
                self.assertTrue(err)

    def test_apply_gold_and_rule_hits(self):
        rec = {"vendor": "Omgea", "name": "x"}
        rules = [{"on": "inventory", "field": "vendor", "op": "is", "value": "omgea", "action": "set",
                  "target": "vendor", "to": "Omega"}]
        out = eb.apply_gold("inventory", dict(rec), rules)
        self.assertEqual((out["vendor"], out["gold_rules"]), ("Omega", [1]))
        self.assertIsNone(eb.apply_gold("inventory", dict(rec), [
            {"on": "inventory", "field": "name", "op": "is_not", "value": "y", "action": "exclude"}]))
        # a rule for another kind of record never fires
        self.assertEqual(eb.apply_gold("customers", dict(rec), rules)["gold_rules"], [])

    def test_key_hash_and_key_shape(self):
        key = KEYS["full"]
        self.assertRegex(key, eb.API_KEY_RE)
        self.assertEqual(len(eb.key_hash(key)), 64)

    def test_make_verifier_config_checks(self):
        self.assertIsNone(eb.make_verifier(None, "api"))
        self.assertIsNone(eb.make_verifier({"team_domain": "yourteam.cloudflareaccess.com", "aud": ""}, "api"))
        with self.assertRaises(SystemExit):
            eb.make_verifier({"team_domain": "evil.example.com", "aud": AUD}, "api")
        with self.assertRaises(SystemExit):
            eb.make_verifier({"team_domain": TEAM, "aud": "short"}, "api")

    def test_came_through_proxy(self):
        self.assertTrue(eb.came_through_proxy({"Cf-Ray": "x"}))
        self.assertTrue(eb.came_through_proxy({"X-Forwarded-For": "1.2.3.4"}))
        self.assertTrue(eb.came_through_proxy({"Via": "1.1 proxy"}))
        self.assertFalse(eb.came_through_proxy({"Authorization": "x", "Host": "h"}))

    def test_window(self):
        w = eb.Window(3, 60)
        for _ in range(3):
            self.assertEqual(w.wait("a"), 0)
            w.hit("a")
        self.assertGreater(w.wait("a"), 0)
        self.assertEqual(w.wait("b"), 0)


# --------------------------------------------------------------------------
# Ingest (silver layer inputs, deliveries)
# --------------------------------------------------------------------------

class TestIngest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def env(self, **overrides):
        return Env(self._tmp.name, **overrides)

    def test_counts_and_states(self):
        env = self.env()
        self.assertTrue(env.ingest())
        db = env.db()
        one = lambda sql: db.execute(sql).fetchone()[0]
        self.assertEqual(one("SELECT COUNT(*) FROM items"), 16)
        self.assertEqual(one("SELECT COUNT(*) FROM customers"), 6)
        self.assertEqual(one("SELECT COUNT(*) FROM sales"), 4)
        self.assertEqual(one("SELECT COUNT(*) FROM files"), 2)
        states = {r[0]: r[1] for r in db.execute("SELECT state, COUNT(*) FROM items GROUP BY state")}
        self.assertEqual(states, {"available": 9, "sold": 5, "layaway": 1, "removed": 1})
        self.assertEqual(one("SELECT COUNT(*) FROM changes WHERE field = 'first seen'"), 16)
        # files were put away, not left in the inbox
        self.assertEqual(sorted(os.listdir(env.inbox)), ["completed"])
        self.assertEqual(len(os.listdir(os.path.join(env.inbox, "completed"))), 2)
        db.close()

    def test_sku_fallback_and_missing_sku(self):
        env = self.env()
        env.ingest()
        db = env.db()
        row = db.execute("SELECT sku, sku_source FROM items WHERE item_key='001-I004'").fetchone()
        self.assertEqual((row["sku"], row["sku_source"]), ("OM-200", "ItemStyle"))
        row = db.execute("SELECT sku, sku_source FROM items WHERE item_key='001-I007'").fetchone()
        self.assertEqual((row["sku"], row["sku_source"]), ("", None))
        self.assertEqual([r[0] for r in db.execute("SELECT item_key FROM unreferenced")], ["001-I007"])
        db.close()

    def test_sales_are_grouped_and_customer_attached(self):
        env = self.env()
        env.ingest()
        db = env.db()
        sale = json.loads(db.execute("SELECT payload FROM sales WHERE sale_key='001-S5000'").fetchone()[0])
        self.assertEqual((sale["total"], sale["tax"], sale["type"], sale["status"]),
                         (1500.0, 195.0, "sale", "completed"))
        self.assertEqual(sorted(i["item_key"] for i in sale["items"]), ["001-I010", "001-I011"])
        self.assertEqual(sale["customer"]["key"], "001-1004")
        self.assertEqual(sale["customer"]["email"], "PRIYA.patel@example.com")  # raw in the stored sale
        self.assertEqual(sale["date"], "2026-11-05T10:00:00-05:00")
        layaway = json.loads(db.execute("SELECT payload FROM sales WHERE sale_key='001-S5002'").fetchone()[0])
        self.assertEqual(layaway["type"], "layaway")
        orphan = json.loads(db.execute("SELECT payload FROM sales WHERE sale_key='001-S5003'").fetchone()[0])
        self.assertEqual(orphan["customer"], {"key": "001-9999"})
        # a sold piece with no sale key makes no sale
        self.assertIsNone(db.execute("SELECT 1 FROM sales WHERE payload LIKE '%001-I015%'").fetchone())
        db.close()

    def test_ssn_is_never_stored(self):
        env = self.env()
        env.ingest()
        db = env.db()
        data = db.execute("SELECT data FROM customers WHERE customer_key='001-1004'").fetchone()[0]
        self.assertNotIn("CustomerSSNO", data)
        self.assertNotIn("000-00-0000", data)
        db.close()

    def test_views(self):
        env = self.env()
        env.ingest()
        db = env.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM skus").fetchone()[0], 5)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sale_lines").fetchone()[0], 6)
        row = db.execute("SELECT sales, total_spent FROM customer_list WHERE customer_key='001-1004'").fetchone()
        self.assertEqual((row["sales"], row["total_spent"]), (1, 1500.0))
        sku = db.execute("SELECT * FROM skus WHERE sku='OM-100'").fetchone()
        self.assertEqual((sku["pieces"], sku["available"], sku["sold"]), (5, 3, 2))  # removed piece excluded
        db.close()

    def test_same_content_again_is_skipped(self):
        env = self.env()
        env.ingest()
        env.write("AgainItemList.json", {"Items": base_items()})
        env.write("AgainCustomerList.json", base_customers())
        env.write("Third_ItemList.json", {"Items": base_items()})
        self.assertTrue(eb.run_once(env.cfg))
        db = env.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)  # hashes already known
        self.assertEqual(db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 16)
        self.assertEqual([f for f in os.listdir(env.inbox) if f.endswith(".json")], [])
        db.close()

    def test_incremental_file_records_changes_and_cancels_released_layaway(self):
        env = self.env()
        env.ingest()
        released = base_items()[12]          # 001-I013, the layaway, goes back on the shelf
        d = released["PairValue"]
        for k in ("ItemSaleKey", "ItemSaleDate", "ItemSalePrice", "ItemSaleTax", "ItemSaleCustKey"):
            d.pop(k)
        d["ItemStatus"] = "I"
        repriced = base_items()[0]            # 001-I001 changes price
        repriced["PairValue"]["ItemCurrentPrice"] = 4000
        unchanged = base_items()[1]
        env.write("IncrementalItemList.json", [released, repriced, unchanged])
        self.assertTrue(eb.run_once(env.cfg))
        db = env.db()
        self.assertEqual(db.execute("SELECT state FROM items WHERE item_key='001-I013'").fetchone()[0], "available")
        sale = json.loads(db.execute("SELECT payload FROM sales WHERE sale_key='001-S5002'").fetchone()[0])
        self.assertEqual(sale["status"], "cancelled")
        fields = {r[0] for r in db.execute("SELECT field FROM changes WHERE item_key='001-I001' "
                                           "AND field <> 'first seen'")}
        self.assertEqual(fields, {"ItemCurrentPrice"})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM changes WHERE item_key='001-I002' "
                                    "AND field <> 'first seen'").fetchone()[0], 0)
        db.close()

    def test_unreadable_file_goes_to_failed(self):
        env = self.env()
        with open(os.path.join(env.inbox, "BrokenItemList.json"), "w") as f:
            f.write("{not json")
        eb.run_once(env.cfg)
        self.assertTrue(os.path.exists(os.path.join(env.inbox, "failed", "BrokenItemList.json")))
        self.assertFalse(os.path.exists(os.path.join(env.inbox, "BrokenItemList.json")))

    def test_file_still_uploading_is_left_alone(self):
        env = self.env(settle_seconds=3600)
        env.ingest()
        db = env.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        db.close()
        self.assertTrue(os.path.exists(os.path.join(env.inbox, "FullItemList.json")))

    def test_retention_zero_deletes_processed_files(self):
        env = self.env(retention_days=0)
        env.ingest()
        self.assertEqual(os.listdir(env.inbox), [])

    def test_dry_run_changes_nothing_and_lists_events(self):
        env = self.env(destinations=[{"name": "hook", "type": "webhook", "url": "https://example.com/edt"}])
        env.write("FullCustomerList.json", base_customers())
        env.write("FullItemList.json", {"Items": base_items()})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            eb.run_once(env.cfg, dry_run=True)
        kinds = [m.group(1) for m in re.finditer(r"^\[hook\] (\w+) ", out.getvalue(), re.M)]
        self.assertEqual({k: kinds.count(k) for k in set(kinds)},
                         {"sale": 4, "stock": 5, "item": 16, "customer": 6})
        self.assertIn("Nothing was sent", out.getvalue())
        self.assertFalse(os.path.exists(env.data))                       # no database written
        self.assertTrue(os.path.exists(os.path.join(env.inbox, "FullItemList.json")))  # not moved

    def test_sku_fields_change_regroups(self):
        env = self.env()
        env.ingest()
        cfg = copy.deepcopy(env.cfg)
        cfg["sku_fields"] = ["ItemSerialNumber"]
        db = eb.open_db(cfg)
        eb.resku(db, cfg)
        db.commit()
        self.assertEqual(db.execute("SELECT sku FROM items WHERE item_key='001-I001'").fetchone()[0], "OM1001")
        self.assertEqual(db.execute("SELECT sku FROM items WHERE item_key='001-I005'").fetchone()[0], "")
        db.close()

    def test_webhook_delivery_sent_and_permanent_failure(self):
        received, status = [], {"code": 200}

        class Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.headers.get("Authorization"), body))
                self.send_response(status["code"])
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_address[1]}/edt"
        with mock.patch.dict(os.environ, {"TEST_HOOK_TOKEN": "tok-fake"}):
            env = self.env(destinations=[{"name": "hook", "type": "webhook", "url": url,
                                          "token_env": "TEST_HOOK_TOKEN", "batch_size": 10,
                                          "events": ["sale", "stock"]}])
            self.assertTrue(env.ingest())
        events = [e for _, body in received for e in body["events"]]
        self.assertEqual(sorted(e["event"] for e in events), ["sale"] * 4 + ["stock"] * 5)
        self.assertTrue(all(auth == "Bearer tok-fake" for auth, _ in received))
        db = env.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE state='sent'").fetchone()[0], 9)
        db.close()
        # a destination that answers 400 gives up at once (no retries)
        status["code"] = 400
        env2 = Env(tempfile.mkdtemp(dir=self._tmp.name), destinations=env.cfg["destinations"])
        self.assertFalse(env2.ingest())
        db = env2.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE state='failed'").fetchone()[0], 9)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE state='sent'").fetchone()[0], 0)
        db.close()


# --------------------------------------------------------------------------
# Data API over HTTP, local mode
# --------------------------------------------------------------------------

class ApiTestCase(unittest.TestCase):
    port = None

    def get(self, path, key="full", headers=None, **kw):
        return fetch(self.port, path, KEYS[key] if key else None, headers, **kw)

    def ok(self, path, key="full", **kw):
        status, body, _ = self.get(path, key, **kw)
        self.assertEqual(status, 200, (path, body))
        return body

    def data_by(self, rows, field):
        return {r[field]: r for r in rows}


class TestApiLocal(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        reset_caches()
        cls.port = start_api(ENV.cfg, local=True)

    # ---- basics
    def test_ping_headers_and_scopes(self):
        status, body, h = self.get("/v1/ping")
        self.assertEqual(status, 200)
        self.assertEqual(body["scopes"], ["customers", "inventory", "sales"])
        self.assertIsNotNone(body["last_file"])
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertNotIn("python", h["server"].lower())
        self.assertEqual(self.ok("/v1/ping", "invonly")["scopes"], ["inventory"])

    def test_index_describes_api(self):
        body = self.ok("/v1", "invonly")
        self.assertTrue(body["read_only"])
        self.assertEqual(len(body["endpoints"]), 9)
        self.assertEqual(body["your_scopes"], ["inventory"])
        self.assertIn("0 set", body["layers"]["gold"])
        self.assertEqual(body["layers"]["default"], "silver")
        self.assertEqual(self.ok("/v1/", "full")["name"], "Edge Bridge data API")

    # ---- inventory
    def test_inventory_by_sku(self):
        body = self.ok("/v1/inventory")
        self.assertEqual(body["layer"], "silver")
        rows = self.data_by(body["data"], "sku")
        self.assertEqual(set(rows), {"OM-100", "OM-200", "IW-300", "MX-400"})   # ZZ-500 has none in stock
        om = rows["OM-100"]
        self.assertEqual(om["quantity"], 3)
        self.assertEqual(om["by_store"], {"1": 2, "2": 1})
        self.assertEqual(sorted(om["serial_numbers"]), ["OM1001", "OM1002", "OM1003"])  # om1002 upper-cased
        self.assertEqual((om["sold"], om["layaway"]), (2, 0))
        self.assertEqual((om["price_from"], om["price_to"]), (2500, 4500))
        self.assertEqual(om["vendor"], "Omega")
        self.assertEqual(rows["MX-400"]["layaway"], 1)
        self.assertEqual(rows["OM-200"]["quantity"], 1)

    def test_inventory_quantity_matches_serials_and_stores(self):
        rows = self.ok("/v1/inventory")["data"]
        for r in rows:
            with self.subTest(sku=r["sku"]):
                self.assertGreater(r["quantity"], 0)
                self.assertGreaterEqual(r["quantity"], len(r["serial_numbers"]))
                self.assertEqual(r["quantity"], sum(r["by_store"].values()))

    def test_inventory_quantity_equals_piece_count(self):
        skus = self.ok("/v1/inventory")["data"]
        pieces = self.ok("/v1/inventory?group=piece&limit=500")["data"]
        self.assertTrue(all(p["state"] == "available" for p in pieces))
        self.assertEqual(len(pieces), 9)
        self.assertEqual(len([p for p in pieces if p["sku"]]), sum(r["quantity"] for r in skus))
        self.assertEqual([p["item_key"] for p in pieces if not p["sku"]], ["001-I007"])

    def test_inventory_state_all_includes_sold_only_sku(self):
        rows = self.data_by(self.ok("/v1/inventory?state=all")["data"], "sku")
        self.assertIn("ZZ-500", rows)
        self.assertEqual((rows["ZZ-500"]["quantity"], rows["ZZ-500"]["sold"]), (0, 1))

    def test_inventory_sku_detail(self):
        data = self.ok("/v1/inventory/OM-100")["data"]
        self.assertEqual(data["quantity"], 3)
        self.assertEqual(len(data["pieces"]), 6)           # 3 available, 2 sold, 1 removed
        self.assertEqual(self.get("/v1/inventory/NOPE")[0], 404)

    def test_inventory_piece_filters(self):
        keys = lambda path: [p["item_key"] for p in self.ok(path)["data"]]
        self.assertEqual(keys("/v1/inventory?group=piece&serial=OM1001"), ["001-I001"])
        self.assertEqual(keys("/v1/inventory?group=piece&item_key=001-I002"), ["001-I002"])
        self.assertEqual(keys("/v1/inventory?group=piece&store=2"), ["001-I003"])
        self.assertEqual(keys("/v1/inventory?group=piece&sku=OM-100"), ["001-I001", "001-I002", "001-I003"])
        self.assertEqual(keys("/v1/inventory?group=piece&q=omega"),
                         ["001-I001", "001-I002", "001-I003", "001-I004"])
        self.assertEqual(keys("/v1/inventory?group=piece&state=layaway"), ["001-I013"])
        self.assertEqual([r["sku"] for r in self.ok("/v1/inventory?q=omega")["data"]], ["OM-100", "OM-200"])
        self.assertEqual([r["sku"] for r in self.ok("/v1/inventory?store=2")["data"]], ["OM-100"])

    def test_pagination_pieces_with_cursor(self):
        seen, cursor, pages = [], None, 0
        while True:
            body = self.ok("/v1/inventory?group=piece&state=all&limit=5" + (f"&cursor={cursor}" if cursor else ""))
            self.assertLessEqual(len(body["data"]), 5)
            seen += [p["item_key"] for p in body["data"]]
            cursor, pages = body["next_cursor"], pages + 1
            if not cursor:
                break
        self.assertEqual(pages, 4)
        self.assertEqual(len(seen), 16)
        self.assertEqual(seen, sorted(set(seen)))      # no repeats, in order

    def test_pagination_skus_and_sales_and_customers(self):
        for path, field, total in [("/v1/inventory?state=all", "sku", 5), ("/v1/sales", "sale_key", 4),
                                   ("/v1/customers", "customer_key", 6)]:
            with self.subTest(path=path):
                seen, cursor = [], None
                while True:
                    sep = "&" if "?" in path else "?"
                    body = self.ok(f"{path}{sep}limit=2" + (f"&cursor={cursor}" if cursor else ""))
                    seen += [r[field] for r in body["data"]]
                    cursor = body["next_cursor"]
                    if not cursor:
                        break
                self.assertEqual(len(seen), total)
                self.assertEqual(len(set(seen)), total)

    def test_updated_since(self):
        everything = self.ok("/v1/inventory?state=all&updated_since=2020-01-01T00:00:00Z")["data"]
        self.assertEqual(len(everything), 5)
        self.assertEqual(self.ok("/v1/inventory?updated_since=2999-01-01")["data"], [])
        self.assertEqual(self.ok("/v1/sales?updated_since=2999-01-01")["data"], [])
        self.assertEqual(self.ok("/v1/customers?updated_since=2999-01-01")["data"], [])
        self.assertEqual(self.get("/v1/inventory?updated_since=notatime")[0], 400)

    # ---- sales
    def test_sales_list_and_detail(self):
        rows = self.data_by(self.ok("/v1/sales")["data"], "sale_key")
        self.assertEqual(sorted(rows), ["001-S5000", "001-S5001", "001-S5002", "001-S5003"])
        s = rows["001-S5000"]
        self.assertEqual((s["total"], s["tax"], s["type"], s["currency"]), (1500.0, 195.0, "sale", "CAD"))
        self.assertEqual(len(s["items"]), 2)
        self.assertTrue(s["date"].startswith("2026-11-05"))
        self.assertEqual(s["customer"]["email"], "priya.patel@example.com")   # cleaned
        self.assertEqual(s["customer"]["phone"], "+14165550104")
        self.assertEqual(s["customer"]["first_name"], "Priya")
        self.assertEqual(self.ok("/v1/sales/001-S5001")["data"]["total"], 8000.0)
        self.assertEqual(self.get("/v1/sales/nope")[0], 404)

    def test_sales_filters(self):
        keys = lambda path: [s["sale_key"] for s in self.ok(path)["data"]]
        self.assertEqual(keys("/v1/sales?since=2026-11-05&until=2026-11-05"), ["001-S5000"])
        self.assertEqual(keys("/v1/sales?since=2026-11-07"), ["001-S5002", "001-S5003"])
        self.assertEqual(keys("/v1/sales?type=layaway"), ["001-S5002"])
        self.assertEqual(keys("/v1/sales?type=sale"), ["001-S5000", "001-S5001", "001-S5003"])
        self.assertEqual(keys("/v1/sales?status=cancelled"), [])
        self.assertEqual(keys("/v1/sales?customer_key=001-1001"), ["001-S5001"])
        self.assertEqual(keys("/v1/sales?sku=OM-200"), ["001-S5000"])
        for bad in ("since=yesterday", "since=2026-13-45", "type=gift", "status=x"):
            self.assertEqual(self.get("/v1/sales?" + bad)[0], 400, bad)

    def test_sale_silver_issues(self):
        issues = lambda key: [i["code"] for i in self.ok("/v1/sales/" + key)["data"]["issues"]]
        self.assertEqual(issues("001-S5000"), [])
        self.assertEqual(issues("001-S5003"), ["customer_not_in_feed"])

    # ---- customers
    def test_customers_list_and_detail(self):
        rows = self.data_by(self.ok("/v1/customers")["data"], "customer_key")
        self.assertEqual(len(rows), 6)
        c = self.ok("/v1/customers/001-1004")["data"]
        self.assertEqual(c["first_name"], "Priya")
        self.assertEqual(c["last_name"], "McDonald-O'Brien")
        self.assertEqual(c["emails"], ["priya.patel@example.com"])
        self.assertEqual(c["phones"], ["+14165550104"])
        self.assertEqual(c["issues"], [])
        self.assertEqual([s["sale_key"] for s in c["sales"]], ["001-S5000"])   # full key has the sales scope
        self.assertEqual(self.get("/v1/customers/nope")[0], 404)

    def test_customer_search_and_email_lookup(self):
        keys = lambda path: [c["customer_key"] for c in self.ok(path)["data"]]
        self.assertEqual(keys("/v1/customers?q=priya"), ["001-1004"])
        self.assertEqual(keys("/v1/customers?q=001-1002"), ["001-1002"])
        self.assertEqual(keys("/v1/customers?email=PRIYA.patel@example.com"), ["001-1004"])
        self.assertEqual(keys("/v1/customers?email=jane.doe@example.com"), ["001-1001", "001-1005"])
        self.assertEqual(keys("/v1/customers?email=nobody@example.com"), [])

    def test_customer_without_sales_scope_has_no_sales(self):
        c = self.ok("/v1/customers/001-1004", "custonly")["data"]
        self.assertNotIn("sales", c)
        self.assertEqual(c["emails"], ["priya.patel@example.com"])

    # ---- scopes
    def test_inventory_only_key_cannot_read_sales_or_customers(self):
        for path in ("/v1/sales", "/v1/sales/001-S5000", "/v1/customers", "/v1/customers/001-1004"):
            self.assertEqual(self.get(path, "invonly")[0], 403, path)
        self.assertEqual(self.get("/v1/customers", "invsales")[0], 403)
        self.assertEqual(self.get("/v1/inventory", "custonly")[0], 403)
        self.assertEqual(self.get("/v1/sales", "custonly")[0], 403)

    def test_inventory_only_key_gets_no_sale_fields(self):
        sold = self.ok("/v1/inventory?group=piece&state=sold", "invonly")["data"]
        self.assertTrue(sold)
        for p in sold:
            for field in ("sale_key", "sale_date", "sale_price"):
                self.assertNotIn(field, p)
        full = self.ok("/v1/inventory?group=piece&state=sold", "full")["data"]
        self.assertTrue(any(p.get("sale_price") for p in full))
        self.assertTrue(all("sale_key" in p for p in full))

    def test_sales_scope_without_customers_scope_sees_only_customer_key(self):
        rows = self.ok("/v1/sales", "invsales")["data"]
        self.assertEqual(len(rows), 4)
        for s in rows:
            self.assertEqual(set(s["customer"]), {"key"})
        self.assertEqual(self.ok("/v1/sales/001-S5000", "invsales")["data"]["customer"], {"key": "001-1004"})

    def test_no_customer_pii_without_customers_scope(self):
        seen = []
        for key in ("invonly", "invsales"):
            for path in ("/v1/inventory", "/v1/inventory?group=piece&state=all", "/v1/inventory/OM-100",
                         "/v1/inventory?layer=gold&state=all", "/v1/issues", "/v1/ping", "/v1"):
                seen.append(json.dumps(self.ok(path, key)))
            if key == "invsales":
                for path in ("/v1/sales", "/v1/sales/001-S5000", "/v1/sales?layer=gold"):
                    seen.append(json.dumps(self.ok(path, key)))
        blob = " ".join(seen).lower()
        for needle in ("example.com", "priya", "jane", "john", "+1416", "416-555"):
            self.assertNotIn(needle, blob)

    # ---- layers
    def test_bronze_needs_bronze_scope(self):
        for path in ("/v1/inventory?layer=bronze", "/v1/customers/001-1004?layer=bronze",
                     "/v1/sales?layer=bronze"):
            self.assertEqual(self.get(path, "full")[0], 403, path)
            self.assertEqual(self.get(path, "invonly")[0], 403, path)

    def test_bronze_customer_is_raw(self):
        body = self.ok("/v1/customers/001-1004?layer=bronze", "raw")
        self.assertEqual(body["layer"], "bronze")
        raw = body["data"]["raw"]
        self.assertEqual(raw["CustomerFirstName"], "  PRIYA ")     # untouched
        self.assertEqual(raw["Emails"][0]["PairValue"]["EmailEmail"], "PRIYA.patel@example.com")
        self.assertNotIn("CustomerSSNO", raw)

    def test_bronze_inventory_is_raw_pieces(self):
        body = self.ok("/v1/inventory?layer=bronze&limit=3", "raw")
        self.assertEqual(len(body["data"]), 3)
        self.assertTrue(body["next_cursor"])
        self.assertEqual(body["data"][0]["raw"]["ItemKey"], "001-I001")
        self.assertEqual(body["data"][0]["raw"]["ItemVendStyleCode"], "OM-100")
        self.assertEqual(self.ok("/v1/inventory?layer=bronze&state=all&limit=500", "raw")["data"][1]["raw"]
                         ["ItemSerialNumber"], "om1002")           # not upper-cased in bronze
        self.assertEqual(self.get("/v1/inventory?layer=bronze&group=sku", "raw")[0], 400)

    def test_bronze_sale_has_raw_items(self):
        sale = self.ok("/v1/sales/001-S5000?layer=bronze", "raw")["data"]
        self.assertEqual(sorted(i["ItemKey"] for i in sale["raw_items"]), ["001-I010", "001-I011"])

    def test_silver_is_default_and_cleans_names_emails_phones(self):
        body = self.ok("/v1/customers/001-1001")
        self.assertEqual(body["layer"], "silver")
        c = body["data"]
        self.assertEqual((c["first_name"], c["last_name"]), ("Jane", "Doe"))
        self.assertEqual(c["emails"], ["jane.doe@example.com"])
        self.assertEqual(c["phones"], ["+14165550101"])
        self.assertEqual(c["addresses"][0]["zip"], "M5R 2E3")
        self.assertEqual(c["addresses"][0]["state"], "ON")
        self.assertEqual(c["addresses"][0]["street"], "1 Fake St")
        john = self.ok("/v1/customers/001-1002")["data"]
        self.assertEqual((john["first_name"], john["last_name"], john["phones"]),
                         ("John", "Smith", ["+14165550102"]))

    def test_silver_flags_dirty_customers(self):
        codes = lambda key: sorted(i["code"] for i in self.ok("/v1/customers/" + key)["data"]["issues"])
        c = self.ok("/v1/customers/001-1000")["data"]
        self.assertEqual((c["emails"], c["phones"]), ([], []))
        self.assertEqual(codes("001-1000"), ["invalid_email", "invalid_phone", "no_contact"])
        self.assertEqual(codes("001-1001"), ["duplicate_email"])
        self.assertEqual(codes("001-1005"), ["duplicate_email"])
        self.assertEqual(codes("001-1006"), ["no_name"])

    def test_silver_cleans_and_flags_pieces(self):
        pieces = self.data_by(self.ok("/v1/inventory?group=piece&limit=500")["data"], "item_key")
        codes = lambda k: sorted(i["code"] for i in pieces[k]["issues"])
        self.assertEqual(pieces["001-I002"]["serial_number"], "OM1002")
        self.assertEqual(pieces["001-I005"]["name"], "Fake Pilot")
        self.assertEqual(codes("001-I001"), [])
        self.assertEqual(codes("001-I007"), ["no_sku"])
        self.assertEqual(codes("001-I008"), ["duplicate_serial", "no_price"])
        self.assertEqual(codes("001-I009"), ["duplicate_serial"])
        sold = self.data_by(self.ok("/v1/inventory?group=piece&state=sold&limit=500")["data"], "item_key")
        self.assertEqual([i["code"] for i in sold["001-I015"]["issues"]], ["sold_without_sale"])

    def test_gold_without_rules_matches_silver(self):
        silver = self.ok("/v1/inventory?limit=500")
        gold = self.ok("/v1/inventory?layer=gold&limit=500")
        self.assertEqual(gold["layer"], "gold")
        self.assertEqual(gold["data"], silver["data"])
        self.assertEqual(self.get("/v1/inventory?layer=platinum")[0], 400)

    def test_layer_must_be_given_once(self):
        self.assertEqual(self.get("/v1/inventory?layer=silver&layer=gold")[0], 400)

    # ---- issues
    def test_issues_counts(self):
        body = self.ok("/v1/issues")
        self.assertEqual(body["counts"], {
            "no_sku": 1, "no_price": 1, "duplicate_serial": 2, "sold_without_sale": 1,
            "customer_not_in_feed": 1, "invalid_email": 1, "invalid_phone": 1, "no_contact": 1,
            "duplicate_email": 2, "no_name": 1})
        self.assertEqual(body["total"], 12)
        self.assertEqual(len(body["data"]), 12)

    def test_issues_filters_and_limit(self):
        self.assertEqual({i["type"] for i in self.ok("/v1/issues?type=customers")["data"]}, {"customers"})
        only = self.ok("/v1/issues?code=no_sku")
        self.assertEqual([(i["type"], i["key"]) for i in only["data"]], [("inventory", "001-I007")])
        limited = self.ok("/v1/issues?limit=2")
        self.assertEqual((len(limited["data"]), limited["total"]), (2, 12))
        self.assertEqual(self.get("/v1/issues?type=bogus")[0], 400)

    def test_issues_for_inventory_key_hide_customer_and_sales_issues(self):
        body = self.ok("/v1/issues", "invonly")
        self.assertTrue(body["data"])
        self.assertEqual({i["type"] for i in body["data"]}, {"inventory"})
        self.assertNotIn("invalid_email", body["counts"])
        self.assertNotIn("example", json.dumps(body))
        self.assertNotIn("not-an-email", json.dumps(body))
        self.assertEqual(self.get("/v1/issues?type=customers", "invonly")[0], 400)
        self.assertEqual({i["type"] for i in self.ok("/v1/issues", "invsales")["data"]},
                         {"inventory", "sales"})

    # ---- bad input
    def test_bad_input_is_refused(self):
        cases = [("/v1/inventory?foo=1", 400), ("/v1/inventory?limit=99999", 400),
                 ("/v1/inventory?limit=0", 400), ("/v1/inventory?limit=-1", 400),
                 ("/v1/inventory?limit=x", 400), ("/v1/inventory?sku=a&sku=b", 400),
                 ("/v1/inventory?state=x", 400), ("/v1/inventory?q=" + "a" * 300, 400),
                 ("/v1/inventory?q=%00", 400), ("/v1/inventory?group=bogus", 400),
                 ("/v1/ping?x=1", 400), ("/v1/inventory/OM-100?limit=1", 400),
                 ("/v1/nope", 404), ("/", 404), ("/v2/inventory", 404), ("/v1/inventory/a/b", 404),
                 ("/v1/inventory/..%2f..%2fetc", 404), ("/v1/sales/x'%20OR%201=1--", 404)]
        for path, want in cases:
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], want)

    def test_sql_injection_looking_values_find_nothing(self):
        evil = "x'%20OR%20'1'='1"
        for path in (f"/v1/inventory?sku={evil}", f"/v1/inventory?group=piece&sku={evil}",
                     f"/v1/inventory?group=piece&serial={evil}", f"/v1/inventory?q={evil}",
                     "/v1/inventory?store=1%20OR%201=1", f"/v1/sales?customer_key={evil}",
                     f"/v1/sales?sku={evil}", f"/v1/customers?email={evil}", f"/v1/customers?q={evil}",
                     "/v1/customers?q=%27%3BDROP%20TABLE%20customers%3B--"):
            body = self.ok(path)
            self.assertEqual(body["data"], [], path)
        # an injected cursor is just a (strange) string to compare with, never SQL
        body = self.ok("/v1/customers?cursor='%20UNION%20SELECT%20hash%20FROM%20api_keys--")
        self.assertTrue(all(set(c) >= {"customer_key"} for c in body["data"]))
        self.assertNotIn(eb.key_hash(KEYS["full"]), json.dumps(body))
        # and the tables are still there
        self.assertEqual(len(self.ok("/v1/customers")["data"]), 6)

    def test_like_wildcards_are_literal(self):
        for path in ("/v1/inventory?q=%25", "/v1/inventory?q=_", "/v1/inventory?group=piece&q=%25",
                     "/v1/inventory?group=piece&q=_", "/v1/customers?q=%25", "/v1/customers?q=_",
                     "/v1/inventory?q=O_-100", "/v1/inventory?q=%25M"):
            self.assertEqual(self.ok(path)["data"], [], path)
        self.assertEqual([r["sku"] for r in self.ok("/v1/inventory?q=OM-1")["data"]], ["OM-100"])

    def test_only_get_is_allowed(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(self.get("/v1/inventory", method=method)[0], 405)
        self.assertEqual(self.get("/v1/inventory", method="POST", data=b'{"a":1}')[0], 405)

    def test_extreme_times_are_a_400_not_a_crash(self):
        for value in ("0001-01-01T00:00:00%2B05:00", "9999-12-31T23:59:59-05:00"):
            with self.subTest(value=value):
                self.assertIn(self.get("/v1/inventory?updated_since=" + value)[0], (200, 400))

    def test_long_path_refused(self):
        self.assertEqual(self.get("/v1/" + "a" * 2100)[0], 414)

    # ---- local mode refuses proxied traffic
    def test_local_mode_refuses_proxy_headers(self):
        for header in ({"Cf-Connecting-Ip": "1.2.3.4"}, {"X-Forwarded-For": "1.2.3.4"}, {"Via": "1.1 x"},
                       {"Forwarded": "for=1.2.3.4"}, {"Cf-Ray": "abc"}, {"CF-Access-Jwt-Assertion": "x"},
                       {"X-Real-Ip": "1.2.3.4"}):
            with self.subTest(header=header):
                self.assertEqual(self.get("/v1/ping", headers=header)[0], 403)

    # ---- audit log
    def test_audit_log_has_no_secrets_or_search_text(self):
        self.ok("/v1/customers?q=zzsecretsearch&email=zz@example.com")
        self.ok("/v1/customers/001-1004")
        path = os.path.join(ENV.data, "api_audit.log")
        deadline = time.time() + 3
        text = ""
        while time.time() < deadline and "zz" not in text and "customers" not in text:
            with open(path) as f:
                text = f.read()
        self.assertIn("/v1/customers", text)
        lines = [json.loads(l) for l in text.splitlines() if l.strip()]
        self.assertTrue(any(l.get("params") == ["email", "q"] for l in lines))
        for needle in (KEYS["full"], KEYS["full"].split("_", 2)[2], "zzsecretsearch", "example.com", "PRIYA", "Priya"):
            self.assertNotIn(needle, text)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class TestApiAuth(ApiTestCase):
    """Key checks. Every bad key counts against the caller, so this has its own server."""

    @classmethod
    def setUpClass(cls):
        cls.port = start_api(ENV.cfg, local=True)

    def test_no_key_is_401_with_challenge(self):
        status, _, h = self.get("/v1/ping", None)
        self.assertEqual(status, 401)
        self.assertEqual(h["www-authenticate"], "Bearer")

    def test_revoked_expired_tampered_and_malformed_keys(self):
        self.assertEqual(self.get("/v1/ping", "dead")[0], 401)
        self.assertEqual(self.get("/v1/ping", "expired")[0], 401)
        k = KEYS["full"]
        self.assertEqual(fetch(self.port, "/v1/ping", k[:-1] + ("A" if k[-1] != "A" else "B"))[0], 401)
        self.assertEqual(fetch(self.port, "/v1/ping", "not-a-key")[0], 401)
        self.assertEqual(fetch(self.port, "/v1/ping", None, {"Authorization": "Basic " + k})[0], 401)
        self.assertEqual(fetch(self.port, "/v1/ping", None, {"Authorization": k})[0], 401)
        self.assertEqual(self.get("/v1/ping", "full")[0], 200)       # a good key still works

    def test_key_via_query_string_is_not_accepted(self):
        self.assertEqual(fetch(self.port, "/v1/ping?key=" + KEYS["full"])[0], 401)


class TestApiRateLimit(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = start_api(ENV.cfg, local=True, api={"requests_per_minute": 5})

    def test_rate_limit_per_key(self):
        codes = [self.get("/v1/ping", "invonly")[0] for _ in range(8)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(set(codes[5:]), {429})
        status, body, h = self.get("/v1/ping", "invonly")
        self.assertEqual(status, 429)
        self.assertTrue(h["retry-after"].isdigit())
        self.assertEqual(self.get("/v1/ping", "invsales")[0], 200)   # another key has its own allowance


class TestApiLockout(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = start_api(ENV.cfg, local=True)

    def test_bad_key_lockout(self):
        fake = "edt_000000000000_" + "A" * 43
        codes = [fetch(self.port, "/v1/ping", fake)[0] for _ in range(12)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)
        self.assertLessEqual(codes.count(401), 10)
        status, _, h = self.get("/v1/ping", "full")          # even a good key is held back
        self.assertEqual(status, 429)
        self.assertTrue(h["retry-after"].isdigit())


class TestApiGoldRules(unittest.TestCase):
    """Gold layer: the business's own rules on top of silver, through api_query and over HTTP."""

    RULES = [
        {"on": "inventory", "field": "vendor", "op": "is", "value": "omega", "action": "set",
         "target": "vendor", "to": "OMEGA SA"},
        {"on": "inventory", "field": "vendor", "op": "is", "value": "IWC", "action": "exclude"},
        {"on": "customers", "field": "last_name", "op": "starts_with", "value": "mcdonald",
         "action": "set", "to": "MacDonald"},
        {"on": "customers", "field": "email", "op": "is_empty", "action": "exclude"},
        {"on": "sales", "field": "type", "op": "is", "value": "layaway", "action": "exclude"},
    ]
    SCOPES = {"inventory", "sales", "customers", "bronze"}

    @classmethod
    def setUpClass(cls):
        reset_caches()
        cls.cfg = copy.deepcopy(ENV.cfg)
        cls.cfg["gold_rules"] = copy.deepcopy(cls.RULES)
        cls.db = ENV.ro_db()
        cls.port = start_api(cls.cfg, local=True, api={"default_layer": "gold"})

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def q(self, parts, query="", layer="gold", scopes=None):
        return eb.api_query(self.db, self.cfg, scopes or self.SCOPES, parts, query, 500, layer)

    def test_set_rule_renames_vendor_in_gold_only(self):
        _, silver = self.q(["inventory"], "limit=500", "silver")
        _, gold = self.q(["inventory"], "limit=500", "gold")
        self.assertEqual(gold["layer"], "gold")
        sv, gv = ({r["sku"]: r for r in b["data"]} for b in (silver, gold))
        self.assertEqual(sv["OM-100"]["vendor"], "Omega")
        self.assertEqual(gv["OM-100"]["vendor"], "OMEGA SA")
        self.assertEqual(gv["OM-200"]["vendor"], "OMEGA SA")
        self.assertFalse(any(r["vendor"] == "OMEGA SA" for r in sv.values()))

    def test_exclude_rule_leaves_pieces_and_skus_out(self):
        _, silver = self.q(["inventory"], "limit=500", "silver")
        _, gold = self.q(["inventory"], "limit=500", "gold")
        self.assertIn("IW-300", {r["sku"] for r in silver["data"]})
        self.assertNotIn("IW-300", {r["sku"] for r in gold["data"]})
        _, pieces = self.q(["inventory"], "group=piece&state=all&limit=500")
        self.assertFalse(any(p["vendor"] == "IWC" for p in pieces["data"]))
        self.assertTrue(any(p["gold_rules"] == [1] for p in pieces["data"]))
        self.assertEqual(self.q(["inventory", "IW-300"])[0], 404)
        self.assertEqual(self.q(["inventory", "OM-100"])[0], 200)

    def test_customer_rules(self):
        _, c = self.q(["customers", "001-1004"])
        self.assertEqual(c["data"]["last_name"], "MacDonald")
        self.assertEqual(c["data"]["gold_rules"], [3])
        self.assertEqual(self.q(["customers", "001-1000"])[0], 404)   # no email: left out
        self.assertEqual(self.q(["customers", "001-1006"])[0], 404)
        _, lst = self.q(["customers"], "limit=500")
        self.assertEqual([r["customer_key"] for r in lst["data"]],
                         ["001-1001", "001-1002", "001-1004", "001-1005"])

    def test_sales_rules(self):
        _, silver = self.q(["sales"], "", "silver")
        _, gold = self.q(["sales"], "")
        self.assertEqual(len(silver["data"]), 4)
        self.assertTrue(all(s["type"] == "sale" for s in gold["data"]))
        self.assertEqual(len(gold["data"]), 3)
        self.assertEqual(self.q(["sales", "001-S5002"])[0], 404)
        items = [i for s in gold["data"] for i in s["items"]]
        self.assertTrue(any(i["vendor"] == "OMEGA SA" for i in items))

    def test_default_layer_and_index_over_http(self):
        status, body, _ = fetch(self.port, "/v1/inventory", KEYS["invonly"])
        self.assertEqual(status, 200)
        self.assertEqual(body["layer"], "gold")
        self.assertNotIn("IW-300", [r["sku"] for r in body["data"]])
        body = fetch(self.port, "/v1", KEYS["invonly"])[1]
        self.assertEqual(body["layers"]["default"], "gold")
        self.assertIn("5 set", body["layers"]["gold"])
        # silver can still be asked for
        body = fetch(self.port, "/v1/inventory?layer=silver", KEYS["invonly"])[1]
        self.assertIn("IW-300", [r["sku"] for r in body["data"]])


class TestDirtyPrices(unittest.TestCase):
    """The feed can hold prices as text. Silver promises prices as numbers."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.env = Env(cls._tmp.name)
        cls.env.ingest(items=[
            item("001-D001", "ItemVendStyleCode", "TX-1", retail="1,250.00", current="1200.50"),
            item("001-D002", "ItemVendStyleCode", "TX-1", retail=1300, current=1250),
        ], customers=[])
        cls.key = cls.env.make_key("t", "inventory")
        reset_caches()
        cls.port = start_api(cls.env.cfg, local=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_text_prices_become_numbers_for_pieces(self):
        status, body, _ = fetch(self.port, "/v1/inventory?group=piece", self.key)
        self.assertEqual(status, 200, body)
        prices = {p["item_key"]: (p["retail_price"], p["current_price"]) for p in body["data"]}
        self.assertEqual(prices["001-D001"], (1250.0, 1200.5))        # "1,250.00" reads as 1250
        self.assertEqual(prices["001-D002"], (1300.0, 1250.0))

    def test_text_prices_do_not_break_the_sku_endpoint(self):
        status, body, _ = fetch(self.port, "/v1/inventory", self.key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"][0]["price_from"], 1200.5)
        self.assertEqual(body["data"][0]["price_to"], 1250)


# --------------------------------------------------------------------------
# Cloudflare Access mode
# --------------------------------------------------------------------------

@unittest.skipUnless(HAVE_OPENSSL, "openssl is not installed")
class TestApiAccessMode(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rsa = Rsa(cls._tmp.name, "good")
        cls.other = Rsa(cls._tmp.name, "other")
        patch_access_keys(cls, cls.rsa)
        cfg = copy.deepcopy(ENV.cfg)
        cfg["api"] = {"port": 0, "requests_per_minute": 100000, "max_limit": 500,
                      "cloudflare_access": {"team_domain": TEAM, "aud": AUD}}
        cls.port = start_api(cfg, local=False)

    def call(self, token, key="full", path="/v1/ping", extra=None):
        headers = {"Cf-Connecting-Ip": "203.0.113.9"}
        if token is not None:
            headers["Cf-Access-Jwt-Assertion"] = token
        headers.update(extra or {})
        return self.get(path, key, headers)[0]

    def test_good_token_and_key(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims())), 200)

    def test_good_token_without_key_is_401(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims()), key=None), 401)

    def test_no_token_is_403_even_with_a_good_key(self):
        self.assertEqual(self.call(None), 403)

    def test_wrong_signer(self):
        self.assertEqual(self.call(self.other.jwt(good_claims())), 403)

    def test_wrong_audience(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(aud=["b" * 64]))), 403)
        self.assertEqual(self.call(self.rsa.jwt(good_claims(aud=[]))), 403)

    def test_audience_may_be_a_list_holding_ours(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(aud=["c" * 64, AUD]))), 200)

    def test_wrong_issuer(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(iss="https://evil.cloudflareaccess.com"))), 403)

    def test_expired(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(exp=int(time.time()) - 5))), 403)

    def test_missing_or_odd_exp(self):
        claims = good_claims()
        del claims["exp"]
        self.assertEqual(self.call(self.rsa.jwt(claims)), 403)
        self.assertEqual(self.call(self.rsa.jwt(good_claims(exp=True))), 403)
        self.assertEqual(self.call(self.rsa.jwt(good_claims(exp="9999999999"))), 403)

    def test_not_yet_valid(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(nbf=int(time.time()) + 3600))), 403)

    def test_unknown_kid(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(), kid="zzz")), 403)

    def test_alg_none(self):
        token = self.rsa.jwt(good_claims())
        _, body, _ = token.split(".")
        forged = b64(json.dumps({"alg": "none", "kid": "k1"}).encode()) + "." + body + "."
        self.assertEqual(self.call(forged), 403)

    def test_hs256_confusion(self):
        self.assertEqual(self.call(self.rsa.jwt(good_claims(), alg="HS256")), 403)

    def test_tampered_body(self):
        head, _, sig = self.rsa.jwt(good_claims()).split(".")
        forged = b64(json.dumps(good_claims(exp=int(time.time()) + 99999, common_name="evil")).encode())
        self.assertEqual(self.call(f"{head}.{forged}.{sig}"), 403)

    def test_garbage_tokens(self):
        for token in ("a.b.c", "....", "x" * 9000, "", "not-a-jwt"):
            with self.subTest(token=token[:20]):
                self.assertEqual(self.call(token), 403)
        head, body, _ = self.rsa.jwt(good_claims()).split(".")
        self.assertEqual(self.call(f"{head}.{body}.{b64(bytes(256))}"), 403)   # all-zero signature

    def test_scopes_still_apply_behind_access(self):
        token = self.rsa.jwt(good_claims())
        h = {"Cf-Access-Jwt-Assertion": token, "Cf-Connecting-Ip": "203.0.113.9"}
        self.assertEqual(self.get("/v1/sales", "invonly", h)[0], 403)
        sold = self.get("/v1/inventory?group=piece&state=sold", "invonly", h)[1]["data"]
        self.assertTrue(sold and all("sale_price" not in p and "sale_key" not in p for p in sold))
        sold = self.get("/v1/inventory?group=piece&state=sold", "full", h)[1]["data"]
        self.assertTrue(any(p.get("sale_price") is not None for p in sold))

    def test_audit_names_the_service_token(self):
        self.call(self.rsa.jwt(good_claims()), path="/v1/ping")
        with open(os.path.join(ENV.data, "api_audit.log")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(any(l.get("who") == "svc-token.access" and l.get("ip") == "203.0.113.9" for l in lines))

    def test_api_refuses_to_start_without_access_unless_local(self):
        cfg = copy.deepcopy(ENV.cfg)
        cfg["api"] = dict(cfg["api"], port=free_port())
        with self.assertRaises(SystemExit):
            eb.cmd_api(cfg, False)


# --------------------------------------------------------------------------
# Web interface (cmd_serve)
# --------------------------------------------------------------------------

class TestWebUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.config_path = os.path.join(cls._tmp.name, "config_ui.json")
        shutil.copy(ENV.config_path, cls.config_path)
        os.chmod(cls.config_path, 0o644)
        cls.cfg = eb.load_config(cls.config_path)
        reset_caches()
        cls.port = start_ui(cls.cfg, {"host": "127.0.0.1"}, cls.config_path)

    def get(self, path, headers=None, port=None):
        return fetch(port or self.port, path, None, headers)

    def post(self, body, headers=None, raw=None, port=None):
        h = {"Content-Type": "application/json", "X-EDT-Settings": "1"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else json.dumps(body).encode()
        return fetch(port or self.port, "/api/settings", None, h, "POST", data)[:2]

    def file(self):
        with open(self.config_path) as f:
            return json.load(f)

    # ---- reading
    def test_page_and_summary(self):
        status, body, h = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        status, s, _ = self.get("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual({k: s[k] for k in ("skus", "skus_in_stock", "pieces_available", "sales",
                                            "sales_total", "layaways", "customers", "buyers", "failed")},
                         {"skus": 5, "skus_in_stock": 4, "pieces_available": 9, "sales": 4,
                          "sales_total": 10500.0, "layaways": 1, "customers": 6, "buyers": 4, "failed": 0})

    def test_lists(self):
        s = self.get("/api/skus")[1]
        self.assertEqual((s["total"], s["unreferenced"]), (5, 1))
        self.assertEqual([r["sku"] for r in self.get("/api/skus?stock=in")[1]["rows"]],
                         ["IW-300", "MX-400", "OM-100", "OM-200"])
        self.assertEqual([r["sku"] for r in self.get("/api/skus?q=omega")[1]["rows"]], ["OM-100", "OM-200"])
        pieces = self.get("/api/pieces?sku=OM-100")[1]
        self.assertEqual([p["state"] for p in pieces], ["available"] * 3 + ["sold"] * 2)
        self.assertEqual(self.get("/api/sales")[1]["total"], 4)
        self.assertEqual(self.get("/api/customers?q=priya")[1]["total"], 1)
        c = self.get("/api/customer?key=001-1004")[1]
        self.assertEqual(c["customer"]["customer_key"], "001-1004")
        self.assertEqual(len(c["sales"]), 1)
        self.assertEqual(self.get("/api/customer?key=nope")[0], 404)

    def test_unknown_paths(self):
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertEqual(self.get("/elsewhere")[0], 404)

    def test_ui_never_returns_ssn(self):
        c = json.dumps(self.get("/api/customer?key=001-1004")[1])
        self.assertNotIn("000-00-0000", c)

    # ---- refusing to open up
    def test_refuses_non_loopback_host(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):
            with self.subTest(host=host), self.assertRaises(SystemExit) as ctx:
                eb.cmd_serve(dict(self.cfg, ui={"host": host, "port": free_port()}))
            self.assertIn("only listens on this machine", str(ctx.exception))

    def test_proxied_requests_refused_without_access(self):
        for h in ({"Cf-Connecting-Ip": "1.2.3.4", "Cf-Ray": "x"}, {"X-Forwarded-For": "1.2.3.4"},
                  {"Via": "1.1 proxy"}):
            with self.subTest(h=h):
                self.assertEqual(self.get("/api/customers", h)[0], 403)
                self.assertEqual(self.get("/", h)[0], 403)

    # ---- settings view
    def test_settings_view_has_no_secrets(self):
        status, body, _ = self.get("/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("default_layer", body["editable"])
        self.assertGreaterEqual(len(body["info"]["keys"]), 6)
        self.assertEqual({k["state"] for k in body["info"]["keys"]}, {"active", "revoked", "expired"})
        blob = json.dumps(body)
        for name, key in KEYS.items():
            self.assertNotIn(key.split("_", 2)[2], blob, name)          # the secret part of the key
            self.assertNotIn(eb.key_hash(key), blob, name)
        self.assertNotIn('"hash"', blob)
        self.assertNotIn("password", blob.lower())

    # ---- settings writes
    def test_save_writes_file_privately(self):
        status, body = self.post({"currency": "usd", "default_layer": "gold", "retention_days": 7,
                                  "sku_fields": "ItemStyle, ItemVendStyleCode", "phone_country_code": "+44",
                                  "exclude_item_prefixes": "TMP, TEST", "max_limit": 200,
                                  "requests_per_minute": 90, "timezone": "America/Vancouver"})
        self.assertEqual((status, body), (200, {"saved": True}))
        saved = self.file()
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["retention_days"], 7)
        self.assertEqual(saved["sku_fields"], ["ItemStyle", "ItemVendStyleCode"])
        self.assertEqual(saved["phone_country_code"], "44")
        self.assertEqual(saved["exclude_item_prefixes"], ["TMP", "TEST"])
        self.assertEqual(saved["timezone"], "America/Vancouver")
        self.assertEqual((saved["api"]["default_layer"], saved["api"]["max_limit"],
                          saved["api"]["requests_per_minute"]), ("gold", 200, 90))
        self.assertEqual(saved["api"]["port"], self.cfg["api"]["port"])    # untouched
        self.assertEqual(saved["data_dir"], self.cfg["data_dir"])
        self.assertEqual(saved["inbox"], self.cfg["inbox"])
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(self.config_path)).count("config_ui.json"), 1)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.config_path)) if f.endswith(".tmp")], [])
        view = self.get("/api/settings")[1]["editable"]
        self.assertEqual((view["currency"], view["default_layer"], view["retention_days"]), ("USD", "gold", 7))
        self.post({"currency": "CAD", "default_layer": "silver", "timezone": "America/Toronto"})

    def test_gold_rules_saved_from_settings(self):
        status, _ = self.post({"gold_rules": TestApiGoldRules.RULES})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.file()["gold_rules"]), 5)
        self.assertEqual(len(self.get("/api/settings")[1]["editable"]["gold_rules"]), 5)
        self.assertEqual(self.post({"gold_rules": []})[0], 200)
        self.assertEqual(self.file()["gold_rules"], [])

    def test_locked_fields_are_rejected(self):
        before = self.file()
        for bad in ({"data_dir": "/tmp/x"}, {"inbox": "/"}, {"destinations": [{"name": "x", "url": "https://evil.example"}]},
                    {"ui": {"host": "0.0.0.0"}}, {"api": {"cloudflare_access": {}}}, {"api": {"port": 1}},
                    {"heartbeat_url": "http://169.254.169.254/"}, {"settle_seconds": 0}, {"poll_seconds": 1},
                    {"nonsense": 1}, {"currency": "CAD", "inbox": "/"}):
            with self.subTest(bad=list(bad)):
                status, body = self.post(bad)
                self.assertEqual(status, 400)
                self.assertIn("_", body["errors"])
        self.assertEqual(self.file(), before)

    def test_invalid_values_are_rejected_and_change_nothing(self):
        before = self.file()
        for bad in ({"timezone": "Mars/Phobos"}, {"timezone": "../../etc/passwd"}, {"currency": "DOLLARS"},
                    {"currency": "C$"}, {"sku_fields": "CustomerSSNO"}, {"sku_fields": ""},
                    {"sku_fields": "ItemA,ItemB,ItemC,ItemD,ItemE,ItemF,ItemG"}, {"retention_days": -1},
                    {"retention_days": "7"}, {"retention_days": True}, {"retention_days": 7.5},
                    {"retention_days": 99999}, {"max_limit": 5000}, {"max_limit": 0}, {"requests_per_minute": 0},
                    {"default_layer": "platinum"}, {"phone_country_code": "abc"}, {"phone_country_code": "0"},
                    {"exclude_item_prefixes": "a;b"}, {"exclude_item_prefixes": 5}):
            with self.subTest(bad=bad):
                status, body = self.post(bad)
                self.assertEqual(status, 400)
                self.assertTrue(body["errors"])
        self.assertEqual(self.file(), before)

    def test_bad_gold_rules_are_rejected(self):
        for bad in ([{"on": "nope"}], "x", [{"on": "inventory", "field": "sku", "op": "is", "value": "x",
                                              "action": "set", "target": "sku", "to": "y"}],
                    [dict(TestApiGoldRules.RULES[0], op="regex")]):
            status, body = self.post({"gold_rules": bad})
            self.assertEqual(status, 400)
            self.assertIn("gold_rules", body["errors"])

    def test_malformed_and_oversized_bodies(self):
        before = self.file()
        self.assertEqual(self.post(None, raw=b"{nope")[0], 400)
        self.assertEqual(self.post(None, raw=b"[1]")[0], 400)
        self.assertEqual(self.post(None, raw=b"")[0], 400)
        self.assertEqual(self.post(None, raw=b'{"currency":"' + b"A" * 30000 + b'"}')[0], 400)
        # 201 rules is over the 20 KB body limit before the 200-rule limit is even reached
        status, body = self.post({"gold_rules": [TestApiGoldRules.RULES[0]] * 201})
        self.assertEqual(status, 400)
        self.assertEqual(self.file(), before)

    # ---- CSRF
    def test_csrf_missing_header(self):
        before = self.file()
        self.assertEqual(self.post({"currency": "EUR"}, {"X-EDT-Settings": None})[0], 403)
        self.assertEqual(self.post({"currency": "EUR"}, {"X-EDT-Settings": "0"})[0], 403)
        self.assertEqual(self.file(), before)

    def test_csrf_wrong_content_type(self):
        before = self.file()
        self.assertEqual(self.post({"currency": "EUR"}, {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.post({"currency": "EUR"}, {"Content-Type": "application/x-www-form-urlencoded"})[0], 403)
        self.assertEqual(self.file(), before)

    def test_csrf_foreign_origin(self):
        before = self.file()
        for origin in ("https://evil.example", "http://127.0.0.1:1", "null", "http://localhost:%d" % self.port):
            with self.subTest(origin=origin):
                self.assertEqual(self.post({"currency": "EUR"}, {"Origin": origin})[0], 403)
        self.assertEqual(self.file(), before)

    def test_same_origin_and_no_origin_are_allowed(self):
        self.assertEqual(self.post({"currency": "CAD"}, {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.post({"currency": "CAD"})[0], 200)

    def test_only_settings_can_be_posted(self):
        h = {"Content-Type": "application/json", "X-EDT-Settings": "1"}
        self.assertEqual(fetch(self.port, "/api/customers", None, h, "POST", b"{}")[0], 404)
        self.assertEqual(fetch(self.port, "/", None, h, "POST", b"{}")[0], 404)

    def test_post_through_a_proxy_is_refused(self):
        before = self.file()
        self.assertEqual(self.post({"currency": "EUR"}, {"Cf-Ray": "x"})[0], 403)
        self.assertEqual(self.post({"currency": "EUR"}, {"X-Forwarded-For": "1.2.3.4"})[0], 403)
        self.assertEqual(self.file(), before)

    def test_settings_post_needs_a_config_file(self):
        port = start_ui(self.cfg, {"host": "127.0.0.1"}, None)
        self.assertEqual(self.post({"currency": "CAD"}, port=port)[0], 404)


class TestWebUiPassword(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_caches()
        cls.port = start_ui(ENV.cfg, {"host": "127.0.0.1", "password": "pw-correct"})

    @staticmethod
    def basic(pw):
        return {"Authorization": "Basic " + base64.b64encode(("u:" + pw).encode()).decode()}

    def test_password_lockout(self):
        self.assertEqual(fetch(self.port, "/api/summary")[0], 401)              # no attempt, no strike
        self.assertEqual(fetch(self.port, "/api/summary", None, self.basic("pw-correct"))[0], 200)
        codes = [fetch(self.port, "/", None, self.basic("wrong%d" % i))[0] for i in range(12)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(fetch(self.port, "/", None, self.basic("pw-correct"))[0], 429)


@unittest.skipUnless(HAVE_OPENSSL, "openssl is not installed")
class TestWebUiAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rsa = Rsa(cls._tmp.name, "good")
        cls.other = Rsa(cls._tmp.name, "other")
        patch_access_keys(cls, cls.rsa)
        reset_caches()
        cls.port = start_ui(ENV.cfg, {"host": "127.0.0.1", "password": "pw-correct",
                                      "cloudflare_access": {"team_domain": TEAM, "aud": AUD}})

    @staticmethod
    def basic(pw):
        return {"Authorization": "Basic " + base64.b64encode(("u:" + pw).encode()).decode()}

    def via_tunnel(self, token, pw="pw-correct"):
        h = dict(self.basic(pw) if pw else {}, **{"Cf-Connecting-Ip": "203.0.113.9"})
        if token is not None:
            h["Cf-Access-Jwt-Assertion"] = token
        return fetch(self.port, "/", None, h)[0]

    def test_tunnelled_without_token_is_403(self):
        self.assertEqual(self.via_tunnel(None), 403)

    def test_tunnelled_with_forged_token_is_403(self):
        self.assertEqual(self.via_tunnel(self.other.jwt(good_claims())), 403)
        self.assertEqual(self.via_tunnel(self.rsa.jwt(good_claims(exp=int(time.time()) - 5))), 403)
        self.assertEqual(self.via_tunnel(self.rsa.jwt(good_claims(aud=["b" * 64]))), 403)

    def test_tunnelled_with_token_still_needs_the_password(self):
        token = self.rsa.jwt(good_claims())
        self.assertEqual(self.via_tunnel(token), 200)
        self.assertEqual(self.via_tunnel(token, pw=None), 401)
        self.assertEqual(self.via_tunnel(token, pw="wrong"), 401)

    def test_local_requests_need_only_the_password(self):
        self.assertEqual(fetch(self.port, "/", None, self.basic("pw-correct"))[0], 200)




class TestRebindingAndPlugins(unittest.TestCase):
    """A direct request must name this machine in Host; add-on destinations load and send."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.env = Env(cls._tmp.name)
        cls.env.ingest()
        reset_caches()
        cls.ui_port = start_ui(cls.env.cfg, {"host": "127.0.0.1"}, cls.env.config_path)
        cls.api_port = start_api(cls.env.cfg, local=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_foreign_host_is_refused_by_the_interface(self):
        self.assertEqual(fetch(self.ui_port, "/api/summary")[0], 200)
        for host in ("evil.example", f"evil.example:{self.ui_port}", "127.0.0.1:1", ""):
            status, _, _ = fetch(self.ui_port, "/api/customers", headers={"Host": host})
            self.assertEqual(status, 403, host)
        status, _, _ = fetch(self.ui_port, "/api/settings", method="POST", data=b'{"currency":"USD"}',
                             headers={"Host": f"evil.example:{self.ui_port}", "X-EDT-Settings": "1",
                                      "Origin": f"http://evil.example:{self.ui_port}",
                                      "Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_foreign_host_is_refused_by_the_local_api(self):
        status, _, _ = fetch(self.api_port, "/v1/ping", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_plugin_destination_sends_and_is_checked(self):
        path = os.path.join(self._tmp.name, "addon.py")
        with open(path, "w") as f:
            f.write("EVENTS = ['sale']\nSEEN = []\n"
                    "def send(dest, event, bridge):\n"
                    "    SEEN.append((event['event'], event['key'], bridge.VERSION))\n"
                    "    return 200, 'ok'\n")
        os.chmod(path, 0o600)
        dest = {"name": "addon", "type": "plugin", "module": path}
        self.assertTrue(eb.wants(dest, "sale"))
        self.assertFalse(eb.wants(dest, "customer"))
        cfg = dict(self.env.cfg, destinations=[dest])
        db = eb.open_db(cfg)
        try:
            eb.enqueue(db, cfg, "sale", "S1", {"sale_key": "S1"})
            db.commit()
            self.assertTrue(eb.deliver(db, cfg))
            self.assertEqual(eb.load_plugin(dest).SEEN, [("sale", "S1", eb.VERSION)])
            state = db.execute("SELECT state FROM outbox WHERE dest='addon'").fetchone()[0]
            self.assertEqual(state, "sent")
        finally:
            db.close()
        loose = os.path.join(self._tmp.name, "loose.py")
        shutil.copy(path, loose)
        os.chmod(loose, 0o666)  # anyone could rewrite it: refused
        with self.assertRaises(PermissionError):
            eb.load_plugin({"module": loose})

    def test_post_does_not_follow_redirects(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        status, _ = eb.http_post(f"http://127.0.0.1:{srv.server_address[1]}/", "{}", {"Authorization": "Bearer t"})
        srv.server_close()
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()
