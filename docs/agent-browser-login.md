# Temporary agent browser login

Use this to inspect the live Moresocial UI from VS Code/Playwright without interactive
Google sign-in. It uses operator SSH access to create an ordinary, short-lived session
for an existing allowlisted account. No public login endpoint, shared password, Google
grant, account creation, schema change or application restart is involved.

The session has the selected account's normal permissions, including Admin if that
account is an administrator. It is not a server-enforced read-only session. For visual
reviews, instruct the agent to browse only and avoid submitting mutations.

## Issue a session

From `moresocial/`:

```bash
python3 scripts/agent-session.py issue --email errslima@gmail.com
```

The workstation must have working operator SSH access through
`~/.ssh/qoc_vps_ed25519`. The command runs `app/agent_session.py` over stdin in the
existing web container, so it also works before this helper is deployed in an image.
It creates one session in the live database and writes:

- `.dev/agent-browser.json`: the Playwright storage state (secret, mode 0600).
- `.dev/agent-browser.session.json`: its non-secret ID and UTC expiry, for revocation.

Both files are already covered by the repository's `.dev/` ignore rule. The credential
is never printed or put in a URL. The temporary server-side copy is removed after
transfer. Do not paste, commit or include the storage-state file in logs or reports.

Default lifetime is **120 minutes**. Use `--minutes 30` for a shorter session; the
maximum is 1440 minutes. Revoke before issuing another session to the same output path:
existing files and symlinks are never overwritten. `--state`, `--host`, `--ssh-key` and
`--container` can override the local output and operator connection settings.

## Use it in Playwright

Run from `moresocial/` using the existing virtual environment:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(
        storage_state='.dev/agent-browser.json',
        viewport={'width': 1440, 'height': 900},
    )
    page = context.new_page()
    page.goto('https://1f517.com/moresocial/whatsapp')
    page.screenshot(path='.dev/whatsapp-review.png', full_page=True)
    browser.close()
```

This file does not automatically sign in Safari, VS Code's webview, or an unrelated
browser tool. The automation browser must explicitly load it. If Chromium is missing,
install it using `.venv/bin/python -m playwright install chromium`.

Suggested agent instruction:

> Read `docs/agent-browser-login.md`. Use Playwright with
> `.dev/agent-browser.json` to inspect the live Home and WhatsApp pages at desktop and
> mobile sizes. Browse only; do not submit changes. Do not print credentials or private
> chat content. If the file has expired, revoke it and issue a new two-hour session
> using the documented SSH helper. Revoke the review session when finished.

## Revoke access

```bash
python3 scripts/agent-session.py revoke
```

This revokes only the recorded session and removes both local files. Your other
browser sessions stay signed in. Normal Sign out also revokes this session. Deleting
the file alone does **not** revoke a cookie already loaded in a browser; the database
expiry still applies to all copies.

If only the session ID remains, revoke it with operator access:

```bash
ssh -i ~/.ssh/qoc_vps_ed25519 ubuntu@54.37.204.161 \
  'sudo docker exec -i moresocial-web-1 python - revoke --session-id SESSION_UUID' \
  < app/agent_session.py
```

## Verification

`tests/test_agent_session.py` covers private file permissions, no account or Google
grant creation, workspace isolation, unchanged CSRF protection, bounded expiry,
revocation/logout, refusal of inactive or disallowed accounts, HTTPS, and rollback on
file-write failures. Existing Google authentication tests must also continue to pass.
