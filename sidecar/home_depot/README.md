# Retail sidecar

Clawbolt's product search at Home Depot and Lowe's, plus Home Depot store lookup,
backed by a real browser.

The directory is still named `home_depot` because a rename would change the
published image name and every deployment pointing at it. Not worth the churn for
a name.

## Why this exists

Neither retailer has a public API, and every product route sits behind Akamai
Bot Manager. Home Depot additionally runs Akamai's **advanced** tier. Things
that do **not** work, all measured rather than assumed:

- `httpx` or `requests`: rejected on the TLS handshake.
- `curl_cffi` with Chrome TLS impersonation: product routes return `403` or a
  `206` wrapping `{"GenericError": null}`.
- Stock Playwright Chromium, headless or headful: `403`. The CDP
  `Runtime.enable` leak gives it away.
- **patchright** (Chromium with the CDP leaks patched): cleared Lowe's, but Home
  Depot's advanced tier still answered `206` on every product and store call. It
  fingerprints the CDP automation protocol itself, which every Chromium driver
  speaks over, so patching the individual leaks is not enough (issue #1498). The
  behavioral sensor validated and the block held regardless.

What works is **Camoufox**, a hardened Firefox that is not driven over CDP, so
that tell is absent. Two things carry the session past the behavioral sensor:

- **A humanized warm.** Both retailers keep the session unvalidated until the
  Akamai sensor sees human-like input, so the warm-up moves the pointer and
  scrolls. Camoufox renders those moves as realistic curves; a teleported cursor
  is not enough.
- **No persistent profile.** The humanized warm validates a first-time session
  on its own, so the "returning visitor" a saved profile used to buy is no
  longer needed, and Camoufox's fingerprint is in fact weaker in persistent-context
  mode (enough that Lowe's denies it). Each launch is a fresh browser.

This is not IP-related: it reproduces from a residential connection and a
datacenter one alike, and the fix is the browser, not the egress.

## Running it

Requires Python 3.11+ and a Linux box with a display or Xvfb.

```bash
cd sidecar/home_depot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch          # downloads the patched Firefox

# Run as a normal user, not root.
export HD_SIDECAR_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
xvfb-run -a python -m uvicorn sidecar:app --host 0.0.0.0 --port 8899
```

Camoufox publishes arm64 builds, so an arm64 box needs no platform override. If
`python -m camoufox fetch` reports an unsupported OS, it still downloads a
working fallback build.

Startup warms the browser on the homepage, which takes a few seconds. After
that a search costs roughly a second.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `HD_SIDECAR_TOKEN` | | Shared bearer token. Empty disables auth, which is only safe on loopback. |
| `HD_WARM_SECONDS` | `7` | Homepage settle time before serving. |
| `HD_IDLE_SECONDS` | `3600` | Close the browser after this many idle seconds. The next request warms a fresh one. Set to `0` to keep it open. |
| `HD_REQUEST_BUDGET_SECONDS` | `25` | How long one request may drive a retailer's page once it holds that retailer's lock. Must stay below the client's 35s timeout: a request that outlives its caller holds the lock with nobody waiting for the answer. |
| `HD_HEADLESS` | | Set to `1` to run Camoufox headless (no display). For local fingerprint debugging only; leave unset in production, where the humanized warm needs a real display via Xvfb. |

## Running it in a container

```bash
docker build -t hd-sidecar sidecar/home_depot
docker run --rm -p 8899:8899 -e HD_SIDECAR_TOKEN=secret hd-sidecar
```

Nothing to mount: the sidecar keeps no state between runs. A fresh browser plus
the humanized warm looks like a first-time visitor every boot, which is fine.
The Camoufox browser is baked into the image at build time.

The container starts as root only to drop to an unprivileged user before
starting the browser. That handover has to set `HOME`: `setpriv` changes uid but
leaves the environment alone, and a `HOME` the new user cannot write breaks
Firefox startup. `XDG_CACHE_HOME` follows `HOME` for the same reason, and it is
where Camoufox looks for the browser fetched at build time, so the two have to
agree. Running as a normal user is hygiene, not a bot-detection requirement.

### Railway

Deploy from the **published image** rather than from source:

```
ghcr.io/mozilla-ai/clawbolt-hd-sidecar:latest
```

`.github/workflows/hd-sidecar-image.yml` builds and pushes that on every change
under `sidecar/home_depot/`. Deploying the image is the path of least resistance
for a reason worth knowing: if the Railway project lives in a personal account
while this repo belongs to an org, connecting the repo needs org admin to grant
the Railway GitHub App access. Without it Railway cannot fetch new commits and
keeps rebuilding the last snapshot it managed to get, which presents as fixes
that never take effect and build logs where every layer is `cached`. The image
route sidesteps that entirely. The GHCR package defaults to private, so make it
public once or give Railway a registry credential.

Then, on the service:

- Set `HD_SIDECAR_TOKEN`, and set `PORT=8899` so the listening port is
  predictable. Railway injects its own `PORT`, which the entrypoint honours, so
  without pinning it the internal URL below will not match.
- Give it ~1GB of memory and point the healthcheck at `/health`.
- Do not assign a public domain. Reach it over the private network instead:
  `http://<service>.railway.internal:8899`.

No volume is needed: the sidecar is stateless.

Confirm `/search` returns products before wiring the app to it: `/health` going
green only means the port is up and the browser launched, not that Home Depot is
answering.

## API

`GET /health` binds and answers immediately, before the browser is ready, and
reports what state it is in. After the idle interval it reports `idle`; the next
search or store lookup waits for a fresh warm-up before running:

```json
{"ok": true,  "state": "ready",    "error": null}
{"ok": false, "state": "starting", "error": null}
{"ok": false, "state": "idle",     "error": null}
{"ok": false, "state": "failed",   "error": "Error: Failed to launch: no DISPLAY..."}
```

`ok` round-trips an expression through the page, so a crashed browser reports
`false` rather than staying green. `state` and `error` exist because the browser
takes 15 to 25 seconds to come up and can fail outright: warming it inside the
ASGI lifespan would leave the port closed for that whole window, which a
platform healthcheck reads as a dead container and which gives a remote operator
no HTTP response to diagnose. `/search` and `/stores` return `503` with the same
reason until the browser is ready.

`GET /search?q=<keyword>&site=<home_depot|lowes>&zip=<zip>&store_id=<id>&limit=<n>`

`site` defaults to `home_depot`. The two retailers are reached differently, which
is invisible to callers but explains the parameter differences: Home Depot answers
a GraphQL call issued from inside the page, so `zip` and `store_id` localize the
result, while Lowe's has no reachable product API and its results are read out of
the search page's embedded state, with localization riding on the session's own
store rather than on parameters.

```console
$ curl -s 'localhost:8899/search?q=cordless+drill&zip=30301&limit=2' | jq
{
  "keyword": "cordless drill",
  "total_products": 825,
  "used_nav_param": "N-5yc1vZc27fZ1z140i3",
  "products": [
    {
      "item_id": "315994093",
      "name": "Atomic 20V Max Lithium-Ion Brushless Cordless Compact 1/2 in. Drill/Driver",
      "brand": "DEWALT",
      "price_dollars": 99.0,
      "in_stock": true,
      "rating": 4.7012,
      "product_url": "https://www.homedepot.com/p/..."
    }
  ]
}
```

`used_nav_param` is set when Home Depot maps the keyword to a category browse
page rather than a keyword result set. The sidecar detects that, pulls the bare
`N-` token out of the redirect, and retries with it. Passing the surrounding
path instead of the bare token silently returns zero results, which is the one
non-obvious thing about this API.

`GET /stores?near=<zip|city|address>&radius_miles=<n>&limit=<n>`

```console
$ curl -s 'localhost:8899/stores?near=30301&limit=2' | jq
{
  "near": "30301",
  "geocoded": false,
  "stores": [
    {"store_id": "0159", "name": "Midtown", "street": "650 Ponce De Leon",
     "city": "Atlanta", "state": "GA", "zip_code": "30308",
     "phone": "(404)892-8042", "distance_miles": 2.1}
  ]
}
```

A non-zip `near` makes Home Depot answer with geocoding candidates instead of
stores; the sidecar resolves the first candidate to coordinates and looks up
again, reporting `geocoded: true` when it did.

## Lowe's

Lowe's runs Akamai's standard tier only, so it never needed the Camoufox switch
that Home Depot's advanced tier forced; patchright cleared it. It shares the
migration anyway, because the humanized warm is what validates the session for
both. A homepage visit that never touches the mouse is refused with a 403 edge
deny at `/search`; the pointer movement in the warm flips it, and the same
navigation is served. Under Chromium this took an organic click into a `/pl/`
category first; the humanized warm replaces that step.

Results come from `window['__PRELOADED_STATE__']`, not the DOM. The payload is
~400KB and carries `itemList` with price, per-store on-hand quantity, brand,
model, rating and review count. Two reasons to prefer it over selectors: it is
stable against markup changes, and a DOM scrape picks up the "Previously Viewed"
carousel, which silently returns items from earlier queries.

Store lookup is Home Depot only. Lowe's has no equivalent implemented, and
SerpApi has no Lowe's engine at all, so there is no fallback for Lowe's search
either: if the sidecar cannot answer, the tool reports that rather than quietly
returning Home Depot prices.

## Connecting Clawbolt

Point Clawbolt at the sidecar and it becomes the preferred product-search
backend, ahead of the optional SerpApi fallback, and the only source of store
lookup:

```bash
HOME_DEPOT_SIDECAR_URL=http://localhost:8899
HOME_DEPOT_SIDECAR_TOKEN=<same as HD_SIDECAR_TOKEN>
```

If Clawbolt runs somewhere a browser cannot (a small container, a PaaS dyno),
run this on a machine that can and expose it over your own tunnel or private
network. Do not put it on the public internet without the token set.

When the sidecar is unreachable, product search falls through to SerpApi if a
key is set, so it degrades rather than breaking. Store lookup has no fallback:
SerpApi has no equivalent endpoint, so no sidecar means no store lookup.

## Maintenance

`search_model.graphql` is captured verbatim from the live site. Home Depot's
gateway rejects documents it does not recognise and there is no public schema to
validate a rewrite against, so do not hand-trim it. To refresh, capture the
`searchModel` request the search page issues and replace the file wholesale.

Expect this to need occasional attention. Bot detection changes, and when it
does the symptom is product search returning blocks while `/health` stays green.
When it comes to that, the levers are: bump the `camoufox` pin (its fingerprint
tracks Firefox releases), lengthen `HD_WARM_SECONDS` so the humanized warm feeds
the sensor more, or, if a retailer escalates its tier the way Home Depot did, a
different browser engine. Do not "fix" a `206` by accepting it: the body parses
as valid JSON, so relaxing the status check turns a visible failure into a silent
"No products found" for every search.
