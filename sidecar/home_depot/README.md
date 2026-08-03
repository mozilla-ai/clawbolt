# Home Depot sidecar

Clawbolt's Home Depot product search and store lookup, backed by a real browser.

## Why this exists

Home Depot has no public API, and every product route (`/s/`, `/b/`, `/p/`, and
the GraphQL gateway) sits behind their bot manager. Things that do **not** work,
all measured rather than assumed:

- `httpx` or `requests`: rejected on the TLS handshake.
- `curl_cffi` with Chrome TLS impersonation: product routes return `403` or a
  `206` wrapping `{"GenericError": null}`. The store locator served this client
  for a while and then stopped, answering `206` while the browser kept getting
  `200` from the same address. Do not assume a working endpoint stays working.
- Stock Playwright Chromium, headless or headful: same `403`. The CDP
  `Runtime.enable` leak gives it away.
- Exporting a trusted browser's cookies into `curl_cffi`: still `206`. The
  request has to come from the browser itself, so cookies alone are not enough.

What works is a browser with no automation tells, which is what this runs:

- **patchright** instead of playwright, which patches the `Runtime.enable` leak.
- **A non-root user**, so Chromium's real sandbox is on and `--no-sandbox` (a
  strong bot signal) is not needed.
- **A persistent profile**, so the session looks like a returning visitor.

None of this is IP-related. It was all measured from a residential connection.

## Running it

Requires Python 3.11+ and a Linux box with a display or Xvfb.

```bash
cd sidecar/home_depot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
patchright install chromium

# Chromium's sandbox refuses to run as root; use an unprivileged user.
export HD_SIDECAR_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
xvfb-run -a python -m uvicorn sidecar:app --host 0.0.0.0 --port 8899
```

On a headless arm64 box, Playwright has no official build. Force the Ubuntu
24.04 arm64 one:

```bash
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-arm64 patchright install chromium
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-arm64 patchright install-deps chromium
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-arm64 \
  PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
  xvfb-run -a python -m uvicorn sidecar:app --host 0.0.0.0 --port 8899
```

Startup warms the browser on the homepage, which takes a few seconds. After
that a search costs roughly a second.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `HD_SIDECAR_TOKEN` | | Shared bearer token. Empty disables auth, which is only safe on loopback. |
| `HD_PROFILE_DIR` | `~/.hd-sidecar-profile` | Persistent browser profile. Keep it between runs. |
| `HD_WARM_SECONDS` | `7` | Homepage settle time before serving. |

## Running it in a container

```bash
docker build -t hd-sidecar sidecar/home_depot
docker run --rm -p 8899:8899 -v hd-profile:/data -e HD_SIDECAR_TOKEN=secret hd-sidecar
```

Mount something at `/data`. The browser profile lives there, and without a mount
it is rebuilt on every restart, which makes each boot look like a first-time
visitor to Home Depot. There is no `VOLUME` instruction in the Dockerfile
because Railway's builder rejects it, so persistence is always the deployer's
call.

The container starts as root purely to take ownership of that mount, then drops
to an unprivileged user before starting Chromium. Chromium running as root
disables its own sandbox, which is one of the signals this whole approach
depends on avoiding, so this is not optional. It does mean the host has to allow
unprivileged user namespaces; where it does not, Chromium refuses to start
rather than quietly downgrading.

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

- Add a volume with mount path `/data`.
- Set `HD_SIDECAR_TOKEN`, and set `PORT=8899` so the listening port is
  predictable. Railway injects its own `PORT`, which the entrypoint honours, so
  without pinning it the internal URL below will not match.
- Give it ~1GB of memory and point the healthcheck at `/health`.
- Do not assign a public domain. Reach it over the private network instead:
  `http://<service>.railway.internal:8899`.

Confirm `/search` returns products before wiring the app to it: `/health` going
green only means the port is up and the browser launched, not that Home Depot is
answering.

## API

`GET /health` binds and answers immediately, before the browser is ready, and
reports what state it is in:

```json
{"ok": true,  "state": "ready",    "error": null}
{"ok": false, "state": "starting", "error": null}
{"ok": false, "state": "failed",   "error": "Error: Failed to move to new namespace..."}
```

`ok` round-trips an expression through the page, so a crashed browser reports
`false` rather than staying green. `state` and `error` exist because the browser
takes 15 to 25 seconds to come up and can fail outright: warming it inside the
ASGI lifespan would leave the port closed for that whole window, which a
platform healthcheck reads as a dead container and which gives a remote operator
no HTTP response to diagnose. `/search` and `/stores` return `503` with the same
reason until the browser is ready.

`GET /search?q=<keyword>&zip=<zip>&store_id=<id>&limit=<n>`

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
