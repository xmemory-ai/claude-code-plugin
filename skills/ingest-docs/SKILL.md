---
name: ingest-docs
description: Use when the user wants to load a documentation corpus into xmemory — "ingest these docs", "load our documentation into xmemory", "import the docs so I can ask questions about them", "build a memory from this site / llms.txt / folder of Markdown", or wants to benchmark question answering over documentation. Works into new instances or one the user already has. Starts from the questions the user will ask, drafts or checks the schema, pilots a few sections so the user sees what comes out, then splits the docs by section and writes them in batches, resumably. Runs on xmemcli and a bundled script.
---

# Ingest documentation into xmemory

xmemory is a **first-party memory store**: it holds the data you explicitly save to your
xmemory instance, in xmemory's own backend. It does **NOT** read the assistant's built-in
memory, your past chat history, or your files, email, or cloud drives — it only stores and
returns what is written to this instance.

This skill turns a list of questions plus a documentation source into one or two populated
instances — new ones built for the docs, or one the user already has. The promise to the user, in one sentence: *give it your question list and the docs;
it drafts the schema, runs extraction on a few pages so you see what comes out, then splits the
docs by section and writes them in batches.* Every step below exists to make that true.

Two facts drive the design:

- **The schema decides what is kept.** An instance stores what its schema describes, so the
  schema comes from the questions; the docs are only input.
- **Extraction is most accurate on section-sized inputs.** So the corpus is split by section
  before anything is written, and every piece names its page and section.

## Ground rules

- **One question per message.** Use the client's option picker when 2–4 choices cover it.
- **Show every proposal before acting.** Create or write nothing without the user's go-ahead.
  There are three gates: before creating instances (with the pilot), before the bulk write, and
  before any schema change once data is in.
- **Script, not context.** Never read the corpus into your own context — no reading pages or
  chunks wholesale. The bundled script fetches, chunks, writes, retries and keeps the books. You
  read only the handful of sample chunks, the pilot output, and the answers.
- **The credential stays with xmemcli.** Never print, paste or export an API key; the script
  calls `xmemcli`, which reads its own credential.
- Write the product name lowercase: xmemory, including when you describe what you did.

## The bundled script

This skill includes [`scripts/ingest.py`](scripts/ingest.py). Resolve that linked resource to its
absolute installed path; it is `<ingest>` below. Run it with `uv`, which provides Python and the
script's pinned HTML parser libraries (its lockfile `ingest.py.lock` sits beside it):

```bash
uv run <ingest> <command> ...
```

Without `uv`, `python3 <ingest> ...` (3.9 or newer) works for Markdown and plain-text sources;
an HTML page then fails with a message saying to run it with `uv run`.

Every command prints one JSON document on stdout and progress lines on stderr. Everything a run
produces — fetched pages, chunks, the manifest, the write log — lives in its **run directory**.
Use `xmemory-ingest/<short-name>` under the working directory; in a git repository, add the
line `xmemory-ingest/` to the local exclude file, `$(git rev-parse --git-path info/exclude)`,
unless it is already there: a local ignore that is never committed, so nothing a run saves is. Rerunning any command resumes from what the directory holds.

| Command | What it does |
|---|---|
| `discover SOURCE... --run DIR` | Lists the pages a source offers, grouped; sends nothing to xmemory |
| `prepare --run DIR` | Fetches the pages in scope, converts them to Markdown, splits them into chunks, writes `manifest.jsonl` |
| `sample --run DIR --questions FILE` | Picks the chunks that best match each question |
| `write --run DIR --instance ID... (--chunks IDS \| --all) [--sync]` | Writes chunks to every listed instance through xmemcli; `--sync` waits for each write and reports what it stored (the pilot, and any rewrite you want to inspect); `--force`, `--resend-unknown` and `--mark-stored` are explained where they are used |
| `status --run DIR` | What has been written, per instance |
| `ask --run DIR --instance ID... --questions FILE` | Runs the questions through `xmemcli read` |

### Searching the chunks

Each chunk is a file in `<run>/chunks/`, named by its chunk id plus `.md` — the id that
`--chunks` takes. To find the chunks that mention something, search for a literal term:
`grep -rlF -- '<term>' <run>/chunks` (the `--` lets a term like `--force` through; in PowerShell,
`Select-String -Path <run>\chunks\* -SimpleMatch -List '<term>' | Select-Object Path`). Read at
most three of the matches; when many match, narrow the term rather than reading more.

### Checking what is stored

To see whether a value is in an instance, list every record of the type that would hold it,
save the list, and search it locally:

```bash
xmemcli --json --instance-id <id> read --read-mode raw "List every <Type> with all its fields" > <run>/check-<Type>.json
grep -F -- '<part of the value>' <run>/check-<Type>.json
```

(In PowerShell, `Select-String -Path <run>\check-<Type>.json -SimpleMatch '<part of the value>'`.)
The saved JSON escapes accented letters, quotes and backslashes, so search for a plain ASCII
part of the value without them.

Ask for the whole type and search the saved listing: a direct check that does not depend on
how a condition in the question is worded. A full listing without the value counts as not
stored; for a type with very many records, confirm the listing covers them all before treating a
miss as final. When the question is whether one chunk's write landed, search for a value only
that chunk states; a record other chunks also produce does not show it.

## 1. Preflight

```bash
xmemcli --json status
xmemcli --json auth status
```

Read the fields, not the exit codes:

- **`command not found`** → ask to install it: `uv tool install xmemcli`.
- **`version` below `1.5.1`** → `uv tool upgrade xmemcli`. The pilot shows what each write
  changed, which the CLI reports from 1.5.1.
- **`auth status` says `"authenticated": true`** → ready, whether the key comes from a sign-in
  or from `XMEM_API_KEY` in the environment. Go by `auth status` here: `status` reports only a
  sign-in, so it says `false` when the key comes from the environment.
- **`"authenticated": false`, or an `error` with `"stage": "auth"`** → the key is missing or was
  rejected:
  - **while `XMEM_API_KEY` is set** (check with `[ -n "$XMEM_API_KEY" ]`, or
    `[bool]$env:XMEM_API_KEY` in PowerShell — never print it) → that key takes precedence over a
    sign-in, so signing in would not help: ask the user to replace or unset it;
  - **otherwise** → sign in (below).
- **Any other `error`** → the CLI could not check the key (network, server, configuration); show
  the error and stop. Signing in does not fix it.

To sign in, `xmemcli auth login` opens the Console in a browser. Without a browser, run
`xmemcli auth login --email <their-address>` for them: tell them first that a sign-in email is on
its way and their one action is pressing **Approve**; the command waits up to ten minutes, so
give it a long timeout and do not rerun it (each run sends another email).

Then check `uv --version`. When `uv` is missing, ask to install it — `xmemcli` is usually
installed with it — or, for sources that are all Markdown, use `python3` 3.9 or newer.

## 2. Inputs

Ask for three things, **one per message**:

1. **Where the docs go** — new instances built for them (the default, steps 4–6), or an instance
   the user already has. If they already have one, list their instances with
   `xmemcli --json org list instances` and let them pick (from a long list, ask for the name and
   match it); then follow
   [Into an existing instance](#into-an-existing-instance) in place of step 4.
2. **The question list** — 20 to 30 questions the user will actually ask, pasted or as a file.
   Save them one per line to `<run>/questions.txt`. Fewer than ten is fine; say the schema will
   only be as wide as the questions.
3. **The doc source** — anything: a site root URL, `llms.txt`, `llms-full.txt`, `sitemap.xml`,
   page URLs, a folder of Markdown or HTML, or `@list.txt` (one URL or path per line; quote it as
   `'@list.txt'` in PowerShell). For a site
   root the script finds `llms-full.txt`, `llms.txt` or `sitemap.xml` itself, and it prefers each
   site's own Markdown rendering of a page over its HTML. The script reads Markdown, MDX, plain
   text and HTML; anything else is converted first (below).

Then show what the source holds and settle the scope:

```bash
uv run <ingest> discover <source>... --run <run>
```

Show the groups (name, page count, examples) and ask which are in scope — a multi-select when
there are few groups, free text otherwise. Apply it by running `discover` again with
`--include TEXT` / `--exclude TEXT` (each matches a substring of a page's URL, group or title;
repeatable) until the pages in scope are what the user meant: `in_scope_pages` lists them (the
first 60; `pages_file` holds every page with its `in_scope` flag). The run remembers the scope: a
later `discover` without those flags keeps it, and `--reset-scope` clears it.

### Other document formats

`discover` lists what it could not take under `skipped_documents`, by file type, with a few
paths per type under `skipped_examples`; `skipped_file` holds every skipped path with its type.
Nothing there is lost silently: tell the user what was
left out and offer to convert it to Markdown files in `<run>/converted/`, then pass that folder
to `discover` as one more source.
Convert with a tool or a small script — never by reading the files into your context:

| Format | Convert with |
|---|---|
| PDF | `pdftotext <file>.pdf <run>/converted/<name>.md` (poppler). A scanned PDF has no text to extract: it needs OCR first (`ocrmypdf`), so say so. |
| Word (`.docx`), PowerPoint (`.pptx`), OpenDocument, RTF, EPUB, reStructuredText, AsciiDoc, LaTeX, Org, Jupyter notebooks | `pandoc <file> -t gfm -o <run>/converted/<name>.md` — a recent pandoc; older releases cannot read PowerPoint or AsciiDoc. The old binary `.doc` / `.ppt` need saving as `.docx` / `.pptx` first (`soffice --headless --convert-to docx --outdir <run>/converted <file>`). |
| OpenAPI / Swagger specs (`.yaml`, `.json`) | A short script that writes one Markdown section per operation — method and path as the heading, then summary, parameters and responses — so every endpoint becomes its own chunk. A local file is listed when it opens as a spec, a linked one when its file name says so (`openapi.json`, `swagger.yaml`, `api-docs.json`). Other JSON and YAML is counted as site configuration under `skipped_other`, with a few names under `skipped_config_examples` and all of them in `skipped_file`: glance at them in case a spec goes by another name. |
| Spreadsheets, CSV | Ask first: a sheet of reference values can become Markdown tables through a short script; a data export is not documentation. |

A missing tool is the user's to install (`brew install poppler pandoc`, or the system's package
manager) — ask, and do not try to work around it. A page that failed in `prepare` with *no
readable text* is rendered by JavaScript; ask whether the site offers `llms.txt`, Markdown copies
or an export instead. Pages behind a sign-in need an export from the user.

## 3. Chunk

```bash
uv run <ingest> prepare --run <run>
```

Nothing is sent to xmemory here. The script splits each page on its headings — one chunk per
page when it fits, else per section, else per subsection — packs small neighbouring sections
together, and caps chunks at 6,000 characters. It never splits a table row (a long table is cut
between rows with its header repeated), and keeps a code block whole unless it alone runs past
18,000 characters, when it is cut between lines. Every chunk starts with its context:

```
Page: <title> > Section: <path>
Source: <url>
```

Report pages loaded, pages that failed (and why — a JavaScript-rendered or signed-in page cannot
be fetched), the chunk count and sizes. On Windows, a chunk listed under `too_long_for_windows`
cannot be passed to the CLI: shorten that section in the source before writing. Leave `--max-chars` alone unless the pilot gives a
reason.

## 4. Question analysis

Group the questions by shape and show the grouping as a table:

- **Exact lookups** — a value of a named thing: a product code, a price, a version, an endpoint,
  a flag, a limit.
- **Descriptive** — how or why something works, procedures, comparisons.

Propose one **tight** instance for the lookup groups and, when descriptive questions exist, one
**broad** instance for them. The user picks one or both. Name each in 2–4 words.

## 5. Schema — you write the XMD

Read the XMD guide first and follow it: fetch `https://xmemory.ai/xmd/index.html.md` (the
Markdown copy of https://xmemory.ai/xmd/). Write the schema yourself from the questions; do not
hand it to a generator. The guide is your reference, not part of the corpus: read it whole even
when the docs being loaded include it. Where the guide suggests trying a schema with `extract`
before creating an instance, the pilot below does that job on real sections.

Look at how the docs phrase things before you write: run `sample` on the questions and read five
of the chunk files it names, spread across the question groups — those five only.

```bash
uv run <ingest> sample --run <run> --questions <run>/questions.txt
```

Write one XMD file per instance, `<run>/<instance-slug>.xmd.yml`:

- **Objects** for the things the questions ask about, **fields** for the values they ask for,
  **relations** only where a question crosses from one thing to another.
- **Descriptions are extraction rules**, not labels: what qualifies and what does not, units and
  formats, how to tell similar fields apart, enum decision rules. Say in descriptions that each
  input starts with a `Page:` / `Section:` breadcrumb and a `Source:` URL, and that a value's
  subject (the product a limit belongs to, say) may come from that breadcrumb rather than the
  paragraph.
- For the broad instance, model the subjects the descriptive questions ask about — features,
  procedures, concepts — with text fields that keep the explanation.
- **Provenance is optional but cheap.** Decide on a `SourcePage` object keyed on `url` (every
  chunk carries its `Source:` line, so this key is always present) with a relation to the
  extracted records, so answers can name their page; leave it out when it would bloat a small
  schema. The schema table shows the choice, so the user can change it at the gate.

### Keys: settle them in the pilot

A primary key is a **unique constraint**: two mentions with the same key values are one record.

- **Key only on values the docs always state.** Records are matched by their key, so a record
  stored without its key value is matched with every other one stored without it. Key only on
  values that are stated *every* time the thing is mentioned — a product code, an endpoint path, a version number,
  a command name — and mark those fields `required: true`. Use a composite key when one value is
  not unique alone (`[product, version]`).
- **No reliable key → `primary_key: []`.** Records then stay separate, and a thing described in
  several sections may appear more than once.
- **Settle keys in the pilot.** A key can always be relaxed later. Adding one, or making one
  stricter, needs the stored records to be distinct under the new key — `xmemcli schema dry-run`
  says whether they are. A documentation corpus mentions the same things in many sections, so the
  pilot, while the instance holds only a handful of sections, is the easy moment to get keys right.
- Never add a key or a generated id just so an object can take part in a relation.

Validate each file until it passes (exit 0; a pass prints the schema back, normalised):

```bash
xmemcli xmd validate <run>/<instance-slug>.xmd.yml
```

When the account sees more than one cluster, `xmd validate` and `instance create` say so. Pick
the cluster yourself: for an instance that already exists, pass `--instance-id <id>` before the
command; for a new one, take the cluster the user's other instances are on, else the one named
`Default` (`xmemcli --json org list clusters` lists them), set `XMEM_CLUSTER_ID=<its id>` for
both commands, and say which one you used. Ask only when neither settles it — their instances
spread over several clusters and none is named `Default`.

Show each instance as a table — objects, their fields (type, required, allowed values), keys,
relations, and whether `SourcePage` is in — not raw YAML unless asked.

Pick the pilot now, so the gate can name it: 5–10 chunks that cover every question group. Write
one or two representative questions per group to `<run>/pilot-questions.txt` and run `sample` on
that file; its `chunk_ids` line is the starting point. `sample` matches words, not meaning, so
look at the first lines of each pick (the pilot shows them anyway): when one does not hold its
question's answer, [search the chunks](#searching-the-chunks) for a term the answer would contain
and pilot that chunk instead.

**Gate 1.** Ask: "Create <names> and write these <N> pilot sections to them?" — list the
sections — *Create and pilot* / *Change something*.

### Into an existing instance

When the docs go into an instance the user already has — one they prepared for them, one that
already holds other docs, or one with their own records — its schema is the starting point: it
is extended, never replaced. Follow these items in place of step 4; step 5's guidance — the XMD
guide, `sample`, the five chunks, keys — applies to what you add.

1. Read the schema: `xmemcli --json schema get <id>`. Show it as a table (objects, fields, keys,
   relations), then map every question to the object and field that would hold its answer, and
   list the questions with no place in it. When most questions have no place, say so and offer
   new instances for the docs (steps 4–6) instead of reshaping theirs.
2. For questions with no place, write additions — new fields, objects or relations, with
   descriptions that mention the `Page:` / `Section:` breadcrumb. Start from
   `xmemcli schema get <id> -o <file>`, edit that file, `xmemcli xmd validate` it, then
   `xmemcli schema dry-run <id> --schema-file <file>`, show the preview, and on approval
   `xmemcli schema update <id> --schema-file <file>`. Leave the existing keys alone unless the
   user asks, and change one only if the dry-run finds no colliding records.
3. Say plainly what writing docs here does, as their schema defines it: records the docs name
   are updated in place, values already there included, and new records are added.
4. Pick the pilot as above, grouping the questions by shape as step 4 does. **Gate 1** becomes:
   "Write these <N> pilot sections to <name>?" — *Write the pilot* / *Change something*.

Then run step 6 without the create. Its `overwrote … value(s)` warnings show which values
already in the instance the docs replace; show them, so the user sees it before the bulk write.
A fix goes into their schema as in item 2. If a key must change and the dry-run refuses it, do
not replace their instance — offer a separate instance for the docs and let the user decide.

## 6. Create and pilot

```bash
xmemcli --json instance create --name "<Name>" --description "<one line>" --schema-file <run>/<instance-slug>.xmd.yml
```

Keep each returned instance id. The CLI picks the cluster when there is exactly one; with more
than one, use the cluster chosen at validation (step 5). On an instance-limit error,
say the plan's limit is reached, point at the Console, and stop — never delete anything to make
room.

Then write the pilot chunks for real to every chosen instance, waiting for each:

```bash
uv run <ingest> write --run <run> --instance <id> [--instance <id2>] --chunks <chunk_ids from sample> --sync
```

These are real writes into the instance; they count as done, and the bulk write skips them.
The report shows, per chunk and instance, what each write stored — records created with their
field values, fields it changed or filled on earlier records (old → new, `(empty)` → new) — plus
warnings, the tokens the write used, and a Console link; `pilot/*.json` holds the full record,
one file per write (the chunk id and the write id are in its name), so earlier rounds stay.
A chunk lists up to 15 records; `more_objects` counts the rest, so when a record you expected is
not listed, look in that chunk's `pilot/*.json` file. Show each chunk's section and first dozen
lines (read those pilot chunk files only) next to what came out of it, then the `token_estimate`
projection for the whole corpus. It is projected from this command's writes that report tokens
(`based_on_chunks` counts them), so take it from the full pilot; a rewrite of one or two chunks
projects roughly at best.

Check, and say what you see:

- Does each question group's information come out as fields? The report lists what each write
  changed (`changed_records`), not everything it extracted: a record extracted exactly as an
  earlier chunk or round already stored it is not listed. So an empty report from a chunk that
  clearly holds the answer is inconclusive — [check what is stored](#checking-what-is-stored)
  for that answer; only when it is missing is a description too narrow. A record with
  `report_error` landed but its report could not be read: look at that write in the Console.
- **`came out without its key`** — a keyed object was extracted without its key value, so it
  would be matched with others like it. Fix the key or the description that should fill it.
- **`merged_field_conflicts`** — the server merged records whose values disagree: the key may be
  too coarse. Other `server_notes` buckets are counts for information.
- **`overwrote … value(s)`** — a write changed a record an earlier chunk wrote, and the warning
  names the fields. Fine when it is the same thing described again (a page title refined, a
  purpose reworded); a sign the key is too coarse when two different things now share one record.
  Pilot writes run four at a time, so which chunk wrote first can differ from round to round.
- **The same thing as separate records** of an unkeyed object across chunks — it needs a key,
  and now is when it can still get one cleanly.
- **`deleted`** — unexpected when loading documentation; find the description that
  made it.

Fixing it: edit the XMD and `xmemcli xmd validate` it. A stray record the pilot stored (a thing
the docs only mention in passing, say) can be removed by a structured write, as under
**Stored wrong** in [Verify](#8-verify), once the description that produced it is fixed —
only a record the pilot created (its pilot files list them under `objects` and, for unkeyed
types, `server_notes` → `created_keyless_objects`). Then
there are two ways to apply a schema fix:

- **Update in place** — `xmemcli schema dry-run <id> --schema-file <file>`, show the preview,
  and on approval `xmemcli schema update <id> --schema-file <file>`; then write the pilot chunks
  again with `--chunks <ids> --force --sync`. This fits changes that only add or reword: new
  fields, objects or relations, new enum values, sharper descriptions, and a key the dry-run
  accepts. A rewrite updates keyed records in place, but it does not take back what the earlier
  version stored: records of an unkeyed object may be stored again beside the first ones, and a
  record the fix now names differently stays under its old name too. Say so when you show the
  result.
- **A fresh pilot instance** — for anything else: removing or renaming a field or an enum value,
  a key the dry-run refuses because pilot records already collide under it, or a pilot that the
  rewrite above would leave cluttered. Create a new instance from the corrected schema (with
  approval) and pilot that; the old one holds only pilot sections, and the user can delete it in
  the Console. From then on name only the new instance in `write`; `status` still lists the old
  one as unwritten, which is expected. When the dry-run asks for a migration plan or refuses the
  change, take this path rather than working around it. It is for instances this skill created:
  an instance the user already had is never replaced (see
  [Into an existing instance](#into-an-existing-instance)).

Repeat until the user is satisfied. A pilot write the server has not finished when the wait runs
out is `pending`: rerun the command later without `--force`, and it collects the write and its
report without sending it again. With `--force`, it waits for that write to finish and then
sends the chunk again, so a rewrite never overlaps an earlier copy and its report is the
rewrite's own. A write reported `unknown` may have landed; the report names its chunk, and no
later run sends it on its own. [Check whether it landed](#checking-what-is-stored), then handle it
as under **`unknown`** in [step 7](#7-bulk-write).

If the pilot shows no token figures and a `tokens_error` about a Console origin (an API other than
`https://api.xmemory.ai`), set `XMEM_CONSOLE_URL` to the Console paired with that API and rerun;
everything else in the report does not depend on it.

## 7. Bulk write

**Gate 2.** State, then ask to start:

- chunks × instances = writes, minus the pilot chunks already written;
- the projected tokens from the pilot against what is left of the quota:
  `xmemcli --json --instance-id <id> quota` lists usage and limit per window when the plan sets
  them; when it lists nothing or reports an error, ask the user to check the plan in the
  Console. A run that hits a quota stops cleanly and resumes later;
- the time it may take: the first full pilot's `seconds` divided by its chunks, times the chunks
  left, is a rough guide (both run four writes at a time, and the pilot also builds its
  reports, so it errs long); say that an interrupted run resumes where it stopped.

```bash
uv run <ingest> write --run <run> --instance <id> [--instance <id2>] --all
```

Every chunk goes to every chosen instance; each schema keeps what it describes. A large corpus
takes a while: run it as a background task if the client supports one and check its progress lines
now and then; otherwise give it a long timeout. An interrupted run sends nothing more; writes
already sent finish on the server, and the same command picks them up without sending them
again. That includes a background task the client stopped at a time limit, with no report:
run the same command again. Only one `write` or `prepare` runs per run directory at a time.

When it ends, report per instance: `completed`, `already_written`, `failed`, `pending`,
`unknown`, `not_attempted`, and `stopped` if set. The bulk write does not count tokens per write;
the Console shows what the run used.

- **Quota** (`stopped: quota exhausted…`) — say which limit; the same command resumes after the
  reset or a plan change.
- **Unreachable** (`stopped: xmemory could not be reached…`) — run the same command again once
  it answers; the write that was leaving when it stopped comes back as `unknown`, settled as
  below.
- **`failed`** — list the chunk ids and errors; the same command retries them. A write that keeps
  failing with the same error needs a look at that one chunk.
- **`pending`** — still processing when the wait ran out; the same command checks them again
  without resending.
- **`unknown`** — the script could not confirm the outcome (for example, the connection dropped
  before the answer arrived), or the write is too old for its status to be looked up. It may
  already be stored, so it is not sent again on its own. (`failed` means it was certainly not
  stored.)
  The report lists each under `unknown` with its chunk and reason, including those held back
  from earlier runs. [Check whether each landed](#checking-what-is-stored), and settle it:
  - **found** → `write --chunks <ids> --instance <id> --mark-stored`, naming the one instance
    the check covered; it sends nothing and stops the chunk coming up again;
  - **clearly not found** in a full listing → check once more after the `--status-timeout` wait
    has passed (a write that left late can still land), then send it with
    `write --chunks <ids> --resend-unknown`;
  - **the check cannot settle it** (a listing that may be incomplete) → ask the user, saying a
    resend may store it twice.

  Tell the user what was found and done for each.

`uv run <ingest> status --run <run>` shows where every instance stands, counting `outdated`
chunks: an earlier version of them was written, the current one not yet.

## 8. Verify

```bash
uv run <ingest> ask --run <run> --instance <id> [--instance <id2>] --questions <run>/questions.txt
```

Judge every answer. To check one against the docs, [search the chunks](#searching-the-chunks) for
a term the answer would contain, or run `sample` on just that question with `--per-question 3` —
never read the corpus. Show a table: question,
instance, short answer, verdict (correct, partial, wrong, no answer), Console link.
`answers.jsonl` keeps every round, each answer with the time it was asked.

For each miss, first [check whether the answer was stored](#checking-what-is-stored), listing
the type that should hold it.

- **Stored, under different words** — the record is there; the question did not reach it. Try the
  question phrased the way the user would really ask it, and consider a field description that
  names how people refer to the value.
- **Not stored** — either the schema has no place for it (a new field or object) or a
  description needs to say more precisely what to pick up.
- **Stored wrong** — a keyed record holds a wrong value, or a record should not be there at all.
  A rewrite does not remove records or clear fields, and extracts by the same descriptions as
  before. Correct it with a structured write, which changes exactly the record named and
  extracts nothing. To set a field, write a file holding
  `[{"object_mutation": {"object_type": "<Type>", "update": {"key": {"<key field>": "<value>"},
  "values": {"<field>": "<correct value>"}}}}]` and run
  `xmemcli --json --instance-id <id> write --mutations-file <file>`. To remove one record, run
  `xmemcli --json --instance-id <id> write --delete '<Type>:<key field>=<value>'` (a composite
  key as `'<Type>:<field>=<value>,<field>=<value>'`; a value with a comma in it needs a `delete`
  entry, `{"key": {...}}`, in a mutations file instead); a record of an unkeyed type is named by
  its id instead, `'<Type>:<xuid>'`. Always pass `--instance-id`.
  Show the exact change and get the user's approval first, and fix the description that led to
  it, so a later rewrite does not bring it back.

Propose the fix. Schema changes go through `xmemcli xmd validate`, `xmemcli schema dry-run` and the
user's approval before `xmemcli schema update` — **never replace a live schema from scratch**. A
key can only be tightened now if the dry-run finds no colliding records. A schema change does not
re-extract what is already written: rewrite the chunks that matter — [search the
chunks](#searching-the-chunks) for a term from the answer or from the wrong record, then
`write --chunks <ids> --force`, adding
`--sync` to see what each rewrite changed. Rewrite everything with `--all --force`, at full
cost, only if the user asks. As in the pilot, a rewrite does not take back what the earlier
version stored: unkeyed records may now appear twice.

## Finish

Summarise: instances (name and id), chunks written, how many questions answered, the gaps and
proposed fixes. Offer to connect the instances for later sessions — the plugin's `connect`
skill records the binding, and `xmemcli instance setup <id>` prints the MCP entry. The run
directory stays: when the docs change, run `discover` on the same sources (the saved scope
applies), `prepare --refresh`, and `write --all`; only chunks whose text changed are written
again. Records from sections that were removed, and the earlier values of changed ones that the
new text does not overwrite, stay in the instance.
