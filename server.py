"""Servidor HTTP: API JSON, streaming SSE e arquivos estaticos. Sem dependencias."""

import json
import logging
import mimetypes
import os
import queue
import re
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import speedtest

log = logging.getLogger("netmon.server")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

PREFERRED_PORT = 666
FALLBACK_PORT = 8666
MAX_POINTS = 4000

AGG_TABLE = {"minute": "agg_minute", "hour": "agg_hour"}


def choose_res(span):
    if span <= 3 * 3600:
        return "raw"
    if span <= 48 * 3600:
        return "minute"
    return "hour"


def _stride(rows_estimate, bucket):
    if rows_estimate <= MAX_POINTS:
        return 1
    return max(1, int(rows_estimate / MAX_POINTS) + 1) * bucket


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "netmon"

    # -- utilidades -------------------------------------------------------
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, extra={"Cache-Control": "no-store"})

    def _err(self, code, msg):
        self._json({"erro": msg}, code)

    def _query(self):
        q = urllib.parse.urlsplit(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    @property
    def app(self):
        return self.server.app

    # -- roteamento -------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/status":
                return self.api_status()
            if path == "/api/samples":
                return self.api_samples()
            if path == "/api/events":
                return self.api_events()
            if path == "/api/summary":
                return self.api_summary()
            if path == "/api/speedtest":
                return self.api_speedtest_get()
            if path == "/api/speedtest.csv":
                return self.api_speedtest_csv()
            if path == "/api/config":
                return self._json(db.get_config())
            if path == "/api/links":
                return self.api_links_get()
            if path == "/api/ifaces":
                return self._json({"ifaces": listar_ifaces()})
            if path == "/api/health":
                return self._json({
                    "ok": True, "pid": os.getpid(),
                    "uptime_s": int(time.time() - self.app["started"]),
                    "clientes_sse": self.app["bus"].count(),
                })
            if path == "/api/stream":
                return self.api_stream()
            if path == "/api/report.pdf":
                return self.api_report()
            if path == "/api/logs":
                return self.api_logs()
            return self.serve_static(path)
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("erro tratando %s", path)
            try:
                self._err(500, "erro interno")
            except OSError:
                pass

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._err(400, "JSON invalido")

            if path == "/api/config":
                return self.api_config_post(data)
            if path == "/api/links":
                return self.api_links_post(data)
            if path == "/api/reset":
                return self.api_reset(data)
            if path == "/api/speedtest":
                return self.api_speedtest_post(data)
            if path == "/api/webhook/test":
                url = data.get("webhook_url") or db.get_config().get("webhook_url")
                if not url:
                    return self._err(400, "informe a URL do webhook")
                return self._json(self.app["alerts"].send_test(url))
            return self._err(404, "rota nao encontrada")
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("erro tratando POST %s", path)
            try:
                self._err(500, "erro interno")
            except OSError:
                pass

    # -- endpoints --------------------------------------------------------
    def api_status(self):
        probes = self.app["probes"]
        now = int(time.time())
        links = []
        for p in probes:
            snap = p.snapshot()
            snap["uptime"] = uptime_periodos(p.link_id, now)
            snap["ultima_queda"] = ultima_queda(p.link_id)
            snap["evento_aberto"] = evento_aberto(p.link_id)
            links.append(snap)
        self._json({
            "ts": now,
            "porta": self.app["port"],
            "port_fallback": self.app["port"] != PREFERRED_PORT,
            "servidor_uptime_s": now - int(self.app["started"]),
            "links": links,
        })

    def api_samples(self):
        q = self._query()
        name = (q.get("link") or "").upper()
        mapa = db.link_ids()
        link_id = mapa.get(name)
        if not link_id:
            return self._err(400, "parametro link deve ser um de: %s"
                             % ", ".join(sorted(mapa)))
        now = int(time.time())
        try:
            to = int(q.get("to") or now)
            frm = int(q.get("from") or (to - 3600))
        except ValueError:
            return self._err(400, "from/to devem ser epoch em segundos")
        if frm >= to:
            return self._err(400, "intervalo invalido")
        res = q.get("res") or "auto"
        if res == "auto":
            res = choose_res(to - frm)

        conn = db.connect(readonly=True)
        try:
            if res == "raw":
                rows = conn.execute(
                    "SELECT ts,rtt_avg,rtt_min,rtt_max,loss,jitter,state FROM samples "
                    "WHERE link_id=? AND ts>=? AND ts<=? ORDER BY ts", (link_id, frm, to)
                ).fetchall()
                pts = [[r["ts"], r["rtt_avg"], r["rtt_min"], r["rtt_max"],
                        r["loss"], r["jitter"]] for r in rows]
                # o commit e em lote (30s): sem juntar a ponta que ainda esta na
                # fila, a janela de 30s do painel viria quase vazia
                for s in db.amostras_recentes(link_id, frm, to,
                                              pts[-1][0] if pts else None):
                    pts.append([s["ts"], s.get("rtt_avg"), s.get("rtt_min"),
                                s.get("rtt_max"), s["loss"], s.get("jitter")])
            else:
                table = AGG_TABLE[res]
                bucket = 60 if res == "minute" else 3600
                step = _stride((to - frm) / bucket, bucket)
                rows = conn.execute(
                    "SELECT ts,rtt_avg,rtt_min,rtt_max,loss_avg,jitter_avg,"
                    "n_samples,n_up,n_degraded,n_down FROM %s "
                    "WHERE link_id=? AND ts>=? AND ts<=? AND (ts %% ?)=0 ORDER BY ts"
                    % table, (link_id, frm, to, step)
                ).fetchall()
                pts = [[r["ts"], r["rtt_avg"], r["rtt_min"], r["rtt_max"],
                        r["loss_avg"], r["jitter_avg"], r["n_samples"],
                        r["n_up"], r["n_degraded"], r["n_down"]] for r in rows]
                pts += _cauda_agregada(conn, link_id, bucket, step,
                                       pts[-1][0] if pts else frm - 1, to)
        finally:
            conn.close()
        self._json({"link": name, "res": res, "from": frm, "to": to,
                    "campos": ["ts", "rtt_avg", "rtt_min", "rtt_max", "loss", "jitter"],
                    "points": pts})

    def api_events(self):
        q = self._query()
        where, params = ["1=1"], []
        name = (q.get("link") or "").upper()
        mapa = db.link_ids()
        if name in mapa:
            where.append("e.link_id=?")
            params.append(mapa[name])
        if q.get("tipo"):
            where.append("e.type=?")
            params.append(q["tipo"])
        for key, op in (("from", ">="), ("to", "<=")):
            if q.get(key):
                try:
                    where.append("e.started_at %s ?" % op)
                    params.append(int(q[key]))
                except ValueError:
                    return self._err(400, "from/to devem ser epoch")
        try:
            limit = min(500, max(1, int(q.get("limit") or 100)))
            offset = max(0, int(q.get("offset") or 0))
        except ValueError:
            return self._err(400, "limit/offset invalidos")

        clause = " AND ".join(where)
        conn = db.connect(readonly=True)
        try:
            total = conn.execute(
                "SELECT COUNT(*) c FROM events e WHERE " + clause, params).fetchone()["c"]
            rows = conn.execute(
                "SELECT e.*, l.name link FROM events e JOIN links l ON l.id=e.link_id "
                "WHERE " + clause + " ORDER BY e.started_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            eventos = []
            for r in rows:
                d = dict(r)
                if d.get("details"):
                    try:
                        d["details"] = json.loads(d["details"])
                    except ValueError:
                        pass
                d.pop("link_id", None)
                eventos.append(d)
        finally:
            conn.close()
        self._json({"total": total, "limit": limit, "offset": offset, "events": eventos})

    def api_summary(self):
        q = self._query()
        now = int(time.time())
        period = q.get("period") or "24h"
        spans = {"1h": 3600, "6h": 21600, "24h": 86400,
                 "7d": 604800, "30d": 2592000}
        if period == "custom":
            try:
                frm, to = int(q["from"]), int(q["to"])
            except (KeyError, ValueError):
                return self._err(400, "period=custom exige from e to")
        else:
            if period not in spans:
                return self._err(400, "period invalido")
            to, frm = now, now - spans[period]

        out = {}
        for l in db.list_links():
            out[l["name"]] = resumo_link(l["id"], frm, to)
        self._json({"period": period, "from": frm, "to": to, "links": out})

    def api_config_post(self, data):
        permitido = {"webhook_url", "webhook_enabled", "lat_limiar_ms",
                     "loss_limiar_pct", "jitter_limiar_ms", "som_habilitado",
                     "cooldown_s"}
        pairs = {}
        for k, v in data.items():
            if k not in permitido:
                continue
            if k == "webhook_url" and v:
                u = urllib.parse.urlsplit(str(v))
                if u.scheme not in ("http", "https") or not u.hostname:
                    return self._err(400, "URL do webhook deve comecar com http:// ou https://")
            if k in ("lat_limiar_ms", "loss_limiar_pct", "jitter_limiar_ms", "cooldown_s"):
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    return self._err(400, "%s deve ser numerico" % k)
                if n <= 0:
                    return self._err(400, "%s deve ser maior que zero" % k)
                v = n
            if k in ("webhook_enabled", "som_habilitado"):
                v = "1" if str(v) in ("1", "true", "True", "on") else "0"
            pairs[k] = v
        if not pairs:
            return self._err(400, "nada para atualizar")
        self._json(db.set_config(pairs))

    # -- teste de velocidade ----------------------------------------------
    def api_speedtest_get(self):
        q = self._query()
        name = (q.get("link") or "").upper()
        link_id = db.link_ids().get(name)
        try:
            limit = min(500, max(1, int(q.get("limit") or 20)))
        except ValueError:
            return self._err(400, "limit invalido")
        self._json({
            "rodando": speedtest.em_andamento(),
            "ultimos": db.last_speedtests(),
            "historico": db.list_speedtests(link_id, limit),
        })

    def api_speedtest_post(self, data):
        name = str(data.get("link") or "").upper()
        # so links de internet: medir a velocidade ate o proprio roteador
        # mediria o cabo de casa, nao a contratacao
        aceitos = [p.name_link for p in self.app["probes"] if p.kind == "internet"]
        if name not in aceitos:
            return self._err(400, "informe link=%s" % " ou link=".join(aceitos))
        try:
            dur = float(data.get("dur") or speedtest.DUR_PADRAO)
        except (TypeError, ValueError):
            return self._err(400, "dur deve ser numerico")
        try:
            speedtest.iniciar(self.app, name, dur)
        except RuntimeError as exc:
            return self._err(409, str(exc))
        self._json({"ok": True, "link": name, "dur": dur,
                    "mensagem": "teste iniciado; acompanhe pelo /api/stream"}, 202)

    # -- log dos testes de velocidade -------------------------------------
    def api_speedtest_csv(self):
        """O historico inteiro em CSV, para abrir no LibreOffice/Excel."""
        q = self._query()
        link_id = db.link_ids().get((q.get("link") or "").upper())
        linhas = ["quando;link;download_mbps;upload_mbps;ping_ms;jitter_ms;"
                  "bytes_baixados;bytes_enviados;seg_download;seg_upload;servidor;erro"]
        import alerts as _al
        for t in db.list_speedtests(link_id, 5000):
            linhas.append(";".join(str(x) for x in [
                _al.fmt_iso(t["ts"]), t["link"],
                _num(t["down_mbps"]), _num(t["up_mbps"]),
                _num(t["ping_ms"]), _num(t["jitter_ms"]),
                t["bytes_down"] or "", t["bytes_up"] or "",
                _num(t["dur_down"]), _num(t["dur_up"]),
                t["servidor"] or "", (t["erro"] or "").replace(";", ","),
            ]))
        # BOM: sem ele o Excel abre "GIGA" certo mas estraga os acentos do erro
        body = ("\ufeff" + "\r\n".join(linhas) + "\r\n").encode("utf-8")
        nome = "testes-velocidade-%s.csv" % _al.datetime.fromtimestamp(
            time.time(), _al.TZ).strftime("%Y%m%d-%H%M")
        self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": 'attachment; filename="%s"' % nome,
            "Cache-Control": "no-store",
        })

    # -- interfaces e links -----------------------------------------------
    def api_links_get(self):
        emuso = {}
        for p in self.app["probes"]:
            emuso[p.name_link] = p.iface
        links = []
        for l in db.list_links(apenas_ativos=False):
            l["iface_ativa"] = emuso.get(l["name"], l["iface"])
            links.append(l)
        self._json({"links": links, "ifaces": listar_ifaces()})

    def api_links_post(self, data):
        """Troca a placa de rede (e o alvo, no link de LAN) de um link.

        Vale ao vivo: a sonda passa a usar a interface nova no ciclo seguinte,
        sem reiniciar o servico.
        """
        atuais = {l["name"]: l for l in db.list_links(apenas_ativos=False)}
        pedidos = data.get("links")
        if not isinstance(pedidos, dict) or not pedidos:
            return self._err(400, "envie {\"links\": {\"GIGA\": {\"iface\": \"eth0\"}}}")

        disponiveis = {i["iface"] for i in listar_ifaces()}
        mudancas = {}
        for nome, cfg in pedidos.items():
            nome = str(nome).upper()
            if nome not in atuais:
                return self._err(400, "link desconhecido: %s" % nome)
            if not isinstance(cfg, dict):
                return self._err(400, "configuracao invalida para %s" % nome)
            iface = str(cfg.get("iface") or atuais[nome]["iface"]).strip()
            if not RE_IFACE.match(iface):
                return self._err(400, "nome de interface invalido: %s" % iface)
            # avisar, nao impedir: a placa pode estar desconectada agora e voltar
            # depois, e travar a escolha deixaria o usuario sem saida
            alvo = cfg.get("target")
            if alvo is not None:
                alvo = str(alvo).strip()
                if alvo and not RE_IPV4.match(alvo):
                    return self._err(400, "alvo invalido para %s: %s" % (nome, alvo))
                if atuais[nome]["kind"] != "lan":
                    alvo = None          # so o link de LAN tem alvo escolhivel
            mudancas[nome] = (atuais[nome]["id"], iface, alvo)

        usadas = {}
        for nome, l in atuais.items():
            iface = mudancas[nome][1] if nome in mudancas else l["iface"]
            usadas.setdefault(iface, []).append(nome)
        repetidas = [i for i, ns in usadas.items() if len(ns) > 1]
        if repetidas:
            return self._err(400, "a interface %s ficaria em dois links (%s) - cada "
                             "link precisa da sua propria placa"
                             % (repetidas[0], " e ".join(usadas[repetidas[0]])))

        for nome, (lid, iface, alvo) in mudancas.items():
            db.set_link(lid, iface=iface, target=alvo)
            for p in self.app["probes"]:
                if p.name_link == nome:
                    p.trocar_iface(iface, alvo)
        log.warning("interfaces atualizadas pela interface web: %s",
                    ", ".join("%s=%s" % (n, v[1]) for n, v in mudancas.items()))
        fora = [i for _, i, _ in mudancas.values() if i not in disponiveis]
        self._json({
            "ok": True,
            "aviso": ("a interface %s nao existe no sistema agora - o link fica sem "
                      "medicao ate ela aparecer" % fora[0]) if fora else None,
            "links": db.list_links(apenas_ativos=False),
        })

    # -- relatorio PDF ----------------------------------------------------
    def api_report(self):
        import report                      # tardio: report importa server
        q = self._query()
        period = q.get("period") or "24h"
        frm = to = None
        if period == "custom":
            try:
                frm, to = int(q["from"]), int(q["to"])
            except (KeyError, ValueError):
                return self._err(400, "period=custom exige from e to")
            if frm >= to:
                return self._err(400, "intervalo invalido")
        elif period not in report.PERIODOS:
            return self._err(400, "period invalido")
        link = (q.get("link") or "").upper() or None
        internet = report.links_do_relatorio()
        if link and link not in internet:
            return self._err(400, "link deve ser um de: %s" % ", ".join(sorted(internet)))
        try:
            blob = report.gerar(period, frm, to, link)
        except Exception:
            log.exception("falha gerando o relatorio PDF")
            return self._err(500, "falha gerando o relatorio")
        import alerts as _al
        nome = "%s-%s-%s.pdf" % (
            ("quedas-%s" % link.lower()) if link else "relatorio-netmon",
            period, _al.datetime.fromtimestamp(time.time(), _al.TZ).strftime("%Y%m%d-%H%M"))
        self._send(200, blob, "application/pdf", {
            "Content-Disposition": 'attachment; filename="%s"' % nome,
            "Cache-Control": "no-store",
        })

    # -- logs -------------------------------------------------------------
    def api_logs(self):
        partes = []
        import alerts as _al
        agora = _al.fmt_iso(time.time())
        partes.append("=" * 72)
        partes.append("netmon - pacote de diagnostico gerado em %s" % agora)
        partes.append("horarios em America/Sao_Paulo (UTC-3); o aparelho roda em UTC")
        partes.append("=" * 72)
        partes.append("")
        try:
            partes.append("--- ESTADO ATUAL ---")
            for p in self.app["probes"]:
                s = p.snapshot()
                partes.append(
                    "  %-9s %-8s %-8s iface=%s ip=%s gw=%s rtt=%s perda=%s%% gw_ok=%s"
                    % (s["name"], s["kind"], s["state"], s["iface"], s["ip"],
                       s["gateway"], s.get("rtt_avg"), s.get("loss"), s.get("gw_ok")))
                if s["kind"] == "internet":
                    partes.append("             IP externo: %s (visto em %s)"
                                  % (s.get("ip_externo") or "-",
                                     _al.fmt_iso(s["ip_externo_ts"])
                                     if s.get("ip_externo_ts") else "-"))
                elif s.get("target"):
                    partes.append("             alvo na LAN: %s" % s["target"])
            partes.append("")
            partes.append("--- PLACAS DE REDE ---")
            for i in listar_ifaces():
                partes.append("  %-18s ip=%-15s gw=%-15s link=%s cabo=%s %s"
                              % (i["iface"], i["ip"] or "-", i["gateway"] or "-",
                                 "sim" if i["up"] else "nao",
                                 {True: "sim", False: "nao"}.get(i["cabo"], "?"),
                                 "(USB)" if i["usb"] else ""))
            partes.append("")
            partes.append("--- ULTIMOS TESTES DE VELOCIDADE ---")
            for t in db.list_speedtests(None, 20):
                partes.append("  %s %-9s down=%s up=%s ping=%s via=%s %s"
                              % (_al.fmt_iso(t["ts"]), t["link"],
                                 t["down_mbps"], t["up_mbps"], t["ping_ms"],
                                 t["servidor"] or "-",
                                 ("ERRO: " + t["erro"]) if t["erro"] else ""))
            partes.append("")
            partes.append("--- CONFIGURACAO ---")
            for k, v in sorted(db.get_config().items()):
                if k == "webhook_url" and v:
                    v = v[:32] + "..."      # nao vazar token de webhook no arquivo
                partes.append("  %-18s %s" % (k, v))
            partes.append("")
            partes.append("--- ULTIMOS EVENTOS ---")
            conn = db.connect(readonly=True)
            try:
                for r in conn.execute(
                        "SELECT e.*, l.name n FROM events e JOIN links l ON l.id=e.link_id"
                        " ORDER BY e.started_at DESC LIMIT 50"):
                    partes.append("  %-8s %-14s inicio=%s fim=%s duracao=%ss causa=%s"
                                  % (r["n"], r["type"], r["started_at"],
                                     r["ended_at"], r["duration_s"], r["cause"]))
            finally:
                conn.close()
        except Exception as exc:
            partes.append("  (falha coletando diagnostico: %s)" % exc)

        partes.append("")
        partes.append("=" * 72)
        partes.append("--- netmon.log ---")
        partes.append("=" * 72)
        caminho = os.path.join(BASE_DIR, "netmon.log")
        try:
            with open(caminho, "r", encoding="utf-8", errors="replace") as fh:
                partes.append(fh.read())
        except OSError as exc:
            partes.append("(nao foi possivel ler %s: %s)" % (caminho, exc))

        body = "\n".join(partes).encode("utf-8")
        nome = "netmon-%s.log" % _al.datetime.fromtimestamp(
            time.time(), _al.TZ).strftime("%Y%m%d-%H%M")
        self._send(200, body, "text/plain; charset=utf-8", {
            "Content-Disposition": 'attachment; filename="%s"' % nome,
            "Cache-Control": "no-store",
        })

    # -- reset ------------------------------------------------------------
    def api_reset(self, data):
        if data.get("confirmar") != "APAGAR":
            return self._err(400, 'confirmacao ausente: envie {"confirmar":"APAGAR"}')
        escopo = data.get("escopo") or "historico"
        if escopo not in ("historico", "tudo"):
            return self._err(400, "escopo deve ser 'historico' ou 'tudo'")
        log.warning("RESET pedido pela interface (escopo=%s, de %s)",
                    escopo, self.address_string())
        db.reset(escopo)
        for p in self.app["probes"]:
            p.resetar()
        self._json({"ok": True, "escopo": escopo,
                    "mensagem": "Histórico apagado. A coleta recomeça agora."})

    def api_stream(self):
        bus = self.app["bus"]
        q = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            last_ping = time.time()
            vazio = object()
            while not self.app["stop"].is_set():
                try:
                    item = q.get(timeout=5)
                except queue.Empty:
                    item = vazio
                if item is None:
                    break            # o barramento despejou este cliente
                if item is vazio:
                    if time.time() - last_ping >= 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    continue
                kind, data = item
                payload = json.dumps(data, ensure_ascii=False)
                self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, payload)).encode("utf-8"))
                self.wfile.flush()
                last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype, {"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Interfaces de rede
# ---------------------------------------------------------------------------
RE_IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
RE_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Nao sao placas de rede de verdade e so poluiriam a lista de escolha.
IGNORAR = ("lo", "nordlynx", "docker", "br-", "veth", "tun", "tap", "wg")


def listar_ifaces():
    """Placas de rede do sistema, com IP e gateway, para o seletor da pagina."""
    import probe as probe_mod
    try:
        nomes = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []
    out = []
    for nome in nomes:
        if nome.startswith(IGNORAR):
            continue
        ip, gw, up = probe_mod.iface_info(nome)
        mac = velocidade = None
        for arquivo, destino in (("address", "mac"), ("speed", "vel")):
            try:
                with open("/sys/class/net/%s/%s" % (nome, arquivo)) as fh:
                    valor = fh.read().strip()
                if destino == "mac":
                    mac = valor
                elif valor and valor != "-1":
                    velocidade = int(valor)
            except (OSError, ValueError):
                pass
        try:
            with open("/sys/class/net/%s/carrier" % nome) as fh:
                cabo = fh.read().strip() == "1"
        except OSError:
            cabo = None
        out.append({"iface": nome, "ip": ip, "gateway": gw, "up": up,
                    "mac": mac, "mbps": velocidade, "cabo": cabo,
                    "usb": "usb" in os.path.realpath("/sys/class/net/" + nome)})
    return out


def _num(v):
    """Numero em formato brasileiro para o CSV (virgula decimal)."""
    if v is None:
        return ""
    return ("%.2f" % v).replace(".", ",")


# ---------------------------------------------------------------------------
# Consultas auxiliares
# ---------------------------------------------------------------------------
def _downtime(conn, link_id, frm, to, tipo="QUEDA"):
    """Soma da interseccao dos eventos com a janela; retorna (total, n, maior).

    Duas causas ficam de fora, porque em nenhuma delas a operadora tem culpa: a
    queda provocada pelo proprio teste de velocidade (fomos nos que enchemos o
    link) e o buraco de segundos ao trocar a placa de rede do link.
    """
    rows = conn.execute(
        "SELECT started_at, COALESCE(ended_at, ?) fim FROM events "
        "WHERE link_id=? AND type=? AND started_at<=? AND COALESCE(ended_at, ?)>=? "
        "AND COALESCE(cause,'') NOT IN ('teste_velocidade','troca_placa')",
        (int(time.time()), link_id, tipo, to, int(time.time()), frm)).fetchall()
    total = 0
    maior = 0
    n = 0
    for r in rows:
        ini = max(r["started_at"], frm)
        fim = min(r["fim"], to)
        d = max(0, fim - ini)
        total += d
        maior = max(maior, d)
        if frm <= r["started_at"] <= to:     # so conta quedas iniciadas na janela
            n += 1
    return total, n, maior


def _cauda_agregada(conn, link_id, bucket, step, depois_de, to):
    """Agrega na hora o que ainda nao virou rollup.

    O rollup por minuto roda a cada minuto e o por hora a cada hora -- sem esta
    cauda o grafico de 30 dias terminaria ate uma hora no passado, o que parece
    (com razao) um grafico quebrado.
    """
    rows = conn.execute(
        "SELECT (ts/?)*? b, AVG(rtt_avg) a, MIN(rtt_min) mn, MAX(rtt_max) mx,"
        " AVG(loss) l, AVG(jitter) j, COUNT(*) n, SUM(state='UP') up,"
        " SUM(state='DEGRADED') deg, SUM(state IN ('DOWN','NO_LINK')) down"
        " FROM samples WHERE link_id=? AND ts>? AND ts<=? GROUP BY b ORDER BY b",
        (bucket, bucket, link_id, depois_de, to)).fetchall()
    return [[r["b"], r["a"], r["mn"], r["mx"], r["l"], r["j"],
             r["n"], r["up"], r["deg"], r["down"]]
            for r in rows if r["b"] % step == 0]


def _agregar(amostras):
    """Mesmas colunas que a agregacao em SQL devolve, calculadas em Python.

    Existe porque a ponta recente das amostras vem da memoria, e nao da para
    somar isso dentro da consulta.
    """
    def media(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def minimo(vals):
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def maximo(vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    dns = [s.get("dns_ms") for s in amostras if (s.get("dns_ms") or 0) > 0]
    return {
        "n": len(amostras),
        "a": media([s.get("rtt_avg") for s in amostras]),
        "mn": minimo([s.get("rtt_min") for s in amostras]),
        "mx": maximo([s.get("rtt_max") for s in amostras]),
        "j": media([s.get("jitter") for s in amostras]),
        "l": media([s.get("loss") for s in amostras]),
        "d": media(dns),
    }


def resumo_link(link_id, frm, to):
    span = max(1, to - frm)
    res = choose_res(span)
    conn = db.connect(readonly=True)
    try:
        down_s, quedas, maior = _downtime(conn, link_id, frm, to)
        deg_s, n_deg, _ = _downtime(conn, link_id, frm, to, "LATENCIA_ALTA")

        if res == "raw":
            rows = conn.execute(
                "SELECT ts,rtt_avg,rtt_min,rtt_max,jitter,loss,dns_ms FROM samples"
                " WHERE link_id=? AND ts>=? AND ts<=? ORDER BY ts",
                (link_id, frm, to)).fetchall()
            dados = [dict(x) for x in rows]
            # mesma juncao do /api/samples: em janelas curtas quase tudo ainda
            # esta na fila do escritor
            dados += db.amostras_recentes(link_id, frm, to,
                                          dados[-1]["ts"] if dados else None)
            r = _agregar(dados)
            monitorado = (r["n"] or 0) * db.SAMPLE_INTERVAL
        else:
            table = AGG_TABLE[res]
            r = conn.execute(
                "SELECT SUM(n_samples) n,"
                " SUM(rtt_avg*n_samples)/NULLIF(SUM(CASE WHEN rtt_avg IS NOT NULL"
                "   THEN n_samples END),0) a,"
                " MIN(rtt_min) mn, MAX(rtt_max) mx, AVG(jitter_avg) j,"
                " SUM(loss_avg*n_samples)/NULLIF(SUM(n_samples),0) l, AVG(dns_avg) d"
                " FROM %s WHERE link_id=? AND ts>=? AND ts<=?" % table,
                (link_id, frm, to)).fetchone()
            monitorado = (r["n"] or 0) * db.SAMPLE_INTERVAL
    finally:
        conn.close()

    monitorado = min(monitorado, span)
    # sem amostras no periodo nao existe uptime: devolver 100% seria mentira
    uptime = None if monitorado <= 0 else round(
        max(0.0, 100.0 * (monitorado - min(down_s, monitorado)) / monitorado), 3)
    return {
        "uptime_pct": uptime,
        "downtime_s": down_s,
        "quedas": quedas,
        "maior_queda_s": maior,
        "degradacoes": n_deg,
        "degradado_s": deg_s,
        "monitorado_s": monitorado,
        "cobertura_pct": round(100.0 * monitorado / span, 1),
        "rtt_avg": round(r["a"], 2) if r["a"] is not None else None,
        "rtt_min": round(r["mn"], 2) if r["mn"] is not None else None,
        "rtt_max": round(r["mx"], 2) if r["mx"] is not None else None,
        "jitter_avg": round(r["j"], 2) if r["j"] is not None else None,
        "loss_avg": round(r["l"], 3) if r["l"] is not None else None,
        "dns_avg": round(r["d"], 2) if r["d"] is not None else None,
        "amostras": r["n"] or 0,
    }


def uptime_periodos(link_id, now):
    out = {}
    for chave, span in (("h24", 86400), ("d7", 604800), ("d30", 2592000)):
        out[chave] = resumo_link(link_id, now - span, now)["uptime_pct"]
    return out


def ultima_queda(link_id):
    conn = db.connect(readonly=True)
    try:
        r = conn.execute(
            "SELECT started_at, ended_at, duration_s, cause FROM events "
            "WHERE link_id=? AND type='QUEDA' AND ended_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1", (link_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def evento_aberto(link_id):
    conn = db.connect(readonly=True)
    try:
        r = conn.execute(
            "SELECT id, type, cause, started_at FROM events "
            "WHERE link_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (link_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def build(app):
    """Sobe na 666; se o kernel negar (porta privilegiada), cai para 8666."""
    last = None
    for port in (PREFERRED_PORT, FALLBACK_PORT):
        try:
            srv = Server(("0.0.0.0", port), Handler)
        except (PermissionError, OSError) as exc:
            last = exc
            log.warning("nao consegui abrir a porta %d: %s", port, exc)
            continue
        srv.app = app
        app["port"] = port
        log.info("servidor HTTP ouvindo em 0.0.0.0:%d", port)
        return srv
    raise SystemExit("nao foi possivel abrir nenhuma porta: %s" % last)
