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
import threading

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


# Thread-safe data structures
class ThreadSafeDict:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._data[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self._data[key]

    def __contains__(self, key):
        with self._lock:
            return key in self._data

    def pop(self, key, default=None):
        with self._lock:
            return self._data.pop(key, default)

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def values(self):
        with self._lock:
            return list(self._data.values())

    def items(self):
        with self._lock:
            return list(self._data.items())

    def clear(self):
        with self._lock:
            self._data.clear()

    def update(self, other):
        with self._lock:
            self._data.update(other)

    def copy(self):
        with self._lock:
            return self._data.copy()


class ThreadSafeList:
    def __init__(self):
        self._data = []
        self._lock = threading.Lock()

    def append(self, item):
        with self._lock:
            self._data.append(item)

    def remove(self, item):
        with self._lock:
            self._data.remove(item)

    def __contains__(self, item):
        with self._lock:
            return item in self._data

    def __iter__(self):
        with self._lock:
            return iter(self._data.copy())

    def __len__(self):
        with self._lock:
            return len(self._data)

    def clear(self):
        with self._lock:
            self._data.clear()


# Initialize thread-safe data structures
rooms = ThreadSafeDict()  # room_id -> player name -> points
rooms_state = ThreadSafeDict()  # room_id -> room state
connections = ThreadSafeDict()  # room_id -> websocket list (ThreadSafeList)
used_questions = ThreadSafeDict()  # room_id -> list of used indexes
current_questions = ThreadSafeDict()  # room_id -> {"real_question": {real_question}, "fake_question": {fake_question}}
current_answers = ThreadSafeDict()  # room_id -> {player name -> answer}
current_votes = ThreadSafeDict()  # room_id -> {voter_name: voted_player_name}
current_voted_player = ThreadSafeDict()  # room_id -> voted player
current_vote_counts = ThreadSafeDict()  # room_id -> {player_name -> number of votes}
current_valid_voting = ThreadSafeDict()  # room_id -> valid voting
current_liar = ThreadSafeDict()  # room_id -> actual liar
current_diff_points = ThreadSafeDict()  # room_id -> {player: points_diff}


# Initialize connections as ThreadSafeDict of ThreadSafeLists
def get_connection_list(room_id):
    if room_id not in connections:
        connections[room_id] = ThreadSafeList()
    return connections[room_id]


with open('app/question_pool.txt', 'r', encoding='utf-8') as file:
    questions_pool = [line.strip() for line in file if line.strip()]


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, error: str = None):
    return templates.TemplateResponse("form.html", {"request": request, "error": error})


@app.post("/join", response_class=HTMLResponse)
async def join_room(room_id: int = Form(...), name: str = Form(...)):
    if room_id in rooms and name in rooms[room_id]:
        return RedirectResponse(f"/?error=Name%20already%20taken%20in%20Room%20{room_id}", status_code=302)

    if room_id in rooms_state and rooms_state[room_id] is not State.ROOM:
        return RedirectResponse(f"/?error=Game%20already%20started%20in%20Room%20{room_id}", status_code=302)
    if room_id not in rooms:
        rooms[room_id] = {}
    rooms[room_id][name] = 0  # Initialize with 0 points
    rooms_state[room_id] = State.ROOM

    # Broadcast new list of players
    asyncio.create_task(broadcast_player_list(room_id))

    return RedirectResponse(f"/room/{room_id}?name={name}", status_code=302)


async def broadcast_answers_submitted(room_id: int):
    rooms_state[room_id] = State.DISCUSSION
    message = {
        "action": "answers_submitted",
        "real_question": current_questions[room_id]["real_question"]
    }
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(message)
        except:
            pass


async def broadcast_start_voting(room_id: int):
    rooms_state[room_id] = State.VOTING
    current_votes.pop(room_id, None)
    message = {
        "action": "start_voting"
    }
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(message)
        except:
            pass


async def broadcast_show_points(room_id: int):
    rooms_state[room_id] = State.POINTS
    liar = current_liar.get(room_id)
    voted = current_voted_player.get(room_id)
    current_votes_ = current_votes.get(room_id, {})
    prev_points = rooms.get(room_id, {}).copy()

    if voted != liar:
        rooms[room_id][liar] = rooms[room_id].get(liar, 0) + 3

    for player in rooms.get(room_id, {}):
        if player == liar:
            continue
        if current_votes_.get(player) == liar:
            rooms[room_id][player] = rooms[room_id].get(player, 0) + 1

    points = rooms.get(room_id, {})
    prev_points_dict = prev_points if isinstance(prev_points, dict) else {}
    diff = {player: points.get(player, 0) - prev_points_dict.get(player, 0)
            for player in set(prev_points_dict) | set(points)}
    current_diff_points[room_id] = diff
    message = {
        "action": "show_points",
        "points": points,
        "liar": liar,
        "diff": diff
    }
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(message)
        except:
            pass


async def broadcast_votes_submited(room_id: int):
    room_votes = current_votes.get(room_id, {})
    vote_counts = defaultdict(int)
    for voted_player in room_votes.values():
        vote_counts[voted_player] += 1

    current_vote_counts[room_id] = dict(vote_counts)

    max_votes = max(vote_counts.values(), default=0)
    top_voted_players = [player for player, votes in vote_counts.items() if votes == max_votes]
    valid_voting = len(top_voted_players) == 1
    current_valid_voting[room_id] = valid_voting
    if valid_voting:
        rooms_state[room_id] = State.VOTING_RESULTS
        current_voted_player[room_id] = top_voted_players[0]
    else:
        rooms_state[room_id] = State.VOTE_AGAIN
    message = {
        "action": "votes_submitted",
        "votes": dict(vote_counts),
        "validVoting": valid_voting,
    }
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(message)
        except:
            pass


@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: int, name: str):
    players = list(rooms.get(room_id, {}).keys())
    return templates.TemplateResponse("room.html", {
        "request": request,
        "room_id": room_id,
        "players": players,
        "name": name
    })


async def broadcast_player_list(room_id: int):
    players = list(rooms.get(room_id, {}).keys())
    data = {"players": players}
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(data)
        except:
            pass


async def broadcast_next_round(room_id: int):
    current_answers.pop(room_id, None)
    current_votes.pop(room_id, None)
    players = list(rooms.get(room_id, {}).keys())
    if len(players) < 2:
        return JSONResponse({"error": "Not enough players"}, status_code=400)

    total_questions = len(questions_pool)
    used = used_questions.get(room_id, [])
    available_indexes = [i for i in range(total_questions) if i not in used]

    if len(available_indexes) < 2:
        return JSONResponse({"error": "Not enough unused questions"}, status_code=400)

    rooms_state[room_id] = State.ANSWER

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
    used_questions[room_id] = used + [same_idx, odd_idx]

    data = {"action": "ping_start_game"}
    for ws in get_connection_list(room_id):
        try:
            await ws.send_json(data)
        except:
            pass

    return JSONResponse({"message": f"Game started."})


@app.post("/start_game/{room_id}")
async def start_game(room_id: int):
    return await broadcast_next_round(room_id)


@app.get("/reset")
async def reset_app():
    # Broadcast redirect to home for all active WebSockets
    all_connections = {}
    for room_id in connections.keys():
        all_connections[room_id] = list(get_connection_list(room_id))

    for room_id, websockets in all_connections.items():
        for ws in websockets:
            try:
                await ws.send_json({"action": "redirect", "target": "/"})
            except:
                pass  # ignore errors on dead sockets

    # Now it's safe to clear the data
    rooms.clear()
    rooms_state.clear()
    connections.clear()
    used_questions.clear()
    current_questions.clear()
    current_answers.clear()
    current_votes.clear()
    current_voted_player.clear()
    current_vote_counts.clear()
    current_valid_voting.clear()
    current_liar.clear()
    current_diff_points.clear()

    return RedirectResponse(f"/?error=Reset%20Success", status_code=302)


async def sendPackage(ws, data):
    try:
        await ws.send_json(data)
    except:
        pass


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    await websocket.accept()
    room_id = int(room_id)
    player_name = websocket.query_params.get("name")

    conn_list = get_connection_list(room_id)
    conn_list.append(websocket)

    if rooms_state.get(room_id) == State.ROOM:
        rooms[room_id][player_name] = 0
        await broadcast_player_list(room_id)
    else:
        players = list(rooms.get(room_id, {}).keys())

        if player_name not in players:
            await sendPackage(websocket, {"action": "redirect"})

        your_question = current_questions[room_id]["fake_question"] if player_name == current_liar.get(room_id) else \
        current_questions[room_id]["real_question"]

        already_answered = player_name in current_answers.get(room_id, {})
        answer = current_answers.get(room_id, {}).get(player_name, "")

        real_question = current_questions.get(room_id, {}).get("real_question", "")

        already_voted = player_name in current_votes.get(room_id, {})
        vote = current_votes.get(room_id, {}).get(player_name, "")

        current_state = rooms_state.get(room_id)
        data = {
            "action": "state",
            "state": current_state.name if current_state else None,
            "players": players,
            "your_question": your_question,
            "already_answered": already_answered,
            "your_answer": answer,
            "real_question": real_question,
            "already_voted": already_voted,
            "your_vote": vote
        }

        if current_state in [State.VOTING_RESULTS, State.VOTE_AGAIN, State.POINTS]:
            data.update({
                "votes_count": current_vote_counts.get(room_id, {}),
                "valid_voting": current_valid_voting.get(room_id, False)
            })

        if current_state == State.POINTS:
            data.update({
                "points": rooms.get(room_id, {}),
                "liar": current_liar.get(room_id),
                "diff": current_diff_points.get(room_id, {})
            })

        await sendPackage(websocket, data)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "submit_answer":
                name = data.get("name")
                answer = data.get("answer")
                if room_id not in current_answers:
                    current_answers[room_id] = {}
                current_answers[room_id][name] = answer
                if len(current_answers[room_id]) == len(rooms.get(room_id, {})):
                    await broadcast_answers_submitted(room_id)

            elif action == "pong_start_game":
                name = data.get("name")
                your_question = current_questions[room_id]["fake_question"] if name == current_liar.get(room_id) else \
                current_questions[room_id]["real_question"]
                await sendPackage(websocket, {
                    "action": "start_game",
                    "question": your_question
                })

            elif action == "start_voting_request":
                await broadcast_start_voting(room_id)

            elif action == "submit_vote":
                voted = data.get("target")
                voter = data.get("voter")
                if room_id not in current_votes:
                    current_votes[room_id] = {}
                current_votes[room_id][voter] = voted
                if len(current_votes[room_id]) == len(rooms.get(room_id, {})):
                    await broadcast_votes_submited(room_id)

            elif action == "show_points_request":
                await broadcast_show_points(room_id)

            elif action == "next_round_request":
                await broadcast_next_round(room_id)

            elif action == "vote_again_request":
                await broadcast_start_voting(room_id)

    except WebSocketDisconnect:
        conn_list.remove(websocket)
        if rooms_state.get(room_id) == State.ROOM:
            if room_id in rooms and player_name in rooms[room_id]:
                del rooms[room_id][player_name]
            await broadcast_player_list(room_id)