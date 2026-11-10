# Edge Bridge

Get your data out of The Edge and into wherever you want it.

The Edge (the jewellery point-of-sale system) can export your inventory and customers on a schedule through Edge Data Transfer (EDT). Edge Bridge takes those files and turns them into something other systems can use: a database you own, events pushed to your own endpoints, a web interface, and a read-only API.

One file. Python 3.9 or newer. No packages to install.

Not affiliated with or endorsed by the makers of The Edge.

## What it does

1. Watches a folder for the JSON files EDT uploads (`*ItemList.json`, `*CustomerList.json`, full or incremental).
2. Keeps a local SQLite database of pieces, customers and sales, so it knows what changed since the last file.
3. Pushes the changes to your destinations as events (`sale`, `stock`, `item`, `customer`), with retries.
4. Serves a web interface: inventory, sales, customers, settings.
5. Serves a read-only data API for other systems and agents.

## Quick start

```bash
python3 edt_bridge.py init --config /srv/edt/config.json   # writes a starter config
# edit the config: the inbox folder EDT uploads to, and where the data lives
python3 edt_bridge.py run --dry-run --config /srv/edt/config.json   # show what would happen
python3 edt_bridge.py run    --config /srv/edt/config.json          # one pass (cron)
python3 edt_bridge.py watch  --config /srv/edt/config.json          # or keep it running
python3 edt_bridge.py serve  --config /srv/edt/config.json          # web interface on 127.0.0.1:8787
python3 edt_bridge.py api    --config /srv/edt/config.json          # data API on 127.0.0.1:8788
```

Set `EDT_BRIDGE_CONFIG=/srv/edt/config.json` once and drop the `--config`.

`python3 edt_bridge.py --help` and the notes at the top of `edt_bridge.py` cover every command.

## Your data stays yours

Every store has its own settings; EDT works the same way for all of them. Keep each store's environment outside this folder:

| What | Where it lives | In git? |
|---|---|---|
| Config (`config.json`) | wherever `--config` or `EDT_BRIDGE_CONFIG` points | never |
| Secrets (tokens, passwords) | environment variables, named in the config (`"token_env": "MY_TOKEN"`) | never |
| Database, audit log | `data_dir` in the config | never |
| Files from Edge | `inbox` in the config | never |

`config.example.json` shows the shape with no real values. The `.gitignore` refuses configs, databases, Edge files, logs and keys if they land in this folder anyway. New databases and configs are created readable only by the user that runs the bridge. Social security numbers are dropped when a file is read and are never stored.

## The data API

Read-only JSON. `GET /v1` describes every endpoint and parameter, so an agent handed nothing but a key can work out how to use it.

| Endpoint | Returns |
|---|---|
| `GET /v1/inventory` | one row per SKU: quantity, serial numbers, count per store, price range |
| `GET /v1/inventory?group=piece` | one row per physical piece |
| `GET /v1/inventory/{sku}` | one SKU with every piece under it |
| `GET /v1/sales`, `/v1/sales/{key}` | sales and layaways with their pieces |
| `GET /v1/customers`, `/v1/customers/{key}` | customers |
| `GET /v1/issues` | every problem found in the feed, with counts |

Lists take `limit`, `cursor` and `updated_since`. To keep another system in step, call with `state=all&updated_since=<time of your last call>`.

### Three layers

Add `layer=bronze|silver|gold` to any request. Silver is the default.

- **Bronze**: exactly what Edge sent, every field. A raw piece carries its sale price, sale number and customer number, so a key with `bronze` sees everything in the record types it can reach.
- **Silver**: cleaned and checked. Emails lower-cased and validated, phones as `+14165550100`, names and spacing tidied, postal codes formatted, prices as numbers, and an `issues` list on every record.
- **Gold**: silver plus your own rules, edited on the Settings page. "When vendor is Omgea, change vendor to Omega." "When category is Repairs, leave it out." Each business sets its own.

### Security

Every layer has to pass, and none can be switched off in the config.

1. **This machine only.** The API and the web interface listen on 127.0.0.1. There is no setting to open them to the network.
2. **Cloudflare Tunnel and Access.** The way in from outside is a Cloudflare Tunnel with Cloudflare Access in front. Every request must carry the token Access signs, and the bridge verifies it itself (signature, audience, issuer, expiry).
3. **API keys.** `apikey create` shows a key once and stores only a hash. `apikey list`, `apikey revoke <id>`.
4. **Scopes per key**: `inventory`, `sales`, `customers`, `bronze`. Customer names, emails and phones only go to a key with `customers`. Raw Edge records only to a key with `bronze`.
5. **Limits and a record.** Rate limit per key, lockout after repeated bad keys, GET only, a read-only database connection, and an audit line per request.

Setup, once, on the machine that runs the bridge:

```bash
cloudflared tunnel create edt-bridge
cloudflared tunnel route dns edt-bridge edt-api.example.com
cloudflared tunnel run --url http://127.0.0.1:8788 edt-bridge
```

In Cloudflare Zero Trust, add a self-hosted Access application for that hostname with one policy, action "Service Auth", that includes only your service tokens. Copy its Application Audience (AUD) tag and your team domain into `api.cloudflare_access` in the config. Callers send `CF-Access-Client-Id`, `CF-Access-Client-Secret` and `Authorization: Bearer <key>`.

To try the API on the machine itself, without Cloudflare: `python3 edt_bridge.py api --local`.

## Destinations

- **webhook**: POSTs `{"events": [...]}` as JSON to any URL, with an optional bearer token.
- **plugin**: your own sender, for a system that needs its own format or request signing. Point the config at a Python file that defines `send(dest, event, bridge)` and returns `(status, text)`:

```json
{"name": "crm", "type": "plugin", "module": "/srv/edt/crm.py", "token_env": "CRM_TOKEN"}
```

The file runs with the bridge's rights, so it must belong to the user that runs the bridge and not be writable by anyone else. Keep it with your config, outside this folder.

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite builds its own fake Edge files in a temporary folder. It needs nothing installed; the Cloudflare Access tests use `openssl` if it is there.

## Settings

The Settings page in the web interface covers what differs between stores: time zone, currency, which Edge fields make the SKU, file retention, the default API layer, and the gold rules. Folders, destinations, passwords, API keys and Cloudflare Access are set on the server and cannot be changed from a browser.

MIT licensed. Built by [H&CO](https://www.itshco.com).
