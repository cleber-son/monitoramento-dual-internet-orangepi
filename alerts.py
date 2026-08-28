"""Alertas: regras de disparo, anti-flapping, webhook e barramento SSE."""

import http.client
import json
import logging
import queue
import socket
import ssl
import threading
import time
import urllib.parse

import db

log = logging.getLogger("netmon.alerts")

SO_BINDTODEVICE = 25
MAX_SSE_CLIENTS = 10
FLAP_WINDOW = 600      # 10 min
FLAP_COUNT = 3         # 3 quedas na janela => instavel

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:                                    # pragma: no cover
    from datetime import timedelta, timezone
    TZ = timezone(timedelta(hours=-3))

from datetime import datetime


def fmt_hora(ts):
    return datetime.fromtimestamp(int(ts), TZ).strftime("%H:%M:%S")


def fmt_iso(ts):
    return datetime.fromtimestamp(int(ts), TZ).isoformat(timespec="seconds")


def fmt_dur(seg):
    if seg is None:
        return "?"
    seg = int(seg)
    if seg < 60:
        return "%ds" % seg
    if seg < 3600:
        return "%dmin %ds" % (seg // 60, seg % 60)
    h, r = divmod(seg, 3600)
    if h < 24:
        return "%dh %dmin" % (h, r // 60)
    d, h = divmod(h, 24)
    return "%dd %dh" % (d, h)


CAUSA_TXT = {
    "provedor": "provavel queda do provedor",
    "roteador_local": "roteador local nao responde - verifique o roteador",
    "cabo": "sem link fisico - verifique o cabo do adaptador",
}


# ---------------------------------------------------------------------------
# Barramento SSE
# ---------------------------------------------------------------------------
class Bus:
    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            while len(self._subs) >= MAX_SSE_CLIENTS:
                old = self._subs.pop(0)
                try:
                    old.put_nowait(None)
                except queue.Full:
                    pass
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind, data):
        msg = (kind, data)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def count(self):
        with self._lock:
            return len(self._subs)


# ---------------------------------------------------------------------------
# Envio HTTP com fallback por interface
# ---------------------------------------------------------------------------
# O Discord so aceita {"content": ...} ou {"embeds": [...]}; qualquer outro JSON
# volta 400. O Slack quer {"text": ...}. Entao o payload generico e traduzido
# conforme o destino, em vez de exigir que o usuario monte um adaptador.
DISCORD_COR = {
    "queda": 0xD03B3B, "recuperacao": 0x0CA30C, "latencia_alta": 0xFAB219,
    "latencia_normalizada": 0x0CA30C, "instavel": 0xEC835A, "teste": 0x3987E5,
}
ICONE = {"queda": "🔴", "recuperacao": "🟢", "latencia_alta": "🟡",
         "latencia_normalizada": "🟢", "instavel": "⚠️", "teste": "✅"}


def eh_discord(url):
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


def _campo(nome, valor, inline=True):
    return {"name": nome, "value": str(valor), "inline": inline}


def para_discord(p):
    ev = p.get("event", "teste")
    icone = ICONE.get(ev, "•")
    titulo = {
        "queda": "%s %s CAIU" % (icone, p.get("link")),
        "recuperacao": "%s %s VOLTOU" % (icone, p.get("link")),
        "latencia_alta": "%s %s com latencia alta" % (icone, p.get("link")),
        "latencia_normalizada": "%s %s normalizou" % (icone, p.get("link")),
        "instavel": "%s %s instavel" % (icone, p.get("link")),
        "teste": "%s netmon conectado ao Discord" % icone,
    }.get(ev, "%s netmon" % icone)

    campos = []
    if p.get("inicio_ts"):
        campos.append(_campo("Inicio", fmt_hora(p["inicio_ts"])))
    if p.get("fim_ts"):
        campos.append(_campo("Retorno", fmt_hora(p["fim_ts"])))
    if p.get("duracao_txt"):
        campos.append(_campo("Ficou fora", p["duracao_txt"]))
    if p.get("causa"):
        campos.append(_campo("Causa", CAUSA_TXT.get(p["causa"], p["causa"]), False))
    m = p.get("metricas") or {}
    if m.get("rtt_avg") is not None:
        campos.append(_campo("Latencia", "%s ms" % m["rtt_avg"]))
    if m.get("loss") is not None:
        campos.append(_campo("Perda", "%s%%" % m["loss"]))
    if p.get("iface"):
        campos.append(_campo("Interface", "%s (%s)" % (p["iface"], m.get("gateway") or "-"), False))
    if p.get("flapping"):
        campos.append(_campo("Atencao", "link instavel: varias quedas seguidas", False))

    return {
        "username": "netmon",
        "embeds": [{
            "title": titulo,
            "description": p.get("mensagem", ""),
            "color": DISCORD_COR.get(ev, 0x898781),
            "fields": campos,
            "footer": {"text": "netmon · %s" % p.get("host", "")},
            "timestamp": datetime.fromtimestamp(
                p.get("inicio_ts") or time.time(), TZ).isoformat(),
        }],
    }


def adaptar(url, payload):
    """Traduz o payload generico para o formato que o destino entende."""
    if eh_discord(url):
        return para_discord(payload)
    if "hooks.slack.com" in url:
        return {"text": payload.get("mensagem", "netmon")}
    return payload


def post_json(url, payload, iface=None, timeout=10):
    """POST JSON. Se `iface` vier preenchida, o socket sai por aquela interface."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, 0, "URL invalida"
    use_tls = u.scheme == "https"
    port = u.port or (443 if use_tls else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    sock = None
    try:
        host_ip = socket.gethostbyname(u.hostname)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        if iface:
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        sock.connect((host_ip, port))
        if use_tls:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=u.hostname)

        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
        conn.sock = sock
        conn.request("POST", path, body, {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "netmon/1.0",
            "Content-Length": str(len(body)),
        })
        resp = conn.getresponse()
        resp.read(4096)
        conn.close()
        return 200 <= resp.status < 300, resp.status, None
    except Exception as exc:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return False, 0, str(exc)


# ---------------------------------------------------------------------------
# Gerenciador de alertas
# ---------------------------------------------------------------------------
class Alerts:
    def __init__(self, bus, stop_event):
        self.bus = bus
        self.stop_event = stop_event
        self.q = queue.Queue(maxsize=200)
        self._last_sent = {}          # (link,event) -> ts
        self.probes = []              # preenchido pelo netmon.py
        self.thread = threading.Thread(target=self._sender_loop,
                                       name="alert-sender", daemon=True)

    def start(self):
        self.thread.start()

    # -- callbacks das sondas --------------------------------------------
    def on_down(self, probe, cause, started_at, event_id):
        recentes = db.count_recent_events(probe.link_id, "QUEDA", time.time() - FLAP_WINDOW)
        flapping = recentes >= FLAP_COUNT
        if flapping and event_id:
            db.mark_flapping(event_id)
        txt = CAUSA_TXT.get(cause, cause or "causa desconhecida")
        msg = "🔴 %s CAIU as %s (%s)" % (probe.name_link, fmt_hora(started_at), txt)
        log.warning(msg)
        payload = self._payload("queda", probe, "DOWN", cause, started_at,
                                None, None, flapping, msg)
        self._emit("queda", probe, payload, event_id)

    def on_up(self, probe, started_at, ended_at, duration, event_id):
        msg = "🟢 %s VOLTOU as %s — ficou fora %s (caiu as %s)" % (
            probe.name_link, fmt_hora(ended_at), fmt_dur(duration), fmt_hora(started_at))
        log.warning(msg)
        payload = self._payload("recuperacao", probe, "UP", None, started_at,
                                ended_at, duration, False, msg)
        self._emit("recuperacao", probe, payload, event_id, ignore_cooldown=True)

    def on_degraded(self, probe, rtt, loss, jitter, event_id):
        msg = "🟡 %s com latencia alta: %.0f ms, perda %.0f%%, jitter %.0f ms" % (
            probe.name_link, rtt, loss, jitter)
        log.warning(msg)
        payload = self._payload("latencia_alta", probe, "DEGRADED", None,
                                int(time.time()), None, None, False, msg)
        self._emit("latencia_alta", probe, payload, event_id)

    def on_normalized(self, probe, rtt, event_id):
        msg = "🟢 %s normalizou (%.0f ms)" % (probe.name_link, rtt)
        log.info(msg)
        payload = self._payload("latencia_normalizada", probe, "UP", None,
                                int(time.time()), None, None, False, msg)
        self._emit("latencia_normalizada", probe, payload, event_id,
                   ignore_cooldown=True)

    # -- internos ---------------------------------------------------------
    def _payload(self, event, probe, estado, causa, inicio, fim, dur, flapping, msg):
        return {
            "source": "netmon",
            "host": socket.gethostname(),
            "event": event,
            "link": probe.name_link,
            "iface": probe.iface,
            "estado": estado,
            "causa": causa,
            "inicio": fmt_iso(inicio),
            "fim": fmt_iso(fim) if fim else None,
            "inicio_ts": int(inicio),
            "fim_ts": int(fim) if fim else None,
            "duracao_s": dur,
            "duracao_txt": fmt_dur(dur) if dur is not None else None,
            "flapping": bool(flapping),
            "metricas": {
                "rtt_avg": probe.last.get("rtt_avg"),
                "loss": probe.last.get("loss"),
                "gw_ok": bool(probe.last.get("gw_ok")),
                "ip": probe.ip,
                "gateway": probe.gateway,
            },
            "mensagem": msg,
        }

    def _emit(self, event, probe, payload, event_id, ignore_cooldown=False):
        payload["event_id"] = event_id
        self.bus.publish("alerta", payload)          # front reage na hora
        cfg = db.get_config()
        if cfg.get("webhook_enabled") != "1" or not cfg.get("webhook_url"):
            return
        key = (probe.name_link, event)
        cooldown = db.cfg_int("cooldown_s", 300)
        now = time.time()
        if not ignore_cooldown and now - self._last_sent.get(key, 0) < cooldown:
            log.info("webhook em cooldown para %s/%s", probe.name_link, event)
            return
        self._last_sent[key] = now
        try:
            self.q.put_nowait((cfg["webhook_url"], payload, 0))
        except queue.Full:
            log.warning("fila de webhook cheia")

    def send_test(self, url):
        agora = int(time.time())
        payload = {
            "source": "netmon", "host": socket.gethostname(), "event": "teste",
            "link": "-", "estado": "TESTE", "inicio": fmt_iso(agora),
            "inicio_ts": agora, "fim_ts": None,
            "mensagem": "netmon conectado. Voce sera avisado aqui quando um "
                        "link cair, voltar ou ficar com latencia alta.",
        }
        ok, status, err = self._send_with_fallback(url, payload)
        return {"ok": ok, "status_code": status, "erro": err}

    def _send_with_fallback(self, url, payload):
        """Tenta pela rota normal; se falhar, tenta por cada link que esta UP.

        Isso importa porque a rota default sai pela GIGA: se a GIGA cair, o
        webhook precisa sair pela IMPACTO para o aviso chegar.
        """
        corpo = adaptar(url, payload)          # Discord/Slack tem formato proprio
        ok, status, err = post_json(url, corpo)
        if ok:
            return ok, status, err
        for p in self.probes:
            if p.state in ("UP", "DEGRADED"):
                ok2, status2, err2 = post_json(url, corpo, iface=p.iface)
                if ok2:
                    log.info("webhook entregue pela interface %s", p.iface)
                    return ok2, status2, err2
                err = err2 or err
        return False, status, err

    def _sender_loop(self):
        while not self.stop_event.is_set():
            try:
                url, payload, tentativa = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            ok, status, err = self._send_with_fallback(url, payload)
            if ok:
                log.info("webhook ok (%s) para %s", status, payload.get("event"))
                continue
            log.warning("webhook falhou (tentativa %d): %s", tentativa + 1, err)
            if tentativa < 2:
                delay = 30 if tentativa == 0 else 120
                threading.Timer(
                    delay,
                    lambda: self.q.put((url, payload, tentativa + 1)),
                ).start()
