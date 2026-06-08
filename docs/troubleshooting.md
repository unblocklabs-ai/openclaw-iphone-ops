# Troubleshooting

## Device Not Found

Check:

```sh
xcrun devicectl list devices
```

If no iPhone appears:

- Confirm `xcode-select -p` points to full Xcode, usually
  `/Applications/Xcode.app/Contents/Developer`, not
  `/Library/Developer/CommandLineTools`.
- Confirm USB connection.
- Confirm the phone trusts the host.
- Confirm Xcode can see the device.
- Confirm Developer Mode is enabled.

## Phone Locked

Prevent this where possible by setting the agent phone to
`Settings -> Display & Brightness -> Auto-Lock -> Never`.

Check:

```sh
xcrun devicectl device info lockState --device "$DEVICE_ID" --json-output "$TMPDIR/lock-state.json"
```

If locked, try one best-effort WDA unlock:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda unlock --verify
```

This agent phone is expected to have no passcode, so WDA unlock should normally
recover from lock screen without human input. If verification still reports
`passcode-required: true`, foreground automation is blocked until human unlock.
Do not loop on unlock attempts.

## WDA Responds But Is Not Ready

Check:

```sh
curl -fsS "$WDA_URL/status"
```

If `ready` is false, restart or rebuild the WDA/XCUITest lane. Do not trust old logs.

## WDA Build Succeeds But Test Does Not Stay Running

WDA needs the `WebDriverAgentRunner` scheme to run as an XCTest process. A plain
build is not enough. Use `Product -> Test` in Xcode or `openclaw-iphone wda run`.

If `xcodebuild test` logs `unable to find utility "devicectl"`, switch the host
to full Xcode:

```sh
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
xcode-select -p
```

If the runner prints `ServerURLHere->http://...:8100<-ServerURLHere`, WDA
launched. Start or restart the `iproxy` tunnel and verify `wda status`.

## Codesign Keychain Prompt

If `codesign` or Xcode asks to access the Apple Development private key, enter
the login keychain password, which can differ from the Mac login password. On
OpenClaw-managed hosts, check local operator notes or the keychain unlock launch
agent for the configured login keychain password. Choose Always Allow so future
non-interactive builds do not block on the GUI prompt.

Do not commit keychain passwords, Apple ID passwords, or certificate private-key
material to this repo.

## Developer Profile Will Not Verify On The iPhone

If the phone says the Developer App certificate is not trusted, go to
`Settings -> General -> VPN & Device Management` and trust the developer profile.

If tapping Verify flashes but does not complete:

- Confirm the phone has working internet.
- Temporarily disable VPN, DNS filtering, firewall profiles, or content blockers.
- Confirm date/time is automatic.
- Delete the WDA runner app from the phone.
- Reboot the phone.
- Run `WebDriverAgentRunner` again from Xcode or the CLI, then trust the profile.

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
- "Blocked at lock state: WDA unlock was attempted, but passcode is still required."
- "Blocked at signing: the XCUITest runner is not provisioned for this device."
- "Blocked at secure confirmation: the prompt requires Face ID/passcode and cannot be completed remotely."
- "Blocked at credential: the stored Apple ID password was rejected."
