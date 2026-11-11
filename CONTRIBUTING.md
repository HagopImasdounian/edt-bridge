# Contributing

Thank you for helping. This project aims to stay small, plain and safe to run for a jeweller's own data.

## Ground rules

- **One file.** The program is `edt_bridge.py`. Do not split it into a package.
- **Standard library only.** No new dependencies, not even for tests or tools.
- **Python 3.9 or newer.** CI runs on 3.9 and 3.12. Do not use newer syntax.
- **Keep the docs true.** If you change behaviour, update the notes at the top of `edt_bridge.py`, the files in `docs/` and `CHANGELOG.md` in the same change.
- **Plain words.** Short comments that say why. Short messages a shop owner can read.

## Run the checks

Run both from the repository root before you open a pull request.

```bash
python3 -m unittest discover -s tests
python3 tools/secret_scan.py .
```

The tests build their own fake Edge files in a temporary folder and need nothing installed. The Cloudflare Access tests use `openssl` if it is there and skip otherwise. The secret scan must print `secret_scan: clean`. CI runs both on every push.

Add a test for every change in behaviour. Tests that cover a security rule matter most.

## Try it by hand

The [quick start](README.md#quick-start-5-minutes-on-a-laptop) runs the whole thing on a laptop in a few minutes. Keep your test config and data outside the repository.

## Never commit real data

This is a public repository. Never commit any of these.

- Real Edge files, including anything that matches `*ItemList.json` or `*CustomerList.json`
- A real `config.json`, a database, a log or an audit log
- A key, token, password, certificate or tunnel credential
- Real customer, store, company or host names, or real addresses and phone numbers

Use fake data only:

| Kind | Use |
|---|---|
| Email | `name@example.com` |
| Domain | `example.com`, `edt.example.com`, `edt-api.example.com` |
| Phone | `416-555-0100` to `416-555-0199` |
| Server address | `203.0.113.10` |
| Store | `Example Jewellers` |
| Names | Obviously made up |

The `.gitignore` and `tools/secret_scan.py` refuse many of these. They are a safety net. Check your own diff before you push. If you commit a secret by mistake, tell the maintainers at once through the Security tab, and treat the secret as stolen. Removing it in a later commit does not remove it from history.

## Pull requests

1. Make one change per pull request, with a short description of what and why.
2. Include the tests and doc updates described above.
3. Make sure both checks pass.
4. Be ready to explain how the change keeps customer data private.

Security problems do not go in issues or pull requests. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your work is released under the MIT license in [LICENSE](LICENSE).
