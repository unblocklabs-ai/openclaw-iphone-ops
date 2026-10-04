# Mechanics

The physical phone is the target. Simulator results do not establish App Store,
Apple ID, USB or physical-device behavior.

CoreDevice selects the phone by physical UDID and supplies the USB tunnel URL.
A signed WebDriverAgentRunner stays running under the existing Xcode/launchd
service. [One session](session.md) owns the control lock and reuses WDA. No new
daemon or alternate phone backend is introduced.

WDA checks screen lock at the mutation boundary. Read-only acquisition and
capture do not need an unlocked screen. A clear/type compound input shares one
lock check. Do not layer extra diagnosis/lock probes on every agent action.

Safe reads may reacquire the same physical phone; writes are never automatically
replayed. Cleanup has its own bounded deadline, and cleanup failure does not
change acknowledged input. Session close is lifecycle completion, not task proof.

Configure Auto-Lock to Never only when device policy permits. An explicit
`wda unlock --verify` or the existing watchdog can recover a screen lock only
when iOS does not require passcode, Face ID or another secure confirmation.
Those hardware/security requirements cannot be removed by simplifying the harness.

App-specific expectations remain agent knowledge. App Store results may include
ads and similar titles: inspect the exact title and publisher before an authorized
install. See [the supervised template](app-store-installs.md).
