# Contributing to LightNVR

Thanks for considering it. This is a small, self-hosted project - the bar
for contributing is low, but a few conventions keep it maintainable.

## Before you start

For anything bigger than a small fix (a new feature, a change to how
streaming/storage/auth works), open an issue first describing what you want
to do. Saves you from writing a PR that gets rejected on approach rather
than implementation.

For small fixes (typos, an obvious bug, a doc correction), just open a PR
directly.

## Development setup

**Full stack (Docker):**
```bash
git clone https://github.com/<you>/lightnvr.git
cd lightnvr
docker compose up -d --build
```
Open `http://localhost:8080` and go through the setup wizard.

**Backend only (faster iteration):**
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Requires `ffmpeg` on PATH. Defaults in `app/core/config.py` work without a
`.env` file for local dev.

**Frontend only:**
```bash
cd frontend
npm install
npm run dev
```
Proxies `/api` to `http://localhost:8000` (see `vite.config.ts`) - run the
backend alongside it for a working dev environment.

See the README's Architecture section for how the three pieces (backend,
frontend, nginx) fit together.

## Code style

There's no enforced linter/formatter configured yet (a `ruff` + `eslint`
setup would be a welcome contribution on its own). In the meantime, match
the style already in the file you're editing:

- Backend: type-hinted Python, async throughout, comments explain *why*
  something is done a non-obvious way (a constraint, a workaround, an
  invariant) rather than restating what the code does.
- Frontend: TypeScript, function components + hooks, no class components.
- Keep changes scoped - a bug fix shouldn't carry an unrelated refactor.

## Testing

There's no automated test suite yet (also a welcome contribution). Until
there is one, verify changes manually and describe how in the PR:
- Backend changes: confirm the affected endpoint(s) still work end-to-end
  against a running stack (`docker compose up -d --build`).
- Frontend changes: check the affected page/component in a browser,
  including the states that are easy to miss (empty list, error response,
  viewer-role vs admin-role if the change touches permissions).

## Commit messages / PRs

Explain *why*, not just *what* - the diff already shows what changed. Look
at the existing git log for the tone this project uses. Keep a PR focused on
one change; split unrelated changes into separate PRs.

CI builds both Docker images on every PR - make sure `docker compose build`
succeeds locally before pushing if you've touched backend or frontend
dependencies.

## Reporting bugs / requesting features

Use the issue templates. For security issues, see [SECURITY.md](SECURITY.md)
instead of opening a public issue.
