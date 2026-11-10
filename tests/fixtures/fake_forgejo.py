#!/usr/bin/env python3
"""Loopback-only Forgejo fixture, with response shapes from issue #54.

The JSON model is shared with fake gh. Its tokens are exclusively synthetic.
Issue GET and injected offset/shuffle/resolver cases are synthetic,
source-backed cases, not claims about a live trial. Squash, branch deletion,
and PR-body PATCH are synthetic cases based on the pinned API contracts.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import subprocess
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

RECORDINGS = Path(__file__).parent / "forgejo/recordings"
DECISIONS = ("APPROVED", "REQUEST_CHANGES")


def recording(name: str) -> object:
    return json.loads((RECORDINGS / name).read_text())["response"]["body"]


def review_wire(row: dict) -> dict:
    template = recording("022-e2-body-first.json")
    template.update(deepcopy(row))
    template["state"] = {
        "COMMENTED": "COMMENT",
        "CHANGES_REQUESTED": "REQUEST_CHANGES",
    }.get(row["state"], row["state"])
    template.setdefault("dismissed", False)
    if template["submitted_at"] is None:
        template["submitted_at"] = row["created_at"]
    return template


def pr_wire(row: dict, base_sha: str | None) -> dict:
    template = recording("017-setup-pr-after-base-push.json")
    template.update(deepcopy(row))
    template["base"].setdefault("repo", {"full_name": "MagiLand/trial"})
    template["merge_base"] = row.get("merge_base", row["base"]["sha"])
    # Forgejo reports the live base tip, including for closed PRs.
    template["base"]["sha"] = base_sha or ""
    template.pop("mergeable_state", None)
    return template


def comment_wire(row: dict, pr: dict, settings: dict) -> dict:
    template = recording("026-e2-comments.json")[0]
    template.update(deepcopy(row))
    root = next(
        (
            c
            for c in pr.get("comments", [])
            if c["id"] == row.get("in_reply_to_id")
        ),
        row,
    )
    start = root.get("start_line") or root.get("line")
    end = root.get("line")
    template["position"] = root.get("position", start or 0)
    template["path"] = root["path"]
    template["extra_lines_count"] = (end - start) if start and end else 0
    template["diff_hunk"] = root.get(
        "diff_hunk",
        (
            f"@@ -0,0 +{start},{end - start + 1} @@\n"
            + "\n".join("+fixture line" for _ in range(end - start + 1))
            if start and end
            else ""
        ),
    )
    if root["path"] in settings.get("empty_hunk_paths", []):
        template["diff_hunk"] = ""
    state = next(
        (
            t
            for t in pr.get("threads", [])
            if t["comments"]["nodes"][0]["databaseId"] == root["id"]
        ),
        None,
    )
    template["resolver"] = (
        {"login": "human"} if state and state["isResolved"] else None
    )
    if settings.get("hide_resolver"):
        template.pop("resolver", None)
    for key in ("line", "start_line", "side", "in_reply_to_id"):
        template.pop(key, None)
    return template


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def do_PATCH(self) -> None:
        self.handle_request()

    def do_DELETE(self) -> None:
        self.handle_request()

    def do_PUT(self) -> None:
        self.handle_request()

    def handle_request(self) -> None:
        with self.server.model_lock:
            model = json.loads(self.server.model_path.read_text())
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size)) if size else None
            auth = self.headers.get("Authorization")
            model.setdefault("calls", []).append(
                {
                    "method": self.command,
                    "path": self.path,
                    "Authorization": auth,
                    "body": body,
                }
            )
            status, result = self.dispatch(model, auth, body)
            self.server.model_path.write_text(json.dumps(model))
        content = json.dumps(result).encode("utf-8")
        self.send_response(status)
        if status == 302:
            self.send_header("Location", model["settings"]["redirect_to"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def git(self, model: dict, *args: str, cwd: str | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or model["origin"],
            env=self.server.git_env, text=True, capture_output=True,
            check=True, timeout=30, shell=False,
        )
        return result.stdout.strip()

    def branch(self, model: dict, branch: str) -> str | None:
        if self._branch_refs is None:
            rows = self.git(
                model, "for-each-ref", "--format=%(refname) %(objectname)",
                "refs/heads/",
            ).splitlines()
            self._branch_refs = dict(row.split() for row in rows)
        return self._branch_refs.get(f"refs/heads/{branch}")

    @staticmethod
    def record(model: dict, account: str, body: str) -> dict:
        model["next_id"] = model.get("next_id", 100) + 1
        ident = model["next_id"]
        date = (
            datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=ident)
        )
        return {"id": ident, "body": body, "user": {"login": account},
                "created_at": date.isoformat().replace("+00:00", "Z")}

    def merge_pr(
        self, model: dict, pr: dict, body: dict,
    ) -> tuple[int, object]:
        settings = model["settings"]
        if (settings.get("head_out_of_date_409")
                or settings.get("head_race_409")):
            return 409, recording("guard/007-e7-wrong-approved-head.json")
        if settings.get("merge_405"):
            return 405, recording("049-e7-no-approval.json")
        if settings.get("merge_422"):
            return 422, {"message": "injected merge refusal"}
        if body.get("head_commit_id") != pr["head"]["sha"]:
            return 409, {"message": "head out of date"}
        with tempfile.TemporaryDirectory(prefix="fake-forgejo-merge-") as tmp:
            def git(*args):
                return self.git(model, *args, cwd=tmp)
            git("clone", "--quiet", "--shared", "--no-checkout",
                model["origin"], ".")
            git("checkout", "--detach", self.branch(model, pr["base"]["ref"]))
            if body["Do"] == "merge":
                git("merge", "--no-ff", "--no-edit", pr["head"]["sha"])
            elif body["Do"] == "squash":
                git("merge", "--squash", pr["head"]["sha"])
                git("commit", "-m", "Squash fixture PR")
            else:
                return 422, {"message": "unsupported merge method"}
            if settings.get("wrong_merge_tree"):
                (Path(tmp) / "unreviewed.txt").write_text("unreviewed\n")
                git("add", "unreviewed.txt")
                git("commit", "--amend", "--no-edit")
            sha = git("rev-parse", "HEAD")
            git("push", "origin", f"HEAD:refs/heads/{pr['base']['ref']}")
            self._branch_refs = None
            if (body["delete_branch_after_merge"]
                    and not settings.get("leave_branch")):
                git("push", "origin", f":refs/heads/{pr['head']['ref']}")
                self._branch_refs = None
        pr.update(merged=True, state="closed", merge_commit_sha=sha)
        # Forgejo closes each issue a default closing keyword names in the
        # body (confirmed on 16.0.3 in the #146 triage).
        for key in re.findall(
            r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):? +#([0-9]+)\b",
            pr.get("body") or "",
        ):
            if key in model.get("issues", {}):
                model["issues"][key]["state"] = "closed"
        if settings.get("post_merge_head_discrepancy"):
            pr["head"]["sha"] = pr["base"]["sha"]
            pr["head"]["ref"] = "synthetic-pull-ref"
        return 200, None

    def dispatch(
        self, model: dict, auth: str | None, body: object
    ) -> tuple[int, object]:
        # A handler can serve multiple requests on a persistent connection.
        self._branch_refs: dict[str, str] | None = None
        settings = model.get("settings", {})
        if settings.get("redirect_to"):
            return 302, {"message": "redirect refused"}
        parsed = urlsplit(self.path)
        api_prefix = self.server.path_prefix + "/api/v1"
        if not parsed.path.startswith(api_prefix + "/"):
            return 404, {"message": "unknown instance path"}
        parts = [
            unquote(p)
            for p in parsed.path[len(api_prefix):].strip("/").split("/")
        ]
        query = parse_qs(parsed.query)
        account = (auth or "").removeprefix("token fake-token-")
        if parts == ["version"]:
            return 200, {"version": settings.get("version", "16.0.3")}
        if (
            not auth
            or not auth.startswith("token fake-token-")
            or account in settings.get("token_failure", [])
        ):
            return 401, {"message": "invalid token: " + (auth or "missing")}
        if parts == ["user"]:
            if account in settings.get("user_failure", []):
                return 401, {"message": "user unavailable: " + auth}
            return 200, {"login": settings.get("login_mismatch", account)}
        if parts[:3] != ["repos", "MagiLand", "trial"]:
            return 404, {"message": "repository not found"}
        if account in settings.get("repository_denied", []):
            return 403, {"message": "repository unreadable: " + auth}
        if settings.get("repository_missing"):
            return 404, {"message": "repository not found"}
        if len(parts) == 3:
            result = recording("009-setup-repository-author.json")
            permission = settings.get("permission", "write")
            result.update(full_name="MagiLand/trial", name="trial")
            result["permissions"] = {
                "admin": permission == "admin",
                "push": permission in ("write", "admin"),
                "pull": permission != "none",
            }
            return 200, result
        if parts[3] == "branch_protections":
            status = (403 if settings.get("protection_403")
                      else settings.get("rules_status", 403))
            return (
                (200, settings.get("protection", {}))
                if status == 200
                else (
                    status,
                    {"message": "rules unavailable"},
                )
            )
        if parts[3] == "branches":
            branch = parts[4]
            if settings.get("branch_read_403"):
                return 403, {"message": "branch unreadable"}
            if (self.command == "DELETE"
                    and settings.get("branch_removed_before_delete")):
                # Another deletion completes between the read and the DELETE.
                self.git(model, "update-ref", "-d", f"refs/heads/{branch}")
                self._branch_refs = None
            head = self.branch(model, branch)
            if self.command == "DELETE":
                if settings.get("branch_delete_403"):
                    return 403, {"message": "branch deletion forbidden"}
                if head is None:
                    return 500, {"message": "absent branch deletion"}
                if not settings.get("branch_delete_ignored"):
                    self.git(model, "update-ref", "-d", f"refs/heads/{branch}")
                    self._branch_refs = None
                return 204, None
            if head is None:
                return 404, {"message": "branch not found"}
            return 200, {"name": branch, "commit": {"id": head}}
        if parts[3] == "commits" and parts[5:] == ["status"]:
            return self.combined_status(model, account, parts[4], query)
        prs = model.get("prs", {})
        number = parts[4] if len(parts) > 4 else None
        if parts[3] == "labels":
            return 200, self.paged(model.get("labels", []), query)
        if parts[3] == "issues" and number is None:
            return self.issues(model, account, body, query)
        if parts[3] == "issues":
            # Forgejo numbers issues and PRs together; this model numbers
            # them apart, so the fixtures' issue 1 and PR 1 share a number.
            issue = model.get("issues", {}).get(number)
            pr = prs.get(number)
            if parts[5:] == ["comments"]:
                # Decisions and stops on a shared number reach the PR; an
                # issue note there is refused rather than written to the PR.
                target = pr or issue
                if target is None:
                    return 404, {"message": "issue not found"}
                if self.command == "POST":
                    note = body["body"].startswith(
                        "AGENT_SQUAD/0.5.0 NOTE ")
                    if note and issue is not None and pr is not None:
                        return 409, {
                            "message": "fixture issue and PR share number "
                            + number,
                        }
                    row = self.record(model, account, body["body"])
                    target.setdefault("conversation", []).append(row)
                    return 201, row
                return 200, target.get("conversation", [])
            if parts[5:6] in (["dependencies"], ["blocks"]) and (
                self.command == "GET"
            ):
                return self.dependencies(
                    model, account, issue, number, parts[5], query)
            if parts[5:6] == ["labels"]:
                return self.issue_labels(model, issue, parts[6:], body)
            if issue is None and pr is not None:
                return 200, self.pr_issue(pr)
            return ((200, issue) if issue
                    else (404, {"message": "issue not found"}))
        if parts[3] != "pulls":
            return 404, {"message": "unknown endpoint"}
        if number is None:
            if self.command == "POST":
                number = str(max([int(k) for k in prs] + [0]) + 1)
                row = self.record(model, account, body["body"])
                row.update(
                    number=int(number), title=body["title"],
                    head={"ref": body["head"],
                          "sha": self.branch(model, body["head"])},
                    base={"ref": body["base"],
                          "sha": self.branch(model, body["base"])},
                    state="open", merged=False, merge_commit_sha=None,
                    reviews=[], comments=[], conversation=[], threads=[],
                )
                prs[number] = row
                return 201, pr_wire(row, self.branch(model, body["base"]))
            rows = [pr_wire(p, self.branch(model, p["base"]["ref"]))
                    for p in prs.values()
                    if query.get("state", ["all"])[0] in ("all", p["state"])]
            return 200, self.paged(rows, query)
        if number not in prs:
            return 404, {"message": "pull request not found"}
        pr = prs[number]
        if pr["state"] == "open":
            pr["head"]["sha"] = self.branch(model, pr["head"]["ref"])
            pr.setdefault("merge_base", pr["base"]["sha"])
        if len(parts) == 5:
            if pr["merged"] and settings.get("merge_confirmation_403"):
                return 403, {"message": "merge confirmation unreadable"}
            if self.command == "PATCH":
                if settings.get("fail_mirror"):
                    return 503, {"message": "injected Task mirror failure"}
                if not settings.get("body_update_ignored"):
                    pr["body"] = body["body"]
                return 201, pr_wire(
                    pr, self.branch(model, pr["base"]["ref"])
                )
            return 200, pr_wire(pr, self.branch(model, pr["base"]["ref"]))
        if parts[5:] == ["merge"] and self.command == "POST":
            return self.merge_pr(model, pr, body)
        if parts[5] != "reviews":
            return 404, {"message": "unknown endpoint"}
        reviews = pr.setdefault("reviews", [])
        if settings.get("pending_draft") and account == "reviewer":
            if not any(r["id"] == 9999 for r in reviews):
                draft = recording("035-e6-draft-before-submit.json")[-1]
                draft.update(id=9999, user={"login": account},
                             commit_id=pr["head"]["sha"])
                reviews.append(draft)
        if len(parts) == 6:
            if self.command == "POST":
                if settings.get("interrupt_before_review"):
                    return 503, {"message": "interrupted before review"}
                if (settings.get("author_approval_422")
                        and account == pr["user"]["login"]
                        and body.get("event") in (
                            "APPROVED", "REQUEST_CHANGES",
                        )):
                    name = (
                        "042-e8-approved.json" if body["event"] == "APPROVED"
                        else "043-e8-request_changes.json"
                    )
                    return 422, recording(name)
                row = self.record(model, account, body.get("body", ""))
                row.update(
                    commit_id=body["commit_id"], dismissed=False,
                    state=("PENDING" if settings.get("unknown_event_pending")
                           else body["event"]), submitted_at=row["created_at"],
                )
                pending = next((r for r in reviews if r["state"] == "PENDING"
                                and r["user"]["login"] == account), None)
                if pending and settings.get("absorb_pending_draft"):
                    row["id"] = pending["id"]
                    pending.update(row)
                else:
                    if row["state"] in DECISIONS:
                        # E9 (Forgejo 16.0.3 CreateReview and SubmitReview):
                        # only the account's newest decision stays current.
                        for earlier in reviews:
                            if earlier["user"]["login"] != account:
                                continue
                            earlier["official"] = False
                            if earlier["state"] in DECISIONS:
                                earlier["dismissed"] = True
                        row["official"] = True
                    reviews.append(row)
                if settings.get("interrupt_after_body"):
                    return 503, {
                        "message": f"review {row['id']} stored; interrupted",
                    }
                return 200, review_wire(row)
            rows = [
                review_wire(r) for r in reviews
                if r["state"] != "PENDING" or r["user"]["login"] == account
            ]
            if settings.get("request_review_rows"):
                rows.insert(
                    0, recording("015-setup-requested-reviews.json")[0],
                )
            return 200, self.paged(rows, query)
        review_id = int(parts[6])
        review = next((r for r in reviews if r["id"] == review_id), None)
        if review is None:
            return 404, {"message": "review not found"}
        if len(parts) == 7:
            if self.command == "DELETE":
                if (review["state"] != "PENDING"
                        or review["user"]["login"] != account):
                    return 403, {"message": "draft not owned"}
                reviews.remove(review)
                pr["comments"] = [c for c in pr.get("comments", [])
                                  if c["pull_request_review_id"] != review_id]
                if review_id == 9999:
                    settings["pending_draft"] = False
                return 204, None
            return 200, review_wire(review)
        if parts[7:] == ["comments"]:
            if self.command == "POST":
                fid = body["body"].split("]")[0].removeprefix("[")
                if fid in settings.get("fail_roots", []):
                    return 422, {"message": "injected root failure"}
                start = body["new_position"]
                row = self.record(model, account, body["body"])
                row.update(
                    pull_request_review_id=review_id, path=body["path"],
                    position=start,
                    line=start + body.get("extra_lines_count", 0),
                    start_line=(
                        start if body.get("extra_lines_count") else None
                    ),
                )
                if settings.get("out_of_diff_accepted"):
                    row["diff_hunk"] = ""
                if not settings.get("drop_root"):
                    pr.setdefault("comments", []).append(row)
                return 200, comment_wire(row, pr, settings)
            rows = [comment_wire(c, pr, settings)
                    for c in pr.get("comments", [])
                    if c["pull_request_review_id"] == review_id]
            if (settings.get("shuffle_comments")
                    or settings.get("random_comment_order")) and rows:
                offset = len(model["calls"]) % len(rows)
                rows = list(reversed(rows[offset:] + rows[:offset]))
            return 200, rows
        return 404, {"message": "unknown endpoint"}

    def dependencies(
        self, model: dict, account: str, issue: dict | None, number: str,
        direction: str, query: dict,
    ) -> tuple[int, object]:
        """Serve F16's dependency lists (#144), modelled as on the fake gh.

        As in F16 GetIssueDependencies, the blocked-by list answers 404
        while ``dependencies_disabled`` is set, and the ref ``hidden`` is
        served as an issue numbered 0, as F16 serves a blocker the identity
        may not read. As in GetIssueBlocks, the blocks list pages before it
        drops a foreign issue marked ``unreadable``, and never caps limit;
        the blocked-by list caps it at 50 and keeps a row for every blocker.
        """
        settings = model["settings"]
        if issue is None or account in settings.get(
            "dependencies_denied", []
        ):
            return 404, {"message": "not found"}
        if direction == "dependencies":
            if settings.get("dependencies_disabled"):
                return 404, {"message": "not found"}
            # F16 DefaultPagingNum 30 for a limit of 0; MaxResponseItems 50.
            limit = int(query.get("limit", ["0"])[0]) or 30
            page, limit = self.page_of(query), min(limit, 50)
            return 200, [
                self.dependency_row(model, ref)
                for ref in issue.get("blocked_by", [])[
                    (page - 1) * limit:page * limit]
            ]
        foreign = model.get("foreign_issues", {})
        rows = [*model.get("issues", {}).items(), *foreign.items()]
        refs = [ref for ref, row in rows
                if number in row.get("blocked_by", [])]
        limit = int(query.get("limit", ["0"])[0])
        page, limit = self.page_of(query), limit if limit > 1 else 30
        return 200, [
            self.dependency_row(model, ref)
            for ref in refs[(page - 1) * limit:page * limit]
            if not foreign.get(ref, {}).get("unreadable")
        ]

    @staticmethod
    def page_of(query: dict) -> int:
        return max(int(query.get("page", ["1"])[0]), 1)

    @staticmethod
    def dependency_row(model: dict, ref: str) -> dict:
        if ref == "hidden":
            return {"id": 0, "number": 0, "title": "HIDDEN",
                    "state": "open", "repository": None}
        source, _, key = ref.rpartition("#")
        row = model["foreign_issues"][ref] if source else model["issues"][ref]
        source = source or "MagiLand/trial"
        return {
            "id": 9000 + int(key), "number": int(key), "title": row["title"],
            "state": row["state"],
            "repository": {"id": 1, "name": source.split("/")[1],
                           "owner": source.split("/")[0],
                           "full_name": source},
        }

    def issue_labels(
        self, model: dict, issue: dict | None, rest: list[str], body: object,
    ) -> tuple[int, object]:
        """Serve F16's issue label writes (#144).

        POST takes all IDs or all names and drops an unknown one silently.
        DELETE takes a name, or an ID when no label has that name, answers
        422 when neither matches and 204 otherwise. ``ignore_label_writes``
        answers as if the change were made and changes nothing.
        """
        if issue is None:
            return 404, {"message": "issue not found"}
        known = model.get("labels", [])
        keep = not model["settings"].get("ignore_label_writes")
        names = [label["name"] for label in issue["labels"]]
        if self.command == "POST":
            values = body.get("labels", [])
            if len({type(value) for value in values}) > 1:
                return 400, {"message": "labels should be an array of"
                             " strings or integers"}
            for label in known:
                if keep and label["name"] not in names and (
                    label["id"] in values or label["name"] in values
                ):
                    issue["labels"].append(dict(label))
            return 200, issue["labels"]
        if self.command == "DELETE" and len(rest) == 1:
            match = next(
                (label for label in known if label["name"] == rest[0]),
                None,
            ) or next(
                (label for label in known if str(label["id"]) == rest[0]),
                None,
            )
            if match is None:
                return 422, {"message": "label does not exist"}
            if keep:
                issue["labels"] = [label for label in issue["labels"]
                                   if label["name"] != match["name"]]
            return 204, None
        return 200, issue["labels"]

    @staticmethod
    def pr_issue(pr: dict) -> dict:
        # The issues API serves a PR number as an issue marked as a PR
        # (synthetic, source-backed like issue GET).
        fields = ("id", "number", "title", "state", "body", "user",
                  "created_at")
        return {**{k: pr[k] for k in fields}, "labels": [],
                "pull_request": {"merged": pr["merged"]}}

    def issues(
        self, model: dict, account: str, body: object, query: dict,
    ) -> tuple[int, object]:
        """Create or list issues (synthetic, source-backed: F16 CreateIssue
        takes label IDs and silently drops unknown ones; ListIssues filters
        by state and type)."""
        issues = model.setdefault("issues", {})
        prs = model.get("prs", {})
        if self.command == "POST":
            # A new issue takes a number above every issue and PR.
            number = str(max(map(int, [*issues, *prs, "0"])) + 1)
            labels = [] if model["settings"].get("drop_issue_labels") else [
                label for label in model.get("labels", [])
                if label["id"] in body.get("labels", [])
            ]
            row = self.record(model, account, body.get("body", ""))
            row.update(number=int(number), title=body["title"],
                       state="open", labels=labels, conversation=[])
            issues[number] = row
            return 201, row
        state = query.get("state", ["open"])[0]
        kind = query.get("type", [None])[0]
        rows = [] if kind == "pulls" else list(issues.values())
        if kind != "issues":
            rows += [self.pr_issue(pr) for pr in prs.values()]
        rows = [r for r in rows if state in ("all", r["state"])]
        return 200, self.paged(
            sorted(rows, key=lambda r: r["number"], reverse=True), query)

    @staticmethod
    def combined_status(
        model: dict, account: str, sha: str, query: dict
    ) -> tuple[int, object]:
        """Serve Forgejo 16.0.3's combined status as probed for #145.

        ``model["checks"][sha]`` lists statuses in creation order; the read
        keeps the latest per context, serves 30 rows by default and at most
        50, and reports ``state`` and ``total_count`` for its page alone.
        """
        if account in model.get("settings", {}).get("checks_denied", []):
            return 403, {"message": "status read refused"}
        sequence = model.get("check_sequence", {}).get(sha)
        if sequence and query.get("page", ["1"])[0] == "1":
            # Each complete read takes the next scripted state.
            model.setdefault("checks", {})[sha] = sequence.pop(0)
        latest: dict[str, dict] = {}
        for row in model.get("checks", {}).get(sha, []):
            latest.pop(row["context"], None)
            latest[row["context"]] = row
        size = min(int(query.get("limit", ["30"])[0]), 50)
        page = int(query.get("page", ["1"])[0])
        rows = list(latest.values())[(page - 1) * size:page * size]
        states = {row["status"] for row in rows}
        state = (
            "" if not rows
            else "failure" if states - {"success", "pending", "skipped"}
            else "pending" if "pending" in states
            else "success"
        )
        return 200, {
            "sha": sha,
            "state": state,
            "total_count": len(rows),
            "statuses": rows or None,
        }

    @staticmethod
    def paged(rows: list, query: dict) -> list:
        size = int(query.get("limit", ["50"])[0])
        page = int(query.get("page", ["1"])[0])
        return rows[(page - 1) * size:page * size]


class FakeForgejo:
    """Own exactly one ephemeral listening socket and its serving thread."""

    def __init__(self, *, path_prefix: str = "") -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.model_path = Path(os.environ["FAKE_FORGE_MODEL"])
        self.server.model_lock = threading.Lock()
        self.server.path_prefix = path_prefix
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        port = self.server.server_port
        self.base_url = f"http://127.0.0.1:{port}{path_prefix}"
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("fake Forgejo serving thread did not stop")
            self.closed = True
