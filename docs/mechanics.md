# Mechanics

The physical phone is the target. Simulator results do not establish App Store,
Apple ID, USB or physical-device behavior.

CoreDevice selects the phone by physical UDID and supplies the USB tunnel URL.
A signed WebDriverAgentRunner stays running under Xcode/launchd.
[One session](session.md) owns the control lock and reuses WDA.

Controls dispatch without lock-status preflights. Use explicit lock/unlock/status
commands for diagnosis or recovery, not as a ritual before input. Plain screenshots
read only the image; device dimensions are fetched for masking or coordinate conversion.

Safe reads may reacquire the same physical phone; writes are never automatically
replayed. Cleanup has its own bounded deadline, and cleanup failure does not
change acknowledged input. Session close is lifecycle completion, not task proof.

Configure Auto-Lock to Never only when device policy permits. An explicit
`wda unlock --verify` or the optional watchdog can recover a screen lock only
when iOS does not require passcode, Face ID or another secure confirmation.
Those hardware/security requirements cannot be removed by simplifying the harness.

App-specific expectations remain agent knowledge. App Store results may include
ads and similar titles: inspect the exact title and publisher before an authorized
install. See [the supervised template](app-store-installs.md).
