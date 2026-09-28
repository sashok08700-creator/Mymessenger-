import os, uuid, json
from datetime import datetime
from flask import Flask, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = "local-chat"
socketio = SocketIO(app, cors_allowed_origins="*")

DATA_DIR = "data"
UP_DIR = os.path.join(DATA_DIR, "uploads")
ST_DIR = os.path.join(DATA_DIR, "stickers")
os.makedirs(UP_DIR, exist_ok=True)
os.makedirs(ST_DIR, exist_ok=True)

ROOMS = {}
USERS = {}
ALLOWED_IMG = {"png", "jpg", "jpeg", "gif", "webp"}
STICKER_SIZE = 240

def now():
    return datetime.now().strftime("%H:%M")

def save_state():
    with open(os.path.join(DATA_DIR, "state.json"), "w", encoding="utf-8") as f:
        json.dump(ROOMS, f, ensure_ascii=False)

def load_state():
    global ROOMS
    p = os.path.join(DATA_DIR, "state.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                ROOMS = json.load(f)
        except Exception:
            ROOMS = {}

load_state()

@app.route("/")
def index():
    return send_from_directory(".", "chat.html")

@app.route("/uploads/<name>")
def uploaded(name):
    return send_from_directory(UP_DIR, name)

@app.route("/stickers/<name>")
def sticker_file(name):
    return send_from_directory(ST_DIR, name)

@app.route("/avatar", methods=["POST"])
def upload_avatar():
    f = request.files.get("file")
    uid = request.form.get("uid", "").strip()
    if not f or not f.filename or not uid:
        return jsonify({"ok": False}), 400
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMG:
        return jsonify({"ok": False, "error": "bad_ext"}), 400
    fn = "av_" + uuid.uuid4().hex + "." + ext
    f.save(os.path.join(UP_DIR, fn))
    url = "/uploads/" + fn
    for r in ROOMS.values():
        if uid in r["members"]:
            r["members"][uid]["avatar"] = url
    save_state()
    return jsonify({"ok": True, "url": url})

@app.route("/upload", methods=["POST"])
def upload_chat():
    f = request.files.get("file")
    uid = request.form.get("uid", "").strip()
    rid = request.form.get("room", "").strip()
    if not f or not f.filename or rid not in ROOMS:
        return jsonify({"ok": False}), 400
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMG:
        return jsonify({"ok": False, "error": "bad_ext"}), 400
    fn = uuid.uuid4().hex + "." + ext
    path = os.path.join(UP_DIR, fn)
    f.save(path)
    if os.path.getsize(path) > 8 * 1024 * 1024:
        os.remove(path)
        return jsonify({"ok": False, "error": "too_big"}), 400
    u = ROOMS[rid]["members"].get(uid, {"name": "Anon", "avatar": ""})
    msg = {"type": "image", "uid": uid, "name": u["name"], "avatar": u.get("avatar", ""), "url": "/uploads/" + fn, "time": now()}
    ROOMS[rid]["messages"].append(msg)
    ROOMS[rid]["messages"] = ROOMS[rid]["messages"][-300:]
    save_state()
    socketio.emit("message", {"room": rid, "msg": msg}, to=rid)
    return jsonify({"ok": True})

@app.route("/sticker", methods=["POST"])
def create_sticker():
    if not PIL_OK:
        return jsonify({"ok": False, "error": "pillow_not_installed"}), 500
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False}), 400
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMG:
        return jsonify({"ok": False, "error": "bad_ext"}), 400
    tmp = os.path.join(UP_DIR, "_tmp_" + uuid.uuid4().hex)
    f.save(tmp)
    try:
        img = Image.open(tmp).convert("RGBA")
        w, h = img.size
        s = min(w, h)
        img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        img = img.resize((STICKER_SIZE, STICKER_SIZE), Image.LANCZOS)
        fn = "st_" + uuid.uuid4().hex + ".png"
        img.save(os.path.join(ST_DIR, fn), "PNG")
    except Exception as e:
        os.remove(tmp)
        return jsonify({"ok": False, "error": str(e)}), 400
    os.remove(tmp)
    return jsonify({"ok": True, "url": "/stickers/" + fn})

@socketio.on("connect_user")
def on_connect(data):
    uid = data.get("uid")
    name = (data.get("name") or "Anon").strip()[:24]
    avatar = data.get("avatar", "")
    USERS[request.sid] = {"uid": uid, "name": name, "avatar": avatar, "room": None}
    emit("rooms_list", room_list(uid))

@socketio.on("create_room")
def on_create(data):
    uid = data["uid"]
    name = (data.get("name") or "Чат").strip()[:40]
    rid = uuid.uuid4().hex[:10]
    ROOMS[rid] = {
        "name": name,
        "owner": uid,
        "members": {uid: {"name": USERS[request.sid]["name"], "avatar": USERS[request.sid]["avatar"], "role": "admin"}},
        "messages": []
    }
    save_state()
    emit("rooms_list", room_list(uid))
    socketio.emit("rooms_list_push")

@socketio.on("join_room_chat")
def on_join_room(data):
    uid = data["uid"]; rid = data["room"]
    if rid not in ROOMS:
        return
    prev = USERS[request.sid].get("room")
    if prev:
        leave_room(prev)
    join_room(rid)
    USERS[request.sid]["room"] = rid
    emit("history", ROOMS[rid]["messages"][-80:])
    emit("room_info", room_info(rid, uid))

@socketio.on("add_member")
def on_add(data):
    uid = data["uid"]; rid = data["room"]; target = (data.get("name") or "").strip()[:24]
    if rid not in ROOMS:
        return
    me = ROOMS[rid]["members"].get(uid)
    if not me or me["role"] != "admin":
        return
    for sid, u in USERS.items():
        if u["name"] == target:
            ROOMS[rid]["members"][u["uid"]] = {"name": u["name"], "avatar": u["avatar"], "role": "member"}
            save_state()
            emit("room_info", room_info(rid, uid))
            socketio.emit("rooms_list_push")
            socketio.emit("you_added", {"room": rid, "name": ROOMS[rid]["name"]}, to=sid)
            return

@socketio.on("kick")
def on_kick(data):
    uid = data["uid"]; rid = data["room"]; target = data["target"]
    if rid not in ROOMS:
        return
    me = ROOMS[rid]["members"].get(uid)
    if not me or me["role"] != "admin":
        return
    if target == ROOMS[rid]["owner"]:
        return
    ROOMS[rid]["members"].pop(target, None)
    save_state()
    emit("room_info", room_info(rid, uid))
    socketio.emit("rooms_list_push")

@socketio.on("make_admin")
def on_admin(data):
    uid = data["uid"]; rid = data["room"]; target = data["target"]
    if rid not in ROOMS:
        return
    me = ROOMS[rid]["members"].get(uid)
    if not me or me["role"] != "admin":
        return
    if target in ROOMS[rid]["members"]:
        ROOMS[rid]["members"][target]["role"] = "admin"
        save_state()
        emit("room_info", room_info(rid, uid))

@socketio.on("delete_room")
def on_delete(data):
    uid = data["uid"]; rid = data["room"]
    if rid not in ROOMS:
        return
    if ROOMS[rid]["owner"] != uid:
        return
    del ROOMS[rid]
    save_state()
    socketio.emit("rooms_list_push")

@socketio.on("message")
def on_message(data):
    uid = data["uid"]; rid = data["room"]
    text = (data.get("text") or "").strip()[:2000]
    sticker = data.get("sticker")
    if rid not in ROOMS:
        return
    u = ROOMS[rid]["members"].get(uid)
    if not u:
        return
    if sticker:
        msg = {"type": "sticker", "uid": uid, "name": u["name"], "avatar": u.get("avatar", ""), "url": sticker, "time": now()}
    else:
        if not text:
            return
        msg = {"type": "text", "uid": uid, "name": u["name"], "avatar": u.get("avatar", ""), "text": text, "time": now()}
    ROOMS[rid]["messages"].append(msg)
    ROOMS[rid]["messages"] = ROOMS[rid]["messages"][-300:]
    save_state()
    socketio.emit("message", {"room": rid, "msg": msg}, to=rid)
@socketio.on("delete_msg")
def on_delete_msg(data):
    uid = data["uid"]; rid = data["room"]; idx = data.get("index")
    if rid not in ROOMS: return
    me = ROOMS[rid]["members"].get(uid)
    if not me or me["role"] != "admin": return
    msgs = ROOMS[rid]["messages"]
    if idx is None or idx < 0 or idx >= len(msgs): return
    if msgs[idx].get("uid") != uid and ROOMS[rid]["owner"] != uid: return
    msgs.pop(idx)
    save_state()
    socketio.emit("msg_deleted", {"room": rid, "index": idx}, to=rid)
@socketio.on("disconnect")
def on_disconnect():
    USERS.pop(request.sid, None)

def room_list(uid):
    out = []
    for rid, r in ROOMS.items():
        if uid in r["members"]:
            out.append({"id": rid, "name": r["name"], "role": r["members"][uid]["role"], "count": len(r["members"])})
    return out

def room_info(rid, uid):
    r = ROOMS[rid]
    return {
        "id": rid, "name": r["name"], "owner": r["owner"],
        "me": r["members"].get(uid, {}),
        "members": [{"uid": k, **v} for k, v in r["members"].items()],
    }

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
