"""Teste de velocidade sob demanda: download e upload reais de cada link.

Mesma regra das sondas: o soquete e preso a interface (SO_BINDTODEVICE), entao
o numero medido e do link escolhido e nao da rota default. Ate o nome do
servidor e resolvido por uma consulta DNS presa a interface — senao, testar a
IMPACTO com a GIGA caida falharia na resolucao, que sai pelo resolvedor local.

Sem dependencias externas: socket + ssl da biblioteca padrao. Medido aqui na
bancada, o Python aguenta ~450 Mbps neste aparelho, bem acima do link.
"""

import logging
import random
import socket
import ssl
import struct
import threading
import time

import db

log = logging.getLogger("netmon.speed")

SO_BINDTODEVICE = 25

PORT = 443
HOST_UP = "speed.cloudflare.com"
UP_PATH = "/__up"

# Fontes de download, na ordem. A Cloudflare responde 429 quando alguem testa
# muitas vezes seguidas do mesmo IP; nesse caso a CacheFly assume, para o botao
# nao virar um botao de erro. Ambas tem ponto de presenca no Brasil.
FONTES_DOWN = [
    ("speed.cloudflare.com", "/__down?bytes=50000000"),
    ("cachefly.cachefly.net", "/100mb.test"),
]
DNS_SERVER = "9.9.9.9"          # mesmo alvo das sondas: nunca o Pi-hole local

DUR_PADRAO = 5.0                # segundos medidos por direcao
DUR_MIN, DUR_MAX = 2.0, 15.0
AQUECIMENTO = 0.8               # descartado: TCP em slow start ainda mente
# nas reconexoes o aquecimento e menor de proposito: num link rapido cada
# conexao entrega os 50 MB em ~1 s e um aquecimento cheio custaria mais tempo
# de relogio do que a propria medicao
AQUECIMENTO_RECONEXAO = 0.25
CHUNK = 64 * 1024
# O servidor recusa (403) pedidos de 100 MB ou mais, entao cada conexao baixa
# no maximo 50 MB e o laco reconecta ate fechar o tempo pedido.
PEDIDO_BYTES = 50_000_000
# Tetos de volume por direcao. Alem de nao torrar a franquia de ninguem, sao
# eles que seguram o 429 do servidor de teste: cada conexao de download leva no
# maximo 50 MB, entao um teste da GIGA cabe em ~3 conexoes em vez de 8.
TETO_DOWN = 150_000_000
TETO_UP = 250_000_000
TIMEOUT = 10.0
PROGRESSO_A_CADA = 0.4          # segundos entre avisos de progresso


# ---------------------------------------------------------------------------
# Resolucao de nome presa a interface
# ---------------------------------------------------------------------------
def resolver(iface, nome, timeout=3.0):
    """Consulta A crua por UDP, presa a interface. Devolve IP ou None."""
    tid = random.getrandbits(16)
    pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in nome.split("."):
        pkt += bytes([len(label)]) + label.encode()
    pkt += b"\x00" + struct.pack(">HH", 1, 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        sock.settimeout(timeout)
        sock.sendto(pkt, (DNS_SERVER, 53))
        fim = time.monotonic() + timeout
        while time.monotonic() < fim:
            data, _ = sock.recvfrom(2048)
            if len(data) < 12 or struct.unpack(">H", data[:2])[0] != tid:
                continue
            return _primeiro_a(data)
        return None
    except OSError as exc:
        log.debug("DNS por %s falhou: %s", iface, exc)
        return None
    finally:
        sock.close()


def _pular_nome(data, pos):
    """Anda por um nome DNS, seja ele literal ou ponteiro comprimido."""
    while pos < len(data):
        n = data[pos]
        if n == 0:
            return pos + 1
        if n & 0xC0 == 0xC0:          # ponteiro: 2 bytes e acabou
            return pos + 2
        pos += 1 + n
    return pos


def _primeiro_a(data):
    qd, an = struct.unpack(">HH", data[4:8])
    pos = 12
    for _ in range(qd):
        pos = _pular_nome(data, pos) + 4
    for _ in range(an):
        pos = _pular_nome(data, pos)
        if pos + 10 > len(data):
            return None
        tipo, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if tipo == 1 and rdlen == 4:
            return socket.inet_ntoa(data[pos:pos + 4])
        pos += rdlen
    return None


def _endereco(iface, host):
    ip = resolver(iface, host)
    if ip:
        return ip
    # ultimo recurso: resolvedor do sistema (sai pela rota default)
    try:
        return socket.getaddrinfo(host, PORT, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError as exc:
        raise RuntimeError("nao consegui resolver %s pelo link (%s)" % (host, exc))


def _abrir(iface, ip, host):
    ctx = ssl.create_default_context()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        raw.settimeout(TIMEOUT)
        raw.connect((ip, PORT))
    except OSError:
        raw.close()
        raise
    return ctx.wrap_socket(raw, server_hostname=host)


# ---------------------------------------------------------------------------
# Medicoes
# ---------------------------------------------------------------------------
def _cabecalho_fim(buf):
    i = buf.find(b"\r\n\r\n")
    return -1 if i < 0 else i + 4


def medir_download(iface, dur, progresso=None):
    """Tenta as fontes em ordem; devolve (bytes, segundos, host que respondeu)."""
    erros = []
    for host, caminho in FONTES_DOWN:
        try:
            ip = _endereco(iface, host)
            b, t = _baixar(iface, ip, host, caminho, dur, progresso)
            return b, t, host
        except Exception as exc:
            erros.append("%s: %s" % (host, exc))
            log.warning("fonte de download %s falhou: %s", host, exc)
    raise RuntimeError(" · ".join(erros))


def _baixar(iface, ip, host, caminho, dur, progresso=None):
    """Devolve (bytes, segundos_medidos). Reconecta se o servidor cortar antes."""
    bytes_tot = 0.0
    tempo_tot = 0.0
    restante = dur
    aviso = 0.0
    primeira = True
    # teto de relogio: reconexao tem custo, e o botao nao pode ficar preso
    fim_geral = time.monotonic() + dur * 3 + 6
    while restante > 0.05 and bytes_tot < TETO_DOWN and time.monotonic() < fim_geral:
        sock = _abrir(iface, ip, host)
        try:
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
                   "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
                   % (caminho, host))
            sock.sendall(req.encode())

            cab = b""
            while _cabecalho_fim(cab) < 0:
                pedaco = sock.recv(4096)
                if not pedaco:
                    raise RuntimeError("o servidor de teste fechou a conexao")
                cab += pedaco
            if b" 200" not in cab[:16]:
                status = cab[:cab.find(b"\r\n")].decode("ascii", "replace")
                if " 429" in status:
                    raise RuntimeError(
                        "o servidor de teste pediu para esperar (429): "
                        "muitos testes seguidos, tente de novo em alguns minutos")
                raise RuntimeError("servidor de teste respondeu %s" % status)

            t_ini = time.monotonic()
            t_medindo = t_ini + (AQUECIMENTO if primeira else AQUECIMENTO_RECONEXAO)
            primeira = False                      # antes disso e slow start
            fim = min(t_medindo + restante, fim_geral)
            n = 0
            while True:
                agora = time.monotonic()
                if agora >= fim:
                    break
                pedaco = sock.recv(CHUNK)
                if not pedaco:
                    break                          # acabou o arquivo: reconecta
                if agora >= t_medindo:
                    n += len(pedaco)
                    if bytes_tot + n >= TETO_DOWN:
                        break              # teto de volume: nao torra a franquia
                if progresso and agora - aviso >= PROGRESSO_A_CADA:
                    aviso = agora
                    decorrido = max(0.001, agora - t_medindo)
                    progresso(min(1.0, (dur - restante + decorrido) / dur),
                              n * 8 / decorrido / 1e6 if decorrido > 0.2 else None)
            agora = time.monotonic()
            medido = max(0.0, agora - max(t_medindo, t_ini))
            bytes_tot += n
            tempo_tot += medido
            restante -= medido
        finally:
            try:
                sock.close()
            except OSError:
                pass
    if tempo_tot <= 0:
        raise RuntimeError("nao deu tempo de medir o download")
    return bytes_tot, tempo_tot


def medir_upload(iface, dur, progresso=None):
    """Envia em chunked encoding pelo tempo pedido e devolve (bytes, segundos)."""
    sock = _abrir(iface, _endereco(iface, HOST_UP), HOST_UP)
    try:
        cab = ("POST %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
               "Content-Type: application/octet-stream\r\n"
               "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
               % (UP_PATH, HOST_UP))
        sock.sendall(cab.encode())

        bloco = bytes(random.getrandbits(8) for _ in range(1024)) * (CHUNK // 1024)
        prefixo = b"%x\r\n" % len(bloco)
        t_ini = time.monotonic()
        t_medindo = t_ini + AQUECIMENTO
        fim = t_medindo + dur
        n = 0
        aviso = 0.0
        while True:
            agora = time.monotonic()
            if agora >= fim or n >= TETO_UP:
                break
            sock.sendall(prefixo + bloco + b"\r\n")
            if agora >= t_medindo:
                n += len(bloco)
            if progresso and agora - aviso >= PROGRESSO_A_CADA:
                aviso = agora
                decorrido = max(0.001, agora - t_medindo)
                progresso(min(1.0, max(0.0, decorrido) / dur),
                          n * 8 / decorrido / 1e6 if decorrido > 0.2 else None)
        medido = max(0.001, time.monotonic() - t_medindo)
        try:
            sock.sendall(b"0\r\n\r\n")
            sock.recv(256)
        except OSError:
            pass                     # a resposta nao muda a medicao
        return n, medido
    finally:
        try:
            sock.close()
        except OSError:
            pass


def medir_ping(iface):
    """Latencia e jitter de referencia, do mesmo jeito que a sonda mede."""
    import probe                      # tardio: probe importa db, nao a gente
    r = probe.ping(iface, probe.PING_TARGETS[0], count=5, interval=0.2, wait=2)
    return r.get("rtt_avg"), r.get("jitter")


# ---------------------------------------------------------------------------
# Execucao com estado global (um teste por vez no aparelho)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_estado = {"atual": None}


def em_andamento():
    a = _estado["atual"]
    return dict(a) if a else None


def _publicar(app, dados):
    try:
        app["bus"].publish("speedtest", dados)
    except Exception:
        log.exception("falha publicando progresso do teste de velocidade")


def _sonda(app, nome):
    for p in app["probes"]:
        if p.name_link == nome:
            return p
    return None


def executar(app, nome, dur=DUR_PADRAO, origem="manual"):
    """Roda o teste inteiro (ping, download, upload) e grava no banco.

    `origem` separa o que o usuario pediu no botao ("manual") do que o
    agendador disparou sozinho ("auto") -- os relatorios filtram por isso.
    """
    sonda = _sonda(app, nome)
    if sonda is None:
        raise RuntimeError("link desconhecido: %s" % nome)
    iface = sonda.iface
    dur = max(DUR_MIN, min(DUR_MAX, float(dur)))
    inicio = int(time.time())
    linha = {"link_id": sonda.link_id, "ts": inicio, "origem": origem}
    parcial = {"link": nome, "iface": iface, "ts": inicio, "dur": dur}

    def passo(fase, pct=0.0, mbps=None, **extra):
        parcial.update(extra)
        parcial["fase"] = fase
        parcial["pct"] = round(pct, 3)
        parcial["mbps"] = round(mbps, 1) if mbps else None
        _estado["atual"] = dict(parcial)
        _publicar(app, dict(parcial))

    # o teste satura o link de proposito: sem isso, a propria medicao dispararia
    # um alerta de latencia alta (e um webhook) a cada botao apertado
    sonda.silenciar_degradacao(dur * 2 + 40)
    log.info("teste de velocidade em %s (%s), %.0fs por direcao", nome, iface, dur)
    try:
        passo("preparando")
        passo("ping")
        ping_ms, jitter_ms = medir_ping(iface)
        linha["ping_ms"] = ping_ms
        linha["jitter_ms"] = jitter_ms
        passo("ping", 1.0, ping_ms=ping_ms, jitter_ms=jitter_ms)

        b, t, host = medir_download(
            iface, dur, lambda pct, mbps: passo("download", pct, mbps))
        down = b * 8 / t / 1e6
        linha.update(bytes_down=int(b), dur_down=round(t, 2),
                     down_mbps=round(down, 2), servidor=host)
        passo("download", 1.0, down, down_mbps=round(down, 2), servidor=host)

        # o upload pode falhar sozinho (so a Cloudflare aceita): nesse caso o
        # download ja medido continua valendo, com o aviso junto
        up = None
        try:
            b, t = medir_upload(iface, dur,
                                lambda pct, mbps: passo("upload", pct, mbps))
            up = b * 8 / t / 1e6
            linha.update(bytes_up=int(b), dur_up=round(t, 2), up_mbps=round(up, 2))
            passo("upload", 1.0, up, up_mbps=round(up, 2))
        except Exception as exc:
            linha["erro"] = ("upload falhou (%s); o download foi medido"
                             % (str(exc) or exc.__class__.__name__))[:200]
            log.warning("upload de %s falhou: %s", nome, exc)

        linha["id"] = db.save_speedtest(linha)
        log.info("teste de %s: %.1f Mbps down / %s Mbps up / ping %s ms (via %s)",
                 nome, down, "%.1f" % up if up else "-", ping_ms, host)
        resultado = dict(linha, link=nome, fase="fim", ok=True)
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        log.warning("teste de velocidade em %s falhou: %s", nome, msg)
        linha["erro"] = msg[:200]
        linha["id"] = db.save_speedtest(linha)
        resultado = dict(linha, link=nome, fase="erro", ok=False)
    finally:
        _estado["atual"] = None
    _publicar(app, resultado)
    return resultado


def iniciar(app, nome, dur=DUR_PADRAO, origem="manual"):
    """Dispara o teste numa thread. Levanta RuntimeError se ja houver um rodando."""
    if not _lock.acquire(blocking=False):
        atual = em_andamento() or {}
        raise RuntimeError("ja existe um teste rodando (%s)" % atual.get("link", "?"))

    def alvo():
        try:
            executar(app, nome, dur, origem)
        finally:
            _lock.release()

    try:
        threading.Thread(target=alvo, name="speedtest-%s" % nome, daemon=True).start()
    except Exception:
        _lock.release()
        raise
