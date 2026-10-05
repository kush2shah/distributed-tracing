"""Check a case's trace in LangSmith and update the results matrix.

    uv run python verify.py 1a [2a ...]     # verify cases, then rebuild the matrix
    uv run python verify.py --matrix        # rebuild results/matrix.md only

For every expected run (a Check in the case record) the verdict is one of:

  PASS          in the root's trace, under the expected ancestor, in the expected project
  WRONG_PARENT  in the root's trace but not under the expected ancestor
  SPLIT         exists, but in a different trace (the "starts a new trace" symptom)
  NOT_REPLICATED in the primary project but missing from the replica project
  MISROUTED     not in the expected project, but found in another harness project
  MISSING       not found anywhere (never ran, errored, or dropped on ingestion)

Ingestion is asynchronous, so verification polls until every check passes or the
verdicts stop changing.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from fnmatch import fnmatch

from langsmith import Client
from langsmith.utils import LangSmithNotFoundError

from harness import config
from harness.results import load

VERIFY_DIR = config.RESULTS_DIR / "verify"
TIMEOUT_S = 120
MIN_WAIT_S = 30
POLL_S = 6


def _clients() -> dict[str, Client]:
    clients = {"primary": Client()}
    if config.REPLICA_API_KEY:
        clients["replica"] = Client(api_key=config.REPLICA_API_KEY, api_url=config.REPLICA_API_URL)
    return clients


def _workspace(project: str) -> str:
    return "replica" if project == config.REPLICA_WS_PROJECT and config.REPLICA_API_KEY else "primary"


# Where a run might land if it is misrouted.
SEARCH = [
    ("primary", config.PRIMARY_PROJECT),
    ("primary", config.REPLICA_PROJECT),
    ("primary", config.REPLICA_WS_PROJECT),
    ("primary", config.ADK_OVERRIDE_PROJECT),
    ("replica", config.REPLICA_WS_PROJECT),
]


SELECTS = ["ID", "NAME", "TRACE_ID", "PARENT_RUN_IDS", "METADATA", "START_TIME", "STATUS", "ERROR"]
_project_ids: dict[tuple[str, str], str | None] = {}


def _project_id(ws: str, client: Client, project: str) -> str | None:
    if (ws, project) not in _project_ids:
        try:
            _project_ids[(ws, project)] = str(client.read_project(project_name=project).id)
        except LangSmithNotFoundError:
            return None  # not created yet; re-check next poll
    return _project_ids[(ws, project)]


async def _runs(ws: str, client: Client, project: str, since: datetime) -> list:
    """All runs started in `project` since the case began. API errors propagate:
    a harness bug must not look like a tracing failure."""
    project_id = _project_id(ws, client, project)
    if project_id is None:
        return []
    page = await client.runs.query(project_ids=[project_id], min_start_time=since, selects=SELECTS, page_size=500)
    return [run async for run in page]


def _metadata(run) -> dict:
    return run.metadata or {}


async def _snapshot(clients: dict[str, Client], since: datetime) -> dict[tuple[str, str], list]:
    targets = [(ws, project) for ws, project in SEARCH if ws in clients]
    results = await asyncio.gather(*(_runs(ws, clients[ws], project, since) for ws, project in targets))
    return dict(zip(targets, results))


def _ours(run, record: dict) -> bool:
    """Exclude runs tagged with another case's correlation ID. Untagged runs (OTel
    spans, framework-created runs) are kept; cases run one at a time."""
    tag = _metadata(run).get("harness_run")
    return tag is None or tag == record["harness_run"]


def _find_root(runs: list, harness_run: str, root_id: str | None):
    roots = [r for r in runs if r.name == "harness_root"]
    for r in roots:
        if root_id and str(r.id) == root_id:
            return r
    for r in roots:  # replicas remap IDs; match on the correlation ID instead
        if _metadata(r).get("harness_run") == harness_run:
            return r
    return roots[0] if len(roots) == 1 else None


def _has_ancestor(run, pattern: str, by_id: dict, root) -> bool:
    """`parent_run_ids` is the full ancestor chain, root first."""
    for ancestor_id in run.parent_run_ids or []:
        if pattern == "ROOT":
            if ancestor_id == root.id:
                return True
        elif (ancestor := by_id.get(ancestor_id)) is not None and fnmatch(ancestor.name, pattern):
            return True
    return False


def _parent_name(run, by_id: dict) -> str:
    parents = run.parent_run_ids or []
    if not parents:
        return "<none: trace root>"
    return by_id[parents[-1]].name if parents[-1] in by_id else str(parents[-1])


def _evaluate(record: dict, snapshot: dict) -> list[dict]:
    verdicts = []
    for check in record["checks"]:
        for project in check["projects"]:
            ws = _workspace(project)
            runs = snapshot.get((ws, project), [])
            root = _find_root(runs, record["harness_run"], record.get("root_run_id"))
            matches = [r for r in runs if fnmatch(r.name, check["name"]) and r.name != "harness_root" and _ours(r, record)]
            verdict = {"name": check["name"], "ancestor": check["ancestor"], "project": project,
                       "expect": check["expect"], "detail": ""}
            if root is None:
                verdict.update(status="MISSING", detail="harness_root not found in this project")
            elif not matches:
                elsewhere = [
                    p for (w, p), rs in snapshot.items()
                    if (w, p) != (ws, project) and any(fnmatch(r.name, check["name"]) and _ours(r, record) for r in rs)
                ]
                if elsewhere == [config.PRIMARY_PROJECT] and project != config.PRIMARY_PROJECT:
                    verdict.update(status="NOT_REPLICATED", detail="only in the primary project")
                elif elsewhere:
                    verdict.update(status="MISROUTED", detail=f"found in {', '.join(elsewhere)}")
                else:
                    verdict.update(status="MISSING")
            else:
                in_trace = [r for r in matches if r.trace_id == root.trace_id]
                by_id = {r.id: r for r in runs if r.trace_id == root.trace_id}
                if not in_trace:
                    other = matches[0]
                    other_root = next((r for r in runs if r.trace_id == other.trace_id and not r.parent_run_ids), None)
                    verdict.update(status="SPLIT", detail=f"own trace rooted at '{other_root.name if other_root else '?'}'")
                elif any(_has_ancestor(r, check["ancestor"], by_id, root) for r in in_trace):
                    verdict.update(status="PASS")
                else:
                    parents = {_parent_name(r, by_id) for r in in_trace}
                    verdict.update(status="WRONG_PARENT", detail=f"parent: {', '.join(sorted(parents))}")
            verdicts.append(verdict)
    return verdicts


async def verify(case: str) -> dict:
    record = load(case)
    if skipped := record["notes"].get("skipped"):
        result = {"case": case, "title": record["title"], "result": "SKIPPED", "as_expected": True,
                  "verdicts": [], "runner_error": None, "skipped": skipped}
        VERIFY_DIR.mkdir(parents=True, exist_ok=True)
        (VERIFY_DIR / f"{case}.json").write_text(json.dumps(result, indent=2))
        print(f"\n{case}: SKIPPED ({skipped})")
        return result
    clients = _clients()
    since = datetime.fromisoformat(record["started_at"]) - timedelta(seconds=5)
    start, history = time.monotonic(), []
    while True:
        verdicts = _evaluate(record, await _snapshot(clients, since))
        statuses = [v["status"] for v in verdicts]
        history.append(statuses)
        elapsed = time.monotonic() - start
        if all(s == "PASS" for s in statuses) or elapsed > TIMEOUT_S:
            break
        if elapsed > MIN_WAIT_S and len(history) >= 3 and history[-1] == history[-2] == history[-3]:
            break
        await asyncio.sleep(POLL_S)

    result = {
        "case": case,
        "title": record["title"],
        "harness_run": record["harness_run"],
        "root_run_id": record.get("root_run_id"),
        "result": "PASS" if all(s == "PASS" for s in statuses) else "FAIL",
        "as_expected": all(v["expect"] == "?" or (v["status"] == "PASS") == (v["expect"] == "PASS") for v in verdicts),
        "verdicts": verdicts,
        "runner_error": record["notes"].get("error"),
    }
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    (VERIFY_DIR / f"{case}.json").write_text(json.dumps(result, indent=2))
    _print(result)
    return result


def _print(result: dict) -> None:
    print(f"\n{result['case']}: {result['result']}  ({result['title']})  root={result['root_run_id']}")
    if result["runner_error"]:
        print(f"  runner error: {result['runner_error']}")
    for v in result["verdicts"]:
        flag = "ok " if v["status"] == "PASS" else "-- "
        exp = "" if v["expect"] == "PASS" else f" [expected {v['expect']}]"
        detail = f"  ({v['detail']})" if v["detail"] else ""
        print(f"  {flag}{v['status']:<12} {v['name']}  under {v['ancestor']}  in {v['project']}{exp}{detail}")


def _sort_key(case: str) -> tuple:
    return (int("".join(c for c in case if c.isdigit()) or 0), case)


def matrix() -> None:
    rows = []
    for path in sorted(VERIFY_DIR.glob("*.json"), key=lambda p: _sort_key(p.stem)):
        r = json.loads(path.read_text())
        problems = "; ".join(
            f"{v['name']} @ {v['project']}: {v['status']}" for v in r["verdicts"] if v["status"] != "PASS"
        ) or r.get("skipped") or "-"
        expected = "yes" if r["as_expected"] else "**no**"
        rows.append(f"| {r['case']} | {r['title']} | **{r['result']}** | {expected} | {problems} |")
    table = "\n".join(
        ["| Case | Setup | One trace? | As expected | Non-passing runs |", "|---|---|---|---|---|", *rows]
    )
    (config.RESULTS_DIR / "matrix.md").write_text(
        "# Results matrix\n\nGenerated by `verify.py`. Per-run detail is in `results/verify/<case>.json`.\n\n" + table + "\n"
    )
    print("\n" + table)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*")
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()
    for case in args.cases:
        asyncio.run(verify(case))
    matrix()
    sys.exit(0)
