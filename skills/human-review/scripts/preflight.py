#!/usr/bin/env python3
"""The gate, then the wipe, then the run's start markers — in that order, or not at all.

A review of a branch that does not build is worse than no review: it is a confident-looking
guide about code nobody has proven compiles, with every number on it measured from a tree of
unknown status. So the branch is pushed and the build for the *exact commit that was pushed*
must be green before anything else happens.

The order is the substance of this script, and it is why it is a script:

  1. **gate** — push, then wait for green **for `$SHA`**, never for "the latest run on the
     branch". A branch almost always has some green run on it, which is what makes
     `gh run list --branch` so tempting and so wrong: it is the confidently-wrong signal
     this whole page exists to avoid. An empty run list is not a pass either — **absence is
     not success** — and a repository with genuinely no CI is let through only if the guide
     then says *"no build proved this"*.
  2. **wipe** — and only now (`assets/`, and the earlier builds' `*.out` logs beside the
     page). Every fragment producer writes to a fixed path and the
     renderer inlines whatever it finds with no freshness check, so a step that fails
     silently leaves the previous run's artifact in place: a green `compatible` seal for a
     diff it never saw. Because the wipe is destructive, it comes *after* the gate — a run
     that stops at the gate leaves the previous guide intact instead of destroying it on the
     way out.
  3. **reset the ledger**, for the same reason and worse. A stale `.steps.json` parses
     perfectly: every tab it names is reported as measured, none of this run's turns fall
     inside last run's windows, and the page prints a confident **`$0.00`** against every
     tab. That reads as "this run was free", not as "we did not measure".
  4. **start markers** — `.started` is what lets the page report what it cost; `.session` is
     what lets a later rebuild read the right transcript instead of publishing the wrong one.

The gate is about the branch **as pushed now, not the tree at the end of the run**. This
skill deliberately leaves the review's own fixes uncommitted for a human to inspect, so this
never means "push the review's fixes too".

**Which** build counts is configured, not guessed from whatever ran: a push triggers several
workflows, and a green Pages deploy proves nothing about the application. `--workflow` (or
`"ci": {"workflows": [...]}` in `human-review.json`) names the authoritative ones and **all**
of them must be green for the exact SHA. Unconfigured, the one workflow named like `ci`/`build`
is used; with none or several, every run on the SHA has to be green. A cancelled run, a
workflow that never got a runner, and GitHub not answering are reported as what they are —
none of them is a red build, and none of them is a pass. The decision lands in
`.human-review/.gate.json` (workflow, run id, SHA, verdict), on a refusal too — and, when
that run has Sonar steps, `sonar` (their verdict, the quality gate, the dashboard URL).

Exit codes:  0 ready · 1 the gate refused · 2 misuse.

Usage:
  preflight.py --base origin/main
  preflight.py --base origin/main --workflow ci.yml
  preflight.py --no-gate          # local experiment; the guide must say the gate was skipped
  preflight.py --ci-only          # re-read Sonar, and CI on the branch head; no push, no wipe
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from hrbuild.shared.forge import api_forge  # noqa: E402

HR = Path(".human-review")


def run(cmd: str, capture=True, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def push_failure(stderr: str) -> str:
    """The lines that say *why*, not the first 300 characters.

    A pre-push hook that lints prints its warnings first — ninety of them on petclinic —
    and the one error that blocked the push last; a prefix of stderr is all warnings and
    reads as "could not push" with no reason. So: the error lines, then the hook's verdict."""
    lines = [l.rstrip() for l in stderr.splitlines() if l.strip()]
    keep = [l for l in lines if re.search(r"\berror\b|❌|rejected|denied|fatal", l, re.I)]
    return "\n".join((keep or lines[-5:])[:12])


#: A workflow "named like CI" — matched as a whole word in the workflow's name or file stem,
#: so `ci.yml`, `CI`, `Java CI with Maven` and `build.yml` qualify and `Skill CLIs` does not.
CI_LIKE = re.compile(r"(?<![a-z0-9])(ci|build)(?![a-z0-9])", re.I)
#: …unless it is plainly a deployment. `pages-build-deployment` has "build" in it and proves
#: nothing about the application, which is the exact confusion this gate exists to prevent.
NOT_CI = re.compile(r"deploy|pages|release|publish", re.I)
RUN_FIELDS = "databaseId,status,conclusion,workflowName,headSha,event,createdAt,url"
#: Statuses in which no runner has picked the job up yet. Still one of these at the deadline
#: means a runner outage, not a red build — and the report has to say which it was.
NOT_STARTED = {"queued", "waiting", "requested", "pending"}


def configured_workflows(cli: list[str], config: str) -> list[str]:
    """`--workflow` wins; else `"ci": {"workflows": [...]}` (or `"ci": "ci.yml"`) in the
    project's `human-review.json`; else nothing, and the gate auto-detects."""
    if cli:
        return list(dict.fromkeys(cli))
    try:
        data = json.loads(Path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ci = data.get("ci") if isinstance(data, dict) else None
    if isinstance(ci, dict):
        ci = ci.get("workflows")
    if isinstance(ci, str):
        ci = [ci]
    return [w for w in (ci or []) if isinstance(w, str) and w.strip()]


def list_workflows() -> list[dict] | None:
    """The repository's active workflows, or None when GitHub could not be asked — which is
    a discovery failure, never "this repository has no CI"."""
    r = api_forge().ci_workflows()
    if r.returncode != 0:
        return None
    try:
        wfs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return [w for w in wfs if isinstance(w, dict) and w.get("state", "active") == "active"]


def auto_detect(workflows: list[dict]) -> list[str]:
    """The one workflow named like ci/build, as its file name. Two candidates is ambiguity,
    and ambiguity is not resolved by guessing: the caller falls back to requiring every run."""
    hits = []
    for w in workflows:
        path = w.get("path") or ""
        if path.startswith("dynamic/"):      # GitHub-managed: pages, dependabot, copilot
            continue
        stem = Path(path).stem
        name = w.get("name") or ""
        if (CI_LIKE.search(name) or CI_LIKE.search(stem)) and not (
                NOT_CI.search(name) or NOT_CI.search(stem)):
            hits.append(Path(path).name or name)
    return hits if len(hits) == 1 else []


def latest_runs(sha: str, workflow: str | None,
                fields: str = RUN_FIELDS) -> tuple[list[dict] | None, str]:
    """Runs for exactly `sha` (optionally of one workflow), newest first. None + the reason
    when GitHub could not be asked."""
    r = api_forge().ci_runs(sha, workflow, fields)
    if r.returncode != 0:
        return None, ((r.stderr or "") + (r.stdout or "")).strip()
    try:
        runs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None, "unparseable `gh run list` output"
    # `--commit` already filters, but the evidence claims an exact SHA, so check it here too.
    runs = [x for x in runs if isinstance(x, dict) and x.get("headSha", sha) == sha]
    runs.sort(key=lambda x: (x.get("createdAt") or "", x.get("databaseId") or 0), reverse=True)
    # A `skipped` run built nothing (e.g. the pull_request twin of a push run, guarded by `if:`),
    # so it must not shadow a run of the same workflow that did build this SHA. Stable sort.
    runs.sort(key=lambda x: (x.get("conclusion") or "").lower() == "skipped")
    return runs, ""


def _evidence(workflow: str, sha: str, one: dict | None, verdict: str, detail: str = "") -> dict:
    one = one or {}
    return {"workflow": workflow, "name": one.get("workflowName") or workflow,
            "runId": one.get("databaseId"), "url": one.get("url"), "sha": sha,
            "event": one.get("event"), "status": one.get("status"),
            "conclusion": one.get("conclusion"), "verdict": verdict,
            **({"detail": detail} if detail else {})}


def _verdict(one: dict) -> str:
    """One completed run → one verdict. Only `failure` is the application failing; a
    cancellation, a timeout or a run that could not start says nothing about the code."""
    c = (one.get("conclusion") or "").lower()
    return {"success": "success", "failure": "failure", "cancelled": "cancelled",
            "timed_out": "timed-out", "startup_failure": "startup-failure"}.get(c, c or "unknown")


def _refusal(e: dict, wait_minutes: float) -> str:
    sha12, name = e["sha"][:12], e["name"]
    run_id = f" (run {e['runId']})" if e.get("runId") else ""
    v = e["verdict"]
    if v == "failure":
        return (f"{name}{run_id} concluded failure for {sha12} — name it and the failing job "
                "in the report, and fix that first")
    if v == "cancelled":
        return (f"{name}{run_id} was cancelled for {sha12} — not a verdict on the code, but "
                "not a pass either; re-run it")
    if v == "queued":
        return (f"{name}{run_id} still {e.get('status')} after {wait_minutes:g} min, never "
                "picked up by a runner — a runner outage, not a failing build; re-run when "
                "runners are back")
    if v == "stuck":
        return f"{name}{run_id} still running after {wait_minutes:g} min — treat it as stuck"
    if v == "not-found":
        return (f"no {name} run for {sha12} after {wait_minutes:g} min, and this repository "
                "does have workflows. Absence is not success")
    if v == "workflow-not-found":
        return (f"configured workflow {e['workflow']!r} does not exist in this repository — "
                f"fix `ci.workflows` / --workflow ({e.get('detail', '')})")
    if v == "discovery-failed":
        return (f"could not ask GitHub about {name} for {sha12} — not a build failure, a "
                f"discovery failure: {e.get('detail', '')}")
    return f"{name}{run_id} concluded {e.get('conclusion') or v} for {sha12} — not a pass"


#: A CI step that runs Sonar: its name says so. Which one is the scan and which the quality
#: gate is told apart by the name too — petclinic's job also has `Cache SonarCloud packages`
#: and its `Post …` twin, which say "sonar" and analyse nothing.
SONAR_STEP = re.compile(r"sonar", re.I)
SONAR_GATE_STEP = re.compile(r"quality[\s_-]*gate", re.I)
SONAR_NOT_A_STEP = re.compile(r"^post\b|cache", re.I)
#: The public API is asked to confirm the gate unless this is "0" (tests, offline runs).
SONAR_API_ENV = "HR_SONAR_API"
SONAR_API_TIMEOUT_S = 5.0


def sonar_steps(jobs: list) -> tuple[dict | None, dict | None]:
    """`(scan, quality gate)` among a run's job steps, either None when the run has none."""
    scan = gate_step = None
    for job in jobs or []:
        for st in (job.get("steps") or []) if isinstance(job, dict) else []:
            name = str(st.get("name") or "") if isinstance(st, dict) else ""
            if not SONAR_STEP.search(name) or SONAR_NOT_A_STEP.search(name):
                continue
            if SONAR_GATE_STEP.search(name):
                gate_step = gate_step or st
            else:
                scan = scan or st
    return scan, gate_step


def sonar_project(root: Path = Path(".")) -> tuple[str | None, str]:
    """`(projectKey, host)` — from `sonar-project.properties`, else the pom's
    `<sonar.projectKey>`, else a `-Dsonar.projectKey=` in a workflow. SonarCloud unless
    `sonar.host.url` says otherwise."""
    key, host = None, "https://sonarcloud.io"
    try:
        props = (root / "sonar-project.properties").read_text(encoding="utf-8")
    except OSError:
        props = ""
    for line in props.splitlines():
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "sonar.projectKey" and v:
            key = v
        elif k == "sonar.host.url" and v:
            host = v.rstrip("/")
    if not key:
        for pom in [root / "pom.xml", *sorted(root.glob("*/pom.xml"))]:
            try:
                m = re.search(r"<sonar\.projectKey>\s*([^<\s]+)\s*</", pom.read_text("utf-8"))
            except OSError:
                continue
            if m:
                key = m.group(1)
                break
    if not key:
        for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
            try:
                m = re.search(r"-Dsonar\.projectKey=([\w.:-]+)", wf.read_text("utf-8"))
            except OSError:
                continue
            if m:
                key = m.group(1)
                break
    return key, host


def sonar_url(host: str, key: str, branch: str | None, pr: int | str | None) -> str:
    """The dashboard of what was analysed: the pull request's analysis, else the branch's."""
    from urllib.parse import quote
    page = "summary/new_code" if "sonarcloud.io" in host else "dashboard"
    url = f"{host}/{page}?id={quote(key, safe='')}"
    if pr:
        return f"{url}&pullRequest={pr}"
    return f"{url}&branch={quote(branch, safe='')}" if branch else url


def _sonar_api_status(host: str, key: str, branch: str | None, pr) -> dict | None:
    """The quality gate SonarCloud holds for this branch/PR, with the commit it describes —
    best effort: any network or parse error is None, never a failed build."""
    if os.environ.get(SONAR_API_ENV) == "0":
        return None
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(key, safe="")
    path = (f"api/project_pull_requests/list?project={q}" if pr
            else f"api/project_branches/list?project={q}")
    try:
        with urllib.request.urlopen(f"{host}/{path}", timeout=SONAR_API_TIMEOUT_S) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — offline, 401 on a private project, a slow host
        return None
    rows = doc.get("pullRequests" if pr else "branches") if isinstance(doc, dict) else None
    want = str(pr) if pr else branch
    row = next((r for r in rows or [] if isinstance(r, dict)
                and str(r.get("key" if pr else "name")) == want), None)
    if not row:
        return None
    return {"status": (row.get("status") or {}).get("qualityGateStatus"),
            "sha": (row.get("commit") or {}).get("sha")}


def sonar_from_run(view: dict, sha: str, root: Path = Path("."), pr=None) -> dict | None:
    """`{verdict, gate, url, …}` for a run's Sonar steps, from `gh run view --json
    jobs,event,headBranch`; None when the run has no Sonar step.

    The verdict is the steps' unless the public API holds a red gate **for this very
    commit**: a quality-gate step with `continue-on-error`, or none at all, leaves a green
    job over a red gate. An API answer about another commit (the branch moved on) is kept as
    `api.stale` and changes nothing."""
    scan, gate_step = sonar_steps(view.get("jobs") or [])
    if not scan and not gate_step:
        return None
    sc = ((scan or gate_step).get("conclusion") or "").lower()
    gc = ((gate_step or {}).get("conclusion") or "").lower() or None
    if sc == "skipped":
        verdict = "skipped"
    elif "failure" in (sc, gc):
        verdict = "failure"
    elif sc == "success":
        verdict = "success"
    else:
        verdict = sc or "unknown"
    out = {"verdict": verdict, "gate": gc,
           "scanStep": (scan or {}).get("name"), "gateStep": (gate_step or {}).get("name")}
    key, host = sonar_project(root)
    branch = view.get("headBranch")
    if key:
        out["projectKey"] = key
        out["url"] = sonar_url(host, key, branch, pr)
        api = _sonar_api_status(host, key, branch, pr) if verdict != "skipped" else None
        if api:
            api["stale"] = bool(api.get("sha") and sha and api["sha"] != sha)
            out["api"] = api
            if not api["stale"] and api.get("status") == "ERROR":
                out["verdict"] = "failure"
    return out


def sonar_evidence(evidence: dict, root: Path = Path(".")) -> dict | None:
    """Sonar's verdict on the run the gate decided on: the first authoritative run with a
    Sonar step. Any `gh` failure is silence, not a verdict."""
    for wf in evidence.get("workflows") or []:
        run_id = wf.get("runId")
        if not run_id or wf.get("status") not in (None, "completed"):
            continue
        r = api_forge().ci_run(run_id)
        try:
            view = json.loads(r.stdout or "") if r.returncode == 0 else None
        except json.JSONDecodeError:
            view = None
        if not isinstance(view, dict):
            continue
        pr = None
        if (view.get("event") or "").startswith("pull_request"):
            pr = api_forge().ci_run_pr(run_id)
            if not pr.isdigit() and view.get("headBranch"):
                pr = api_forge().pr_for_branch(view["headBranch"])
            pr = int(pr) if pr.isdigit() else None
        found = sonar_from_run(view, evidence.get("sha") or wf.get("sha") or "", root, pr)
        if found:
            return dict(found, runId=run_id)
    return None


#: What the head lookup asks of each run: the branch and the attempt, on top of the gate's.
HEAD_RUN_FIELDS = RUN_FIELDS + ",headBranch,attempt"
#: Conclusions that built nothing: a duplicate concluding one of these does not speak for
#: the commit when another run of the same workflow on it did conclude.
NO_BUILD = {"skipped", "cancelled"}


def pick_head_run(runs: list[dict], branch: str | None) -> dict | None:
    """The one run of a workflow that speaks for the head commit.

    A run that concluded beats a skipped or cancelled duplicate (petclinic: the push run
    failed while its pull_request twin was skipped by an `if:`), and a run still going beats
    nothing. Among those, the push run on the branch, else the pull_request one, else the
    newest. `gh run list` already reports each run's latest attempt."""
    def concluded(r):
        return r.get("status") == "completed" and \
            (r.get("conclusion") or "").lower() not in NO_BUILD
    pool = ([r for r in runs if concluded(r)]
            or [r for r in runs if r.get("status") != "completed"] or list(runs))
    push = [r for r in pool if r.get("event") == "push"
            and (not branch or r.get("headBranch") in (None, branch))]
    pr = [r for r in pool if str(r.get("event") or "").startswith("pull_request")]
    return (push or pr or pool or [None])[0]


def head_evidence(sha: str, branch: str | None, workflows: list[str]) -> dict:
    """What CI says *now* about `sha`, the branch's head, without waiting and without
    pushing: one entry per authoritative workflow (`"*"`: every workflow that ran), and an
    overall verdict — `green`, the first non-green one, or `discovery-failed`."""
    out: dict = {"sha": sha, "branch": branch, "workflows": []}
    picked: list[tuple[str, dict | None]] = []
    for wf in workflows or ["*"]:
        runs, err = latest_runs(sha, None if wf == "*" else wf, HEAD_RUN_FIELDS)
        if runs is None:
            out["workflows"].append(_evidence(wf, sha, None, "discovery-failed", err))
            continue
        if wf == "*":
            names: dict[str, list] = {}
            for x in runs:
                names.setdefault(x.get("workflowName") or "?", []).append(x)
            picked += [(n, pick_head_run(rs, branch)) for n, rs in names.items()]
            if not runs:
                picked.append((wf, None))
        else:
            picked.append((wf, pick_head_run(runs, branch)))
    for wf, one in picked:
        if one is None:
            v = "not-found"
        elif one.get("status") != "completed":
            v = "pending"
        else:
            v = _verdict(one)
        e = _evidence(wf, sha, one, v)
        if one and one.get("attempt"):
            e["attempt"] = one["attempt"]
        out["workflows"].append(e)
    bad = [e["verdict"] for e in out["workflows"] if e["verdict"] != "success"]
    out["verdict"] = ("failure" if "failure" in bad else bad[0]) if bad else "green"
    sonar = sonar_evidence(out)
    if sonar:
        out["sonar"] = sonar
    return out


def branch_head() -> tuple[str | None, str | None]:
    """`(branch, sha)` of the branch as pushed — the PR as it stands — else the local HEAD."""
    branch = run("git rev-parse --abbrev-ref HEAD").stdout.strip() or None
    if branch == "HEAD":
        branch = None
    remote = run(f"git ls-remote origin refs/heads/{shlex.quote(branch)}").stdout.split() \
        if branch else []
    sha = remote[0] if remote and re.fullmatch(r"[0-9a-f]{40}", remote[0]) \
        else run("git rev-parse HEAD").stdout.strip() or None
    return branch, sha


def refresh_ci() -> int:
    """`--ci-only`: re-read what CI says, into the `.gate.json` already there. No push, no
    gate, no wipe — the recorded verdict, SHA and run stand.

    Two things move: `sonar`, re-read off the recorded run, and `head` — CI on the branch's
    head when it is not the reviewed commit. The gate proves the commit that was reviewed;
    the PR moves on, and a page saying "CI green" over a head whose build is red (petclinic,
    20 Oct 2026: 39346ec5 failed its architecture check under a green be762955) answers a
    question the reader did not ask. No head entry when the head is the reviewed commit."""
    path = HR / ".gate.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"[preflight] no readable {path} — run the gate first", file=sys.stderr)
        return 2
    evidence.pop("sonar", None)
    evidence.pop("head", None)
    found = sonar_evidence(evidence)
    if found:
        evidence["sonar"] = found
    branch, head = branch_head()
    if head and evidence.get("sha") and head != evidence["sha"]:
        wfs = [w.get("workflow") for w in evidence.get("workflows") or []
               if isinstance(w, dict) and w.get("workflow")]
        evidence["head"] = head_evidence(head, branch, list(dict.fromkeys(wfs)))
    write_evidence(evidence)
    return 0


def gate(wait_minutes: float, workflows: list[str] | None = None,
         poll: float = 15.0) -> tuple[bool, str, dict]:
    """True when the pushed commit is proven green by **every** authoritative workflow (or
    the repo has no CI at all). The third value is the machine-readable evidence."""
    evidence: dict = {"sha": None, "selection": None, "workflows": []}
    dirty = run("git status --porcelain").stdout.strip()
    if dirty:
        print("[preflight] working tree is not clean. Decide, deliberately, what belongs "
              "in the branch before the gate runs:\n" + dirty, file=sys.stderr)

    print("[preflight] git push")
    push = run("git push", capture=True)
    if push.returncode != 0 and "up-to-date" not in (push.stderr or "").lower():
        # A push that fails is not a gate failure yet — it may need -u on a new branch.
        up = run("git push -u origin HEAD")
        if up.returncode != 0:
            return False, "cannot push: " + push_failure(
                (up.stdout or "") + (up.stderr or "") or (push.stdout or "") + (push.stderr or "")), \
                dict(evidence, verdict="push-failed")

    sha = run("git rev-parse HEAD").stdout.strip()
    if not sha:
        return False, "cannot resolve HEAD", dict(evidence, verdict="no-head")
    evidence["sha"] = sha

    if not api_forge().available():
        return True, (f"no `gh` — no build proved this commit ({sha[:12]}); say so in the "
                      "guide"), dict(evidence, verdict="unproven-no-gh")

    # Which workflows are authoritative. A push triggers several — Pages, deploys, linters —
    # and a green one of those proves nothing about whether the application builds.
    selection = "configured" if workflows else None
    if not workflows:
        known = list_workflows()
        if known is None:
            evidence.update(selection="unknown", verdict="discovery-failed")
            return False, ("could not list this repository's workflows (`gh workflow list` "
                           "failed) — a discovery failure, not a build failure; configure "
                           "`ci.workflows` or check `gh auth status`"), evidence
        if not known:
            return True, ("no build proved this — the repository has no CI configured. "
                          "Put that in the guide; it must not read as a pass"), \
                dict(evidence, selection="none", verdict="unproven-no-ci")
        workflows = auto_detect(known)
        selection = "auto-detected" if workflows else "all-runs"
    evidence["selection"] = selection
    label = ", ".join(workflows) if workflows else "every workflow that ran"
    print(f"[preflight] waiting on {label} for {sha[:12]} (not for the branch)")

    deadline = time.time() + wait_minutes * 60
    while True:
        expired = time.time() > deadline
        done: list[dict] = []
        waiting: list[str] = []
        if workflows:
            for wf in workflows:
                runs, err = latest_runs(sha, wf)
                if runs is None:
                    if re.search(r"not found|could not find", err, re.I):
                        done.append(_evidence(wf, sha, None, "workflow-not-found", err))
                    elif expired:
                        done.append(_evidence(wf, sha, None, "discovery-failed", err))
                    else:
                        waiting.append(f"{wf}: cannot ask GitHub yet ({err[:80]})")
                    continue
                if not runs:
                    if expired:
                        done.append(_evidence(wf, sha, None, "not-found"))
                    else:
                        waiting.append(f"{wf}: no run registered yet")
                    continue
                one = runs[0]          # the newest run of this workflow for this exact SHA
                if one.get("status") != "completed":
                    if expired:
                        v = "queued" if one.get("status") in NOT_STARTED else "stuck"
                        done.append(_evidence(wf, sha, one, v))
                    else:
                        waiting.append(f"{one.get('workflowName') or wf}: {one.get('status')}")
                    continue
                done.append(_evidence(wf, sha, one, _verdict(one)))
        else:
            # Nothing configured and no single ci/build workflow to choose: the only honest
            # reading is that every workflow that ran on this SHA has to be green.
            runs, err = latest_runs(sha, None)
            if runs is None:
                if expired:
                    done.append(_evidence("*", sha, None, "discovery-failed", err))
                else:
                    waiting.append(f"cannot ask GitHub yet ({err[:80]})")
            elif not runs:
                if expired:
                    done.append(_evidence("*", sha, None, "not-found"))
                else:
                    waiting.append("no run registered yet")
            else:
                newest: dict[str, dict] = {}
                for x in runs:
                    newest.setdefault(x.get("workflowName") or "?", x)
                for name, one in newest.items():
                    if one.get("status") != "completed":
                        if expired:
                            v = "queued" if one.get("status") in NOT_STARTED else "stuck"
                            done.append(_evidence(name, sha, one, v))
                        else:
                            waiting.append(f"{name}: {one.get('status')}")
                    else:
                        done.append(_evidence(name, sha, one, _verdict(one)))

        bad = [e for e in done if e["verdict"] != "success"]
        if bad or not waiting:
            # Fail fast on the first non-green verdict; otherwise everything is in and green.
            evidence["workflows"] = done
            if bad:
                evidence["verdict"] = bad[0]["verdict"]
                return False, "; ".join(_refusal(e, wait_minutes) for e in bad), evidence
            evidence["verdict"] = "green"
            names = ", ".join(f"{e['name']} (run {e['runId']})" for e in done)
            note = ("" if selection != "all-runs" else
                    " — no authoritative workflow configured, so every run had to be green; "
                    "set `ci.workflows` in human-review.json")
            return True, f"green: {names} for {sha[:12]}{note}", evidence
        for w in waiting:
            print(f"[preflight]   {w}; waiting")
        time.sleep(poll)


def write_evidence(evidence: dict) -> None:
    """`.human-review/.gate.json`: which workflow, which run, which SHA, which verdict — the
    gate's decision in a form a caller can check instead of re-deriving it from prose."""
    HR.mkdir(parents=True, exist_ok=True)
    (HR / ".gate.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print("[preflight] evidence " + json.dumps(evidence, separators=(",", ":")))


#: Per-branch model state a run keeps in `.human-review/` between runs: what a 🤖 rerun
#: replaced, and what each paid run cost. The folder outlives a branch.
MODEL_PREV = ".model-prev"
MODEL_LEDGERS = (".model-runs.json", ".film-runs.json", ".content-runs.json")
BRANCH_MARKER = ".branch"


def _fork_time(base: str) -> dt.datetime | None:
    """When HEAD's branch can have started: the earlier of the review base's commit time
    and the oldest author date after it. The base is the review's own
    (`review/state.json`) when HEAD contains it — `origin/main` can be days older."""
    try:
        recorded = json.loads((HR / "review" / "state.json").read_text()).get("base")
    except (OSError, ValueError, AttributeError):
        recorded = None
    if recorded and run(f"git merge-base --is-ancestor {shlex.quote(recorded)} HEAD").returncode == 0:
        fork = recorded
    else:
        fork = run(f"git merge-base {shlex.quote(base)} HEAD").stdout.strip()
    if not fork:
        return None
    secs = run(f"git log -1 --format=%ct {shlex.quote(fork)}").stdout.split()
    secs += run(f"git log --format=%at {shlex.quote(fork + '..HEAD')}").stdout.split()
    secs = [int(x) for x in secs if x.isdigit()]
    return dt.datetime.fromtimestamp(min(secs), dt.timezone.utc) if secs else None


def clear_foreign_model_state(base: str) -> list[str]:
    """Drop the model state that does not belong to HEAD, and say what went.

    Eval run 8 reused a `.human-review/` three branches old: `.model-prev/` held
    `feature-script.hr-claude-5.js` and a matrix from before the branch forked, and the
    AI-rerun chip priced itself off a run of another branch. Gone: everything kept in
    `.model-prev/` when the last run was on another branch (`.branch`), or older than the
    fork; every ledger row of another branch, or from before the fork."""
    head = run("git rev-parse --abbrev-ref HEAD").stdout.strip()
    since = _fork_time(base)
    try:
        last = (HR / BRANCH_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    other = bool(last and head and last != head)
    gone: list[str] = []
    prev = HR / MODEL_PREV
    for f in sorted(prev.iterdir()) if prev.is_dir() else []:
        when = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc)
        if other or (since and when < since):
            shutil.rmtree(f) if f.is_dir() else f.unlink()
            gone.append(f"{MODEL_PREV}/{f.name}")
    for name in MODEL_LEDGERS:
        path = HR / name
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs = doc.get("runs") if isinstance(doc, dict) else doc
        if not isinstance(runs, list):
            continue

        def mine(r) -> bool:
            if not isinstance(r, dict):
                return False
            if r.get("branch") and head:
                return r["branch"] == head
            try:
                when = dt.datetime.fromisoformat(str(r.get("when")).replace("Z", "+00:00"))
            except ValueError:
                return not since
            return not since or when >= since
        kept = [r for r in runs if mine(r)]
        if len(kept) != len(runs):
            gone.append(f"{name}: {len(runs) - len(kept)} run(s) of another branch")
            out = {**doc, "runs": kept} if isinstance(doc, dict) else kept
            path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    # The per-test coverage store too, on a branch switch: eval run 12's first pass failed
    # before the browser suites wrote theirs, and testcov read run 11's Playwright
    # coverage — measured on another branch's commit — as this run's.
    cov = HR / "coverage"
    if other and cov.is_dir():
        shutil.rmtree(cov)
        gone.append("coverage/ (another branch's per-test coverage)")
    if head:
        (HR / BRANCH_MARKER).write_text(head + "\n", encoding="utf-8")
    return gone


#: The logs a run leaves beside its page (`run-steps.out`, `refresh.out`, …), top level only.
LOG_GLOB = "*.out"

#: A log the shell opened for THIS process was created a moment before it started; one
#: that old is still this run's.
LOG_SLACK_S = 10.0


def sweep_old_logs(review: Path, before: float) -> list[str]:
    """Remove the top-level `*.out` logs last written before `before` (epoch seconds).

    Eval run 11 served a `refresh.out` from another folder's build, a `run-steps-2.out`
    measured from an old base and a `run-steps.out` naming tests that no longer exist —
    beside a page they did not describe, for anyone opening the folder to read as this
    run's. A log written since is the current run's and stays."""
    gone = []
    for f in sorted(review.glob(LOG_GLOB)) if review.is_dir() else []:
        try:
            if f.is_file() and f.stat().st_mtime < before:
                f.unlink()
                gone.append(f.name)
        except OSError:
            continue
    return gone


def main(argv=None) -> int:
    began = time.time()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the CI gate; the guide must then say no build proved this")
    ap.add_argument("--wait-minutes", type=float, default=20.0)
    ap.add_argument("--workflow", action="append", default=[], metavar="NAME_OR_FILE",
                    help="an authoritative CI workflow (e.g. ci.yml); repeat for several, all "
                         "must be green. Default: `ci.workflows` in --config, else the one "
                         "workflow named like ci/build, else every run on the SHA")
    ap.add_argument("--config", default="human-review.json")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    ap.add_argument("--ci-only", "--sonar-only", dest="ci_only", action="store_true",
                    help="re-read into .gate.json the Sonar steps of the run it names, and "
                         "CI on the branch's head when that is not the reviewed commit; no "
                         "push, no gate, no wipe")
    args = ap.parse_args(argv)

    if args.ci_only:
        return refresh_ci()

    caveat = ""
    if args.no_gate:
        caveat = "the CI gate was skipped — no build proved this"
        print(f"[preflight] {caveat}", file=sys.stderr)
        write_evidence({"sha": run("git rev-parse HEAD").stdout.strip() or None,
                        "selection": None, "workflows": [], "verdict": "skipped",
                        "caveat": caveat})
    else:
        ok, caveat, evidence = gate(args.wait_minutes,
                                    configured_workflows(args.workflow, args.config))
        # What the run's Sonar steps said, when it has any: the page asks "CI *and* Sonar?".
        sonar = sonar_evidence(evidence)
        if sonar:
            evidence["sonar"] = sonar
        # Written on a refusal too: it is a dotfile beside the guide, not the guide, and the
        # caller needs it most exactly when the gate said no.
        write_evidence(dict(evidence, caveat=caveat))
        print(f"[preflight] {caveat}")
        if not ok:
            print("[preflight] refusing to review a branch CI has not proven. Nothing was "
                  "wiped; the previous guide is intact.", file=sys.stderr)
            return 1

    # Only now, because everything below this line destroys the previous run.
    assets = HR / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    assets.mkdir(parents=True, exist_ok=True)
    for line in clear_foreign_model_state(args.base):
        print(f"[preflight] cleared {line}")
    for name in sweep_old_logs(HR, began - LOG_SLACK_S):
        print(f"[preflight] cleared {name}, an earlier build's log")

    ledger = Path(__file__).resolve().parent / "steps-ledger.py"
    subprocess.run([sys.executable, str(ledger), "reset"], check=False)

    (HR / ".started").write_text(
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00") + "\n",
        encoding="utf-8")
    (HR / ".session").write_text(args.session + "\n", encoding="utf-8")
    if not args.session:
        print("[preflight] no CLAUDE_CODE_SESSION_ID — the cost chip will drop itself "
              "rather than publish a wrong number", file=sys.stderr)

    (HR / ".gate").write_text(caveat + "\n", encoding="utf-8")
    print(f"[preflight] ready. base={args.base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
