#!/usr/bin/env python3
"""netmon - monitor dos links GIGA e IMPACTO e da latencia ate o roteador.

Processo unico, somente biblioteca padrao do Python 3.10.
Threads: uma sonda por link + escritor do banco + manutencao + difusor SSE +
servidor HTTP.
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
import mesh as mesh_mod              # noqa: E402
import probe as probe_mod            # noqa: E402
import server as server_mod          # noqa: E402
import speedtest as speedtest_mod    # noqa: E402

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
            vigia = app.get("dns_lan")
            app["bus"].publish("status", {"ts": now, "porta": app.get("port"),
                                          "links": links,
                                          "dns_lan": vigia.snapshot() if vigia else None})
        except Exception:
            log.exception("falha difundindo status")
        stop.wait(BROADCAST_EVERY)


def _hora_config():
    """(hora, minuto) de `auto_speed_hora`, ou None se estiver invalida."""
    txt = (db.get_config().get("auto_speed_hora") or "").strip()
    try:
        h, _, m = txt.partition(":")
        h, m = int(h), int(m or 0)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return (h, m)
    return None


def agendador_velocidade(app):
    """Teste de velocidade automatico, uma vez por dia, nos links de internet.

    Roda de madrugada porque o teste satura o link de proposito: de dia ele
    atrapalharia o uso real da casa e ainda dispararia o alerta de latencia.
    Os links vao UM DE CADA VEZ -- medir os dois juntos disputaria a mesma CPU
    do Orange Pi e as duas medidas sairiam menores que a verdade.

    Guarda a ultima data executada no banco, e nao na memoria: se o serviço
    reiniciar as 4h05, nao pode disparar o teste de novo.
    """
    stop = app["stop"]
    stop.wait(90)                     # deixa as sondas estabilizarem antes
    while not stop.is_set():
        try:
            cfg = db.get_config()
            if cfg.get("auto_speed_enabled") == "1":
                alvo = _hora_config()
                agora = time.localtime()
                hoje = time.strftime("%Y-%m-%d", agora)
                if alvo and db.get_meta("auto_speed_ultima") != hoje:
                    # so dispara DEPOIS do horario, e nao antes: se o aparelho
                    # estava desligado as 4h, o teste sai quando ele voltar
                    if (agora.tm_hour, agora.tm_min) >= alvo:
                        _rodar_auto(app, cfg, hoje)
        except Exception:
            log.exception("falha no agendador do teste de velocidade")
        stop.wait(60)


def _rodar_auto(app, cfg, hoje):
    try:
        dur = float(cfg.get("auto_speed_dur") or 5)
    except (TypeError, ValueError):
        dur = 5.0
    links = [p.name_link for p in app["probes"] if p.kind == "internet"]
    # marca a data ANTES de rodar: se um teste falhar, nao queremos que o
    # agendador insista a cada minuto ate a meia-noite
    db.set_meta("auto_speed_ultima", hoje)
    log.info("teste de velocidade automatico do dia: %s", ", ".join(links))
    for nome in links:
        if app["stop"].is_set():
            return
        try:
            speedtest_mod.executar(app, nome, dur, origem="auto")
        except Exception as exc:
            log.warning("teste automatico de %s falhou: %s", nome, exc)
        app["stop"].wait(10)          # folga entre os dois links


def maintenance(app):
    """Rollups, retencao e checkpoint do WAL."""
    stop = app["stop"]
    last_hour = 0
    last_purge = 0
    stop.wait(20)
    while not stop.is_set():
        try:
            reconciliar_ao_vivo(app)
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


def reconciliar_placas():
    """Reaponta cada link para a placa onde o gateway dele esta agora."""
    try:
        links = db.list_links()
        achados = probe_mod.detectar_por_gateway(links)
    except Exception:
        log.exception("falha detectando as placas dos links")
        return
    for l in links:
        nova = achados.get(l["name"])
        if nova and nova != l["iface"]:
            log.warning("%s mudou de placa enquanto o servico estava parado: "
                        "%s -> %s (reconhecido pelo gateway)",
                        l["name"], l["iface"], nova)
            db.set_link(l["id"], iface=nova)


def reconciliar_ao_vivo(app):
    """Mesma reconciliacao, com o servico no ar e sem reiniciar nada.

    So age quando ha sinal de que a placa saiu do lugar: interface sumiu do
    sistema, ficou sem IP ou perdeu o gateway. Enquanto tudo estiver medindo,
    nao mexe -- reapontar um link saudavel seria criar um buraco sem motivo.
    """
    suspeitos = []
    for p in app["probes"]:
        if (not p.iface_up or not p.ip
                or not os.path.exists("/sys/class/net/" + p.iface)):
            suspeitos.append(p)
            continue
        # Placa saudavel tambem pode estar no link errado: numa troca CRUZADA de
        # cabos as duas seguem no ar, com IP e com gateway -- nada parece
        # suspeito, e os rotulos ficam invertidos em silencio, atribuindo as
        # medicoes a operadora errada. O sinal certo e o gateway: se a placa
        # deste link passou a ver um gateway diferente do que este link conhece,
        # o cabo mudou de porta.
        if p.kind == "lan":
            continue
        esperado = probe_mod.gw_conhecido(p.name_link)
        if esperado and p.gateway and p.gateway != esperado:
            log.warning("%s: a placa %s ve o gateway %s, mas este link e do %s",
                        p.name_link, p.iface, p.gateway, esperado)
            suspeitos.append(p)
    if not suspeitos:
        return
    achados = probe_mod.detectar_por_gateway(db.list_links())
    mapa = {l["name"]: l["id"] for l in db.list_links()}
    for p in suspeitos:
        nova = achados.get(p.name_link)
        if not nova or nova == p.iface:
            continue
        log.warning("%s: cabo trocado de porta (%s -> %s), reapontando ao vivo",
                    p.name_link, p.iface, nova)
        db.set_link(mapa[p.name_link], iface=nova)
        p.trocar_iface(nova, p.target)


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
        "started": time.time(), "port": None, "dns_lan": None,
        "mesh": None,
    }

    # Cabo trocado de porta enquanto o servico estava parado: o link continua
    # sendo o mesmo, mas o nome da placa mudou. Reencontramos cada um pelo
    # gateway antes de subir as sondas, senao o painel voltaria do boot medindo
    # a GIGA e chamando de IMPACTO.
    reconciliar_placas()

    probes = []
    for r in db.list_links():
        probes.append(probe_mod.LinkProbe(r["id"], r["name"], r["iface"],
                                          writer, alerts, stop,
                                          kind=r["kind"], target=r["target"]))
    app["probes"] = probes
    alerts.probes = probes

    # vigia do resolvedor da casa; depois das sondas, porque descobre o
    # endereco a partir do link de LAN
    dns_lan = probe_mod.SondaDnsLan(app, stop, alerts)
    app["dns_lan"] = dns_lan

    # o CLI do nordvpn custa segundos por chamada: le em segundo plano
    app["mesh"] = mesh_mod.SondaMesh(stop)

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
    dns_lan.start()
    app["mesh"].start()
    threading.Thread(target=broadcaster, args=(app,), name="broadcast",
                     daemon=True).start()
    threading.Thread(target=maintenance, args=(app,), name="manutencao",
                     daemon=True).start()
    threading.Thread(target=agendador_velocidade, args=(app,),
                     name="agenda-velocidade", daemon=True).start()

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
