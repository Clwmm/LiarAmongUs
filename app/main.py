from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
import random

import asyncio

from typing import List, Dict

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

rooms: Dict[int, List[str]] = {}  # room_id -> player names
connections: Dict[int, List[WebSocket]] = {}  # room_id -> websocket list
used_questions: Dict[int, List[int]] = {}  # room_id -> list of used indexes
votes: Dict[int, Dict[str, str]] = {}  # room_id -> {voter_name: voted_player_name}

questions_pool = [
    "What's your favorite food?",
    "Describe your dream vacation.",
    "What's a skill you wish you had?",
    "If you could switch lives with someone, who would it be?",
    "What scares you the most?",
    "What's your most embarrassing moment?",
]


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

async def broadcast_votes(room_id: int):
    vote_result = votes.get(room_id, {})
    message = {
        "action": "reveal_votes",
        "votes": vote_result
    }
    for ws in connections.get(room_id, []):
        try:
            await ws.send_json(message)
        except:
            pass

    # Optional: Clear votes for next round
    votes[room_id] = {}

@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: int, name: str):
    players = rooms.get(room_id, [])
    return templates.TemplateResponse("room.html", {
        "request": request,
        "room_id": room_id,
        "players": players,
        "name": name
    })

# Helper function to broadcast updates
async def broadcast_player_list(room_id: int):
    players = rooms.get(room_id, [])
    print(players)
    data = {"players": players}
    for ws in connections.get(room_id, []):
        await ws.send_json(data)

@app.post("/start_game/{room_id}")
async def start_game(room_id: int):
    players = rooms.get(room_id, [])
    if len(players) < 2:
        return JSONResponse({"error": "Not enough players"}, status_code=400)

    total_questions = len(questions_pool)
    used = used_questions.get(room_id, [])
    available_indexes = [i for i in range(total_questions) if i not in used]

    if len(available_indexes) < 2:
        return JSONResponse({"error": "Not enough unused questions"}, status_code=400)

    # Pick two indexes
    same_idx, odd_idx = random.sample(available_indexes, 2)
    same_q = questions_pool[same_idx]
    odd_q = questions_pool[odd_idx]

    # Randomly choose the odd player
    odd_player = random.choice(players)

    # Store used indexes, not the strings
    used.extend([same_idx, odd_idx])
    used_questions[room_id] = used

    # Send question to each player
    name_to_ws = dict(zip(players, connections.get(room_id, [])))
    for name, ws in name_to_ws.items():
        try:
            await ws.send_json({
                "action": "start_game",
                "question": odd_q if name == odd_player else same_q
            })
        except:
            pass

    # ✅ FIX: Broadcast player list after sending questions to trigger vote buttons on frontend
    await broadcast_player_list(room_id)

    return JSONResponse({"message": f"Game started. Odd player: {odd_player}"})


@app.get("/reset")
async def reset_app():
    # Broadcast redirect to home for all active WebSockets
    all_connections = dict(connections)  # copy to avoid iteration issues
    for room_id, websockets in all_connections.items():
        for ws in websockets:
            try:
                await ws.send_json({"action": "redirect", "target": "/"})
            except:
                pass  # ignore errors on dead sockets

    # Now it's safe to clear the data
    rooms.clear()
    connections.clear()
    used_questions.clear()

    return RedirectResponse(f"/?error=Reset%20Success", status_code=302)


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    await websocket.accept()
    room_id = int(room_id)
    player_name = websocket.query_params.get("name")

    # Track player
    # if room_id not in rooms:
    #     rooms[room_id] = []
    # if player_name not in rooms[room_id]:
    #     rooms[room_id].append(player_name)

    if room_id not in connections:
        connections[room_id] = []
    connections[room_id].append(websocket)

    await broadcast_player_list(room_id)

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "vote":
                voted = data.get("target")
                voter = data.get("voter")
                print("Voted:", voted)
                if room_id not in votes:
                    print("lol")
                    votes[room_id] = {}
                votes[room_id][voter] = voted
                print("votes: ", len(votes[room_id]), "\tplayers: ", len(rooms[room_id]))
                print(votes)

                # When all players have voted
                if len(votes[room_id]) == len(rooms[room_id]):
                    await broadcast_votes(room_id)

    except WebSocketDisconnect:
        if room_id in connections and websocket in connections[room_id]:
            connections[room_id].remove(websocket)
        if room_id in rooms and player_name in rooms[room_id]:
            rooms[room_id].remove(player_name)
        await broadcast_player_list(room_id)


# @app.websocket("/ws/{room_id}")
# async def websocket_endpoint(websocket: WebSocket, room_id: int):
#     await websocket.accept()
#
#     if room_id not in connections:
#         connections[room_id] = []
#     connections[room_id].append(websocket)
#
#     try:
#         while True:
#             await websocket.receive_text()  # keep connection alive
#     except WebSocketDisconnect:
#         connections[room_id].remove(websocket)

