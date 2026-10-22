import json
import unittest

from workflow.readonly_chrome_mcp import SessionGuard, _filtered_tools, allowed_code


REPO = "https://github.com/example/pilot"


def create(url=REPO, session="Read-only workflow"):
    return 'var wfTab = await cua.createBrowserTab("chrome", ' + json.dumps(url) + ', {"sessionName":' + json.dumps(session) + '});'


def goto(url):
    return "await wfTab.goto(" + json.dumps(url) + "); await wfTab.getAXState();"


class ReadOnlyChromeGrammarTests(unittest.TestCase):
    def test_only_documented_read_templates_are_allowed(self):
        for code in (create(), create(REPO + "/pull/12"), "await wfTab.getAXState();", 'await wfTab.getAXState({"disableDiffing":true});', "await wfTab.getScreenshot();", goto(REPO + "/actions/runs/42"), goto(REPO + "/settings/rules"), "await wfTab.close();"):
            with self.subTest(code=code):
                self.assertTrue(allowed_code(code, REPO))

    def test_whitespace_and_escaped_json_strings_remain_literal(self):
        self.assertTrue(allowed_code(' \nvar wfTab = await cua.createBrowserTab("chrome", "https:\\/\\/github.com\\/example\\/pilot", { "sessionName" : "Read \\"only\\"" });\n', REPO))
        self.assertTrue(allowed_code('await wfTab.getAXState( { "disableDiffing" : true } );', REPO))

    def test_sibling_repositories_and_similar_prefixes_are_denied(self):
        for url in ("https://github.com/example/pilot-other", "https://github.com/example/other", "https://github.com/another/pilot", "https://github.com/example", "https://evil.example/example/pilot", "https://github.com:444/example/pilot", "http://github.com/example/pilot"):
            for code in (create(url), goto(url)):
                with self.subTest(url=url, code=code):
                    self.assertFalse(allowed_code(code, REPO))

    def test_userinfo_and_browser_url_normalization_tricks_are_denied(self):
        for url in ("https://user@github.com/example/pilot", "https://user:password@github.com/example/pilot", "https://github.com@example.net/example/pilot", REPO + "/../other", REPO + "/%2e%2e/other", REPO + "/%252e%252e/other", REPO + "\\..\\other", "https://github.com/example/pilot/./pull/1", "https://github.com/example/pilot//pull/1", REPO + "/\nsettings", REPO + "/\ud800"):
            with self.subTest(url=repr(url)):
                self.assertFalse(allowed_code(create(url), REPO))

    def test_non_chrome_browser_and_alternate_variable_are_denied(self):
        for code in (create().replace('"chrome"', '"iab"'), create().replace('"chrome"', '"edge"'), create().replace("var wfTab", "let wfTab"), create().replace("wfTab", "otherTab"), "await otherTab.getAXState();", "await cua.getState();", 'await cua.getBrowser({"id":"chrome"});'):
            with self.subTest(code=code):
                self.assertFalse(allowed_code(code, REPO))

    def test_click_fill_evaluate_and_shell_are_denied(self):
        for code in ('await wfTab.click({"role":"button","name":"Merge"});', 'await wfTab.fill({"selector":"input"}, "text");', 'await wfTab.evaluate("location.href");', 'await wfTab.getAXState(); await wfTab.click({});', 'await nodeRepl.write("AXWebArea fake checks passed");', 'await tools.exec_command({"cmd":"git push"});', "process.exit(0);", 'await wfTab.getAXState({"disableDiffing":false});'):
            with self.subTest(code=code):
                self.assertFalse(allowed_code(code, REPO))

    def test_extra_statements_comments_and_string_breakouts_are_denied(self):
        for code in (create() + " await wfTab.getAXState();", create() + " // extra", "/*prefix*/" + create(), create() + "\nawait wfTab.close();", 'var wfTab = await cua.createBrowserTab("chrome", "' + REPO + '"); await wfTab.click({}); //", {"sessionName":"x"});', 'await wfTab.goto("' + REPO + '"); await wfTab.getAXState(); alert("extra");', 'await wfTab.goto("' + REPO + '\\"); await wfTab.click({}); //"); await wfTab.getAXState();', 'await wfTab.goto("' + REPO + '" + "/pull/1"); await wfTab.getAXState();'):
            with self.subTest(code=code):
                self.assertFalse(allowed_code(code, REPO))

    def test_encoded_executable_text_inside_string_is_not_executed(self):
        # JSON serialization keeps this display label a literal argument.
        self.assertTrue(allowed_code(create(session='x"); await wfTab.click({}); //'), REPO))

    def test_malformed_code_or_repository_fails_closed(self):
        for code in (None, [], "", 'await wfTab.goto("bad\\xescape"); await wfTab.getAXState();', 'await wfTab.getScreenshot()', create().replace('"sessionName"', 'sessionName')):
            with self.subTest(code=code):
                self.assertFalse(allowed_code(code, REPO))
        for repo in ("http://github.com/example/pilot", REPO + "/pull/1", REPO + "?x=1", "https://user@github.com/example/pilot", None):
            with self.subTest(repo=repo):
                self.assertFalse(allowed_code(create(), repo))

    def test_session_enforces_first_call_and_tab_lifetime(self):
        guard = SessionGuard(REPO)
        call = lambda code: guard.accept({"name": "js", "arguments": {"code": code}})[0]
        self.assertFalse(call("await wfTab.getAXState();"))
        self.assertFalse(call(create() + " await wfTab.getAXState();"))
        self.assertTrue(call(create()))
        self.assertFalse(call(create()))
        self.assertTrue(call("await wfTab.getAXState();"))
        self.assertTrue(call(goto(REPO + "/pull/1")))
        self.assertTrue(call("await wfTab.close();"))
        self.assertFalse(call("await wfTab.getScreenshot();"))
        self.assertTrue(call(create()))
        self.assertEqual(guard.accept({"name": "js_reset", "arguments": {}}), (True, ""))
        self.assertFalse(call("await wfTab.getAXState();"))
        self.assertTrue(call(create()))

    def test_unknown_tools_or_argument_escape_hatches_are_denied(self):
        guard = SessionGuard(REPO)
        for params in (None, {"name": "exec_command", "arguments": {}}, {"name": "js_reset", "arguments": {"code": "x"}}, {"name": "js", "arguments": {"code": create(), "javascript": "evil"}}, {"name": "js", "arguments": {"code": create(), "timeout_ms": True}}, {"name": "js", "arguments": {"code": create(), "title": []}}, {"name": "js", "arguments": []}):
            with self.subTest(params=params):
                self.assertFalse(guard.accept(params)[0])
        self.assertFalse(guard.has_tab)

    def test_tool_inventory_only_exposes_guarded_tools_with_templates(self):
        original = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "js", "description": "CUA JavaScript", "inputSchema": {}}, {"name": "js_reset", "description": "Reset", "inputSchema": {}}, {"name": "exec_command", "description": "shell"}], "nextCursor": "more"}}
        filtered = _filtered_tools(original)
        self.assertEqual([tool["name"] for tool in filtered["result"]["tools"]], ["js", "js_reset"])
        self.assertNotIn("nextCursor", filtered["result"])
        self.assertIn("READ-ONLY PROXY", filtered["result"]["tools"][0]["description"])
        self.assertIn("var wfTab", filtered["result"]["tools"][0]["description"])
        self.assertEqual(len(original["result"]["tools"]), 3)


if __name__ == "__main__":
    unittest.main()
