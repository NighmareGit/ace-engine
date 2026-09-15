#!/usr/bin/env python3
"""Test suite for prompt templates in the autonomous coding engine.

Validates that system prompts are complete, code extraction is reliable,
prompt building produces correct output, and retry prompts carry context.

Run with:
    cd coder-harness
    python3 -m unittest test_prompts.py -v
"""

import sys
import os
import unittest

# Ensure the coder-harness directory is on the import path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ═══════════════════════════════════════════════════════════════════════
# 1. System Prompt Completeness
# ═══════════════════════════════════════════════════════════════════════


class TestSystemPromptCompleteness(unittest.TestCase):
    """System prompt must include: role, output format, quality requirements, reasoning."""

    @classmethod
    def setUpClass(cls):
        from code_generator import SYSTEM_PROMPT
        cls.prompt = SYSTEM_PROMPT

    def test_output_format_section(self):
        """Prompt must describe the expected output format."""
        self.assertIn("Output", self.prompt)

    def test_file_tag_format(self):
        """Prompt must specify the <file> XML tag format."""
        self.assertIn("<file", self.prompt)

    def test_type_hints_requirement(self):
        """Prompt must require type hints."""
        self.assertIn("Type hints", self.prompt)

    def test_docstrings_requirement(self):
        """Prompt must require docstrings."""
        self.assertIn("Docstrings", self.prompt)

    def test_error_handling_requirement(self):
        """Prompt must require error handling."""
        self.assertIn("Error handling", self.prompt)

    def test_no_placeholders_requirement(self):
        """Prompt must forbid placeholders like TODO or pass."""
        self.assertIn("No placeholders", self.prompt)

    def test_reasoning_section(self):
        """Prompt must include a reasoning/thinking section."""
        self.assertIn("Reasoning", self.prompt)

    def test_language_detection(self):
        """Prompt must support multiple language detection."""
        self.assertIn("Language Detection", self.prompt)
        self.assertIn("Python", self.prompt)
        self.assertIn("TypeScript", self.prompt)


# ═══════════════════════════════════════════════════════════════════════
# 2. Code Extraction Reliability
# ═══════════════════════════════════════════════════════════════════════


class TestExtractCodeValidXML(unittest.TestCase):
    """Extract code from valid XML <file> format."""

    def test_extracts_multiple_files(self):
        from code_generator import extract_code
        response = '''
<file path="src/user.py">
```python
class User:
    def __init__(self, name):
        self.name = name
```
</file>
<file path="src/auth.py">
```python
def login(user):
    return True
```
</file>
'''
        files = extract_code(response)
        self.assertEqual(len(files), 2)
        self.assertIn("src/user.py", files)
        self.assertIn("src/auth.py", files)
        self.assertIn("class User", files["src/user.py"])
        self.assertIn("def login", files["src/auth.py"])

    def test_preserves_code_indentation(self):
        from code_generator import extract_code
        response = '''
<file path="nested.py">
```python
class Foo:
    def bar(self):
        if True:
            return 42
```
</file>
'''
        files = extract_code(response)
        self.assertIn("nested.py", files)
        self.assertIn("    def bar(self):", files["nested.py"])
        self.assertIn("        if True:", files["nested.py"])


class TestExtractCodeNoFiles(unittest.TestCase):
    """Handle response with no code blocks."""

    def test_empty_string(self):
        from code_generator import extract_code
        files = extract_code("")
        self.assertEqual(len(files), 0)

    def test_plain_text(self):
        from code_generator import extract_code
        files = extract_code("I don't know how to implement this.")
        self.assertEqual(len(files), 0)

    def test_thinking_only(self):
        from code_generator import extract_code
        files = extract_code("Let me think about this problem... Hmm, it's complex.")
        self.assertEqual(len(files), 0)


class TestExtractCodeMalformed(unittest.TestCase):
    """Handle malformed XML gracefully."""

    def test_missing_closing_file_tag(self):
        from code_generator import extract_code
        files = extract_code(
            '<file path="test.py">\n```python\nprint("hi")\n```'
        )
        # Should still extract what it can without crashing
        self.assertIsInstance(files, dict)

    def test_missing_opening_file_tag(self):
        from code_generator import extract_code
        files = extract_code('```python\nprint("hi")\n```')
        # No <file> tag means extraction returns empty
        self.assertEqual(len(files), 0)

    def test_nested_backticks(self):
        from code_generator import extract_code
        response = '''
<file path="example.py">
```python
# This is a comment with ``` inside
x = "```"
```
</file>
'''
        files = extract_code(response)
        # Should not crash, even if content is mangled
        self.assertIsInstance(files, dict)


class TestExtractCodeMultipleLanguages(unittest.TestCase):
    """Extract code from different language blocks."""

    def test_typescript(self):
        from code_generator import extract_code
        response = '''
<file path="app.ts">
```typescript
const x: number = 5;
```
</file>
'''
        files = extract_code(response)
        self.assertIn("app.ts", files)
        self.assertIn("const x: number = 5", files["app.ts"])

    def test_javascript(self):
        from code_generator import extract_code
        response = '''
<file path="app.js">
```javascript
const x = 5;
```
</file>
'''
        files = extract_code(response)
        self.assertIn("app.js", files)

    def test_bash(self):
        from code_generator import extract_code
        response = '''
<file path="setup.sh">
```bash
#!/bin/bash
echo "hello"
```
</file>
'''
        files = extract_code(response)
        self.assertIn("setup.sh", files)

    def test_python_without_lang_tag(self):
        from code_generator import extract_code
        response = '''
<file path="utils.py">
```
def helper():
    pass
```
</file>
'''
        files = extract_code(response)
        self.assertIn("utils.py", files)

    def test_unsupported_language_tag(self):
        """CSS is not in the regex language list, but the <file> tag still matches."""
        from code_generator import extract_code
        response = '''
<file path="style.css">
```css
body { margin: 0; }
```
</file>
'''
        files = extract_code(response)
        # CSS lang tag isn't in the regex, so it won't match via the normal pattern.
        # The key point is no crash.
        self.assertIsInstance(files, dict)


# ═══════════════════════════════════════════════════════════════════════
# 3. Prompt Building
# ═══════════════════════════════════════════════════════════════════════


class TestBuildPromptMinimal(unittest.TestCase):
    """Build prompt with minimal task info (no context)."""

    def test_contains_title(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test Task", "description": "Create a hello world function"}
        prompt = gen._build_prompt(task)
        self.assertIn("Test Task", prompt)

    def test_contains_description(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Create a hello world function"}
        prompt = gen._build_prompt(task)
        self.assertIn("hello world", prompt.lower())

    def test_not_empty(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        prompt = gen._build_prompt(task)
        self.assertGreater(len(prompt), 100)

    def test_contains_system_instructions_header(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        prompt = gen._build_prompt(task)
        self.assertIn("## System Instructions", prompt)

    def test_contains_task_header(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        prompt = gen._build_prompt(task)
        self.assertIn("## Task", prompt)

    def test_contains_verification_checklist(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        prompt = gen._build_prompt(task)
        self.assertIn("## Verification Checklist", prompt)

    def test_default_title_when_missing(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"description": "Do something"}
        prompt = gen._build_prompt(task)
        self.assertIn("Untitled", prompt)

    def test_default_description_when_missing(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test"}
        prompt = gen._build_prompt(task)
        self.assertIn("No description", prompt)


class TestBuildPromptWithContext(unittest.TestCase):
    """Build prompt with project context."""

    def test_includes_project_path(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Add user model", "description": "Create User class"}
        context = {
            "project_path": "/home/<user>/projects/myapp",
            "project_tree": ["src/", "src/models/", "tests/"],
            "existing_files": {"src/__init__.py": "# init"},
        }
        prompt = gen._build_prompt(task, context)
        self.assertIn("myapp", prompt)

    def test_includes_project_tree(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Add user model", "description": "Create User class"}
        context = {
            "project_path": "/home/<user>/projects/myapp",
            "project_tree": ["src/", "src/models/", "tests/"],
            "existing_files": {},
        }
        prompt = gen._build_prompt(task, context)
        self.assertIn("src/", prompt)
        self.assertIn("tests/", prompt)

    def test_includes_existing_files(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Add user model", "description": "Create User class"}
        context = {
            "project_path": "/home/<user>/projects/myapp",
            "existing_files": {"src/__init__.py": "# init package"},
        }
        prompt = gen._build_prompt(task, context)
        self.assertIn("src/__init__.py", prompt)
        self.assertIn("# init package", prompt)

    def test_includes_task_description_in_context(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Add user model", "description": "Create User class"}
        context = {"project_path": "/tmp/project"}
        prompt = gen._build_prompt(task, context)
        self.assertIn("User", prompt)

    def test_includes_conventions_when_present(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        context = {"conventions": "Use PEP 8 style"}
        prompt = gen._build_prompt(task, context)
        self.assertIn("## Project Conventions", prompt)
        self.assertIn("PEP 8", prompt)

    def test_includes_code_patterns_when_present(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "Test", "description": "Do something"}
        context = {"code_patterns": "Use dataclasses for models"}
        prompt = gen._build_prompt(task, context)
        self.assertIn("## Existing Code Patterns", prompt)
        self.assertIn("dataclasses", prompt)

    def test_truncates_long_existing_files(self):
        """Existing files with >50 lines should be truncated in the prompt."""
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        long_content = "\n".join([f"line {i}" for i in range(100)])
        task = {"title": "Test", "description": "Do something"}
        context = {"existing_files": {"big.py": long_content}}
        prompt = gen._build_prompt(task, context)
        self.assertIn("more lines", prompt)


class TestBuildPromptWithFilesToCreateModify(unittest.TestCase):
    """Build prompt includes files_to_create and files_to_modify."""

    def test_files_to_create(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {
            "title": "Test",
            "description": "Do something",
            "files_to_create": ["src/models/user.py", "tests/test_user.py"],
        }
        prompt = gen._build_prompt(task)
        self.assertIn("src/models/user.py", prompt)
        self.assertIn("tests/test_user.py", prompt)

    def test_files_to_modify(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {
            "title": "Test",
            "description": "Do something",
            "files_to_modify": ["src/main.py"],
        }
        prompt = gen._build_prompt(task)
        self.assertIn("src/main.py", prompt)


class TestBuildPromptWithAcceptanceCriteria(unittest.TestCase):
    """Build prompt includes acceptance criteria in both task and checklist."""

    def test_acceptance_criteria_in_prompt(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {
            "title": "Test",
            "description": "Do something",
            "acceptance_criteria": ["Returns 200", "Has type hints"],
        }
        prompt = gen._build_prompt(task)
        self.assertIn("Returns 200", prompt)
        self.assertIn("type hints", prompt.lower())

    def test_acceptance_criteria_in_checklist(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {
            "title": "Test",
            "description": "Do something",
            "acceptance_criteria": ["No SQL injection", "Rate limited"],
        }
        prompt = gen._build_prompt(task)
        # Acceptance criteria should appear in verification checklist too
        self.assertIn("No SQL injection", prompt)
        self.assertIn("Rate limited", prompt)


# ═══════════════════════════════════════════════════════════════════════
# 4. Retry Prompt
# ═══════════════════════════════════════════════════════════════════════


class TestRetryPrompt(unittest.TestCase):
    """Retry prompt must carry original task, error details, and previous code."""

    @classmethod
    def setUpClass(cls):
        from code_generator import CodeGenerator
        gen = CodeGenerator(max_retries=3)
        cls.task = {
            "title": "Fix login",
            "description": "Login returns 500 instead of 200",
        }
        cls.error = {
            "status": "failed",
            "summary": "1 failed",
            "test_results": [
                {
                    "name": "test_login",
                    "status": "failed",
                    "error": "500 != 200",
                    "traceback": "assert 500 == 200",
                }
            ],
        }
        cls.previous_files = {"src/auth.py": "def login(): pass"}
        cls.prompt = gen._build_retry_prompt(cls.task, cls.error, cls.previous_files, attempt=1)

    def test_includes_original_title(self):
        self.assertIn("Fix login", self.prompt)

    def test_includes_original_description(self):
        self.assertIn("Login returns 500", self.prompt)

    def test_includes_error_status(self):
        self.assertIn("failed", self.prompt)

    def test_includes_error_summary(self):
        self.assertIn("1 failed", self.prompt)

    def test_includes_previous_code(self):
        self.assertIn("def login()", self.prompt)

    def test_includes_retry_context(self):
        self.assertIn("Retry", self.prompt)
        self.assertIn("attempt 1", self.prompt)

    def test_includes_failed_test_name(self):
        self.assertIn("test_login", self.prompt)

    def test_includes_traceback(self):
        # The traceback field takes precedence over the error field in the prompt
        self.assertIn("assert 500 == 200", self.prompt)

    def test_includes_system_prompt(self):
        """Retry prompt should also include system instructions."""
        self.assertIn("## System Instructions", self.prompt)

    def test_attempt_number_included(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator(max_retries=3)
        task = {"title": "X", "description": "Y"}
        error = {"status": "error", "summary": "crash", "test_results": []}
        prompt = gen._build_retry_prompt(task, error, {}, attempt=3)
        self.assertIn("attempt 3", prompt)

    def test_error_type_syntax(self):
        """When status is 'error' (not 'failed'), different analysis is shown."""
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "X", "description": "Y"}
        error = {"status": "error", "summary": "syntax error", "test_results": []}
        prompt = gen._build_retry_prompt(task, error, {}, attempt=1)
        self.assertIn("syntax or runtime errors", prompt.lower())

    def test_error_type_test_failure(self):
        """When status is 'failed', test failure analysis is shown."""
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        task = {"title": "X", "description": "Y"}
        error = {
            "status": "failed",
            "summary": "tests failed",
            "test_results": [{"name": "test_a", "status": "failed", "error": "bad"}],
        }
        prompt = gen._build_retry_prompt(task, error, {}, attempt=1)
        self.assertIn("tests failed", prompt.lower())
        self.assertIn("test_a", prompt)


# ═══════════════════════════════════════════════════════════════════════
# 5. Task Type Detection (prompt_templates.py — may not exist yet)
# ═══════════════════════════════════════════════════════════════════════


class TestDetectTaskType(unittest.TestCase):
    """Classify tasks by description keywords."""

    def test_bug_fix_detection(self):
        try:
            from prompt_templates import detect_task_type
            self.assertEqual(
                detect_task_type({"description": "Fix the login bug"}), "bug_fix"
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_refactor_detection(self):
        try:
            from prompt_templates import detect_task_type
            self.assertEqual(
                detect_task_type({"description": "Refactor the auth module"}), "refactor"
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_test_detection(self):
        try:
            from prompt_templates import detect_task_type
            self.assertEqual(
                detect_task_type({"description": "Write tests for user model"}),
                "test",
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_documentation_detection(self):
        try:
            from prompt_templates import detect_task_type
            self.assertEqual(
                detect_task_type({"description": "Add README documentation"}),
                "documentation",
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_config_detection(self):
        try:
            from prompt_templates import detect_task_type
            # NOTE: "Create Dockerfile" contains "doc" which matches the
            # documentation check before config. The config keyword list
            # includes "dockerfile" but the documentation list fires first
            # because "doc" is a substring of "dockerfile".
            # Use a description that doesn't contain "doc" to test config detection.
            self.assertEqual(
                detect_task_type({"description": "Create docker-compose.yml"}), "config"
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_dockerfile_detected_as_config(self):
        """Dockerfile should be classified as config, not documentation."""
        try:
            from prompt_templates import detect_task_type
            result = detect_task_type({"description": "Create Dockerfile"})
            self.assertEqual(result, "config")
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")

    def test_code_generation_detection(self):
        try:
            from prompt_templates import detect_task_type
            self.assertEqual(
                detect_task_type({"description": "Implement user registration"}),
                "code_generation",
            )
        except ImportError:
            self.skipTest("prompt_templates.py not yet created")


# ═══════════════════════════════════════════════════════════════════════
# 6. CodeGenerator class defaults and structure
# ═══════════════════════════════════════════════════════════════════════


class TestCodeGeneratorDefaults(unittest.TestCase):
    """CodeGenerator should have sane defaults and expose expected API."""

    def test_default_max_retries(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertEqual(gen.max_retries, 3)

    def test_default_max_tokens(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertEqual(gen.max_tokens, 8192)

    def test_default_temperature(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertAlmostEqual(gen.temperature, 0.3)

    def test_custom_parameters(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator(max_retries=5, max_tokens=4096, temperature=0.7)
        self.assertEqual(gen.max_retries, 5)
        self.assertEqual(gen.max_tokens, 4096)
        self.assertAlmostEqual(gen.temperature, 0.7)

    def test_has_build_prompt(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "_build_prompt", None)))

    def test_has_build_retry_prompt(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "_build_retry_prompt", None)))

    def test_has_generate(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "generate", None)))

    def test_has_extract_code(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "_extract_code", None)))

    def test_has_write_files(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "_write_files", None)))

    def test_has_run_tests(self):
        from code_generator import CodeGenerator
        gen = CodeGenerator()
        self.assertTrue(callable(getattr(gen, "_run_tests", None)))


if __name__ == "__main__":
    unittest.main()
