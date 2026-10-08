"""Hand-off from a case runner to verify.py: what ran, and what the trace should look like."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from harness import config


@dataclass
class Check:
    """One run that must exist in the trace.

    `name` and `ancestor` are substrings of run names. `ancestor="ROOT"` means the
    harness root run. `projects` lists every project the run must appear in; a
    replica project is checked with the replica's client and remapped IDs.
    """

    name: str
    ancestor: str = "ROOT"
    projects: list[str] = field(default_factory=lambda: [config.PRIMARY_PROJECT])
    # Expected outcome for negative controls; verify.py still records what happened.
    expect: str = "PASS"


@dataclass
class CaseRun:
    case: str
    title: str
    harness_run: str
    started_at: str
    root_run_id: str | None = None
    checks: list[Check] = field(default_factory=list)
    # Free-form notes from the runner (service responses, errors raised).
    notes: dict = field(default_factory=dict)

    def save(self) -> None:
        out = config.RESULTS_DIR / "runs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.case}.json").write_text(json.dumps(asdict(self), indent=2, default=str))


def load(case: str) -> dict:
    return json.loads((config.RESULTS_DIR / "runs" / f"{case}.json").read_text())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
