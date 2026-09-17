# plane-app

The web front end for plane timetabling: a login-gated 3D view of the timetable, staff/roster
intake (CSV/XLSX/PDF), a chat panel backed by an LLM provider of your choice, and a Settings page
for configuring the engine connection and LLM provider. It talks to the `plane-engine` service
over HTTP for all algorithm work (build, solve, check, score, query) — it never imports the
timetabling engine directly.

## Build and Solve

**Build** places a draft instantly with the engine's greedy pass. **Solve** runs the engine's
constraint solver as a job with a preset — *keep it close* (stay near the live timetable),
*balanced*, or *best quality* — and a time limit (10 to 900 s). The progress card shows elapsed
time and the best objective so far, and can stop early keeping the best result. When the job
finishes the draft is promoted and the Quality panel scores six rules (spread, stability,
compact, even days, edge, venue) before and after. Solve time is metered against your licence
key's monthly budget; Build is not. Timetable exports (aSc) are imported with student groups and
bands derived from the lessons, so student-side clashes are checked as well as teachers and rooms.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | Yes in production (`APP_ENV=production`) | Password for the single admin login. Defaults to `admin` outside production. |
| `SECRET_KEY` | Recommended | Signs the session cookie. If unset, a key is generated once and persisted under `DATA_DIR/secret_key`. |
| `ENGINE_URL` | Defaults to `http://localhost:8000`; set it in production | Base URL of the `plane-engine` service, e.g. the engine's Railway URL. The Engine URL on the Settings page, when filled in, takes precedence. |
| `ENGINE_KEY` | No | Engine API key, if the engine requires one. Can also be set (and overridden) from the Settings page — a licence key entered there is stored per-organisation and takes precedence over this variable. |
| `DATA_DIR` | No | Where the app stores its SQLite database and generated secret key. Defaults to `./data`. In a container this should be a mounted volume. |
| `APP_ENV` | No | Set to `production` to make `ADMIN_PASSWORD` mandatory. Proxy headers (`X-Forwarded-Proto` for the Secure cookie flag, `X-Forwarded-For` for login throttling) are always honoured. |

The engine licence/API key can also be entered on the Settings page in the browser; that value is
stored in the app's database and takes precedence over `ENGINE_KEY`.

## Trusted configuration

The engine URL and the provider base URL entered in Settings are trusted as given: the app sends
the licence key to whatever engine URL is configured and the provider API key to whatever base
URL is configured. Only enter URLs you control or that the SaaS operator gave you.

## Data volume

The app persists its SQLite database (and, if `SECRET_KEY` is not set, a generated secret) under
`DATA_DIR`. When running in a container, mount a persistent volume at `/data` (the image sets
`DATA_DIR=/data`) so this state survives restarts and deploys.

## Running locally

Start the engine first, then the app, pointing the app at the engine:

```bash
DATA_DIR=/tmp/plane-engine-dev uvicorn plane_engine.main:app --port 8000
```

```bash
DATA_DIR=/tmp/plane-app-dev ENGINE_URL=http://localhost:8000 ENGINE_KEY=<key> ADMIN_PASSWORD=dev uvicorn plane_app.main:app --port 8080
```

Then open `http://localhost:8080` and log in with the `ADMIN_PASSWORD` you set.

## Template export

`scripts/export-template.sh <target-dir>` produces a standalone copy of this app (plus the
minimal `plane_timetabling.model` module it depends on) suitable for publishing as a public
template repository. It does not include any of the timetabling engine's algorithm code
(`solid.py`, `checks.py`, `build.py`, `solve.py`, `score.py`, `search.py`, `store.py`, `loadrest.py`, or `agents/`) — the
exported template talks to a separately deployed engine over HTTP, exactly as this app does.
