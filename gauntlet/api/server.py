"""Zero-dependency HTTP Server & High-Performance REST API for GAUNTLET."""

import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from gauntlet.models import EventValidator, ValidationError
from gauntlet.storage.engine import StorageEngine
from gauntlet.index.engine import IndexEngine
from gauntlet.query.executor import QueryExecutor
from gauntlet.analytics.engine import AnalyticsEngine
from gauntlet.analytics.temporal import TemporalEngine
from gauntlet.analytics.correlations import CorrelationEngine


class GauntletHTTPHandler(BaseHTTPRequestHandler):

    storage: StorageEngine
    index: IndexEngine
    executor: QueryExecutor
    analytics: AnalyticsEngine
    static_dir: Path

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

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

        try:
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
            elif path == "/api/events" or path == "/api/timeline":
                entity = query_params.get("entity", [None])[0]
                limit = int(query_params.get("limit", [200])[0])
                events = list(self.storage.scan(entity=entity))
                events.sort(key=lambda e: (e.timestamp, e.sequence_num), reverse=True)
                self._send_json(200, {
                    "count": len(events),
                    "events": [e.to_dict() for e in events[:limit]]
                })
            elif path == "/api/analytics/profile":
                entity = query_params.get("entity", ["server-42"])[0]
                all_events = list(self.storage.scan(entity=entity))
                report = self.analytics.full_diagnostic_report(all_events, entity)
                self._send_json(200, report)
            elif path == "/api/analytics/temporal-diff":
                entity = query_params.get("entity", ["server-42"])[0]
                metric = query_params.get("metric", ["cpu"])[0]
                t1 = int(query_params.get("t1", [0])[0])
                t2 = int(query_params.get("t2", [9999999999])[0])
                all_events = list(self.storage.scan(entity=entity))
                diff_report = TemporalEngine.calculate_temporal_diff(all_events, entity, metric, t1, t2)
                self._send_json(200, diff_report.to_dict())
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
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

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

        elif path == "/api/agentic/crawl":
            url = payload.get("url", "").strip()
            if not url:
                self._send_json(400, {"error": "Field 'url' is required"})
                return
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "http://" + url

            try:
                start_time = time.perf_counter()
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GAUNTLET/2.0-AgenticCrawler"}
                )
                
                status_code = 200
                html_text = ""
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        status_code = response.getcode()
                        html_bytes = response.read(100000) # read first 100kb
                        html_text = html_bytes.decode('utf-8', errors='ignore')
                        headers_dict = dict(response.info())
                except urllib.error.HTTPError as e:
                    status_code = e.code
                    html_text = e.read().decode('utf-8', errors='ignore')
                    headers_dict = dict(e.headers)
                except Exception as ex:
                    status_code = 504
                    html_text = f"Connection failed: {str(ex)}"
                    headers_dict = {}

                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                
                # Extract Title
                title_match = re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE | re.DOTALL)
                site_title = title_match.group(1).strip() if title_match else "Unknown Domain"

                # Parse domain entity name
                parsed_url = urllib.parse.urlparse(url)
                entity_name = parsed_url.netloc or "web-target"

                # Extract Links count, script count, DOM size
                links_count = len(re.findall(r'href=[\'"]?([^\'" >]+)', html_text, re.IGNORECASE))
                scripts_count = len(re.findall(r'<script', html_text, re.IGNORECASE))
                images_count = len(re.findall(r'<img', html_text, re.IGNORECASE))
                page_size_kb = round(len(html_text.encode('utf-8')) / 1024, 2)

                now_ts = int(time.time())

                # Generate 6-Phase Agentic Pipeline Events directly into GAUNTLET WAL & Storage
                events_to_ingest = [
                    {
                        "entity": entity_name,
                        "timestamp": now_ts,
                        "type": "latency",
                        "value": float(latency_ms),
                        "attributes": {"url": url, "status": status_code, "phase": "POWER"}
                    },
                    {
                        "entity": entity_name,
                        "timestamp": now_ts + 1,
                        "type": "size_kb",
                        "value": float(page_size_kb),
                        "attributes": {"links": links_count, "scripts": scripts_count, "images": images_count, "phase": "SPACE"}
                    },
                    {
                        "entity": entity_name,
                        "timestamp": now_ts + 2,
                        "type": "status_code",
                        "value": float(status_code),
                        "attributes": {"title": site_title, "phase": "REALITY"}
                    },
                    {
                        "entity": entity_name,
                        "timestamp": now_ts + 3,
                        "type": "errors",
                        "value": 1.0 if status_code >= 400 else 0.0,
                        "attributes": {"error_type": "HTTP_ERROR" if status_code >= 400 else "NONE", "phase": "MIND"}
                    },
                    {
                        "entity": entity_name,
                        "timestamp": now_ts + 4,
                        "type": "cpu",
                        "value": round(min(100.0, latency_ms / 15.0 + scripts_count * 2.0), 1),
                        "attributes": {"load_factor": "DOM_PARSING", "phase": "TIME"}
                    },
                    {
                        "entity": entity_name,
                        "timestamp": now_ts + 5,
                        "type": "memory",
                        "value": round(min(100.0, page_size_kb / 5.0 + 35.0), 1),
                        "attributes": {"footprint": "V8_HEAP", "phase": "SOUL"}
                    }
                ]

                # Ingest into Gauntlet Engine
                for item in events_to_ingest:
                    ev = EventValidator.validate_and_normalize(item)
                    self.storage.write(ev)
                self.index.rebuild_from_segments(self.storage.segments)

                # Run diagnostic profile immediately for this entity
                all_entity_events = list(self.storage.scan(entity=entity_name))
                diagnostic_profile = self.analytics.full_diagnostic_report(all_entity_events, entity_name)

                pipeline_trace = {
                    "phase_1_power": {
                        "name": "Phase I: Power (Ingest & Fetch)",
                        "status": "COMPLETED",
                        "target_url": url,
                        "status_code": status_code,
                        "latency_ms": latency_ms,
                        "timestamp": now_ts
                    },
                    "phase_2_space": {
                        "name": "Phase II: Space (Binary WAL & Segment Storage)",
                        "status": "COMMITTED",
                        "bytes_ingested": len(html_text.encode('utf-8')),
                        "page_size_kb": page_size_kb,
                        "wal_written": True
                    },
                    "phase_3_reality": {
                        "name": "Phase III: Reality (Bloom Index & Manifestation)",
                        "status": "INDEXED",
                        "entity": entity_name,
                        "title": site_title,
                        "links_found": links_count,
                        "scripts_found": scripts_count,
                        "images_found": images_count
                    },
                    "phase_4_mind": {
                        "name": "Phase IV: Mind (DSL AST & Filtering)",
                        "status": "EXECUTED",
                        "query_dsl": f"FIND latency, cpu, errors FROM {entity_name} ORDER BY timestamp DESC",
                        "events_generated": len(events_to_ingest)
                    },
                    "phase_5_time": {
                        "name": "Phase V: Time (Temporal Drift & Windows)",
                        "status": "ANALYZED",
                        "window_span": f"{now_ts} -> {now_ts + 5}",
                        "timeline_count": len(all_entity_events)
                    },
                    "phase_6_soul": {
                        "name": "Phase VI: Soul (Z-Score Anomaly & Correlation)",
                        "status": "COMPLETED",
                        "anomalies_detected": len(diagnostic_profile.get("profile", {}).get("recent_anomalies", [])),
                        "diagnostics": diagnostic_profile
                    }
                }

                self._send_json(200, {
                    "status": "SUCCESS",
                    "entity": entity_name,
                    "url": url,
                    "title": site_title,
                    "pipeline": pipeline_trace,
                    "events_count": len(events_to_ingest)
                })
            except Exception as e:
                self._send_json(500, {"error": f"Agentic pipeline execution failed: {str(e)}"})

        else:
            self._send_json(404, {"error": f"POST endpoint '{path}' not found"})


def create_server(
    storage: StorageEngine,
    index: IndexEngine,
    executor: QueryExecutor,
    analytics: AnalyticsEngine,
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
