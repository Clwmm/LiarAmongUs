from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
import asyncio

from typing import List, Dict

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

rooms: Dict[int, List[str]] = {}  # room_id -> list of names
connections: Dict[int, List[WebSocket]] = {}  # room_id -> list of sockets

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, error: str = None):
    return templates.TemplateResponse("form.html", {"request": request, "error": error})

@app.post("/join", response_class=HTMLResponse)
async def join_room(room_id: int = Form(...), name: str = Form(...)):
    if room_id in rooms and name in rooms[room_id]:
        return RedirectResponse(f"/?error=Name%20already%20taken%20in%20Room%20{room_id}", status_code=302)

    if room_id not in rooms:
        rooms[room_id] = []
    rooms[room_id].append(name)

    # Broadcast new list of players
    asyncio.create_task(broadcast_player_list(room_id))

    return RedirectResponse(f"/room/{room_id}?name={name}", status_code=302)

@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: int, name: str):
    players = rooms.get(room_id, [])
    return templates.TemplateResponse("room.html", {
        "request": request,
        "room_id": room_id,
        "players": players,
        "name": name
    })

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    await websocket.accept()

    if room_id not in connections:
        connections[room_id] = []
    connections[room_id].append(websocket)

    try:
        while True:
            await websocket.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        connections[room_id].remove(websocket)

# Helper function to broadcast updates
async def broadcast_player_list(room_id: int):
    players = rooms.get(room_id, [])
    data = {"players": players}
    for ws in connections.get(room_id, []):
        await ws.send_json(data)
