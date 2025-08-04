from collections import defaultdict
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from enum import Enum, auto
import random

import asyncio

from typing import List, Dict

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

class State(Enum):
    ROOM = auto()
    ANSWER = auto()
    DISCUSSION = auto()
    VOTING = auto()
    VOTING_RESULTS = auto()
    VOTE_AGAIN = auto()
    POINTS = auto()

rooms: Dict[int, Dict[str, int]] = {}  # room_id -> player name -> points
rooms_state: Dict[int, State] = {} # room_id -> room state
connections: Dict[int, List[WebSocket]] = {}  # room_id -> websocket list
used_questions: Dict[int, List[int]] = {}  # room_id -> list of used indexes
current_questions: Dict[int, Dict[str, str]] = {} # room_id -> {"real_question": {real_question}, "fake_question": {fake_question}}
current_answers: Dict[int, int] = {} # room_id -> number of submitted answers
current_votes: Dict[int, Dict[str, str]] = {}  # room_id -> {voter_name: voted_player_name}
current_voted_player: Dict[int, str] = {} # room_is -> voted player
current_liar: Dict[int, str] = {} # room_id -> actual liar

with open('app/question_pool.txt', 'r', encoding='utf-8') as file:
    questions_pool = [line.strip() for line in file if line.strip()]


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, error: str = None):
    return templates.TemplateResponse("form.html", {"request": request, "error": error})

@app.post("/join", response_class=HTMLResponse)
async def join_room(room_id: int = Form(...), name: str = Form(...)):
    if room_id in rooms and name in rooms[room_id]:
        return RedirectResponse(f"/?error=Name%20already%20taken%20in%20Room%20{room_id}", status_code=302)

    if room_id not in rooms:
        rooms[room_id] = {}
    rooms[room_id][name] = 0  # Initialize with 0 points

    # Broadcast new list of players
    asyncio.create_task(broadcast_player_list(room_id))

    return RedirectResponse(f"/room/{room_id}?name={name}", status_code=302)

async def broadcast_answers_submitted(room_id: int):
    message = {
        "action": "answers_submitted",
        "real_question": current_questions[room_id]["real_question"]
    }
    for ws in connections.get(room_id, []):
        try:
            await ws.send_json(message)
        except:
            pass

    current_answers[room_id] = 0

async def broadcast_start_voting(room_id: int):
    message = {
        "action": "start_voting"
    }
    for ws in connections.get(room_id, []):
        try:
            await ws.send_json(message)
        except:
            pass

async def broadcast_show_points(room_id: int):
    liar = current_liar.get(room_id)
    voted = current_voted_player.get(room_id)

    if voted != liar:
        rooms[room_id][liar] += 3
    else:
        for player in rooms[room_id]:
            if player != liar:
                rooms[room_id][player] += 1

    points = rooms.get(room_id, {})
    message = {
        "action": "show_points",
        "points": points,
        "liar": liar
    }
    for ws in connections.get(room_id, []):
        try:
            await ws.send_json(message)
        except:
            pass

async def broadcast_votes_submited(room_id: int):
    room_votes = current_votes.get(room_id, {})
    vote_counts: Dict[str, int] = defaultdict(int)
    for voted_player in room_votes.values():
        vote_counts[voted_player] += 1

    max_votes = max(vote_counts.values())
    top_voted_players = [player for player, votes in vote_counts.items() if votes == max_votes]
    validVoting = len(top_voted_players) == 1
    if validVoting:
        current_voted_player[room_id] = top_voted_players[0]
    message = {
        "action": "votes_submitted",
        "votes": dict(vote_counts),
        "validVoting": validVoting,
    }
    for ws in connections.get(room_id, []):
        try:
            await ws.send_json(message)
        except:
            pass

    current_answers.pop(room_id, None)


@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: int, name: str):
    players = list(rooms.get(room_id, {}).keys())
    return templates.TemplateResponse("room.html", {
        "request": request,
        "room_id": room_id,
        "players": players,
        "name": name
    })

# Helper function to broadcast updates
async def broadcast_player_list(room_id: int):
    players = list(rooms.get(room_id, {}).keys())
    data = {"players": players}
    for ws in connections.get(room_id, []):
        await ws.send_json(data)

@app.post("/start_game/{room_id}")
async def start_game(room_id: int):
    players = list(rooms.get(room_id, {}).keys())
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
    current_questions[room_id] = {
        "real_question": same_q,
        "fake_question": odd_q
    }

    # Randomly choose the odd player
    odd_player = random.choice(players)
    current_liar[room_id] = odd_player

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
    # await broadcast_player_list(room_id)

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
    current_questions.clear()
    current_answers.clear()
    current_votes.clear()

    return RedirectResponse(f"/?error=Reset%20Success", status_code=302)


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    await websocket.accept()
    room_id = int(room_id)
    player_name = websocket.query_params.get("name")

    # Track player
    # if room_id not in rooms:
    #     rooms[room_id] = {}
    # if player_name not in rooms[room_id]:
    #     rooms[room_id][player_name] = 0  # Initialize with 0 points

    if room_id not in connections:
        connections[room_id] = []
    connections[room_id].append(websocket)

    await broadcast_player_list(room_id)

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "submit_answer":
                current_answers[room_id] = current_answers.get(room_id, 0) + 1
                if current_answers[room_id] == len(rooms[room_id]):
                    await broadcast_answers_submitted(room_id)

            if data.get("action") == "start_voting_request":
                await broadcast_start_voting(room_id)

            if data.get("action") == "submit_vote":
                voted = data.get("target")
                voter = data.get("voter")
                if room_id not in current_votes:
                    current_votes[room_id] = {}

                current_votes[room_id][voter] = voted
                if len(current_votes[room_id]) == len(rooms[room_id]):
                    await broadcast_votes_submited(room_id)

            if data.get("action") == "show_points_request":
                await broadcast_show_points(room_id)

    except WebSocketDisconnect:
        if room_id in connections and websocket in connections[room_id]:
            connections[room_id].remove(websocket)
        if room_id in rooms and player_name in rooms[room_id]:
            del rooms[room_id][player_name]
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


# TODO:
#   1. Wyświetlanie odpowiedzi na całym ekranie aby pokazywać innym
#   2. Dodanie zatwierdzenia głosowania, aby ktoś mógł naprawić błąd
#   3. Dodanie ukrywania oryginalnego pytania po głosowaniu i pokazywaniu odpowiedzi
#       3a. Dodanie przycisku "show" do oryginalnego pytania po grze
#   4. Check in answer state and voting state if user answered and voted to not show the submit answer
