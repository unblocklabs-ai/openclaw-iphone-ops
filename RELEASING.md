# Releases

A stable GitHub Release (`vX.Y.Z`) triggers `.github/workflows/release.yml`.
Ordinary pushes, PRs, drafts, and prereleases do not publish packages.

## Outputs

- Public npm package: `@unblocklabs/openclaw-iphone-ops` (macOS CLI plus skills).
- GitHub assets: the tested npm `.tgz`, Python wheel, Python source distribution,
  and `SHA256SUMS`. No automatic PyPI or ClawHub publication.
- No device actions, service installation, or Apple credentials in the release
  jobs. Offline tests do not prove physical-device readiness.

The npm archive uses an explicit allowlist. Local config, evidence, tests,
release tooling, and historical `build/` notes are excluded. The Python wheel
contains Python code only; its source distribution includes docs/skills/snippets.
The repository does not currently grant an open-source license; npm metadata is
`UNLICENSED`, rather than inventing a license as part of packaging.

## One-time npm setup

An npm owner must bootstrap the package before enabling trusted publishing:

1. Log in with `npm login` as an owner of `@unblocklabs` (complete any 2FA prompt).
2. From a clean, tested commit already on `main`, run the preparation checks
   below and `python3 scripts/check_npm_package.py --output-dir dist`.
3. Publish that tested archive with `npm publish
   dist/unblocklabs-openclaw-iphone-ops-X.Y.Z.tgz --access public`. A local first
   publish cannot provide GitHub Actions provenance; do not claim it does.
4. In the npm package Settings, configure a **GitHub Actions trusted publisher**:
   organization `unblocklabs-ai`, repository `openclaw-iphone-ops`, workflow
   `release.yml`, no environment. Subsequent releases use OIDC and provenance.
5. Publish the matching GitHub Release. Its workflow verifies the bootstrap npm
   archive is byte-identical instead of trying to republish an immutable version.

Alternatively, bootstrap from the release workflow using a short-lived granular
npm token with publish rights to this package/scope and bypass-2FA permission,
stored as the repository's `NPM_TOKEN` secret. Never commit or log it. Remove
that secret after trusted publishing works. The normal OIDC path needs no token.

## Prepare each release

Use macOS, Python 3.11+, and Node 24/npm (npm >=11.5.1 for OIDC). Start from the
latest `origin/main`; preserve unrelated local work.

1. Update `package.json`, `pyproject.toml`, and
   `src/openclaw_iphone/__init__.py` to the same stable version. Regenerate the
   lockfile with `npm install --package-lock-only --ignore-scripts`. Update the
   pinned install examples in README.
2. Run:

   ```sh
   npm ci --ignore-scripts
   npm run release:check -- vX.Y.Z
   npm run preflight
   sh -n bin/openclaw-iphone
   for script in snippets/*.sh snippets/launchd/*.sh; do sh -n "$script" || exit; done
   ```

   Preflight runs the full Python suite, compares all versions, inspects every
   packed npm path, and installs the actual archive into an isolated prefix.
   Its CLI smoke check runs outside the checkout without device access.
3. Commit/push the reviewed changes to `main` through the repository's normal
   review process and wait for both Python CI jobs. Ensure the release commit
   is reachable from `origin/main` and the worktree is clean.
4. Write concise release notes with changes, validation, and known limits.
   Explicitly distinguish mocked/loopback tests from live-device validation.
5. Create the release at the exact tested commit:

   ```sh
   gh release create vX.Y.Z --target <full-tested-main-commit> \
     --title "vX.Y.Z" --notes-file /absolute/path/to/release-notes.md
   ```

## Verify and recover

Wait for the Release workflow to pass; the GitHub Release appearing is not proof
that npm publication succeeded. Check:

```sh
gh run list --workflow release.yml --limit 5
gh release view vX.Y.Z
npm view @unblocklabs/openclaw-iphone-ops@X.Y.Z version dist.integrity dist.attestations --json
npm view @unblocklabs/openclaw-iphone-ops dist-tags --json
```

Download the GitHub npm archive and compare its SHA-512 integrity with the npm
registry value. Also test a fresh registry install with `--version` and `--help`.
Validate `SHA256SUMS` for downloaded assets. Keep package versions immutable.

If publishing fails, inspect the failed step and fix authentication or runner
infrastructure, then rerun that workflow. It replaces generated GitHub assets
and skips npm publication **only when the existing version's integrity exactly
matches the tested tarball**. Registry failures and content mismatches stop the
job. Do not unpublish, move a released tag, or overwrite a different package to
force success; code changes require a new patch release.

Release jobs have `id-token: write` for npm OIDC and `contents: write` for GitHub
assets. Only publish trusted, reviewed commits. The tag/version and main-ancestry
checks prevent accidental off-branch releases; they do not replace review or
GitHub access control. Maintainers should protect `main` and release tags as
appropriate for the organization.
