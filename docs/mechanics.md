# Mechanics

## Principle

Treat the connected physical iPhone as the target device. The simulator is only a fallback for simulator-appropriate tasks and cannot prove App Store, Apple ID, or physical-device flows.

## Control Layers

### CoreDevice / devicectl

Use `xcrun devicectl` first because it is built into Xcode and talks to real devices through CoreDevice.

Useful capabilities:

- Device discovery
- Lock-state checks
- App launch and termination
- Process inspection
- Installed-app inspection
- App install and uninstall when signing and device state allow it
- Sysdiagnose and trace-adjacent troubleshooting

Scriptable command output should use `--json-output <file>` because `devicectl` documents JSON files as the supported machine-readable interface.

### XCUITest

Use a signed XCUITest runner for real UI work:

- Tapping
- Typing
- Scrolling
- App Store search and navigation
- Prompt handling when controls are exposed to accessibility

This requires a working Apple Developer signing setup. If signing or provisioning breaks, that is a real setup blocker.

### WebDriverAgent

Use WebDriverAgent when repeated UI control is needed through HTTP.

Before trusting WDA:

1. Confirm the endpoint responds.
2. Check `/status` and require `ready: true`.
3. Make one live call such as `/source`, `/screenshot`, or session creation.

If WDA was already proven in the same run, reuse the warm session unless it goes stale.

## Lock State Rule

Always check lock state before foreground launches, tests, or taps.

Example:

```sh
xcrun devicectl device info lockState --device "$DEVICE_ID" --json-output "$TMPDIR/lock-state.json"
```

If the phone is locked, try `openclaw-iphone wda unlock --verify` once. This
agent phone is expected to have no passcode, so WDA unlock should normally
recover without human input. If verification still reports passcode required,
do not keep pushing UI commands. Ask for an unlock and report the exact boundary.

## App Control Pattern

1. Discover devices.
2. Select target device.
3. Check lock state.
4. Launch or terminate app with `devicectl`, or begin UI control through XCUITest/WDA.
5. Capture evidence only for the states that matter.
6. Verify the final state from a second signal when possible.

## App Store Specifics

App Store search results can include ads, similar apps, and unrelated nearby buttons. Verify the exact app title before tapping `Get`, `Install`, or `Open`.

Confirmation sheets can appear on-screen while missing from the WDA accessibility tree. If the screenshot clearly shows the sheet and source does not expose controls, screenshot-verified coordinate taps are acceptable.

Password prompts are not automatically human blockers. Use the configured local credential source if available. Escalate only if the credential is missing, rejected, or the prompt requires secure confirmation that automation cannot complete.

For first-launch "Sign in with Apple" flows, choose not to share the email address by default unless the task explicitly says otherwise.

## Reporting

When blocked, report the exact failed step and what is needed:

- "The phone is locked; foreground UI automation cannot continue until it is unlocked."
- "The XCUITest runner is not signed for this device; signing/provisioning needs setup."
- "The App Store prompt rejected the stored credential; a human needs to verify the Apple ID credential or complete the prompt."

Avoid generic statements like "the iPhone cannot be used" unless device discovery itself fails after focused checks.
