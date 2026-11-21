# Synthetic source for isolated release checks

This fixture is a separate test-only Stremio addon, with no external providers or credentials. It serves a synthetic MP4 and supports byte ranges. It must be reachable both by Stream Picker for probing and by the player for playback; set `FIXTURE_BASE_URL` to that reachable address. The addon manifest may use a different address reachable from the Stream Picker container. Do not point real users at this fixture.

Requirements: Docker Compose, ffmpeg with libx264, Python 3, Node and Playwright with Chromium. Use a fresh working directory, separate Compose project, container name and host port, and a disposable addon secret and admin account. Never reuse a real installation's data or key. Change the published Compose file's image to a locally built candidate, container name and port before starting it. Generate the key and `.env` as in the public setup guide. The generated video deliberately stretches timestamps to pass the normal feature-length filter; it demonstrates transport and decoding, not normal motion or real content.

Generate the media (about 11 MB):

```sh
ffmpeg -t 2 -f lavfi -i 'testsrc2=s=640x360:r=24,noise=alls=30:allf=t+u' \
  -vf 'setpts=4000*PTS' -fps_mode vfr -c:v libx264 -preset ultrafast \
  -crf 16 -pix_fmt yuv420p -movflags +faststart -y fixture.mp4
```

Run `source.py` in a separate container on the isolated Compose network, with `FIXTURE_MEDIA` pointing to that file and `FIXTURE_BASE_URL` set to its reachable HTTP base. Connect to `/manifest.json` in the wizard. For failure/retry, first use `/missing`, confirm the HTTP error, then correct the manifest URL. The addon link appears after the new process is ready and settings are applied. Readiness alone does not establish playback: request the displayed manifest, then `/stream/movie/tt0133093.json` under the same addon prefix, retrying if the initial fast result is empty while verification finishes. Fetch bytes from a returned stream URL and check that a player can decode them. A player on a different host needs addresses routable from that host, not `localhost` or a container-only DNS name. The fixture uses the well-known IMDb ID and title of The Matrix for identity matching; its media is synthetic and is not that film.

`tests/browser/setup_failures.cjs` exercises simulated partial-check and restart-rejection responses in Chromium against the dashboard. Its fixed administrator credentials are disposable fixture values. Install Playwright separately and invoke it with `NODE_PATH=<playwright node_modules> node tests/browser/setup_failures.cjs <dashboard-url>`. Use fresh state or an instance enrolled with those fixture credentials.

A run on 2026-11-21 against an isolated locally built candidate enrolled an administrator, saw a source 404, corrected the source, saved and applied the settings, and displayed the install URL. The manifest and source endpoint were reachable, and the synthetic media passed the candidate's byte and duration probe. The player-facing stream response remained empty: no TMDB or OMDb credentials were configured, so the identity profile had no authoritative aliases and the picker could not promote the candidate. These provider endpoints are fixed HTTPS endpoints, and seeding the internal OMDb cache would bypass the public setup contract. A synthetic Jellyfin API fixture or a supplied metadata credential could close this gap; neither was available in this run. Readable returned media and stock Nuvio playback were not established. Check container logs and source reachability if the stream remains empty. This result is not a release playback pass.
