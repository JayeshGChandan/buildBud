import json
import pathlib
import subprocess
from typing import Optional, Tuple

from langchain_core.tools import tool

PROJECT_ROOT = pathlib.Path.cwd() / "generated_project"

# Where we persist the last build/update plan so follow-up runs stay consistent.
STATE_FILE = PROJECT_ROOT / ".buildbud" / "state.json"

# Cap how much of each file we feed back to the planner so a large project
# does not blow up the model's context window.
MAX_CONTEXT_FILE_CHARS = 4000


def safe_path_for_project(path: str) -> pathlib.Path:
    p = (PROJECT_ROOT / path).resolve()
    if PROJECT_ROOT.resolve() not in p.parents and PROJECT_ROOT.resolve() != p.parent and PROJECT_ROOT.resolve() != p:
        raise ValueError("Attempt to write outside project root")
    return p


@tool
def write_file(path: str, content: str) -> str:
    """Writes content to a file at the specified path within the project root."""
    p = safe_path_for_project(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"WROTE:{p}"


@tool
def read_file(path: str) -> str:
    """Reads content from a file at the specified path within the project root."""
    p = safe_path_for_project(path)
    if not p.exists():
        return ""
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


@tool
def get_current_directory() -> str:
    """Returns the current working directory."""
    return str(PROJECT_ROOT)


@tool
def list_files(directory: str = ".") -> str:
    """Lists all files in the specified directory within the project root."""
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    p = safe_path_for_project(directory)
    if not p.is_dir():
        return "No files found."
    files = [
        str(f.relative_to(PROJECT_ROOT))
        for f in p.glob("**/*")
        if f.is_file() and ".buildbud" not in f.parts
    ]
    return "\n".join(files) if files else "No files found."

@tool
def run_cmd(cmd: str, cwd: str = None, timeout: int = 30) -> Tuple[int, str, str]:
    """Runs a shell command in the specified directory and returns the result."""
    cwd_dir = safe_path_for_project(cwd) if cwd else PROJECT_ROOT
    res = subprocess.run(cmd, shell=True, cwd=str(cwd_dir), capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def init_project_root():
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    return str(PROJECT_ROOT)


def project_has_files() -> bool:
    """True if generated_project/ already contains real (non-state) files."""
    if not PROJECT_ROOT.exists():
        return False
    for f in PROJECT_ROOT.glob("**/*"):
        if f.is_file() and ".buildbud" not in f.parts:
            return True
    return False


def get_project_context() -> str:
    """Builds a text snapshot of the existing project for the update planner.

    Includes the file tree and each file's contents (truncated), plus the
    saved plan summary (name / tech stack / features) when available.
    """
    parts: list[str] = []

    state = load_project_state()
    if state and state.get("plan"):
        plan = state["plan"]
        parts.append(
            "PREVIOUS PLAN SUMMARY:\n"
            f"- name: {plan.get('name')}\n"
            f"- description: {plan.get('description')}\n"
            f"- techstack: {plan.get('techstack')}\n"
            f"- features: {', '.join(plan.get('features', []))}\n"
        )

    files = sorted(
        f for f in PROJECT_ROOT.glob("**/*")
        if f.is_file() and ".buildbud" not in f.parts
    )
    if not files:
        return "The project is currently empty."

    tree = "\n".join(str(f.relative_to(PROJECT_ROOT)) for f in files)
    parts.append(f"EXISTING FILES:\n{tree}\n")

    for f in files:
        rel = f.relative_to(PROJECT_ROOT)
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            parts.append(f"--- {rel} ---\n[binary or unreadable file]\n")
            continue
        if len(content) > MAX_CONTEXT_FILE_CHARS:
            content = content[:MAX_CONTEXT_FILE_CHARS] + "\n... [truncated]"
        parts.append(f"--- {rel} ---\n{content}\n")

    return "\n".join(parts)


def save_project_state(plan: Optional[dict], task_plan: Optional[dict]) -> None:
    """Persists the latest plan/task plan to .buildbud/state.json."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": plan, "task_plan": task_plan}
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project_state() -> Optional[dict]:
    """Loads the saved state.json, or None if it does not exist / is invalid."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
