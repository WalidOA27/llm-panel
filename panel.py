"""LLM Panel: start/stop local models and services, switch profiles, and show GPU/system stats.

Runs on 127.0.0.1:9400. Services and profiles are declared in services.json
(or services.example.json when no local config exists).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
PORT = int(os.environ.get("LLM_PANEL_PORT", "9400"))
STATE = Path(os.environ.get("LLM_PANEL_STATE", "~/.local/state/llm-panel")).expanduser()
CONFIG_FILE = Path(os.environ.get("LLM_PANEL_CONFIG", str(HERE / "services.json"))).expanduser()
EXAMPLE_FILE = HERE / "services.example.json"
UPSTREAM_FILE = Path(os.environ.get("LLM_PANEL_UPSTREAM_FILE", "~/.config/llm-panel/upstream")).expanduser()
IGPU_ENV = Path(os.environ.get("LLM_PANEL_IGPU_ENV", "~/.config/environment.d/10-igpu.conf")).expanduser()
IGPU_PCI = os.environ.get("LLM_PANEL_IGPU_PCI", "")
CARD = Path(os.environ.get("LLM_PANEL_CARD", "/sys/class/drm/card0/device")).expanduser()
DGPU_PDEV = os.environ.get("LLM_PANEL_DGPU_PDEV", "")

STATE.mkdir(parents=True, exist_ok=True)
pending = {}  # service id -> "starting" / "stopping" while an action runs
events = []   # recent action results shown in the UI


def config():
    path = CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def service(sid):
    return next(s for s in config()["services"] if s["id"] == sid)


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def note(msg, ok=True):
    events.insert(0, {"t": time.strftime("%H:%M:%S"), "msg": msg, "ok": ok})
    del events[30:]


# ---- control ------------------------------------------------------------------------

def unit_cmd(ctl, verb):
    if ctl["type"] == "user-unit":
        return ["systemctl", "--user", verb, ctl["unit"]]
    return ["sudo", "-n", "/usr/bin/systemctl", verb, ctl["unit"]]


def unit_state(ctl):
    if ctl["type"] == "user-unit":
        return run(["systemctl", "--user", "is-active", ctl["unit"]], 10)[1]
    return run(["systemctl", "is-active", ctl["unit"]], 10)[1]


def do_action(sid, verb):
    s = service(sid)
    ctl = s["control"]
    pending[sid] = {"start": "starting", "stop": "stopping", "restart": "restarting"}.get(verb, verb)
    try:
        if verb == "free":
            req = urllib.request.Request(f"http://127.0.0.1:{s['port']}/free", b'{"unload_models":true,"free_memory":true}',
                                         {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            return note(f"{s['name']}: VRAM freed")
        if ctl["type"] in ("user-unit", "system-unit"):
            code, out = run(unit_cmd(ctl, verb))
        elif verb == "restart":
            run(ctl["stop"])
            time.sleep(2)
            code, out = run(ctl["start"], 300)
        else:
            code, out = run(ctl[verb], 300)
        if code == 0 and verb in ("start", "restart"):
            for _ in range(150):  # wait for the model to load so the UI shows "ready"
                if health(s):
                    break
                time.sleep(2)
        note(f"{s['name']}: {verb} {'OK' if code == 0 else 'failed'}" + (f" — {out.splitlines()[-1][:160]}" if code and out else ""), code == 0)
    except Exception as e:
        note(f"{s['name']}: {verb} failed — {e}", False)
    finally:
        pending.pop(sid, None)


def apply_profile(pid):
    p = next(x for x in config()["profiles"] if x["id"] == pid)
    note(f"Profile “{p['name']}”…")
    for sid in p.get("stop", []):
        if is_running(service(sid)):
            do_action(sid, "stop")
    for sid in p.get("free", []):
        if health(service(sid)):
            do_action(sid, "free")
    if "upstream" in p:
        UPSTREAM_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPSTREAM_FILE.write_text(p["upstream"] + "\n")
    for sid in p.get("start", []):
        if not health(service(sid)):
            do_action(sid, "start")
    note(f"Profile “{p['name']}” applied")


# ---- status -------------------------------------------------------------------------

def health(s):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s['port']}{s.get('health', '/health')}", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def port_pid(port):
    code, out = run(["ss", "-ltnpH", f"sport = :{port}"], 5)
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else None


def is_running(s):
    ctl = s["control"]
    if ctl["type"] in ("user-unit", "system-unit"):
        return unit_state(ctl) in ("active", "activating")
    return port_pid(s["port"]) is not None or health(s)


def proc_vram(pid):
    """VRAM held by the process, from the DRM fdinfo (deduplicated per DRM client)."""
    clients = {}
    try:
        for fd in os.listdir(f"/proc/{pid}/fdinfo"):
            try:
                txt = open(f"/proc/{pid}/fdinfo/{fd}").read()
            except OSError:
                continue
            if DGPU_PDEV and DGPU_PDEV not in txt:
                continue
            cid = re.search(r"drm-client-id:\s*(\d+)", txt)
            # total minus shared: "resident" also counts purgeable buffers the driver can drop at any time
            vram = re.search(r"drm-(?:total|memory)-vram:\s*(\d+)\s*KiB", txt)
            shared = re.search(r"drm-shared-vram:\s*(\d+)\s*KiB", txt)
            if cid and vram:
                clients[cid.group(1)] = (int(vram.group(1)) - (int(shared.group(1)) if shared else 0)) * 1024
    except (FileNotFoundError, PermissionError):
        return None
    return sum(clients.values()) if clients else None


def proc_rss(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None


def llama_metrics(s):
    """Model alias and speed from llama-server (/v1/models, /metrics when --metrics is on)."""
    info = {}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s['port']}/v1/models", timeout=1.5) as r:
            info["model"] = json.load(r)["data"][0]["id"]
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s['port']}/metrics", timeout=1.5) as r:
            txt = r.read().decode()
        for key, name in (("predicted_tokens_seconds", "tg"), ("prompt_tokens_seconds", "pp"),
                          ("tokens_predicted_total", "tokens_total"), ("requests_processing", "busy")):
            m = re.search(rf"^llamacpp:{key} ([\d.e+-]+)", txt, re.M)
            if m:
                info[name] = float(m.group(1))
    except Exception:
        pass
    return info


def read_num(path, scale=1):
    try:
        return int(Path(path).read_text().strip()) / scale
    except (OSError, ValueError):
        return None


def system_stats():
    hw = next(CARD.glob("hwmon/hwmon*"), None)
    mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    du = shutil.disk_usage("/")
    return {
        "gpu": {"vram_used": read_num(CARD / "mem_info_vram_used"), "vram_total": read_num(CARD / "mem_info_vram_total"),
                "gtt_used": read_num(CARD / "mem_info_gtt_used"), "gtt_total": read_num(CARD / "mem_info_gtt_total"),
                "busy": read_num(CARD / "gpu_busy_percent"),
                "temp": read_num(hw / "temp1_input", 1000) if hw else None,
                "power": read_num(hw / "power1_average", 1e6) if hw else None},
        "ram": {"total": mem["MemTotal"], "available": mem["MemAvailable"],
                "swap_used": mem["SwapTotal"] - mem["SwapFree"], "swap_total": mem["SwapTotal"]},
        "load": os.getloadavg(),
        "disk": {"free": du.free, "total": du.total},
        "desktop_gpu": "igpu" if IGPU_ENV.exists() else "dgpu",
    }


def status():
    out = []
    for s in config()["services"]:
        up = health(s)
        pid = port_pid(s["port"])
        st = pending.get(s["id"]) or ("ready" if up else ("loading" if is_running(s) else "stopped"))
        item = {k: s[k] for k in ("id", "name", "desc", "group", "port")}
        item.update(state=st, pid=pid, extra=s.get("extra", []),
                    vram=proc_vram(pid) if pid else None, rss=proc_rss(pid) if pid else None)
        if up and s["group"] == "llm":
            item.update(llama_metrics(s))
        out.append(item)
    cfg = config()
    ui = dict(cfg.get("ui", {}))
    ui["current_upstream"] = UPSTREAM_FILE.read_text().strip() if UPSTREAM_FILE.exists() else None
    return {"services": out, "others": other_gpu_procs({s["pid"] for s in out}), "profiles": cfg["profiles"],
            "system": system_stats(), "events": events, "ui": ui}


def other_gpu_procs(known):
    """Processes outside services.json holding >256 MB of VRAM (tests, games, browsers...)."""
    found = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in known:
            continue
        v = proc_vram(int(d))
        if v and v > 256 * 2**20:
            try:
                cmd = Path(f"/proc/{d}/cmdline").read_bytes().split(b"\0")
                name = Path(cmd[0].decode()).name
                port = re.search(r"--port\s+(\d+)", " ".join(c.decode(errors="replace") for c in cmd))
            except OSError:
                continue
            found.append({"pid": int(d), "name": name + (f" :{port.group(1)}" if port else ""), "vram": v})
    return found


def read_log(s, n=120):
    lg = s["log"]
    if lg["type"] == "file":
        p = Path(os.path.expanduser(lg["path"]))
        if not p.exists():
            return "(no log yet)"
        return "\n".join(p.read_text(errors="replace").splitlines()[-n:])
    cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(n), "-u", lg["unit"]]
    if lg["type"] == "journal-user":
        cmd.insert(1, "--user")
    return run(cmd, 10)[1] or "(no entries)"


def connected_outputs():
    drm = CARD.parent.parent
    prefix = CARD.parent.name + "-"
    return [f.name for f in drm.glob(prefix + "*")
            if (f / "status").exists() and (f / "status").read_text().strip() == "connected"]


def set_desktop_gpu(mode):
    """Only applies after logout/login. Requires LLM_PANEL_IGPU_PCI to be set."""
    if mode == "igpu":
        if not IGPU_PCI:
            return note("LLM_PANEL_IGPU_PCI is not set; cannot configure the iGPU", False)
        connected = connected_outputs()
        if connected:
            return note(f"The discrete GPU has monitors connected ({', '.join(connected)}); disconnect them first", False)
        IGPU_ENV.parent.mkdir(parents=True, exist_ok=True)
        IGPU_ENV.write_text(f"DRI_PRIME={IGPU_PCI}\nMESA_VK_DEVICE_SELECT={IGPU_PCI}\n")
        note("Desktop set to iGPU: log out and back in")
    else:
        IGPU_ENV.unlink(missing_ok=True)
        note("Desktop set to dGPU: connect the monitors to it and log out")


# ---- http ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            return self._send(200, status())
        if url.path == "/api/log":
            return self._send(200, {"log": read_log(service(parse_qs(url.query)["id"][0]))})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/action":
            sid, verb = body["id"], body["action"]
            if verb not in ("start", "stop", "restart", "free") or sid in pending:
                return self._send(400, {"error": "invalid or in-progress action"})
            threading.Thread(target=do_action, args=(sid, verb), daemon=True).start()
            return self._send(202, {"ok": True})
        if self.path == "/api/profile":
            threading.Thread(target=apply_profile, args=(body["id"],), daemon=True).start()
            return self._send(202, {"ok": True})
        if self.path == "/api/upstream":
            UPSTREAM_FILE.parent.mkdir(parents=True, exist_ok=True)
            UPSTREAM_FILE.write_text(body["url"] + "\n")
            note(f"Upstream set to {body['url']}")
            return self._send(200, {"ok": True})
        if self.path == "/api/desktop-gpu":
            if not config().get("ui", {}).get("desktop_gpu"):
                return self._send(403, {"error": "desktop GPU switching is not enabled"})
            set_desktop_gpu(body["mode"])
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"LLM Panel on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
