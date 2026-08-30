"""Zero-dependency HTTP Server & REST API for GAUNTLET."""

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from gauntlet.models import EventValidator, ValidationError
from gauntlet.storage.engine import StorageEngine
from gauntlet.index.engine import IndexEngine
from gauntlet.query.executor import QueryExecutor
from gauntlet.contracts.analytics import BuiltinAnalyticsEngine


class GauntletHTTPHandler(BaseHTTPRequestHandler):

    storage: StorageEngine
    index: IndexEngine
    executor: QueryExecutor
    analytics: BuiltinAnalyticsEngine
    static_dir: Path

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self._send_json(200, {"status": "ONLINE", "version": "1.0.0"})
        elif path == "/api/stats":
            integrity = self.storage.verify_integrity()
            self._send_json(200, {
                "storage": integrity,
                "segments": [s.to_dict() for s in self.storage.segments],
                "memtable_events": len(self.storage.memtable),
                "wal_size_bytes": self.storage.wal_path.stat().st_size if self.storage.wal_path.exists() else 0
            })
        elif path == "/api/events":
            entity = query_params.get("entity", [None])[0]
            limit = int(query_params.get("limit", [100])[0])
            events = list(self.storage.scan(entity=entity))[:limit]
            self._send_json(200, {
                "count": len(events),
                "events": [e.to_dict() for e in events]
            })
        elif path == "/api/analytics/profile":
            entity = query_params.get("entity", ["server-42"])[0]
            all_events = list(self.storage.scan(entity=entity))
            profile = self.analytics.generate_entity_profile(all_events, entity)
            self._send_json(200, profile.to_dict())
        elif path == "/" or path == "/index.html":
            index_html = self.static_dir / "index.html"
            if index_html.exists():
                content = index_html.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self._send_json(404, {"error": "UI index.html not found"})
        elif path.startswith("/static/"):
            file_name = path[len("/static/"):]
            file_path = self.static_dir / file_name
            if file_path.exists() and file_path.is_file():
                content = file_path.read_bytes()
                mime = "text/css" if file_path.suffix == ".css" else "application/javascript" if file_path.suffix == ".js" else "image/png" if file_path.suffix == ".png" else "text/plain"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self._send_json(404, {"error": "Static file not found"})
        else:
            self._send_json(404, {"error": f"Endpoint '{path}' not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON request body"})
            return

        if path == "/api/ingest":
            try:
                if isinstance(payload, list):
                    # Batch ingest
                    events = []
                    for item in payload:
                        e = EventValidator.validate_and_normalize(item)
                        self.storage.write(e)
                        events.append(e)
                    self.index.rebuild_from_segments(self.storage.segments)
                    self._send_json(200, {"status": "SUCCESS", "ingested_count": len(events)})
                else:
                    event = EventValidator.validate_and_normalize(payload)
                    seq = self.storage.write(event)
                    self.index.rebuild_from_segments(self.storage.segments)
                    self._send_json(200, {"status": "SUCCESS", "sequence_num": seq, "event": event.to_dict()})
            except ValidationError as ve:
                self._send_json(400, {"error": str(ve)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        elif path == "/api/query":
            query_str = payload.get("query", "")
            if not query_str:
                self._send_json(400, {"error": "Field 'query' is required"})
                return
            try:
                result = self.executor.execute(query_str)
                self._send_json(200, result.to_dict())
            except Exception as e:
                self._send_json(400, {"error": str(e)})

        elif path == "/api/storage/flush":
            meta = self.storage.flush()
            if meta:
                self.index.register_segment(meta)
            self._send_json(200, {"flushed_segment": meta.to_dict() if meta else None})

        elif path == "/api/storage/compact":
            compacted = self.storage.compact()
            if compacted:
                self.index.rebuild_from_segments(self.storage.segments)
            self._send_json(200, {"compacted_segment": compacted.to_dict() if compacted else None})

        else:
            self._send_json(404, {"error": f"POST endpoint '{path}' not found"})


def create_server(
    storage: StorageEngine,
    index: IndexEngine,
    executor: QueryExecutor,
    analytics: BuiltinAnalyticsEngine,
    host: str = "127.0.0.1",
    port: int = 8080,
    static_dir: Optional[Path] = None
) -> HTTPServer:
    GauntletHTTPHandler.storage = storage
    GauntletHTTPHandler.index = index
    GauntletHTTPHandler.executor = executor
    GauntletHTTPHandler.analytics = analytics
    GauntletHTTPHandler.static_dir = static_dir or (Path(__file__).parent.parent.parent / "web" / "static")

    return HTTPServer((host, port), GauntletHTTPHandler)
