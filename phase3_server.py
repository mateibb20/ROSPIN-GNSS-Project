import json
import mimetypes
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ROSPIN_proj import GRID_SIZE_DEGREES, setup_database, setup_grid_tables


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DB_PATH = ROOT / "romania_black_sea_adsb.db"
HOST = "127.0.0.1"
PORT = 8000


def ensure_database():
    setup_database()
    with sqlite3.connect(DB_PATH) as connection:
        setup_grid_tables(connection)
        connection.commit()


class MapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self, sql, params=()):
        if not DB_PATH.exists():
            return []
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/dates":
            dates = self._query(
                "SELECT DISTINCT archive_date FROM daily_grid "
                "WHERE archive_date IS NOT NULL ORDER BY archive_date DESC"
            )
            self._send_json({"dates": [row["archive_date"] for row in dates]})
            return

        if parsed.path == "/api/grid":
            query = parse_qs(parsed.query)
            selected_date = query.get("date", [None])[0]
            if selected_date:
                rows = self._query(
                    "SELECT * FROM daily_grid WHERE archive_date = ? "
                    "ORDER BY grid_row, grid_col",
                    (selected_date,),
                )
            else:
                rows = self._query(
                    "SELECT * FROM daily_grid ORDER BY archive_date DESC, grid_row, grid_col"
                )
            self._send_json({"grid_size_degrees": GRID_SIZE_DEGREES, "cells": rows})
            return

        if parsed.path == "/api/status":
            counts = self._query(
                "SELECT (SELECT COUNT(*) FROM flights) AS flights, "
                "(SELECT COUNT(*) FROM daily_grid) AS grid_cells"
            )
            self._send_json(counts[0] if counts else {"flights": 0, "grid_cells": 0})
            return

        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


if __name__ == "__main__":
    ensure_database()
    print(f"ROSPIN map available at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the server.")
    ThreadingHTTPServer((HOST, PORT), MapHandler).serve_forever()
