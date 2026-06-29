"""Deterministic review file policy and path-rule matching for CR v2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, List


SUPPORTED_EXTENSIONS = {
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".groovy",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".hxx",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".vue",
    ".svelte",
    ".xml",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".env",
    ".gradle",
    ".cmake",
    ".properties",
}

SUPPORTED_FILENAMES = {
    "dockerfile",
    "jenkinsfile",
    "makefile",
    "pom.xml",
    "package.json",
    "cargo.toml",
    "build.gradle",
    "settings.gradle",
}

DEFAULT_EXCLUDE_PATTERNS = (
    "**/*_test.go",
    "**/src/test/java/**/*.java",
    "**/src/test/**/*.kt",
    "**/*.test.{js,jsx,ts,tsx}",
    "**/*.spec.{js,jsx,ts,tsx}",
    "**/__tests__/**",
    "**/test/**/*_test.py",
    "**/tests/**/*_test.py",
    "**/*_test.py",
    "**/*_spec.rb",
    "**/spec/**/*_spec.rb",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*_test.rs",
    "**/*.test.ets",
    "**/oh_modules/**",
)

BLOAT_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "gemfile.lock",
    "cargo.lock",
    "composer.lock",
    "go.sum",
    "gradle.lockfile",
    ".terraform.lock.hcl",
}

BLOAT_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".ico",
    ".bmp",
    ".tif",
    ".tiff",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".jar",
    ".war",
    ".ear",
    ".class",
    ".o",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".wasm",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".mp3",
    ".wav",
    ".flac",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".dump",
    ".har",
    ".map",
    ".snap",
    ".lock",
}

SYSTEM_RULES = (
    ("**/*{mapper,dao}*.xml", "mapper_dao_xml", "references/rules/mapper_dao_xml.md"),
    ("**/pom.xml", "pom_xml", "references/rules/pom_xml.md"),
    ("**/build.gradle", "build_gradle", "references/rules/build_gradle.md"),
    ("**/package.json", "package_json", "references/rules/package_json.md"),
    ("**/*.{yaml,yml}", "yaml", "references/rules/yaml.md"),
    ("**/*.{json,json5}", "json", "references/rules/json.md"),
    ("**/*.properties", "properties", "references/rules/properties.md"),
    ("**/*.java", "java", "references/rules/java.md"),
    ("**/*.{kt,kts}", "kotlin", "references/rules/kotlin.md"),
    ("**/*.{ts,js,tsx,jsx}", "ts_js_tsx_jsx", "references/rules/ts_js_tsx_jsx.md"),
    ("**/*.{c,cpp,cc,cxx,h,hpp,hxx}", "c_cpp", "references/rules/c_cpp.md"),
    ("**/*.rs", "rust", "references/rules/rust.md"),
)


@dataclass(frozen=True)
class ReviewFilePolicy:
    path: str
    supported: bool
    test: bool
    bloat: bool
    skipped: bool
    skip_reason: str = ""


@dataclass(frozen=True)
class ReviewRuleMatch:
    path: str
    rule_names: List[str]
    references: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


def classify_review_file(path: str) -> ReviewFilePolicy:
    lowered = _normalize(path)
    filename = Path(lowered).name
    suffix = Path(lowered).suffix
    supported = suffix in SUPPORTED_EXTENSIONS or filename in SUPPORTED_FILENAMES
    test = _is_test_path(lowered)
    bloat = _is_bloat_path(lowered)
    if test:
        return ReviewFilePolicy(path=path, supported=supported, test=True, bloat=bloat, skipped=True, skip_reason="test file")
    if bloat:
        return ReviewFilePolicy(path=path, supported=supported, test=False, bloat=True, skipped=True, skip_reason="bloat file type")
    if not supported:
        return ReviewFilePolicy(path=path, supported=False, test=False, bloat=False, skipped=True, skip_reason="unsupported file type")
    return ReviewFilePolicy(path=path, supported=True, test=False, bloat=False, skipped=False)


def match_review_rules(paths: Iterable[str]) -> List[ReviewRuleMatch]:
    matches: List[ReviewRuleMatch] = []
    for path in paths:
        lowered = _normalize(path)
        names = ["default"]
        references = ["references/rules/default.md"]
        for pattern, name, reference in SYSTEM_RULES:
            if _matches(pattern, lowered):
                names.append(name)
                references.append(reference)
                break
        matches.append(ReviewRuleMatch(path=path, rule_names=names, references=references))
    return matches


def _is_test_path(lowered_path: str) -> bool:
    if _matches_any(DEFAULT_EXCLUDE_PATTERNS, lowered_path):
        return True
    parts = {part for part in lowered_path.split("/") if part}
    if parts & {"test", "tests", "__tests__", "testdata"}:
        return True
    if lowered_path.startswith(("test/", "tests/", "__tests__/", "spec/", "specs/")):
        return True
    name = Path(lowered_path).name
    return name.endswith(("test.py", "_test.py", "_test.go", ".test.js", ".test.jsx", ".test.ts", ".test.tsx"))


def _is_bloat_path(lowered_path: str) -> bool:
    name = Path(lowered_path).name
    if name in BLOAT_FILENAMES:
        return True
    if lowered_path.endswith((".min.js", ".min.css")):
        return True
    return Path(lowered_path).suffix in BLOAT_EXTENSIONS


def _matches_any(patterns: Iterable[str], lowered_path: str) -> bool:
    return any(_matches(pattern, lowered_path) for pattern in patterns)


def _matches(pattern: str, lowered_path: str) -> bool:
    for expanded in _expand_braces(pattern.lower()):
        if fnmatchcase(lowered_path, expanded) or fnmatchcase("/" + lowered_path, expanded):
            return True
        if expanded.startswith("**/") and fnmatchcase(lowered_path, expanded[3:]):
            return True
    return False


def _expand_braces(pattern: str) -> List[str]:
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    end = pattern.find("}", start)
    if end < 0:
        return [pattern]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    return [prefix + item + suffix for item in pattern[start + 1 : end].split(",")]


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lower().lstrip("./")
