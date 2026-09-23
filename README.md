# plane-app

The web front end for plane timetabling: a login-gated timetable grid (with a 3D model as the
expert view), staff/roster
intake (CSV/XLSX/PDF), a chat panel backed by an LLM provider of your choice, and a Settings page
for configuring the engine connection and LLM provider. It talks to the `plane-engine` service
over HTTP for all algorithm work (build, solve, check, score, query) — it never imports the
timetabling engine directly.

## Getting started

A new timetable opens on the **Start wizard**: say what you are timetabling — a ward, a clinic, a
school, an office, a sports centre — and the chat proposes a few configurations, one question at a
time, with the trade-offs beside it. Choose one and it hands you three things: a **workbook** with
the sheets your kind of organisation needs and a few example rows to replace, a **sample PDF** of
what the printed timetable will look like, and a one-page **guide** explaining every column in your
own words. Fill the workbook in and drop it back into the chat.

From there it is the plan loop: the app reads the workbook into the Plan tab, lists anything that
does not add up in the Issues panel, and — once nothing is blocking — **Generate** turns the plan
into a draft timetable, **Quick timetable** places it, **Best timetable** improves it, and
**Print** hands it out. Re-drop a corrected workbook at any time; the plan merges and the loop
runs again.

## The timetable view

The page opens on the **Timetable** tab: pick a teacher, class, option group or room and see the
familiar grid, days down and periods across. Cells with a problem are outlined; hover for the
reason, click to select the lesson and see what is wrong in the **Selected** card, then **Fix…**
in the Checks list asks the assistant for a proposal. **Print this** opens the print dialog on the
same timetable. The **3D model** tab is the expert view of the same data; "Why 'Plane'?" in the
header explains the picture. Once a start-wizard template is chosen, the page uses its words
(nurses, wards, shifts) everywhere.

## Quick and best timetables

**Quick timetable** places a draft in seconds. **Best timetable** runs the constraint solver as a
job with a preference — *keep it close* (stay near the live timetable), *balanced*, or *best
quality* — and a time limit (10 to 900 s). The progress card shows elapsed time and the best
objective so far, and can stop early keeping the best result. When the job finishes the draft is
promoted and the Quality panel scores six rules (spread, stability, compact, even days, edge,
venue) before and after. Best-timetable time is metered against your licence key's monthly
budget; Quick timetable is not. Timetable exports (aSc) are imported with student groups and
bands derived from the lessons, so student-side clashes are checked as well as teachers and rooms.

## Fixing and booking

The assistant in the chat panel proposes individual moves, swaps, and bookings and applies them
only after you say yes; **Undo** reverts the last change. Set the term calendar in Settings for
dated bookings so the assistant can reason about term breaks and holidays. The conversation, the
documents you dropped and any option waiting for your yes belong to the timetable, not to your
browser: log in from another device and carry on where you left off.

## Period timetables

A period is a timetable in force for some dates — an exam week, a camp, a block of weeks after the
exams — for the whole school, some levels or some classes. It is its own timetable, made from the
normal one: the lessons of the classes it covers are left out for you to plan, and every other
lesson is copied in and pinned where it is. Create one from the **Periods** card in the side panel
(**New period**: a name, the first and last day, and whole school, levels or classes) or ask in
chat ("make a Sec 4 exam week from 6 to 10 October"). The card opens the new period straight away;
fill it in by chat, workbook or plan, then Quick or Best timetable. The timetable selector marks it
"· period", and a banner says which timetable you are looking at, with **Back to the normal
timetable**. Two periods cannot overlap for the same classes.

Dated views switch by themselves: on a date inside a period, the grid's "Week of", the print
dialog's week of a date and the assistant's where/who and free_venues questions on a date use the
period's timetable. While today falls inside a period, the normal timetable shows a banner saying it
is in force, with **Open**. When the normal timetable changes, the card counts the changes made to
it since the period was made, and **Refresh** copies its other lessons into the period's draft
again, keeping what the period has of its own (its exam papers, its own rooms). The copied lessons
take effect once the period is built again: build it again — Quick or Best timetable — for its dates
to use them. Bookings stay on
the normal timetable and show in its periods too. **Remove** takes the period off the list and
asks whether to delete its timetable as well; kept, it becomes an ordinary timetable.

## Relief cover

When a teacher is away, the **Relief** card in the side panel records the absence and offers cover.
**Add absence** takes the teacher, the first and last day (at most 31 days), optionally only some of
the day (picked by the timetable's own labels, such as P5 to P7, breaks left out; leave both blank for
the whole day) and a reason; or say it in chat ("Ms Lee is away on 6 October"). Each absence shows how
many of its lessons are covered: "open", "2 cards waiting in the chat", "1 of 3 covered", "all 3
covered". **Plan cover** puts one cover card per lesson in the chat, each with the teacher
chosen and two others who are also free. The choice is the relief pool first, then a teacher of the
same subject, then whoever has covered least this term, then the lightest day; a teacher never takes
more than their load allows or more covers in a day than the relief settings say. Apply the cards
you want one at a time — the others stay on offer — or say yes to take them in order. A lesson no
one can take gets a card saying who came nearest, with nothing to apply.

A cover never changes the timetable itself: the week of that date (the grid's "Week of", a printed
week, the assistant's where/who on a date) shows the covering teacher, with "(cover for …)" after
the lesson. **Undo** takes the last cover back; **Remove** on an absence removes it with its covers.
**Relief settings** on the card set the relief pool (the teachers asked first) and the most covers a
teacher takes in a day. **Ledger** opens a printable page of the covers each teacher has taken this
term, most first, with the dates — for claims and moderation.

## Printing

**Print**, beside the timetable selector, opens a dialog: pick a teacher, class, group or room —
or build a custom timetable from any set of people — then the whole cycle or the week of a date,
and either **View** (a printable page in a new tab) or **PDF** (a download). The same row offers
all teachers, all classes or all rooms as a single PDF with a page per timetable; those render in
the request and can take some seconds for a large school. In the chat panel you can just ask —
"print Ms Lee's timetable" — and the assistant hands back both links.

## Curriculum plan

The **Plan** tab, beside the draft tables, holds the curriculum plan: what has to be taught, not
when it happens. One requirement per teaching group — how many periods of what length, which
classes it draws from, who teaches it, what kind of room it needs — plus the divisions and bands
of option groups that run against each other, the staff and their load factors, and rules such as
edge subjects and pinned events like assembly. Fill it by dropping the staff-deployment workbook
(with any `Teaching Group, Enrolled, Capacity` sizes CSVs beside it) on the intake dropzone or on
the Plan tab's **Upload workbook** — the dropzone recognises a deployment workbook by the shape of
its sheets and routes it here — or by describing requirements in chat ("Sec 3 Science: 6 periods,
two doubles, in a lab, Mr Tan"). The Issues panel lists what is missing or contradictory: blocking
issues (a requirement with no teacher, periods that do not match the lesson counts) keep
**Generate draft** disabled, warnings (a class with no size, a teacher over capacity) do not. Fix,
re-read the issues, then Generate: the plan becomes the draft organisation, and Quick timetable and Best
timetable work on it exactly as they do on any other draft. A re-import of an updated workbook merges by id
and keeps what you set in the app — venue, availability, rules and class sizes.

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
