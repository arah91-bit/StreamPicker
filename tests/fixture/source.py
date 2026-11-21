"""Standalone synthetic Stremio source for release verification, never production."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MEDIA = Path(os.environ.get('FIXTURE_MEDIA', 'fixture.mp4')).read_bytes()
BASE = os.environ['FIXTURE_BASE_URL'].rstrip('/')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/manifest.json':
            return self.json({'id': 'org.streampicker.fixture', 'version': '1.0.0',
                              'name': 'Synthetic fixture', 'resources': ['stream'],
                              'types': ['movie'], 'catalogs': [], 'idPrefixes': ['tt']})
        if self.path.startswith('/stream/movie/') and self.path.endswith('.json'):
            return self.json({'streams': [{'name': 'Synthetic fixture',
                              'description': 'The.Matrix.1999.1080p.WEB-DL.H264',
                              'url': BASE + '/media2.mp4'}]})
        if self.path == '/media2.mp4':
            start, end = 0, len(MEDIA) - 1
            value = self.headers.get('Range', '')
            if value.startswith('bytes='):
                try:
                    a, b = value[6:].split('-', 1)
                    start = int(a) if a else max(0, len(MEDIA) - int(b))
                    end = min(int(b), end) if a and b else end
                    if start > end or start >= len(MEDIA): raise ValueError()
                except ValueError:
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{len(MEDIA)}'); self.end_headers(); return
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(MEDIA)}')
            else: self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers(); self.wfile.write(MEDIA[start:end + 1]); return
        self.send_error(404)

    def json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


ThreadingHTTPServer(('0.0.0.0', 18082), Handler).serve_forever()
