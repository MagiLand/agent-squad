# Forgejo 16.0.3 write experiments

[record.py](record.py) is a disposable experiment driver, not product code or
the future fake Forgejo server. It uses Python 3.11+ and Git, with no Python
dependencies. The observations and limitations are in the
[issue #54 evidence record](../../../docs/verification/2026-09-25-issue-54.md).

## Start the disposable instance

The recorded run used Podman, the image tag below, and a named volume. Only
the HTTP port was published, on IPv4 loopback. No SSH port was published.
The environment settings completed the SQLite installation automatically.

```sh
podman volume create agent-squad-issue54-data
podman run -d --name agent-squad-issue54 --label agent-squad.issue=54 \
  -p 127.0.0.1:18554:3000 -v agent-squad-issue54-data:/data \
  -e FORGEJO__database__DB_TYPE=sqlite3 \
  -e FORGEJO__security__INSTALL_LOCK=true \
  -e FORGEJO__server__DOMAIN=127.0.0.1 \
  -e FORGEJO__server__ROOT_URL=http://127.0.0.1:18554/ \
  -e FORGEJO__server__DISABLE_SSH=true \
  -e FORGEJO__service__DISABLE_REGISTRATION=true \
  -e FORGEJO__picture__DISABLE_GRAVATAR=true \
  -e FORGEJO__actions__ENABLED=false \
  codeberg.org/forgejo/forgejo:16.0.3
```

Use `podman image inspect` to record the resolved digest. The recorded image
digest was `sha256:21df691c3d51f66050025beab3c7781fc23870ba96004cf7c55799419da048ca`.

Using `podman exec --user git agent-squad-issue54 forgejo admin user create`,
create only the throwaway logins `approver`, `author`, and `other`; make
`approver` the administrator. Use throwaway passwords and synthetic addresses,
and set `--must-change-password=false`. Do not use an existing personal account.
Generate each token with `forgejo admin user generate-access-token --username
<login> --token-name issue54 --scopes write:repository,write:issue,read:user
--raw`, redirecting it into a private file outside the checkout with mode 600.
Do not place passwords or token values in shell history, output, or recordings.
The recorded run performed this setup through a private Python subprocess
wrapper; its token/password files were removed after the container was removed.

## Regenerate

Run from the checkout, replacing the paths with private token-file and scratch
locations. The output directory and Git working directory must not exist.
The repository name must be new. The driver verifies the version and all three
logins before mutation, refuses a repository already visible to its owner,
and does not follow HTTP redirects. HTTP is allowed only on loopback; use
HTTPS for the later authorized disposable VPS trial.

```sh
python3 tests/fixtures/forgejo/record.py \
  --base-url http://127.0.0.1:18554 \
  --author-token-file /private/tmp/issue54/author.token \
  --approver-token-file /private/tmp/issue54/approver.token \
  --other-token-file /private/tmp/issue54/other.token \
  --repository issue54-review-api \
  --output /private/tmp/issue54/new-recordings \
  --git-workdir /private/tmp/issue54/new-git
```

The script pauses for three real browser operations:

1. Repository creation needs `write:user`, which is intentionally absent from
   the scoped experiment tokens. After the recorded 403, sign in as `approver`
   at the specified origin and create the named **private** repository with
   initial README and default branch `main`. Resume the script. The script
   grants `author` write and `other` read access, seeds the diff through real
   Git pushes, and creates the protected PR as `author`.
2. At the first E6 pause, sign in as `author`, open PR 1's **Files changed**,
   add a comment on the new side of `beta.txt` line 5 containing exactly
   `E6 UI draft absorbed`, and select **Start review**. Confirm the **Pending**
   badge; do not select **Finish review**. Inspect E2/E3/E5 as described in the
   evidence record, then resume the script.
3. At the next pause, reload the diff, add `E6 UI draft deleted` on the new side
   of `beta.txt` line 4, select **Start review**, and confirm **Pending**.
   Resume. The API will delete this draft and exercise E7–E9.

The script then creates and merges PR 2 to separate the head guard from the
approval refusal, and takes a later read of PR 1's review flags. Those requests
are indexed in `guard/README.md` and `settled/README.md`. These supplemental
phases were added after the initial observations in the recorded run and were
invoked separately with the same arguments plus `--phase guard` and
`--phase settled`. A normal `--phase full` now includes them. Only use the
supplemental modes with the disposable repository created by the full phase.
The two preflight repository reads were added after recording as a safety
check; a regeneration therefore has two additional setup recordings and
shifted primary filenames. IDs, times, Git SHAs, and comment order can differ.

The output has one JSON envelope per API request, including failing requests.
Each preserves the method, API-relative path, actor, request body, HTTP status,
and response body. `git-operations.json` records the actual Git commands and
results; `seed-diff.txt` preserves the separated hunks. Browser HTTP bodies and
cookies are not captured; browser observations and the server's corresponding
HTTP status lines are documented separately in the evidence record.
No experiment imports or invokes Agent Squad product code, `fj`, or a real
model. This script performs real writes and merges only when explicitly run.

## Offline checks and cleanup

`python3 -m unittest tests.unit.test_forgejo_recordings` checks redaction,
transport guards, failure recording, and the fixture inventory without a
network or container. The test is included in `make test` and CI's `rest` group.

After collecting the UI observations, remove only the owned disposable
container and volume, and verify their absence:

```sh
podman rm -f agent-squad-issue54
podman volume rm agent-squad-issue54-data
podman ps -a --filter name=agent-squad-issue54
podman volume ls --filter name=agent-squad-issue54-data
lsof -nP -iTCP:18554 -sTCP:LISTEN
```

The final command should find no listener. Remove the private throwaway
credential files. The driver does not remove containers, volumes, output
directories, or Git working directories itself.
