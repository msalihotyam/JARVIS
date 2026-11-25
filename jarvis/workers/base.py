import abc
from dataclasses import dataclass
import re
import unicodedata

@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str
    changed_files: tuple[str, ...]
    evidence: tuple[str, ...]

    def __post_init__(self):
        valid_statuses = {"completed", "partial", "failed", "timeout", "blocked_by_gate"}
        if self.status not in valid_statuses:
            raise ValueError(f"invalid status: {self.status}")

@dataclass(frozen=True)
class AgentProfile:
    name: str
    tools: frozenset[str]
    paths: frozenset[str]
    network: bool
    max_turns: int
    budget_sessions: int

    def __post_init__(self):
        if self.max_turns <= 0:
            raise ValueError("max_turns must be > 0")
        if self.budget_sessions < 0:
            raise ValueError("budget_sessions must be >= 0")

class Worker(abc.ABC):
    @abc.abstractmethod
    def start(self, task: str) -> str:
        pass

    @abc.abstractmethod
    def status(self, run_id: str) -> str:
        pass

    @abc.abstractmethod
    def stop(self, run_id: str) -> None:
        pass

    @abc.abstractmethod
    def collect(self, run_id: str) -> WorkerResult:
        pass

# Aliases Windows resolves to another name: ":" streams/drives, "~" 8.3 short
# names, globs; a part ending in "." or " " is the same file without it.
_PATH_UNSAFE = frozenset(':~*?[]"<>|')
_DEVICE = re.compile(r"con|prn|aux|nul|conin\$|conout\$|com[0-9]|lpt[0-9]")


def _is_device(part: str) -> bool:
    """A DOS device name in any form: case, NFKC lookalikes (COM¹), any extension, "NUL .txt"."""
    folded = unicodedata.normalize("NFKC", part).casefold().replace("ı", "i")
    return _DEVICE.fullmatch(folded.split(".")[0].rstrip(" ")) is not None


def _path_parts(path: object) -> tuple[str, ...] | None:
    """A profile path (repo-relative, "/" = the whole tree) as exact parts; None if unsafe."""
    if type(path) is not str or not path:
        return None
    path = path.replace("\\", "/")
    if path.startswith("//"):  # UNC or device namespace (//server, //./, //?/)
        return None
    parts = tuple(part for part in path.split("/") if part not in ("", "."))
    for part in parts:
        if (part == ".." or part[-1] in ". " or _PATH_UNSAFE & set(part) or not part.isprintable()
                or _is_device(part)):
            return None
    return parts


def _under(path: str, root: str) -> bool:
    """Part-wise and case-exact: a case-only variant is refused, not folded in (fail closed)."""
    child, parent = _path_parts(path), _path_parts(root)
    return child is not None and parent is not None and child[: len(parent)] == parent


def effective_authority(*profiles: AgentProfile) -> AgentProfile:
    """The authority every profile grants: the I9 gate a child is checked against.

    A path is kept only if it lies under some path of every profile, so a child
    path outside its parent never survives, and an unsafe path never does.
    """
    if not profiles:
        raise ValueError("at least one profile is required")

    first = profiles[0]
    tools = first.tools.intersection(*(p.tools for p in profiles[1:]))
    paths = frozenset(
        path for p in profiles for path in p.paths
        if all(any(_under(path, root) for root in other.paths) for other in profiles)
    )
    network = all(p.network for p in profiles)
    max_turns = min(p.max_turns for p in profiles)
    budget_sessions = min(p.budget_sessions for p in profiles)

    return AgentProfile(
        name="effective",
        tools=tools,
        paths=paths,
        network=network,
        max_turns=max_turns,
        budget_sessions=budget_sessions
    )
