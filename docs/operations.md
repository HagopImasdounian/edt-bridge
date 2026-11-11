# Operations

By the end of this page you can check that the bridge is healthy, fix stuck deliveries, back it up, upgrade it, manage API keys and diagnose the common faults.

The examples assume the [production setup](setup.md), including the `edt` shortcut:

```bash
edt() { sudo -u edt env EDT_BRIDGE_CONFIG=/srv/edt/config/config.json python3 /opt/edt-bridge/edt_bridge.py "$@"; }
```

Run bridge commands as the bridge user, through `edt`. If you run them as root, the database becomes root-owned and the services cannot open it. Fix that with `sudo chown -R edt:edt /srv/edt/data`.

## Daily checks

| Check | How | Healthy |
|---|---|---|
| Services are up | `systemctl is-active edt-bridge-watch edt-bridge-ui edt-bridge-api cloudflared` | `active` for each |
| Files are arriving | `edt status`, look at "Last files" | A recent time |
| Nothing is stuck | `edt status`, look at "Deliveries" and "Last failures" | No `failed` rows |
| The API sees fresh data | `GET /v1/ping`, look at `last_file` | A recent time |
| The bridge is alive | Set `heartbeat_url` to a monitoring service | It is called after each pass with no failed deliveries |

The web interface home also shows the last file and the number of failed deliveries.

Delivery states in `status`:

| State | Meaning |
|---|---|
| `pending` | Queued, or waiting to retry after a failure |
| `sent` | The destination answered 2xx |
| `failed` | Given up. Either a permanent 4xx answer, or retries ran out. |

## Commands

Run `edt --help` for the list. Add `--config PATH` to any command to use another config.

| Command | What it does |
|---|---|
| `init` | Writes a starter config. Refuses to overwrite one. |
| `run` | One pass: read new files, then deliver. Exits 1 if a delivery failed. Use it from cron if you do not use `watch`. |
| `run --dry-run` | Shows the events that would be queued. Changes nothing. |
| `watch` | Runs a pass every `poll_seconds`, forever. Re-reads the config each pass. |
| `status` | Counts, deliveries by destination and state, last five files, last five failures. |
| `retry` | Puts every `failed` delivery back in the queue with its attempts reset. |
| `fields` | Shows how well each Edge field would work as the SKU. |
| `show X` | Looks up `X` as an item key, a serial number or a SKU. |
| `serve` | The web interface. |
| `api` | The data API. `--local` skips Cloudflare Access, for testing only. |
| `apikey create`, `list`, `revoke ID` | Manage API keys. |

Only one of `run` and `watch` can work at a time. A second one stops with `another edt-bridge run is in progress`.

### retry

Fix the cause first. Read the reason in `edt status` under "Last failures". Then run `edt retry`. The watcher delivers them on its next pass.

### fields

Run it after the first full import and whenever grouping looks wrong. For each candidate field it prints:

| Column | Meaning |
|---|---|
| `filled` | Share of pieces that have a value |
| `values` | How many different values there are |
| `pieces per value` | Average group size |
| `largest group` | Size of the biggest group |

A good SKU field is filled on nearly every piece and groups identical pieces, with more than one piece per value. A field with exactly one piece per value is a serial number, not a SKU.

### show

`edt show 001-I001` prints one piece: its fields, its history (every change to price, status, store, location and SKU, with times), and how many pieces share its SKU. `edt show EX-100` prints a SKU and every piece under it. Serial numbers must match exactly, including capitals, as Edge sent them.

## Querying the database

The database is yours to query. Open it read-only so you cannot change it by accident.

```bash
sudo -u edt sqlite3 -readonly /srv/edt/data/edt.db "SELECT * FROM skus LIMIT 5"
```

| View | One row per |
|---|---|
| `pieces` | Physical piece: serial number, status, price, sale |
| `skus` | SKU: pieces, available, sold, layaway, price range |
| `unreferenced` | Piece with no SKU, so it cannot be grouped |
| `sale_lines` | Piece sold or on layaway |
| `customer_list` | Customer, with number of sales and total spent |
| `changes` (a table) | Change to a piece, with the time |

The tables underneath are `items`, `customers`, `sales`, `outbox` (deliveries), `files`, `meta` and `api_keys`. Do not write to the database while the services run.

## Logs and the audit log

| What | Where |
|---|---|
| Watcher | `journalctl -u edt-bridge-watch`. One line per file, and per delivery batch. |
| Interface | `journalctl -u edt-bridge-ui`. Startup, errors and settings changes (field names only). It keeps no access log, because searches can hold names. |
| API errors | `journalctl -u edt-bridge-api` |
| API requests | `/srv/edt/data/api_audit.log`, the audit log |
| SSH and uploads | `journalctl -u ssh`, or `/var/log/auth.log` |

The audit log has one JSON line per API request, including refused ones:

```json
{"at": "2026-11-10T22:52:17+00:00", "ip": "198.51.100.7", "who": "service-token-id.access", "key": "a1b2c3d4e5f6 website", "path": "/v1/inventory", "params": ["limit", "state"], "rows": 100, "status": 200}
```

| Field | Meaning |
|---|---|
| `at` | When, in UTC |
| `ip` | The caller's address as Cloudflare reports it. `local` in test mode. |
| `who` | The Access identity: the service token or the person's email |
| `key` | The key id and name. Never the key itself. |
| `path`, `params` | What was asked. Parameter names only, never their values, so search text is not kept. |
| `rows` | How many rows came back |
| `status` | The HTTP status |

The file is readable by the bridge user only. It rotates at 5 MB and keeps five old files (`api_audit.log.1` to `.5`). To see who used a key: `sudo -u edt grep a1b2c3d4e5f6 /srv/edt/data/api_audit.log`.

## Retention: what is deleted and when

| What | Deleted when |
|---|---|
| Processed Edge files in `completed/` | Older than `retention_days` |
| Files that were not valid JSON, in `failed/` | Older than `retention_days` |
| Any file as soon as it is processed | When `retention_days` is `0`. Files already in `failed/` are then kept until you delete them. |
| Other files in the inbox, such as item photos or test uploads | Older than `retention_days`, or one day if that is smaller |
| Records in the database (pieces, customers, sales, history, deliveries) | Never |
| `api_audit.log` | Rotated at 5 MB, five old files kept |
| Social security numbers | Never stored. Dropped as a file is read. |

Points to know:

- Ages count from the file's time on disk, which is when it was uploaded.
- The database keeps what it has seen. If Edge deletes a customer, the bridge keeps its last copy. There is no command to erase one customer. Remove the record from Edge first, then remove the row from the database with SQL while the services are stopped.
- The processed files and the database both hold customer names, emails and phones. Keep `retention_days` as low as your needs allow.

## Backups

| What | Back it up? | Why |
|---|---|---|
| `/srv/edt/data/edt.db` | Yes | All records, API key hashes and delivery history |
| `/srv/edt/config/config.json` | Yes | Your settings and gold rules |
| `/etc/edt-bridge/env` | Yes, stored securely | The secrets |
| `/srv/edt/plugins/` | Yes, if you use plugins | Your sender code |
| `/etc/cloudflared/` and your Access settings | Yes, or write them down | Rebuilding the tunnel and Access without them is slow |
| `/etc/ssh/sshd_config.d/10-edge-upload.conf` and the unit files | Yes, or keep them in your own config management | Rebuilding the server |
| `api_audit.log*` | Optional | Your record of API use |
| The inbox and `completed/` | No | Temporary. Edge can send a full file again. |

Do not copy `edt.db` while a service is writing to it. Make a consistent copy with SQLite's backup. This needs only Python:

```bash
sudo install -d -o edt -g edt -m 0700 /srv/edt/backup
sudo -u edt python3 - <<'PY'
import sqlite3
src = sqlite3.connect("file:/srv/edt/data/edt.db?mode=ro", uri=True)
dst = sqlite3.connect("/srv/edt/backup/edt-backup.db")
src.backup(dst)
dst.close(); src.close()
PY
```

Then copy `/srv/edt/backup/edt-backup.db` off the server, encrypted. It holds customer details.

To restore:

1. Stop the services: `sudo systemctl stop edt-bridge-watch edt-bridge-ui edt-bridge-api`.
2. Copy the backup to `/srv/edt/data/edt.db`.
3. Set the owner and mode: `sudo chown edt:edt /srv/edt/data/edt.db && sudo chmod 0600 /srv/edt/data/edt.db`.
4. Start the services and run `edt status`.

If you lose the database and have no backup, a fresh full send from Edge rebuilds pieces, customers and sales. You lose the API keys and the delivery history. Create new keys.

## Upgrading

1. Read [CHANGELOG.md](../CHANGELOG.md) for the new version.
2. Make a backup, as above.
3. Get the new code and run its tests.

   ```bash
   git -C /tmp/edt-bridge pull
   python3 -m unittest discover -s /tmp/edt-bridge/tests
   ```

4. Install the file and restart the services.

   ```bash
   sudo install -o root -g root -m 0644 /tmp/edt-bridge/edt_bridge.py /opt/edt-bridge/edt_bridge.py
   sudo systemctl restart edt-bridge-watch edt-bridge-ui edt-bridge-api
   ```

5. Check `edt status`, `journalctl -u edt-bridge-watch -n 20` and `GET /v1`, which shows the running version.

On start, the bridge creates any missing tables and rebuilds the database views if their version changed. Your data is not rewritten. Plugins load again on restart.

To roll back, check out the earlier version, install that file again and restart. Restore the backup too if the changelog says the database changed.

## API keys

A key is `edt_` plus a 12-character id and a secret. Only a hash of it is stored, so a lost key cannot be recovered. Make a new one.

| Task | Command |
|---|---|
| Create | `edt apikey create --name website --scopes inventory,sales --expires-days 365` |
| List | `edt apikey list` (id, name, scopes, state) |
| Revoke | `edt apikey revoke ID` |

- `--scopes` takes `inventory`, `sales`, `customers` and `bronze`. Scopes cannot be edited later. Make a new key instead.
- `--expires-days` makes the key stop working after that many days. Without it, the key never expires. Set it.
- The key is printed once. Put it straight into the caller's secret store.
- Revoking takes effect on the next request. An expired key shows as `expired` in the list.

### Rotating a key

1. Create a new key with the same scopes.
2. Give it to the caller and switch the caller over.
3. Check the audit log. The old key's id should stop appearing.
4. Revoke the old key.

### If a key may have leaked

1. Revoke it now: `edt apikey revoke ID`.
2. Read what it did: `sudo -u edt grep ID /srv/edt/data/api_audit.log*`.
3. Create a replacement. If the key had `bronze`, treat every record it could reach as seen.
4. Also check the Cloudflare service token. Delete or replace it in Zero Trust if it may have leaked too.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **API will not start**: `The API only answers requests that came through Cloudflare Access` | `api.cloudflare_access` is not filled in | Set `team_domain` and `aud` ([setup, step 7.3](setup.md#73-protect-the-api)). To test only, run `api --local`. |
| API will not start: `api.cloudflare_access needs team_domain ... and aud` | `aud` is not 64 lowercase hex characters, or `team_domain` is not `name.cloudflareaccess.com` | Copy the tag again. No spaces, no capitals. |
| API will not start: `Could not load Cloudflare Access signing keys` | The server cannot reach `https://<team domain>/cdn-cgi/access/certs`, or the team domain is wrong | Run `curl -s https://yourteam.cloudflareaccess.com/cdn-cgi/access/certs` on the server. Fix DNS, firewall or the name. systemd retries every 10 seconds. |
| API or interface will not start: `Address already in use` | Another process has the port, often an `api --local` test left running | `sudo ss -tlnp \| grep 8788`, then stop it |
| Service fails with `unable to open database file` or `readonly database` | The data folder belongs to the wrong user, or the unit does not list it in `ReadWritePaths` | `sudo chown -R edt:edt /srv/edt/data`. Compare your unit with the example. |
| **403 from the API**, body `{"error": "forbidden"}` | No valid Access token reached the bridge: the request skipped Access, the `aud` is for the other application, the team domain is wrong, or the server clock is off | Check `api.cloudflare_access` against the API application's AUD tag. Check `timedatectl`. In `--local` mode, the request must come straight to `127.0.0.1:8788`, with no proxy headers. |
| 403, body `this key does not have the ... scope` | The key lacks that scope | Create a new key with the scope. Scopes cannot be edited. |
| 403 with an HTML page or a Cloudflare message | Cloudflare Access refused the request before the bridge | Check the service token is in the Service Auth policy, and that you send both `CF-Access-Client-Id` and `CF-Access-Client-Secret`. |
| **401**, `a valid API key is needed` | Key missing, mistyped, revoked, expired or from another install. The header must be `Authorization: Bearer edt_...` | `edt apikey list` shows the state. Create a new key if needed. Ten bad keys in a row cause a 429 lockout. |
| **429**, `rate limit reached` | The key went over `requests_per_minute` | Wait `Retry-After` seconds. Slow the caller, or raise the limit on the Settings page. |
| 429, `too many bad keys, try later` | Ten wrong keys from one address in five minutes | Wait `Retry-After` seconds. Fix the caller. The lockout covers every key from that address. |
| 503 `busy, try again` | 16 requests are in flight | Retry in a couple of seconds. Make fewer parallel calls. |
| **Files not being picked up** | The name does not end in `ItemList.json` or `CustomerList.json`, or the file is in a subfolder | The bridge reads only those names, directly in the inbox. Ask the Edge administrator to check the export folder. |
| Files not picked up | The file is younger than `settle_seconds` (30) | Wait. The log says `is still uploading, will pick it up next pass`. |
| Files not picked up | The watcher is stopped, or its `inbox` is a different folder from the SFTP one | `systemctl status edt-bridge-watch`. Check `inbox` in the config. |
| Files not picked up | The bridge user cannot read or move the file | `ls -l /srv/edt/sftp/inbox`. The group must be `edge-upload`, mode `-rw-r-----`, and `id edt` must list `edge-upload`. Restart the watcher after any group change. |
| Files not picked up | The same content was processed before | The log says `was already processed, skipping`. The file moves to `completed/`. This is normal. |
| Files not picked up | The file is not valid JSON | The log says `is not readable JSON`. It moved to `failed/`. Ask Edge to resend. |
| **Uploads refused**: the log shows `bad ownership or modes for chroot directory` | `/srv/edt` or `/srv/edt/sftp` is not owned by root, or others can write to it | `sudo chown root:root /srv/edt /srv/edt/sftp && sudo chmod 0755 /srv/edt /srv/edt/sftp` |
| Uploads refused: login fails | Wrong password, `AllowUsers` does not match Edge's address, the account is locked, or an earlier file turns passwords off for it | `sudo sshd -T -C user=edge-upload,host=x,addr=EDGE_IP \| grep -E 'passwordauthentication\|allowusers'`. Check `sudo passwd -S edge-upload`. Make sure the file is named `10-edge-upload.conf`. |
| Uploads refused: `Permission denied` on put | The inbox is not `edt:edge-upload` with mode 0770, or the disk is full | `ls -ld /srv/edt/sftp/inbox`, `df -h /srv` |
| The upload tool says it cannot list the folder | The inbox was tightened to mode 0730, which hides the listing | `sudo chmod 0770 /srv/edt/sftp/inbox` |
| **Settings will not save behind a tunnel**: 403 | The Host header was rewritten, so the page's origin and the Host differ | Remove `httpHostHeader` and any proxy rule that changes Host. Load the page from the same hostname you save on. |
| Settings will not save: 403 or a login page | The request carries no valid Access token | Check `ui.cloudflare_access` against the interface application's AUD tag. |
| Settings will not save: `could not save` | The service cannot write the config folder | The unit needs `ReadWritePaths=/srv/edt/data /srv/edt/config`. The folder must belong to `edt`. |
| Settings will not save: an error under a field | The value is not allowed | Read the message under the field. See the ranges in [configuration.md](configuration.md#what-the-settings-page-can-change). |
| Saved settings do not change the API | The API reloads the config when the file's time changes | Check `journalctl -u edt-bridge-api` for `could not reload the config`. Fix the file. |
| **Wrong SKU grouping**: identical pieces split, or different pieces merged | `sku_fields` lists the wrong field, or the right field is empty on many pieces | See [wrong SKU grouping](#wrong-sku-grouping). |
| Deliveries stay `failed` | The destination refused them, or the URL or token is wrong | Read the reason in `edt status`. Fix it. Run `edt retry`. |
| Times are wrong by some hours | `timezone` is not the store's zone | Set `timezone`. Edge sends local times with no offset. |
| Interface says `Forbidden` on the server | The Host header is not `127.0.0.1:8787` or `localhost:8787` | Use those addresses, or go through the tunnel. |

### Wrong SKU grouping

1. Run `edt fields`. Find the field that is filled on nearly every piece and has more than one piece per value.
2. Set `sku_fields` to that field first, followed by fallbacks. Use the Settings page or the config.
3. Wait for the next watcher pass, or run `edt run` if no watcher is running. The bridge regroups every piece and queues a `stock` event for each SKU that changed.
4. Check one SKU with `edt show EX-100`. Pieces with no SKU show in the `unreferenced` view and as `no_sku` issues at `/v1/issues`.

The order matters. The first field that is filled in on a piece wins. A piece missing that field falls to the next one. If two stores type the same style differently, such as `ex-100` and `EX-100`, they become two SKUs. Fix it in Edge.
