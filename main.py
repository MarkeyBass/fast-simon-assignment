"""Simple key-value DB on App Engine Standard (Python 3) backed by Cloud Datastore.

Commands are driven by HTTP GET requests, handled one at a time (concurrency ignored
per the assignment). State persists in Datastore between requests because each GAE
request is a fresh, stateless task.

Datastore layout (all access is key-based -> O(1), no scans except /end):
  Kind "Var"     key=name           prop value(str)        -> the variables
  Kind "Count"   key=value          prop count(int)        -> index: how many vars == value
  Kind "Journal" key="journal"      props undo, redo(json) -> undo/redo stacks (singleton)

A journal entry is [name, prev_value] where prev_value is null ("did not exist") or a
string. UNDO restores prev_value and pushes the pre-undo value onto redo; any new
SET/UNSET clears redo.

Improvement implemented: the Count index + Var write happen inside a single Datastore
transaction so the index can never drift out of sync with the variables (e.g. if a write
half-fails). See _apply_change(). Tradeoff: one transaction adds a little latency vs.
naked puts, in exchange for a count that is always exactly correct.
"""

import json

from flask import Flask, request
from google.cloud import datastore

app = Flask(__name__)
client = datastore.Client()

VAR_KIND = "Var"
COUNT_KIND = "Count"
JOURNAL_KIND = "Journal"
JOURNAL_ID = "journal"


# --------------------------------------------------------------------------- #
# Datastore helpers
# --------------------------------------------------------------------------- #
def _var_key(name):
    return client.key(VAR_KIND, name)


def _count_key(value):
    return client.key(COUNT_KIND, value)


def _get_value(name):
    """Current value of `name`, or None if not set."""
    entity = client.get(_var_key(name))
    return entity["value"] if entity else None


def _bump_count(value, delta):
    """Adjust the count index for `value` by delta (must run inside a transaction)."""
    if value is None:
        return
    key = _count_key(value)
    entity = client.get(key)
    new_count = (entity["count"] if entity else 0) + delta
    if new_count <= 0:
        if entity is not None:
            client.delete(key)
        return
    if entity is None:
        entity = datastore.Entity(key=key)
    entity["count"] = new_count
    client.put(entity)


def _apply_change(name, new_value):
    """Set name->new_value (new_value=None means unset), keeping the count index
    consistent. The whole change is atomic. Returns the previous value (or None)."""
    with client.transaction():
        old_value = _get_value(name)
        if old_value == new_value:
            return old_value  # no-op, nothing to record
        # update the index for the old and new values
        _bump_count(old_value, -1)
        _bump_count(new_value, +1)
        # update the variable itself
        if new_value is None:
            client.delete(_var_key(name))
        else:
            entity = datastore.Entity(key=_var_key(name))
            entity["value"] = new_value
            client.put(entity)
        return old_value


# --------------------------------------------------------------------------- #
# Journal (undo/redo stacks) — persisted as JSON in a singleton entity
# --------------------------------------------------------------------------- #
def _load_journal():
    entity = client.get(client.key(JOURNAL_KIND, JOURNAL_ID))
    if entity is None:
        return [], []
    return json.loads(entity.get("undo", "[]")), json.loads(entity.get("redo", "[]"))


def _save_journal(undo, redo):
    key = client.key(JOURNAL_KIND, JOURNAL_ID)
    entity = datastore.Entity(key=key, exclude_from_indexes=("undo", "redo"))
    entity["undo"] = json.dumps(undo)
    entity["redo"] = json.dumps(redo)
    client.put(entity)


def _record_user_change(name, prev_value):
    """Push a user SET/UNSET onto the undo stack and clear redo."""
    undo, _redo = _load_journal()
    undo.append([name, prev_value])
    _save_journal(undo, [])


def _fmt(name, value):
    return "%s = %s" % (name, "None" if value is None else value)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return "key-value DB up. commands: /set /get /unset /numequalto /undo /redo /end"


@app.route("/set")
def set_var():
    name = request.args.get("name")
    value = request.args.get("value")
    if not name or value is None or value == "":
        return "usage: /set?name=X&value=V", 400
    prev = _apply_change(name, value)
    _record_user_change(name, prev)
    return _fmt(name, value)


@app.route("/get")
def get_var():
    name = request.args.get("name")
    if not name:
        return "usage: /get?name=X", 400
    value = _get_value(name)
    return "None" if value is None else value


@app.route("/unset")
def unset_var():
    name = request.args.get("name")
    if not name:
        return "usage: /unset?name=X", 400
    prev = _apply_change(name, None)
    _record_user_change(name, prev)
    return _fmt(name, None)


@app.route("/numequalto")
def numequalto():
    value = request.args.get("value")
    if value is None or value == "":
        return "usage: /numequalto?value=V", 400
    entity = client.get(_count_key(value))
    return str(entity["count"] if entity else 0)


@app.route("/undo")
def undo():
    undo_stack, redo_stack = _load_journal()
    if not undo_stack:
        return "NO COMMANDS"
    name, prev_value = undo_stack.pop()
    current = _apply_change(name, prev_value)   # revert to prev_value
    redo_stack.append([name, current])          # so redo can re-apply
    _save_journal(undo_stack, redo_stack)
    return _fmt(name, prev_value)


@app.route("/redo")
def redo():
    undo_stack, redo_stack = _load_journal()
    if not redo_stack:
        return "NO COMMANDS"
    name, redo_value = redo_stack.pop()
    current = _apply_change(name, redo_value)    # re-apply the undone change
    undo_stack.append([name, current])
    _save_journal(undo_stack, redo_stack)
    return _fmt(name, redo_value)


@app.route("/end")
def end():
    for kind in (VAR_KIND, COUNT_KIND, JOURNAL_KIND):
        keys = [e.key for e in client.query(kind=kind).fetch()]
        if keys:
            client.delete_multi(keys)
    return "CLEANED"


@app.errorhandler(Exception)
def handle_unexpected(exc):
    # Backstop: never leak a traceback / 500 stack to the client.
    return "error: %s" % exc, 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
