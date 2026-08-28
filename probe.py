"""Motor de sondagem por link.

Cada link e uma interface fisica, nunca um IP fixo: o gateway e o endereco sao
redetectados em runtime, entao mover o cabo do adaptador USB de uma rede para
outra (ex.: 192.168.200.0 -> 192.168.17.0) nao exige mexer no codigo.

Todas as sondas sao presas a interface (ping -I / SO_BINDTODEVICE) para nao
vazarem pela rota default nem pela VPN.
"""

import json
import logging
import random
import re
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db

log = logging.getLogger("netmon.probe")

SO_BINDTODEVICE = 25

CYCLE = db.SAMPLE_INTERVAL   # 2s entre ciclos
DOWN_CONFIRM = 2           # 2 ciclos ruins seguidos => QUEDA em ~3-4s
UP_CONFIRM = 3             # 3 ciclos bons seguidos => retorno em ~6s
DEG_LOSS_CYCLES = 3        # ~6s de perda alta
DEG_LAT_CYCLES = 5         # ~10s de latencia alta
DEG_PICO_CYCLES = 2        # latencia acima de 3x o limiar: alerta quase imediato
DEG_PICO_FATOR = 3.0
DEG_CLEAR_CYCLES = 10      # ~20s para sair de degradado (histerese)
EWMA_ALPHA = 0.4           # reage rapido, ja que o ciclo agora e curto

# Alvos escolhidos de proposito fora de 1.1.1.1/8.8.8.8: existem rotas estaticas
# fixando esses dois IPs em `dev eth0`, o que confundiria a medicao da IMPACTO.
PING_TARGETS = ["9.9.9.9", "208.67.222.222"]
DNS_SERVER = "9.9.9.9"     # nunca 127.0.0.1: mediria o Pi-hole, nao o link
DNS_NAME = "www.google.com"
TCP_TARGET = ("9.9.9.9", 443)
HTTP_TARGET = ("1.1.1.1", 80, "cp.cloudflare.com", "/generate_204")

RE_LOSS = re.compile(r"([\d.]+)% packet loss")
RE_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")

# cadencia das sondas secundarias, em ciclos de 2s
EVERY_DNS = 15     # 30s
EVERY_TCP = 15     # 30s
EVERY_HTTP = 30    # 60s
EVERY_IFACE = 30   # 60s


# ---------------------------------------------------------------------------
# Descoberta de interface
# ---------------------------------------------------------------------------
def _run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("comando falhou %s: %s", cmd, exc)
        return None


def iface_info(iface):
    """Retorna (ip, gateway, up) da interface, redetectado a cada chamada."""
    ip = gw = None
    up = False

    res = _run(["ip", "-j", "addr", "show", "dev", iface])
    if res and res.returncode == 0 and res.stdout.strip():
        try:
            data = json.loads(res.stdout)
            if data:
                entry = data[0]
                up = entry.get("operstate") in ("UP", "UNKNOWN")
                for a in entry.get("addr_info", []):
                    if a.get("family") == "inet":
                        ip = a.get("local")
                        break
        except ValueError:
            pass
    else:  # fallback texto
        res = _run(["ip", "addr", "show", "dev", iface])
        if res and res.returncode == 0:
            up = "state UP" in res.stdout or "state UNKNOWN" in res.stdout
            m = re.search(r"inet ([\d.]+)/", res.stdout)
            if m:
                ip = m.group(1)

    res = _run(["ip", "-j", "route", "show", "dev", iface])
    if res and res.returncode == 0 and res.stdout.strip():
        try:
            for r in json.loads(res.stdout):
                if r.get("dst") == "default" and r.get("gateway"):
                    gw = r["gateway"]
                    break
        except ValueError:
            pass
    if gw is None:
        res = _run(["ip", "route", "show", "dev", iface])
        if res and res.returncode == 0:
            m = re.search(r"^default via ([\d.]+)", res.stdout, re.M)
            if m:
                gw = m.group(1)
    # ultimo recurso: assume .1 da propria rede so para ter um alvo de gateway
    if gw is None and ip:
        gw = ip.rsplit(".", 1)[0] + ".1"

    return ip, gw, up


# ---------------------------------------------------------------------------
# Sondas
# ---------------------------------------------------------------------------
def ping(iface, target, count=3, interval=0.25, wait=1):
    """Retorna dict(loss, rtt_min, rtt_avg, rtt_max, jitter, err)."""
    cmd = ["ping", "-I", iface, "-n", "-q", "-c", str(count),
           "-i", str(interval), "-W", str(wait), target]
    res = _run(cmd, timeout=count * (interval + wait) + 6)
    out = {"loss": 100.0, "rtt_min": None, "rtt_avg": None,
           "rtt_max": None, "jitter": None, "err": None}
    if res is None:
        out["err"] = "timeout"
        return out
    text = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 2 or "Cannot assign" in text or "Network is unreachable" in text \
            or "unknown iface" in text or "Name or service not known" in text:
        out["err"] = "no_link"
        return out
    m = RE_LOSS.search(text)
    if m:
        try:
            out["loss"] = float(m.group(1))
        except ValueError:
            pass
    m = RE_RTT.search(text)
    if m:
        out["rtt_min"] = float(m.group(1))
        out["rtt_avg"] = float(m.group(2))
        out["rtt_max"] = float(m.group(3))
        out["jitter"] = float(m.group(4))
    return out


def _bind(sock, iface):
    sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")


def dns_probe(iface, timeout=2.0):
    """Consulta A montada a mao via UDP, presa a interface. ms, ou -1 em falha."""
    tid = random.getrandbits(16)
    pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in DNS_NAME.split("."):
        pkt += bytes([len(label)]) + label.encode()
    pkt += b"\x00" + struct.pack(">HH", 1, 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _bind(sock, iface)
        sock.settimeout(timeout)
        t0 = time.monotonic()
        sock.sendto(pkt, (DNS_SERVER, 53))
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            data, _ = sock.recvfrom(2048)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == tid:
                return round((time.monotonic() - t0) * 1000, 2)
        return -1.0
    except OSError:
        return -1.0
    finally:
        sock.close()


def tcp_probe(iface, timeout=3.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _bind(sock, iface)
        sock.settimeout(timeout)
        t0 = time.monotonic()
        sock.connect(TCP_TARGET)
        return round((time.monotonic() - t0) * 1000, 2)
    except OSError:
        return -1.0
    finally:
        sock.close()


def http_probe(iface, timeout=5.0):
    ip, port, host, path = HTTP_TARGET
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _bind(sock, iface)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
               "Connection: close\r\n\r\n" % (path, host))
        sock.sendall(req.encode())
        data = sock.recv(256)
        return data.startswith(b"HTTP/") and b" 204" in data[:20] or data.startswith(b"HTTP/")
    except OSError:
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Maquina de estados por link
# ---------------------------------------------------------------------------
class LinkProbe(threading.Thread):

    def __init__(self, link_id, name, iface, writer, alerts, stop_event):
        super().__init__(name="probe-%s" % name, daemon=True)
        self.link_id = link_id
        self.name_link = name
        self.iface = iface
        self.writer = writer
        self.alerts = alerts
        self.stop_event = stop_event

        now = int(time.time())
        self.state = "UP"
        self.state_since = now
        self.fail_streak = 0
        self.ok_streak = 0
        self.first_fail_ts = None
        self.first_ok_ts = None
        self.down_event_id = None
        self.lat_event_id = None
        self.deg_streak = 0
        self.clear_streak = 0

        self.ewma_rtt = None
        self.ewma_loss = 0.0
        self.ewma_jitter = None

        self.ip = None
        self.gateway = None
        self.iface_up = False
        self.gw_hist = []          # ultimos 4 resultados de ping ao gateway
        self.last = {}
        # enquanto um teste de velocidade satura o link, nao faz sentido
        # anunciar "latencia alta": a fila e nossa
        self.mudo_degradacao_ate = 0
        self._lock = threading.Lock()
        self._cycle = 0
        # 3 sondas ICMP simultaneas por ciclo: alvo primario, alvo alternativo
        # e gateway
        self.pool = ThreadPoolExecutor(max_workers=3,
                                       thread_name_prefix="ping-%s" % name)

    # -- helpers ----------------------------------------------------------
    def _ewma(self, prev, value):
        if value is None:
            return prev
        if prev is None:
            return value
        return EWMA_ALPHA * value + (1 - EWMA_ALPHA) * prev

    def silenciar_degradacao(self, segundos):
        """Suspende a deteccao de latencia alta (usado no teste de velocidade).

        A queda continua sendo detectada normalmente: so o alerta de degradacao
        e suspenso, porque quem esta enchendo o link somos nos.
        """
        self.mudo_degradacao_ate = time.time() + max(0, segundos)
        log.info("%s: alerta de latencia alta suspenso por %ds",
                 self.name_link, int(segundos))

    def resetar(self):
        """Zera a maquina de estados apos um reset do banco.

        Volta para UP mesmo se o link estiver caido: os eventos que apontavam
        para linhas apagadas sumiram, e o proximo ciclo (2s) reavalia e reabre
        a queda do zero, com evento novo e integro.
        """
        agora = int(time.time())
        self.state = "UP"
        self.state_since = agora
        self.fail_streak = self.ok_streak = 0
        self.first_fail_ts = self.first_ok_ts = None
        self.down_event_id = self.lat_event_id = None
        self.deg_streak = self.clear_streak = 0
        self.ewma_rtt = self.ewma_jitter = None
        self.ewma_loss = 0.0
        self.gw_hist = []
        log.warning("maquina de estados de %s reiniciada", self.name_link)

    def snapshot(self):
        with self._lock:
            snap = dict(self.last)
        snap.update({
            "name": self.name_link,
            "iface": self.iface,
            "ip": self.ip,
            "gateway": self.gateway,
            "state": self.state,
            "state_since": self.state_since,
            "rtt_ewma": round(self.ewma_rtt, 2) if self.ewma_rtt is not None else None,
            "loss_ewma": round(self.ewma_loss, 2),
        })
        return snap

    # -- ciclo ------------------------------------------------------------
    def run(self):
        log.info("sonda %s iniciada na interface %s", self.name_link, self.iface)
        self._refresh_iface()
        self._reconcile_open_events()
        next_at = time.monotonic()
        while not self.stop_event.is_set():
            next_at += CYCLE
            try:
                self._one_cycle()
            except Exception:
                log.exception("erro no ciclo da sonda %s", self.name_link)
            delay = next_at - time.monotonic()
            if delay < 0:                      # atrasou: nao acumula divida
                next_at = time.monotonic()
                delay = 0
            if self.stop_event.wait(delay):
                break
        self.pool.shutdown(wait=False)
        log.info("sonda %s encerrada", self.name_link)

    def _refresh_iface(self):
        self.ip, self.gateway, self.iface_up = iface_info(self.iface)

    def _reconcile_open_events(self):
        """Depois de um restart, eventos abertos deste link voltam a ser rastreados."""
        for ev in db.open_events():
            if ev["link_id"] != self.link_id:
                continue
            if ev["type"] == "QUEDA":
                self.down_event_id = ev["id"]
                self.state = "DOWN"
                self.state_since = ev["started_at"]
                self.first_fail_ts = ev["started_at"]
                self.fail_streak = DOWN_CONFIRM
            elif ev["type"] == "LATENCIA_ALTA":
                self.lat_event_id = ev["id"]

    def _one_cycle(self):
        self._cycle += 1
        ts = int(time.time())

        if self._cycle == 1 or self._cycle % EVERY_IFACE == 0:
            self._refresh_iface()

        # 1+2) os dois alvos de internet e o gateway vao JUNTOS, em paralelo.
        # Em serie, um ciclo com o link caido custaria ~4,5s (tres esperas de
        # timeout somadas) e o alerta demoraria. Em paralelo custa ~1,5s, que e
        # o que permite confirmar uma queda em 3-4s.
        f_a = self.pool.submit(ping, self.iface, PING_TARGETS[0])
        f_b = self.pool.submit(ping, self.iface, PING_TARGETS[1])
        f_gw = (self.pool.submit(ping, self.iface, self.gateway, 2, 0.25, 1)
                if self.gateway else None)

        res = f_a.result()
        alt = f_b.result()
        # o alvo primario manda; o segundo so entra se o primario sumiu por
        # completo, para nao mascarar perda parcial real do link
        if (res["loss"] >= 100.0 or res["err"]) and not (alt["loss"] >= 100.0 or alt["err"]):
            res = alt

        no_link = (res["err"] == "no_link" and alt["err"] == "no_link") or not self.iface_up

        # gateway: distingue queda do provedor de queda do roteador local.
        # 2 pacotes, nao 1: a ONT da GIGA descarta ~12% dos ICMP dirigidos a ela,
        # e um unico pacote perdido apontaria "roteador local" sem motivo.
        gw_ok, gw_rtt = False, None
        if f_gw is not None:
            g = f_gw.result()
            gw_ok = g["loss"] < 100.0
            gw_rtt = g["rtt_avg"]
        self.gw_hist.append(gw_ok)
        del self.gw_hist[:-4]
        if not gw_ok and self.gateway:
            self._refresh_iface()

        # 3) sondas secundarias
        dns_ms = tcp_ms = None
        http_ok = None
        if self._cycle % EVERY_DNS == 0:
            dns_ms = dns_probe(self.iface)
        if self._cycle % EVERY_TCP == 0:
            tcp_ms = tcp_probe(self.iface)
        if self._cycle % EVERY_HTTP == 0:
            http_ok = http_probe(self.iface)

        self.ewma_rtt = self._ewma(self.ewma_rtt, res["rtt_avg"])
        self.ewma_jitter = self._ewma(self.ewma_jitter, res["jitter"])
        self.ewma_loss = EWMA_ALPHA * res["loss"] + (1 - EWMA_ALPHA) * self.ewma_loss

        bad = res["loss"] >= 100.0 or no_link
        self._transition(ts, bad, no_link, gw_ok, res)

        sample = {
            "ts": ts, "link_id": self.link_id, "state": self.state,
            "loss": res["loss"], "rtt_min": res["rtt_min"], "rtt_avg": res["rtt_avg"],
            "rtt_max": res["rtt_max"], "jitter": res["jitter"],
            "gw_ok": gw_ok, "gw_rtt": gw_rtt,
            "dns_ms": dns_ms, "tcp_ms": tcp_ms, "http_ok": http_ok,
        }
        self.writer.put(sample)
        with self._lock:
            keep = dict(self.last)
            keep.update({k: v for k, v in sample.items() if v is not None or k in
                         ("rtt_avg", "rtt_min", "rtt_max", "jitter", "gw_rtt")})
            keep["ts"] = ts
            keep["gw_ok"] = gw_ok
            self.last = keep

    # -- transicoes -------------------------------------------------------
    def _transition(self, ts, bad, no_link, gw_ok, res):
        cfg = db.get_config()
        lat_limiar = float(cfg.get("lat_limiar_ms", 150) or 150)
        loss_limiar = float(cfg.get("loss_limiar_pct", 20) or 20)
        jit_limiar = float(cfg.get("jitter_limiar_ms", 60) or 60)

        if bad:
            self.ok_streak = 0
            self.first_ok_ts = None
            if self.fail_streak == 0:
                self.first_fail_ts = ts       # hora EXATA da queda (retroativa)
            self.fail_streak += 1
            if self.fail_streak >= DOWN_CONFIRM and self.state not in ("DOWN", "NO_LINK"):
                new_state = "NO_LINK" if no_link else "DOWN"
                # o roteador local so e culpado se ficou mudo na janela inteira
                gw_vivo = any(self.gw_hist)
                cause = ("cabo" if no_link else
                         "roteador_local" if not gw_vivo else "provedor")
                self.state = new_state
                self.state_since = self.first_fail_ts
                self.down_event_id = db.open_event(
                    self.link_id, "QUEDA", self.first_fail_ts, cause,
                    {"gateway": self.gateway, "iface": self.iface, "gw_ok": gw_ok},
                )
                self.alerts.on_down(self, cause, self.first_fail_ts, self.down_event_id)
            return

        # ciclo bom
        self.fail_streak = 0
        if self.ok_streak == 0:
            self.first_ok_ts = ts
        self.ok_streak += 1

        if self.state in ("DOWN", "NO_LINK"):
            if self.ok_streak >= UP_CONFIRM:
                ended = self.first_ok_ts
                dur = None
                if self.down_event_id:
                    dur = db.close_event(self.down_event_id, ended,
                                         {"rtt_retorno": res["rtt_avg"]})
                started = self.state_since
                self.state = "UP"
                self.state_since = ended
                self.alerts.on_up(self, started, ended, dur, self.down_event_id)
                self.down_event_id = None
            return

        # UP <-> DEGRADED
        if ts < self.mudo_degradacao_ate and self.state != "DEGRADED":
            # teste de velocidade em curso: o link esta cheio por nossa conta
            self.deg_streak = 0
            return

        rtt = self.ewma_rtt if self.ewma_rtt is not None else 0.0
        jit = self.ewma_jitter if self.ewma_jitter is not None else 0.0
        degraded_now = (self.ewma_loss >= loss_limiar or rtt > lat_limiar or jit > jit_limiar)
        if rtt > lat_limiar * DEG_PICO_FATOR:
            needed = DEG_PICO_CYCLES        # pico absurdo: avisa quase na hora
        elif self.ewma_loss >= loss_limiar:
            needed = DEG_LOSS_CYCLES
        else:
            needed = DEG_LAT_CYCLES

        if degraded_now:
            self.clear_streak = 0
            self.deg_streak += 1
            if self.deg_streak >= needed and self.state == "UP":
                self.state = "DEGRADED"
                self.state_since = ts
                self.lat_event_id = db.open_event(
                    self.link_id, "LATENCIA_ALTA", ts, None,
                    {"rtt": round(rtt, 1), "loss": round(self.ewma_loss, 1),
                     "jitter": round(jit, 1), "limiar_ms": lat_limiar},
                )
                self.alerts.on_degraded(self, rtt, self.ewma_loss, jit, self.lat_event_id)
        else:
            self.deg_streak = 0
            if self.state == "DEGRADED":
                # histerese: so limpa abaixo de 80% do limiar
                folga = (rtt < lat_limiar * 0.8 and self.ewma_loss < loss_limiar * 0.8
                         and jit < jit_limiar * 0.8)
                if folga:
                    self.clear_streak += 1
                    if self.clear_streak >= DEG_CLEAR_CYCLES:
                        self.state = "UP"
                        self.state_since = ts
                        if self.lat_event_id:
                            db.close_event(self.lat_event_id, ts)
                        self.alerts.on_normalized(self, rtt, self.lat_event_id)
                        self.lat_event_id = None
                        self.clear_streak = 0
                else:
                    self.clear_streak = 0
