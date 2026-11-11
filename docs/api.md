# Data API

By the end of this page you can read your inventory, sales and customers from another system, keep a copy in step and handle every error the API returns.

The API is read-only JSON over HTTP GET. All examples use `https://edt-api.example.com`. Replace it with your own hostname. To set the API up, see [setup.md](setup.md).

## Contents

- [Authentication](#authentication)
- [Endpoints](#endpoints)
- [Lists, paging and syncing](#lists-paging-and-syncing)
- [The three layers](#the-three-layers)
- [Scopes](#scopes)
- [Errors and status codes](#errors-and-status-codes)
- [Limits](#limits)
- [Examples](#examples)

## Authentication

Every request needs two things.

| What | Header | Checked by |
|---|---|---|
| A Cloudflare Access service token | `CF-Access-Client-Id` and `CF-Access-Client-Secret` | Cloudflare, then the bridge verifies the token Cloudflare signs |
| An API key | `Authorization: Bearer edt_<id>_<secret>` | The bridge |

Without a valid Access token the bridge answers `403`. Without a valid key it answers `401`. Keys are made on the server with `apikey create`. See [operations.md](operations.md#api-keys).

## Endpoints

All paths start with `/v1`. Every endpoint takes `layer=bronze|silver|gold`. Unknown parameters are refused with `400`.

| Endpoint | Scope | Returns |
|---|---|---|
| `GET /v1` | any | A description of the API: endpoints, parameters, layers and your scopes |
| `GET /v1/ping` | any | `{"ok": true, "scopes": [...], "last_file": "<time>"}` |
| `GET /v1/inventory` | `inventory` | One row per SKU |
| `GET /v1/inventory?group=piece` | `inventory` | One row per physical piece |
| `GET /v1/inventory/{sku}` | `inventory` | One SKU with every piece under it |
| `GET /v1/sales` | `sales` | Sales and layaways with their pieces |
| `GET /v1/sales/{sale_key}` | `sales` | One sale |
| `GET /v1/customers` | `customers` | Customers |
| `GET /v1/customers/{customer_key}` | `customers` | One customer, with their sales if the key also holds `sales` |
| `GET /v1/issues` | any | Problems found in the feed, with counts |

`last_file` in `ping` is when the bridge last processed an Edge file. Use it to check the feed is alive.

### Parameters

All list endpoints share `limit`, `cursor`, `updated_since` and `layer`. The rest are listed per endpoint.

| Parameter | Applies to | Values and meaning |
|---|---|---|
| `layer` | everything | `bronze`, `silver` or `gold`. Default is `api.default_layer`, normally `silver`. |
| `limit` | lists | 1 up to the server's `max_limit` (500 unless changed, never above 900). Default 100, or `max_limit` if smaller. |
| `cursor` | lists | The `next_cursor` from the previous page. |
| `updated_since` | lists | An ISO 8601 time, such as `2026-11-10T14:00:00Z`. A plain date also works. A time with no zone is read as UTC. Matches records stored at or after it. |
| `group` | `inventory` | `sku` (default) or `piece`. With `layer=bronze` the default is `piece`, and `sku` is refused. |
| `state` | `inventory` as SKUs | `available` (default) or `all`. |
| `state` | `inventory?group=piece` | `available` (default), `sold`, `layaway`, `removed`, `excluded` or `all`. |
| `sku` | `inventory`, `sales` | Exact SKU. On sales, only sales that include it. |
| `store` | `inventory` | Exact store id. With SKU rows, the quantity counts that store only. |
| `q` | `inventory` | Contains-text search. SKU rows search `sku`, `name`, `vendor`. Piece rows add `serial_number`. |
| `serial` | `inventory?group=piece` | Exact match on the serial number. Capitals and surrounding spaces are ignored. Use `q` to match part of one. |
| `item_key` | `inventory?group=piece` | Exact item key. |
| `since`, `until` | `sales` | Sale date range, `YYYY-MM-DD`, both ends included, in the store's time zone. |
| `status` | `sales` | `completed` or `cancelled`. |
| `type` | `sales` | `sale` or `layaway`. |
| `customer_key` | `sales` | Exact customer key. |
| `q` | `customers` | Contains-text search over customer key, first name, last name, company, first email and first phone. |
| `email` | `customers` | Exact email address, any case, matched against every address on the customer. |
| `type` | `issues` | `inventory`, `sales` or `customers`, limited to the scopes your key holds. |
| `code` | `issues` | One issue code, such as `no_sku`. |
| `limit` | `issues` | As above. `issues` has no paging. |

A parameter may appear once. Values are at most 200 characters, and a request has at most 20 parameters.

### Response shapes

A list:

```json
{"data": [ ... ], "next_cursor": "001-I004", "layer": "silver"}
```

One record: `{"data": {...}, "layer": "silver"}`. Issues: `{"total": 2, "counts": {"no_sku": 1}, "data": [...], "layer": "silver"}`.

#### SKU row

```json
{
  "sku": "EX-100",
  "name": "Example Watch",
  "vendor": "Omgea",
  "category": "Watches",
  "quantity": 2,
  "by_store": {"1": 1, "2": 1},
  "serial_numbers": ["AB123", "AB124"],
  "sold": 0,
  "layaway": 0,
  "price_from": 1000.0,
  "price_to": 1100.0,
  "updated_at": "2026-11-10T22:52:02+00:00"
}
```

- `quantity` and `by_store` count available pieces only. `sold` and `layaway` count pieces in that state.
- `price_from` and `price_to` cover available pieces. A piece's current price is used, or its retail price if there is none.
- `name`, `vendor` and `category` come from the first piece that has them, with spacing tidied.
- `/v1/inventory/{sku}` adds `pieces`, with every piece under the SKU in any state.

#### Piece row (silver and gold)

```json
{
  "item_key": "001-I001", "sku": "EX-100", "serial_number": "AB123",
  "state": "available", "store_id": "1", "location": "Case 1",
  "name": "Example Watch", "vendor": "Omgea", "category": "Watches",
  "vendor_style_code": "EX-100", "style": null,
  "retail_price": 1200.0, "current_price": 1000.0,
  "sale_key": null, "sale_date": null, "sale_price": null,
  "updated_at": "2026-11-10T22:52:02+00:00",
  "issues": []
}
```

`state` is `available`, `sold`, `layaway`, `removed` or `excluded`. `store_id` is text on pieces and a number on sales and customers. A key without the `sales` scope does not get `sale_key`, `sale_date` or `sale_price`.

#### Sale

```json
{
  "sale_key": "001-S5000", "status": "completed", "type": "sale",
  "date": "2026-11-05T10:00:00-05:00", "total": 450.0, "tax": 58.5,
  "currency": "CAD", "store_id": 1, "orig_sale_key": null,
  "customer": {"key": "001-1001", "first_name": "Mary", "last_name": "McDonald",
               "email": "mary.mcdonald@example.com", "phone": "+14165550100"},
  "items": [{"item_key": "001-I003", "sku": "RG-1", "name": "Example Ring",
             "price": 450.0, "serial_number": null, "category": "Rings", "vendor": null}],
  "updated_at": "2026-11-10T22:52:02+00:00",
  "issues": []
}
```

`total` is before tax. `type` is `layaway` if any piece is on layaway. `status` is `cancelled` when every piece has left the sale.

#### Customer

```json
{
  "customer_key": "001-1001", "first_name": "Mary", "last_name": "McDonald", "company": null,
  "emails": ["mary.mcdonald@example.com"], "phones": ["+14165550100"],
  "addresses": [{"type": null, "street": "1 Example St", "street2": null, "city": "Toronto",
                 "state": "ON", "zip": "M5R 2E3", "country": "CA"}],
  "inactive": false, "store_id": 1, "date_entered": null,
  "issues": [], "updated_at": "2026-11-10T22:52:02+00:00"
}
```

#### Issue codes

| Type | Code | Meaning |
|---|---|---|
| inventory | `no_sku` | The piece has no reference, so it cannot be grouped |
| inventory | `no_price` | In stock with no price |
| inventory | `sold_without_sale` | Marked sold or on layaway with no sale number |
| inventory | `duplicate_serial` | The serial number is on more than one piece in stock |
| sales | `no_customer` | No customer on the sale |
| sales | `customer_not_in_feed` | The sale names a customer Edge has not sent |
| sales | `no_date` | No sale date |
| sales | `zero_total` | A completed sale adds up to zero |
| sales | `item_without_sku` | A piece in the sale has no reference |
| customers | `invalid_email` | An address is not usable. It is left out of `emails`. |
| customers | `invalid_phone` | A number is not usable. It is left out of `phones`. |
| customers | `no_name` | No name or company |
| customers | `no_contact` | No usable email or phone |
| customers | `duplicate_email` | The address is on more than one customer |

## Lists, paging and syncing

### Paging

1. Call the list with a `limit`.
2. If `next_cursor` is not `null`, call again with `cursor=<next_cursor>` and the same other parameters.
3. Stop when `next_cursor` is `null`.

Lists are ordered by key (SKU, item key, sale key or customer key). A gold rule can remove rows, so a page may hold fewer rows than `limit` while `next_cursor` is still set. Keep going until it is `null`.

### Keeping another system in step

1. Note the time before you start, in UTC.
2. Call each list you need with `state=all` (on inventory) and `updated_since=<time of your last sync>`. Follow every page.
3. Save each record under its key. A record you already hold is replaced.
4. Next run, use the time you noted in step 1. Subtract a minute for safety.

Notes:

- `updated_at` is when the bridge stored the change, in UTC. It is not Edge's own time.
- `updated_since` includes records stored exactly at that time, so expect the odd repeat. Saving by key handles it.
- Use `state=all`. Without it, a SKU that just sold out disappears from the list instead of showing `quantity: 0`.
- Pieces are not deleted. A piece Edge removes shows `state: "removed"` with `group=piece&state=all`. A cancelled sale shows `status: "cancelled"`.

## The three layers

Add `layer=bronze|silver|gold` to any request.

| Layer | What you get | Needs |
|---|---|---|
| bronze | Exactly what Edge sent, every field, one raw record per piece or customer | The `bronze` scope on top of the data scope |
| silver | Cleaned and checked, with an `issues` list on every record | The data scope |
| gold | Silver plus your own rules from [gold rules](configuration.md#gold-rules) | The data scope |

With no rules, gold matches silver. Silver is the default unless the server sets `api.default_layer`.

### What bronze returns

- Pieces: `{"item_key", "updated_at", "raw"}`, where `raw` is the Edge record.
- Customers: `{"customer_key", "updated_at", "raw"}`.
- Sales: the sale as the bridge built it, plus `raw_items`, the Edge record of each piece, in the same order as `items`.

A raw piece carries its sale price, sale number and customer number. A key with `bronze` can see all of that, whatever other scopes it has. Customer social security numbers are removed when a file is read and are not in the raw record. Nothing else is removed. Give `bronze` only to a key that must have it.

### What silver does

| Field | Before (as Edge sent it) | After (silver) |
|---|---|---|
| First name | `MARY` | `Mary` |
| Last name | `mcdonald` | `McDonald` |
| Email | ` Mary.McDonald@Example.COM ` | `mary.mcdonald@example.com` |
| Phone | `(416) 555-0100` | `+14165550100` |
| Street | ` 1  Example St ` | `1 Example St` |
| State | `on` | `ON` |
| Postal code | `m5r2e3` | `M5R 2E3` |
| Piece name | `  Example   Watch ` | `Example Watch` |
| Serial number | `ab123` | `AB123` |
| Retail price | `"1,200.00"` | `1200.0` |

The same customer, first as bronze:

```json
{
  "customer_key": "001-1001",
  "updated_at": "2026-11-10T22:52:02+00:00",
  "raw": {
    "CustomerKey": "001-1001",
    "CustomerFirstName": "MARY",
    "CustomerLastName": "mcdonald",
    "Emails": [{"Key": "e", "PairValue": {"EmailEmail": " Mary.McDonald@Example.COM "}}],
    "Phones": [{"Key": "p", "PairValue": {"PhonePhone": "(416) 555-0100"}}],
    "Addresses": [{"Key": "a", "PairValue": {"AddressStreet": " 1  Example St ",
      "AddressCity": "Toronto", "AddressState": "on", "AddressZip": "m5r2e3",
      "AddressCountry": "CA"}}]
  }
}
```

Then as silver:

```json
{
  "customer_key": "001-1001",
  "first_name": "Mary",
  "last_name": "McDonald",
  "emails": ["mary.mcdonald@example.com"],
  "phones": ["+14165550100"],
  "addresses": [{"street": "1 Example St", "city": "Toronto", "state": "ON",
                 "zip": "M5R 2E3", "country": "CA"}],
  "issues": []
}
```

Other silver rules:

- Spacing is collapsed and trimmed. Names typed all in capitals or all in lower case are capitalised.
- An invalid email or phone is dropped from its list and reported in `issues`.
- Phones follow `phone_country_code`. The default `1` accepts North American numbers and rejects ones that cannot be real.
- Prices become numbers. Dates on sales and pieces carry the store's offset, from `timezone`.

## Scopes

A key holds one or more scopes. They limit what it can read, on top of Cloudflare Access.

| Scope | Lets the key |
|---|---|
| `inventory` | Read `/v1/inventory`. See inventory issues. |
| `sales` | Read `/v1/sales`. See `sale_key`, `sale_date` and `sale_price` on pieces. See a customer's sales. |
| `customers` | Read `/v1/customers`. See customer name, email and phone inside sales. |
| `bronze` | Ask for `layer=bronze`, together with the scope for the data it wants. Reads raw Edge records. |

Without `customers`, a sale's `customer` is only `{"key": "..."}`. Without `bronze`, any request for `layer=bronze` returns `403`, even `/v1/ping`. Bronze is the widest key you can issue: raw pieces carry sale and customer fields even for a key without `sales` or `customers`.

## Errors and status codes

Every error is JSON: `{"error": "message"}`.

| Status | Message (shortened) | Meaning |
|---|---|---|
| 200 | | Success |
| 400 | `unknown parameter; allowed: ...`, `limit must be between 1 and 500`, `layer must be one of: ...`, `<name> was given twice` | A bad parameter. The message names it. |
| 401 | `a valid API key is needed` | Key missing, wrong, revoked or expired. Comes with `WWW-Authenticate: Bearer`. |
| 403 | `forbidden` | No valid Cloudflare Access token. |
| 403 | `this key does not have the <scope> scope` | The key lacks a scope for this request. |
| 404 | `not found` | No such record, or a path that is not `/v1/...`. |
| 405 | `this API is read-only: GET only` | Any method except GET. |
| 414 | `address too long` | A request line over 2000 characters. |
| 429 | `rate limit reached` | The key used its requests for this minute. Wait `Retry-After` seconds. |
| 429 | `too many bad keys, try later` | The caller sent 10 bad keys in 5 minutes. Wait `Retry-After` seconds. |
| 500 | `the request could not be answered` | A fault inside the bridge. Details are in the service log. |
| 503 | `busy, try again` | 16 requests are already in flight. Retry in about 2 seconds. |

Cloudflare can also answer before the bridge does, with its own page or status. A body that is not the JSON above came from Cloudflare.

## Limits

| Limit | Value | Set by |
|---|---|---|
| Requests per key | 120 per minute | `api.requests_per_minute` |
| Largest page | 500 rows | `api.max_limit`, never above 900 |
| Bad keys per caller | 10 in 5 minutes, then locked out | Fixed |
| Requests in flight | 16 | Fixed |
| Time the bridge waits on a slow caller | 15 seconds | Fixed |

The lockout counts the caller's address as Cloudflare reports it. It blocks every key from that address until the 5 minutes pass. A good key does not clear it.

Every request is written to `api_audit.log` in the data folder: time, caller, key id and name, path, parameter names, row count and status. Search text is not logged. See [operations.md](operations.md#logs-and-the-audit-log).

## Examples

Set up once. These examples use bash.

```bash
export API=https://edt-api.example.com/v1
export CF_CLIENT_ID=your-service-token-id.access
export CF_CLIENT_SECRET=your-service-token-secret
export EDT_KEY=edt_xxxxxxxxxxxx_your-api-key
AUTH=(-H "CF-Access-Client-Id: $CF_CLIENT_ID" \
      -H "CF-Access-Client-Secret: $CF_CLIENT_SECRET" \
      -H "Authorization: Bearer $EDT_KEY")
```

Check the key and the feed:

```bash
curl -s "${AUTH[@]}" "$API/ping"
# {"ok": true, "scopes": ["customers", "inventory", "sales"], "last_file": "2026-11-10T22:52:02+00:00"}
```

Read the API's own description. An agent with only a key can start here:

```bash
curl -s "${AUTH[@]}" "$API"
```

Pieces in stock at store 1 that match "watch":

```bash
curl -s "${AUTH[@]}" "$API/inventory?q=watch&store=1&limit=50"
```

One SKU with its pieces:

```bash
curl -s "${AUTH[@]}" "$API/inventory/EX-100"
```

Every piece, sold or not, whose serial number contains `AB123` in any case:

```bash
curl -s "${AUTH[@]}" "$API/inventory?group=piece&state=all&q=AB123"
```

Sales in November that are layaways:

```bash
curl -s "${AUTH[@]}" "$API/sales?since=2026-11-01&until=2026-11-30&type=layaway"
```

A customer by email, then all their sales:

```bash
curl -s "${AUTH[@]}" "$API/customers?email=mary.mcdonald@example.com"
curl -s "${AUTH[@]}" "$API/sales?customer_key=001-1001"
```

The gold layer, with your corrections:

```bash
curl -s "${AUTH[@]}" "$API/inventory?layer=gold&group=piece"
```

Raw Edge records. This needs a key with `bronze`:

```bash
curl -s "${AUTH[@]}" "$API/inventory?layer=bronze&limit=2"
```

Page through every customer. This uses `jq`, which is optional:

```bash
cursor=""
while :; do
  page=$(curl -s "${AUTH[@]}" "$API/customers?limit=200${cursor:+&cursor=$cursor}")
  echo "$page" | jq -c '.data[]'
  cursor=$(echo "$page" | jq -r '.next_cursor // empty')
  [ -z "$cursor" ] && break
done
```

Everything that changed since your last sync. The time may end in `Z` or carry an offset such as `-05:00`.

```bash
SINCE=2026-11-10T14:00:00Z
curl -s "${AUTH[@]}" "$API/inventory?state=all&updated_since=$SINCE"
curl -s "${AUTH[@]}" "$API/sales?updated_since=$SINCE"
curl -s "${AUTH[@]}" "$API/customers?updated_since=$SINCE"
```

The problems in the feed, counted by kind:

```bash
curl -s "${AUTH[@]}" "$API/issues?type=customers&code=invalid_phone"
```

What a missing scope looks like:

```bash
curl -s -w "\n%{http_code}\n" "${AUTH[@]}" "$API/customers"
# {"error": "this key does not have the customers scope"}
# 403
```
