"""
Codebase-Aware Retrieval Subsystem for PR Sentinel.
Extracts symbols, imports, and call-sites from PR diffs and retrieves targeted
cross-file context from the repository without an external vector database.
"""
from dataclasses import dataclass, field
import ast
import logging
import posixpath
import re
from typing import Any, Callable, Coroutine

from app.config import settings

logger = logging.getLogger("pr-sentinel.retrieval")

# Built-in or generic keywords/functions to ignore when searching for cross-file call sites
COMMON_IGNORE_SYMBOLS = {
    "print", "len", "range", "str", "int", "float", "bool", "dict", "list", "set",
    "tuple", "isinstance", "issubclass", "getattr", "setattr", "hasattr", "open",
    "close", "read", "write", "min", "max", "sum", "any", "all", "enumerate", "zip",
    "map", "filter", "super", "type", "id", "repr", "format", "iter", "next",
    "logger", "info", "warning", "error", "debug", "critical", "exception", "log",
    "get", "post", "put", "delete", "patch", "append", "extend", "update", "pop",
    "split", "strip", "replace", "join", "startswith", "endswith", "lower", "upper",
    "json", "loads", "dumps", "dump", "load", "model_validate", "model_dump",
    "asyncio", "sleep", "create_task", "gather", "wait", "run",
    "console", "require", "module", "exports", "window", "document",
}


@dataclass
class ExtractedSymbolReference:
    """Represents a symbol, import, or call reference extracted from a PR diff."""
    name: str
    kind: str  # "import", "class", "function", "call"
    source_file: str
    module: str | None = None
    imported_names: list[str] = field(default_factory=list)


class SymbolExtractor:
    """
    Parses unified diff additions to extract:
    1. Imported modules and imported symbol names
    2. Modified / newly defined classes and functions
    3. External function and method call sites
    """

    @staticmethod
    def extract_added_lines(patch: str) -> list[str]:
        """Extracts added or modified lines (starting with '+', excluding '+++')."""
        if not patch:
            return []
        added = []
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:].rstrip())
        return added

    @classmethod
    def extract_from_file_diff(cls, filename: str, patch: str) -> list[ExtractedSymbolReference]:
        """Extracts symbol references from a single file's patch."""
        added_lines = cls.extract_added_lines(patch)
        if not added_lines:
            return []

        ext = posixpath.splitext(filename)[1].lower()
        if ext == ".py":
            return cls._extract_python_symbols(filename, added_lines)
        elif ext in (".ts", ".tsx", ".js", ".jsx"):
            return cls._extract_js_ts_symbols(filename, added_lines)
        elif ext == ".go":
            return cls._extract_go_symbols(filename, added_lines)
        else:
            return cls._extract_generic_symbols(filename, added_lines)

    @classmethod
    def _extract_python_symbols(cls, filename: str, added_lines: list[str]) -> list[ExtractedSymbolReference]:
        references: list[ExtractedSymbolReference] = []

        # 1. First attempt full AST parsing on combined added lines
        joined_code = "\n".join(added_lines)
        try:
            tree = ast.parse(joined_code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        references.append(ExtractedSymbolReference(
                            name=alias.name,
                            kind="import",
                            source_file=filename,
                            module=alias.name,
                            imported_names=[alias.name],
                        ))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    level = node.level
                    if level > 0:
                        # Preserve relative import even when module is empty:
                        # from . import foo  -> module="."
                        # from .. import foo -> module=".."
                        mod = "." * level + mod  

                    names = [alias.name for alias in node.names if alias.name != "*"]
                    references.append(ExtractedSymbolReference(
                        name=mod,
                        kind="import",
                        source_file=filename,
                        module=mod,
                        imported_names=names,
                    ))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    references.append(ExtractedSymbolReference(
                        name=node.name,
                        kind="function",
                        source_file=filename,
                    ))
                elif isinstance(node, ast.ClassDef):
                    references.append(ExtractedSymbolReference(
                        name=node.name,
                        kind="class",
                        source_file=filename,
                    ))
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id not in COMMON_IGNORE_SYMBOLS:
                        references.append(ExtractedSymbolReference(
                            name=node.func.id,
                            kind="call",
                            source_file=filename,
                        ))
                    elif isinstance(node.func, ast.Attribute) and node.func.attr not in COMMON_IGNORE_SYMBOLS:
                        references.append(ExtractedSymbolReference(
                            name=node.func.attr,
                            kind="call",
                            source_file=filename,
                        ))
            if references:
                return references
        except Exception:
            # Code snippet might have unindented or incomplete syntax; fallback to robust line-by-line regex
            pass

        # Regex fallback for Python
        for line in added_lines:
            stripped = line.strip()
            # from X import Y, Z
            m_from = re.match(r"^from\s+([a-zA-Z0-9_\.]+)\s+import\s+([a-zA-Z0-9_,\s\*]+)", stripped)
            if m_from:
                mod = m_from.group(1)
                names = [n.strip() for n in m_from.group(2).split(",") if n.strip() and n.strip() != "*"]
                references.append(ExtractedSymbolReference(
                    name=mod,
                    kind="import",
                    source_file=filename,
                    module=mod,
                    imported_names=names,
                ))
                continue

            # import X
            m_imp = re.match(r"^import\s+([a-zA-Z0-9_\.,\s]+)", stripped)
            if m_imp:
                modules = [m.strip().split(" as ")[0] for m in m_imp.group(1).split(",") if m.strip()]
                for mod in modules:
                    references.append(ExtractedSymbolReference(
                        name=mod,
                        kind="import",
                        source_file=filename,
                        module=mod,
                        imported_names=[mod],
                    ))
                continue

            # class X
            m_cls = re.match(r"^(?:class)\s+([a-zA-Z0-9_]+)", stripped)
            if m_cls:
                references.append(ExtractedSymbolReference(
                    name=m_cls.group(1),
                    kind="class",
                    source_file=filename,
                ))
                continue

            # def X
            m_func = re.match(r"^(?:async\s+)?def\s+([a-zA-Z0-9_]+)", stripped)
            if m_func:
                references.append(ExtractedSymbolReference(
                    name=m_func.group(1),
                    kind="function",
                    source_file=filename,
                ))
                continue

            # calls: foo(...)
            for call_match in re.finditer(r"\b([a-zA-Z0-9_]{3,})\s*\(", stripped):
                fn_name = call_match.group(1)
                if fn_name not in COMMON_IGNORE_SYMBOLS and not fn_name.startswith("__"):
                    references.append(ExtractedSymbolReference(
                        name=fn_name,
                        kind="call",
                        source_file=filename,
                    ))

        return references

    @classmethod
    def _extract_js_ts_symbols(cls, filename: str, added_lines: list[str]) -> list[ExtractedSymbolReference]:
        references: list[ExtractedSymbolReference] = []
        for line in added_lines:
            stripped = line.strip()
            # import ... from '...'
            m_imp = re.match(r"^import\s+(?:\{([^}]+)\}|([a-zA-Z0-9_$]+)|\*\s+as\s+[a-zA-Z0-9_$]+)\s+from\s+['\"]([^'\"]+)['\"]", stripped)
            if m_imp:
                names = []
                if m_imp.group(1):
                    names = [n.strip().split(" as ")[0] for n in m_imp.group(1).split(",") if n.strip()]
                elif m_imp.group(2):
                    names = [m_imp.group(2).strip()]
                mod = m_imp.group(3)
                references.append(ExtractedSymbolReference(
                    name=mod,
                    kind="import",
                    source_file=filename,
                    module=mod,
                    imported_names=names,
                ))
                continue

            # require('...')
            m_req = re.match(r"(?:const|let|var)\s+(?:\{([^}]+)\}|([a-zA-Z0-9_$]+))\s*=\s*require\(['\"]([^'\"]+)['\"]\)", stripped)
            if m_req:
                names = []
                if m_req.group(1):
                    names = [n.strip() for n in m_req.group(1).split(",") if n.strip()]
                elif m_req.group(2):
                    names = [m_req.group(2).strip()]
                mod = m_req.group(3)
                references.append(ExtractedSymbolReference(
                    name=mod,
                    kind="import",
                    source_file=filename,
                    module=mod,
                    imported_names=names,
                ))
                continue

            # function / class / interface / type definitions
            m_def = re.match(r"^(?:export\s+)?(?:default\s+)?(?:class|interface|type|function)\s+([a-zA-Z0-9_$]+)", stripped)
            if m_def:
                references.append(ExtractedSymbolReference(
                    name=m_def.group(1),
                    kind="class",
                    source_file=filename,
                ))
        return references

    @classmethod
    def _extract_go_symbols(cls, filename: str, added_lines: list[str]) -> list[ExtractedSymbolReference]:
        references: list[ExtractedSymbolReference] = []
        for line in added_lines:
            stripped = line.strip()
            m_imp = re.match(r"^import\s+['\"]([^'\"]+)['\"]", stripped)
            if m_imp:
                mod = m_imp.group(1)
                references.append(ExtractedSymbolReference(
                    name=mod,
                    kind="import",
                    source_file=filename,
                    module=mod,
                    imported_names=[posixpath.basename(mod)],
                ))
        return references

    @classmethod
    def _extract_generic_symbols(cls, filename: str, added_lines: list[str]) -> list[ExtractedSymbolReference]:
        references: list[ExtractedSymbolReference] = []
        for line in added_lines:
            stripped = line.strip()
            # import / include
            m_imp = re.match(r"^(?:#include|import|require)\s+[<\'\"]([^>\'\"]+)[>\'\"]", stripped)
            if m_imp:
                references.append(ExtractedSymbolReference(
                    name=m_imp.group(1),
                    kind="import",
                    source_file=filename,
                    module=m_imp.group(1),
                    imported_names=[],
                ))
        return references


class RepoPathResolver:
    """
    Resolves extracted module and import references into normalized repository file paths.
    Enforces strict path-traversal prevention.
    """

    @staticmethod
    def is_safe_relative_path(path: str) -> bool:
        """Ensures path does not attempt path traversal outside repository root."""
        if not path or not path.strip():
            return False
        normalized = posixpath.normpath(path.strip())
        if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
            return False
        # Disallow Windows drive colon or absolute markers
        if ":" in normalized or "\\" in normalized:
            return False
        # Disallow standalone dot or just an extension e.g. ".py", ".ts"
        if normalized in (".", ".py", ".ts", ".tsx", ".js", ".jsx", ".go"):
            return False
        # Disallow filenames that start with dot extension e.g. "app/.py"
        base = posixpath.basename(normalized)
        if base in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ""):
            return False
        return True

    @classmethod
    def resolve_candidates(
        cls,
        ref: ExtractedSymbolReference,
        existing_changed_files: set[str],
    ) -> list[str]:
        """
        Returns a list of candidate repository paths for a given symbol reference.
        Filters out files that are already part of the PR changed_files.
        """
        module = (ref.module or "").strip()
        if not module:
            logger.debug(
                "Skipping reference with empty module from %s (name=%s, kind=%s)",
                ref.source_file,
                ref.name,
                ref.kind,
            )
            return []

        source_dir = posixpath.dirname(ref.source_file)

        candidates: list[str] = []
        # 1. Relative imports (e.g. .helpers, ..utils, ./component, ../services)
        if module.startswith("."):
            candidates.extend(
                cls._resolve_relative_path(
                    source_dir,
                    module,
                    ref.source_file,
                    imported_names=ref.imported_names,
                )
            )
        else:
            # 2. Package / module imports (e.g. app.models.invoice, src/utils/formatter)
            candidates.extend(cls._resolve_package_path(source_dir, module, ref.source_file))

        # Filter and validate candidates
        valid_candidates: list[str] = []
        for c in candidates:
            if not c or not c.strip():
                continue
            norm = posixpath.normpath(c.strip())
            if not cls.is_safe_relative_path(norm):
                logger.warning("Rejected unsafe or invalid path in retrieval: %s", c)
                continue
            # Do not retrieve files already in the review diff!
            if norm in existing_changed_files:
                continue
            if norm not in valid_candidates:
                valid_candidates.append(norm)

        return valid_candidates

    @classmethod
    def _resolve_relative_path(
        cls,
        source_dir: str,
        module: str,
        source_file: str,
        imported_names: list[str] | None = None,
    ) -> list[str]:
        ext = posixpath.splitext(source_file)[1].lower()
        candidates: list[str] = []

        # Count leading dots for Python
        leading_dots = len(module) - len(module.lstrip("."))
        sub_mod = module.lstrip(".")
        sub_path = sub_mod.replace(".", "/") if sub_mod else ""

        target_dir = source_dir
        # Python relative imports: '.' is current dir, '..' is parent, '...' is parent's parent
        if leading_dots > 1:
            for _ in range(leading_dots - 1):
                target_dir = posixpath.dirname(target_dir)

        if sub_path:
            base_path = posixpath.join(target_dir, sub_path) if target_dir else sub_path
            if ext == ".py":
                candidates.append(f"{base_path}.py")
                candidates.append(posixpath.join(base_path, "__init__.py"))
            elif ext in (".ts", ".tsx", ".js", ".jsx"):
                for js_ext in (".ts", ".tsx", ".js", ".jsx"):
                    candidates.append(f"{base_path}{js_ext}")
                    candidates.append(posixpath.join(base_path, f"index{js_ext}"))
            else:
                candidates.append(base_path)
        else:
            # sub_path is empty (e.g. `from . import foo` or `from .. import bar`)
            if imported_names:
                for name in imported_names:
                    if not name or name == "*":
                        continue
                    name_sub = name.replace(".", "/")
                    name_path = posixpath.join(target_dir, name_sub) if target_dir else name_sub
                    if ext == ".py":
                        candidates.append(f"{name_path}.py")
                        candidates.append(posixpath.join(name_path, "__init__.py"))
                    elif ext in (".ts", ".tsx", ".js", ".jsx"):
                        for js_ext in (".ts", ".tsx", ".js", ".jsx"):
                            candidates.append(f"{name_path}{js_ext}")
                            candidates.append(posixpath.join(name_path, f"index{js_ext}"))
                    else:
                        candidates.append(name_path)

            # Package __init__.py if target_dir is present or top-level package
            pkg_init = posixpath.join(target_dir, "__init__.py") if target_dir else "__init__.py"
            if ext == ".py":
                candidates.append(pkg_init)

            if target_dir:
                if ext == ".py":
                    candidates.append(f"{target_dir}.py")
                elif ext in (".ts", ".tsx", ".js", ".jsx"):
                    for js_ext in (".ts", ".tsx", ".js", ".jsx"):
                        candidates.append(f"{target_dir}{js_ext}")

        return candidates

    @classmethod
    def _resolve_package_path(cls, source_dir: str, module: str, source_file: str) -> list[str]:
        ext = posixpath.splitext(source_file)[1].lower()
        candidates: list[str] = []

        clean_mod = module.strip()
        if not clean_mod:
            return []

        # For Python: e.g. "app.models.invoice" -> "app/models/invoice.py"
        slashed = clean_mod.replace(".", "/")
        if not slashed or slashed in (".", "/"):
            return []

        if ext == ".py":
            candidates.append(f"{slashed}.py")
            candidates.append(posixpath.join(slashed, "__init__.py"))
            # If source_dir has a root prefix (e.g. "backend/app/services/foo.py" where module is "app.models.invoice")
            if source_dir:
                parts = source_dir.split("/")
                for i in range(1, len(parts)):
                    prefix = "/".join(parts[:i])
                    if prefix:
                        candidates.append(posixpath.join(prefix, f"{slashed}.py"))
                        candidates.append(posixpath.join(prefix, slashed, "__init__.py"))
        elif ext in (".ts", ".tsx", ".js", ".jsx"):
            clean_mod = clean_mod.lstrip("@/").lstrip("~/")
            if clean_mod:
                for js_ext in (".ts", ".tsx", ".js", ".jsx"):
                    candidates.append(f"{clean_mod}{js_ext}")
                    candidates.append(posixpath.join(clean_mod, f"index{js_ext}"))
                    candidates.append(posixpath.join("src", f"{clean_mod}{js_ext}"))
        else:
            candidates.append(slashed)

        return candidates


class SymbolSlicer:
    """
    Slices raw file content to extract targeted symbol definitions (class, function,
    interface, or signature) rather than including unnecessary lines.
    Enforces strict snippet byte boundaries and escapes markdown code-fences.
    """

    @classmethod
    def slice_content(
        cls,
        content: str,
        target_symbols: list[str],
        max_bytes: int = 2000,
    ) -> str:
        """
        Extracts relevant symbol definitions from file content.
        Falls back to the top of the file if specific symbols are not found.
        """
        if not content:
            return ""

        lines = content.splitlines()

        # 1. Search for target symbol definitions if target_symbols provided
        if target_symbols:
            extracted_sections: list[str] = []
            matched_symbols = set()

            for target in target_symbols:
                if not target or target in matched_symbols:
                    continue
                snippet = cls._extract_symbol_block(lines, target)
                if snippet:
                    extracted_sections.append(snippet)
                    matched_symbols.add(target)

            if extracted_sections:
                combined = "\n\n".join(extracted_sections)
                return cls._sanitize_and_bound(combined, max_bytes)

        # 2. If no target symbols specified or none found, and file is small, keep whole file
        if len(lines) <= 60 and len(content.encode("utf-8")) <= max_bytes:
            return cls._sanitize_and_bound(content, max_bytes)

        # 3. Fallback: take header and top definitions (first 45 lines)
        header_lines = lines[:45]
        header_text = "\n".join(header_lines)
        return cls._sanitize_and_bound(header_text, max_bytes)

    @classmethod
    def _extract_symbol_block(cls, lines: list[str], symbol: str) -> str | None:
        """Locates the definition of symbol (class or def or interface) and captures its block."""
        symbol_pattern = re.compile(
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:class|def|interface|type|function)\s+{re.escape(symbol)}\b"
        )
        start_idx = None
        start_indent = 0

        for idx, line in enumerate(lines):
            if symbol_pattern.search(line):
                start_idx = idx
                start_indent = len(line) - len(line.lstrip())
                break

        if start_idx is None:
            # Check for variable assignment: e.g. "symbol = ..." or "export const symbol = ..."
            var_pattern = re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)?\s*{re.escape(symbol)}\s*[:=]")
            for idx, line in enumerate(lines):
                if var_pattern.search(line):
                    start_idx = idx
                    start_indent = len(line) - len(line.lstrip())
                    break

        if start_idx is None:
            return None

        # Capture block until indentation drops back to start_indent or max 40 lines
        block_lines = [lines[start_idx]]
        for i in range(start_idx + 1, min(len(lines), start_idx + 40)):
            curr_line = lines[i]
            if not curr_line.strip():
                block_lines.append(curr_line)
                continue
            curr_indent = len(curr_line) - len(curr_line.lstrip())
            # In Python / JS, if an unindented top-level definition begins, the block ended
            if curr_indent <= start_indent and re.match(r"^\s*(?:class|def|async\s+def|export|function)\b", curr_line):
                break
            block_lines.append(curr_line)

        return "\n".join(block_lines)

    @staticmethod
    def _sanitize_and_bound(snippet: str, max_bytes: int) -> str:
        """
        Escapes backtick code fences to prevent prompt injection escapes,
        and bounds snippet strictly to max_bytes.
        """
        # Escape markdown code fences inside retrieved snippet (replace ``` with ''')
        sanitized = snippet.replace("```", "'''")

        # Encode and truncate safely without splitting multi-byte UTF-8 sequences
        encoded = sanitized.encode("utf-8")
        if len(encoded) > max_bytes:
            suffix = "\n... [context truncated]"
            suffix_bytes = suffix.encode("utf-8")
            cut_len = max(max_bytes - len(suffix_bytes), 0)
            truncated_bytes = encoded[:cut_len]
            sanitized = truncated_bytes.decode("utf-8", errors="ignore") + suffix

        final_encoded = sanitized.encode("utf-8")
        if len(final_encoded) > max_bytes:
            sanitized = final_encoded[:max_bytes].decode("utf-8", errors="ignore")

        return sanitized.strip()


class RetrievalEngine:
    """
    High-level orchestrator that drives symbol extraction, candidate resolution,
    file content fetching via GitHub API, slicing, and context budget management.
    """

    def __init__(
        self,
        fetch_content_fn: Callable[[str, str, str | None], Coroutine[Any, Any, str | None]],
    ):
        self.fetch_content_fn = fetch_content_fn

    async def retrieve_context_for_pr(
        self,
        repo_full_name: str,
        commit_sha: str | None,
        changed_files: list[dict],
    ) -> list[dict]:
        """
        Extracts references from PR changed files, resolves candidate repository paths,
        fetches file contents at commit_sha, slices relevant symbols, and returns
        bounded retrieved_context list.
        """
        if not getattr(settings, "ENABLE_CODEBASE_RETRIEVAL", True):
            logger.info("Codebase-aware retrieval is disabled via configuration.")
            return []

        if not changed_files:
            return []

        existing_files = {
            f["filename"] for f in changed_files if isinstance(f, dict) and f.get("filename")
        }

        # 1. Extract symbol references from changed files
        all_references: list[ExtractedSymbolReference] = []
        for file_info in changed_files:
            filename = file_info.get("filename", "")
            patch = file_info.get("patch", "")
            if filename and patch:
                refs = SymbolExtractor.extract_from_file_diff(filename, patch)
                all_references.extend(refs)

        if not all_references:
            logger.debug("No cross-file symbols or imports extracted from PR diffs.")
            return []

        # 2. Group candidate paths by reference so alternate candidates (e.g. __init__.py)
        # are only attempted if the primary candidate (.py) is not found
        max_files = getattr(settings, "MAX_RETRIEVED_FILES", 5)
        max_snippet_bytes = getattr(settings, "MAX_RETRIEVED_SNIPPET_BYTES", 2000)
        max_total_bytes = getattr(settings, "MAX_TOTAL_RETRIEVAL_BYTES", 8000)

        retrieved_items: list[dict] = []
        retrieved_paths: set[str] = set()
        total_retrieved_bytes = 0
        fetched_cache: dict[str, str | None] = {}

        for ref in all_references:
            if len(retrieved_paths) >= max_files or total_retrieved_bytes >= max_total_bytes:
                break

            symbols_to_find = ref.imported_names or ([ref.name] if ref.kind in ("class", "function", "call") else [])
            candidates = RepoPathResolver.resolve_candidates(ref, existing_files)
            if not candidates:
                continue

            logger.debug(
                "Source file '%s': extracted %s '%s' (module='%s', symbols=%s) -> candidate paths: %s",
                ref.source_file,
                ref.kind,
                ref.name,
                ref.module,
                symbols_to_find,
                candidates,
            )

            # For this reference, try candidates in order. Once a candidate file is found, stop!
            for cand in candidates:
                if len(retrieved_paths) >= max_files or total_retrieved_bytes >= max_total_bytes:
                    break

                if cand in fetched_cache:
                    content = fetched_cache[cand]
                else:
                    try:
                        content = await self.fetch_content_fn(repo_full_name, cand, commit_sha)
                    except Exception as exc:
                        logger.debug("Error fetching repository file %s: %s", cand, exc)
                        content = None
                    fetched_cache[cand] = content

                if not content:
                    continue  # Try next candidate for this reference

                # File was found! If we already sliced it from another reference, merge symbols
                if cand in retrieved_paths:
                    break

                remaining_budget = max_total_bytes - total_retrieved_bytes
                if remaining_budget <= 20:
                    break

                snippet_budget = min(max_snippet_bytes, remaining_budget)
                sliced = SymbolSlicer.slice_content(content, symbols_to_find, max_bytes=snippet_budget)

                if sliced:
                    item_bytes = len(sliced.encode("utf-8"))
                    if total_retrieved_bytes + item_bytes > max_total_bytes:
                        allowed = max(max_total_bytes - total_retrieved_bytes, 0)
                        sliced = sliced.encode("utf-8")[:allowed].decode("utf-8", errors="ignore").strip()
                        item_bytes = len(sliced.encode("utf-8"))

                    if item_bytes > 0:
                        total_retrieved_bytes += item_bytes
                        retrieved_paths.add(cand)
                        logger.info(
                            "Retrieved context for '%s' from repository file '%s' (symbol(s): %s, %s bytes)",
                            ref.source_file,
                            cand,
                            ", ".join(symbols_to_find) if symbols_to_find else "module",
                            item_bytes,
                        )
                        retrieved_items.append({
                            "path": cand,
                            "symbol": ", ".join(symbols_to_find) if symbols_to_find else "module",
                            "relationship": "imported_definition" if symbols_to_find else "repository_context",
                            "content": sliced,
                        })

                # Since candidate succeeded, break to next reference
                break

        logger.info(
            "Retrieved %s context snippet(s) (%s bytes) across %s candidate file(s)",
            len(retrieved_items),
            total_retrieved_bytes,
            len(retrieved_paths),
        )
        return retrieved_items
