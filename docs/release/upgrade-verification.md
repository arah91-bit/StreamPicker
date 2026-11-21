# Existing installation upgrade check

On 2026-11-21, the public baseline `ece39508703780632ae1f24ad007ce633f0c5997` was upgraded to candidate `2918b98f0`. Both were locally built images started with the baseline's Compose file, with only the image, container name and host port changed. The isolated installation used the synthetic Jellyfin fixture and a disposable secret and encryption key. Its existing data directory, key, environment and install link were retained across `docker compose up -d` image replacement.

Before and after the upgrade, the saved password decrypted successfully, the same manifest identified `org.streampicker.auto`, the movie request returned two streams, and a 65,536-byte range from a returned signed library URL matched the fixture bytes (HTTP 206). The saved config and key had identical hashes across replacement, and the fixture password was absent from the config plaintext. No upgrade regression was observed. This verifies transport through the library source, not playback in a client or every provider. The initial candidate request raced startup and was retried after the service was ready.

## Reproduce

Run from a candidate checkout with Docker, Compose, Python 3, OpenSSL and ffmpeg with libx264. Choose an unused project name, container names and host port if these are already taken. This creates disposable state in `/tmp/sp-upgrade-check`; do not use an existing installation. The command uses the baseline Dockerfile and Compose contract; the fixture comes from the candidate checkout. The password and addon secret below are public fixture values only.

```bash
work=/tmp/sp-upgrade-check
repo=$PWD
mkdir -p "$work/prior" "$work/data" "$work/secrets"
git archive ece395087 | tar -x -C "$work/prior"
docker build -t sp-upgrade-prior "$work/prior"
docker build -t sp-upgrade-candidate "$repo"
openssl rand -out "$work/secrets/stream-picker-config.key" 32
chmod 400 "$work/secrets/stream-picker-config.key"
printf 'ADDON_SECRET=upgrade-fixture-secret-1234567890\nADDON_PUBLIC_URL=http://stream-picker:8000\n' > "$work/.env"
cp "$work/prior/docker-compose.yml" "$work/docker-compose.yml"
sed -i 's@ghcr.io/arah91-bit/streampicker:latest@sp-upgrade-prior@;s/container_name: stream-picker/container_name: sp-upgrade-check-app/;s/"8011:8000"/"127.0.0.1:18091:8000"/' "$work/docker-compose.yml"
ffmpeg -loglevel error -t 2 -f lavfi -i 'testsrc2=s=640x360:r=24,noise=alls=30:allf=t+u' -vf 'setpts=4000*PTS' -fps_mode vfr -c:v libx264 -preset ultrafast -crf 16 -pix_fmt yuv420p -movflags +faststart -y "$work/fixture.mp4"
cd "$work"
docker compose -p sp-upgrade-check up -d
docker run -d --name sp-upgrade-check-fixture --network sp-upgrade-check_default --network-alias fixture -v "$work/fixture.mp4:/fixture.mp4:ro" -v "$repo/tests/fixture/source.py:/source.py:ro" -e FIXTURE_MEDIA=/fixture.mp4 -e FIXTURE_BASE_URL=http://fixture:18082 python:3.12-slim python /source.py
docker compose -p sp-upgrade-check exec -T stream-picker python -c 'from app import config; config.save({"JELLYFIN_URL":"http://fixture:18082", "JELLYFIN_USERNAME":"fixture", "JELLYFIN_PASSWORD":"fixture-password"})'
docker compose -p sp-upgrade-check restart stream-picker
```

Wait for `curl -fsS http://localhost:18091/health/ready` to succeed. Run the following check before **and** after replacing the image. It follows the same player install URL and compares the returned library stream bytes with the original fixture. Container DNS is used because the addon must also probe its own signed URL.

```bash
python3 -c 'from pathlib import Path; assert "fixture-password" not in Path("data/config.json").read_text()'
docker compose -p sp-upgrade-check exec -T stream-picker python -c 'from app import config; config.apply_env(); import os; assert os.getenv("JELLYFIN_PASSWORD")=="fixture-password"'
docker compose -p sp-upgrade-check exec -T stream-picker python -c 'import urllib.request,json; base="http://stream-picker:8000/upgrade-fixture-secret-1234567890"; manifest=json.load(urllib.request.urlopen(base+"/manifest.json")); assert manifest["id"]=="org.streampicker.auto"; streams=json.load(urllib.request.urlopen(base+"/stream/movie/tt0133093.json"))["streams"]; url=next(s["url"] for s in streams if "/library/" in s.get("url", "")); r=urllib.request.urlopen(urllib.request.Request(url,headers={"Range":"bytes=0-65535"})); data=r.read(); expected=urllib.request.urlopen(urllib.request.Request("http://fixture:18082/media2.mp4",headers={"Range":"bytes=0-65535"})).read(); assert r.status==206 and len(data)==65536 and data==expected; print("manifest, streams, media range OK")'
```

Replace the image and wait for readiness before repeating the check:

```bash
sha256sum data/config.json secrets/stream-picker-config.key > before.sha256
sed -i 's/sp-upgrade-prior/sp-upgrade-candidate/' docker-compose.yml
docker compose -p sp-upgrade-check up -d
sha256sum -c before.sha256
```

Remove only the disposable resources after checking:

```bash
docker rm -f sp-upgrade-check-fixture
docker compose -p sp-upgrade-check down
```
