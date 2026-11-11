# Edge Bridge

Get your data out of The Edge and into wherever you want it.

Edge Bridge is one Python file with no packages to install. It reads the files The Edge (the jewellery point-of-sale system) exports through Edge Data Transfer (EDT). It turns them into a SQLite database, change events for your other systems, a web interface and a read-only data API.

Not affiliated with or endorsed by the makers of The Edge.

## What it does

1. Watches a folder for the JSON files EDT uploads (`*ItemList.json`, `*CustomerList.json`, full or incremental).
2. Keeps a SQLite database of pieces, customers and sales, so it knows what changed since the last file.
3. Pushes changes to your destinations as events (`sale`, `stock`, `item`, `customer`), with retries. A destination is a webhook or a small Python plugin you write.
4. Serves a web interface for inventory, sales, customers and settings.
5. Serves a read-only data API with three layers: bronze (what Edge sent), silver (cleaned) and gold (silver plus your own rules).

## Quick start (5 minutes, on a laptop)

You need Python 3.9 or newer. Run these from the folder that holds `edt_bridge.py`. Everything goes in `~/edt-demo`, outside the code folder.

1. Make the folders and a starter config.

   ```bash
   mkdir -p ~/edt-demo/inbox ~/edt-demo/data
   export EDT_BRIDGE_CONFIG=~/edt-demo/config.json
   python3 edt_bridge.py init
   ```

2. Edit the config. This sets the two folders, removes the example webhook and lets files be picked up at once.

   ```bash
   python3 - <<'EOF'
   import json, os
   p = os.environ["EDT_BRIDGE_CONFIG"]
   home = os.path.expanduser("~/edt-demo")
   c = json.load(open(p))
   c.update(inbox=home + "/inbox", data_dir=home + "/data", destinations=[], settle_seconds=0)
   json.dump(c, open(p, "w"), indent=2)
   EOF
   ```

3. Drop a sample file in the inbox. This is fake data in the shape Edge sends.

   ```bash
   cat > ~/edt-demo/inbox/sample-CustomerList.json <<'EOF'
   {"Customers": [{"Key": "001-1001", "PairValue": {"CustomerKey": "001-1001",
     "CustomerFirstName": "MARY", "CustomerLastName": "mcdonald",
     "Emails": [{"Key": "e", "PairValue": {"EmailEmail": " Mary.McDonald@Example.COM "}}],
     "Phones": [{"Key": "p", "PairValue": {"PhonePhone": "(416) 555-0100"}}]}}]}
   EOF
   cat > ~/edt-demo/inbox/sample-ItemList.json <<'EOF'
   {"Items": [
     {"Key": "001-I001", "PairValue": {"ItemKey": "001-I001", "ItemStatus": "I", "ItemStoreId": 1,
       "ItemDesc": "Example Watch", "CategoryName": "Watches", "ItemCurrentPrice": 1000,
       "ItemVendStyleCode": "EX-100", "ItemSerialNumber": "AB123"}},
     {"Key": "001-I002", "PairValue": {"ItemKey": "001-I002", "ItemStatus": "S", "ItemStoreId": 1,
       "ItemDesc": "Example Ring", "CategoryName": "Rings", "ItemCurrentPrice": 500,
       "ItemStyle": "RG-1", "ItemSaleKey": "001-S5000", "ItemSaleDate": "2026-11-05T10:00:00",
       "ItemSalePrice": 450, "ItemSaleTax": 58.5, "ItemSaleCustKey": "001-1001"}}]}
   EOF
   ```

4. Run one pass, then look at the result.

   ```bash
   python3 edt_bridge.py run --dry-run   # shows what would happen, changes nothing
   python3 edt_bridge.py run             # processes the files
   python3 edt_bridge.py status          # records, deliveries, last files
   ```

   The log shows lines like `sample-ItemList.json: 2 records, 2 changed`. `status` reports `2 items, 1 customers, 1 sales`. The processed files move to `~/edt-demo/inbox/completed/`.

5. Open the web interface.

   ```bash
   python3 edt_bridge.py serve
   ```

   Visit http://127.0.0.1:8787. It listens on this machine only. If port 8787 is busy, change `ui.port` in the config.

6. Try the API. In a second terminal, from the same folder:

   ```bash
   export EDT_BRIDGE_CONFIG=~/edt-demo/config.json
   python3 edt_bridge.py apikey create --name demo --scopes inventory,sales,customers
   python3 edt_bridge.py api --local
   ```

   Copy the key it printed (shown once). In a third terminal:

   ```bash
   curl -H "Authorization: Bearer PASTE_THE_KEY_HERE" http://127.0.0.1:8788/v1/customers
   ```

   The customer comes back as Mary McDonald, `mary.mcdonald@example.com`, `+14165550100`. That is the silver layer at work. `api --local` is for testing only. It skips Cloudflare Access.

Ready for a real server? Follow [docs/setup.md](docs/setup.md).

## Documentation

| Document | Read it to |
|---|---|
| [docs/setup.md](docs/setup.md) | Install on a Linux server, with SFTP intake, systemd services and Cloudflare |
| [docs/configuration.md](docs/configuration.md) | Look up every config key, destinations, plugins and gold rules |
| [docs/api.md](docs/api.md) | Call the data API: endpoints, parameters, layers, scopes, errors |
| [docs/operations.md](docs/operations.md) | Run it day to day, back it up, upgrade it, fix problems |
| [SECURITY.md](SECURITY.md) | Understand the security model and report a vulnerability |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Change the code and run the tests |
| [CHANGELOG.md](CHANGELOG.md) | See what changed in each version |

Run `python3 edt_bridge.py --help` for the command list. The notes at the top of `edt_bridge.py` are a short reference.

## Your data stays yours

Every store has its own settings. Keep each store's environment outside this folder.

| What | Where it lives | In git? |
|---|---|---|
| Config (`config.json`) | wherever `--config` or `EDT_BRIDGE_CONFIG` points | never |
| Secrets (tokens, passwords) | environment variables, named in the config (`"token_env": "MY_TOKEN"`) | never |
| Database, audit log | `data_dir` in the config | never |
| Files from Edge | `inbox` in the config | never |

`config.example.json` shows the shape with no real values. The `.gitignore` refuses configs, databases, Edge files, logs and keys if they land in this folder anyway. New databases and configs are created readable only by the user that runs the bridge. Social security numbers are dropped when a file is read and are never stored.

## License

MIT. See [LICENSE](LICENSE).

Built by [H&CO](https://www.itshco.com).
