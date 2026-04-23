import json, time, shutil, logging, re, hashlib, requests
from config import (BRAIN_FILE, BACKUP_DIR, FLUSH_INTERVAL, CONF_DECAY, CONF_FLOOR, CONF_BOOST, CONF_DEFAULT, LLM_KEY, LLM_URL, MODEL)
log = logging.getLogger("brain")
_brain, _dirty, _last_flush = {}, False, 0.0
def _default(): return {"version": 3, "nodes": {}, "reflection_log": [], "meta": {"interact_count": 0, "decay_cycle": 0, "consolidation_count": 0, "created": time.time(), "last_commit": 0.0, "recent_interactions": [], "recall_ids_this_turn": []}}
def _nodes(): return _brain.setdefault("nodes", {})
def meta(): return _brain.setdefault("meta", _default()["meta"])
def reflection_log(): return _brain.setdefault("reflection_log", [])
def _key_hash(key): return hashlib.md5(key.lower().strip().encode()).hexdigest()[:10]
def load():
    global _brain, _dirty
    if BRAIN_FILE.exists():
        try:
            data = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
            if data.get("version") == 2:
                data["version"] = 3; data.setdefault("reflection_log", []); data["meta"].update({"consolidation_count": 0, "last_commit": 0.0, "recent_interactions": [], "recall_ids_this_turn": []})
                for n in data.get("nodes", {}).values(): n.update({"ctx": "", "source": "user"})
            if data.get("version") == 3: _brain = data; log.info("brain loaded: %d nodes", len(_brain["nodes"])); return
        except Exception as e: log.warning("brain load failed: %s", e)
    _brain, _dirty = _default(), True; flush(force=True)
def flush(force=False):
    global _dirty, _last_flush
    if not _dirty or (not force and (time.time() - _last_flush < FLUSH_INTERVAL)): return
    try: BRAIN_FILE.write_text(json.dumps(_brain, indent=2, ensure_ascii=False), encoding="utf-8"); _dirty, _last_flush = False, time.time()
    except Exception as e: log.error("brain flush failed: %s", e)
def backup():
    if not BRAIN_FILE.exists(): return "no brain file"
    dest = BACKUP_DIR / f"brain_{int(time.time())}.json"
    shutil.copy2(BRAIN_FILE, dest)
    for old in sorted(BACKUP_DIR.glob("brain_*.json"))[:-10]: old.unlink(missing_ok=True)
    return f"backed up → {dest.name}"
def commit():
    from omni_tools import _shell_raw
    flush(force=True)
    try:
        res = _shell_raw('git add brain.json && git commit -m "brain: auto-checkpoint [skip ci]" && git push')
        if "ERROR" in res or res.startswith("fatal"): return f"git failed: {res[:100]}"
        meta()["last_commit"] = time.time(); return res
    except Exception as e: return f"git error: {e}"
def _adjudicate(key, ov, nv, oc, nc):
    p = f"Conflicting beliefs about '{key}':\nA (conf={oc:.2f}): {ov}\nB (conf={nc:.2f}): {nv}\nReply ONLY: WINNER=A or WINNER=B"
    try:
        r = requests.post(LLM_URL, headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}, json={"model": MODEL, "messages": [{"role": "user", "content": p}], "temperature": 0.0, "max_tokens": 10}, timeout=12)
        if r.status_code == 200 and "WINNER=B" in r.json()["choices"][0]["message"]["content"]: return {"value": nv, "conf": min(1.0, nc + 0.10)}
    except: pass
    return {"value": nv, "conf": nc} if nc >= oc else {"value": ov, "conf": oc}
def learn(key, val, conf=CONF_DEFAULT, ctx="", source="user"):
    global _dirty
    key, val, conf, khash, nodes = key.strip()[:150], val.strip()[:500], max(0.0, min(1.0, conf)), _key_hash(key), _nodes()
    if khash in nodes:
        node = nodes[khash]
        if node["value"].lower() == val.lower():
            o, node["conf"], node["ts"], node["source"], _dirty = node["conf"], round(min(1.0, node["conf"] + 0.08), 3), time.time(), source, True
            return f"reinforced '{key}': conf {o:.2f}→{node['conf']:.2f}"
        w = _adjudicate(key, node["value"], val, node["conf"], conf)
        node["value"], node["conf"], node["ts"], node["source"], _dirty = w["value"], round(w["conf"], 3), time.time(), source, True
        return f"relearned '{key}' → '{w['value'][:60]}' (conf={node['conf']:.2f})"
    nodes[khash] = {"key": key, "value": val, "conf": round(conf, 3), "ts": time.time(), "hits": 0, "cycle": meta().get("decay_cycle", 0), "ctx": ctx, "source": source}; _dirty = True
    return f"learned '{key}' = '{val[:60]}' (conf={conf:.2f})"
def recall(query, top_n=5, ctx_filter=""):
    global _dirty
    nodes, q_tok, scored = _nodes(), set(re.findall(r"\w+", query.lower())), []
    for khash, node in nodes.items():
        if ctx_filter and node.get("ctx") and node["ctx"] != ctx_filter: continue
        score = len(q_tok & (set(re.findall(r"\w+", node["key"].lower())) | set(re.findall(r"\w+", node["value"].lower())))) * node["conf"]
        if node["key"].lower() in query.lower(): score += 6
        if score > 0: scored.append((score, khash, node))
    scored.sort(key=lambda x: x[0], reverse=True); hits = scored[:top_n]
    if not hits: return []
    cyc, seen, bst = meta().get("decay_cycle", 0), set(meta().get("recall_ids_this_turn", [])), False
    for _, khash, node in hits:
        if khash not in seen: node["conf"], node["hits"], node["cycle"], seen, bst = round(min(1.0, node["conf"] + CONF_BOOST), 3), node.get("hits", 0) + 1, cyc, seen | {khash}, True
    if bst: meta()["recall_ids_this_turn"], _dirty = list(seen), True
    return [node for _, _, node in hits]
def forget(key):
    global _dirty
    khash, nodes = _key_hash(key.strip()), _nodes()
    if khash in nodes: del nodes[khash]; _dirty = True; return f"forgotten '{key}'"
    return f"not found '{key}'"
def decay_pass(full=False):
    global _dirty
    m, nodes, cyc = meta(), _nodes(), meta().get("decay_cycle", 0); m["decay_cycle"] = cyc + 1
    amt, decayed, prune = (CONF_DECAY if full else CONF_DECAY * 0.4), 0, []
    for khash, node in nodes.items():
        if node.get("cycle", 0) < cyc: node["conf"], decayed = round(node["conf"] - amt, 3), decayed + 1
        if full: node["hits"] = 0
        if node["conf"] < CONF_FLOOR: prune.append(khash)
    if full:
        for k in prune: del nodes[k]
    _dirty = True; return f"decay [{'full' if full else 'light'}] cycle={cyc+1}: {decayed} decayed, {len(prune) if full else 0} pruned, {len(nodes)} surviving"
def consolidate(do_commit=True):
    m, nodes = meta(), _nodes(); bk = backup()
    if len(nodes) < 5: return f"sparse (< 5). {bk}"
    snap = "\n".join(f"{n['key']}: {n['value']} (conf={n['conf']:.2f})" for n in nodes.values())
    p = f"You are a memory consolidation engine. snapshot:\n{snap}\n\nFind up to 3 pairs that should merge. Reply ONLY as JSON array: [{{'keep':'k','drop':'d','merged_value':'v'}},...] or []."
    try:
        r = requests.post(LLM_URL, headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}, json={"model": MODEL, "messages": [{"role": "user", "content": p}], "temperature": 0.1, "max_tokens": 400}, timeout=30)
        if r.status_code == 200:
            mrg = json.loads(re.search(r"\[.*\]", r.json()["choices"][0]["message"]["content"], re.DOTALL).group())
            for mg in mrg: learn(mg["keep"], mg["merged_value"], conf=0.90, source="agent"); forget(mg["drop"])
    except: pass
    return f"consolidated | {decay_pass(full=True)} | {bk}"
def reflect():
    m, rec = meta(), meta().get("recent_interactions", [])
    if len(rec) < 3: return "not enough info"
    p = f"You are OmniAgent's critic. interactions:\n{rec[-10:]}\n\nReply ONLY as JSON: {{'summary':'critique','memories_to_add':[{{'key':'k','value':'v'}}],'actions':['a1']}}"
    try:
        r = requests.post(LLM_URL, headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}, json={"model": MODEL, "messages": [{"role": "user", "content": p}], "temperature": 0.3, "max_tokens": 400}, timeout=30)
        d = json.loads(re.search(r"\{.*\}", r.json()["choices"][0]["message"]["content"], re.DOTALL).group())
        for mem in d.get("memories_to_add", []): learn(mem["key"], mem["value"], conf=0.65, source="agent")
        reflection_log().append({"ts": time.time(), "summary": d.get("summary", ""), "actions": d.get("actions", [])})
        if len(reflection_log()) > 10: _brain["reflection_log"] = reflection_log()[-10:]
        flush(force=True); return f"reflected: {d.get('summary', '')}"
    except: return "reflection error"
def status():
    nodes, m = _nodes(), meta()
    if not nodes: return "🧠 empty"
    avg = sum(n["conf"] for n in nodes.values()) / len(nodes); top3 = sorted(nodes.values(), key=lambda n: n["conf"], reverse=True)[:3]
    top_s = " | ".join([f"{n['key']}({n['conf']:.2f})" for n in top3])
    return f"🧠 {len(nodes)} nodes | avg={avg:.2f} | cycle={m.get('decay_cycle', 0)} | top: {top_s}"
def memory_context(user_msg, ctx_filter=""):
    hits = recall(user_msg, top_n=4, ctx_filter=ctx_filter)
    if not hits: return ""
    return "\n\n━━ GROUNDED MEMORY ━━\n" + "\n".join(f"  • {n['key']}: {n['value']}  [conf={n['conf']:.2f}]" for n in hits) + "\n━━━━━━━━━━━━━━━━━━━━"
