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

From there it is the plan loop: the app reads the workbook into the Requirements tab, lists anything that
does not add up in the Issues panel, and — once nothing is blocking — **Generate** turns the plan
into a draft timetable, **Quick timetable** places it, **Best timetable** improves it, and
**Print** hands it out. Re-drop a corrected workbook at any time; the plan merges and the loop
runs again.

## The page

The app is dark (the Nocturne design system: `app.css` carries its tokens) and laid out as numbered
steps along the top bar, with the **Assistant** chat as a panel on the right that collapses to a thin
rail. The steps, left to right:

| Tab | What it holds |
|---|---|
| 02 Project | The timetable picker (New, Clone, Rename, Delete), Start wizard and the document drop zone |
| 03 Manpower | Everyone in the plan |
| 04 Locations | The rooms of the current timetable, read-only |
| 05 Requirements | The plan: workbook upload and export, the Issues panel, the plan's notes, the deployment board and its tables |
| 06 Constraints | The timetable rules (changed in Settings), the plan's rules and the five checks |
| 07 Build | Generate draft, Quick and Best timetable, beside the 3D view |
| 08 Consolidation | The checks and their clashes, beside the 3D view |
| 09 Summary | The timetable grid beside the 3D view, Workload, Ask and Print |
| 10 Relief | Absences and cover |
| 11 Custom | Period timetables (an exam week, a camp) |
| 12 Settings | The Settings page |

The current tab is in the address (`/#build`), so Back and Forward move between tabs and a link can
open one. A first visit lands on Summary when a timetable exists. On Summary, pick a teacher, class,
option group or room to see the grid, days down and periods across; cells with a problem are
outlined, a click selects the lesson for the **Selected** card, and **Print this** opens the print
dialog. The 3D view is the expert picture of the same data ("Why 'Plane'?" on the Project tab
explains it); both the grid and the 3D view have a full-screen button (Escape closes it). "Hide
setup tabs 2–8" on Summary keeps the bar short once the timetable is running. Once a start-wizard
template is chosen, the page uses its words (nurses, wards, shifts) everywhere.

Templates load scripts and the stylesheet through `{{ asset('name') }}` (`plane_app/assets.py`),
which adds a hash of the file's contents (`/static/model.js?v=…`): a deploy that changes a file
changes its address, so browsers never run a cached old copy beside a new one.

## Learning from use

The app keeps a log of what people decide (moves and swaps they apply, proposals they dismiss,
relief covers, and every quick or best timetable), per timetable, as ids and the plan's own words
(`plane_app/learning/`). From it:

- **Suggestions.** The Constraints tab's "Learned from how you work" card suggests rules when the
  same kind of change keeps happening: keep a subject away from the ends of the day, keep new
  timetables close to the last one, add a teacher who keeps being picked to the relief pool. Nothing
  changes until you accept; every learned rule has an on/off switch, and switching one off puts back
  exactly what it changed (unless it has been changed by hand since).
- **Proof.** The same card shows how many changes were made after each of the last timetables, so
  you can see whether the learned rules cut the hand-fixing.
- **Templates.** "Save as template" on the Project tab turns the current timetable into a start
  wizard template (times, rules, the plan's shape and learned rules; never names, class codes or the
  timetable's name). Saved templates appear in the wizard beside the built-in ones.

### Sharing templates with other schools

A saved template can be shared: **Share with other schools** on the Project tab shows every field
that will be sent (structure only — never names, class codes or the timetable's name) and sends it to
the shared library on the engine. It waits there until the operator approves it; the template then
shows "Shared with other schools", and other schools see it in their start wizard with how many
schools used it and kept it. **Stop sharing** withdraws it. If the engine is slow or down, the wizard
simply shows the built-in and your own templates.

**Reviewing shared items (operator).** Open `<engine URL>/admin/library` and log in as `admin` with
the engine's `ENGINE_ADMIN_TOKEN` (a Railway variable on the plane-engine service). Approve or reject
each waiting item, with a note the school sees. From a shell the same works with
`railway ssh --service plane-engine -- python -m plane_engine.library list|approve ID|reject ID --note "…"`.
Without `ENGINE_ADMIN_TOKEN` the review page does not exist.

### Solved problems

When you apply a move, swap or room change that clears a clash, the app records a small note of it so
the assistant can prefer the kind of fix that has worked before (it asks before proposing fixes).

- **What is recorded.** Only the kind of clash (a person in two places, a room double-booked, lessons
  that must start together, someone outside their hours, an overloaded person), the lesson's length
  (1 to 8 periods), whether it sat at the start or end of a day, and the kind of fix (another period
  the same day, another day, another room, a swap). No names, ids, class codes or text.
- **Undo still counts.** A fix that was applied and then undone stays in the count: it was tried, and
  the note records what was applied, not whether it lasted.
- **Deleting a timetable** removes its records with it.
- **Sharing (off by default).** Switch on **Share solved problems with other schools** in
  Settings → Learning. Then, for each combination of clash, length, start/end of day and kind of fix,
  the app sends exactly that combination and how many times this school has used it (`count`, 1 to
  100000) to the shared library on the engine; nothing else. The first time you switch it on, the
  existing counts are sent in small batches (about 20 a minute) in the background. With sharing off,
  the app makes no fix request to the engine at all. Turning it off stops sending; counts already
  shared stay. What comes back is numbers only (how many schools and how often); the assistant's
  sentences are written by the app in your timetable's own words.

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
the lesson. Each absence lists the covers applied for it ("Tue 6 Oct P3 Maths, set A — Mr Tan"),
each with its own **Remove** to take that one cover back (or say it in chat: "take back Mr Tan's
cover on 6 October"); **Undo** puts a removed cover back, and takes the last applied cover back.
**Remove** on an absence removes it with its covers. **Relief settings** on the card set the relief
pool (the teachers asked first), the most covers a teacher takes in a day and the day the term
starts (leave it blank to use the term calendar's); the card says which day the ledger counts from.
**Ledger** opens a printable page of the covers each teacher has taken since then, most first, with
the dates — for claims and moderation. An absence's lesson count is taken again once it has ended,
so a timetable change during the absence does not leave a stale count.

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
edge subjects and pinned events like assembly. Fill it by dropping the staff-deployment workbook to the
Assistant (📎, a drop on the chat, the Intake box or the Requirements tab's **Upload workbook**) —
it recognises a deployment workbook by the shape of its sheets and shows it as a card to apply. A
`Teaching Group, Enrolled, Capacity` (or `Class, Enrolled`) sizes CSV is read exactly too: give it with the
workbook in one drop and its sizes go into the workbook's card, applied on top of it; given on its own once
the workbook is applied, it is a card of its own that sets the sizes of the classes and groups in the plan.
Or describe requirements in chat ("Sec 3 Science: 6 periods,
two doubles, in a lab, Mr Tan"). The Issues panel lists what is missing or contradictory: blocking
issues (a requirement with no teacher, periods that do not match the lesson counts) keep
**Generate draft** disabled, warnings (a class with no size, a teacher over capacity) do not. Fix,
re-read the issues, then Generate: the plan becomes the draft organisation, and Quick timetable and Best
timetable work on it exactly as they do on any other draft. A re-import of an updated workbook merges by id
and keeps what you set in the app that the workbook does not hold — rooms, lessons to run together,
availability, a day's maximum, rules and class sizes — and, while a row is unchanged, its venue kind too.

**Download template** beside Upload workbook gives a blank workbook in the importer's own shape: a
README sheet explaining every column, an example `MATH` sheet (rows marked "example: replace or
delete"), `Groups` and `Control` with example staff, `Classes` (code, level, size, home room) and
`Venues` (room, block, level, capacity, kind). **Export to Excel** writes the current plan
the same way — one sheet per department with a row per cell (a split cell is one row with its
teachers and `Split` "3/2", locked rows say `Locked: yes`), the divisions and band codes on
`Groups`, and staff with their allowance, reductions ("HOD 8; CCA 2") and provisional flag on
`Control` — so it can be edited offline and uploaded again. In a split row each Teacher column is
one share: a name, `(unassigned)`, or `Kelly Wong + Eugene Lee` for a share taught together.
Importing an export into an empty timetable gives the same plan back, and uploading it into the
timetable it came from keeps rooms and availability; anything a workbook cannot hold exactly (a
lesson longer than four periods, two staff with one name) is listed on its README sheet.

### Any other workbook: the smart reader

Schools keep rooms and manpower in their own layouts. A `.xlsx` or `.csv` the importers above do not
recognise, given to the Assistant (see **Files to the Assistant** below), goes to the **reader**: the
configured model looks at the sheets once and writes a short *reading recipe* (where the rows or the
room codes are, which cells hold class and size, which labels give block and level); the app runs
that recipe itself, checks the result against the plan, and shows it as a card in the Assistant panel —
counts, a few sample rows with the cell each came from, and the things to check (a cell that did not
match, a home room for a class the plan does not have, a room used twice). Nothing changes until
you apply the card, which merges rooms and home rooms into the plan (adding and updating, never
deleting); **Skip file** sets the reading aside. Each file in an upload is reported on its own, so
one file the reader cannot read never hides another's import.

When a workbook reading is applied the recipe is saved as a **reader** for that layout (Project tab,
**Saved readers**, with Delete). The next workbook in the same layout — next semester's copy — is read
by the saved reader without calling the model. The workbook's text goes to the configured model provider
only when no saved reader fits; nothing is shared with other schools. Rooms (`venues`) and home rooms
become the draft's locations on **Generate draft**: a lesson for one class with no room kind set is placed
in that class's home room. The template's `Venues` and `Classes` sheets remain the AI-free way in.

### Files to the Assistant

Give the Assistant whatever the school has — staff lists, allocation or deployment workbooks, a Word or
PDF handbook, last year's timetable export — and it reads each file into a **card** in the chat. Nothing
read from a file changes the plan, the draft or the settings until you apply it. Four ways in all do the
same thing: the **📎** button beside the chat box, a drop anywhere on the Assistant panel, the Project
tab's Intake box and the Requirements tab's **Upload workbook**. Accepted: `.pdf`, `.docx`, `.xlsx`,
`.csv`, several at once.

Each file shows as a chip while it is read (a type badge, the name, then a one-line count or why it could
not be read), then as a card: how it was read (exact, saved reader, or read by AI), and a section for each
kind of thing found — Staff, Classes, Rooms, Requirements, Rules, Notes — each with a tick-box (ticked by
default), a count and a few examples (**Show all** asks the Assistant to list the rest). Names matched by guess, classes not in
the plan and rows left out are listed as things to check. An aSc timetable export (or a document that reads
as a timetable) gets one section, **Start from this timetable**, with the options to keep the existing
placement or start again and to check each class.

- **Apply ticked** applies the ticked sections, merged into the plan as it is then. A section that needs
  another (requirements naming staff or classes the plan lacks, classes given home rooms the plan lacks) is
  refused with the section to tick too. The card folds to a green line saying what was applied, with
  **Undo**, which puts back the plan (or the draft) as it was before that apply. Undo is refused once the
  plan (or draft) has changed in content since; undoing later applies first and then earlier ones works.
- **Skip file** marks the card skipped: it stays in the chat, and the stored file is deleted. **Clear
  readings** (Project tab) drops every reading still waiting, failed or to try again; applied and skipped
  cards stay.
- Giving a file with the same name again replaces its reading, and with it that reading's Undo.
- A file that could not be read gets a card saying why. When the model was busy or not set up, or nothing
  usable came back from it, the card offers **Try again**, which reads the stored file again.
- Each timetable keeps its newest 20 readings; past that the oldest applied, skipped or failed ones go
  first. A card still waiting (or to try again) is never dropped.
- **Or tell me what to change**: ask in the chat ("leave out the Art teachers", "that class is 3C, not
  3G") and the Assistant edits the waiting card; it never applies a card in the same turn that read or
  edited it. A plain **yes** (or "ok", "apply", "go ahead") applies the newest waiting card at once,
  exactly as its Apply ticked would, when that card is newer than the Assistant's last reply; after a
  reply, the yes goes to the Assistant. The Assistant applies a card only when your latest message, sent
  after the card appeared, starts with a yes word and has no "?" and none of "no", "not", "don't",
  "never", "wait", "stop", "cancel" or "but".
- After an apply the Assistant says what is still missing and, once the plan is ready, offers **Make the
  timetable** (generate the draft and a Quick timetable). It never builds unasked.

Exact readers run with no model call: aSc exports, planning (deployment or duties) workbooks and sizes
CSVs. Other
workbooks use a saved reader when one fits; other workbooks and `.docx` or PDF documents otherwise go to
the configured model, which returns structured data that is checked, never run.

A rule in a document that the plan can hold (edge subjects, a pinned lesson such as assembly) is applied as
part of the plan. Anything else ("no PE after lunch on Fridays") is kept as a **note**: the Requirements
tab lists the plan's notes, each with the file it came from and a ✕ to remove it. Notes are for the
timetabler to read; the solver does not use them.

Limits: up to 10 files in one drop and 20 MB a file; a file that fails never stops the others. Old `.doc`
and `.xls` files are refused with a message to save them as `.docx` or `.xlsx`; a scanned PDF (no text
layer) is refused with a message saying so. Uploads, readings and the Assistant's file tools are for the
admin login; department accounts cannot use them. Readings belong to the timetable they were made in.

### Deployment board

The Requirements tab opens on the **Board** (switch to **Tables** for the same plan field by field; the
choice, department and level are remembered in the browser). Pick a department and a level tab
(Sec 1, Sec 2, …): subjects run down the side, classes across, and each cell says how many of its
periods are assigned ("3/5p"), what is still unassigned, and who teaches it — one chip per
teacher or split share, dashed while it can change and solid once locked, "PROV" for a
provisional teacher. Option groups of a band sit on their own lines under one subject, spanning
the classes they draw from.

The teacher tray below lists the department's staff (filters: Available, Fully assigned,
Provisional, All departments) with each teacher's load against their allowance after reductions,
a bar of period blocks and a colour for what is left: green 8 or more, amber under 8, orange under
4, red none. Drag a teacher onto a cell to assign them; onto a cell that already has a teacher,
choose **Replace**, **Co-teach** or **Split…**. Without a mouse, click a teacher to pick them up
and then click cells (Esc puts them down). Click a cell to split its periods (the splits its
lessons allow are offered, "3/2", "4/1"), remove a teacher or lock it; **Lock row**, **Lock Sec
1** and **Lock entire MATH** lock more at once, and a locked cell refuses a drop until it is
unlocked. **Add subject** adds a row for the classes you tick, **Create band** adds option groups
that run together (a blank group code is made up, e.g. 1MEP), **+ Provisional teacher** adds "New
MATH teacher 1" to name later with its **Edit** button (allowance, reductions such as "HOD 8"),
and **Undo** takes back the last board change. The Issues panel and Generate draft stay above the
board and follow every change.

## Appearance

The app has three looks: **Classic** (the dark Nocturne look and its 3D planes view), **Toy town**
(bright, rounded and playful) and **Construction** (Toy town's style as a building site, with a
hazard stripe under the top bar). Settings → **Appearance** sets the school's look, which everyone sees,
the login and department pages included. Anyone can pick another look for themselves from the menu in
the top bar; it is remembered in their browser, and picking the one marked "(school default)" goes back
to following the school. The looks only change
colours, the font (Fredoka, shipped with the app under `static/fonts/` with its OFL licence) and shapes;
printouts stay plain. Toy town and Construction are defined in `static/looks.css`.

## Campus

In Toy town and Construction the 3D area (Build, Consolidation, Summary) opens on the **campus**: the
school drawn as a small 3D model from the timetable itself. Blocks become buildings and levels become
floors, using the blocks and levels the smart reader (or the template's Venues sheet) gave each room;
rooms with no block go to an "Other rooms" building, halls stand on their own, fields lie on the
grounds. With no block data the campus still draws every room and says how to add blocks.

- **Periods:** pick a day and period, or press ‹ › / ← → or Play. Rooms light up in their lesson's
  department colour (a legend shows which); teachers who are free that period walk the courtyard.
- **Clashes** are pinned on the rooms where they happen in that period; **All clashes** shows a count on
  every room with one.
- **Click** a room or a teacher (or find one by name) for a card: what is on now, what is next, how
  much the room is used, a teacher's load and free periods. "Now" selects the lesson in the timetable
  grid, and selecting a lesson in the grid highlights its room.
- **Construction** shows the school as a building site (scaffolding, a tower crane, a fence) until the
  timetable is checked with no clashes; then it shows the finished school.
- **Campus · Planes** switches to the planes view, which in these two looks gets soft light, shadows and
  rounded tiles too. Classic shows only the planes view, as before, and loads none of the campus code.

The campus is served by `GET /api/campus` (`plane_app/campus.py`, which also lays the school out) and
drawn by `static/campus/` with Three.js r160, shipped under `static/vendor/three-0.160.0/` (MIT); nothing
is loaded from a CDN. It renders only while visible and lowers its quality on slow devices.

### Setup tabs

In Toy town and Construction the five setup tabs also show the campus, between each tab's words and its
tables. Each scene is drawn from that tab's own data, from `GET /api/campus/setup` (`plane_app/campus_setup.py`).
Only admin accounts can read it. The scenes only read; they change nothing.

- **Project:** each file given to the Assistant is a sheet on the intake pad, with a tick, a warning or a cross. The pad shows the latest 8 files.
- **Manpower:** teachers stand by department in the department's colour. A teacher's height follows their load against their allowance. A red ring marks a teacher over their allowance.
- **Locations:** rooms are windows coloured by kind. A class's home room is amber unless it is a lab, a computer room or a hall. Rooms with no block stand in "Other rooms", except halls and fields, which stand on their own.
- **Requirements:** each class is a tower of lesson blocks, against the periods the cycle has room for. Blocks over that line glow red. A subject with no teacher yet is see-through: exactly that subject's periods in each class, whichever subject it is. Past 40 classes the towers are grouped by level, and at most 120 are drawn.
- **Constraints:** the week is a row of period tiles a day. Rests are gaps, pinned events are flags, and edge subjects colour the first and last tile of each day.

Click a teacher, class tower, room, period tile or file sheet for a card. Its link goes to the item's row in the tab's
table, or to the file's card in the Assistant. Classic is unchanged: it shows no campus on the setup tabs.

## Journey

A slim bar under the top bar shows where the school is on the way to a timetable: **Files read →
Plan ready → Draft → Timetable → Checked**. Each step is reached (with the time it was first reached),
the current one, or still to do; a step that was reached but is no longer true (a new clash after
Checked, a blocking plan issue after Plan ready) turns amber rather than staying green. Clicking a step
opens its tab. The bar comes from `GET /api/journey` (`plane_app/journey.py`); the first-reached times
are kept per timetable and go with it when it is deleted. The full-screen 3D views hide the bar.

In Toy town and Construction the campus plays each step as it happens: the files fly in to the intake
pad, the buildings rise (in Construction the scaffolding goes up and the crane lowers each floor), the
lessons pop into rooms, rooms flicker while the solver works, and a clean check sweeps the school (in
Construction the site is handed over and becomes the finished school). If the campus is not on screen,
the latest of each step waits and a "Watch it on the campus ▶" note offers to show it. Classic shows the
bar only.

- **▶ Build** replays the whole story on the current data (about 20 seconds) and ends on the true state.
- **Present** (in full screen) is presentation mode for a meeting: the build once, then a new period
  every 4 seconds with the camera turning slowly. Space pauses; Esc, another tab or a reload leaves.
- **Counters** on the campus show lessons placed, teachers deployed, rooms in use this period and
  clashes, counting up as they change.
- **Animations: Full · Reduced · Off** (top bar, this browser only) sets how much moves. Reduced is the
  default when the device asks for reduced motion; Off shows each end state at once and hides ▶ Build.

## Department accounts

The timetabler logs in with the admin password, leaving Username blank. Heads of department log in
with the username and temporary password the timetabler gives them, and must choose a new password
(at least 10 characters) before anything else. A department account reaches only its department's
board, the live timetable and printing; everything else answers "not available to department
accounts".

**Setting up (the timetabler).** In **Settings → Departments & accounts**, choose the timetable
heads of department work on (a period timetable cannot be it), then **Add account** with a
username, name and department (as the plan names it, e.g. MATH). The temporary password is shown
once, with a **Copy** button: pass it on, since it will not be shown again. **Reset password**
makes a new one the same way; **Deactivate** stops the account logging in; **Delete** asks first.
The same section lists each department of that timetable's plan as Open or Submitted, with
**Reopen**. To let a teacher work for another department, give them a share in their **Edit**
box on the board: "MATH 10; SCI 6" (periods a cycle each department may give them).

**The department page.** A head of department logs in to one page: their name, department and
the timetable, **Change password** and **Log out**; a status line ("Open — submit MATH when it is
done"); their department's board (no department select; their own teachers and those shared with
it on the tray, each against what they may be given here); the issues of their department; and
below, the live timetable read-only with **Print this**. They can assign, split, lock, add a
subject or a provisional teacher, name a provisional teacher, and edit their own teachers' names
and reductions; an assignment past a teacher's share is refused. **Submit department** (asked
once more on the page) locks every row and marks it Submitted: the board is then read-only to them
until the timetabler presses **Reopen** (beside the department select on the board, or in
Settings). The **Activity** panel under the board lists who changed what. When two people change
the same cell, the later change is refused with who made the other one, and the board shows the
latest to try again.

- **Log out** ends the session on that browser only.
- **Changing your password** logs you out everywhere else; the browser you changed it on stays
  logged in. A password reset, deactivating or deleting the account also ends all its sessions.
- Five wrong passwords for one username within 15 minutes pause logins to that username for the
  rest of the 15 minutes, from any address.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | Yes in production (`APP_ENV=production`) | Password for the single admin login. Defaults to `admin` outside production. |
| `SECRET_KEY` | Recommended | Signs the session cookie. If unset, a key is generated once and persisted under `DATA_DIR/secret_key`. |
| `ENGINE_URL` | Defaults to `http://localhost:8000`; set it in production | Base URL of the `plane-engine` service, e.g. the engine's Railway URL. The Engine URL on the Settings page, when filled in, takes precedence. |
| `ENGINE_KEY` | No | Engine API key, if the engine requires one. Can also be set (and overridden) from the Settings page — a licence key entered there is stored per-organisation and takes precedence over this variable. |
| `DATA_DIR` | No | Where the app stores its SQLite database and generated secret key. Defaults to `./data`. In a container this should be a mounted volume. |
| `APP_ENV` | No | Set to `production` to make `ADMIN_PASSWORD` mandatory. Proxy headers (`X-Forwarded-Proto` for the Secure cookie flag, `X-Forwarded-For` for login throttling, its rightmost entry: the address the proxy saw) are always honoured. |

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
