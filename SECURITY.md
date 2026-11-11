# Security

Edge Bridge handles customer names, emails, phone numbers and sales. This page explains how it protects them, what is and is not in scope for a report, and how to report a vulnerability.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting.

1. Open this repository on GitHub.
2. Go to the Security tab.
3. Choose "Report a vulnerability".

Please do not open a public issue or pull request for a vulnerability. Say which version you tested, how to reproduce it and what an attacker gains. Never include real customer data, real Edge files, tokens or keys in a report. Fake data is enough.

Fixes go into the latest release. Older versions are not patched.

## The security model

The bridge assumes the server is yours, the people who use it are not all equally trusted, and the internet is hostile. Several independent layers each have to pass. None can be switched off in the config.

| Layer | What it does | What it protects against |
|---|---|---|
| Loopback only | The interface and the API listen on `127.0.0.1`. There is no setting to open them to the network. | Reaching customer data from the network without going through your tunnel |
| Cloudflare Tunnel | The only way in is an outbound tunnel. The server needs no open inbound web port. | Port scans and direct attacks on the server |
| Cloudflare Access, checked by the bridge | A request through a tunnel or proxy must carry the token Access signs. The bridge checks signature, audience, issuer and expiry itself. | A misrouted tunnel, a second hostname with no policy, or a caller on the server who skips Access |
| API keys | Shown once, stored only as a SHA-256 hash, optional expiry, revocable. | A stolen database revealing working keys |
| Scopes | `inventory`, `sales`, `customers` and `bronze`. Names, emails and phones go only to `customers`. Raw Edge records go only to `bronze`. | A key reading more than it needs |
| Limits | A rate limit per key, a lockout after 10 bad keys in 5 minutes, and at most 16 requests at once. | Key guessing and floods |
| Read-only API | GET only. The database connection is read-only. | Changing data through the API |
| Audit log | One line per API request: who, what, how many rows. Search text is never logged. | Not knowing who read what |
| Settings page limits | It changes a short list of keys. It cannot change folders, destinations, secrets or Cloudflare settings. A change needs a custom header and a matching origin. | A web page on another site changing your settings, or a browser user redirecting your data |
| Host check | Without Cloudflare Access, the interface and `api --local` accept only `127.0.0.1`, `localhost` or `::1` as the Host. | A web page on another site reaching the interface by pointing its own name at your machine |
| Private files | The database, config and audit log are created readable only by their owner. Settings are written whole, then moved into place. | Other users on the machine reading customer details |
| No social security numbers | They are dropped as a file is read. They are never stored. | Keeping data you do not need |
| Redirects refused | Webhook delivery does not follow redirects. | A bearer token being sent to a place you did not name |
| Plugin file check | A plugin loads only if the bridge's own user owns it and nobody else can write it. | Another user on the machine planting code |
| Retention | Processed files are deleted after `retention_days`. | Customer files piling up on disk |

The example SFTP and systemd files in [examples/](examples/) add:

- A password account that can do nothing but upload into one folder it cannot list.
- Key-only SSH for everyone else.
- Services that run unprivileged, cannot gain privileges and can write only to their own folders.

## What you must do

The bridge cannot do these for you.

- Keep the operating system and OpenSSH updated.
- Use disk encryption on the server, or encrypt its volume. The database is not encrypted.
- Encrypt backups. They hold customer details.
- Turn on two-step login for your Cloudflare and GitHub accounts.
- Give each API caller its own key with the fewest scopes. Set `--expires-days`.
- Treat a `bronze` key as able to read everything in the record types it can reach.
- Read any plugin before you install it. A plugin runs with the bridge's rights.
- Allow SFTP only from Edge's address if it is fixed, and use a long random password.
- Never commit your config, database, secrets or Edge files. The `.gitignore` and `tools/secret_scan.py` help, but they are a safety net, not a plan.

## In scope

Report these.

- A way to read data without a valid Access token and API key.
- A way to read data beyond a key's scopes, or to get raw records without `bronze`.
- A way to change data or settings that the design says cannot change.
- The interface or API listening on an address other than the loopback.
- SQL injection, path traversal, header injection or request smuggling in the bridge's own code.
- A cross-site request that changes settings.
- A bypass of the plugin file check.
- Secrets, search text or customer data written to logs.
- A weakness in the Access token check, such as accepting a token for another application or one that has expired.
- A request that crashes or hangs a service from a single caller.

## Out of scope

- Attacks that need root, or the bridge user, on the server.
- A plugin or config you installed that does harm.
- Settings you chose that weaken the design, such as putting your own proxy in front that opens the loopback ports, or giving every key `bronze`.
- Weaknesses in Cloudflare, OpenSSH, Python, SQLite or The Edge. Report those to their makers.
- Heavy traffic floods. Use Cloudflare's protections for that.
- Social engineering, phishing, or theft of a key from a caller's own system.
- `api --local`. It is for testing on one machine and says so when it starts.
- Findings that apply only to versions that are not the latest.

## Known limits

These are not vulnerabilities. They are the design as it stands.

- Inside the server, traffic between the tunnel and the bridge is plain HTTP on the loopback. The tunnel encrypts the rest of the way.
- Anyone Cloudflare Access lets into the interface sees all customer data. The interface has no per-person roles. `ui.password_env` is one shared password.
- Rate-limit and lockout counters are kept in memory. They reset when the service restarts.
- The audit log is a file the bridge user can write. It is a record, not tamper-proof evidence.
- The interface page asks the viewer's browser to load fonts from Google Fonts. Google sees the viewer's address. No customer data is sent.
