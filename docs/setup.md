# Production setup

By the end of this page you will have Edge Bridge running on a small Linux server. Edge uploads its files over SFTP. The bridge keeps a database, serves a web interface for your staff and a read-only API for your other systems. Nothing on the server listens to the internet except SSH.

Want to try it first? Use the [quick start](../README.md#quick-start-5-minutes-on-a-laptop).

## What you will build

```
Edge (EDT) --SFTP--> edge-upload account, locked in /srv/edt/sftp/inbox
                          |
                          v   edt-bridge-watch reads, moves and processes the files
                     /srv/edt/data/edt.db (SQLite)
                          |
        +-----------------+------------------+
        v                                    v
 edt-bridge-ui  127.0.0.1:8787        edt-bridge-api  127.0.0.1:8788
        |                                    |
 cloudflared tunnel                   cloudflared tunnel
 edt.example.com                      edt-api.example.com/v1
 Access: your staff                   Access: service tokens
```

## Before you start

You need:

- A small Linux server you can SSH into with a key and run `sudo` on. The commands below suit Debian and Ubuntu.
- Python 3.9 or newer, OpenSSH 8.7 or newer and systemd 245 or newer. Check with `python3 --version`, `ssh -V` and `systemctl --version`.
- A Cloudflare account with your domain, and Cloudflare Zero Trust turned on.
- The address Edge uploads from, if it is fixed. This page uses `203.0.113.10` as the example.
- A person at your store who can change the Edge Data Transfer settings.

The names used below are examples. Change them if you like, and change them in the config files too.

| Name | Example |
|---|---|
| Bridge user | `edt` |
| Upload user | `edge-upload` |
| Interface hostname | `edt.example.com` |
| API hostname | `edt-api.example.com` |
| Zero Trust team domain | `yourteam.cloudflareaccess.com` |

## 1. Make the users and folders

Two accounts keep the jobs apart. The bridge user runs the program. The upload user can only drop files into one folder.

1. Create the bridge user. It cannot log in.

   ```bash
   sudo useradd --system --user-group --home-dir /srv/edt --shell /usr/sbin/nologin edt
   ```

2. Create the upload user, set a long random password, and let the bridge user share its group.

   ```bash
   sudo useradd --user-group --no-create-home --home-dir /srv/edt/sftp --shell /usr/sbin/nologin edge-upload
   sudo passwd edge-upload
   sudo usermod -aG edge-upload edt
   ```

3. Create the folders with the right owners.

   ```bash
   sudo install -d -o root -g root -m 0755 /srv/edt /srv/edt/sftp /opt/edt-bridge
   sudo install -d -o edt  -g edge-upload -m 0770 /srv/edt/sftp/inbox
   sudo install -d -o edt  -g edt -m 0700 /srv/edt/data /srv/edt/config /srv/edt/plugins
   sudo install -d -o root -g root -m 0700 /etc/edt-bridge
   ```

| Folder | Owner and mode | Why |
|---|---|---|
| `/srv/edt`, `/srv/edt/sftp` | `root`, 0755 | SSH refuses a chroot unless every folder down to it is owned by root and not writable by anyone else. |
| `/srv/edt/sftp/inbox` | `edt:edge-upload`, 0770 | The upload user can add files and see the folder, which most upload tools need. The bridge user owns it, so it can read, move and delete files. |
| `/srv/edt/data` | `edt`, 0700 | The database and audit log hold customer details. Only the bridge user can open them. |
| `/srv/edt/config` | `edt`, 0700 | The config can hold tokens. The Settings page saves here. |
| `/srv/edt/plugins` | `edt`, 0700 | Plugin files, if you write any. They must belong to the bridge user. |
| `/opt/edt-bridge` | `root`, 0755 | The program. The bridge user can run it but not change it. |
| `/etc/edt-bridge` | `root`, 0700 | The secrets file for systemd. |

Check it:

```bash
ls -ld /srv/edt /srv/edt/sftp /srv/edt/sftp/inbox /srv/edt/data
id edt   # the groups list should include edge-upload
```

## 2. Install the program

1. Get the file and put it where the bridge user can run it but not edit it.

   ```bash
   git clone https://github.com/HagopImasdounian/edt-bridge.git /tmp/edt-bridge
   sudo install -o root -g root -m 0644 /tmp/edt-bridge/edt_bridge.py /opt/edt-bridge/edt_bridge.py
   ```

2. Add a shortcut to your shell. It runs any bridge command as the bridge user with the right config. The rest of the docs use it.

   ```bash
   edt() { sudo -u edt env EDT_BRIDGE_CONFIG=/srv/edt/config/config.json python3 /opt/edt-bridge/edt_bridge.py "$@"; }
   ```

   Add that line to `~/.bashrc` to keep it. Always run commands as `edt`. If you run them as root, the database becomes root-owned and the services stop working.

Check it: `edt --help` prints the command list.

## 3. Write the config

1. Make the starter config as the bridge user.

   ```bash
   sudo -u edt python3 /opt/edt-bridge/edt_bridge.py init --config /srv/edt/config/config.json
   ```

2. Edit it: `sudo -u edt nano /srv/edt/config/config.json`. Set at least these keys.

   | Key | Set it to |
   |---|---|
   | `inbox` | `/srv/edt/sftp/inbox` |
   | `data_dir` | `/srv/edt/data` |
   | `timezone` | Your store's zone, like `America/Toronto` |
   | `currency` | Your currency, like `CAD` |
   | `destinations` | `[]` for now. Add yours in step 8. |

   Every key is explained in [configuration.md](configuration.md).

3. If you use a secret, such as a webhook token or an interface password, put it in the systemd secrets file, not the config. The config names the variable.

   ```bash
   sudo install -m 0600 -o root -g root /dev/null /etc/edt-bridge/env
   sudo nano /etc/edt-bridge/env
   ```

   One `NAME=value` per line, for example `EDT_UI_PASSWORD=` followed by a long random password. In the config, `"password_env": "EDT_UI_PASSWORD"` points at it.

Check it: `edt status` prints `Records: 0 items, 0 customers, 0 sales`. It also creates the database.

## 4. Get Edge's files in with SFTP

The upload account can use a password, so it is locked down hard. It sees one folder and can run nothing else. Every other account uses SSH keys only.

1. Copy [examples/sshd/edge-upload.conf](../examples/sshd/edge-upload.conf) to the server as `/etc/ssh/sshd_config.d/10-edge-upload.conf`.

   ```bash
   sudo install -m 0644 -o root -g root examples/sshd/edge-upload.conf /etc/ssh/sshd_config.d/10-edge-upload.conf
   ```

2. Edit it. Replace `admin` with your own login and `203.0.113.10` with the address Edge uploads from. If you do not know the address, ask the Edge administrator. A wrong `AllowUsers` line locks you out.

3. Make sure your main `sshd_config` has an `Include /etc/ssh/sshd_config.d/*.conf` line near the top. Debian and Ubuntu do. If yours does not, add it above all other settings.

4. Test the syntax. Keep your current SSH session open while you do.

   ```bash
   sudo sshd -t
   sudo systemctl reload ssh      # the unit is called sshd on some systems
   ```

5. Open a second SSH session as your admin login to prove you are still in.

What each setting protects against:

| Setting | Protects against |
|---|---|
| `PasswordAuthentication no` for everyone else | Password guessing against real accounts |
| `PermitRootLogin no` | A direct attack on root |
| `AllowUsers ... edge-upload@203.0.113.10` | The upload account being used from anywhere but Edge |
| `ChrootDirectory /srv/edt/sftp` | The upload account reading the rest of the server |
| `ForceCommand internal-sftp -d /inbox -u 027` | A shell. The account can only do SFTP and starts in `/inbox`. Files it creates are group-readable by the bridge user only. |
| `PermitTTY no`, `AllowTcpForwarding no` and the other forwarding lines | Using the account as a tunnel into your network |

Also close every inbound port except SSH on your firewall. The tunnel in step 7 only makes outbound connections. If Edge has a fixed address, allow port 22 from that address and your own only.

Check it:

1. See what sshd will apply to the upload account.

   ```bash
   sudo sshd -T -C user=edge-upload,host=edge.example.com,addr=203.0.113.10 | grep -E '^(chrootdirectory|forcecommand|passwordauthentication)'
   ```

   Expect `chrootdirectory /srv/edt/sftp`, `forcecommand internal-sftp -d /inbox -u 027` and `passwordauthentication yes`.

2. Test with a plain file from a machine at the allowed address.

   ```bash
   echo hello > hello.txt
   sftp edge-upload@server.example.com      # then at the sftp> prompt: put hello.txt
   ```

   The `put` works. `ls` inside sftp is refused, which is intended. A normal `ssh edge-upload@server.example.com` is refused too.

3. On the server, check what landed.

   ```bash
   sudo ls -l /srv/edt/sftp/inbox
   sudo -u edt head -c 20 /srv/edt/sftp/inbox/hello.txt
   ```

   The file should be `-rw-r-----` owned by `edge-upload`, group `edge-upload`, and the bridge user can read it. The bridge deletes a file like this, which is not an Edge export, after the retention period (at least one day).

The upload account can see file names in the inbox, but cannot leave the folder. If your upload tool only ever adds files and never lists the folder, you can tighten it with `sudo chmod 0730 /srv/edt/sftp/inbox`.

## 5. Run the three services

The bridge has three jobs. Each runs as its own service. Each service is hardened.

| Service | Command | Writes to |
|---|---|---|
| `edt-bridge-watch` | `watch` | the database and the inbox |
| `edt-bridge-ui` | `serve` | the database and the config folder (the Settings page saves the config) |
| `edt-bridge-api` | `api` | the database and the audit log. The config is read-only to it. |

What the unit files switch on:

| Setting | Protects against |
|---|---|
| `NoNewPrivileges=yes` | A bug gaining more rights than the service starts with |
| `ProtectSystem=strict` and `ReadWritePaths=` | Writing anywhere except the folders listed |
| `ProtectHome=yes`, `PrivateTmp=yes`, `PrivateDevices=yes` | Reading home folders, shared temp files and devices |
| `UMask=0077` | Files the service creates being readable by other users |
| `RestrictAddressFamilies`, `RestrictNamespaces`, `RestrictSUIDSGID`, `LockPersonality` | Unusual network types and privilege tricks |
| `CapabilityBoundingSet=` (empty) | Using any Linux capability |
| `SystemCallFilter=@system-service` | Calls a normal program never needs |

1. Copy the three files from [examples/systemd/](../examples/systemd/) and load them.

   ```bash
   sudo install -m 0644 examples/systemd/edt-bridge-watch.service examples/systemd/edt-bridge-ui.service examples/systemd/edt-bridge-api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

2. Start the watcher and the interface. The API waits for step 7, because it will not start without Cloudflare Access.

   ```bash
   sudo systemctl enable --now edt-bridge-watch edt-bridge-ui
   ```

Check it:

```bash
systemctl is-active edt-bridge-watch edt-bridge-ui     # active, active
journalctl -u edt-bridge-watch -n 20 --no-pager        # no tracebacks
curl -s http://127.0.0.1:8787/api/summary              # JSON with "skus": 0
# If you set an interface password, add:  -u :YOUR_PASSWORD
systemd-analyze verify /etc/systemd/system/edt-bridge-*.service
systemd-analyze security edt-bridge-watch              # shows what is still open
```

## 6. Point Edge at the server

Ask the Edge administrator to set up the transfer. Send them these details through a safe channel, not plain email.

| Detail | Value |
|---|---|
| Protocol | SFTP, port 22 |
| Host | Your server's address, for example `server.example.com` |
| User | `edge-upload` |
| Password | The one you set in step 1 |
| Folder | `/inbox` |
| Files | The item list and customer list exports |

Then ask them to run a one-time full send of the items and the customers. After that, EDT sends changes on its own schedule. The bridge reads files named `*ItemList.json` and `*CustomerList.json`, full or incremental.

Decide about destinations first. A destination only receives changes that happen after you add it. To give a new webhook the whole history, add it in step 8 before the full send. To skip the history, add it after. A first full send queues an event for every piece and customer.

Check it:

```bash
journalctl -u edt-bridge-watch -f      # wait for lines like "20261110ItemList.json: 4210 records, 4210 changed"
edt status                             # item and customer counts match what you expect
edt fields                             # shows which Edge field would make the best SKU
sudo ls /srv/edt/sftp/inbox/completed  # the processed files
```

Files are read once they are 30 seconds old (`settle_seconds`). A big file may take a minute or two.

Then choose the SKU field. `edt fields` shows, for each candidate, how many pieces have it filled and how many pieces share a value. A good one is filled on nearly every piece and groups identical pieces. If the first choice is wrong, change `sku_fields` on the Settings page, or in the config. The bridge regroups on its next pass. See [operations.md](operations.md#wrong-sku-grouping).

## 7. Put Cloudflare in front

The interface and the API both listen on this machine only. A Cloudflare Tunnel carries traffic in. Cloudflare Access decides who may come. The bridge checks Access's signed token itself, so a request that skips Access is refused.

The interface (for people) and the API (for other systems) get separate Access applications, so they have separate rules and separate `aud` tags.

### 7.1 Install the tunnel

1. Install `cloudflared` from Cloudflare's instructions for your system.

2. Log in and create the tunnel. Run these as your admin user.

   ```bash
   cloudflared tunnel login
   cloudflared tunnel create edt-bridge
   cloudflared tunnel route dns edt-bridge edt.example.com
   cloudflared tunnel route dns edt-bridge edt-api.example.com
   ```

   `create` prints a tunnel id and writes `<id>.json` to `~/.cloudflared/`. Keep that file secret.

3. Install the config and the credentials.

   ```bash
   sudo install -d -m 0755 /etc/cloudflared
   sudo install -m 0600 ~/.cloudflared/TUNNEL-UUID.json /etc/cloudflared/TUNNEL-UUID.json
   sudo install -m 0644 examples/cloudflared/config.yml /etc/cloudflared/config.yml
   sudo nano /etc/cloudflared/config.yml      # replace TUNNEL-UUID twice, and the hostnames
   ```

4. Run it as a service.

   ```bash
   sudo cloudflared --config /etc/cloudflared/config.yml service install
   sudo systemctl enable --now cloudflared
   ```

The example [config.yml](../examples/cloudflared/config.yml) sends only paths under `/v1` on the API hostname to the API. Do not set `httpHostHeader`. The interface refuses a Settings save when the page's origin and the Host header differ.

### 7.2 Protect the interface

1. In the Cloudflare Zero Trust dashboard, add a self-hosted Access application for `edt.example.com`.
2. Add one policy with the action Allow, and include only your staff (their email addresses, or your company's email domain).
3. Open the application and copy its Application Audience (AUD) tag. It is 64 characters of 0 to 9 and a to f.
4. Put it and your team domain in the config.

   ```json
   "ui": {
     "host": "127.0.0.1",
     "port": 8787,
     "password_env": "EDT_UI_PASSWORD",
     "cloudflare_access": {
       "team_domain": "yourteam.cloudflareaccess.com",
       "aud": "<the 64-character tag for the interface application>"
     }
   }
   ```

5. Restart it: `sudo systemctl restart edt-bridge-ui`.

`ui.password_env` adds a password on top of Access. It is optional. If the variable is not in `/etc/edt-bridge/env`, there is no password.

### 7.3 Protect the API

1. In Zero Trust, create a service token for each system that will call the API. Copy its client id and secret at once. The secret is shown once.
2. Add a second self-hosted Access application. Give it the destination `edt-api.example.com` with the path `/v1`.
3. Add one policy with the action Service Auth, including only your service tokens. Do not add any Allow policy for people.
4. Copy this application's AUD tag. It differs from the interface's.
5. Put it in the config.

   ```json
   "api": {
     "port": 8788,
     "default_layer": "silver",
     "requests_per_minute": 120,
     "max_limit": 500,
     "cloudflare_access": {
       "team_domain": "yourteam.cloudflareaccess.com",
       "aud": "<the 64-character tag for the API application>"
     }
   }
   ```

6. Start the API: `sudo systemctl enable --now edt-bridge-api`.

Cloudflare's dashboard menus change from time to time. Look for self-hosted applications, policies and service tokens under Access.

Check it:

```bash
# People: a browser at https://edt.example.com shows the Cloudflare login, then the interface.

# No credentials at all: Cloudflare stops the request. The reply is not the bridge's JSON.
curl -i https://edt-api.example.com/v1/ping

# Service token but no API key: the bridge answers 401.
curl -s -H "CF-Access-Client-Id: $CF_CLIENT_ID" -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET" \
  https://edt-api.example.com/v1/ping
# {"error": "a valid API key is needed"}

# On the server, skipping Cloudflare: the bridge refuses.
curl -s -H "Authorization: Bearer $EDT_KEY" http://127.0.0.1:8788/v1/ping
# {"error": "forbidden"}
```

The last test is the point. Even someone on the server cannot call the API without a token that Access signed.

## 8. Create API keys and a destination

### API keys

Make one key per caller, with the fewest scopes it needs. Give `bronze` only if it must read raw Edge records.

```bash
edt apikey create --name website --scopes inventory --expires-days 365
edt apikey list
```

The key is shown once. Store it in the caller's secret store. Then check it:

```bash
curl -s -H "CF-Access-Client-Id: $CF_CLIENT_ID" -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET" \
  -H "Authorization: Bearer $EDT_KEY" https://edt-api.example.com/v1/ping
# {"ok": true, "scopes": ["inventory"], "last_file": "..."}
```

Revoking and rotating keys are covered in [operations.md](operations.md#api-keys).

### A destination (optional)

Add a webhook or plugin under `destinations` in the config, with its secret in `/etc/edt-bridge/env`. See [configuration.md](configuration.md#destinations). Then restart the watcher: `sudo systemctl restart edt-bridge-watch`. Check it with `edt run --dry-run` before and `edt status` after. Sent events show as `sent`.

## 9. Final check

| Check | Command | Expected |
|---|---|---|
| Services run | `systemctl is-active edt-bridge-watch edt-bridge-ui edt-bridge-api cloudflared` | Four lines of `active` |
| Only SSH listens publicly | `sudo ss -tlnp` | Ports 8787 and 8788 on `127.0.0.1` only |
| Files flow | `edt status` | Recent files listed, no failed deliveries |
| Data folder is private | `sudo ls -ld /srv/edt/data` | `drwx------ edt edt` |
| Keys work, scopes are tight | `edt apikey list` | One key per caller, scopes as intended |
| Access is enforced | The three `curl` tests in step 7 | 302 or 403 from Cloudflare, 401, then `forbidden` |

Next, read [operations.md](operations.md) for backups, upgrades and what to do when something breaks.
