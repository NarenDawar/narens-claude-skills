import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

SKILL = helpers.REPO_ROOT / "plugins" / "model-bakeoff" / "skills" / "model-bakeoff" / "scripts"
TAX = helpers.REPO_ROOT / "plugins" / "subagent-tax-auditor" / "skills" / "subagent-tax-auditor" / "scripts"
sys.path.insert(0, str(SKILL))
import agent_edit  # noqa: E402
import bk_agents  # noqa: E402

AGENT = (
    "---\n"
    "name: reviewer\n"
    "description: Reviews code for bugs\n"
    "tools: Read, Grep\n"
    "model: sonnet\n"
    "---\n"
    "You review code.\nBe brief.\n"
)


class SyncTests(unittest.TestCase):
    def test_the_editor_copy_is_byte_identical_to_the_tax_auditors(self):
        a = hashlib.sha256((SKILL / "agent_edit.py").read_bytes()).hexdigest()
        b = hashlib.sha256((TAX / "agent_edit.py").read_bytes()).hexdigest()
        self.assertEqual(a, b, "agent_edit.py differs between model-bakeoff and subagent-tax-auditor; copy one over the other")


class ParseTests(unittest.TestCase):
    def test_a_normal_agent(self):
        d = bk_agents.parse_definition(AGENT, "fallback")
        self.assertEqual(d["name"], "reviewer")
        self.assertEqual(d["description"], "Reviews code for bugs")
        self.assertEqual(d["tools"], ["Read", "Grep"])
        self.assertEqual(d["model"], "sonnet")
        self.assertEqual(d["prompt"], "You review code.\nBe brief.")

    def test_defaults_and_quotes(self):
        text = "---\ndescription: 'It says \"hi\"'\n---\nBody\n"
        d = bk_agents.parse_definition(text, "stem")
        self.assertEqual((d["name"], d["tools"], d["model"]), ("stem", None, None))
        self.assertEqual(d["description"], 'It says "hi"')

    def test_a_trailing_comment_and_blank_lines_are_ignored(self):
        text = "---\nname: a # the name\n\n# a comment\ntools: Read # reading\n---\nB\n"
        d = bk_agents.parse_definition(text)
        self.assertEqual((d["name"], d["tools"]), ("a", ["Read"]))

    def test_crlf_and_bom(self):
        text = "﻿---\r\nname: a\r\ntools: Read, Grep\r\n---\r\nBody\r\n"
        d = bk_agents.parse_definition(text)
        self.assertEqual((d["name"], d["tools"], d["prompt"]), ("a", ["Read", "Grep"], "Body"))

    def test_unreadable_frontmatter_is_refused_with_a_reason(self):
        bad = [
            "no frontmatter\n",
            "---\nname: a\n",
            "---\ntools:\n  - Read\n---\nB\n",
            "---\ndescription: |\n  long text\n---\nB\n",
            "---\ndescription: >\n  folded\n---\nB\n",
            "---\nthis is not a key value line\n---\nB\n",
        ]
        for text in bad:
            with self.subTest(text[:30]):
                with self.assertRaises(agent_edit.AgentError):
                    bk_agents.parse_definition(text)


class ReadOnlyTests(unittest.TestCase):
    def test_only_read_tools_are_read_only(self):
        self.assertTrue(bk_agents.is_read_only(["Read", "Grep", "Glob"]))
        self.assertTrue(bk_agents.is_read_only(["Read"]))
        self.assertFalse(bk_agents.is_read_only(["Read", "Bash"]))
        self.assertFalse(bk_agents.is_read_only(["Edit"]))
        self.assertFalse(bk_agents.is_read_only(["mcp__x__y"]))
        self.assertFalse(bk_agents.is_read_only([]))
        self.assertFalse(bk_agents.is_read_only(None))  # lists no tools: inherits everything


class DefinitionJsonTests(unittest.TestCase):
    def test_the_agents_json_has_description_prompt_and_tools_but_no_model(self):
        d = bk_agents.parse_definition(AGENT)
        data = json.loads(bk_agents.agents_json(d))
        self.assertEqual(list(data), ["reviewer"])
        self.assertEqual(data["reviewer"], {"description": "Reviews code for bugs", "prompt": "You review code.\nBe brief.", "tools": ["Read", "Grep"]})

    def test_no_tools_key_when_the_file_lists_none(self):
        d = bk_agents.parse_definition("---\nname: a\n---\nB\n")
        data = json.loads(bk_agents.agents_json(d))
        self.assertEqual(data["a"], {"description": "a", "prompt": "B"})


class ListTests(unittest.TestCase):
    def test_rows_show_tools_and_read_only_and_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "reviewer.md").write_text(AGENT, encoding="utf-8")
            (root / "writer.md").write_text("---\nname: writer\ntools: Read, Edit\n---\nB\n", encoding="utf-8")
            (root / "broken.md").write_text("---\ntools:\n  - Read\n---\nB\n", encoding="utf-8")
            rows = {r["name"]: r for r in bk_agents.list_rows([root])}
        self.assertTrue(rows["reviewer"]["readOnly"])
        self.assertEqual(rows["reviewer"]["tools"], ["Read", "Grep"])
        self.assertFalse(rows["writer"]["readOnly"])
        self.assertIn("problem", rows["broken"])

    def test_load_definition_finds_an_agent_or_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "reviewer.md").write_text(AGENT, encoding="utf-8")
            definition, path = bk_agents.load_definition("reviewer", [root])
            self.assertEqual(definition["name"], "reviewer")
            self.assertEqual(path, root / "reviewer.md")
            with self.assertRaises(agent_edit.AgentError) as ctx:
                bk_agents.load_definition("ghost", [root])
            self.assertIn("ghost", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
