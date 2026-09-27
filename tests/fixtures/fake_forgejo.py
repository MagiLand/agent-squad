#!/usr/bin/env python3
"""Loopback-only Forgejo fixture, with response shapes from issue #54.

The JSON model is shared with fake gh. Its tokens are exclusively synthetic.
Issue GET and injected offset/shuffle/resolver cases are synthetic,
source-backed cases, not claims about a live trial. Writes model recorded
faults;
production mutation commands remain disabled until Increment 4.
"""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, unquote, urlsplit

RECORDINGS = Path(__file__).parent / "forgejo/recordings"


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


def pr_wire(row: dict) -> dict:
    template = recording("017-setup-pr-after-base-push.json")
    template.update(deepcopy(row))
    template["base"].setdefault("repo", {"full_name": "MagiLand/trial"})
    template["merge_base"] = row.get("merge_base", row["base"]["sha"])
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

    def dispatch(
        self, model: dict, auth: str | None, body: object
    ) -> tuple[int, object]:
        settings = model.get("settings", {})
        if settings.get("redirect_to"):
            return 302, {"message": "redirect refused"}
        parsed = urlsplit(self.path)
        api_prefix = self.server.path_prefix + "/api/v1"
        if not parsed.path.startswith(api_prefix + "/"):
            return 404, {"message": "unknown instance path"}
        parts = [
            unquote(p)
            for p in parsed.path[len(api_prefix) :].strip("/").split("/")
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
        if len(parts) == 2 and parts[0] == "users":
            if parts[1] in settings.get("missing_users", []):
                return 404, {"message": "user not found"}
            return 200, {"login": parts[1]}
        if parts[:3] != ["repos", "MagiLand", "trial"]:
            return 404, {"message": "repository not found"}
        if account in settings.get("repository_denied", []):
            return 403, {"message": "repository unreadable: " + auth}
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
            status = settings.get("rules_status", 403)
            return (
                (200, settings.get("protection", {}))
                if status == 200
                else (
                    status,
                    {"message": "rules unavailable"},
                )
            )
        prs = model.get("prs", {})
        number = parts[4] if len(parts) > 4 else None
        if parts[3] == "issues":
            issue = model.get("issues", {}).get(number)
            pr = prs.get(number)
            if parts[5:] == ["comments"]:
                target = pr or issue
                return (
                    (200, target.get("conversation", []))
                    if target
                    else (404, {"message": "issue not found"})
                )
            return (
                (200, issue)
                if issue
                else (404, {"message": "issue not found"})
            )
        if parts[3] != "pulls":
            return 404, {"message": "unknown endpoint"}
        if number is None:
            return 200, self.paged([pr_wire(p) for p in prs.values()], query)
        if number not in prs:
            return 404, {"message": "pull request not found"}
        pr = prs[number]
        if len(parts) == 5:
            return 200, pr_wire(pr)
        if parts[5:] == ["merge"] and self.command == "POST":
            if settings.get("head_out_of_date_409"):
                return 409, recording("guard/007-e7-wrong-approved-head.json")
            return 405, recording("049-e7-no-approval.json")
        if parts[5] != "reviews":
            return 404, {"message": "unknown endpoint"}
        if len(parts) == 6:
            if self.command == "POST":
                if (
                    settings.get("author_approval_422")
                    and account == pr["user"]["login"]
                    and body.get("event") in ("APPROVED", "REQUEST_CHANGES")
                ):
                    name = (
                        "042-e8-approved.json"
                        if body["event"] == "APPROVED"
                        else "043-e8-request_changes.json"
                    )
                    return 422, recording(name)
                if settings.get("unknown_event_pending"):
                    result = recording("044-e8-unknown_event.json")
                    result.update(
                        user={"login": account}, commit_id=body["commit_id"]
                    )
                    pr.setdefault("reviews", []).append(result)
                    return 200, result
                return 501, {"message": "fixture write not implemented"}
            rows = [
                review_wire(r)
                for r in pr.get("reviews", [])
                if r["state"] != "PENDING" or r["user"]["login"] == account
            ]
            if settings.get("request_review_rows"):
                rows.insert(
                    0, recording("015-setup-requested-reviews.json")[0]
                )
            if settings.get("pending_draft") and account == "reviewer":
                draft = recording("035-e6-draft-before-submit.json")[-1]
                draft.update(
                    id=9999,
                    user={"login": account},
                    commit_id=pr["head"]["sha"],
                )
                rows.append(draft)
            return 200, self.paged(rows, query)
        if parts[7:] == ["comments"]:
            rows = [
                comment_wire(c, pr, settings)
                for c in pr.get("comments", [])
                if c["pull_request_review_id"] == int(parts[6])
            ]
            if settings.get("shuffle_comments") and rows:
                offset = len(model["calls"]) % len(rows)
                rows = list(reversed(rows[offset:] + rows[:offset]))
            return 200, rows
        return 404, {"message": "unknown endpoint"}

    @staticmethod
    def paged(rows: list, query: dict) -> list:
        size = int(query.get("limit", ["50"])[0])
        page = int(query.get("page", ["1"])[0])
        return rows[(page - 1) * size : page * size]


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
