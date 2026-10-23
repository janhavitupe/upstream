# Running Upstream's pipeline in Jenkins

The pipeline is in [`Jenkinsfile`](../Jenkinsfile):

| Stage | What it does |
|---|---|
| Install | Creates a clean virtual environment and installs `requirements-dev.txt` |
| Syntax check | Compiles every Python file |
| Unit tests | Runs `pytest`: 167 tests covering every feature, offline (fake PayPal, AI and GitHub), ~10 s |
| End-to-end | Every money flow against the **real PayPal sandbox** (10 tests, ~2 min). Runs **every night** by itself, or tick `RUN_E2E` |
| Live smoke test | *Optional.* Calls the real PayPal sandbox with credentials stored in Jenkins |
| Deploy to Render | *Optional, `main` only.* Triggers a Render deploy through a deploy hook |

Jenkins checks GitHub for new commits every 5 minutes and builds automatically, and runs the full
suite including end-to-end tests every night around 2 AM (only while Jenkins is running).

## 1. Install Java and Jenkins (Windows)

Open PowerShell **as Administrator**:

```powershell
winget install EclipseAdoptium.Temurin.21.JDK
winget install Jenkins.Jenkins
```

If either ID isn't found, run `winget search jenkins` or `winget search temurin` and use the ID it shows.
The Jenkins installer sets Jenkins up as a Windows service on **http://localhost:8080**.
(Port 8080 doesn't clash with Upstream, which runs on 8000.)

## 2. Unlock Jenkins

1. Open http://localhost:8080.
2. Paste the password from `C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword`.
3. Choose **Install suggested plugins**. They include everything the pipeline needs
   (Pipeline, Git, JUnit, Credentials Binding).
4. Create your admin user.

## 3. Create the pipeline job

1. **New Item** → name it `upstream` → **Pipeline** → OK.
2. Under **Pipeline**: Definition **Pipeline script from SCM** → SCM **Git**.
3. Repository URL: `https://github.com/janhavitupe/upstream.git` (public, so no credentials).
4. Branch: `*/main`. Script Path: `Jenkinsfile`. **Save**.

## 4. First build

1. Click **Build Now**. The first build runs with default settings and teaches Jenkins the
   parameters. It may fail at **Install** if Jenkins can't find Python: see below.
2. From then on, use **Build with Parameters**.

**Python not found?** The Jenkins service runs as a system account that doesn't see your user's
Python. Set the `PYTHON` parameter to the full path, for example:

```
C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe
```

(Find yours with `where python` in a normal terminal.) To make it the default, edit
`defaultValue` of `PYTHON` in the `Jenkinsfile`.

**Git not found?** Install Git for Windows, then set its path under
*Manage Jenkins → Tools → Git installations* (e.g. `C:\Program Files\Git\bin\git.exe`).

After a build, open it and click **Tests** to see every test, and **Console Output** for the log.

## 5. End-to-end tests (real PayPal sandbox)

They need the same credentials as the smoke test below (`paypal-client-id`, `paypal-client-secret`,
and optionally `groq-api-key` for the chat test). Then either wait for the nightly run, or
**Build with Parameters** → tick **RUN_E2E**. Results appear under **Tests** next to the unit tests.

Freestyle job: add a second batch command (with the credentials bound under *Build Environment →
Use secret text(s) or file(s)*):

```bat
set UPSTREAM_E2E=1
.venv-ci\Scripts\python.exe -m pytest -m e2e --junitxml=reports\e2e.xml
```

Run them on your own PC: `$env:UPSTREAM_E2E="1"; python -m pytest -m e2e` (keys come from `.env`).

## 6. Optional: live smoke test

Add these under *Manage Jenkins → Credentials → System → Global credentials → Add Credentials*,
kind **Secret text**, with exactly these IDs:

| ID | Value |
|---|---|
| `paypal-client-id` | your sandbox Client ID |
| `paypal-client-secret` | your sandbox Secret |
| `groq-api-key` | your Groq key |
| `github-token` | your GitHub token |

Then **Build with Parameters** → tick **RUN_SMOKE**. The default sections (`config oauth github`)
don't move any sandbox money; add `invoices` or `payouts` to `SMOKE_SECTIONS` for a fuller check.

## 7. Optional: deploy from Jenkins

1. Render → your service → **Settings** → **Deploy Hook** → copy the URL.
2. Add it in Jenkins as a Secret text credential with ID `render-deploy-hook`.
3. If you want Jenkins (not Render) to decide when to deploy, turn off **Auto-Deploy** in the same
   Render settings page; otherwise both would deploy on every push.
4. **Build with Parameters** → tick **DEPLOY**. It only deploys from `main`, and only if the tests passed.

## Run the same checks without Jenkins

```powershell
pip install -r requirements-dev.txt
python -m pytest
```
