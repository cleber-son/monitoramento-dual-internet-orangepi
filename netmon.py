#!/usr/bin/env python3
"""netmon - monitor dos links de internet GIGA e IMPACTO.

Processo unico, somente biblioteca padrao do Python 3.10.
Threads: 2 sondas + escritor do banco + manutencao + difusor SSE + servidor HTTP.
"""

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import alerts as alerts_mod          # noqa: E402
import db                            # noqa: E402
import probe as probe_mod            # noqa: E402
import server as server_mod          # noqa: E402

LOG_PATH = os.path.join(BASE_DIR, "netmon.log")
PID_PATH = os.path.join(BASE_DIR, "netmon.pid")

BROADCAST_EVERY = probe_mod.CYCLE     # difunde status a cada ciclo (2s)
UPTIME_EVERY = 30                     # recalcula uptime a cada 30 difusoes (~60s)


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    # o aparelho roda em UTC, mas o painel e o relatorio mostram America/Sao_Paulo:
    # o log segue o mesmo fuso para os horarios baterem na hora de investigar
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, alerts_mod.TZ).timetuple()
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # o console so entra quando rodamos no terminal; sob o cron a saida ja e
    # redirecionada para o mesmo netmon.log e duplicaria cada linha
    if sys.stdout.isatty():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)


log = logging.getLogger("netmon")


def broadcaster(app):
    """Publica o estado atual no barramento SSE a cada ciclo."""
    tick = 0
    stop = app["stop"]
    while not stop.is_set():
        tick += 1
        try:
            now = int(time.time())
            links = []
            for p in app["probes"]:
                snap = p.snapshot()
                snap["evento_aberto"] = server_mod.evento_aberto(p.link_id)
                if tick % UPTIME_EVERY == 1:
                    snap["uptime"] = server_mod.uptime_periodos(p.link_id, now)
                    snap["ultima_queda"] = server_mod.ultima_queda(p.link_id)
                links.append(snap)
            app["bus"].publish("status", {"ts": now, "porta": app.get("port"),
                                          "links": links})
        except Exception:
            log.exception("falha difundindo status")
        stop.wait(BROADCAST_EVERY)


def maintenance(app):
    """Rollups, retencao e checkpoint do WAL."""
    stop = app["stop"]
    last_hour = 0
    last_purge = 0
    stop.wait(20)
    while not stop.is_set():
        try:
            db.rollup_minutes()
            now = time.time()
            if now - last_hour >= 3600:
                db.rollup_hours()
                last_hour = now
            # purga uma vez por dia, de madrugada
            hora = time.localtime(now).tm_hour
            if now - last_purge >= 20 * 3600 and hora == 4:
                log.info("executando purga e checkpoint")
                db.purge()
                last_purge = now
        except Exception:
            log.exception("falha na manutencao")
        stop.wait(60)


def main():
    setup_logging()
    log.info("=" * 60)
    log.info("netmon iniciando (pid %d)", os.getpid())

    db.init()
    db.load_config()

    stop = threading.Event()
    bus = alerts_mod.Bus()
    alerts = alerts_mod.Alerts(bus, stop)
    writer = db.Writer(stop)

    app = {
        "probes": [], "bus": bus, "alerts": alerts, "stop": stop,
        "started": time.time(), "port": None,
    }

    probes = []
    conn = db.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id,name,iface FROM links WHERE enabled=1 ORDER BY id").fetchall()
    finally:
        conn.close()
    for r in rows:
        probes.append(probe_mod.LinkProbe(r["id"], r["name"], r["iface"],
                                          writer, alerts, stop))
    app["probes"] = probes
    alerts.probes = probes

    srv = server_mod.build(app)

    with open(PID_PATH, "w") as fh:
        fh.write(str(os.getpid()))

    def encerrar(signum, _frame):
        log.info("sinal %s recebido, encerrando", signum)
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)

    writer.start()
    alerts.start()
    for p in probes:
        p.start()
    threading.Thread(target=broadcaster, args=(app,), name="broadcast",
                     daemon=True).start()
    threading.Thread(target=maintenance, args=(app,), name="manutencao",
                     daemon=True).start()

    log.info("monitorando: %s", ", ".join("%s(%s)" % (p.name_link, p.iface)
                                          for p in probes))
    log.info("acesse http://<ip-do-orangepi>:%d/", app["port"])
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        srv.server_close()
        writer.join(timeout=10)
        try:
            os.unlink(PID_PATH)
        except OSError:
            pass
        log.info("netmon encerrado")


if __name__ == "__main__":
    main()
