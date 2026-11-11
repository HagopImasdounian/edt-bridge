# Configuration

By the end of this page you can read, change and check every setting, add a destination, write a plugin and set gold rules.

The config is one JSON file. Point the bridge at it with `--config /path/config.json` or the `EDT_BRIDGE_CONFIG` environment variable. Without either, it looks for `config.json` next to `edt_bridge.py`.

`python3 edt_bridge.py init` writes a starter file with every default below. Any key you leave out takes its default. The config holds tokens, so `init` makes it readable by its owner only. A broken file stops the bridge with an error, so check it after every edit with `python3 edt_bridge.py status`.

Settings are merged one level deep. A top-level key in your file replaces the default for that key. The `api` block fills in its own missing keys. The `ui` block does too, except `password_env`: its default applies only when your file has no `ui` block at all. If you write a `ui` block, name `password_env` in it.

## Top-level keys

| Key | Type | Default | What it does |
|---|---|---|---|
| `inbox` | string | `/srv/edt/inbox` | The folder EDT uploads to. The bridge reads files here and moves them into `completed/` and `failed/` inside it. |
| `data_dir` | string | `/srv/edt/data` | Holds `edt.db`, `api_audit.log` and a `lock` file. Created readable only by its owner. |
| `timezone` | string | `America/Toronto` | Edge sends local times with no offset. The bridge adds this zone's offset. Use a name like `America/Toronto`. |
| `currency` | string | `CAD` | Three-letter code put on every sale event and API sale. |
| `retention_days` | integer | `30` | How long processed files are kept. `0` deletes a file as soon as it is processed. See [operations](operations.md#retention-what-is-deleted-and-when). |
| `sales_lookback_days` | integer | `30` | Sales older than this are stored but no `sale` event is sent. This stops a first full import from flooding your destinations with history. `0` sends every sale. |
| `settle_seconds` | integer | `30` | A file must be this many seconds old before it is read. Stops the bridge reading a half-uploaded file. |
| `poll_seconds` | integer | `60` | How long `watch` sleeps between passes. |
| `exclude_item_prefixes` | list of strings | `[]` | Pieces whose item key starts with one of these are marked `excluded`, not `available`. |
| `sku_fields` | list of strings | `["ItemMetaBaseSKU", "ItemVendStyleCode", "ItemStyle"]` | Edge fields that make the SKU, in order. The first one filled in wins. Change it and the bridge regroups every piece on the next pass. Run `fields` to choose. |
| `heartbeat_url` | string | `""` | A URL the bridge requests with GET after a pass in which every delivery succeeded. Point a monitoring service at it to learn when the bridge stops. |
| `db_key_env` | string | `""` | Turns on database encryption. Names the environment variable that holds the key. See [encrypting the database](#encrypting-the-database). |
| `phone_country_code` | string | `"1"` | Used by silver to write phone numbers as `+14165550100`. `"1"` applies North American rules. Another code, like `"44"`, is added in front of numbers that have none. |
| `gold_rules` | list | `[]` | Your own corrections for the gold API layer. See [gold rules](#gold-rules). |
| `ui` | object | see below | The web interface. |
| `api` | object | see below | The data API. |
| `destinations` | list | `[]` in code, one example webhook in `init` | Where events go. See [destinations](#destinations). Every destination needs a unique `name`. |

## The `ui` block

| Key | Type | Default | What it does |
|---|---|---|---|
| `ui.host` | string | `127.0.0.1` | Must be `127.0.0.1`, `localhost` or `::1`. Anything else stops `serve` with an error. The interface never listens on the network. |
| `ui.port` | integer | `8787` | Port on this machine. |
| `ui.password_env` | string | `EDT_UI_PASSWORD` | Name of the environment variable that holds a password. If the variable is set, the interface asks for it (HTTP Basic, any user name). If it is not set, there is no password. |
| `ui.password` | string | not set | The password written straight in the config. Prefer `password_env`. |
| `ui.cloudflare_access.team_domain` | string | `yourteam.cloudflareaccess.com` | Your Cloudflare Zero Trust team domain. |
| `ui.cloudflare_access.aud` | string | `""` | The Application Audience tag of the Access application that protects the interface. 64 lowercase hex characters. |

With `aud` empty and the placeholder team domain, the interface refuses any request that came through a tunnel or proxy. With both filled in, it accepts those requests only when they carry a valid Access token. Filling in one and not the other stops `serve` with an error.

## The `api` block

| Key | Type | Default | What it does |
|---|---|---|---|
| `api.port` | integer | `8788` | Port on this machine. The API always listens on `127.0.0.1`. There is no setting to change that. |
| `api.default_layer` | string | `silver` | Layer used when a request has no `layer`. One of `bronze`, `silver`, `gold`. |
| `api.requests_per_minute` | integer | `120` | Rate limit per API key. |
| `api.max_limit` | integer | `500` | Largest `limit` a caller may ask for. The bridge caps it at 900 whatever you set. |
| `api.cloudflare_access.team_domain` | string | `yourteam.cloudflareaccess.com` | Your Zero Trust team domain. |
| `api.cloudflare_access.aud` | string | `""` | The Application Audience tag of the Access application that protects `/v1`. 64 lowercase hex characters. |

The API will not start without a real `team_domain` and `aud`, unless you run `api --local`. The interface and the API use different Access applications, so their `aud` values differ.

## What the Settings page can change

The Settings page in the web interface edits a small set of keys and saves them to the config file. The API and `watch` pick up the change without a restart. Everything else is set on the server, on purpose, so that nobody with a browser can redirect your data or open a door.

| Key | On the Settings page? | Allowed values |
|---|---|---|
| `timezone` | Yes | A valid zone name |
| `currency` | Yes | Three letters |
| `sku_fields` | Yes | One to six names that start with `Item` |
| `exclude_item_prefixes` | Yes | Up to 50 prefixes, 30 characters each |
| `retention_days` | Yes | 0 to 3650 |
| `sales_lookback_days` | Yes | 0 to 3650 |
| `phone_country_code` | Yes | One to three digits, no leading zero |
| `api.default_layer` | Yes | `bronze`, `silver`, `gold` |
| `api.requests_per_minute` | Yes | 1 to 6000 |
| `api.max_limit` | Yes | 1 to 900 |
| `gold_rules` | Yes | Up to 200 valid rules |
| `inbox`, `data_dir` | Server only | |
| `settle_seconds`, `poll_seconds`, `heartbeat_url`, `db_key_env` | Server only | |
| `destinations` and all their secrets | Server only | |
| `ui.*` including the password and Access settings | Server only | |
| `api.port`, `api.cloudflare_access.*` | Server only | |
| API keys | Server only (`apikey` command). The page lists them but cannot create or revoke. | |

Saving rewrites the whole file with its other keys intact, and keeps it readable by its owner only. The service that runs `serve` therefore needs write access to the folder that holds the config.

## Secrets

Never put a token or password in git. Give the value through an environment variable and name the variable in the config.

| Where | Direct value | Environment variable |
|---|---|---|
| A destination's bearer token | `"token": "..."` | `"token_env": "EDT_WEBHOOK_TOKEN"` |
| The interface password | `"password": "..."` | `"password_env": "EDT_UI_PASSWORD"` |

A direct value wins if both are set. Under systemd, put the variables in a root-owned file named by `EnvironmentFile=`. [setup.md](setup.md) shows how. A plugin reads its own secrets the same way, with `bridge.secret(dest, "token")`.

## Destinations

A destination is where the bridge sends events. Each one has its own queue, so a slow or broken destination does not hold back the others.

| Key | Type | Default | Applies to | What it does |
|---|---|---|---|---|
| `name` | string | required | both | Unique label. Shown in `status`. |
| `type` | string | `webhook` | both | `webhook` or `plugin`. Anything other than `plugin` is a webhook. |
| `url` | string | required for webhook | webhook | Where to POST. |
| `token` or `token_env` | string | none | both | Bearer token. A webhook sends it as `Authorization: Bearer ...`. A plugin reads it with `bridge.secret`. |
| `events` | list | all four | both | Any of `sale`, `stock`, `item`, `customer`. For a plugin, leaving it out lets the plugin's own `EVENTS` decide. |
| `batch_size` | integer | `100` | webhook | Events per request. |
| `include_raw` | boolean | `false` | both | Adds the record exactly as Edge sent it, under `data.raw`, to `item` and `customer` events. |
| `module` | string | required for plugin | plugin | Absolute path of the plugin file. |
| `delay_seconds` | number | the plugin's `DELAY_SECONDS`, else `0` | plugin | Pause after each event. |

Any other keys you add to a plugin destination are passed to the plugin untouched in `dest`.

### The four events

| Event | One per | `key` | When it is queued |
|---|---|---|---|
| `sale` | sale or layaway | sale number | A sold or layaway piece appears or changes. A `status` of `cancelled` is sent if every piece leaves the sale. |
| `stock` | SKU | the SKU | A piece with that SKU changes. Carries how many are available, in total and per store. |
| `item` | changed piece | item key | A piece is new or any field changed. |
| `customer` | changed customer | customer key | A customer is new or any field changed. |

A newer event for the same thing replaces one that has not gone out yet. Events carry the values as Edge sent them. Cleaning (silver) applies to the API, not to events. Gold rules apply to the API only.

The first full import queues an event for every piece and customer. Use `events` to limit what a new destination receives, and run `python3 edt_bridge.py run --dry-run` first to see the queue.

### Webhook payload

The bridge POSTs JSON with `Content-Type: application/json` and `User-Agent: edt-bridge/0.1.0`. A bearer token goes in `Authorization`.

```json
{
  "source": "edt-bridge",
  "events": [
    {
      "id": 2,
      "event": "sale",
      "key": "001-S5000",
      "data": {
        "sale_key": "001-S5000",
        "status": "completed",
        "type": "sale",
        "date": "2026-11-05T10:00:00-05:00",
        "total": 450.0,
        "tax": 58.5,
        "currency": "CAD",
        "store_id": 1,
        "orig_sale_key": null,
        "customer": {
          "key": "001-1001",
          "first_name": "MARY",
          "last_name": "mcdonald",
          "email": " Mary.McDonald@Example.COM ",
          "phone": "(416) 555-0100"
        },
        "items": [
          {
            "item_key": "001-I003",
            "sku": "RG-1",
            "name": "Example Ring",
            "price": 450.0,
            "serial_number": null,
            "category": "Rings",
            "vendor": null
          }
        ]
      }
    },
    {
      "id": 4,
      "event": "stock",
      "key": "EX-100",
      "data": {"sku": "EX-100", "available": 2, "by_store": {"1": 1, "2": 1}}
    }
  ]
}
```

- `id` is the bridge's own queue number. Use it to spot repeats.
- `total` is the sum of piece prices before tax. `tax` is separate.
- `type` is `layaway` if any piece is on layaway.
- `item` events carry `item_key`, `state`, `status`, `sku`, `store_id`, `stock_no`, `name`, `style`, `category`, `vendor`, `vendor_style_code`, `barcode`, `serial_number`, `retail_price`, `current_price`, `special_price`, `metal_type`, `metal_color`, `size`, `length`, `image`, `location`, `sale_key` and `date_entered`.
- `customer` events carry `customer_key`, `first_name`, `last_name`, `company`, `emails`, `phones`, `addresses`, `inactive`, `store_id` and `date_entered`.
- `state` is one of `available`, `sold`, `layaway`, `removed` or `excluded`.

### What counts as delivered

| Your endpoint answers | The bridge does |
|---|---|
| 2xx | Marks the events sent. |
| 4xx except 408 and 429 | Marks them failed now. Retrying will not help. |
| 408, 429, 5xx, a redirect, a timeout or no answer | Retries after 1, 5, 30, 120, 360, 720 and 1440 minutes, then marks them failed. |

The bridge does not follow redirects, so a token is never sent to a place you did not name. Failed events stay in the database. `python3 edt_bridge.py retry` queues them again.

### Plugins

Use a plugin when a system needs its own format, signing or API calls. A plugin is a Python file with a `send` function.

```python
def send(dest, event, bridge):
    ...
    return status, text
```

| Name | Meaning |
|---|---|
| `dest` | The destination's config block, as a dict. |
| `event` | `{"id": 3, "event": "sale", "key": "001-S5000", "data": {...}}`. `data` is the same object a webhook sends. |
| `bridge.http_post(url, body, headers)` | POSTs a JSON string. Returns `(status, text)`. Status `0` means no answer. Does not follow redirects. |
| `bridge.secret(dest, "token")` | Returns `dest["token"]`, or the variable named in `dest["token_env"]`. |
| `bridge.log` | A standard `logging` logger. |
| `bridge.VERSION` | The bridge version string. |
| `EVENTS` (optional, module level) | A list such as `["sale"]`. Used when the destination has no `events` key. |
| `DELAY_SECONDS` (optional, module level) | Pause after each event, in seconds. |

Return an HTTP-style status. The rules in the table above apply: 2xx is sent, other 4xx is failed, anything else is retried. If `send` raises an error, the bridge logs it and treats it as status `0`, so it retries. Events go out one at a time. After a `0`, `429` or 5xx the bridge stops for that destination and tries again next pass.

A complete minimal plugin. It sends each sale to a CRM as one request:

```python
"""Send each sale to a CRM as one JSON request."""
import json

EVENTS = ["sale"]       # only sales come here
DELAY_SECONDS = 0.5     # pause between events


def send(dest, event, bridge):
    token = bridge.secret(dest, "token")
    headers = {"Authorization": "Bearer " + token} if token else {}
    body = json.dumps({"id": event["id"], "type": event["event"], "sale": event["data"]})
    status, text = bridge.http_post(dest["url"], body, headers)
    if not 200 <= status < 300:
        bridge.log.warning("crm refused event %s: %s %s", event["id"], status, text)
    return status, text
```

Its destination block:

```json
{"name": "crm", "type": "plugin", "module": "/srv/edt/plugins/crm.py",
 "url": "https://crm.example.com/api/sales", "token_env": "CRM_TOKEN"}
```

Rules for plugin files:

- The file runs with the bridge's rights. It must belong to the user that runs the bridge and must not be writable by group or others. Otherwise the bridge refuses to load it.
- Keep it outside the code folder, next to your config. Never commit it.
- The bridge loads a plugin once. Restart `watch` after you edit it.
- Test it with fake events before you point it at a live system.

## Gold rules

Gold rules are your own corrections, applied on top of silver when an API request asks for `layer=gold` (or `api.default_layer` is `gold`). They do not change the database, events or the web interface. Edit them on the Settings page or in `gold_rules`.

A rule reads: "For `inventory`, when `vendor` is `Omgea`, set `vendor` to `Omega`."

| Key | Required | Meaning |
|---|---|---|
| `on` | yes | The kind of record: `inventory`, `customers` or `sales`. |
| `field` | yes | The field to test. See the table below. |
| `op` | yes | How to test it. |
| `value` | unless `op` is `is_empty` or `is_not_empty` | What to compare with. Up to 120 characters. |
| `action` | yes | `set` or `exclude`. |
| `target` | for `set` | The field to change. Defaults to `field`. |
| `to` | for `set` | The new value. Required, up to 120 characters. |

Fields by record kind:

| `on` | Fields you can test (`field`) | Fields you can change (`target`) |
|---|---|---|
| `inventory` | `sku`, `name`, `vendor`, `category`, `style`, `vendor_style_code`, `location`, `store_id`, `serial_number`, `state` | `name`, `vendor`, `category`, `style`, `location`, `store_id` |
| `customers` | `first_name`, `last_name`, `company`, `email`, `store_id` | `first_name`, `last_name`, `company` |
| `sales` | `type`, `status`, `store_id` | none (only `exclude`) |

Operators:

| `op` | True when the field |
|---|---|
| `is` | equals the value |
| `is_not` | does not equal the value |
| `contains` | contains the value |
| `starts_with` | starts with the value |
| `ends_with` | ends with the value |
| `is_empty` | is blank |
| `is_not_empty` | is not blank |

How rules run:

- Rules run in the order listed, top to bottom, for the record kind they name.
- Matching ignores capitals and spaces at the ends. `Omgea` matches `omgea`.
- A `set` rule changes the field, and later rules see the new value.
- An `exclude` rule removes the record from the response and ends the rules for it.
- `email` tests every email address on the customer.
- A record that a `set` rule changed lists the rule numbers (counting from 1) in `gold_rules`.
- Inventory `set` rules also correct the pieces listed inside a sale and the SKU summaries. A piece that is excluded does not count towards a SKU's quantity.
- A rule that is not complete, names a field it may not use or changes a field it may not change is refused. The Settings page shows which rule and why.

Example:

```json
"gold_rules": [
  {"on": "inventory", "field": "vendor", "op": "is", "value": "Omgea",
   "action": "set", "target": "vendor", "to": "Omega"},
  {"on": "inventory", "field": "category", "op": "is", "value": "Repairs",
   "action": "exclude"},
  {"on": "customers", "field": "email", "op": "ends_with", "value": "@staff.example.com",
   "action": "exclude"}
]
```

With these rules, `layer=gold` returns `Omega` for pieces Edge calls `Omgea`. It leaves out repair jobs and customers with a staff address. Silver and bronze still show everything.

## Encrypting the database

Off by default. Turn it on and the whole database file is encrypted with AES-256 (SQLCipher). Nothing else changes: the interface, the API, the views and the commands work the same.

1. Install the one optional package for the user that runs the bridge: `pip install sqlcipher3-binary`.
2. Make a key: `python3 -c "import secrets; print(secrets.token_hex(32))"`.
3. Put it in the service's environment file, readable by root only: `EDT_DB_KEY=<the 64 characters>`.
4. Set `"db_key_env": "EDT_DB_KEY"` in the config.
5. If a database already exists, stop the services and run `edt_bridge.py encrypt` once. A new install needs nothing more.
6. Start the services.

What to know:

- The key is never read from the config file, so it never sits next to the database.
- With `db_key_env` set, the bridge refuses to start if the key or the package is missing. It never falls back to an unencrypted database.
- Lose the key and the data cannot be read. Keep a copy in your password manager. Edge can send a full file again, but API keys and history would be gone.
- The key is on the same server as the database. Encryption protects a copied database file, a backup and a disk snapshot. It does not protect a server someone has already taken over.
- Processed Edge files in `completed/` and `failed/` are not encrypted. Set `retention_days` to `0` so they are deleted as soon as they are read.
- A backup made with the steps in [operations](operations.md) must use the same package and key, or copy the encrypted `edt.db` file itself while the services are stopped.
