"""Motor de sondagem por link.

Cada link e uma interface fisica, nunca um IP fixo: o gateway e o endereco sao
redetectados em runtime, entao mover o cabo do adaptador USB de uma rede para
outra (ex.: 192.168.200.0 -> 192.168.17.0) nao exige mexer no codigo.

Todas as sondas sao presas a interface (ping -I / SO_BINDTODEVICE) para nao
vazarem pela rota default nem pela VPN.
"""

import json
import logging
import os
import random
import re
import socket
import ssl
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
TROCA_CARENCIA = 60        # s de carencia depois de trocar a placa de rede
EWMA_ALPHA = 0.4           # reage rapido, ja que o ciclo agora e curto

# Dois provedores diferentes de proposito: o alvo secundario so entra quando o
# primario some por completo, e usar o mesmo dono nos dois faria uma queda da
# Google parecer queda do link.
#
# Ate 30/08 o 8.8.8.8 era evitado aqui porque havia rotas estaticas fixando esse
# IP numa unica interface -- medir a outra operadora por ele daria um numero
# falso. Essas rotas foram removidas (elas mesmas derrubaram o DNS da casa), e
# hoje `ip route get 8.8.8.8` sai pela rota default normal. Se um dia voltarem,
# este alvo volta a mentir: confira com `ip route show | grep 8.8.8.8`.
PING_TARGETS = ["8.8.8.8", "9.9.9.9"]
DNS_SERVER = "9.9.9.9"     # nunca 127.0.0.1: mediria o Pi-hole, nao o link
DNS_NAME = "www.google.com"
TCP_TARGET = ("9.9.9.9", 443)
HTTP_TARGET = ("1.1.1.1", 80, "cp.cloudflare.com", "/generate_204")

# IP externo: quem responde e a propria borda da Cloudflare, entao o IP que ela
# devolve e exatamente o que o mundo ve saindo por AQUELA interface. Vai preso
# ao device, senao todo link responderia com o IP da rota default.
EXTIP_HOST = "1.1.1.1"
EXTIP_SNI = "one.one.one.one"
EXTIP_PATH = "/cdn-cgi/trace"
RE_EXTIP = re.compile(r"^ip=([0-9a-fA-F.:]+)$", re.M)

def alvos_das_sondas():
    """Para onde cada sonda aponta, do jeito que a pagina mostra.

    Fica aqui, e nao no servidor, porque quem define os alvos e este modulo:
    assim a pagina nunca exibe um IP diferente do que esta sendo medido.
    """
    return {
        "ping": list(PING_TARGETS),
        "ping_intervalo_s": CYCLE,
        "dns_servidor": DNS_SERVER,
        "dns_nome": DNS_NAME,
        "dns_a_cada_s": EVERY_DNS * CYCLE,
        "tcp": "%s:%d" % TCP_TARGET,
        "tcp_a_cada_s": EVERY_TCP * CYCLE,
        "http": "%s (%s%s)" % (HTTP_TARGET[0], HTTP_TARGET[2], HTTP_TARGET[3]),
        "http_a_cada_s": EVERY_HTTP * CYCLE,
        "ip_externo": "%s (%s%s)" % (EXTIP_HOST, EXTIP_SNI, EXTIP_PATH),
        "ip_externo_a_cada_s": EVERY_EXTIP * CYCLE,
    }


# ---------------------------------------------------------------------------
# Quem e quem depois de trocar o cabo de porta
# ---------------------------------------------------------------------------
# A identidade de um link e a interface, mas a interface muda quando o cabo
# muda de porta -- e ai o painel mede a GIGA achando que e a IMPACTO. O que NAO
# muda e o gateway: 192.168.18.1 e da GIGA, 192.168.17.1 e da IMPACTO, doa a
# quem doer em que placa o cabo esteja. Entao guardamos o gateway visto de cada
# link e, quando as placas se embaralham, reencontramos cada um pelo gateway.
GW_SEMENTE = {"GIGA": "192.168.18.1", "IMPACTO": "192.168.17.1"}


def gw_conhecido(nome):
    v = db.get_meta("gw_conhecido:%s" % nome)
    if isinstance(v, str) and v:
        return v
    return GW_SEMENTE.get(nome)


def lembrar_gw(nome, gw):
    if gw and gw_conhecido(nome) != gw:
        db.set_meta("gw_conhecido:%s" % nome, gw)
        log.info("%s: gateway conhecido agora e %s", nome, gw)


def detectar_por_gateway(links):
    """Descobre em que placa cada link esta agora. Devolve {NOME: iface}.

    Um link so entra no resultado se o gateway dele foi encontrado em UMA placa:
    na duvida nao mexemos, porque apontar o link para a placa errada e pior do
    que deixar como esta. O link de LAN e reconhecido pela rede do alvo dele
    (o roteador de casa), que tambem nao muda de endereco.
    """
    try:
        nomes = [n for n in sorted(os.listdir("/sys/class/net"))
                 if not n.startswith(("lo", "nordlynx", "docker", "br-", "veth",
                                      "tun", "tap", "wg"))]
    except OSError:
        return {}

    placas = {}
    for n in nomes:
        ip, gw, up = iface_info(n)
        placas[n] = {"ip": ip, "gw": gw, "up": up}

    achados, tomadas = {}, set()
    for l in links:
        nome = l["name"]
        if l.get("kind") == "lan":
            # o alvo e um IP da rede de casa: a placa certa e a que tem endereco
            # na mesma rede /24
            alvo = l.get("target") or ""
            rede = alvo.rsplit(".", 1)[0] + "." if alvo.count(".") == 3 else None
            cand = [n for n, d in placas.items()
                    if rede and d["ip"] and d["ip"].startswith(rede)]
        else:
            gw = gw_conhecido(nome)
            cand = [n for n, d in placas.items() if gw and d["gw"] == gw]
        cand = [n for n in cand if n not in tomadas]
        if len(cand) == 1:
            achados[nome] = cand[0]
            tomadas.add(cand[0])
    return achados


def dns_do_sistema():
    """Resolvedores do /etc/resolv.conf -- o DNS que o resto do aparelho usa.

    Nao e o mesmo que o das sondas, e a diferenca importa: aqui costuma estar o
    Pi-hole (127.0.0.1), que mediria o filtro local e nao o link.
    """
    saida = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as fh:
            for linha in fh:
                partes = linha.split()
                if len(partes) >= 2 and partes[0] == "nameserver":
                    saida.append(partes[1])
    except OSError:
        pass
    return saida


def dns_da_interface(iface):
    """DNS que o DHCP daquela placa entregou (o resolvedor da operadora)."""
    res = _run(["nmcli", "-t", "-f", "IP4.DNS", "device", "show", iface], timeout=5)
    if not res or res.returncode != 0:
        return []
    fora = []
    for linha in res.stdout.splitlines():
        _, _, valor = linha.partition(":")
        valor = valor.strip()
        if valor and valor not in fora:
            fora.append(valor)
    return fora


RE_LOSS = re.compile(r"([\d.]+)% packet loss")
RE_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")

# cadencia das sondas secundarias, em ciclos de 2s
EVERY_DNS = 15     # 30s
EVERY_TCP = 15     # 30s
EVERY_HTTP = 30    # 60s
EVERY_IFACE = 30   # 60s
EVERY_EXTIP = 150  # 5min -- IP publico muda raramente e a consulta custa um TLS


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


# --------------------------------------------------------------------------
# DNS da LAN: o resolvedor que os outros aparelhos usam (o Pi-hole)
# --------------------------------------------------------------------------
# Existe separado do dns_probe de propósito. O dns_probe mede o LINK: sai preso
# a uma interface e vai direto num resolvedor publico. Em 30/08 isso mostrou
# "DNS 4,5 ms, tudo certo" enquanto a casa inteira estava sem resolucao de nome,
# porque o que tinha quebrado era a rota padrao ate os upstreams do Pi-hole --
# caminho que nenhuma sonda de link percorre. Esta sonda faz o contrario: usa a
# rota normal, sem prender a interface, e pergunta ao proprio Pi-hole.
DNS_LAN_ZONA = "dnscheck-netmon.example.com"   # nunca existe: a resposta e NXDOMAIN


def resolver_probe(servidor, timeout=3.0):
    """Pergunta um nome ALEATORIO ao resolvedor da casa, pela rota normal.

    O nome muda a cada consulta porque um nome fixo seria respondido do cache do
    dnsmasq: o Pi-hole continuaria "respondendo" com os upstreams inalcancaveis,
    que e exatamente o estado que precisamos detectar. Como o nome nao existe, a
    resposta certa e NXDOMAIN -- e recebe-la ja prova que a recursao chegou la
    fora e voltou. Sucesso = NOERROR ou NXDOMAIN; SERVFAIL e silencio = falha.
    """
    nome = "%08x.%s" % (random.getrandbits(32), DNS_LAN_ZONA)
    tid = random.getrandbits(16)
    pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in nome.split("."):
        pkt += bytes([len(label)]) + label.encode()
    pkt += b"\x00" + struct.pack(">HH", 1, 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        t0 = time.monotonic()
        sock.sendto(pkt, (servidor, 53))
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            data, _ = sock.recvfrom(2048)
            if len(data) < 4 or struct.unpack(">H", data[:2])[0] != tid:
                continue
            ms = round((time.monotonic() - t0) * 1000, 2)
            rcode = struct.unpack(">H", data[2:4])[0] & 0x0F
            if rcode in (0, 3):                      # NOERROR / NXDOMAIN
                return {"ok": True, "ms": ms, "rcode": rcode, "erro": None}
            return {"ok": False, "ms": ms, "rcode": rcode,
                    "erro": "resolvedor respondeu rcode %d (sem acesso aos "
                            "upstreams?)" % rcode}
        return {"ok": False, "ms": None, "rcode": None,
                "erro": "sem resposta em %.0fs" % timeout}
    except OSError as exc:
        return {"ok": False, "ms": None, "rcode": None,
                "erro": str(exc) or exc.__class__.__name__}
    finally:
        sock.close()


def _eh_privado(ip):
    """RFC1918 / loopback -- endereco que so pode ser um resolvedor da casa."""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return False
    return (a == 10 or a == 127 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168))


def rota_default():
    """A rota padrao vencedora agora: {iface, gateway, metrica}, ou None.

    Quem tem a menor metrica leva. Serve para dizer por qual operadora o
    trafego que NAO esta preso a uma interface esta saindo -- o tunel do
    Meshnet, por exemplo.
    """
    res = _run(["ip", "-j", "-4", "route", "show", "default"])
    if not res or res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        rotas = json.loads(res.stdout)
    except ValueError:
        return None
    melhor = None
    for r in rotas:
        if not r.get("dev"):
            continue
        m = r.get("metric", 0) or 0
        if melhor is None or m < melhor["metrica"]:
            melhor = {"iface": r["dev"], "gateway": r.get("gateway"), "metrica": m}
    return melhor


def ips_locais_privados():
    """Todo IPv4 privado deste aparelho, por interface. {ip: iface}.

    O Pi-hole escuta em 0.0.0.0, entao responde em qualquer um deles -- e
    qualquer um pode estar configurado como servidor DNS num aparelho da casa.
    """
    fora = {}
    res = _run(["ip", "-j", "addr", "show"])
    if not res or res.returncode != 0 or not res.stdout.strip():
        return fora
    try:
        dados = json.loads(res.stdout)
    except ValueError:
        return fora
    for entrada in dados:
        nome = entrada.get("ifname")
        if not nome or nome == "lo" or entrada.get("operstate") not in ("UP", "UNKNOWN"):
            continue
        for a in entrada.get("addr_info", []):
            ip = a.get("local")
            if a.get("family") == "inet" and ip and _eh_privado(ip) and not ip.startswith("127."):
                fora[ip] = nome
    return fora


class SondaDnsLan:
    """Vigia TODOS os enderecos em que este aparelho serve DNS.

    Um so endereco nao basta. Este Pi responde DNS em varios IPs ao mesmo tempo
    (o fixo anunciado pelo DHCP, o que ele pegou de lease, o da LAN), e cada
    aparelho da casa pode ter sido configurado com um deles: o roteador do
    usuario, por exemplo, pergunta pelo IP de LEASE, nao pelo fixo. Vigiar so um
    deixaria o outro cair em silencio -- foi assim que o apagao de 30/08 passou
    despercebido pelo painel. Falhou qualquer um, o alerta sai nomeando qual.
    """

    INTERVALO = 30          # segundos entre rodadas
    CONFIRMA = 2            # falhas seguidas para declarar queda

    def __init__(self, app, stop, alerts=None):
        self.app = app
        self.stop = stop
        self.alerts = alerts
        self.ok = None
        self.desde = None
        self.servidores = {}      # ip -> {ok, ms, erro, falhas, desde, papel}
        self.thread = threading.Thread(target=self._loop, name="dns-lan",
                                       daemon=True)

    def start(self):
        self.thread.start()

    def _descobrir_servidores(self):
        """[(ip, papel)] -- quem vigiar e por que aquele endereco importa."""
        cfg = db.get_config()
        fixo = (cfg.get("dns_lan_servidor") or "").strip()
        if fixo:
            return [(x.strip(), "configurado a mao") for x in fixo.split(",") if x.strip()]

        achados = []
        vistos = set()

        def juntar(ip, papel):
            if ip and ip not in vistos:
                vistos.add(ip)
                achados.append((ip, papel))

        # 1) o que o DHCP da rede ANUNCIA como servidor DNS: e o endereco que os
        #    aparelhos configuram sozinhos, e o primeiro que precisa funcionar
        for p in self.app.get("probes", []):
            if p.kind != "internet" or not p.iface:
                continue
            for ip in dns_da_interface(p.iface):
                if _eh_privado(ip):
                    juntar(ip, "anunciado pelo DHCP da %s" % p.name_link)

        # 2) os demais enderecos deste aparelho: alguem pode ter apontado para
        #    qualquer um deles na mao (o roteador aponta para o de lease)
        for ip, iface in sorted(ips_locais_privados().items()):
            juntar(ip, "endereco deste aparelho em %s" % iface)

        return achados or [("127.0.0.1", "loopback (nenhum endereco encontrado)")]

    def snapshot(self):
        lista = [dict(d, servidor=ip) for ip, d in self.servidores.items()]
        lista.sort(key=lambda d: (d.get("ok") is not False, d["servidor"]))
        ruins = [d for d in lista if d.get("ok") is False]
        return {
            "ok": self.ok,
            "desde": self.desde,
            "zona": DNS_LAN_ZONA,
            "servidores": lista,
            # o principal e o anunciado pelo DHCP; a pilula do topo mostra ele
            "servidor": lista[0]["servidor"] if lista else None,
            "ms": next((d["ms"] for d in lista if d.get("ok")), None),
            "erro": ruins[0]["erro"] if ruins else None,
            "falhando": [d["servidor"] for d in ruins],
        }

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._rodada()
            except Exception:
                log.exception("falha na sonda de DNS da LAN")
            self.stop.wait(self.INTERVALO)

    def _rodada(self):
        agora = int(time.time())
        atuais = self._descobrir_servidores()
        # endereco que sumiu (lease liberada, placa fora) deixa de ser vigiado
        for ip in list(self.servidores):
            if ip not in [x[0] for x in atuais]:
                del self.servidores[ip]

        for ip, papel in atuais:
            est = self.servidores.setdefault(
                ip, {"ok": None, "ms": None, "erro": None, "falhas": 0,
                     "desde": None, "papel": papel})
            est["papel"] = papel
            r = resolver_probe(ip)
            est["ms"] = r["ms"]
            est["erro"] = r["erro"]
            if r["ok"]:
                est["falhas"] = 0
                if est["ok"] is not True:
                    antes, est["ok"] = est["ok"], True
                    est["desde"] = agora
                    if antes is False:
                        log.warning("DNS da LAN em %s (%s) voltou a resolver",
                                    ip, papel)
                        if self.alerts:
                            self.alerts.on_dns_lan(True, ip, r, papel)
            else:
                est["falhas"] += 1
                if est["ok"] is not False and est["falhas"] >= self.CONFIRMA:
                    est["ok"] = False
                    est["desde"] = agora
                    log.error("DNS da LAN em %s (%s) parou de resolver: %s",
                              ip, papel, r["erro"])
                    if self.alerts:
                        self.alerts.on_dns_lan(False, ip, r, papel)

        conhecidos = [e["ok"] for e in self.servidores.values() if e["ok"] is not None]
        novo = all(conhecidos) if conhecidos else None
        if novo != self.ok:
            self.ok = novo
            self.desde = agora


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


def ip_externo(iface, timeout=6.0):
    """IP publico visto pelo mundo naquela interface, ou None.

    A verificacao do certificado fica desligada de proposito: conectamos por IP
    (1.1.1.1) e nao por nome, e nao ha segredo nenhum trafegando -- a resposta e
    publica. Ligar a verificacao so trocaria "sem IP" por "erro de certificado".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls = None
    try:
        _bind(sock, iface)
        sock.settimeout(timeout)
        sock.connect((EXTIP_HOST, 443))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(sock, server_hostname=EXTIP_SNI)
        tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
                     "Connection: close\r\n\r\n" % (EXTIP_PATH, EXTIP_SNI)).encode())
        buf = b""
        while len(buf) < 8192:
            pedaco = tls.recv(2048)
            if not pedaco:
                break
            buf += pedaco
        m = RE_EXTIP.search(buf.decode("utf-8", "replace"))
        return m.group(1) if m else None
    except (OSError, ssl.SSLError, ValueError) as exc:
        log.debug("ip externo de %s falhou: %s", iface, exc)
        return None
    finally:
        try:
            (tls or sock).close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Maquina de estados por link
# ---------------------------------------------------------------------------
class LinkProbe(threading.Thread):

    def __init__(self, link_id, name, iface, writer, alerts, stop_event,
                 kind="internet", target=None):
        super().__init__(name="probe-%s" % name, daemon=True)
        self.link_id = link_id
        self.name_link = name
        self.iface = iface
        # kind='lan': o alvo e um IP da rede local (o roteador de casa). Nao ha
        # internet a medir ali, entao DNS/TCP/HTTP e IP externo ficam de fora.
        self.kind = kind
        self.target = target
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
        self.ip_externo = None
        self.ip_externo_ts = None
        self.ip_externo_desde = None
        self._extip_ocupado = False
        self.gw_hist = []          # ultimos 4 resultados de ping ao gateway
        self.last = {}
        # enquanto um teste de velocidade satura o link, nao faz sentido
        # anunciar "latencia alta": a fila e nossa
        self.mudo_degradacao_ate = 0
        # janela logo apos trocar de placa: o buraco entre uma interface e outra
        # e nosso, nao da operadora
        self.troca_ate = 0
        self._lock = threading.Lock()
        self._cycle = 0
        # 3 sondas ICMP simultaneas por ciclo: alvo primario, alvo alternativo
        # e gateway
        # 3 sondas ICMP + a consulta de IP externo, que roda solta para nao
        # segurar o ciclo por ate 6s quando o link esta ruim
        self.pool = ThreadPoolExecutor(max_workers=4,
                                       thread_name_prefix="ping-%s" % name)
        self._restaurar_ip_externo()

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

    def _restaurar_ip_externo(self):
        """O ultimo IP publico conhecido fica no banco: depois de um restart o
        card ja abre preenchido em vez de esperar 5 min pela primeira consulta."""
        if self.kind != "internet":
            return
        d = db.get_meta("ip_externo:%s" % self.name_link) or {}
        if isinstance(d, dict) and d.get("ip"):
            self.ip_externo = d["ip"]
            self.ip_externo_ts = d.get("ts")
            self.ip_externo_desde = d.get("desde")

    def _consultar_ip_externo(self):
        if self._extip_ocupado:
            return
        self._extip_ocupado = True

        def tarefa():
            try:
                novo = ip_externo(self.iface)
                agora = int(time.time())
                if not novo:
                    return
                if novo != self.ip_externo:
                    if self.ip_externo:
                        log.warning("%s: IP externo mudou de %s para %s",
                                    self.name_link, self.ip_externo, novo)
                    self.ip_externo_desde = agora
                self.ip_externo = novo
                self.ip_externo_ts = agora
                db.set_meta("ip_externo:%s" % self.name_link,
                            {"ip": novo, "ts": agora,
                             "desde": self.ip_externo_desde or agora,
                             "iface": self.iface})
            except Exception:
                log.exception("falha consultando o IP externo de %s", self.name_link)
            finally:
                self._extip_ocupado = False

        try:
            self.pool.submit(tarefa)
        except RuntimeError:
            self._extip_ocupado = False

    def trocar_iface(self, iface, target=None):
        """Passa a sondar por outra placa de rede, sem reiniciar o processo.

        E o caminho normal quando o adaptador USB e trocado: o nome da interface
        muda, o link continua sendo o mesmo. O estado antigo nao serve mais --
        latencia, IP e gateway sao de outra placa -- entao zera tudo.
        """
        antiga = self.iface
        self.iface = iface
        if target is not None:
            self.target = target or None
        if antiga == iface:
            return
        log.warning("%s: interface trocada de %s para %s", self.name_link, antiga, iface)
        # a placa nova leva alguns segundos para pegar IP e rota; a queda desses
        # segundos e da troca e fica marcada como tal, fora do relatorio
        self.troca_ate = time.time() + TROCA_CARENCIA
        self.ip_externo = None
        self.ip_externo_ts = self.ip_externo_desde = None
        self.ewma_rtt = self.ewma_jitter = None
        self.ewma_loss = 0.0
        self.gw_hist = []
        with self._lock:
            self.last = {}
        self._refresh_iface()

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
            "kind": self.kind,
            "target": self.target,
            # o alvo do ping vai junto do estado: e a primeira pergunta de quem
            # olha um numero de latencia -- "latencia ate onde?"
            "alvo_ping": (self.target or self.gateway) if self.kind == "lan"
                         else PING_TARGETS[0],
            "alvo_dns": None if self.kind == "lan" else DNS_SERVER,
            "iface": self.iface,
            "ip": self.ip,
            "ip_externo": self.ip_externo,
            "ip_externo_ts": self.ip_externo_ts,
            "ip_externo_desde": self.ip_externo_desde,
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
        # no link de LAN quem manda e o alvo escolhido pelo usuario: o roteador
        # dele pode nao ser o gateway default daquela placa
        if self.kind == "lan" and self.target:
            self.gateway = self.target
        elif self.kind == "internet" and self.gateway and self.iface_up:
            # guarda o gateway desta operadora: e por ele que o link e
            # reencontrado quando o cabo mudar de porta
            lembrar_gw(self.name_link, self.gateway)

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

        if self.kind == "lan":
            # a "internet" deste link e um IP da propria casa: um unico alvo,
            # que ao mesmo tempo e o alvo da medicao e o gateway
            alvo = self.target or self.gateway
            res = ping(self.iface, alvo) if alvo else {
                "loss": 100.0, "rtt_min": None, "rtt_avg": None, "rtt_max": None,
                "jitter": None, "err": "no_link"}
            alt = res
            no_link = res["err"] == "no_link" or not self.iface_up
            gw_ok = res["loss"] < 100.0
            gw_rtt = res["rtt_avg"]
            dns_ms = tcp_ms = None
            http_ok = None
        else:
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

            # 3) sondas secundarias
            dns_ms = tcp_ms = None
            http_ok = None
            if self._cycle % EVERY_DNS == 0:
                dns_ms = dns_probe(self.iface)
            if self._cycle % EVERY_TCP == 0:
                tcp_ms = tcp_probe(self.iface)
            if self._cycle % EVERY_HTTP == 0:
                http_ok = http_probe(self.iface)
            if self._cycle == 1 or self._cycle % EVERY_EXTIP == 0:
                self._consultar_ip_externo()

        self.gw_hist.append(gw_ok)
        del self.gw_hist[:-4]
        if not gw_ok and self.gateway:
            self._refresh_iface()

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
                if self.kind == "lan":
                    # aqui nao existe provedor: ou a placa perdeu link, ou o
                    # proprio roteador de casa parou de responder
                    cause = "cabo" if no_link else "roteador_local"
                else:
                    cause = ("cabo" if no_link else
                             "roteador_local" if not gw_vivo else "provedor")
                # queda que comeca durante o teste de velocidade e nossa: num link
                # de 6 Mbps saturado o ICMP morre. Fica registrada, mas marcada --
                # e fora do relatorio que vai para a operadora
                if ts < self.mudo_degradacao_ate and not no_link:
                    cause = "teste_velocidade"
                if ts < self.troca_ate:
                    cause = "troca_placa"
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
