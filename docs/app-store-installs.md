# App Store Install Flow

## Preconditions

- Physical iPhone is connected, trusted, awake, and unlocked.
- Developer Mode is enabled.
- App Store is reachable on the device.
- XCUITest or WDA can perform taps and text entry.
- Any required Apple ID credential is supplied through the operator's secure local configuration, not committed to this repo.

## Flow

1. Verify device discovery.
2. Check lock state.
3. Open App Store.
4. Search for the requested app.
5. Verify the exact target app title and publisher when visible.
6. Tap the correct result's action button.
7. Handle Apple ID, confirmation, and free-item prompts.
8. Verify the App Store state reaches `Open` or equivalent.
9. Verify the app appears in `devicectl` installed-app output.

The reusable example is `snippets/wda-app-store-install-example.py`. It assumes
WebDriverAgent is already running and resolves the selected device's CoreDevice
URL. It requires `APP_NAME`, `EXPECTED_PUBLISHER`, `EXPECTED_BUNDLE_ID`, and
`ALLOW_INSTALL=1`, plus optional `DEVICE_ID`. A human must inspect the page and
type `INSTALL` in the terminal before the install tap. Element requests are
session-scoped, exact and unambiguous. It does not handle credentials, paid
buttons, cloud-icon guesses, or secure prompts. This is a supervised template,
not a validated unattended installer.

## Default Prompt Choices

- If App Store asks to save the password for free items, choose `Not Now` unless the task explicitly says to save it.
- If first launch asks whether to share email through Sign in with Apple, choose not to share the email address unless the task explicitly says otherwise.

## Proof Rules

Do not claim install success from only a visible `Open` button. Also verify installed-app state through `devicectl`.

Minimum evidence for repeat installs:

- Live automation check at start
- Exact target-result proof before install tap
- Final success or blocker screen
- Fresh installed-app proof

Capture more only when a new prompt, ambiguity, or blocker appears.
