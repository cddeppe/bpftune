#!/usr/bin/env python3
"""IP label/group editor API.  Serves on port 8081.
nginx proxies /api/labels here.
GET  /api/labels → full labels + aliases
POST /api/labels {"ip":"82.43.0.0","label":"home"} → add/update one
POST /api/labels {"ips":["1.2.0.0","3.4.0.0"],"label":"group1"} → group IPs
POST /api/labels {"ip":"82.43.0.0","label":""} → remove label
"""
import json, os, sys, tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

LABELS_FILE = "/var/lib/bpftune/aliases.labels.json"
ALIASES_FILE = "/var/lib/bpftune/aliases"
PORT = 8081
CORS = {"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,POST,DELETE,OPTIONS","Access-Control-Allow-Headers":"Content-Type"}

def load_labels():
    try:
        with open(LABELS_FILE) as f: d=json.load(f)
        return d if isinstance(d,dict) else {}
    except: return {}

def save_labels(labels):
    d=os.path.dirname(LABELS_FILE)
    fd,tmp=tempfile.mkstemp(dir=d,suffix=".tmp")
    with os.fdopen(fd,"w") as f: json.dump(labels,f,indent=2,sort_keys=True); f.write("\n")
    os.chmod(tmp,0o644); os.rename(tmp,LABELS_FILE)

def load_aliases():
    try:
        with open(ALIASES_FILE) as f: return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except: return []

def save_aliases(lines):
    d=os.path.dirname(ALIASES_FILE) or "."
    fd,tmp=tempfile.mkstemp(dir=d,suffix=".tmp")
    with os.fdopen(fd,"w") as f:
        for l in lines: f.write(l+"\n")
    os.chmod(tmp,0o644); os.rename(tmp,ALIASES_FILE)

class H(BaseHTTPRequestHandler):
    def _json(self,code,data):
        b=json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        for k,v in CORS.items(): self.send_header(k,v)
        self.send_header("Content-Length",str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_OPTIONS(self): self._json(200,{"ok":True})
    def do_GET(self):
        qs=parse_qs(urlparse(self.path).query)
        labels=load_labels()
        if "ip" in qs:
            ip=qs["ip"][0]; self._json(200,{"ip":ip,"label":labels.get(ip,"")})
        else:
            self._json(200,{"labels":labels,"aliases":load_aliases()})
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length",0))
            req=json.loads(self.rfile.read(length).decode())
        except: self._json(400,{"error":"invalid JSON"}); return
        labels=load_labels()
        # Group mode
        if "ips" in req and isinstance(req["ips"],list):
            label=req.get("label","").strip(); ips=req["ips"]
            if not label:
                for ip in ips: labels.pop(ip,None)
            else:
                for ip in ips: labels[ip]=label
            save_labels(labels)
            aliases=load_aliases()
            aliases=[a for a in aliases if not any(ip in a for ip in ips)]
            if label: aliases.append(",".join(ips)+":"+label)
            save_aliases(aliases)
            self._json(200,{"ok":True,"labels":labels,"aliases":aliases}); return
        # Single IP mode
        ip=req.get("ip","").strip(); label=req.get("label","").strip()
        if not ip: self._json(400,{"error":"ip required"}); return
        if not label: labels.pop(ip,None)
        else: labels[ip]=label
        save_labels(labels)
        aliases=load_aliases()
        aliases=[a for a in aliases if not a.startswith(ip+":") and not a.startswith(ip+",")]
        if label: aliases.append(f"{ip}:{label}")
        save_aliases(aliases)
        self._json(200,{"ok":True,"ip":ip,"label":label,"labels":labels})
    def do_DELETE(self):
        qs=parse_qs(urlparse(self.path).query)
        ip=qs.get("ip",[""])[0]
        if not ip: self._json(400,{"error":"ip required"}); return
        labels=load_labels(); labels.pop(ip,None); save_labels(labels)
        aliases=load_aliases()
        aliases=[a for a in aliases if not a.startswith(ip+":") and not a.startswith(ip+",")]
        save_aliases(aliases)
        self._json(200,{"ok":True,"ip":ip})
    def log_message(self,fmt,*args):
        sys.stderr.write(f"[labels-api] {fmt%args}\n")

if __name__=="__main__":
    print(f"[labels-api] port {PORT}",file=sys.stderr)
    HTTPServer(("127.0.0.1",PORT),H).serve_forever()
