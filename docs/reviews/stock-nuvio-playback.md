# Stock public Nuvio playback check

**Addon install and visible playback passed on Android on 2026-11-21.**

- Candidate: integration revision `a520ee1dca15e8f34bcb3ed1f2139525b9dbff59`, built as `stream-picker-nuvio-check:a520ee1dc` (local image ID `sha256:da06e9a866a2aaca0cd6fad0dfee2813efcd808bcd52200f6fca35aa8b5552a5`).
- Nuvio version: `1.1.0-betta.2 (159)`, as reported by the maintainer.
- Platform: Android 13, Pixel 7 as reported by the media request user agent. The maintainer performed the test in Nuvio.
- Setup: isolated candidate and synthetic Jellyfin fixture using fresh configuration, a disposable key, and a separate LAN endpoint. The fixture served The Matrix (1999), IMDb `tt0133093`.
- Install: the phone requested the supplied manifest and received HTTP 200. An unrelated episode returned no result, as expected for a fixture with one movie.
- Playback: the phone requested the fixture movie stream (HTTP 200), then read the signed library media (HTTP 200 and 206). The maintainer reported seeing the synthetic test pattern. The Jellyfin label comes from the fixture’s source type.

## Repeat when a device is available

1. Build the final candidate and prepare an isolated installation and synthetic Jellyfin source using [the fixture instructions](../../tests/fixture/README.md). Use the fixture's `fixture` / `fixture-password` credentials, not real credentials. Ensure the candidate's `ADDON_PUBLIC_URL` and the fixture address are reachable from the player as well as from the candidate; do not use player-local `localhost` or container-only DNS for a separate device.
2. Complete the documented browser setup in [SETUP.md](../../SETUP.md), select the fixture as the Jellyfin library, apply settings, and wait for readiness. Copy the **Add to Nuvio/Stremio** manifest URL from the dashboard. Do not publish the URL or its secret.
3. On an unmodified public Nuvio installation, record its exact version and platform. Add the copied manifest URL using the documented install flow. Record whether the addon is accepted and any visible error.
4. Open **The Matrix** (IMDb `tt0133093`), select a Stream Picker result and attempt playback. The fixture supplies synthetic H.264 video, not the film. Record whether video actually starts and any client error; an HTTP response or readable media bytes alone is not a playback pass.
5. If installation or playback fails, record the observed error and the affected stage. Check the manifest from the player's network, then the stream response and returned media URL as described in the fixture instructions. Record sanitized status codes and symptoms to distinguish reachability, source and client problems; never include credentials, tokens or signed URLs.
