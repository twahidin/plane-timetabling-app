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

## The week

Settings → **Week** is a table: one row a day of the cycle, each with its own start time, number of
periods and breaks (recess, lunch, with their own minutes). Every period is the same length; a long
lesson takes two or three periods in a row. The cycle is week-based (one to four weeks of Mon–Fri,
two weeks named Odd and Even) or rolling (Day 1…Day N over school days, a holiday using up no day).
Every day keeps as many positions as the longest one; a shorter day's extra positions are closed, so
nothing is placed there and grids and printouts leave them out. Saving a week that changes the
positions moves every lesson, availability, pinned event and room booking with its day and period
number, asks first when lessons would end up on a closed period or a break, and clears Undo.
A week can't change while the timetable has period timetables or a build is running. The wizard asks
for the week after a Mon–Fri template is chosen; an aSc file with another week offers Adopt or Keep.
Timetables saved before the week table read as "every day the same" and are untouched until saved.

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

## Students

Who takes which option group. The app keeps one record per student (ID, name, form class and the
option groups they take) for each timetable, so it can print a student's own timetable, report a
student who is in two places at once, and size option groups and their rooms on real head counts.

**Who sees it.** Only the admin. A department account never sees a name: it gets counts, and the
student routes answer "not available to department accounts". A student's name is never sent to the
AI provider or to the engine, and never written to a log or an error message (those say counts and
row numbers). The engine and the solver see only anonymous choice patterns: each distinct set of
option groups that some student takes together becomes one anonymous member, added when the app calls
the engine and stripped from what comes back, so a stored draft or live timetable never holds one. The
engine must be deployed first (its `/healthz` lists `patterns`); an older engine is sent nothing, and the
app-side clash check still reports.

**Importing.** Drop an Excel or CSV list on the Assistant or the Intake drop. It is read exactly, never by
the AI: columns Student ID, Name, Class and Subject 1, Subject 2 and on (Admission No, Student Name,
Form Class, Option 1 and Band 1 are understood too, and a single Subjects column as a comma list). A
subject cell is an option group code ("3SCIA") or a subject name; a subject taught in several sets is
shared out evenly across them and marked as placed by the app. A file that looks like a student list
but cannot be read exactly (a Word or PDF list, or columns the reader does not know) is refused with
what to rename, and is never passed to the AI. The result is a reading card: **Apply** replaces the
timetable's students, **Undo** puts the old ones back.

**The start wizard's workbook** has a Students sheet after Control, with one example row. Fill it
in and drop the whole workbook: you get two cards, the plan and the student list (read against the
plan the workbook brings). A sheet left with only its example row, or only its headers, adds no students.
The two cards apply and undo independently. Apply the student list without the workbook's plan card (or
before it) and any student who takes an option group the stored plan does not have is listed as one to
check. The copy of the workbook kept for Apply has the Students sheet removed, so the roster is kept
only in the timetable's students.

**Checking.** The Students section of the Requirements tab shows "1,204 students, 37 choice patterns, 3
to check": an unknown class, a subject the class is not offered, two options of one band (they run at the
same time), a group with more students than its room holds, a repeated ID. Add, edit and remove students
there, or remove them all. The board shows each option group's real count beside the size you gave.
**A partial list** (one class, say) makes the draft's option-group sizes count only the students listed,
so give the whole school's list before you generate a draft for room sizes.

**Printing.** Timetables tab, Student kind (search by name or ID): one student's grid shows the class
lessons and only the student's own option groups. Print one student, a class, a level or everyone, as
one PDF with a student on each page, or as a printable page.

## Option blocks

For the upper years, where a school offers subject combinations (C1: Physics, Chemistry, History) with a
limited number of places and students rank them. The app places each student in a combination, then builds
the option blocks: which subjects run together, how many sets each subject needs, and who teaches each set.
It sits on top of Students (above), so read that first.

**Who sees it.** Only the admin; a department account is refused every route. Nothing in the plan or the
students changes until you press Apply.

**What the school enters.**
- **On each student:** a **Merit** rank (1 chooses first) and **Choice 1, Choice 2, ... Choice n**, the
  combination codes in the student's order of preference. They are read exactly, like the rest of the list.
  A bad or missing merit does not stop the list being read: it is listed under "to check" with its row (for
  example "row 7: Merit isn't a whole number from 1"), and the student has no rank until you fix it. To fix a
  student's merit or choices, correct the list and upload it again: the Students form does not edit them. The
  merit column is headed "Merit" or "Merit Rank"; a column called "Rank" or "Order" is left alone. A choice
  column is only picked up when its header is like "Choice 1", "Preference 1" or "Combination 1", with a
  number; any other column is skipped.
- **On the Requirements tab,** the **Option blocks** card, for one level or for chosen classes: a table of
  combinations (code, name, two or more subjects, **places**), then the limits (the number of blocks, the
  smallest and largest set size, and for each subject its periods a cycle and room kind, prefilled from the
  plan).
- **Or in the workbook:** the start wizard's workbook has a **Combinations** sheet (Year, Code, Name,
  Subject 1 to 5, Places), each Subject cell a short subject code such as PHY. The **Year** column says which level each row belongs to. Drop the workbook as
  usual and you get a combinations card beside the plan's; Apply replaces that level's combinations and
  keeps its limits, and Undo puts the old ones back. A sheet left with only its example row adds nothing.

**Checks before placing.** The card lists what stops a placement: a combination with more subjects than
there are blocks, a choice that is not a combination, a missing or repeated merit rank, and a subject with
no periods a cycle or no department (give them in the card, or the plan's requirement for that subject
gives them). More students than places in all is only a warning, since the extra students will be unplaced.
Build is also refused, before the engine is asked, when no teacher of a subject's department has its
periods left ("GEO: no HUM teacher has 3 periods a cycle left").

**The order of placement.** Overrides first (a student you fixed to a combination keeps it and uses a
place). Then the rest in merit order, ties by student ID: each takes their highest-ranked combination that
still has places. Every student has a line saying why ("3rd · 1st choice C1 full · placed in C2 (2nd
choice)", the first part being the student's merit rank), and
the students none of whose choices had room are listed as unplaced; place them by hand with an override. An
override is marked and kept when you place again, until you clear it. Placing is instant and always gives
the same answer for the same input.

**What the engine sees.** Counts only. A run sends each combination as an opaque id with its subjects by
code and the number of students placed in it; for each subject its code, its room size and how many
teachers have the allowance to take a set; for each department, as an unnamed group, its subjects and how
many of its teachers have the allowance for its smallest set, so one block never holds more Physics,
Chemistry and Biology sets together than Science has teachers; and the limits. Periods stay in the app. It never gets a name, a
student ID, a merit rank or a class list; the app turns the answer back into real students itself. The Assistant is told the same kind of thing: a line of counts per
year group ("Option blocks (Level 3): 3 combinations, 36 placed, 0 unplaced, proposal ready").

**Build, Stop and the proposal.** **Build blocks** starts the engine (60 seconds by default); the card shows
progress and **Stop** ends the run. The proposal has a column per block, each with its sets and their
sizes, and a summary of sets and students. Each set has a suggested teacher from its department, someone
not already teaching another set of that block, preferring one whose allowance still covers the set's
periods, then one who already teaches that subject to the year's classes, then the most allowance left;
change any of them from that
department's teachers. Two sets of one block with one teacher, or a teacher over allowance, are warned. If
the limits cannot be met the run says why, by subject code: before the search, for example "HIS needs 70
places but its teachers and rooms give at most 60" or "PHY, CHE, BIO need 7 sets but their department's
teachers give at most 6 (2 per block × 3 blocks)"; after it, the first single change that would fit, for
example "with 1 more block this would fit", "with 1 more HIS teacher this would fit" or "with sets of up to
35 this would fit".

**Apply and Undo.** **Apply** writes the sets as option groups, the blocks as bands over the year group's
division, and each student's sets; building again afterwards replaces them. Apply is refused when the
students or the setup (combinations, limits, subject details) have changed since the run (build again), or
when the year group's classes have, or when a set an
earlier Apply wrote is locked ("3PHYA is locked: unlock it first"). **Undo** puts the
plan and the students back exactly, and is refused once the plan or the students have changed since the
Apply. After Apply, the Students checks, the clash check, sizes and student timetables work as before.
Exporting the plan to Excel and dropping the workbook back regroups the sets into bands per subject; the
sets keep their year group, and the next Apply puts the blocks back.

**Not covered.** Collecting preferences from students (forms or links); the app setting merit ranks (the
school provides them); one block structure across several year groups; blocks with different numbers of
periods; and moving individual students inside the proposal (edit them in the Students section after Apply).

**Deploying.** Update the engine first: its `/healthz` lists `blocks`. An older engine makes a run answer
"The engine needs an update for option blocks"; placing, the checks and the Assistant's counts still work.
Old timetables, and schools that do not use option blocks, are untouched.

## Starting from five CSV files

A school that has a few plain lists, not a planning workbook, can give the app five CSV files instead.
You do not type the classes or a teacher for every class: the app forms the classes and suggests who
teaches them, and you check it. This is a second way into the same plan; the workbook route above keeps
working as it did.

**The blank files.** In the Start wizard, once a template is chosen, **Download the five CSV files** gives
a zip of `staff.csv`, `venues.csv`, `requirements.csv`, `combinations.csv` and `students.csv`. Each has its
column headers and one example row. The example row starts with "e.g." in its first cell, and any row that
does is ignored when the file is read, so it can stay; a file left with only that row adds nothing, and its
card says it holds only the example row.
An `.xlsx` with a single sheet and the same columns is read the same way.

**What goes in each file.** One row per line below. A file is recognised by its columns, not its name,
and a header may be spelt in capitals or with a plural ("Periods a week", "periods per week"); a few
obvious alternatives are understood too ("Teacher" for Name, "Dept" for Department).

- **staff.csv** (`Name, Short, Department, Load, Subjects`): one teacher a row. A Short left empty keeps
  the teacher's own, and gives a teacher new to the plan their initials. Load is the whole periods a cycle
  the teacher can take; a column called Load factor (1, 0.5) is used when Load is empty. Subjects are the
  subjects the teacher teaches, separated by `;`.
- **venues.csv** (`Room, Kind, Capacity`): one room a row. Kind is a word such as Classroom or Lab; use
  the school's own words.
- **requirements.csv** (`Level, Subject, Periods a week, Taught as, Room kind, Department`): one row
  for each subject at each level. Room kind and Department can be left empty; when Department is empty
  it is the one department whose teachers list that subject, and a subject that matches none or more than
  one is listed as "say which department".
- **combinations.csv** (`Level, Code, Name, Subject 1, Subject 2, Subject 3, Places`): one row for each
  subject combination offered, from Sec 3 up. Add Subject 4 and on if a combination has more subjects.
  Code can be left empty: a combination then keeps the code already stored at its level for the same
  subjects, so giving the file again with a row added or moved leaves every student's choice where it was;
  a new set of subjects is numbered C1, C2 and on, skipping the codes already in use. A code written in
  the file is used as it is, but one that students' choices or an override use cannot be given to other
  subjects: the card says so and Apply is refused. Each subject must be a Banded row of requirements.csv
  at that level.
- **students.csv** (`Student ID, Name, Level, Merit, Choice 1, Choice 2, Choice 3`): once the students
  are known. Merit is the student's rank, 1 choosing first. Each Choice is written as the subjects of an offered combination, in any order ("Physics,
  Chemistry, Biology" or "Chemistry; Physics; Biology"). The app matches it to the combination at that
  level with exactly those subjects. A file with a Class column is the Students list described above, not
  this one.

**Taught as.** One word or phrase in requirements.csv: **Class** (each form class on its own, also what
an empty cell means), **Combined 2** (two form classes together; any number from 2) or **Banded** (taken
in option sets, built by Option blocks; the Classes step writes nothing for it). Combined classes are
paired in code order, 1A with 1B, then 1C with 1D; a class left over is taught alone, and the proposal says
so.

**Subjects with levels.** A teacher who takes a subject only at some levels writes them in brackets:
`Physics (Sec 1-2); Chemistry; Science (Sec 1, Sec 2)`. A subject with no brackets is taken at every
level, and "Sec 1-2" means Sec 1 and Sec 2. "Sec 3", "S3" and "3" are the same level everywhere.
Streamed levels ("3E", "3N") share their level's requirements and teachers' subjects, but each keeps its
own option-block setup; combinations.csv for "Sec 3" is the setup of level 3 itself, and for "3E" the setup
of 3E. A level named "1st Year" or "3Sci" keeps its setup under that name, where Option blocks looks for its
formed classes; a setup stored before under the level's number alone is still found, and moved on the next
change, unless another level ("Sec 1" beside "1st Year") also has that number. A teacher placed on a
subject or level outside what they list gets a warning that you can override; a teacher who lists no
subjects gets none.

**Read exactly, never by the AI.** These files are read row by row by the app. They are never sent to the AI
reader, and a student's name never goes to the engine or a log. A file is taken for one of them by its
columns: staff.csv by Name, Department and either Subjects or Load (Subjects may be left out); venues.csv
by Room and either Kind or Capacity; requirements.csv by Level, Subject and Periods a week;
combinations.csv by Level and Subject 1; students.csv by Student ID, Level and Choice 1. These near misses
are refused, with the column to add, and never handed to the AI: a requirements.csv with Level, Subject
and Taught as but no Periods a week column; a combinations.csv with no Places column; and a file with a
Student ID column (or an ID column beside a Level), no Class column, and no Level or no Choice 1. Every other file
keeps the route it had before: a plain list of names and departments, for instance, or any file with a
Class column (the Students list above).

**A row with a problem.** It is listed on the file's card with the file name and the row number ("students.csv
row 88: Choice 1 (Physics, History, Art) isn't an offered combination for Sec 3"). Only rows that cannot
be read are left out, and the card says "N rows not applied" for them: a row with no name, a load or
periods cell that is not a whole number, a repeated ID, a combination with a subject that is not Banded. A row that reads
but does not match something else is kept and listed under "to check": a teacher's subject the
requirements do not have, or a requirement whose department you must say. A student's choice that matches
no combination is left out of that student and listed; the student is kept. Staff subjects are
checked against requirements.csv; a staff.csv given before any requirements says they are "not yet
checked".

**Dropping the files.** Give them to the Assistant or the Intake drop, together or one at a time. When
several come in one drop the order does not matter: requirements are read first, then combinations, staff,
rooms and students, because each is checked against the ones before it. Each file becomes a card with its
counts and its problems, and nothing changes until you **Apply** that card; **Undo** puts it back.
- staff.csv and venues.csv update a teacher by name and a room by room name. Staff or rooms already in
  the plan but not in the file are kept and listed as "not in this file"; the file never deletes them.
- requirements.csv and combinations.csv replace the rows of the levels they contain.
- students.csv replaces the students of the levels it contains, and leaves other levels alone.

**What is set in the app, not in a file.**
- The **expected number of students** of a level, until you have its student list, and each level's
  **class size**, 40 until you change it, and how its classes are named (3A, 3B by default). These are on
  the Classes card.
- The week (periods a day, breaks, the cycle) in Settings or the Start wizard.
- Constraints such as a teacher's availability and a day's maximum, which stay in the app as before.

**The order.**
1. Drop the files and **Apply** each card. Apply requirements.csv first: the Classes card lists the levels it
   brings.
2. On the **Classes** card (Requirements tab), pick a level, check its expected students or the student
   list it counts, the class size and the naming, and press **Propose**. The classes are the number of
   students divided by the class size, rounded up (change the count if you want another), with sizes within
   one of each other. Where a level has combinations and its students' choices, students of one
   combination are kept together as far as even sizes allow. The proposal shows each class, the
   requirements the Class and Combined subjects give it and a suggested teacher for each. The teacher
   comes from the subject's department, prefers someone whose listed subjects and levels cover it and whose
   allowance can hold it, and keeps one teacher with a class's periods of a subject. You can change
   any teacher; over-allowance and outside-subject warnings are shown, never refused. **Apply** writes the
   classes, the requirements and each student's class; **Undo** puts the plan and the students back
   exactly, and is refused once either has changed since. Form each level in turn. Forming a level again
   replaces what it formed before, and is refused, naming it, if one of those requirements is locked.
   When the requirements change after the classes were formed, the card says to propose again.
3. For Sec 3 and up, **Option blocks** (above) places students in their combinations, builds the blocks
   and suggests a teacher for each set, as it does for a workbook.
4. Build the timetable as usual.

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

## Health check

The app answers `GET /healthz`. On Railway, set it as the service's health check path
(Settings → Deploy → Healthcheck Path: `/healthz`), so a deploy only goes live once the app is up.
Railway finds the `Dockerfile` itself and restarts the service on failure by default.

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
