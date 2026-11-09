# GitHub setup

These steps need the GitHub account that owns the repository.
Menu names change now and then; the idea of each step stays the same.

## 1. Create the repository and push
1. github.com, New repository: name `invoiceguard`, **Private** or public, no README, no .gitignore, no license (it must be empty).
2. In the project folder:
   ```bash
   git remote add origin https://github.com/Marouane2005/invoiceguard.git
   git push -u origin main
   ```
   Log in the normal way (browser or a token you create yourself; type it only into the terminal prompt).
3. Open the **Actions** tab. The `ci` workflow starts by itself. It has five jobs: `test` (Python 3.11 and 3.12), `lint`, `proxy`, `audit`, `docker`. All must be green.
   - If `test` fails on 3.11 only, the pinned versions were tested on 3.12 only; compare that job's log with the 3.12 run.

## 2. Turn on the security features (Settings)
- **Code security**: enable Dependabot alerts, Dependabot security updates, **secret scanning**, and **push protection**. (`.github/dependabot.yml` already asks for weekly update pull requests.)

## 3. Protect `main`
Settings, Branches, add a rule for `main`: require a pull request, require the status checks `test`, `audit` and `docker` to pass, block force pushes. (While you work alone you may allow yourself to bypass; keep the checks required.)

## 4. Releasing
1. Bump the version in `invoiceguard/__init__.py` and `pyproject.toml`, add a `## X.Y.Z` section to `CHANGELOG.md`, run `make test ui-test`, commit.
2. Tag that commit and push the tag:
   ```bash
   git tag v1.4.0
   git push origin v1.4.0
   ```
3. The `release` workflow runs only because you pushed that tag. It first checks that the tag, the code version and the changelog agree, reruns the tests and the dependency audit, then publishes a **private** image `ghcr.io/marouane2005/invoiceguard:1.4.0` (and `:latest`) and creates a GitHub Release with the changelog notes.
4. Nothing is ever published without a tag you pushed.

## 5. Deploying the image to your server
Either build on the server (`docker compose -f docker-compose.prod.yml up -d --build`, as in `docs/RUNBOOK.md`), or pull the released image: log in once with a token that has `read:packages` (`docker login ghcr.io`), set `image: ghcr.io/marouane2005/invoiceguard:1.4.0` instead of `build: .` in `docker-compose.prod.yml`, then `docker compose -f docker-compose.prod.yml pull && docker compose -f docker-compose.prod.yml up -d`. Take a backup first (`invoiceguard backup`).

## 6. Secrets
CI and the release workflow need **no** secrets of yours: they use only the temporary token GitHub gives each run. Do not add your `ANTHROPIC_API_KEY` to GitHub. It belongs only in the `.env` file on your server.
