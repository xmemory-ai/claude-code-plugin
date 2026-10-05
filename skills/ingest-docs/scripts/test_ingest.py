# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "beautifulsoup4>=4.12,<5",
#     "lxml>=5,<7",
#     "markdownify>=0.13,<2",
# ]
# ///
"""Offline tests for ingest.py: chunking, discovery formats, HTML conversion, and the write loop.

The write loop runs against a fake xmemcli written to a temporary directory, so no network and
no xmemory account are needed. The HTML tests use the same locked parser libraries as the script:

    uv run skills/ingest-docs/scripts/test_ingest.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ingest", HERE / "ingest.py")
assert spec is not None and spec.loader is not None
ingest = importlib.util.module_from_spec(spec)
sys.modules["ingest"] = ingest
spec.loader.exec_module(ingest)

INSTANCE = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"

FAKE_XMEMCLI = r'''
import json, os, sys, uuid
state = os.environ["FAKE_XMEMCLI_DIR"]
mode = os.environ.get("FAKE_XMEMCLI_MODE", "ok")
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")

def out(doc, code=0):
    print(json.dumps(doc))
    sys.exit(code)

def first(name):
    try:
        os.close(os.open(os.path.join(state, name), os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        return False

def writes_so_far():
    with open(os.path.join(state, "calls.jsonl")) as log:
        return sum(1 for line in log if "write" in json.loads(line) and "write-status" not in json.loads(line))

rest = [a for a in args if a not in ("--json", "--verbose")]
if "--instance-id" in rest:
    i = rest.index("--instance-id")
    rest = rest[:i] + rest[i + 2:]
command = rest[0]
if command == "write":
    if mode.startswith("quota_after:") and writes_so_far() > int(mode.split(":")[1]):
        out({"error": "Daily token quota exceeded.", "status": 402, "stage": "quota"}, 6)
    if mode == "rate_limit_first" and first("rate_limited"):
        out({"error": "rate limited (retry_after: 0s)", "status": 429, "stage": "request"}, 3)
    if mode == "bad_request":
        out({"error": "text too long", "status": 400, "stage": "request"}, 3)
    if mode == "no_write_id" and "--no-wait" in rest:
        out({"queued": True})
    if mode == "unreadable_200":
        out({"error": "the response body is not valid JSON", "status": 200, "stage": "request"}, 3)
    if mode == "unexpected_exit":
        print("Traceback (most recent call last): ...", file=sys.stderr)
        sys.exit(1)
    if mode == "async_502_first" and "--no-wait" in rest and first("bad_gateway"):
        out({"error": "HTTP 502: bad gateway", "status": 502, "stage": "request"}, 3)
    if "--no-wait" not in rest:
        out({"error": "every write is queued; a waiting write is not expected", "status": None, "stage": "usage"}, 2)
    if mode == "still_processing":
        out({"write_id": str(uuid.uuid4()), "write_status": "extracting"}, 8)
    if mode == "network_down":
        out({"error": "Network error: connection refused", "status": None, "stage": "request"}, 3)
    out({"write_id": str(uuid.uuid4())})
if command == "write-status":
    write_id = rest[1]
    if mode == "fail_status_first" and first("failed_once"):
        out({"write_id": write_id, "write_status": "failed", "error_detail": "extraction failed"})
    if mode == "pending":
        out({"write_id": write_id, "write_status": "extracting", "timed_out": True}, 5)
    if mode == "status_not_found":
        out({"write_id": write_id, "write_status": "not_found"})
    if mode == "in_progress_status":
        polls = sum(1 for line in open(os.path.join(state, "calls.jsonl")) if "write-status" in json.loads(line))
        if polls <= 6:
            out({"write_id": write_id, "write_status": "extracted", "timed_out": True}, 5)
    if mode == "report_unreadable" and "--verbose" in args:
        out({"error": "HTTP 503: service unavailable", "status": 503, "stage": "request"}, 3)
    if "--verbose" not in args:
        out({"write_id": write_id, "write_status": "completed"})
    # A completed write's status carries what it stored, and under --verbose its trace and Console
    # link, as xmemcli 1.5.1 prints it.
    out({"write_id": write_id, "write_status": "completed", "trace_id": "t-" + write_id[:6],
         "console_url": "https://console.example/write/x", "changes": {
        "created": {"objects": [
            {"name": "CliCommand", "identifier": "command='write'",
             "fields": [{"name": "purpose", "value": {"string_value": "writes"}}]},
            {"name": "CliCommand", "identifier": "#1",
             "fields": [{"name": "purpose", "value": {"string_value": "no key here"}}]},
        ], "relations": []},
        "updated": {"objects": [
            {"name": "CliCommand", "identifier": "command='read'",
             "fields": [{"name": "purpose", "old_value": {"string_value": "old text"},
                         "new_value": {"string_value": "new text"}},
                        {"name": "usage", "old_value": None, "new_value": {"string_value": "xmemcli read"}}]},
        ], "relations": []},
        "deleted": {"objects": [], "relations": []},
        "overwritten_field_values": [{"object": "CliCommand", "field": "purpose"}],
    }})
if command == "schema":
    out({"data_schema": {"objects": {"CliCommand": {"primary_key": ["command"], "fields": {}}}, "relations": {}}})
if command == "trace":
    # Only the token count is read from a trace.
    if mode == "trace_late" and first("trace_not_ready"):
        out({"found": True, "xmemory_tokens_used": None})
    out({"found": True, "xmemory_tokens_used": 30.0})
if command == "read":
    read_mode = rest[rest.index("--read-mode") + 1] if "--read-mode" in rest else "single"
    if read_mode == "single":
        out({"answer": "answer to " + rest[-1], "console_url": "https://console.example/read/y"})
    # An xresponse answer: the records the read selected, here more than the report lists.
    out({"objects": [{"name": "Term", "identifier": "term='t%d'" % n,
                      "fields": [{"name": "term", "value": {"string_value": "t%d" % n}},
                                 {"name": "definition", "value": {"string_value": "answer to " + rest[-1]}}]}
                     for n in range(17)],
         "relations": [], "pending_suggestions": 0, "console_url": "https://console.example/read/y"})
out({"error": "unexpected " + " ".join(args), "status": None, "stage": "usage"}, 2)
'''


def run_main(argv: list[str]) -> tuple[int, dict]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = ingest.main(argv)
    return code, json.loads(stdout.getvalue())


def page(title: str, sections: dict[str, str]) -> str:
    parts = [f"# {title}", ""]
    for heading, body in sections.items():
        parts += [f"## {heading}", "", body, ""]
    return "\n".join(parts)


class ChunkingTests(unittest.TestCase):
    def test_small_page_is_one_chunk_with_its_breadcrumb(self) -> None:
        title, chunks = ingest.chunk_page("# Limits\n\nEach workspace has 8 seats.\n", "", 6000)
        self.assertEqual(title, "Limits")
        self.assertEqual(len(chunks), 1)
        text = ingest.render_chunk(title, chunks[0][0], "https://docs.example/limits", chunks[0][1])
        self.assertTrue(text.startswith("Page: Limits\nSource: https://docs.example/limits\n\n"))
        self.assertIn("8 seats", text)

    def test_large_page_splits_on_sections_and_keeps_the_path(self) -> None:
        body = "word " * 400
        markdown = page("Workspace", {"Members": body, "Limits": body, "Billing": body})
        title, chunks = ingest.chunk_page(markdown, "", 2500)
        self.assertEqual(title, "Workspace")
        self.assertEqual([label for label, _ in chunks], ["Members", "Limits", "Billing"])
        for _, text in chunks:
            self.assertLessEqual(len(text), 2500)

    def test_subsections_split_when_a_section_is_too_large(self) -> None:
        body = "word " * 300
        markdown = "# Storage\n\n## Buckets\n\nintro\n\n### Create\n\n" + body + "\n\n### Delete\n\n" + body + "\n"
        _, chunks = ingest.chunk_page(markdown, "", 2000)
        labels = [label for label, _ in chunks]
        self.assertIn("Buckets > Create", labels[0])
        self.assertEqual(labels[-1], "Buckets > Delete")

    def test_small_sections_are_packed_together(self) -> None:
        markdown = page("FAQ", {f"Q{i}": "short answer " * 20 for i in range(10)})
        _, chunks = ingest.chunk_page(markdown, "", 1200)
        self.assertLess(len(chunks), 10)
        self.assertTrue(all(len(text) <= 1200 for _, text in chunks))

    def test_a_code_block_is_never_split_and_its_hash_lines_are_not_headings(self) -> None:
        code = "```bash\n" + "\n".join(f"# step {i}\necho {i}" for i in range(200)) + "\n```"
        markdown = "# CLI\n\n## Install\n\n" + "text " * 100 + "\n\n" + code + "\n\n## Next\n\nmore\n"
        _, chunks = ingest.chunk_page(markdown, "", 1500)
        with_code = [text for _, text in chunks if "```bash" in text]
        self.assertEqual(len(with_code), 1)
        self.assertIn("echo 199", with_code[0])
        self.assertTrue(with_code[0].rstrip().endswith("```"))
        self.assertFalse(any("step" in label for label, _ in chunks))

    def test_a_large_table_splits_by_rows_and_repeats_its_header(self) -> None:
        rows = "\n".join(f"| item-{i} | {i} |" for i in range(300))
        markdown = "# Limits\n\n## Table\n\n| Item | Limit |\n| --- | --- |\n" + rows + "\n"
        _, chunks = ingest.chunk_page(markdown, "", 1500)
        self.assertGreater(len(chunks), 1)
        for _, text in chunks:
            self.assertIn("| Item | Limit |\n| --- | --- |", text)
        joined = "\n".join(text for _, text in chunks)
        self.assertIn("| item-0 | 0 |", joined)
        self.assertIn("| item-299 | 299 |", joined)

    def test_a_table_without_outer_pipes_keeps_its_header_in_every_part(self) -> None:
        rows = "\n".join(f"item-{i} | {i}" for i in range(100))
        markdown = "# Limits\n\n## Table\n\nName | Limit\n--- | ---\n" + rows + "\n"
        _, chunks = ingest.chunk_page(markdown, "", 500)
        self.assertGreater(len(chunks), 1)
        for _, text in chunks:
            self.assertIn("Name | Limit\n--- | ---", text)

    def test_an_import_inside_a_nested_code_example_is_kept(self) -> None:
        markdown = '# SDK\n\n````md\n```js\nimport client from "sdk";\nclient.call()\n```\n````\n'
        _, chunks = ingest.chunk_page(markdown, "", 6000)
        self.assertIn('import client from "sdk";', chunks[0][1])

    def test_front_matter_and_mdx_imports_are_dropped(self) -> None:
        markdown = "---\ntitle: From front matter\n---\nimport Tabs from '@theme/Tabs';\n\nBody text.\n"
        title, chunks = ingest.chunk_page(markdown, "", 6000)
        self.assertEqual(title, "From front matter")
        self.assertEqual(chunks[0][1].strip(), "Body text.")

    def test_a_prose_line_that_starts_with_import_is_kept(self) -> None:
        _, chunks = ingest.chunk_page("# Data\n\nimport rows from a CSV file with the loader.\n", "", 6000)
        self.assertIn("import rows from a CSV file", chunks[0][1])

    def test_a_trailing_hash_that_is_part_of_the_name_stays(self) -> None:
        blocks = ingest.parse_blocks("## Using C#\n\n## Closing hashes ##\n")
        self.assertEqual([b.title for b in blocks], ["Using C#", "Closing hashes"])

    def test_crlf_and_lf_copies_of_a_page_chunk_the_same(self) -> None:
        lf = "---\ntitle: T\n---\n# T\n\n## A\n\n" + "a " * 800 + "\n\n## B\n\n" + "b " * 800 + "\n"
        self.assertEqual(ingest.chunk_page(lf, "", 1200), ingest.chunk_page(lf.replace("\n", "\r\n"), "", 1200))

    def test_growing_one_section_leaves_the_other_chunks_alone(self) -> None:
        sections = {f"Section {i}": f"Text of section {i}. " * 12 for i in range(24)}
        before = ingest.chunk_page(page("Guide", sections), "", 2000)[1]
        sections["Section 5"] += "One more sentence."
        after = ingest.chunk_page(page("Guide", sections), "", 2000)[1]
        changed = set(after) - set(before)
        self.assertEqual(len(before), len(after))
        self.assertEqual(len(changed), 1)
        self.assertIn("One more sentence.", next(iter(changed))[1])

    def test_prepare_lists_chunks_too_long_for_a_windows_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp, "docs")
            docs.mkdir()
            # About 29,000 characters: whole at --max-chars 12000, past the limit once its quotes are escaped.
            quoted = "```\n" + "\n".join('say "' + "q" * 20 + '"' for _ in range(1000)) + "\n```"
            (docs / "big.md").write_text("# Big\n\n" + quoted + "\n")
            (docs / "small.md").write_text("# Small\n\nfine\n")
            run_dir = os.path.join(tmp, "run")
            run_main(["discover", str(docs), "--run", run_dir])
            code, prepared = run_main(["prepare", "--run", run_dir, "--max-chars", "12000"])
            self.assertEqual(code, 0)
            self.assertEqual(len(prepared["too_long_for_windows"]), 1)

    def test_only_a_code_block_past_the_hard_limit_is_cut(self) -> None:
        code = "```\n" + "\n".join(f"line {i:05d} " + "x" * 40 for i in range(600)) + "\n```"
        _, chunks = ingest.chunk_page("# Big\n\n## Code\n\n" + code + "\n\n## After\n\nend\n", "", 1500)
        pieces = [text for _, text in chunks if "line 0" in text]
        self.assertGreater(len(pieces), 1)
        for text in pieces:
            self.assertLessEqual(len(text), 1500 * ingest.HARD_MAX_FACTOR + 100)
            self.assertTrue(text.lstrip().startswith(("## Code\n\n```", "```")))


class DiscoveryTests(unittest.TestCase):
    def test_canonical_key_folds_the_renderings_of_one_page(self) -> None:
        keys = {
            ingest.canonical_key(u)
            for u in (
                "https://docs.example/xmd/",
                "https://docs.example/xmd/index.html.md",
                "https://docs.example/xmd.md",
                "https://Docs.example/xmd",
            )
        }
        self.assertEqual(keys, {"https://docs.example/xmd"})

    def test_llms_full_is_split_on_its_page_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = ingest.Run(os.path.join(tmp, "run"))
            run.ensure()
            discovery = ingest.Discovery(run)
            text = (
                "## Nav\n\n# Guides\nSource: https://docs.example/guides.md\n\nGetting started.\n\n"
                "# Reference\nSource: https://docs.example/reference.md\n\nAll options.\n"
            )
            self.assertTrue(discovery.from_llms_full(text, "llms-full.txt"))
            self.assertEqual([p["title"] for p in discovery.pages], ["Guides", "Reference"])
            body = (run.root / discovery.pages[1]["body_file"]).read_text()
            self.assertIn("All options.", body)
            self.assertNotIn("Getting started.", body)

    def test_llms_index_keeps_groups_and_drops_duplicates_and_non_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = ingest.Run(os.path.join(tmp, "run"))
            run.ensure()
            discovery = ingest.Discovery(run)
            text = textwrap.dedent("""\
                # Site
                ## Guides
                - [XMD](https://docs.example/xmd/): the format
                - [XMD again](https://docs.example/xmd/index.html.md)
                ## Machine
                - [Coordinates](https://docs.example/.well-known/x.json)
                - [CLI](/cli/index.html.md)
                """)
            discovery.from_llms_index(text, "https://docs.example/llms.txt")
            self.assertEqual([(p["title"], p["group"]) for p in discovery.pages], [("XMD", "Guides"), ("CLI", "Machine")])
            self.assertEqual(discovery.pages[1]["url"], "https://docs.example/cli/index.html.md")
            self.assertEqual(discovery.skipped, 1)

    def test_discover_reports_the_documents_it_could_not_take(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp, "docs")
            docs.mkdir()
            (docs / "guide.md").write_text("# Guide\n\ntext\n")
            for name in ("manual.pdf", "logo.png", ".DS_Store"):
                (docs / name).write_bytes(b"x")
            (docs / "api.yaml").write_text("openapi: 3.1.0\ninfo:\n  title: API\n")
            (docs / "spec.json").write_text('{"swagger":"2.0","info":{}}')
            (docs / "mkdocs.yml").write_text("site_name: Docs\n")
            (docs / "_category_.json").write_text('{"label": "Guides"}')
            code, found = run_main(["discover", str(docs), "--run", os.path.join(tmp, "run")])
            self.assertEqual(found["pages"], 1)
            self.assertEqual(found["skipped_documents"], {".json": 1, ".pdf": 1, ".yaml": 1})
            self.assertEqual(found["skipped_examples"][".pdf"], [str(docs / "manual.pdf")])
            # The picture and the two site config files are counted, not offered for conversion.
            self.assertEqual(found["skipped_other"], 3)

    def test_a_linked_json_file_is_a_document_only_when_its_name_says_so(self) -> None:
        for name in ("openapi.json", "openapi3.json", "swagger2.yaml", "apispec_1.json", "oas.yaml", "api-docs.json"):
            self.assertTrue(ingest.looks_like_api_spec(f"https://docs.example/ref/{name}"), name)
        for name in (".well-known/api.json", "capital/data.json", "search-index.json", "rapid.json"):
            self.assertFalse(ingest.looks_like_api_spec(f"https://docs.example/{name}"), name)

    def test_linked_config_files_are_named_so_a_spec_under_another_name_shows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp, "llms.txt")
            index.write_text("## Docs\n- [Guide](https://docs.example/guide.md)\n"
                             "- [Spec](https://docs.example/reference/spec.yaml)\n"
                             "- [API](https://docs.example/openapi.json)\n")
            run = ingest.Run(os.path.join(tmp, "run"))
            run.ensure()
            discovery = ingest.Discovery(run)
            discovery.from_llms_index(index.read_text(), "https://docs.example/llms.txt")
            self.assertEqual(discovery.skipped_types, {".json": 1, ".yaml (not an API spec)": 1})
            self.assertEqual(discovery.skipped_examples[".yaml (not an API spec)"],
                             ["https://docs.example/reference/spec.yaml"])

    def test_every_skipped_link_is_kept_beyond_the_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp, "llms.txt")
            links = [f"- [Config {n}](https://docs.example/config/{n}.yaml)" for n in ("a", "b", "c")]
            links.append("- [Spec](https://docs.example/reference/spec.yaml)")
            index.write_text("## Docs\n- [Guide](https://docs.example/guide.md)\n" + "\n".join(links) + "\n")
            code, found = run_main(["discover", str(index), "--run", os.path.join(tmp, "run")])
            self.assertEqual(code, 0)
            self.assertEqual(len(found["skipped_config_examples"][".yaml (not an API spec)"]), 3)
            rows = ingest.read_jsonl(Path(found["skipped_file"]))
            self.assertIn({"location": "https://docs.example/reference/spec.yaml", "kind": ".yaml (not an API spec)"}, rows)
            self.assertEqual(len(rows), 4)
            # A later discover with fewer skipped links replaces the list rather than adding to it.
            index.write_text("## Docs\n- [Guide](https://docs.example/guide.md)\n" + links[0] + "\n")
            _, found = run_main(["discover", str(index), "--run", os.path.join(tmp, "run")])
            self.assertEqual(len(ingest.read_jsonl(Path(found["skipped_file"]))), 1)

    def test_a_remote_index_cannot_point_at_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = ingest.Run(os.path.join(tmp, "run"))
            run.ensure()
            discovery = ingest.Discovery(run)
            text = "## Docs\n- [Guide](guide.md)\n- [Notes](C:/Users/someone/private-notes.txt)\n" \
                "- [Keys](file:///home/someone/.ssh/id_rsa)\n"
            discovery.from_llms_index(text, "https://d.example/llms.txt")
            self.assertEqual([p["url"] for p in discovery.pages], ["https://d.example/guide.md"])
            self.assertFalse(any(p.get("local") for p in discovery.pages))

    def test_an_llms_full_file_without_page_markers_is_one_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "llms-full.txt")
            source.write_text("# Product docs\n\n## Setup\n\nInstall it.\n")
            run_dir = os.path.join(tmp, "run")
            _, found = run_main(["discover", str(source), "--run", run_dir])
            self.assertEqual(found["pages"], 1)
            _, prepared = run_main(["prepare", "--run", run_dir])
            self.assertEqual(prepared["chunks"], 1)

    def test_the_run_directory_is_never_read_as_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "guide.md").write_text("# Guide\n\ntext\n")
            run_dir = os.path.join(tmp, "xmemory-ingest", "site")
            for _ in range(3):
                _, found = run_main(["discover", tmp, "--run", run_dir])
                _, prepared = run_main(["prepare", "--run", run_dir])
            self.assertEqual(found["pages"], 1)
            self.assertEqual(prepared["chunks"], 1)

    def test_a_url_with_a_query_is_fetched_as_given(self) -> None:
        requested = []

        def fake_get(url: str, attempts: int = 3) -> object:
            requested.append(url)
            return ingest.Response(200, "text/html", "<main><h1>API v1</h1><p>" + "Version one. " * 30 + "</p></main>", url)

        original = ingest.http_get
        ingest.http_get = fake_get
        try:
            markdown, _ = ingest.Fetcher().fetch("https://d.example/api?version=v1")
        finally:
            ingest.http_get = original
        self.assertEqual(requested, ["https://d.example/api?version=v1"])
        self.assertIn("# API v1", markdown)

    def test_linked_documents_are_reported_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = ingest.Run(os.path.join(tmp, "run"))
            run.ensure()
            discovery = ingest.Discovery(run)
            text = "## Files\n- [Manual](https://d.example/manual.pdf)\n- [Again](https://d.example/manual.pdf)\n" \
                "- [Notes](https://d.example/notes.rst)\n"
            discovery.from_llms_index(text, "https://d.example/llms.txt")
            self.assertEqual(discovery.skipped_types, {".pdf": 1, ".rst": 1})

    def test_the_query_string_names_a_page(self) -> None:
        self.assertNotEqual(
            ingest.canonical_key("https://wiki.example/index.php?title=A"),
            ingest.canonical_key("https://wiki.example/index.php?title=B"),
        )

    def test_rerunning_discover_keeps_the_saved_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp, "docs")
            (docs / "guides").mkdir(parents=True)
            (docs / "billing").mkdir()
            (docs / "guides" / "setup.md").write_text("# Setup\n\ntext\n")
            (docs / "billing" / "invoice.md").write_text("# Invoice\n\ntext\n")
            run_dir = os.path.join(tmp, "run")
            _, first = run_main(["discover", str(docs), "--run", run_dir, "--include", "guides"])
            self.assertEqual(first["in_scope"], 1)
            self.assertEqual(len(first["in_scope_pages"]), 1)
            self.assertIn("setup.md", first["in_scope_pages"][0])
            _, again = run_main(["discover", str(docs), "--run", run_dir])
            self.assertEqual(again["in_scope"], 1)
            self.assertEqual(again["scope"]["include"], ["guides"])
            _, reset = run_main(["discover", str(docs), "--run", run_dir, "--reset-scope"])
            self.assertEqual(reset["in_scope"], 2)

    def test_a_page_that_cannot_be_read_does_not_stop_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp, "docs")
            docs.mkdir()
            (docs / "a.md").write_text("# A\n\nfine\n")
            (docs / "llms.txt").write_text("# Site\n## Pages\n- [A](a.md)\n- [Gone](missing.md)\n")
            run_dir = os.path.join(tmp, "run")
            _, found = run_main(["discover", str(docs / "llms.txt"), "--run", run_dir])
            self.assertEqual(found["pages"], 2)
            code, prepared = run_main(["prepare", "--run", run_dir])
            self.assertEqual(code, 0)
            self.assertEqual(prepared["chunks"], 1)
            self.assertEqual(prepared["pages_failed_count"], 1)

    def test_scope_filters_by_url_group_or_title(self) -> None:
        pages = [
            {"url": "https://d.example/guides/setup", "group": "guides", "title": "Setup"},
            {"url": "https://d.example/billing/x", "group": "billing", "title": "Invoices"},
        ]
        ingest.apply_scope(pages, ["guides"], [])
        self.assertEqual([p["in_scope"] for p in pages], [True, False])
        ingest.apply_scope(pages, [], ["invoices"])
        self.assertEqual([p["in_scope"] for p in pages], [True, False])


class HtmlTests(unittest.TestCase):
    def test_main_content_headings_lists_code_and_tables(self) -> None:
        document = textwrap.dedent("""\
            <html><head><title>Limits | Docs</title><script>var x = 1;</script></head>
            <body><nav><a href="/">Home</a> Sidebar</nav>
            <main>
              <h1>Limits<a class="hash-link" href="#limits" title="Direct link to Limits"></a></h1>
              <p>See the <a href="/docs/plans">plans page</a>.</p>
              <p>Each <strong>project</strong> has limits.</p>
              <ul><li>Seats: 8</li><li>Projects: 64</li></ul>
              <pre><code class="language-bash">toolctl limits list
            --all</code></pre>
              <table><tr><th>Plan</th><th>Limit</th></tr><tr><td>pro</td><td>8</td></tr></table>
            </main><footer>Copyright</footer></body></html>
            """)
        markdown, title = ingest.html_to_markdown(document)
        self.assertEqual(title, "Limits | Docs")
        self.assertIn("# Limits\n", markdown)
        self.assertIn("See the plans page.", markdown)
        self.assertIn("Each **project** has limits.", markdown)
        self.assertNotIn("](", markdown)  # link targets are dropped, their text kept
        self.assertIn("- Seats: 8\n- Projects: 64", markdown)
        self.assertIn("```bash\ntoolctl limits list\n--all\n```", markdown)
        self.assertIn("| Plan | Limit |\n| --- | --- |\n| pro | 8 |", markdown)
        for chrome in ("Sidebar", "Copyright", "var x"):
            self.assertNotIn(chrome, markdown)

    def test_a_page_whose_content_is_marked_by_role_drops_its_chrome(self) -> None:
        document = (
            '<body><div class="related" role="navigation"><ul><li>index<li>modules</ul></div>'
            '<div class="body" role="main"><h1>Setup\u00b6</h1><p>Install\u200b it first.</p>'
            '<div class="note"><p>Inside the main content.</p></div>'
            # Enough text that the main region is not mistaken for an empty wrapper.
            '<p>' + 'The installer checks the environment and writes a config file. ' * 5 + '</p></div>'
            '<div class="footer">Last updated</div></body>'
        )
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("# Setup\n", markdown)
        self.assertIn("Install it first.", markdown)
        self.assertIn("Inside the main content.", markdown)
        for chrome in ("modules", "Last updated", "\u00b6", "\u200b"):
            self.assertNotIn(chrome, markdown)

    def test_a_skipped_block_inside_the_captured_element_does_not_keep_it_open(self) -> None:
        document = (
            '<body><div role="main"><h1>Title</h1><p>' + "Real content. " * 20 + "</p>"
            '<div id="toc" role="navigation"><div class="toctitle">Contents</div></div>'
            "<p>After the contents.</p></div>"
            '<div id="site-navigation"><h2>Navigation menu</h2></div><div class="footer">Footer text</div></body>'
        )
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("After the contents.", markdown)
        for outside in ("Contents", "Navigation menu", "Footer text"):
            self.assertNotIn(outside, markdown)

    def test_a_table_whose_end_tags_are_left_out_keeps_its_values(self) -> None:
        # HTML lets </th>, </td> and </tr> be omitted; every value must still arrive.
        document = (
            "<main><h1>Limits</h1>" + "<p>" + "Every plan has limits. " * 12 + "</p>"
            "<table><tr><th>Plan<th>Seats<tr><td>Pro<td>8<tr><td>Team<td>50</table></main>"
        )
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("| Plan | Seats |", markdown)
        self.assertIn("| Pro | 8 |", markdown)
        self.assertIn("| Team | 50 |", markdown)

    def test_a_pipe_inside_a_table_cell_stays_in_its_cell(self) -> None:
        document = (
            "<main><h1>Fields</h1><p>" + "Each field has a type. " * 12 + "</p>"
            "<table><tr><th>Field<th>Type<tr><td>id<td><code>string | null</code></table></main>"
        )
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("| id | `string \\| null` |", markdown)

    def test_code_blocks_are_left_exactly_as_written(self) -> None:
        code = 'grep \u00b6 notes.txt\nvalue = "padded  "\n\n\n\nend'
        document = (
            "<main><h1>Search\u00b6</h1><p>" + "Search the notes. " * 12 + "</p>"
            '<pre><code class="language-bash">' + code + "</code></pre></main>"
        )
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("# Search\n", markdown)
        self.assertIn("```bash\n" + code + "\n```", markdown)
        # And the chunk that is sent keeps it too.
        _, chunks = ingest.chunk_page(markdown, "", 6000)
        self.assertIn("```bash\n" + code + "\n```", chunks[0][1])

    def test_a_code_sample_containing_a_fence_line_stays_whole(self) -> None:
        code = "first\n```\ngrep \u00b6\n\n\nlast"
        document = "<main><h1>Fences</h1><p>" + "About fences. " * 15 + "</p><pre><code>" + code + "</code></pre></main>"
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("````\n" + code + "\n````", markdown)
        _, chunks = ingest.chunk_page(markdown, "", 6000)
        self.assertIn("````\n" + code + "\n````", chunks[0][1])

    def test_joiners_that_are_part_of_words_are_kept(self) -> None:
        self.assertEqual(ingest.INVISIBLE.sub("", "a\u200cb\u200dc\u200bd\ufeff"), "a\u200cb\u200dcd")
        self.assertEqual(ingest.clean_heading("Setup\u00b6"), "Setup")
        self.assertEqual(ingest.clean_heading("Legal \u00b6 12"), "Legal \u00b6 12")

    def test_unclosed_tags_inside_skipped_chrome_do_not_swallow_the_page(self) -> None:
        document = "<body><nav><ul><li>A<li>B</ul></nav><h1>Page</h1><p>Real text here.</p></body>"
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("Real text here.", markdown)
        self.assertNotIn("A", markdown.replace("Page", "").replace("Real", ""))
        document = "<main><h1>T</h1><aside><p>Note</aside><h2>Limits</h2><p>Eight seats.</p></main>"
        markdown, _ = ingest.html_to_markdown(document)
        self.assertIn("## Limits", markdown)
        self.assertIn("Eight seats.", markdown)
        self.assertNotIn("Note", markdown)


class WriteLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "fake"
        self.state.mkdir()
        fake = root / "xmemcli"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_XMEMCLI)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fake = str(fake)
        self.docs = root / "docs"
        self.docs.mkdir()
        for name in ("alpha", "beta", "gamma"):
            (self.docs / f"{name}.md").write_text(page(name.title(), {"Usage": f"How {name} works. " * 5}))
        self.run_dir = str(root / "run")
        os.environ["FAKE_XMEMCLI_DIR"] = str(self.state)
        self.mode("ok")
        code, _ = run_main(["discover", str(self.docs), "--run", self.run_dir])
        self.assertEqual(code, 0)
        code, prepared = run_main(["prepare", "--run", self.run_dir])
        self.assertEqual(prepared["chunks"], 3)

    def tearDown(self) -> None:
        os.environ.pop("FAKE_XMEMCLI_DIR", None)
        os.environ.pop("FAKE_XMEMCLI_MODE", None)
        self.tmp.cleanup()

    def mode(self, value: str) -> None:
        os.environ["FAKE_XMEMCLI_MODE"] = value

    def calls(self, command: str) -> int:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text().splitlines() if command in json.loads(line))

    def write(self, *extra: str) -> tuple[int, dict]:
        return run_main(["write", "--run", self.run_dir, "--xmemcli", self.fake, "--instance", INSTANCE,
                         "--concurrency", "1", *extra])

    def test_bulk_write_then_rerun_writes_nothing(self) -> None:
        code, report = self.write("--all")
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 3)
        self.assertEqual(self.calls("--no-wait"), 3)
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 3)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 0)
        self.assertEqual(self.calls("--no-wait"), 3)

    def test_changing_one_section_rewrites_exactly_that_chunk(self) -> None:
        self.write("--all")
        (self.docs / "beta.md").write_text(page("Beta", {"Usage": "Beta changed. " * 5}))
        run_main(["prepare", "--run", self.run_dir])
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE], {
            "completed": 1, "already_written": 2, "failed": 0, "pending": 0, "unknown": 0, "not_attempted": 0,
        })
        _, status = run_main(["status", "--run", self.run_dir])
        self.assertEqual(status["instances"][INSTANCE]["completed"], 3)
        # Every chunk's current version is the one written; none is left outdated.
        self.assertEqual(status["instances"][INSTANCE]["outdated"], 0)

    def test_a_second_writer_on_the_same_run_is_refused(self) -> None:
        lock = Path(self.run_dir, "run.lock")
        lock.write_text(json.dumps({"pid": os.getpid(), "host": ingest.socket.gethostname(), "since": "now"}))
        code, report = self.write("--all")
        self.assertEqual(code, 2)
        self.assertIn("another `write` or `prepare`", report["error"])
        self.assertEqual(self.calls("write"), 0)

    @unittest.skipIf(os.name == "nt", "stale locks are only detected on POSIX")
    def test_a_lock_left_by_a_dead_process_is_taken_over(self) -> None:
        lock = Path(self.run_dir, "run.lock")
        lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": ingest.socket.gethostname(), "since": "then"}))
        code, report = self.write("--all")
        self.assertEqual(code, 0)
        self.assertFalse(lock.exists())

    def test_every_chunk_goes_to_every_instance(self) -> None:
        code, report = run_main(["write", "--run", self.run_dir, "--xmemcli", self.fake, "--instance", INSTANCE,
                                 "--instance", OTHER, "--all"])
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 3)
        self.assertEqual(report["instances"][OTHER]["completed"], 3)

    def test_quota_stops_the_run_and_a_rerun_finishes_the_rest(self) -> None:
        self.mode("quota_after:1")
        code, report = self.write("--all")
        self.assertEqual(code, 3)
        self.assertIn("quota", report["stopped"])
        counts = report["instances"][INSTANCE]
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["not_attempted"], 2)
        self.mode("ok")
        code, report = self.write("--all")
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 2)
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 1)

    def test_a_rate_limit_is_retried(self) -> None:
        self.mode("rate_limit_first")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 2)

    def test_a_rejected_write_is_reported_not_retried(self) -> None:
        self.mode("bad_request")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(code, 1)
        self.assertEqual(report["failures"][0]["error"], "text too long")
        self.assertEqual(self.calls("--no-wait"), 1)

    def test_a_pilot_write_still_in_flight_is_followed_by_its_id_not_sent_again(self) -> None:
        # The server is still extracting when the wait runs out: the write keeps its id, and the
        # same command later picks it up with its report instead of sending it a second time.
        self.mode("pending")
        code, report = self.write("--chunks", self.first_chunk(), "--sync", "--status-timeout", "1")
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["pending"], 1)
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 0)
        self.mode("ok")
        code, report = self.write("--chunks", self.first_chunk(), "--sync")
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(len(report["pilot"]), 1)
        self.assertEqual(self.calls("--no-wait"), 1)

    def test_a_write_accepted_but_still_processing_is_followed_by_its_id(self) -> None:
        self.mode("still_processing")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 0)

    def test_force_waits_for_a_write_still_in_flight_then_sends_again(self) -> None:
        self.mode("pending")
        self.write("--chunks", self.first_chunk(), "--sync", "--status-timeout", "1")
        # Still in flight: --force does not send a second copy beside it.
        code, report = self.write("--chunks", self.first_chunk(), "--sync", "--force", "--status-timeout", "1")
        self.assertEqual(report["instances"][INSTANCE]["pending"], 1)
        self.assertEqual(self.calls("--no-wait"), 1)
        # Once it has landed, --force sends the rewrite, and the report is the rewrite's.
        self.mode("ok")
        code, report = self.write("--chunks", self.first_chunk(), "--sync", "--force")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 2)
        rows = ingest.read_jsonl(Path(self.run_dir, "state.jsonl"))
        completed = [r["write_id"] for r in rows if r["status"] == "completed"]
        self.assertEqual(len(set(completed)), 2)
        calls = [json.loads(line) for line in (self.state / "calls.jsonl").read_text().splitlines()]
        report_reads = [c for c in calls if "write-status" in c and "--verbose" in c]
        self.assertEqual(report_reads[-1][report_reads[-1].index("write-status") + 1], completed[-1])

    def test_a_report_that_cannot_be_read_is_not_shown_as_an_empty_extraction(self) -> None:
        self.mode("report_unreadable")
        saved = ingest.REPORT_RETRY_SECONDS
        ingest.REPORT_RETRY_SECONDS = 0
        try:
            code, report = self.write("--chunks", self.first_chunk(), "--sync")
        finally:
            ingest.REPORT_RETRY_SECONDS = saved
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertIn("could not read the report", report["pilot"][0]["report_error"])
        self.assertEqual(self.calls("--no-wait"), 1)

    def test_a_queueing_call_that_may_have_been_accepted_is_not_resent(self) -> None:
        self.mode("async_502_first")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(self.calls("--no-wait"), 1)
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(self.calls("--no-wait"), 3)
        code, report = self.write("--all", "--resend-unknown")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 2)
        self.assertEqual(self.calls("--no-wait"), 4)

    def test_a_write_that_failed_in_an_unforeseen_way_is_not_resent(self) -> None:
        # An accepted write whose 200 could not be parsed, and an xmemcli that exited unexpectedly, may both
        # have been stored: only a definite rejection is sent again on a later run.
        for mode in ("unreadable_200", "unexpected_exit"):
            self.mode(mode)
            code, report = self.write("--chunks", self.first_chunk(), "--force")
            self.assertEqual(report["instances"][INSTANCE]["unknown"], 1, mode)
        self.mode("ok")
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(self.calls("--no-wait"), 4)

    def test_an_accepted_write_without_an_id_is_not_resent(self) -> None:
        self.mode("no_write_id")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.mode("ok")
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(self.calls("--no-wait"), 3)

    def test_a_rejected_version_does_not_replace_the_stored_one(self) -> None:
        original = page("Beta", {"Usage": "Beta allows 10 seats. " * 5})
        (self.docs / "beta.md").write_text(original)
        run_main(["prepare", "--run", self.run_dir])
        self.write("--all")
        (self.docs / "beta.md").write_text(page("Beta", {"Usage": "Beta allows 20 seats. " * 5}))
        run_main(["prepare", "--run", self.run_dir])
        self.mode("bad_request")
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["failed"], 1)
        (self.docs / "beta.md").write_text(original)
        run_main(["prepare", "--run", self.run_dir])
        self.mode("ok")
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 3)
        self.assertEqual(self.calls("--no-wait"), 4)
        _, status = run_main(["status", "--run", self.run_dir])
        self.assertEqual(status["instances"][INSTANCE]["outdated"], 0)

    def test_a_section_changed_and_changed_back_is_written_again(self) -> None:
        original = page("Beta", {"Usage": "Beta allows 10 seats. " * 5})
        (self.docs / "beta.md").write_text(original)
        run_main(["prepare", "--run", self.run_dir])
        self.write("--all")
        (self.docs / "beta.md").write_text(page("Beta", {"Usage": "Beta allows 20 seats. " * 5}))
        run_main(["prepare", "--run", self.run_dir])
        self.write("--all")
        (self.docs / "beta.md").write_text(original)
        run_main(["prepare", "--run", self.run_dir])
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 5)
        _, status = run_main(["status", "--run", self.run_dir])
        self.assertEqual(status["instances"][INSTANCE]["completed"], 3)

    def test_a_failed_pilot_report_leaves_the_write_completed(self) -> None:
        original = ingest.Writer.pilot_record

        def broken(writer: object, task: object, doc: object) -> dict:
            raise PermissionError("pilot directory is read-only")

        ingest.Writer.pilot_record = broken
        try:
            code, report = self.write("--chunks", self.first_chunk(), "--sync")
        finally:
            ingest.Writer.pilot_record = original
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertIn("read-only", report["pilot"][0]["report_error"])
        code, report = self.write("--chunks", self.first_chunk(), "--sync")
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 1)

    def test_a_resumed_write_the_server_no_longer_knows_is_not_resent(self) -> None:
        self.mode("pending")
        self.write("--chunks", self.first_chunk(), "--status-timeout", "1")
        self.mode("status_not_found")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.mode("ok")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(self.calls("--no-wait"), 1)
        code, report = self.write("--chunks", self.first_chunk(), "--force")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 2)

    def test_a_resumed_pilot_write_still_gets_its_report(self) -> None:
        chunk = ingest.read_jsonl(Path(self.run_dir, "manifest.jsonl"))[0]
        row = {"at": "then", "instance": INSTANCE, "chunk": chunk["id"], "hash": chunk["hash"], "status": "queued",
               "write_id": "00000000-0000-4000-8000-00000000000a"}
        Path(self.run_dir, "state.jsonl").write_text(json.dumps(row) + "\n")
        code, report = self.write("--chunks", chunk["id"], "--sync")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(len(report["pilot"]), 1)
        self.assertEqual(report["pilot"][0]["tokens"], 30.0)
        self.assertEqual(self.calls("--no-wait"), 0)

    def test_prepare_waits_for_a_running_write(self) -> None:
        Path(self.run_dir, "run.lock").write_text(
            json.dumps({"pid": os.getpid(), "host": ingest.socket.gethostname(), "since": "now"})
        )
        code, report = run_main(["prepare", "--run", self.run_dir])
        self.assertEqual(code, 2)
        self.assertIn("another `write` or `prepare`", report["error"])

    def test_a_failed_write_is_retried_once(self) -> None:
        self.mode("fail_status_first")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        statuses = [json.loads(line)["status"] for line in Path(self.run_dir, "state.jsonl").read_text().splitlines()]
        self.assertEqual(statuses, ["sending", "queued", "failed", "sending", "queued", "completed"])

    def test_a_run_killed_while_a_write_was_leaving_does_not_send_it_again(self) -> None:
        # The last row a hard-killed run left for this chunk says it was being sent: it may have
        # landed, so the next run reports it as unknown instead of sending a second copy.
        chunk = ingest.read_jsonl(Path(self.run_dir, "manifest.jsonl"))[0]
        row = {"at": "then", "instance": INSTANCE, "chunk": chunk["id"], "hash": chunk["hash"], "status": "sending"}
        Path(self.run_dir, "state.jsonl").write_text(json.dumps(row) + "\n")
        code, report = self.write("--chunks", chunk["id"])
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        # Held back, but named, so the agent can check that one chunk.
        self.assertEqual([u["chunk"] for u in report["unknown"]], [chunk["id"]])
        self.assertIn("may have landed", report["unknown"][0]["error"])
        self.assertEqual(self.calls("--no-wait"), 0)
        _, status = run_main(["status", "--run", self.run_dir])
        self.assertEqual(status["instances"][INSTANCE]["unknown"], 1)
        code, report = self.write("--chunks", chunk["id"], "--resend-unknown")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 1)

    def test_an_unknown_chunk_found_stored_can_be_marked_written(self) -> None:
        chunks = ingest.read_jsonl(Path(self.run_dir, "manifest.jsonl"))
        unknown = {"at": "then", "instance": INSTANCE, "chunk": chunks[0]["id"], "hash": chunks[0]["hash"], "status": "sending"}
        Path(self.run_dir, "state.jsonl").write_text(json.dumps(unknown) + "\n")
        code, refused = run_main(["write", "--run", self.run_dir, "--instance", INSTANCE, "--all", "--mark-stored"])
        self.assertEqual(code, 2)
        self.assertIn("--mark-stored needs --chunks", refused["error"])
        # It sends nothing, so it works without xmemcli at hand.
        code, marked = run_main(["write", "--run", self.run_dir, "--xmemcli", "no-such-xmemcli", "--instance", INSTANCE,
                                 "--chunks", f"{chunks[0]['id']},{chunks[1]['id']}", "--mark-stored"])
        self.assertEqual(code, 0)
        self.assertEqual([m["chunk"] for m in marked["marked_stored"]], [chunks[0]["id"]])
        self.assertEqual(marked["not_unknown"][0]["status"], "not_written")
        self.assertEqual(self.calls("--no-wait"), 0)
        # Now it counts as written: neither a rerun nor --resend-unknown sends it again.
        code, report = self.write("--all", "--resend-unknown")
        self.assertEqual(report["instances"][INSTANCE]["already_written"], 1)
        self.assertEqual(self.calls("--no-wait"), len(chunks) - 1)

    def test_mark_stored_touches_only_the_instance_where_the_chunk_is_unknown(self) -> None:
        chunk = ingest.read_jsonl(Path(self.run_dir, "manifest.jsonl"))[0]
        rows = [
            {"at": "then", "instance": INSTANCE, "chunk": chunk["id"], "hash": chunk["hash"], "status": "unknown",
             "error": "it may have landed - connection reset"},
            {"at": "then", "instance": OTHER, "chunk": chunk["id"], "hash": chunk["hash"], "status": "completed", "write_id": "w"},
        ]
        Path(self.run_dir, "state.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        # The held chunk is listed with the reason its own log row gave.
        code, report = self.write("--chunks", chunk["id"])
        self.assertEqual(report["unknown"], [{"chunk": chunk["id"], "instance": INSTANCE, "error": "it may have landed - connection reset"}])
        # A check confirms one instance at a time: naming two is refused, so the other is never marked unseen.
        code, refused = run_main(["write", "--run", self.run_dir, "--instance", INSTANCE, "--instance", OTHER,
                                  "--chunks", chunk["id"], "--mark-stored"])
        self.assertEqual(code, 2)
        self.assertIn("takes one --instance", refused["error"])
        code, marked = run_main(["write", "--run", self.run_dir, "--instance", INSTANCE, "--chunks", chunk["id"], "--mark-stored"])
        self.assertEqual(marked["marked_stored"], [{"chunk": chunk["id"], "instance": INSTANCE}])
        code, marked = run_main(["write", "--run", self.run_dir, "--instance", OTHER, "--chunks", chunk["id"], "--mark-stored"])
        self.assertEqual(marked["not_unknown"], [{"chunk": chunk["id"], "instance": OTHER, "status": "completed"}])

    def test_a_lost_connection_stops_the_run_instead_of_leaving_every_chunk_unknown(self) -> None:
        self.mode("network_down")
        code, report = self.write("--all")
        self.assertEqual(code, 3)  # a stopped run, as for an exhausted quota
        self.assertIn("could not be reached", report["stopped"])
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)
        self.assertEqual(report["instances"][INSTANCE]["not_attempted"], 2)
        self.assertEqual(self.calls("--no-wait"), 1)
        # Once it is back, the same command sends the rest and holds the one in doubt for a check.
        self.mode("ok")
        code, report = self.write("--all")
        self.assertEqual(report["instances"][INSTANCE]["completed"], 2)
        self.assertEqual(report["instances"][INSTANCE]["unknown"], 1)

    def test_a_send_that_was_refused_leaves_the_chunk_as_it_was(self) -> None:
        run = ingest.Run(self.run_dir)
        rows = [
            {"instance": INSTANCE, "chunk": "c1", "hash": "A", "status": "completed", "write_id": "w1"},
            {"instance": INSTANCE, "chunk": "c1", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c1", "hash": "B", "status": "not_sent"},
            {"instance": INSTANCE, "chunk": "c2", "hash": "A", "status": "completed", "write_id": "w2"},
            {"instance": INSTANCE, "chunk": "c2", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c2", "hash": "B", "status": "failed", "error": "HTTP 400"},
            {"instance": INSTANCE, "chunk": "c3", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c3", "hash": "A", "status": "not_sent"},
            # A forced rewrite of the stored version, refused as it was sent.
            {"instance": INSTANCE, "chunk": "c4", "hash": "A", "status": "completed", "write_id": "w4"},
            {"instance": INSTANCE, "chunk": "c4", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c4", "hash": "A", "status": "failed", "error": "HTTP 400"},
            # A forced rewrite of the stored version, accepted and then failed on the server.
            {"instance": INSTANCE, "chunk": "c5", "hash": "A", "status": "completed", "write_id": "w5"},
            {"instance": INSTANCE, "chunk": "c5", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c5", "hash": "A", "status": "queued", "write_id": "w6"},
            {"instance": INSTANCE, "chunk": "c5", "hash": "A", "status": "failed", "write_id": "w6", "error": "x"},
            # A first version refused: it was never stored, so it is due again.
            {"instance": INSTANCE, "chunk": "c6", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c6", "hash": "A", "status": "failed", "error": "HTTP 400"},
            # A stored, then B may have landed (unknown, in flight, or no answer at all), then the
            # section changed back to A and A failed: the instance may hold B, so A is due again.
            {"instance": INSTANCE, "chunk": "c7", "hash": "A", "status": "completed", "write_id": "w7"},
            {"instance": INSTANCE, "chunk": "c7", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c7", "hash": "B", "status": "unknown", "error": "x"},
            {"instance": INSTANCE, "chunk": "c7", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c7", "hash": "A", "status": "queued", "write_id": "w8"},
            {"instance": INSTANCE, "chunk": "c7", "hash": "A", "status": "failed", "write_id": "w8", "error": "x"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "A", "status": "completed", "write_id": "w9"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "B", "status": "pending", "write_id": "w10"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "A", "status": "queued", "write_id": "w11"},
            {"instance": INSTANCE, "chunk": "c8", "hash": "A", "status": "failed", "write_id": "w11", "error": "x"},
            {"instance": INSTANCE, "chunk": "c9", "hash": "A", "status": "completed", "write_id": "w12"},
            {"instance": INSTANCE, "chunk": "c9", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c9", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c9", "hash": "A", "status": "queued", "write_id": "w13"},
            {"instance": INSTANCE, "chunk": "c9", "hash": "A", "status": "failed", "write_id": "w13", "error": "x"},
            # B refused for certain, then a forced A refused: A is still what the instance holds.
            {"instance": INSTANCE, "chunk": "c10", "hash": "A", "status": "completed", "write_id": "w14"},
            {"instance": INSTANCE, "chunk": "c10", "hash": "B", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c10", "hash": "B", "status": "failed", "error": "HTTP 400"},
            {"instance": INSTANCE, "chunk": "c10", "hash": "A", "status": "sending"},
            {"instance": INSTANCE, "chunk": "c10", "hash": "A", "status": "failed", "error": "HTTP 400"},
        ]
        Path(self.run_dir, "state.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        latest = ingest.state_by_instance(run)[INSTANCE]
        self.assertEqual((latest["c1"]["hash"], latest["c1"]["status"]), ("A", "completed"))
        self.assertEqual((latest["c2"]["hash"], latest["c2"]["status"]), ("A", "completed"))
        self.assertNotIn("c3", latest)
        self.assertEqual((latest["c4"]["status"], latest["c4"]["write_id"]), ("completed", "w4"))
        self.assertEqual((latest["c5"]["status"], latest["c5"]["write_id"]), ("completed", "w5"))
        self.assertEqual(latest["c6"]["status"], "failed")
        for chunk in ("c7", "c8", "c9"):
            self.assertEqual((latest[chunk]["hash"], latest[chunk]["status"]), ("A", "failed"), chunk)
        self.assertEqual((latest["c10"]["status"], latest["c10"]["write_id"]), ("completed", "w14"))

    def test_a_write_in_progress_is_polled_until_it_lands(self) -> None:
        # The CLI answers exit 5 with the write's state while it is still in flight; more such
        # answers than the error budget must not give up on the write.
        self.mode("in_progress_status")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(code, 0)
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("write-status"), 7)

    def test_a_queued_write_is_polled_on_rerun_not_sent_again(self) -> None:
        self.mode("pending")
        code, report = self.write("--chunks", self.first_chunk(), "--status-timeout", "1")
        self.assertEqual(report["instances"][INSTANCE]["pending"], 1)
        self.mode("ok")
        code, report = self.write("--chunks", self.first_chunk())
        self.assertEqual(report["instances"][INSTANCE]["completed"], 1)
        self.assertEqual(self.calls("--no-wait"), 1)

    def test_the_pilot_shows_objects_key_folding_and_overwrites(self) -> None:
        code, report = self.write("--chunks", self.first_chunk(), "--sync")
        self.assertEqual(code, 0)
        pilot = report["pilot"][0]
        self.assertIn("CliCommand[command='write'](purpose=\"writes\")", pilot["objects"])
        self.assertIn("CliCommand[command='read'].purpose: \"old text\" -> \"new text\"", pilot["objects"])
        # A field filled on an earlier record is shown, but is not an overwrite.
        self.assertIn("CliCommand[command='read'].usage: (empty) -> \"xmemcli read\"", pilot["objects"])
        self.assertIn("CliCommand(purpose=\"no key here\")", pilot["objects"])
        self.assertEqual(pilot["changed_records"], ["CliCommand(command='write')", "CliCommand", "CliCommand(command='read')"])
        # The pilot write was queued like any other (the fake refuses a waiting write) and its
        # report read back from its status.
        self.assertEqual(self.calls("--no-wait"), 1)
        self.assertTrue(any("CliCommand came out without its key field(s) command" in w for w in pilot["warnings"]))
        self.assertTrue(any("overwrote 1 value(s)" in w and "CliCommand.purpose" in w for w in pilot["warnings"]))
        self.assertEqual(pilot["server_notes"], {"overwritten_field_values": 1})
        self.assertEqual(pilot["tokens"], 30.0)
        self.assertIn(INSTANCE, report["token_estimate"])
        self.assertEqual(report["token_estimate"][INSTANCE]["based_on_chunks"], 1)
        saved = json.loads(Path(pilot["file"]).read_text())
        self.assertEqual(saved["overwrote_existing"][0]["old"], "old text")
        # Nothing but the token count is read from the trace.
        self.assertEqual(self.calls("trace"), 1)

    def test_a_pilot_rewrite_keeps_the_earlier_rounds_file(self) -> None:
        chunk = self.first_chunk()
        self.write("--chunks", chunk, "--sync")
        self.write("--chunks", chunk, "--sync", "--force")
        files = sorted(Path(self.run_dir, "pilot").glob(f"{chunk}__*.json"))
        self.assertEqual(len(files), 2)
        ids = {json.loads(path.read_text())["write_id"] for path in files}
        self.assertEqual(len(ids), 2)

    def test_the_pilot_asks_again_for_a_trace_not_yet_available(self) -> None:
        self.mode("trace_late")
        code, report = self.write("--chunks", self.first_chunk(), "--sync")
        self.assertEqual(code, 0)
        pilot = report["pilot"][0]
        self.assertEqual(pilot["tokens"], 30.0)
        self.assertIn("CliCommand[command='write'](purpose=\"writes\")", pilot["objects"])
        self.assertEqual(self.calls("trace"), 2)

    def test_a_key_that_is_present_is_not_reported_as_folding(self) -> None:
        summary = ingest.summarise_changes(
            {"created": {"objects": [{"name": "Plan", "identifier": "code='pro', tier=None", "fields": []}]}},
            {"Plan": ["code"]},
        )
        self.assertEqual(summary["warnings"], [])
        summary = ingest.summarise_changes(
            {"created": {"objects": [{"name": "Plan", "identifier": "code='pro', tier=None", "fields": []}]}},
            {"Plan": ["code", "tier"]},
        )
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertIn("tier", summary["warnings"][0])

    def test_what_the_pilot_warns_about(self) -> None:
        keys = {"Plan": ["code"]}
        keyless = ingest.summarise_changes({"created": {"objects": [{"name": "Note", "identifier": "#1"}]}}, keys)
        self.assertEqual(keyless["warnings"], [])
        folded = ingest.summarise_changes({"created": {"objects": [{"name": "Plan", "identifier": "#2"}]}}, keys)
        self.assertIn("came out without its key", folded["warnings"][0])
        relinked = ingest.summarise_changes({"deleted": {"objects": [], "relations": [{"name": "offers"}]}}, keys)
        self.assertEqual(relinked["warnings"], [])
        # A previous value the server did not report is not taken for an empty one.
        unreported = ingest.summarise_changes({"updated": {"objects": [{"name": "Plan", "identifier": "code='pro'",
            "fields": [{"name": "price", "new_value": {"string_value": "10"}}]}]}}, keys)
        self.assertEqual(unreported["filled_existing"], [])
        self.assertIn("overwrote 1 value(s)", unreported["warnings"][0])
        shown = ingest.compact_pilot({"chunk": "c", "instance": INSTANCE, "section": "s", **unreported})
        self.assertIn("Plan[code='pro'].price: (not reported) -> \"10\"", shown["objects"])
        deleted = ingest.summarise_changes({"deleted": {"objects": [{"name": "Plan", "identifier": "code='pro'"}]}}, keys)
        self.assertIn("deleted 1 record(s)", deleted["warnings"][0])
        merged = ingest.summarise_changes({"merged_field_conflicts": [{"object": "Plan"}]}, keys)
        self.assertIn("key may be too coarse", merged["warnings"][0])

    def test_sample_and_ask(self) -> None:
        questions = Path(self.tmp.name) / "q.txt"
        questions.write_text("1. How does gamma work?\n# a comment\n- What is beta usage?\n")
        code, sample = run_main(["sample", "--run", self.run_dir, "--questions", str(questions)])
        self.assertEqual([p["title"] for p in sample["picks"]], ["Gamma", "Beta"])
        code, answers = run_main(["ask", "--run", self.run_dir, "--xmemcli", self.fake, "--instance", INSTANCE,
                                  "--questions", str(questions)])
        # xresponse by default: the report lists the selected records with plain values, and
        # counts the ones past the first fifteen.
        shown = answers["answers"][0]["answer"]
        self.assertEqual(shown["records"][0],
                         {"type": "Term", "id": "term='t0'",
                          "fields": {"term": "t0", "definition": "answer to How does gamma work?"}})
        self.assertEqual((len(shown["records"]), shown["more_records"]), (15, 2))
        self.assertTrue(Path(answers["file"]).exists())
        # The answers file keeps every record.
        self.assertEqual(len(ingest.read_jsonl(Path(answers["file"]))[0]["answer"]["objects"]), 17)
        code, single = run_main(["ask", "--run", self.run_dir, "--xmemcli", self.fake, "--instance", INSTANCE,
                                 "--questions", str(questions), "--read-mode", "single"])
        self.assertEqual(single["answers"][0]["answer"], "answer to How does gamma work?")
        # Every later round keeps the earlier rounds' answers.
        one = Path(self.tmp.name) / "one.txt"
        one.write_text("What is beta usage?\n")
        run_main(["ask", "--run", self.run_dir, "--xmemcli", self.fake, "--instance", INSTANCE, "--questions", str(one)])
        saved = ingest.read_jsonl(Path(answers["file"]))
        self.assertEqual([row["question"] for row in saved],
                         ["How does gamma work?", "What is beta usage?", "How does gamma work?", "What is beta usage?",
                          "What is beta usage?"])
        self.assertTrue(all(row.get("asked_at") for row in saved))

    def test_a_long_answer_is_shown_within_its_budget(self) -> None:
        answer = {"objects": [{"name": "Concept", "identifier": "", "fields": [
            {"name": "explanation", "value": {"string_value": "x" * 1000}},
            {"name": "rank", "value": {"int_value": n}},
            {"name": "type", "value": {"string_value": "field named type"}}]} for n in range(20)],
            "relations": []}
        shown = ingest.records_shown(answer)
        self.assertLess(len(shown["records"]), 15)
        self.assertEqual(len(shown["records"]) + shown["more_records"], 20)
        self.assertLessEqual(len(json.dumps(shown["records"])), 3500)
        first = shown["records"][0]
        self.assertEqual((first["type"], first["fields"]["type"], first["fields"]["rank"]),
                         ("Concept", "field named type", 0))
        self.assertEqual(len(first["fields"]["explanation"]), 204)
        # Nothing selected is shown as such, not as an error.
        self.assertEqual(ingest.records_shown({"objects": [], "relations": []}), {"records": []})

    def first_chunk(self) -> str:
        manifest = ingest.read_jsonl(Path(self.run_dir, "manifest.jsonl"))
        return manifest[0]["id"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
