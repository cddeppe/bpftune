#!/usr/bin/env python3
"""IP label/group editor API.  Serves on port 8081.
Reads/writes /etc/bpftune/aliases (the MAIN file the BPF reads).
Format: FROM_IP = TO_IP LABEL
"""
import json, os, sys, tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ALIASES_FILE = "/etc/bpftune/aliases"
LABELS_FILE = "/var/lib/bpftune/aliases.labels.json"
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

def parse_aliases():
    """Parse /etc/bpftune/aliases. Returns {label: {to_ip, from_ips:[]}}."""
    groups = {}
    try:
        with open(ALIASES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                parts = line.split()
                if '=' not in parts: continue
                eq = parts.index('=')
                if eq < 1 or eq+1 >= len(parts): continue
                from_ip = parts[0]
                to_ip = parts[eq+1]
                label = ' '.join(parts[eq+2:]) if eq+2 < len(parts) else to_ip
                if label not in groups:
                    groups[label] = {"to_ip": to_ip, "from_ips": []}
                groups[label]["from_ips"].append(from_ip)
    except OSError: pass
    return groups

def add_alias_line(from_ip, to_ip, label):
    """Append a line to /etc/bpftune/aliases."""
    with open(ALIASES_FILE, 'a') as f:
        f.write(f"\n{from_ip} = {to_ip} {label}\n")

def remove_alias_line(from_ip):
    """Remove lines starting with from_ip from /etc/bpftune/aliases."""
    with open(ALIASES_FILE) as f: lines = f.readlines()
    with open(ALIASES_FILE, 'w') as f:
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                parts = stripped.split()
                if parts and parts[0] == from_ip:
                    continue  # skip this line
            f.write(line)

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
        groups=parse_aliases()
        if "ip" in qs:
            ip=qs["ip"][0]; self._json(200,{"ip":ip,"label":labels.get(ip,"")})
        else:
            self._json(200,{"labels":labels,"groups":groups})
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length",0))
            req=json.loads(self.rfile.read(length).decode())
        except: self._json(400,{"error":"invalid JSON"}); return
        labels=load_labels()
        groups=parse_aliases()
        ip=req.get("ip","").strip()
        label=req.get("label","").strip()
        if not ip: self._json(400,{"error":"ip required"}); return
        if not label:
            # Delete: remove from labels + aliases
            labels.pop(ip,None)
            for mb in (16,24,32):
                try:
                    import ipaddress
                    mask=(0xFFFFFFFF<<(32-mb))&0xFFFFFFFF
                    masked=str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(ip))&mask))
                    labels.pop(masked,None)
                except: pass
            remove_alias_line(ip)
        else:
            # Add/update: store in labels + aliases
            labels[ip]=label
            # Find the to_ip for this label (from existing groups)
            to_ip = ip
            if label in groups:
                to_ip = groups[label]["to_ip"]
            else:
                # New group: use the IP as the to_ip (masked to /16)
                try:
                    import ipaddress
                    mask=(0xFFFFFFFF<<16)&0xFFFFFFFF
                    to_ip=str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(ip))&mask))
                except: pass
            add_alias_line(ip, to_ip, label)
        save_labels(labels)
        groups=parse_aliases()
        self._json(200,{"ok":True,"ip":ip,"label":label,"labels":labels,"groups":groups})
    def do_DELETE(self):
        qs=parse_qs(urlparse(self.path).query)
        ip=qs.get("ip",[""])[0]
        if not ip: self._json(400,{"error":"ip required"}); return
        labels=load_labels(); labels.pop(ip,None)
        save_labels(labels)
        remove_alias_line(ip)
        groups=parse_aliases()
        self._json(200,{"ok":True,"ip":ip,"groups":groups})
    def log_message(self,fmt,*args):
        sys.stderr.write(f"[labels-api] {fmt%args}\n")

if __name__=="__main__":
    print(f"[labels-api] port {PORT}",file=sys.stderr)
    HTTPServer(("127.0.0.1",PORT),H).serve_forever()
