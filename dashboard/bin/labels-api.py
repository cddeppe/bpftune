#!/usr/bin/env python3
"""
labels-api.py - IP label/group editor API for the bpftune dashboard.
Port 8081. nginx proxies /api/labels here.

AUTO-FOLD v3: only manage IPs in labels with >1 IP.

  IPs in labels.json that are the SOLE member of their label are treated
  as FOREIGN — their rules are preserved, never deleted.  Only IPs that
  are part of a label with >1 IP are "managed" (auto-folded).

  This prevents auto_fold from deleting manually-added fold rules for
  IPs that happen to be in labels.json but don't need folding (because
  their label only has 1 IP).
"""
import json
import os
import sys
import time
import shutil
import tempfile
import ipaddress
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import defaultdict

LABELS_FILE = "/var/lib/bpftune/aliases.labels.json"
ALIASES_FILE = "/etc/bpftune/aliases"
PORT = 8081

BPF_ALIASES_MAP_NAME = "dest_alias_map"
BPF_REMOTE_HOST_MAP_NAME = "remote_host_map"

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def load_labels():
    try:
        with open(LABELS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_labels(labels):
    d = os.path.dirname(LABELS_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(labels, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.rename(tmp, LABELS_FILE)
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def parse_aliases_rules(text):
    rules = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        lhs, rhs = s.split("=", 1)
        from_ip = lhs.strip()
        rest = rhs.strip().split()
        if not rest:
            continue
        to_ip = rest[0]
        label = rest[1] if len(rest) > 1 else ""
        rules.append({"from": from_ip, "to": to_ip, "label": label, "raw": s})
    return rules


def load_aliases_rules():
    try:
        with open(ALIASES_FILE) as f:
            return parse_aliases_rules(f.read())
    except OSError:
        return []


def _bpftool_map_id(name_substr):
    try:
        out = subprocess.check_output(["bpftool", "map", "show"], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if name_substr in line and ":" in line:
            try:
                return int(line.split(":")[0].strip())
            except ValueError:
                continue
    return None


def ip_to_bpf_hex(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except (ValueError, TypeError):
        return None
    if isinstance(ip, ipaddress.IPv4Address):
        return ("00" * 10) + "ffff" + "".join("%02x" % b for b in ip.packed)
    return "".join("%02x" % b for b in ip.packed)


def _hex_to_spaced(h):
    return " ".join(h[i:i+2] for i in range(0, len(h), 2))


def bpf_aliases_update(from_ip, to_ip):
    mid = _bpftool_map_id(BPF_ALIASES_MAP_NAME)
    if mid is None:
        return False, "no dest_alias_map"
    k = ip_to_bpf_hex(from_ip)
    v = ip_to_bpf_hex(to_ip)
    if not k or not v:
        return False, "bad ip"
    r = subprocess.run(
        ["bpftool", "map", "update", "id", str(mid),
         "key", "hex", _hex_to_spaced(k),
         "value", "hex", _hex_to_spaced(v)],
        capture_output=True, text=True,
    )
    return r.returncode == 0, r.stderr.strip() or r.stdout.strip()


def bpf_aliases_delete(from_ip):
    mid = _bpftool_map_id(BPF_ALIASES_MAP_NAME)
    if mid is None:
        return False, "no dest_alias_map"
    k = ip_to_bpf_hex(from_ip)
    if not k:
        return False, "bad ip"
    r = subprocess.run(
        ["bpftool", "map", "delete", "id", str(mid),
         "key", "hex", _hex_to_spaced(k)],
        capture_output=True, text=True,
    )
    ok = r.returncode == 0 or "no such" in r.stderr.lower()
    return ok, r.stderr.strip() or r.stdout.strip()


def bpf_remote_host_stats():
    mid = _bpftool_map_id(BPF_REMOTE_HOST_MAP_NAME)
    if mid is None:
        return {}
    try:
        out = subprocess.check_output(
            ["bpftool", "map", "dump", "id", str(mid)],
            text=True, stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError:
        return {}
    stats = {}
    blocks = out.split("\n\n")
    for blk in blocks:
        key_hex = None
        value_hex = None
        for ln in blk.splitlines():
            ln = ln.strip()
            if ln.startswith("key:"):
                rest = ln.split(":", 1)[1].strip()
                if rest.lower().startswith("0x"):
                    rest = rest[2:]
                tokens = rest.split()
                if "value:" in tokens:
                    tokens = tokens[:tokens.index("value:")]
                key_hex = "".join(t.lstrip("0x").rstrip(",") for t in tokens if t)
            elif ln.startswith("value:"):
                rest = ln.split(":", 1)[1].strip()
                if rest.lower().startswith("0x"):
                    rest = rest[2:]
                tokens = rest.split()
                value_hex = "".join(t.lstrip("0x").rstrip(",") for t in tokens if t)
        if not (key_hex and value_hex):
            continue
        try:
            kb = bytes.fromhex(key_hex)
        except ValueError:
            continue
        if len(kb) == 16:
            if (kb[0:10] == b'\x00' * 10) and (kb[10:12] == b'\xff\xff'):
                ip = ".".join(str(x) for x in kb[12:16])
            else:
                ip = str(ipaddress.IPv6Address(kb))
        elif len(kb) == 4:
            ip = ".".join(str(x) for x in kb)
        else:
            continue
        try:
            vb = bytes.fromhex(value_hex)
        except ValueError:
            continue
        if len(vb) >= 40:
            instances = int.from_bytes(vb[0:8], "little")
            swaps = int.from_bytes(vb[32:40], "little")
            stats[ip] = {"instances": instances, "swaps": swaps}
    return stats


def bpf_remote_host_delete(ip_str):
    mid = _bpftool_map_id(BPF_REMOTE_HOST_MAP_NAME)
    if mid is None:
        return False, "no remote_host_map"
    k = ip_to_bpf_hex(ip_str)
    if not k:
        return False, "bad ip"
    r = subprocess.run(
        ["bpftool", "map", "delete", "id", str(mid),
         "key", "hex", _hex_to_spaced(k)],
        capture_output=True, text=True,
    )
    ok = r.returncode == 0 or "no such" in r.stderr.lower()
    return ok, r.stderr.strip() or r.stdout.strip()


def _pick_canonical(ips, bpf_stats):
    def score(ip):
        is_v6 = ":" in ip
        try:
            ipobj = ipaddress.ip_address(ip)
            packed = ipobj.packed
            trailing_zeros = len(packed) - len(packed.rstrip(b"\x00"))
        except (ValueError, TypeError):
            trailing_zeros = 0
        inst = bpf_stats.get(ip, {}).get("instances", 0)
        return (inst, int(is_v6), trailing_zeros)
    return max(ips, key=score)


def auto_fold():
    """Line-by-line edit. Only manage IPs in labels with >1 IP.
    Single-IP-label IPs are treated as FOREIGN (preserved)."""
    labels = load_labels()
    bpf_stats = bpf_remote_host_stats()

    by_label = defaultdict(list)
    for ip, lbl in labels.items():
        by_label[lbl].append(ip)

    # KEY FIX: only treat IPs as "managed" if they're part of a label
    # with >1 IP.  Single-IP-label IPs are FOREIGN (preserved).
    managed_ips_for_fold = set()
    for label, ips in by_label.items():
        if not label or len(ips) < 2:
            continue
        managed_ips_for_fold.update(ips)

    desired_managed_rules = []
    managed_canonicals = {}
    results = []

    for label, ips in by_label.items():
        if not label or len(ips) < 2:
            continue
        canonical = _pick_canonical(ips, bpf_stats)
        folded = [ip for ip in ips if ip != canonical]
        deleted = []
        for ip in folded:
            desired_managed_rules.append({
                "from": ip, "to": canonical, "label": label,
            })
            managed_canonicals[ip] = canonical
            ok, err = bpf_aliases_update(ip, canonical)
            if not ok:
                sys.stderr.write(
                    "[auto-fold] BPF aliases update %s -> %s failed: %s\n" %
                    (ip, canonical, err)
                )
            st = bpf_stats.get(ip)
            if st and st.get("swaps", 0) == 0:
                ok2, _ = bpf_remote_host_delete(ip)
                if ok2:
                    deleted.append(ip)
        results.append({
            "label": label,
            "canonical": canonical,
            "folded": folded,
            "deleted_buckets": deleted,
        })

    desired_by_from = {r["from"]: r for r in desired_managed_rules}

    try:
        with open(ALIASES_FILE) as f:
            original_text = f.read()
    except OSError:
        original_text = ""

    new_lines = []
    seen_froms = set()
    removed_folds = []

    for line in original_text.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        if "=" not in stripped:
            new_lines.append(line)
            continue

        try:
            lhs, rhs = stripped.split("=", 1)
            from_ip = lhs.strip()
            rest = rhs.strip().split()
            if not rest:
                new_lines.append(line)
                continue
            to_ip = rest[0]
            label = rest[1] if len(rest) > 1 else ""
        except (ValueError, IndexError):
            new_lines.append(line)
            continue

        # KEY FIX: use managed_ips_for_fold, NOT managed_ips
        if from_ip in managed_ips_for_fold:
            if from_ip in desired_by_from:
                desired = desired_by_from[from_ip]
                if to_ip != desired["to"]:
                    new_line = from_ip + " = " + desired["to"]
                    if desired.get("label"):
                        new_line += " " + desired["label"]
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)
                seen_froms.add(from_ip)
            else:
                bpf_aliases_delete(from_ip)
                removed_folds.append(from_ip)
        else:
            # FOREIGN or single-IP-managed: PRESERVE
            if to_ip in managed_canonicals:
                new_to = managed_canonicals[to_ip]
                if new_to != to_ip:
                    new_line = line.replace(to_ip, new_to, 1)
                    new_lines.append(new_line)
                    bpf_aliases_update(from_ip, new_to)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

    new_rules_to_add = [r for r in desired_managed_rules
                        if r["from"] not in seen_froms]

    if new_rules_to_add:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# auto-fold managed rules (added by labels-api.py auto_fold)")
        for r in new_rules_to_add:
            line = r["from"] + " = " + r["to"]
            if r.get("label"):
                line += " " + r["label"]
            new_lines.append(line)

    new_text = "\n".join(new_lines) + "\n"

    if new_text != original_text:
        try:
            backup = ALIASES_FILE + ".bak." + str(int(time.time()))
            shutil.copy2(ALIASES_FILE, backup)
        except OSError:
            pass
        d = os.path.dirname(ALIASES_FILE) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(new_text)
            os.chmod(tmp, 0o644)
            os.rename(tmp, ALIASES_FILE)
        except OSError:
            try: os.unlink(tmp)
            except OSError: pass
            raise
        sys.stderr.write(
            "[auto-fold] %s: %d -> %d lines (managed_fold: %d, removed: %d, appended: %d)\n" %
            (ALIASES_FILE, len(original_text.splitlines()), len(new_lines),
             len(desired_managed_rules), len(removed_folds),
             len(new_rules_to_add))
        )

    if removed_folds:
        results.append({"removed_folds": removed_folds})

    return results


class LabelsHandler(BaseHTTPRequestHandler):
    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        labels = load_labels()
        if "ip" in qs:
            ip = qs["ip"][0]
            self._send_json(200, {"ip": ip, "label": labels.get(ip, "")})
        else:
            aliases = [r["raw"] for r in load_aliases_rules()]
            self._send_json(200, {"labels": labels, "aliases": aliases})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/labels", "/"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            req = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        labels = load_labels()
        if "ips" in req and isinstance(req["ips"], list):
            label = req.get("label", "").strip()
            ips = [ip.strip() for ip in req["ips"] if ip.strip()]
            if not label:
                for ip in ips:
                    labels.pop(ip, None)
            else:
                for ip in ips:
                    labels[ip] = label
            save_labels(labels)
            fold_results = auto_fold()
            self._send_json(200, {"ok": True, "labels": labels,
                                  "fold_results": fold_results})
            return
        ip = req.get("ip", "").strip()
        label = req.get("label", "").strip()
        if not ip:
            self._send_json(400, {"error": "ip is required"})
            return
        if not label:
            labels.pop(ip, None)
        else:
            labels[ip] = label
        save_labels(labels)
        fold_results = auto_fold()
        self._send_json(200, {"ok": True, "ip": ip, "label": label,
                              "labels": labels, "fold_results": fold_results})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        ip = qs.get("ip", [""])[0]
        if not ip:
            self._send_json(400, {"error": "ip is required"})
            return
        labels = load_labels()
        labels.pop(ip, None)
        save_labels(labels)
        fold_results = auto_fold()
        self._send_json(200, {"ok": True, "ip": ip, "fold_results": fold_results})

    def log_message(self, fmt, *args):
        sys.stderr.write("[labels-api] %s %s\n" % (self.client_address[0], fmt % args))


if __name__ == "__main__":
    print("[labels-api] startup auto-fold pass...", file=sys.stderr)
    try:
        results = auto_fold()
        for r in results:
            if "removed_folds" in r:
                if r["removed_folds"]:
                    print("[labels-api]   removed stale folds: %s" % r["removed_folds"],
                          file=sys.stderr)
            else:
                print("[labels-api]   label '%s': canonical=%s, folded=%s, deleted_buckets=%s" %
                      (r["label"], r["canonical"], r["folded"], r["deleted_buckets"]),
                      file=sys.stderr)
    except Exception as e:
        print("[labels-api] startup auto-fold failed: %s" % e, file=sys.stderr)
    print("[labels-api] listening on port %d" % PORT, file=sys.stderr)
    server = HTTPServer(("127.0.0.1", PORT), LabelsHandler)
    server.serve_forever()
