# Initial Code Review Findings

Scope: read-only review of claims, docs, and snippet correctness in the local
`openclaw-iphone-ops` repository.

## Findings

### High: WDA example uses unscoped element endpoints

`snippets/wda-app-store-install-example.py` uses root WDA element endpoints:

```py
result = wda("POST", "/element", {"using": using, "value": value})
wda("POST", f"/element/{element_id}/click", {})
```

WebDriverAgent's documented examples use session-scoped endpoints such as
`/session/$SESSION_ID/elements` and `/session/$SESSION_ID/element/5/click`.
As written, the example likely fails when finding or tapping UI elements unless
the local WDA build has nonstandard root aliases.

### Medium: WDA readiness check does not match the docs

`docs/mechanics.md` says to check `/status` and require `ready: true` before
trusting WDA.

`snippets/wda-app-store-install-example.py` only checks that `/status` contains
a `value` payload, and `snippets/wda-smoke.sh` saves `/status` and `/source`
without validating readiness.

### Medium: "exact result" claim is weaker than the implementation

The App Store example prints "Waiting for exact result," but the predicate is:

```py
name CONTAINS[c] "<app name>"
```

That is a contains match, not an exact title match. This conflicts with the docs
that warn App Store results can include ads, similar apps, and unrelated nearby
buttons. The example should either tighten the matching behavior or describe the
step as a candidate selection followed by stronger verification.

### Medium: launch snippet records lock state but does not enforce it

`snippets/iphone-launch-app.sh` writes lock-state JSON before launch, but it
does not read the result or stop when the device is locked.

That undercuts the docs' rule that foreground automation should stop and ask for
human unlock when the phone is locked.

### Low: installed-app snippet defaults may be narrower than the docs imply

`snippets/iphone-installed-apps.sh` uses:

```sh
xcrun devicectl device info apps --device "$DEVICE_ID" --json-output "$apps_json"
```

Local `devicectl` help says default app output excludes default apps. That is
probably acceptable for most App Store install verification, but the snippet
name and docs imply general installed-app inspection. Consider adding
`--include-all-apps`, `--bundle-id`, or documenting the default scope.

## Checks Run

```sh
sh -n snippets/iphone-doctor.sh
sh -n snippets/iphone-installed-apps.sh
sh -n snippets/iphone-launch-app.sh
python3 -m py_compile snippets/wda-app-store-install-example.py
xcrun devicectl list devices --help
xcrun devicectl device info apps --help
xcrun devicectl device info lockState --help
xcrun devicectl device process launch --help
```

The shell snippets and Python example passed basic syntax checks. Local
`devicectl` help supports the documented JSON-output, lock-state, app-list, and
launch command shapes.
