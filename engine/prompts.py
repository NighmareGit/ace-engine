"""Engine prompt construction — builds prd_result dict and compiles prompts."""

import json
import os
import tempfile

from engine import Task


def build_prd_result(task, context):
    """
    Build the prd_result dict consumed by ManifestGenerator.generate().

    Args:
        task: Task dataclass instance
        context: Context dataclass with file_tree, relevant_files, imports, framework, existing_code

    Returns:
        prd_result dict with keys: prd_title, tasks (list of 1), project_context
    """
    task_type = (getattr(task, "task_type", "") or "").lower()
    is_research = task_type == "research"

    result = {
        "prd_title": f"Engine run — task {task.id}",
        "tasks": [{
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "module": task.module,
            "files_to_create": list(task.files) if task.files else [task.module],
            "files_to_modify": [],
            "task_type": task.task_type,
            "dependencies": task.dependencies,
            "acceptance_criteria": getattr(task, "acceptance_criteria", []),
            "spec": task.prd_section,
        }],
        "project_context": {
            "project_path": context.file_tree.get("root", ""),
            "framework": context.framework,
        }
    }

    # Research branch: mark the task so the research pipeline (verdict
    # generation, not code generation) is used. The research handler reads
    # task_type directly; this flag is a redundant signal for prompt
    # consumers that branch on it.
    if is_research:
        result["tasks"][0]["is_research"] = True
        result["tasks"][0]["role"] = "researcher"

    return result


def build_project_context(context):
    """
    Build the project_context dict consumed by ManifestGenerator.generate().

    Args:
        context: Context dataclass with file_tree, relevant_files, imports, framework, existing_code

    Returns:
        project_context dict with keys: project_path, file_tree, relevant_files, imports, framework
    """
    return {
        "project_path": context.file_tree.get("root", ""),
        "file_tree": context.file_tree,
        "relevant_files": context.relevant_files,
        "imports": context.imports,
        "framework": context.framework,
    }


def compile_prompt(task_id, prd_result, project_context, retrieved=None):
    """
    Compile a prompt string using ManifestGenerator + PromptCompiler.

    Args:
        task_id: str task ID (e.g. "T01")
        prd_result: dict from build_prd_result()
        project_context: dict from build_project_context()
        retrieved: optional list of Chunk objects (from
            RepoMapBuilder.render() or EmbeddingStore.query()).  When
            provided, assemble_retrieved() injects them into the compiled
            prompt with untrusted-marker guards.  Additive — existing
            callers are unaffected when omitted (the default).

    Returns:
        prompt_text: str — the compiled prompt ready to send to BeeLlama
    """
    from prompt_compiler import PromptCompiler, ManifestGenerator

    # RED-TEAM F7 FIX: mkstemp (mktemp has a name race condition)
    manifest_gen = ManifestGenerator()
    manifest = manifest_gen.generate(prd_result, project_context=project_context)

    fd, manifest_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f)
    try:
        compiler = PromptCompiler(manifest_path)
        prompt_text = compiler.compile(task_id, context={
            "project_path": project_context.get("project_path", ""),
            "file_tree": project_context.get("file_tree", {}),
        })
    finally:
        if os.path.exists(manifest_path):
            os.remove(manifest_path)

    # Retrieval seam (additive — only when retrieved content is provided).
    if retrieved:
        from engine.retrieval.prompt_assemble import assemble_retrieved
        atom_type = (prd_result.get("tasks", [{}])[0].get("task_type", "_default")
                      if prd_result.get("tasks") else "_default")
        budget = 1500  # default budget; assemble_retrieved looks up RETRIEVAL_BUDGETS
        prompt_text = assemble_retrieved(prompt_text, retrieved, budget, atom_type)

    return prompt_text


def append_error_context(prompt_text, error_ctx):
    """
    Append error context to a prompt for retry attempts.

    Args:
        prompt_text: str — existing prompt
        error_ctx: ErrorContext dataclass with previous_code, validation_errors,
                   test_failures, attempt_number

    Returns:
        str — prompt_text with error context appended
    """
    prompt_text += f"\n\n## Previous attempt failed (attempt {error_ctx.attempt_number}):\n"
    if error_ctx.previous_code:
        prompt_text += f"Previous code:\n```python\n{error_ctx.previous_code}\n```\n"
    if error_ctx.validation_errors:
        prompt_text += f"Validation errors:\n" + "\n".join(error_ctx.validation_errors) + "\n"
    if error_ctx.test_failures:
        prompt_text += f"Test failures:\n" + "\n".join(error_ctx.test_failures) + "\n"
    return prompt_text
