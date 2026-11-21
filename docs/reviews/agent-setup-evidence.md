# Independent agent setup verification

2026-11-21, candidate `2918b98f0` (ticket #6). A locally built image and a fresh disposable Compose project used separate data, encryption key, container names, network and host port from the running installation. The synthetic Jellyfin fixture and generated MP4 are described in [its README](../../tests/fixture/README.md). The test did not use browser setup state or real credentials.

- An invalid cache/buffer combination was rejected by `config.save`; the fixture connection and public URL were then saved with `config.save` and `config.validate_pending` succeeded. The password was absent from stored plaintext and the authenticated export. No credentials were emitted in test output.
- Restarting the container applied the mounted settings. `/health/ready` and the addon manifest returned 200; a movie stream response contained two rows; a range request on a returned signed URL returned 206 and 65,536 bytes.
- Changing an environment seed and restarting retained the original manifest name. `docker compose up -d` recreated the container and the manifest then showed the new name. The encrypted data and key were retained.

This verifies transport, not playback in a player or a live provider. The fixture uses a synthetic movie and fixture-only credentials; it does not establish stock Nuvio compatibility.
