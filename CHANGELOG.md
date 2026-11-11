# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional database encryption. Set `db_key_env` and the database file is AES-256 encrypted with SQLCipher (`pip install sqlcipher3-binary`, the one optional package). `encrypt` converts an existing database. The bridge refuses to start unencrypted when a key is configured.

### Security

- A file the bridge cannot read (wrong shape, text where a number belongs, nesting too deep) is moved to `failed/` and nothing from it is kept. Before, one such file stopped every file after it on every pass.
- Links in the inbox are never followed. A linked `*ItemList.json` or `*CustomerList.json` is removed unread, and `completed/` or `failed/` are replaced if they are links. Before, the account that uploads could point `completed/` at another folder and have old `.json` files there deleted.
- A destination that hangs up while sending an error no longer stops the pass.

## [0.1.0] - 2026-11-10

First release.

### Added

- One-file, standard-library-only program for Python 3.9 and newer.
- Intake: watches a folder for the `*ItemList.json` and `*CustomerList.json` files that Edge Data Transfer uploads, full or incremental. It waits for a file to settle, skips content it has already seen and moves bad files to `failed/`.
- A SQLite database of pieces, customers and sales, with a change history, and ready-made views (`pieces`, `skus`, `unreferenced`, `sale_lines`, `customer_list`).
- SKU grouping from a configurable list of Edge fields, with a `fields` command to choose one and automatic regrouping when the list changes.
- Four event types (`sale`, `stock`, `item`, `customer`) delivered to destinations with retries and backoff.
- Destinations: a webhook, or a plugin file with its own `send` function.
- A web interface for inventory, sales, customers and settings.
- A Settings page for time zone, currency, SKU fields, retention, phone country code, API defaults and gold rules. Folders, destinations, secrets and Cloudflare settings stay server-only.
- A read-only data API under `/v1`: inventory by SKU or by piece, sales, customers, feed issues, `ping`, and a `GET /v1` description of itself.
- Three data layers: bronze (what Edge sent), silver (cleaned and checked, the default) and gold (silver plus your own rules).
- `updated_since` and cursor paging for keeping another system in step.
- Security: loopback-only listeners, Cloudflare Access token verified by the bridge, hashed API keys with scopes, optional expiry and revocation, rate limits, lockout after repeated bad keys, GET only, a read-only database connection and an audit log.
- Privacy: customer social security numbers are dropped on read. Config, database and logs are created readable only by their owner. Processed files are deleted after `retention_days`.
- Other files Edge sends, such as item photos, are removed from the inbox once they pass the retention period.
- Commands: `init`, `run`, `watch`, `status`, `retry`, `fields`, `show`, `serve`, `api` and `apikey`.
- 129 tests, a secret scanner and a CI workflow that runs both on Python 3.9 and 3.12.
- Documentation: setup, configuration, API, operations, security and contributing guides, with example systemd, sshd and Cloudflare Tunnel files.
