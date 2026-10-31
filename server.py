#!/usr/bin/env python3
"""Сервер многопользовательской игры «Суд над героями нашего времени».

Только стандартная библиотека Python 3.9+, ничего устанавливать не нужно.
Запуск:   python3 server.py            (порт 8000)
          PORT=80 python3 server.py    (другой порт)
Регистрация и коды не нужны: игра одна, открытая для всех, кто зашёл по ссылке.
Героя для защиты и обвинения участник выбирает у себя в браузере, присяжные во втором раунде оценивают всех героев.
Раунды, итоги и новую игру может запускать любой участник. Состояние хранится в памяти сервера:
после перезапуска сервера игра начинается заново.
За обратным прокси (nginx, Caddy) работает по обычному HTTPS без доп. настроек.
"""
import json
import os
import secrets
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
PLAYERS = ("pechorin", "azamat", "kazbich")
CRITERIA = ("evidence", "persuasion", "answers")
MAX_SCORE = 5
MAX_MEMBERS = 150
ACTIVE_WINDOW = 20
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

LOCK = threading.Lock()
ROOM = {}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def new_votes():
    return {p: {c: {} for c in CRITERIA} for p in PLAYERS}


def get_room():
    if not ROOM:
        ROOM.update(
            round=1,
            finalized=False,
            verdict=None,
            members={},
            votes=new_votes(),
        )
    return ROOM


def get_member(room, pid):
    if pid not in room["members"]:
        raise ApiError(403, "Вы не участвуете в этой игре.")
    member = room["members"][pid]
    member["seen"] = time.time()
    return member


def totals_of(room):
    averages, totals = {}, {}
    for p in PLAYERS:
        averages[p] = {}
        for c in CRITERIA:
            values = list(room["votes"][p][c].values())
            averages[p][c] = {
                "avg": sum(values) / len(values) if values else None,
                "count": len(values),
            }
        avgs = [averages[p][c]["avg"] for c in CRITERIA]
        totals[p] = None if any(a is None for a in avgs) else sum(avgs)
    return averages, totals


def snapshot(room, pid):
    averages, totals = totals_of(room)
    my_votes = {
        p: {c: room["votes"][p][c][pid] for c in CRITERIA if pid in room["votes"][p][c]}
        for p in PLAYERS
    }
    return {
        "round": room["round"],
        "finalized": room["finalized"],
        "verdict": room["verdict"],
        "isHost": True,
        "memberCount": sum(1 for m in room["members"].values() if time.time() - m["seen"] < ACTIVE_WINDOW),
        "averages": averages,
        "totals": totals,
        "myVotes": my_votes,
    }


def prune_members(room):
    now = time.time()
    for pid in [k for k, m in room["members"].items() if now - m["seen"] > 3600]:
        del room["members"][pid]


def api_join(body):
    room = get_room()
    prune_members(room)
    if len(room["members"]) >= MAX_MEMBERS:
        raise ApiError(403, "В игре уже максимальное число участников.")
    pid = secrets.token_urlsafe(16)
    room["members"][pid] = {"seen": time.time()}
    return {"pid": pid}


def api_vote(body):
    room = get_room()
    pid = body.get("pid")
    get_member(room, pid)
    if room["round"] != 2:
        raise ApiError(409, "Голосование начнётся во втором раунде.")
    if room["finalized"]:
        raise ApiError(409, "Голосование уже завершено.")
    player, criterion, value = body.get("player"), body.get("criterion"), body.get("value")
    if player not in PLAYERS or criterion not in CRITERIA:
        raise ApiError(400, "Неизвестный персонаж или критерий.")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SCORE:
        raise ApiError(400, f"Оценка должна быть целым числом от 0 до {MAX_SCORE}.")
    room["votes"][player][criterion][pid] = value
    return {}


def require_host(room, pid):
    get_member(room, pid)


def api_round(body):
    room = get_room()
    require_host(room, body.get("pid"))
    if body.get("round") != 2:
        raise ApiError(400, "Неверный раунд.")
    room["round"] = 2
    return {}


def api_finalize(body):
    room = get_room()
    require_host(room, body.get("pid"))
    if room["round"] != 2:
        raise ApiError(409, "Сначала завершите первый раунд.")
    _, totals = totals_of(room)
    if any(t is None for t in totals.values()):
        raise ApiError(409, "Чтобы подвести итоги, нужны оценки по каждому критерию для каждого персонажа.")
    lowest = min(totals.values())
    losers = [p for p in PLAYERS if abs(totals[p] - lowest) < 1e-9]
    room["finalized"] = True
    room["verdict"] = {"losers": losers, "lowest": lowest}
    return {}


def api_reopen(body):
    room = get_room()
    require_host(room, body.get("pid"))
    room["finalized"] = False
    room["verdict"] = None
    return {}


def api_reset(body):
    room = get_room()
    require_host(room, body.get("pid"))
    room.update(round=1, finalized=False, verdict=None, votes=new_votes())
    return {}


POST_ROUTES = {
    "/api/join": api_join,
    "/api/vote": api_vote,
    "/api/round": api_round,
    "/api/finalize": api_finalize,
    "/api/reopen": api_reopen,
    "/api/reset": api_reset,
}


def static_files():
    files = {}
    for path in BASE.iterdir():
        if path.is_file() and path.suffix.lower() in STATIC_TYPES:
            files[unicodedata.normalize("NFC", path.name)] = path
    return files


class Handler(BaseHTTPRequestHandler):
    server_version = "CourtGame/1.0"

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/state":
            query = parse_qs(url.query)
            try:
                with LOCK:
                    room = get_room()
                    pid = query.get("pid", [""])[0]
                    get_member(room, pid)
                    payload = snapshot(room, pid)
                self.send_json(200, payload)
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            return
        self.serve_static(url.path)

    def do_HEAD(self):
        self.serve_static(urlparse(self.path).path, head_only=True)

    def do_POST(self):
        route = POST_ROUTES.get(urlparse(self.path).path)
        if not route:
            self.send_json(404, {"error": "Не найдено."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ApiError(413, "Слишком большой запрос.")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(400, "Неверный формат запроса.")
            if not isinstance(body, dict):
                raise ApiError(400, "Неверный формат запроса.")
            with LOCK:
                result = route(body)
            self.send_json(200, result)
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})

    def serve_static(self, raw_path, head_only=False):
        name = unicodedata.normalize("NFC", unquote(raw_path)).lstrip("/") or "index.html"
        path = static_files().get(name)
        if path is None:
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[path.suffix.lower()])
        self.send_header("Content-Length", str(len(data)))
        cache = "no-cache" if path.suffix.lower() == ".html" else "public, max-age=86400"
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def log_message(self, fmt, *args):
        if os.environ.get("COURT_LOG"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Игра доступна на http://localhost:{port}")
    httpd.serve_forever()
