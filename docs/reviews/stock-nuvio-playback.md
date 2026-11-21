# Stock public Nuvio playback check

Status: **blocked, not passed** (2026-11-21). The maintainer confirmed that no suitable stock public Nuvio device is available. No client installation, addon installation, title selection, or playback was attempted. Public Nuvio version and platform are therefore unknown; no client compatibility or networking conclusion can be drawn.

The candidate available when this record was prepared was `2918b98f038833527f54ae014f544096ace1b741` (synthetic Jellyfin fixture). This is a reference point, not a claim that Nuvio was tested at that revision. The final release candidate may differ: record the exact revision and image actually tested below, and rerun on the final candidate if necessary.

## Repeat when a device is available

1. Build the final candidate and prepare an isolated installation and synthetic Jellyfin source using [the fixture instructions](../../tests/fixture/README.md). Use the fixture's `fixture` / `fixture-password` credentials, not real credentials. Ensure the candidate's `ADDON_PUBLIC_URL` and the fixture address are reachable from the player as well as from the candidate; do not use player-local `localhost` or container-only DNS for a separate device.
2. Complete the documented browser setup in [SETUP.md](../../SETUP.md), select the fixture as the Jellyfin library, apply settings, and wait for readiness. Copy the **Add to Nuvio/Stremio** manifest URL from the dashboard. Do not publish the URL or its secret.
3. On an unmodified public Nuvio installation, record its exact version and platform. Add the copied manifest URL using the documented install flow. Record whether the addon is accepted and any visible error.
4. Open **The Matrix** (IMDb `tt0133093`), select a Stream Picker result and attempt playback. The fixture supplies synthetic H.264 video, not the film. Record whether video actually starts and any client error; an HTTP response or readable media bytes alone is not a playback pass.
5. If installation or playback fails, record the observed error and the affected stage. Check the manifest from the player's network, then the stream response and returned media URL as described in the fixture instructions. Record sanitized status codes and symptoms to distinguish reachability, source and client problems; never include credentials, tokens or signed URLs.

## Result to complete

- Date / tester:
- Candidate revision and image:
- Public Nuvio version / platform:
- Setup and network topology (sanitized):
- Title / source: The Matrix, `tt0133093` / synthetic Jellyfin fixture
- Addon installation outcome:
- Playback outcome:
- Evidence and observed errors (sanitized):
- Disposition / follow-up:
