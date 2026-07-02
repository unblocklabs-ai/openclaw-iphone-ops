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
PYTHONPATH=src python3 -m openclaw_iphone doctor
```

If locked, try one best-effort WDA unlock:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda unlock --verify
PYTHONPATH=src python3 -m openclaw_iphone watchdog once
```

WDA unlock can recover only when iOS does not require passcode, Face ID, or
another secure confirmation. If verification still reports
`passcode-required: true`, foreground automation is blocked until human unlock.
Do not loop on unlock attempts.

On unattended hosts, the watchdog LaunchAgent may run this same recovery check
periodically. It is recovery-only: it does not tap the screen to keep the phone
awake, because that can corrupt the current foreground workflow.

## WDA Responds But Is Not Ready

Check:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda status
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
launched. Run `openclaw-iphone wda url` to confirm CoreDevice has a connected
USB tunnel, then verify `wda status`.

If the runner installs and starts but fails after about 60 seconds with
`Timed out while enabling automation mode`, signing, trust, and transport are
already past their gates. Check the phone is unlocked, Developer Mode is
enabled, no security prompt is visible, and Xcode can run UI tests on the
device. Record the `.xcresult` path from the log; the failure is at the
XCTest/iOS automation handshake layer, before WDA starts serving HTTP.

## Codesign Keychain Prompt

If `codesign` or Xcode asks to access the Apple Development private key, enter
the login keychain password, which can differ from the Mac login password. On
OpenClaw-managed hosts, check local operator notes or the keychain unlock launch
agent for the configured login keychain password. Choose Always Allow so future
non-interactive builds do not block on the GUI prompt.

If a non-interactive run fails with `errSecInternalComponent`, verify the
private key ACL with a direct codesign smoke test:

```sh
security find-identity -v -p codesigning
tmpdir="$(mktemp -d /tmp/openclaw-codesign.XXXXXX)"
cp /bin/echo "$tmpdir/echo-test"
codesign --force --sign "<identity-sha>" --timestamp=none "$tmpdir/echo-test"
rm -rf "$tmpdir"
```

`errSecInternalComponent` here means the identity exists but this execution
context cannot use the private key. Unlock the login keychain or approve the
private-key access prompt with Always Allow before retrying WDA.

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
- "Blocked at UI automation: WDA installs and launches, but Xcode timed out while enabling automation mode."
- "Blocked at secure confirmation: the prompt requires Face ID/passcode and cannot be completed remotely."
- "Blocked at credential: the stored Apple ID password was rejected."
