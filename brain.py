import json, time, shutil, logging, re, hashlib
import requests

from config import (
    BRAIN_FILE, BACKUP_DIR, FLUSH_INTERVAL,
    CONF_DECAY, CONF_FLOOR, CONF_BOOST, CONF_DEFAULT,
    LLM_KEY, LLM_URL, MODEL,
)

log = logging.getLogger("brain")

_brain:      dict  = {}
_dirty:      bool  = False
_last_flush: float = 0.0


def _default() -> dict:
    return {
        "version": 3,
        "nodes": {},
        "reflection_log": [],
        "meta": {
            "interact_count": 0,
            "decay_cycle": 0,
            "consolidation_count": 0,
            "created": time.time(),
            "last_commit": 0.0,
            "recent_interactions": [],
            "recall_ids_this_turn": [],
        },
    }


def _nodes() -> dict:
    return _brain.setdefault("nodes", {})


def meta() -> dict:
    return _brain.setdefault("meta", _default()["meta"])


def reflection_log() -> list:
    return _brain.setdefault("reflection_log", [])


def _key_hash(key: str) -> str:
    return hashlib.md5(key.lower().strip().encode()).hexdigest()[:10]


def load() -> None:
    global _brain, _dirty
    if BRAIN_FILE.exists():
        try:
            data = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
            if data.get("version") == 2:
                data["version"] = 3
                data.setdefault("reflection_log", [])
                data["meta"].setdefault("consolidation_count", 0)
                data["meta"].setdefault("last_commit", 0.0)
                data["meta"].setdefault("recent_interactions", [])
                data["meta"].setdefault("recall_ids_this_turn", [])
                for n in data.get("nodes", {}).values():
                    n.setdefault("ctx", "")
                    n.setdefault("source", "user")
            if data.get("version") == 3:
                _brain = data
                log.info("brain loaded: %d nodes", len(_brain["nodes"]))
                return
        except Exception as e:
            log.warning("brain load failed: %s", e)
    _brain = _default()
    _dirty = True
    flush(force=True)


def flush(force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty:
        return
    now = time.time()
    if not force and (now - _last_flush < FLUSH_INTERVAL):
        return
    try:
        BRAIN_FILE.write_text(
            json.dumps(_brain, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _dirty      = False
        _last_flush = now
    except Exception as e:
        log.error("brain flush failed: %s", e)


def backup() -> str:
    if not BRAIN_FILE.exists():
        return "no brain file"
    dest = BACKUP_DIR / f"brain_{int(time.time())}.json"
    shutil.copy2(BRAIN_FILE, dest)
    for old in sorted(BACKUP_DIR.glob("brain_*.json"))[:-10]:
        old.unlink(missing_ok=True)
    return f"backed up → {dest.name}"


def commit() -> str:
    from tools import _shell_raw
    flush(force=True)
    try:
        result = _shell_raw(
            'git add brain.json && git commit -m "brain: auto-checkpoint [skip ci]" && git push'
        )
        if "ERROR" in result or result.startswith("fatal"):
            log.warning("git issue: %s", result[:150])
            return f"git failed: {result[:100]}"
        meta()["last_commit"] = time.time()
        return result
    except Exception as e:
        log.error("commit error: %s", e)
        return f"git error: {e}"


def _adjudicate(key: str, old_val: str, new_val: str,
                old_conf: float, new_conf: float) -> dict:
    prompt = (
        f"Conflicting beliefs about '{key}':\n"
        f"A (conf={old_conf:.2f}): {old_val}\n"
        f"B (conf={new_conf:.2f}): {new_val}\n"
        "Reply ONLY: WINNER=A or WINNER=B"
    )
    try:
        r = requests.post(
            LLM_URL,
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.0, "max_tokens": 10},
            timeout=12,
        )
        if r.status_code == 200:
            reply = r.json()["choices"][0]["message"]["content"].strip()
            if "WINNER=B" in reply:
                return {"value": new_val, "conf": min(1.0, new_conf + 0.10)}
            return {"value": old_val, "conf": min(1.0, old_conf + 0.10)}
    except Exception as e:
        log.warning("adjudicate error: %s", e)
    return (
        {"value": new_val, "conf": new_conf}
        if new_conf >= old_conf
        else {"value": old_val, "conf": old_conf}
    )


def learn(key: str, value: str, conf: float = CONF_DEFAULT,
          ctx: str = "", source: str = "user") -> str:
    global _dirty
    key   = key.strip()[:150]
    value = value.strip()[:500]
    conf  = max(0.0, min(1.0, conf))
    khash = _key_hash(key)
    nodes = _nodes()

    if khash in nodes:
        node = nodes[khash]
        if node["value"].lower() == value.lower():
            old = node["conf"]
            node["conf"]   = round(min(1.0, node["conf"] + 0.08), 3)
            node["ts"]     = time.time()
            node["source"] = source
            _dirty = True
            return f"reinforced '{key}': conf {old:.2f}→{node['conf']:.2f}"
        else:
            winner = _adjudicate(key, node["value"], value, node["conf"], conf)
            action = "relearned" if winner["value"] == value else "retained"
            node["value"]  = winner["value"]
            node["conf"]   = round(winner["conf"], 3)
            node["ts"]     = time.time()
            node["source"] = source
            _dirty = True
            return f"{action} '{key}' → '{winner['value'][:60]}' (conf={node['conf']:.2f})"
    else:
        nodes[khash] = {
            "key":    key,
            "value":  value,
            "conf":   round(conf, 3),
            "ts":     time.time(),
            "hits":   0,
            "cycle":  meta().get("decay_cycle", 0),
            "ctx":    ctx,
            "source": source,
        }
        _dirty = True
        return f"learned '{key}' = '{value[:60]}' (conf={conf:.2f})"


def recall(query: str, top_n: int = 5, ctx_filter: str = "") -> list[dict]:
    global _dirty
    nodes  = _nodes()
    q_tok  = set(re.findall(r"\w+", query.lower()))
    scored = []

    for khash, node in nodes.items():
        if ctx_filter and node.get("ctx") and node["ctx"] != ctx_filter:
            continue
        k_tok   = set(re.findall(r"\w+", node["key"].lower()))
        v_tok   = set(re.findall(r"\w+", node["value"].lower()))
        overlap = len(q_tok & (k_tok | v_tok))
        if node["key"].lower() in query.lower():
            overlap += 6
        score = overlap * node["conf"]
        if score > 0:
            scored.append((score, khash, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    hits = scored[:top_n]
    if not hits:
        return []

    now_cycle = meta().get("decay_cycle", 0)
    seen      = set(meta().get("recall_ids_this_turn", []))
    boosted   = False
    for _, khash, node in hits:
        if khash not in seen:
            node["conf"]  = round(min(1.0, node["conf"] + CONF_BOOST), 3)
            node["hits"]  = node.get("hits", 0) + 1
            node["cycle"] = now_cycle
            seen.add(khash)
            boosted = True
    if boosted:
        meta()["recall_ids_this_turn"] = list(seen)
        _dirty = True

    return [node for _, _, node in hits]


def forget(key: str) -> str:
    global _dirty
    khash = _key_hash(key.strip())
    nodes = _nodes()
    if khash in nodes:
        del nodes[khash]
        _dirty = True
        return f"forgotten '{key}'"
    return f"not found '{key}'"


def decay_pass(full: bool = False) -> str:
    global _dirty
    m     = meta()
    nodes = _nodes()
    cycle = m.get("decay_cycle", 0)
    m["decay_cycle"] = cycle + 1

    amount   = CONF_DECAY if full else CONF_DECAY * 0.4
    decayed  = 0
    to_prune: list[str] = []

    for khash, node in nodes.items():
        if node.get("cycle", 0) < cycle:
            node["conf"] = round(node["conf"] - amount, 3)
            decayed += 1
        if full:
            node["hits"] = 0
        if node["conf"] < CONF_FLOOR:
            to_prune.append(khash)

    pruned = 0
    if full:
        for khash in to_prune:
            log.info("prune '%s'", nodes[khash]["key"])
            del nodes[khash]
        pruned = len(to_prune)

    _dirty = True
    label  = "full" if full else "light"
    return f"decay [{label}] cycle={cycle+1}: {decayed} decayed, {pruned} pruned, {len(nodes)} surviving"


def consolidate(do_commit: bool = True) -> str:
    m     = meta()
    since = time.time() - m.get("last_commit", 0.0)
    will_commit = do_commit and since > 300

    bk    = backup()
    nodes = _nodes()
    if len(nodes) < 5:
        return f"brain too sparse (< 5 nodes). {bk}"

    snapshot = "\n".join(
        f"{n['key']}: {n['value']} (conf={n['conf']:.2f})"
        for n in nodes.values()
    )
    prompt = (
        "You are a memory consolidation engine. Knowledge snapshot:\n"
        f"{snapshot}\n\n"
        "Find up to 3 pairs that express essentially the same fact and should merge. "
        "Reply ONLY as JSON array: "
        '[{"keep":"exact_key","drop":"exact_key","merged_value":"unified fact"},...] '
        "or []. No preamble."
    )
    count = 0
    try:
        r = requests.post(
            LLM_URL,
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "max_tokens": 400},
            timeout=30,
        )
        if r.status_code == 200:
            text  = r.json()["choices"][0]["message"]["content"].strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            merges = json.loads(match.group()) if match else []
            for mg in merges:
                k = mg.get("keep", "")
                d = mg.get("drop", "")
                v = mg.get("merged_value", "")
                if k and d and v:
                    learn(k, v, conf=0.90, source="agent")
                    forget(d)
                    count += 1
    except Exception as e:
        log.warning("consolidate error: %s", e)

    decay_r = decay_pass(full=True)
    m["consolidation_count"] = m.get("consolidation_count", 0) + 1
    git_r   = commit() if will_commit else "git skipped (rate-limit)"
    return (
        f"consolidation #{m['consolidation_count']}: {count} merges | "
        f"{decay_r} | {bk} | git: {git_r[:60]}"
    )


def reflect() -> str:
    global _dirty
    m      = meta()
    recent = m.get("recent_interactions", [])
    if len(recent) < 3:
        return "not enough interactions to reflect on"

    transcript = "\n".join(f"- {msg}" for msg in recent[-10:])
    prompt = (
        "You are OmniAgent's self-critic. Review these recent interactions:\n"
        f"{transcript}\n\n"
        "Identify: (1) missed facts to remember, (2) uncertain answers, "
        "(3) user patterns, (4) one concrete improvement.\n"
        'Reply ONLY as JSON: {"summary":"2-sentence critique",'
        '"memories_to_add":[{"key":"k","value":"v"}],'
        '"actions":["action1","action2"]}'
    )
    try:
        r = requests.post(
            LLM_URL,
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 400},
            timeout=30,
        )
        if r.status_code != 200:
            return f"reflection LLM failed HTTP {r.status_code}"
        text  = r.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return "reflection: no structured output"
        data  = json.loads(match.group())

        added = 0
        for mem in data.get("memories_to_add", []):
            if mem.get("key") and mem.get("value"):
                learn(mem["key"], mem["value"], conf=0.65, source="agent")
                added += 1

        entry = {
            "ts":      time.time(),
            "summary": data.get("summary", ""),
            "actions": data.get("actions", []),
        }
        rlog = reflection_log()
        rlog.append(entry)
        if len(rlog) > 10:
            _brain["reflection_log"] = rlog[-10:]

        _dirty = True
        flush()
        return (
            f"reflection: {data.get('summary', '')}\n"
            f"memories added: {added}\n"
            f"actions: {'; '.join(data.get('actions', []))}"
        )
    except Exception as e:
        log.warning("reflect error: %s", e)
        return f"reflection error: {e}"


def status() -> str:
    nodes = _nodes()
    m     = meta()
    if not nodes:
        return "🧠 brain: empty"
    avg_c = sum(n["conf"] for n in nodes.values()) / len(nodes)
    top3  = sorted(nodes.values(), key=lambda n: n["conf"], reverse=True)[:3]
    top_s = " | ".join(f"'{n['key']}'({n['conf']:.2f})" for n in top3)
    rlog  = reflection_log()
    return (
        f"🧠 {len(nodes)} nodes | avg_conf={avg_c:.2f} | "
        f"cycle={m.get('decay_cycle', 0)} | "
        f"consolidations={m.get('consolidation_count', 0)} | "
        f"reflections={len(rlog)} | top: {top_s}"
    )


def memory_context(user_msg: str, ctx_filter: str = "") -> str:
    hits = recall(user_msg, top_n=4, ctx_filter=ctx_filter)
    if not hits:
        return ""
    lines = [
        f"  • {n['key']}: {n['value']}  [conf={n['conf']:.2f}, src={n.get('source', '?')}]"
        for n in hits
    ]
    return (
        "\n\n━━ GROUNDED MEMORY ━━\n"
        "Verified facts from long-term memory. Reason over these. "
        "Never contradict without calling forget() first.\n"
        + "\n".join(lines)
        + "\n━━━━━━━━━━━━━━━━━━━━"
    )
