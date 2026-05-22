"""Search-style read tools: grep_files.

See `docs/specs/tool-dispatcher.md` §5.5. `grep_files` lets the agent find
references across the workspace without dumping whole files into the
transcript — a tool call adds a full LLM round-trip carrying the result,
so reading more than necessary is paid for twice (once when called, again
on every subsequent turn until compaction).
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.tools.errors import ToolExecutionError
from metis.core.tools.protocol import ToolContext, ToolOutput
from metis.core.tools.workspace import WorkspaceEscapeError

# Directories pruned during walks. Conservative list — common build / cache
# trees that explode result counts without adding signal. Source files
# inside these dirs remain readable via `read_file` if explicitly asked.
_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "site",
        ".next",
        ".cache",
    }
)

_DEFAULT_MAX_RESULTS = 50
_HARD_MAX_RESULTS = 200
_MAX_SNIPPET_CHARS = 300
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB; files larger than this are skipped.


class GrepFilesTool:
    definition = ToolDefinition(
        name="grep_files",
        description=(
            "Search the workspace for a Python regular expression and return "
            "matching lines as `path:line: snippet`. Prefer this over reading "
            "whole files when looking for a specific reference — much cheaper "
            "in tokens than dumping a file just to find one line. Skips "
            "common build / cache dirs (.git, __pycache__, .venv, "
            "node_modules, dist, build, etc.), binary files, and files larger "
            "than 5 MB. Results are capped at `max_results` (default 50, "
            "hard ceiling 200); refine `pattern` or `path_glob` if truncated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression to match per line.",
                },
                "path_glob": {
                    "type": "string",
                    "description": (
                        "Optional fnmatch glob to limit searched paths "
                        "(workspace-relative), e.g. '**/*.py'."
                    ),
                },
                "case_sensitive": {
                    "type": "boolean",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _HARD_MAX_RESULTS,
                    "default": _DEFAULT_MAX_RESULTS,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
    )

    async def cancel(self) -> bool:
        # File-system search is fast; cooperative cancel only at start.
        return True

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        pattern_text = input["pattern"]
        path_glob = input.get("path_glob")
        case_sensitive = bool(input.get("case_sensitive", False))
        max_results = int(input.get("max_results", _DEFAULT_MAX_RESULTS))
        max_results = max(1, min(max_results, _HARD_MAX_RESULTS))

        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern_text, flags)
        except re.error as exc:
            raise ToolExecutionError(
                f"invalid regex {pattern_text!r}: {exc}",
                tool_use_id=context.tool_use_id,
                underlying=exc,
            ) from exc

        root = Path(context.workspace_files.workspace_root)
        hits: list[str] = []
        files_scanned = 0
        truncated = False

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # In-place prune so os.walk doesn't descend into excluded dirs.
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            for fn in filenames:
                full = Path(dirpath) / fn
                try:
                    rel = full.relative_to(root)
                except ValueError:
                    continue
                rel_str = str(rel)
                if path_glob and not fnmatch(rel_str, path_glob):
                    continue
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                if size > _MAX_FILE_BYTES:
                    continue
                # Route through workspace_files.read for containment safety
                # (rejects symlinks pointing outside the workspace) and to
                # let UnicodeDecodeError flag binary files.
                try:
                    text = context.workspace_files.read(rel_str)
                except (WorkspaceEscapeError, UnicodeDecodeError, OSError):
                    continue
                files_scanned += 1
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        if len(line) > _MAX_SNIPPET_CHARS:
                            snippet = line[:_MAX_SNIPPET_CHARS] + "..."
                        else:
                            snippet = line
                        hits.append(f"{rel_str}:{line_no}: {snippet}")
                        if len(hits) >= max_results:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        if not hits:
            text_out = f"(no matches for {pattern_text!r}; scanned {files_scanned} files)"
        else:
            header = f"{len(hits)} match{'es' if len(hits) != 1 else ''}"
            if truncated:
                header += (
                    f" (truncated at max_results={max_results}; "
                    "refine pattern or path_glob for more)"
                )
            text_out = header + "\n" + "\n".join(hits)
        return ToolOutput(content=[TextBlock(text=text_out)])
