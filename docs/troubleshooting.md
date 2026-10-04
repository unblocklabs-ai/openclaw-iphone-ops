# Troubleshooting

## Device or runner unavailable

Run `openclaw-iphone doctor --check-ui` after a failure. Confirm full Xcode,
USB pairing/trust, Developer Mode, and the dedicated physical UDID in host
config. `wda run` must keep the signed XCTest runner alive; a successful build
alone is not enough. `wda url` diagnoses CoreDevice tunnel discovery.

For an exact disconnected pin, CoreDevice may perform a bounded read-only
details probe and re-list that same phone. It never substitutes another device.
Sessions can recover safe reads repeatedly, once per read, without restarting
the service. If AX fails, image observation and coordinate controls still work.

## Locked phone

Try `wda unlock --verify` explicitly if appropriate. Passcode/Face ID-required
means human unlock is needed. Do not loop unlock attempts or switch phones.
The existing watchdog remains recovery-only; it does not send fake keepalive taps.

## Unknown or partial input

Inspect the actual screen/field. Don't infer input failure from a failed
screenshot or XML read, and don't replay a password/code automatically.
The session remains usable for inspection and subsequent deliberate requests.
Choose explicit `type` replacement or sequential input only as needed.

## Runner restart

Only after authorization, close the session, preserve its dispatch receipt,
confirm no in-flight writes, and inspect the dedicated runner with
`launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-run"`. Check its wrapper,
config, UDID and logs before restarting that runner once. Reacquire a session
and inspect actual state; don't replay previous input because cleanup failed.
Other controllers outside this package are not coordinated by its lock.

## Provenance or signing

`doctor` reports runtime source, configured repo/WDA path and launchd plist
relationships, not the command currently running. Inspect launchd separately
before approved installation/restart work. See [service setup](launchagent-service.md).

Xcode license, provisioning, keychain/private-key access, developer-profile trust
and the XCTest automation handshake are genuine platform boundaries. Diagnose
the exact failing boundary; don't automatically accept licenses, disable security,
delete runner apps, reboot or change account settings. Keep credentials out of
logs, command arguments, repository files and screenshots shared elsewhere.
