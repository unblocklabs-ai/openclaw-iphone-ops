# Troubleshooting

## Device Not Found

Check:

```sh
xcrun devicectl list devices
```

If no iPhone appears:

- Confirm USB connection.
- Confirm the phone trusts the host.
- Confirm Xcode can see the device.
- Confirm Developer Mode is enabled.

## Phone Locked

Check:

```sh
xcrun devicectl device info lockState --device "$DEVICE_ID" --json-output "$TMPDIR/lock-state.json"
```

If locked, foreground automation is blocked until human unlock.

## WDA Responds But Is Not Ready

Check:

```sh
curl -fsS "$WDA_URL/status"
```

If `ready` is false, restart or rebuild the WDA/XCUITest lane. Do not trust old logs.

## App Store Result Ambiguous

Do not tap. Verify exact title and publisher. Search result layouts can place ads or unrelated buttons near the intended app.

## Prompt Visible But Not In Accessibility Source

Capture screenshot proof. If the visible state is unambiguous, use coordinate taps verified against the screenshot.

## Credential Prompt

Use the configured secure credential source if available. Escalate only when:

- The credential is missing.
- The credential is rejected.
- The prompt requires passcode, Face ID, or another secure confirmation.

## Clean Blocker Language

Use exact language:

- "Blocked at lock state: the device is locked and needs human unlock."
- "Blocked at signing: the XCUITest runner is not provisioned for this device."
- "Blocked at secure confirmation: the prompt requires Face ID/passcode and cannot be completed remotely."
- "Blocked at credential: the stored Apple ID password was rejected."

