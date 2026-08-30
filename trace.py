"""Traceroute sob demanda, escolhendo por qual link o pacote sai.

Este aparelho nao tem `traceroute` nem `mtr` instalados, e instalar exige root
(que aqui pede senha). Entao o caminho e tracado com o que o kernel ja oferece
sem privilegio nenhum:

  * um soquete ICMP de datagrama (SOCK_DGRAM/IPPROTO_ICMP), liberado para
    qualquer usuario por `net.ipv4.ping_group_range`;
  * `IP_TTL` para limitar quantos saltos o pacote anda;
  * `IP_RECVERR` + `MSG_ERRQUEUE` para ler o "tempo de vida excedido" que cada
    roteador devolve -- e dentro dele o endereco de quem respondeu.

Como todo o resto do netmon, o soquete e preso a interface do link
(SO_BINDTODEVICE): tracar a IMPACTO com a GIGA como rota default continua
saindo pela IMPACTO. E isso que torna o traceroute util aqui -- comparar o
caminho das duas operadoras ate o mesmo destino.

As tres consultas de cada salto sao disparadas ao mesmo tempo, em soquetes
separados: um salto que nao responde custa o timeout uma vez, nao tres.
"""

import json
import logging
import re
import select
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db

log = logging.getLogger("netmon.trace")

SO_BINDTODEVICE = 25
IP_RECVERR = 11

MAX_HOPS_PADRAO = 20
MAX_HOPS_TETO = 30
CONSULTAS = 3                 # sondas por salto, como no traceroute classico
TIMEOUT = 1.5                 # s de espera por salto
PAUSA_ENTRE_SALTOS = 0.05

RE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")

# tipos ICMP que interessam
TTL_EXCEDIDO = 11
INALCANCAVEL = 3
MOTIVO_INALCANCAVEL = {
    0: "rede inalcancavel", 1: "host inalcancavel", 2: "protocolo inalcancavel",
    3: "porta inalcancavel", 9: "rede proibida", 10: "host proibido",
    13: "bloqueado por filtro",
}


def _checksum(dados):
    if len(dados) % 2:
        dados += b"\0"
    total = sum(struct.unpack("!%dH" % (len(dados) // 2), dados))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _pacote(seq):
    """Echo request. O kernel reescreve o identificador; a sequencia e nossa."""
    corpo = b"netmon-traceroute"
    cab = struct.pack("!BBHHH", 8, 0, 0, 0, seq & 0xFFFF)
    return cab[:2] + struct.pack("!H", _checksum(cab + corpo)) + cab[4:] + corpo


def _abrir(iface, ttl):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        sock.setsockopt(socket.SOL_IP, IP_RECVERR, 1)
        sock.setsockopt(socket.SOL_IP, socket.IP_TTL, ttl)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


def _ler_erro(sock):
    """Le a fila de erros do soquete. Devolve (ip_de_quem_respondeu, tipo, codigo)."""
    try:
        _dados, anc, _flags, _addr = sock.recvmsg(512, 1024, socket.MSG_ERRQUEUE)
    except (BlockingIOError, InterruptedError):
        return None
    except OSError:
        return None
    for nivel, tipo, blob in anc:
        if nivel != socket.SOL_IP or tipo != IP_RECVERR or len(blob) < 32:
            continue
        # struct sock_extended_err: errno, origin, type, code, pad, info, data
        _errno, _origem, ee_type, ee_code = struct.unpack("!IBBB", blob[:7])[0:4]
        # o endereco de quem gerou o erro vem logo depois, como sockaddr_in
        ofensor = blob[16:32]
        ip = socket.inet_ntoa(ofensor[4:8])
        return ip, ee_type, ee_code
    return None


def _um_salto(iface, destino_ip, ttl, seq0, consultas, timeout):
    """Dispara N sondas simultaneas com o mesmo TTL. Devolve lista de respostas."""
    socks, inicio = [], {}
    try:
        for i in range(consultas):
            try:
                s = _abrir(iface, ttl)
            except OSError as exc:
                raise RuntimeError("nao consegui abrir o soquete em %s (%s)"
                                   % (iface, exc))
            socks.append(s)
            try:
                inicio[s] = time.monotonic()
                s.sendto(_pacote(seq0 + i), (destino_ip, 0))
            except OSError as exc:
                log.debug("envio do salto %d falhou: %s", ttl, exc)

        respostas = [None] * consultas
        pendentes = set(socks)
        fim = time.monotonic() + timeout
        while pendentes:
            restante = fim - time.monotonic()
            if restante <= 0:
                break
            prontos, _, com_erro = select.select(list(pendentes), [],
                                                 list(pendentes), restante)
            for s in set(prontos) | set(com_erro):
                if s not in pendentes:
                    continue
                idx = socks.index(s)
                ms = round((time.monotonic() - inicio[s]) * 1000, 2)
                achado = _ler_erro(s)
                if achado:
                    ip, ee_type, ee_code = achado
                    if ee_type == TTL_EXCEDIDO:
                        respostas[idx] = {"ip": ip, "ms": ms, "fim": False}
                    elif ee_type == INALCANCAVEL:
                        respostas[idx] = {
                            "ip": ip, "ms": ms, "fim": True,
                            "aviso": MOTIVO_INALCANCAVEL.get(ee_code, "inalcancavel"),
                        }
                    else:
                        respostas[idx] = {"ip": ip, "ms": ms, "fim": False}
                    pendentes.discard(s)
                    continue
                try:                       # chegou ao destino: echo reply normal
                    _dados, addr = s.recvfrom(1500)
                    respostas[idx] = {"ip": addr[0], "ms": ms, "fim": True}
                    pendentes.discard(s)
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    pendentes.discard(s)
        return respostas
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Nome reverso: bonito de ver, mas nunca pode segurar o traceroute
# ---------------------------------------------------------------------------
def _reverso(ip, timeout=1.2):
    """PTR do salto. Roda numa thread propria: gethostbyaddr nao aceita prazo."""
    caixa = {}

    def tarefa():
        try:
            caixa["nome"] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            pass

    t = threading.Thread(target=tarefa, daemon=True)
    t.start()
    t.join(timeout)
    return caixa.get("nome")


# ---------------------------------------------------------------------------
# Execucao: um traceroute por vez no aparelho
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_estado = {"atual": None}


def em_andamento():
    a = _estado["atual"]
    return dict(a) if a else None


def ultimo(nome_link):
    """O ultimo traceroute daquele link, para a pagina abrir ja preenchida."""
    d = db.get_meta("traceroute:%s" % nome_link)
    return d if isinstance(d, dict) else None


def validar_destino(texto):
    texto = (texto or "").strip().rstrip(".")
    if not texto or len(texto) > 253:
        raise ValueError("informe um endereco ou nome de destino")
    if not RE_HOST.match(texto):
        raise ValueError("destino invalido: use um IP ou um nome como google.com")
    return texto


def _resolver(iface, destino):
    """Resolve o nome PELO link escolhido, igual ao teste de velocidade faz.

    Se saisse pelo resolvedor do sistema, tracar a IMPACTO com a GIGA caida
    falharia ja na resolucao -- justamente na hora em que o traceroute serve
    para alguma coisa.
    """
    try:
        socket.inet_aton(destino)
        return destino
    except OSError:
        pass
    import speedtest                   # tardio: speedtest importa db, nao a gente
    ip = speedtest.resolver(iface, destino)
    if ip:
        return ip
    try:
        return socket.getaddrinfo(destino, None, socket.AF_INET,
                                  socket.SOCK_STREAM)[0][4][0]
    except OSError:
        raise RuntimeError("nao consegui resolver %s pelo link %s"
                           % (destino, iface))


# ---------------------------------------------------------------------------
# Pais de cada salto
# ---------------------------------------------------------------------------
# Serve para enxergar onde o trafego sai do Brasil: um salto que ja aparece nos
# EUA no 3o pulo explica latencia que nenhuma metrica de link explicaria.
#
# A consulta e feita UMA vez por IP e guardada no banco para sempre -- os saltos
# se repetem a cada traceroute, e a API gratuita limita 15 requisicoes por
# minuto. Sai pela mesma placa do traceroute: consultar pela rota default
# enquanto se traca a IMPACTO com a GIGA caida falharia sem motivo.
GEO_HOST = "ipwho.is"            # gratuito, sem chave, por HTTPS
GEO_PARALELO = 4
_geo_cache = {}
_geo_lock = threading.Lock()


def _reservado(ip):
    """IP que nunca tem pais: rede privada, CGNAT, loopback, link-local."""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except (ValueError, AttributeError):
        return True
    return (a in (0, 10, 127) or (a == 100 and 64 <= b <= 127)
            or (a == 169 and b == 254) or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or a >= 224)


def bandeira(cc):
    """Codigo ISO de 2 letras -> emoji da bandeira (indicadores regionais)."""
    if not cc or len(cc) != 2 or not cc.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def _geo_um(iface, ip):
    """Pais de um IP. (cc, pais), ou (None, None) se a API nao souber."""
    caminho = "/%s?fields=success,country_code,country" % ip
    # o nome e resolvido pelo DNS publico do link, nao pelo do sistema: o
    # Pi-hole desta casa bloqueia varios servicos de geolocalizacao (devolve
    # 0.0.0.0) e a conexao morreria com "connection refused"
    destino = _resolver(iface, GEO_HOST)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls = None
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        sock.settimeout(8.0)
        sock.connect((destino, 443))
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=GEO_HOST)
        tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
                     "Connection: close\r\n\r\n" % (caminho, GEO_HOST)).encode())
        buf = b""
        while len(buf) < 65536:
            pedaco = tls.recv(4096)
            if not pedaco:
                break
            buf += pedaco
    finally:
        try:
            (tls or sock).close()
        except OSError:
            pass
    txt = buf.decode("utf-8", "replace")
    corte = txt.find("{", txt.find("\r\n\r\n"))
    if corte < 0:
        raise ValueError("resposta sem JSON")
    dado = json.loads(txt[corte:txt.rfind("}") + 1])
    if not dado.get("success"):
        return (None, None)
    return (dado.get("country_code"), dado.get("country"))


def geolocalizar(iface, ips):
    """{ip: {"cc","pais","bandeira"}} para os IPs publicos, usando o cache.

    O cache e permanente (tabela meta): os saltos se repetem a cada traceroute,
    e nao faz sentido pagar a consulta de novo. Falha de rede nao derruba o
    tracado -- o salto so fica sem bandeira.
    """
    unicos = [ip for ip in dict.fromkeys(ips) if ip and not _reservado(ip)]
    if not unicos:
        return {}
    faltando = []
    with _geo_lock:
        for ip in unicos:
            if ip in _geo_cache:
                continue
            guardado = db.get_meta("geoip:%s" % ip)
            if isinstance(guardado, dict):
                _geo_cache[ip] = (guardado.get("cc"), guardado.get("pais"))
            else:
                faltando.append(ip)

    if faltando:
        pool = ThreadPoolExecutor(max_workers=GEO_PARALELO,
                                  thread_name_prefix="geoip")
        try:
            futuros = {pool.submit(_geo_um, iface, ip): ip for ip in faltando}
            for fut, ip in futuros.items():
                try:
                    cc, pais = fut.result(timeout=15)
                except Exception as exc:
                    log.debug("pais de %s falhou: %s", ip, exc)
                    continue
                with _geo_lock:
                    _geo_cache[ip] = (cc, pais)
                db.set_meta("geoip:%s" % ip, {"cc": cc, "pais": pais})
        finally:
            pool.shutdown(wait=False)

    fora = {}
    with _geo_lock:
        for ip in unicos:
            cc, pais = _geo_cache.get(ip, (None, None))
            if cc:
                fora[ip] = {"cc": cc, "pais": pais, "bandeira": bandeira(cc)}
    return fora


def _sonda(app, nome):
    for p in app["probes"]:
        if p.name_link == nome:
            return p
    return None


def _publicar(app, dados):
    try:
        app["bus"].publish("traceroute", dados)
    except Exception:
        log.exception("falha publicando o traceroute")


def executar(app, nome, destino, max_hops=MAX_HOPS_PADRAO, consultas=CONSULTAS):
    sonda = _sonda(app, nome)
    if sonda is None:
        raise RuntimeError("link desconhecido: %s" % nome)
    iface = sonda.iface
    destino = validar_destino(destino)
    max_hops = max(1, min(MAX_HOPS_TETO, int(max_hops)))
    consultas = max(1, min(5, int(consultas)))

    inicio = int(time.time())
    resultado = {
        "link": nome, "iface": iface, "destino": destino, "destino_ip": None,
        "ts": inicio, "saltos": [], "fase": "resolvendo", "ok": False,
        "max_hops": max_hops, "erro": None, "chegou": False,
    }
    _estado["atual"] = dict(resultado)
    _publicar(app, dict(resultado))
    log.info("traceroute de %s (%s) ate %s", nome, iface, destino)

    try:
        destino_ip = _resolver(iface, destino)
        resultado["destino_ip"] = destino_ip
        resultado["fase"] = "tracando"
        _estado["atual"] = dict(resultado)
        _publicar(app, dict(resultado))

        seq = (inicio & 0x3FFF) << 1
        for ttl in range(1, max_hops + 1):
            respostas = _um_salto(iface, destino_ip, ttl, seq, consultas, TIMEOUT)
            seq += consultas
            ips = [r["ip"] for r in respostas if r]
            ip = ips[0] if ips else None
            salto = {
                "n": ttl,
                "ip": ip,
                "host": _reverso(ip) if ip else None,
                "ms": [r["ms"] if r else None for r in respostas],
                "aviso": next((r["aviso"] for r in respostas
                               if r and r.get("aviso")), None),
            }
            # roteador com varios caminhos: os enderecos do mesmo salto diferem
            outros = [x for x in dict.fromkeys(ips) if x != ip]
            if outros:
                salto["outros_ips"] = outros
            resultado["saltos"].append(salto)
            if any(r and r["fim"] for r in respostas):
                resultado["chegou"] = ip == destino_ip or not salto["aviso"]
                _estado["atual"] = dict(resultado)
                _publicar(app, dict(resultado, fase="tracando"))
                break
            _estado["atual"] = dict(resultado)
            _publicar(app, dict(resultado, fase="tracando"))
            time.sleep(PAUSA_ENTRE_SALTOS)

        # o pais so entra no fim: a consulta e de rede e travaria o desenho
        # salto a salto, que e a parte que o usuario fica olhando
        try:
            paises = geolocalizar(iface, [s.get("ip") for s in resultado["saltos"]])
            for salto in resultado["saltos"]:
                geo = paises.get(salto.get("ip"))
                if geo:
                    salto.update(geo)
        except Exception:
            log.exception("falha geolocalizando os saltos")

        resultado["ok"] = True
        resultado["fase"] = "fim"
        resultado["duracao_s"] = int(time.time()) - inicio
        if not resultado["chegou"]:
            resultado["erro"] = (
                "o destino nao respondeu em %d saltos - pode estar bloqueando "
                "ICMP, o que e comum e nao significa link ruim" % max_hops)
        db.set_meta("traceroute:%s" % nome, resultado)
        log.info("traceroute de %s ate %s: %d saltos, chegou=%s",
                 nome, destino, len(resultado["saltos"]), resultado["chegou"])
    except Exception as exc:
        resultado["fase"] = "erro"
        resultado["ok"] = False
        resultado["erro"] = (str(exc) or exc.__class__.__name__)[:200]
        log.warning("traceroute de %s falhou: %s", nome, resultado["erro"])
    finally:
        _estado["atual"] = None

    _publicar(app, dict(resultado))
    return resultado


def iniciar(app, nome, destino, max_hops=MAX_HOPS_PADRAO, consultas=CONSULTAS):
    """Dispara numa thread. Levanta RuntimeError se ja houver um rodando."""
    validar_destino(destino)
    if not _lock.acquire(blocking=False):
        atual = em_andamento() or {}
        raise RuntimeError("ja existe um traceroute rodando (%s)"
                           % atual.get("link", "?"))

    def alvo():
        try:
            executar(app, nome, destino, max_hops, consultas)
        finally:
            _lock.release()

    try:
        threading.Thread(target=alvo, name="trace-%s" % nome, daemon=True).start()
    except Exception:
        _lock.release()
        raise
