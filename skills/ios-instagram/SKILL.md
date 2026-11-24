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

Feeds, recommendations, login/consent sheets, and WebViews are dynamic. In-app
search tabs (such as `For you`) are not sorted chronologically; check visible
post dates before treating results as recent. The caller decides when more
evidence is needed and whether a task is complete. Do not automatically clear
consent, follow, like, message, post, or change accounts. Research and content
evaluation belong to the caller's workflow.

To inspect a Reel or video post in an external video tool (such as
`video-understand`) or capture a post URL, follow
[ios-social-video](../ios-social-video/SKILL.md): tap Share/Send -> `Copy link`
on the post (if `Copy link` is scrolled off-screen in the bottom horizontal
share row, swipe that row horizontally until `Copy link` has on-screen bounds
before tapping), call `{"op":"pasteboard"}` and verify a non-empty URL was
returned, then run `openclaw-iphone media fetch "<URL>" --json` when you need
the `.mp4`. iOS accessibility `/source` does not expose underlying `.mp4` stream
URLs.
