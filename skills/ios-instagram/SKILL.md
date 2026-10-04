---
name: ios-instagram
description: App knowledge for navigating Instagram on a physical iPhone using ordinary iphone-control operations. Use for Instagram tabs, searches, profiles and content inspection; no bundled discovery, ranking or video-research engine.
---

# Instagram on iPhone

Use [iphone-control](../iphone-control/SKILL.md) and one ordinary
[session](../../docs/session.md). Instagram's bundle ID is `com.burbn.instagram`.
An `open_url` request may use `instagram://user?username=HANDLE`; verify the
visible profile rather than treating the requested deep link as identity proof.

Labels such as `Explore`, `Search`, and `Search with Meta AI` have been observed
but can change. Inspect current controls, enter the requested query, choose the
intended result tab, and scroll using ordinary gestures. Use images when labels
are missing or ambiguous. Avoid inferring identity from a nearby search result
or attributing content to a profile you have not actually inspected.

Feeds, recommendations, login/consent sheets, and WebViews are dynamic. The
caller decides when more evidence is needed and whether a task is complete.
Do not automatically clear consent, follow, like, message, post, or change
accounts. Research and content evaluation belong to the caller's workflow.
