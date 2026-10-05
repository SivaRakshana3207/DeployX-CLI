# DeployX — Phase 1

DeployX is a one-command deployment platform foundation.

## Phase 1 includes

- Flask web application
- SQLite database
- Tailwind CSS directly inside HTML templates
- Deployment dashboard
- Deployment history
- Project scanner
- React + Vite / Next.js / Vue + Vite detection
- package manager detection
- build command detection
- local production build verification
- CLI command: `python cli/deployx.py deploy`
- Clean architecture ready for Phase 2 n8n automation

## Install as a `deployx` command

From this repository folder:

```powershell
pip install -e .
```

This installs a `deployx` console command into the active Python
environment. After that, open ANY project folder and treat it as the
target project — the current working directory is always the project:

```powershell
cd C:\path\to\your-project
deployx scan          # detect framework / build command (JSON output)
deployx deploy        # scan, build, GitHub repo + push, n8n + Vercel
deployx auth-status   # show GitHub / Vercel authentication status
deployx login         # connect GitHub (device flow)
deployx logout        # disconnect GitHub
deployx --help        # list all commands
```

The legacy entry point (`python cli/deployx.py deploy`) keeps working too.

### No n8n environment variables required

The DeployX backend webhook URLs
(`https://siva327.app.n8n.cloud/webhook/deployx` and
`https://siva327.app.n8n.cloud/webhook/deployx-vercel`) are built into
DeployX (see `config.py`), so `deployx deploy` works out of the box.
Users never set them. An environment variable of the same name can still
override the built-in default if needed.

## Multi-user authentication

DeployX authenticates **each user** with their own accounts. The first
`deployx deploy` starts authorization automatically — you never have to run
`deployx login` first:

- **GitHub** — OAuth Device Flow (browser).
- **Vercel** — OAuth 2.0 Device Authorization Grant (RFC 8628), the same
  browser flow used by the official Vercel CLI.

Saved authorization is reused automatically on later runs, and is
re-created automatically if a token expires or is revoked.

Each user's repository is created in **their own** GitHub account
(`<your-github-username>/<project>`), and the Vercel deployment runs with
**their own** Vercel authorization.

### Credentials are stored securely

Tokens are never written into your project, its Git history, `.env` files
or logs, and are never placed inside Git URLs (Git uses a temporary
ASKPASS helper).

| Platform | Storage |
|---|---|
| Windows | `keyring` → Windows Credential Manager, otherwise a DPAPI-encrypted per-user file (`~/.deployx/credentials.dat`) |
| macOS / Linux | OS keyring via `keyring` |

Install `keyring` to use the OS credential store:

```powershell
pip install keyring
```

### Authentication commands

```powershell
deployx login              # connect GitHub + Vercel (optional)
deployx login github       # connect only GitHub
deployx login vercel       # connect only Vercel
deployx auth-status        # show status (never shows tokens)
deployx logout             # clear all stored authorization
deployx logout github      # clear only GitHub authorization
deployx logout vercel      # clear only Vercel authorization
```

> For production, register your own public Vercel OAuth client and set
> `DEPLOYX_VERCEL_CLIENT_ID` instead of the bundled public client id.

## DeployX backend (token broker)

The backend sits between the CLI and the n8n workflow. It stores each user's
provider tokens **encrypted** (Fernet) and hands n8n a short-lived ticket, so
**provider tokens are never sent to n8n**.

```
DeployX CLI -> DeployX Backend -> encrypted provider credentials
             -> short-lived ticket -> n8n -> DeployX Backend
             -> the user's own GitHub / Vercel account
```

### 1. Configure

```powershell
Copy-Item backend\.env.example backend\.env
python -m backend.crypto      # prints a fresh DEPLOYX_ENCRYPTION_KEY
```

Paste that key into `backend/.env` and set `DEPLOYX_N8N_SERVICE_KEY` to a
random value shared only with n8n. `backend/.env` is git-ignored — never
commit it.

### 2. Start the backend

```powershell
pip install -e .
python -m backend.app          # http://127.0.0.1:8000
```

Verify it:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
```

If the backend is **not** running, `deployx deploy` falls back to the
DeployX-01 behaviour automatically — nothing breaks.

### 3. Run the tests

```powershell
pytest tests -q
```

### API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/session` | CLI session | Exchange provider tokens (once) for a session token |
| `GET /v1/session/me` | CLI session | Non-sensitive user context |
| `POST /v1/deployment-ticket` | CLI session | Mint a short-lived deployment ticket |
| `POST /v1/n8n/deployment-context` | service key + ticket | User / project context |
| `POST /v1/n8n/github/repos/create` | service key + ticket | Create repo in the user's own account |
| `POST /v1/n8n/github/repos/get` | service key + ticket | Fetch repo (ownership enforced) |
| `POST /v1/n8n/vercel/deployments/create` | service key + ticket | Deploy into the user's own Vercel account |
| `POST /v1/n8n/vercel/deployments/status` | service key + ticket | Deployment state |
| `POST /v1/n8n/vercel/env-names` | service key + ticket | Env var **names** only |

n8n authenticates with `Authorization: Bearer <DEPLOYX_N8N_SERVICE_KEY>` and
sends `{"ticket": "<deployment ticket>"}` in the body. The backend resolves the
user from the ticket, so `github_username` / `vercel_username` sent by n8n are
never trusted for authorization.


## Requirements

- Windows 10/11
- Python 3.10+
- Node.js + npm (required only when scanning/building JavaScript projects)
- VS Code

## 1. Open the folder

Open `DeployX_Phase1` in VS Code.

## 2. Create virtual environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Install packages

```powershell
pip install -r requirements.txt
```

## 4. Start DeployX

```powershell
python app.py
```

Open:

http://127.0.0.1:5000

## 5. Test the scanner

Open a second PowerShell terminal.

Go to a JavaScript project:

```powershell
cd C:\path\to\your\react-project
```

Then run:

```powershell
python C:\path\to\DeployX_Phase1\cli\deployx.py deploy
```

The scanner checks the current project, detects its framework, verifies package.json, and can run the production build.

## Supported Phase 1 stacks

1. React + Vite
2. Next.js
3. Vue + Vite

The architecture can be extended later.

## Important

Phase 1 does NOT automatically create GitHub repositories or deploy to Vercel.

Those integrations are intentionally reserved for Phase 2 with n8n.
