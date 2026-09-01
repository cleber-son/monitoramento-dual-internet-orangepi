"""Varredura da rede: quem esta ligado aqui dentro, e o que e cada aparelho.

O netmon sabia tudo sobre os dois canos de internet e nada sobre a casa. Este
modulo preenche o outro lado: quais aparelhos estao na rede agora, o IP e o MAC
de cada um, o fabricante da placa, se entraram por cabo ou por Wi-Fi e quais
portas estao abertas.

Como isso e feito sem root (aqui `sudo` pede senha, e nao ha nmap nem arp-scan):

  * DESCOBERTA: um unico soquete ICMP de datagrama -- liberado a qualquer
    usuario por `net.ipv4.ping_group_range`, o mesmo truque do traceroute --
    dispara um echo para cada endereco da faixa. Junto vai um datagrama UDP
    vazio para a porta 9. O ping acha quem responde; o UDP existe so para
    OBRIGAR o kernel a resolver o ARP de quem NAO responde, e a tabela ARP
    entrega o aparelho mesmo calado. Nesta casa isso levou a conta de 7
    aparelhos (so ICMP) para 11 (ICMP + ARP).
  * FABRICANTE: o prefixo do MAC (OUI) e consultado uma vez e guardado para
    sempre no banco. Ha uma tabela local com os fabricantes mais comuns por
    aqui, e o resto sai de uma API publica -- que limita 1 consulta por
    segundo, dai a fila lenta e o cache permanente.
  * CABO OU WI-FI: nao existe como perguntar isso a um aparelho pela rede. O
    que existe e a assinatura da latencia: nesta casa o cabo responde em
    0,2-0,5 ms com desvio de 0,05 ms, e o Wi-Fi em 3-5 ms com desvio de 0,4 ms.
    A resposta e sempre um PALPITE, e a pagina diz isso com todas as letras.
  * PORTAS: connect() de TCP comum, prazo curto, em paralelo.

Tudo preso a interface (SO_BINDTODEVICE), como o resto do netmon: varrer a rede
da GIGA nao pode sair pela IMPACTO so porque ela e a rota padrao.
"""

import ipaddress
import json
import logging
import re
import select
import socket
import ssl
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db

log = logging.getLogger("netmon.scan")

SO_BINDTODEVICE = 25

MAX_HOSTS = 1024              # /22 e o limite: acima disso a varredura vira castigo
RONDAS = 2                    # dois disparos: um pacote perdido nao apaga o aparelho
ESPERA_ICMP = 1.6             # s ouvindo respostas depois de cada ronda
ESPERA_ARP = 1.2              # s para o kernel terminar de resolver os ARPs
PARALELO_DETALHE = 8          # aparelhos medidos ao mesmo tempo
PARALELO_PORTAS = 24
TIMEOUT_PORTA = 0.7
PTR_TIMEOUT = 1.0

# Portas escolhidas pelo que elas DIZEM sobre o aparelho, nao por serem famosas:
# 9100 e impressora, 62078 e iPhone, 32400 e Plex, 554 e camera.
SERVICOS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP",
    81: "HTTP alt", 88: "HTTP alt", 110: "POP3", 135: "RPC (Windows)",
    139: "NetBIOS", 143: "IMAP", 443: "HTTPS", 445: "SMB (compartilhamento)",
    515: "impressora (LPD)", 548: "AFP (Apple)", 554: "RTSP (camera)",
    631: "IPP (impressora)", 873: "rsync", 902: "VMware", 993: "IMAPS",
    1883: "MQTT (IoT)", 1900: "UPnP", 2049: "NFS", 2323: "Telnet alt",
    3000: "HTTP app", 3306: "MySQL", 3389: "Area de trabalho remota",
    4444: "HTTP app", 5000: "HTTP app / UPnP", 5001: "HTTP app",
    5060: "SIP (telefonia)", 5432: "PostgreSQL", 5555: "ADB (Android)",
    5900: "VNC", 6379: "Redis", 7070: "streaming", 8000: "HTTP alt",
    8008: "Chromecast", 8009: "Chromecast", 8080: "HTTP alt",
    8081: "HTTP alt", 8123: "Home Assistant", 8291: "Mikrotik (Winbox)",
    8443: "HTTPS alt", 8883: "MQTT/TLS", 8888: "HTTP alt", 9000: "HTTP app",
    9100: "impressora (RAW)", 9200: "Elasticsearch", 10000: "Webmin",
    32400: "Plex", 37777: "DVR (camera)", 49152: "UPnP", 62078: "iPhone/iPad",
}

PORTAS_RAPIDO = [21, 22, 23, 53, 80, 139, 443, 445, 554, 631, 1883, 3389,
                 5000, 5900, 8000, 8008, 8080, 8443, 9100, 32400, 62078]
PORTAS_COMPLETO = sorted(SERVICOS)

# Fabricantes que aparecem nesta casa e nas vizinhas. A tabela local existe para
# a varredura funcionar com a internet caida -- que e justamente quando alguem
# vai querer olhar a rede.
OUI_LOCAL = {
    "001560": "Apple", "0017f2": "Apple", "003ee1": "Apple", "0c4de9": "Apple",
    "3c0754": "Apple", "60f81d": "Apple", "7cd1c3": "Apple", "a4d18c": "Apple",
    "d49a20": "Apple", "f0dbe2": "Apple",
    "0012fb": "Samsung", "0021d1": "Samsung", "5001bb": "Samsung",
    "8c7712": "Samsung", "c81479": "Samsung", "e8508b": "Samsung",
    "b827eb": "Raspberry Pi", "dca632": "Raspberry Pi", "e45f01": "Raspberry Pi",
    "d83add": "Raspberry Pi", "2ccf67": "Raspberry Pi",
    "24a160": "Espressif (IoT)", "3c6105": "Espressif (IoT)",
    "5ccf7f": "Espressif (IoT)", "84f3eb": "Espressif (IoT)",
    "a020a6": "Espressif (IoT)", "bcddc2": "Espressif (IoT)",
    "d8f15b": "Espressif (IoT)", "ecfabc": "Espressif (IoT)",
    "0019cb": "Intelbras", "d8365f": "Intelbras", "e0cec3": "Intelbras",
    "4c3fd3": "Intelbras", "3c8375": "Intelbras",
    "d40dab": "Cudy", "d84732": "Cudy",
    "0c8063": "TP-Link", "1027f5": "TP-Link", "50c7bf": "TP-Link",
    "6466b3": "TP-Link", "a42bb0": "TP-Link", "b0be76": "TP-Link",
    "c006c3": "TP-Link", "e894f6": "TP-Link", "f4f26d": "TP-Link",
    "c46e1f": "TP-Link", "9c5322": "Compal/TP-Link",
    "c81e8e": "Mercusys", "5c628b": "Mercusys",
    "c83a35": "Tenda", "d8320f": "Tenda", "04959d": "Tenda",
    "286c07": "Xiaomi", "3c47b1": "Xiaomi", "64cc2e": "Xiaomi",
    "78119e": "Xiaomi", "8cbeed": "Xiaomi", "f8a45f": "Xiaomi",
    "001a11": "Google", "3c5ab4": "Google", "54607b": "Google",
    "6c5ab0": "TCL", "b4a5ef": "TCL",
    "0c47c9": "Amazon", "44650d": "Amazon", "68374a": "Amazon",
    "747548": "Amazon", "fc65de": "Amazon",
    "001788": "Philips Hue", "b0c554": "D-Link", "1cbdb9": "D-Link",
    "0004ed": "Motorola", "4c6d58": "Motorola",
    "001a2b": "Ubiquiti", "24a43c": "Ubiquiti", "788a20": "Ubiquiti",
    "e48d8c": "Mikrotik", "48a98a": "Mikrotik", "dc2c6e": "Mikrotik",
    "001e2a": "Netgear", "a040a0": "Netgear",
    "44237c": "LG", "48594f": "LG", "cc2d8c": "LG",
    "001dba": "Sony", "fcf152": "Sony",
    "0026b9": "Dell", "d4be d9": "Dell", "f8bc12": "Dell",
    "3ca82a": "Hewlett-Packard", "94577a": "Hewlett-Packard",
    "00156d": "Ubiquiti", "9c5c8e": "ASUSTek", "d850e6": "ASUSTek",
    "001e8f": "Canon", "002673": "Brother", "0026ab": "Seiko Epson",
    "44d9e7": "Ubiquiti", "e4fac4": "Huawei", "c0e018": "Huawei",
    "00e04c": "Realtek", "525400": "maquina virtual (QEMU/KVM)",
    "000c29": "VMware", "080027": "VirtualBox", "0242ac": "Docker",
}

# Fabricante -> palpite de aparelho, quando as portas nao dizem nada
VENDOR_TIPO = [
    (("apple",), "aparelho Apple"),
    (("samsung",), "aparelho Samsung"),
    (("xiaomi", "redmi"), "aparelho Xiaomi"),
    (("motorola", "lg elec", "sony", "tcl", "asustek", "huawei"), "celular ou TV"),
    (("espressif", "tuya", "sonoff", "shelly", "philips hue"), "aparelho IoT"),
    (("raspberry",), "Raspberry Pi"),
    (("intelbras", "hikvision", "dahua"), "equipamento Intelbras/CFTV"),
    (("cudy", "tp-link", "mercusys", "tenda", "d-link", "mikrotik", "ubiquiti",
      "netgear", "realtek"), "equipamento de rede"),
    (("hewlett", "epson", "brother", "canon", "lexmark"), "impressora"),
    (("amazon", "google", "roku"), "aparelho de streaming"),
    (("qemu", "vmware", "virtualbox", "docker"), "maquina virtual"),
]

RE_NEIGH = re.compile(
    r"^(?P<ip>\d+\.\d+\.\d+\.\d+)\s+.*?lladdr\s+(?P<mac>[0-9a-f:]{17})\s+(?P<est>\w+)",
    re.I)

VENDOR_HOST = "api.macvendors.com"
VENDOR_PAUSA = 1.2            # a API gratuita corta em ~1 consulta por segundo
VENDOR_MAX = 25               # por varredura; o resto fica para a proxima


# ---------------------------------------------------------------------------
# ICMP cru, igual ao do traceroute
# ---------------------------------------------------------------------------
def _checksum(dados):
    if len(dados) % 2:
        dados += b"\0"
    total = sum(struct.unpack("!%dH" % (len(dados) // 2), dados))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _pacote(seq):
    corpo = b"netmon-scan"
    cab = struct.pack("!BBHHH", 8, 0, 0, 0, seq & 0xFFFF)
    return cab[:2] + struct.pack("!H", _checksum(cab + corpo)) + cab[4:] + corpo


def _bind(sock, iface):
    sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")


def _run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("comando falhou %s: %s", cmd, exc)
        return None


# ---------------------------------------------------------------------------
# Quais redes da para varrer
# ---------------------------------------------------------------------------
IGNORAR = ("lo", "nordlynx", "docker", "br-", "veth", "tun", "tap", "wg")


def redes(app=None):
    """Faixas locais que valem uma varredura, com o rotulo do link de cada uma.

    Uma placa pode ter mais de um endereco (aqui a da GIGA tem o IP do DHCP e o
    192.168.18.2 fixo do Pi-hole) e os dois caem na MESMA faixa: a lista e por
    REDE, nao por endereco, senao a mesma varredura apareceria duas vezes.
    """
    por_iface = {}
    if app:
        for p in app.get("probes", []):
            # no link de LAN o "gateway" util e o roteador que o usuario aponta
            # nas configuracoes: a eth0 daqui nao tem rota default nenhuma
            por_iface[p.iface] = {
                "link": p.name_link, "kind": p.kind,
                "gateway": (p.target if p.kind == "lan" else p.gateway),
            }

    res = _run(["ip", "-j", "-4", "addr", "show"])
    if not res or res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        dados = json.loads(res.stdout)
    except ValueError:
        return []

    fora, vistos = [], set()
    for entrada in dados:
        nome = entrada.get("ifname") or ""
        if not nome or nome.startswith(IGNORAR):
            continue
        for a in entrada.get("addr_info", []):
            ip, prefixo = a.get("local"), a.get("prefixlen")
            if a.get("family") != "inet" or not ip or not prefixo:
                continue
            try:
                rede = ipaddress.ip_network("%s/%d" % (ip, prefixo), strict=False)
            except ValueError:
                continue
            if not rede.is_private or rede.num_addresses > MAX_HOSTS:
                continue
            chave = "%s|%s" % (nome, rede.with_prefixlen)
            if chave in vistos:
                continue
            vistos.add(chave)
            info = por_iface.get(nome, {})
            fora.append({
                "id": chave,
                "iface": nome,
                "cidr": rede.with_prefixlen,
                "ip_local": ip,
                "link": info.get("link"),
                "kind": info.get("kind"),
                "gateway": info.get("gateway"),
                "hosts": rede.num_addresses - 2 if rede.num_addresses > 2 else 1,
                "rotulo": ("%s — rede local" % info["link"]
                           if info.get("kind") == "lan" else
                           "rede da %s" % info["link"] if info.get("link")
                           else "placa %s" % nome) + " · " + rede.with_prefixlen,
            })
    # a rede de casa (o link de LAN) primeiro: e a que o usuario quer ver
    fora.sort(key=lambda r: (r.get("kind") != "lan", r["iface"]))
    return fora


def _rede_por_id(app, rede_id):
    disponiveis = redes(app)
    if not disponiveis:
        raise RuntimeError("nenhuma rede local encontrada para varrer")
    if not rede_id:
        return disponiveis[0]
    for r in disponiveis:
        if r["id"] == rede_id:
            return r
    raise RuntimeError("rede desconhecida: %s" % rede_id)


# ---------------------------------------------------------------------------
# Descoberta
# ---------------------------------------------------------------------------
def mac_da_placa(iface):
    """MAC da propria interface: o Pi nao aparece na tabela ARP dele mesmo."""
    try:
        with open("/sys/class/net/%s/address" % iface) as fh:
            return fh.read().strip().lower() or None
    except OSError:
        return None


def _ips_locais():
    """Todo IPv4 deste aparelho -- para nao confundir o Pi com um vizinho."""
    fora = set()
    res = _run(["ip", "-j", "-4", "addr", "show"])
    if res and res.returncode == 0 and res.stdout.strip():
        try:
            for e in json.loads(res.stdout):
                for a in e.get("addr_info", []):
                    if a.get("family") == "inet" and a.get("local"):
                        fora.add(a["local"])
        except ValueError:
            pass
    return fora


def descobrir(iface, alvos, rondas=RONDAS, aviso=None):
    """Echo para cada endereco + cutucao UDP. Devolve {ip: rtt_ms}.

    O UDP nao espera resposta: ele existe para o kernel ter que descobrir o MAC
    do destino, o que enche a tabela ARP mesmo com o aparelho calado.
    """
    achados = {}
    for ronda in range(rondas):
        icmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _bind(icmp, iface)
            _bind(udp, iface)
            icmp.setblocking(False)
            envio = {}
            for i, ip in enumerate(alvos):
                seq = (ronda << 12 | i) & 0xFFFF
                try:
                    icmp.sendto(_pacote(seq), (ip, 0))
                    envio[seq] = (ip, time.monotonic())
                except OSError:
                    continue
                if ronda == 0:
                    try:
                        udp.sendto(b"\0", (ip, 9))
                    except OSError:
                        pass
                # em rajada solta o kernel enfileira e descarta: 32 por vez
                if i % 32 == 31:
                    time.sleep(0.02)
                    _colher(icmp, envio, achados, 0)
            fim = time.time() + ESPERA_ICMP
            while time.time() < fim:
                _colher(icmp, envio, achados, 0.2)
                if aviso:
                    aviso(len(achados))
        finally:
            icmp.close()
            udp.close()
    return achados


def _colher(icmp, envio, achados, espera):
    pronto, _, _ = select.select([icmp], [], [], espera)
    if not pronto:
        return
    while True:
        try:
            dados, addr = icmp.recvfrom(1024)
        except (BlockingIOError, InterruptedError, OSError):
            return
        if len(dados) < 8:
            continue
        tipo, _code, _c, _id, seq = struct.unpack("!BBHHH", dados[:8])
        if tipo != 0 or seq not in envio:
            continue
        ip, t0 = envio[seq]
        if addr[0] != ip:
            continue
        ms = round((time.monotonic() - t0) * 1000, 2)
        # o primeiro pacote de cada aparelho paga a resolucao do ARP e vem
        # inflado (ja vi 177 ms de um celular a 3 ms): fica o menor
        if ip not in achados or ms < achados[ip]:
            achados[ip] = ms


def vizinhos(iface):
    """Tabela ARP da placa: {ip: (mac, estado)}. FAILED nao entra."""
    fora = {}
    res = _run(["ip", "-4", "neigh", "show", "dev", iface])
    if not res or res.returncode != 0:
        return fora
    for linha in res.stdout.splitlines():
        m = RE_NEIGH.match(linha.strip())
        if not m:
            continue
        est = m.group("est").upper()
        if est in ("FAILED", "INCOMPLETE"):
            continue
        fora[m.group("ip")] = (m.group("mac").lower(), est)
    return fora


# ---------------------------------------------------------------------------
# Detalhe de cada aparelho
# ---------------------------------------------------------------------------
def _reverso(ip, timeout=PTR_TIMEOUT):
    """PTR do aparelho. Numa thread propria: gethostbyaddr nao aceita prazo."""
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


def medir(iface, ip):
    """5 pings depois do ARP resolvido: (rtt_min, mdev, perda)."""
    import probe as probe_mod
    r = probe_mod.ping(iface, ip, count=5, interval=0.2, wait=1)
    return r.get("rtt_min"), r.get("jitter"), r.get("loss")


# Os cortes vieram da medicao desta casa, com o ARP ja resolvido:
#   cabo:   0,17-0,49 ms de minimo, desvio de 0,03 a 0,15 ms
#   Wi-Fi:  2,87-3,68 ms de minimo, desvio de 0,39 a 0,75 ms
# Entre um e outro fica a faixa honesta do "nao sei dizer".
def classificar(rtt_min, mdev, respondeu_antes=False):
    if rtt_min is None:
        if respondeu_antes:
            return ("desconhecida", "respondeu na descoberta e calou depois — "
                                    "sem medida confiável para julgar")
        return ("desconhecida", "o aparelho não respondeu ao ping — só apareceu "
                                "na tabela ARP")
    if rtt_min < 1.0 and (mdev is None or mdev < 0.35):
        return ("cabo", "resposta em %.2f ms, firme — assinatura de cabo" % rtt_min)
    if rtt_min >= 2.0 or (mdev or 0) >= 0.8:
        return ("wifi", "resposta em %.2f ms variando %.2f ms — assinatura de Wi-Fi"
                        % (rtt_min, mdev or 0))
    return ("indefinida", "resposta em %.2f ms: entre o cabo e o Wi-Fi, não dá "
                          "para afirmar" % rtt_min)


def mac_aleatorio(mac):
    """MAC administrado localmente: celular moderno sorteando endereco.

    O segundo bit menos significativo do primeiro octeto ligado quer dizer que
    o endereco nao veio de fabrica -- entao o fabricante nao diz nada, e o
    aparelho aparece com um MAC diferente a cada rede em que entra.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, AttributeError, IndexError):
        return False


def portas_abertas(iface, ip, portas):
    """connect() em paralelo. Devolve [{porta, servico}] em ordem."""
    abertas = []

    def tentar(porta):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _bind(s, iface)
            s.settimeout(TIMEOUT_PORTA)
            if s.connect_ex((ip, porta)) == 0:
                return porta
        except OSError:
            return None
        finally:
            s.close()
        return None

    pool = ThreadPoolExecutor(max_workers=PARALELO_PORTAS,
                              thread_name_prefix="portas")
    try:
        for porta in pool.map(tentar, portas):
            if porta:
                abertas.append({"porta": porta,
                                "servico": SERVICOS.get(porta, "")})
    finally:
        pool.shutdown(wait=False)
    return sorted(abertas, key=lambda p: p["porta"])


# ---------------------------------------------------------------------------
# Fabricante pelo MAC
# ---------------------------------------------------------------------------
_vendor_lock = threading.Lock()
_vendor_cache = {}


def _oui(mac):
    return (mac or "").replace(":", "").replace("-", "").lower()[:6]


def _iface_internet():
    """A placa que esta levando o trafego para fora agora.

    A consulta de fabricante NAO pode sair pela placa varrida: a `eth0` desta
    casa e so rede local, sem rota default nenhuma, e prender o soquete nela
    fazia toda consulta morrer no timeout -- e gravar "fabricante desconhecido"
    no cache permanente, que e pior do que nao ter consultado.
    """
    import probe as probe_mod
    rota = probe_mod.rota_default()
    return rota["iface"] if rota else None


def _vendor_online(mac):
    """Consulta a API publica pela placa que tem internet. Devolve texto puro."""
    import speedtest as speedtest_mod
    iface = _iface_internet()
    if not iface:
        raise RuntimeError("nenhuma rota para a internet")
    destino = speedtest_mod.resolver(iface, VENDOR_HOST)
    if not destino:
        destino = socket.getaddrinfo(VENDOR_HOST, 443, socket.AF_INET,
                                     socket.SOCK_STREAM)[0][4][0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls = None
    try:
        _bind(sock, iface)
        sock.settimeout(8.0)
        sock.connect((destino, 443))
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=VENDOR_HOST)
        tls.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netmon\r\n"
                     "Connection: close\r\n\r\n" % (mac, VENDOR_HOST)).encode())
        buf = b""
        while len(buf) < 16384:
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
    cabeca, _, corpo = txt.partition("\r\n\r\n")
    primeira = cabeca.splitlines()[0] if cabeca else ""
    if " 200 " not in primeira:
        # 404 = OUI sem dono conhecido; 429 = passamos do limite da fonte
        return None
    corpo = corpo.strip()
    # a resposta boa e o nome do fabricante em texto puro; erro vem em JSON
    if not corpo or corpo.startswith("{") or len(corpo) > 120:
        return None
    return corpo


AUSENTE = object()


def fabricante(macs, aviso=None):
    """{mac: fabricante}. Tabela local, cache no banco e, so entao, a rede.

    O cache e permanente de proposito: os aparelhos da casa se repetem a cada
    varredura e a API gratuita corta em uma consulta por segundo. Ele guarda
    tambem o NAO-achado (valor nulo), senao um MAC desconhecido pagaria a
    consulta e a espera de novo a cada varredura.
    """
    fora = {}
    faltando = []
    for mac in dict.fromkeys(macs):
        oui = _oui(mac)
        if not oui:
            continue
        if mac_aleatorio(mac):
            fora[mac] = None            # MAC sorteado: o OUI nao e de ninguem
            continue
        if oui in OUI_LOCAL:
            fora[mac] = OUI_LOCAL[oui]
            continue
        with _vendor_lock:
            guardado = _vendor_cache.get(oui, AUSENTE)
        if guardado is AUSENTE:
            guardado = db.get_meta("oui:%s" % oui, AUSENTE)
        if guardado is not AUSENTE:
            with _vendor_lock:
                _vendor_cache[oui] = guardado
            fora[mac] = guardado
            continue
        faltando.append(mac)

    for i, mac in enumerate(faltando[:VENDOR_MAX]):
        oui = _oui(mac)
        nome = None
        try:
            nome = _vendor_online(mac)
        except Exception as exc:
            log.debug("fabricante de %s falhou: %s", mac, exc)
        with _vendor_lock:
            _vendor_cache[oui] = nome
        db.set_meta("oui:%s" % oui, nome)
        fora[mac] = nome
        if aviso:
            aviso(i + 1, min(len(faltando), VENDOR_MAX))
        if i + 1 < len(faltando[:VENDOR_MAX]):
            time.sleep(VENDOR_PAUSA)
    return fora


# ---------------------------------------------------------------------------
# Palpite do que e o aparelho
# ---------------------------------------------------------------------------
def palpite(vendor, portas, eh_gateway, eh_eu):
    if eh_eu:
        return "este Orange Pi (o proprio monitor)"
    if eh_gateway:
        return "roteador da rede"
    p = {x["porta"] for x in portas or []}
    if p & {9100, 515, 631}:
        return "impressora"
    if p & {554, 37777}:
        return "camera de seguranca"
    if 62078 in p:
        return "iPhone ou iPad"
    if 5555 in p:
        return "aparelho Android"
    if p & {8008, 8009}:
        return "Chromecast / Google"
    if 32400 in p:
        return "servidor de midia (Plex)"
    if 8123 in p:
        return "Home Assistant"
    if p & {3389, 445, 139, 135}:
        return "computador Windows"
    if 22 in p and p & {80, 443, 8080}:
        return "servidor Linux"
    if 22 in p:
        return "computador ou servidor Linux"
    if p & {1883, 8883}:
        return "aparelho IoT (MQTT)"
    baixo = (vendor or "").lower()
    for chaves, rotulo in VENDOR_TIPO:
        if any(c in baixo for c in chaves):
            return rotulo
    if p & {80, 443, 8080, 8443}:
        return "aparelho com pagina web"
    return None


# ---------------------------------------------------------------------------
# Memoria: quem ja foi visto aqui antes
# ---------------------------------------------------------------------------
CHAVE_CONHECIDOS = "scan:conhecidos"
_conhecidos_lock = threading.Lock()


def conhecidos():
    d = db.get_meta(CHAVE_CONHECIDOS)
    return d if isinstance(d, dict) else {}


def apelidar(mac, nome):
    """Nome que o usuario deu ao aparelho. E o unico jeito honesto de saber
    que aquele MAC e 'a TV da sala' -- PTR, NetBIOS e mDNS nao respondem nada
    nesta rede."""
    mac = (mac or "").lower().strip()
    if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
        raise ValueError("MAC invalido")
    nome = (nome or "").strip()[:60]
    with _conhecidos_lock:
        tabela = conhecidos()
        reg = tabela.get(mac) or {"primeiro": int(time.time())}
        reg["nome"] = nome or None
        tabela[mac] = reg
        db.set_meta(CHAVE_CONHECIDOS, tabela)
    return nome or None


def _registrar(hosts):
    """Grava quem foi visto e devolve o conjunto dos que sao novidade."""
    agora = int(time.time())
    novos = set()
    with _conhecidos_lock:
        tabela = conhecidos()
        primeira_vez = not tabela        # na 1a varredura ninguem e "novo"
        for h in hosts:
            mac = h.get("mac")
            if not mac:
                continue
            reg = tabela.get(mac)
            if reg is None:
                reg = {"primeiro": agora, "nome": None}
                if not primeira_vez:
                    novos.add(mac)
            reg["ultimo"] = agora
            reg["ip"] = h.get("ip")
            tabela[mac] = reg
            h["primeiro_visto"] = reg.get("primeiro")
            h["apelido"] = reg.get("nome")
        db.set_meta(CHAVE_CONHECIDOS, tabela)
    for h in hosts:
        h["novo"] = h.get("mac") in novos
    return novos


# ---------------------------------------------------------------------------
# Execucao: uma varredura por vez
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_estado = {"atual": None}


def em_andamento():
    a = _estado["atual"]
    return dict(a) if a else None


def ultimo(rede_id):
    """A varredura guardada, com os apelidos de agora.

    O apelido e gravado depois da varredura, entao o resultado congelado no
    banco ainda tem o nome antigo (ou nenhum): ele e recolado na leitura, senao
    batizar um aparelho so apareceria na proxima varredura.
    """
    d = db.get_meta("scan:ultimo:%s" % rede_id)
    if not isinstance(d, dict):
        return None
    tabela = conhecidos()
    for h in d.get("hosts") or []:
        reg = tabela.get(h.get("mac") or "")
        if reg:
            h["apelido"] = reg.get("nome")
            h["primeiro_visto"] = reg.get("primeiro")
    return d


def _publicar(app, dados):
    try:
        app["bus"].publish("varredura", dados)
    except Exception:
        log.exception("falha publicando a varredura")


def varrer(app, rede_id=None, modo_portas="rapido"):
    rede = _rede_por_id(app, rede_id)
    iface = rede["iface"]
    portas = {"nenhuma": [], "rapido": PORTAS_RAPIDO,
              "completo": PORTAS_COMPLETO}.get(modo_portas, PORTAS_RAPIDO)

    faixa = ipaddress.ip_network(rede["cidr"], strict=False)
    meus = _ips_locais()
    alvos = [str(h) for h in faixa.hosts()]

    inicio = int(time.time())
    r = {
        "rede": rede, "ts": inicio, "fase": "descobrindo", "erro": None,
        "modo_portas": modo_portas, "hosts": [], "total": 0, "ok": False,
        "progresso": {"feito": 0, "total": len(alvos),
                      "etapa": "procurando aparelhos em %s" % rede["cidr"]},
    }
    _estado["atual"] = dict(r)
    _publicar(app, dict(r))
    log.info("varredura de %s (%s), portas=%s", rede["cidr"], iface, modo_portas)

    try:
        ultimo_aviso = [0.0]

        def andamento(n):
            agora = time.time()
            if agora - ultimo_aviso[0] < 0.7:
                return
            ultimo_aviso[0] = agora
            r["progresso"] = {"feito": n, "total": len(alvos),
                              "etapa": "procurando aparelhos em %s" % rede["cidr"]}
            _estado["atual"] = dict(r)
            _publicar(app, dict(r))

        respondeu = descobrir(iface, alvos, aviso=andamento)
        time.sleep(ESPERA_ARP)
        arp = vizinhos(iface)

        # o gateway sai uma vez so: perguntar a rota por aparelho seria rodar
        # `ip route` 254 vezes
        gw = rede.get("gateway") or _gateway(iface)
        meu_mac = mac_da_placa(iface)
        na_faixa = set(alvos)
        ips = sorted(set(list(respondeu) + [ip for ip in arp if ip in na_faixa]),
                     key=lambda x: tuple(int(o) for o in x.split(".")))
        r["hosts"] = [{
            "ip": ip,
            "mac": arp.get(ip, (None, None))[0] or (meu_mac if ip in meus else None),
            "arp": arp.get(ip, (None, None))[1],
            "respondeu_ping": ip in respondeu,
            "eu": ip in meus,
            "gateway": ip == gw,
            "portas": [], "vendor": None, "tipo": None,
            "conexao": "desconhecida", "conexao_motivo": None,
            # o proprio aparelho nao tem latencia de rede: ele responderia a si
            # mesmo pelo loopback, e o numero nao diria nada
            "rtt_ms": None if ip in meus else respondeu.get(ip),
            "jitter_ms": None, "nome": None,
            "mac_aleatorio": mac_aleatorio(arp.get(ip, (None, None))[0]),
            "apelido": None, "primeiro_visto": None, "novo": False,
        } for ip in ips]
        r["total"] = len(r["hosts"])
        r["fase"] = "detalhando"
        r["progresso"] = {"feito": 0, "total": len(r["hosts"]),
                          "etapa": "medindo e olhando as portas de %d aparelho(s)"
                                   % len(r["hosts"])}
        _estado["atual"] = dict(r)
        _publicar(app, dict(r))

        feitos = [0]

        def detalhar(h):
            if not h["eu"]:
                rtt, mdev, _perda = medir(iface, h["ip"])
                if rtt is not None:
                    h["rtt_ms"], h["jitter_ms"] = rtt, mdev
                h["conexao"], h["conexao_motivo"] = classificar(
                    rtt, mdev, h["respondeu_ping"])
            else:
                h["conexao"], h["conexao_motivo"] = ("cabo", "e este proprio aparelho")
            h["nome"] = _reverso(h["ip"])
            if portas and (h["respondeu_ping"] or h["mac"]):
                h["portas"] = portas_abertas(iface, h["ip"], portas)
            feitos[0] += 1
            r["progresso"] = {"feito": feitos[0], "total": len(r["hosts"]),
                              "etapa": "medindo e olhando as portas de %d aparelho(s)"
                                       % len(r["hosts"])}
            _estado["atual"] = dict(r)
            _publicar(app, dict(r))

        pool = ThreadPoolExecutor(max_workers=PARALELO_DETALHE,
                                  thread_name_prefix="scan")
        try:
            list(pool.map(detalhar, r["hosts"]))
        finally:
            pool.shutdown(wait=False)

        r["fase"] = "fabricantes"
        r["progresso"] = {"feito": 0, "total": 0,
                          "etapa": "identificando os fabricantes pelo MAC"}
        _estado["atual"] = dict(r)
        _publicar(app, dict(r))

        def aviso_vendor(feito, total):
            r["progresso"] = {"feito": feito, "total": total,
                              "etapa": "consultando fabricante de MAC novo "
                                       "(1 por segundo, limite da fonte)"}
            _estado["atual"] = dict(r)
            _publicar(app, dict(r))

        marcas = fabricante([h["mac"] for h in r["hosts"] if h["mac"]],
                            aviso=aviso_vendor)
        for h in r["hosts"]:
            h["vendor"] = marcas.get(h["mac"])
            h["tipo"] = palpite(h["vendor"], h["portas"], h["gateway"], h["eu"])

        _registrar(r["hosts"])
        r["ok"] = True
        r["fase"] = "fim"
        r["duracao_s"] = int(time.time()) - inicio
        r["progresso"] = {"feito": len(r["hosts"]), "total": len(r["hosts"]),
                          "etapa": "pronto"}
        db.set_meta("scan:ultimo:%s" % rede["id"], r)
        log.info("varredura de %s: %d aparelhos em %ds",
                 rede["cidr"], len(r["hosts"]), r["duracao_s"])
    except Exception as exc:
        r["fase"] = "erro"
        r["ok"] = False
        r["erro"] = (str(exc) or exc.__class__.__name__)[:200]
        log.warning("varredura de %s falhou: %s", rede.get("cidr"), r["erro"])
    finally:
        _estado["atual"] = None

    _publicar(app, dict(r))
    return r


def _gateway(iface):
    import probe as probe_mod
    try:
        return probe_mod.iface_info(iface)[1]
    except Exception:
        return None


def iniciar(app, rede_id=None, modo_portas="rapido"):
    """Dispara numa thread. RuntimeError se ja houver uma varredura rodando."""
    _rede_por_id(app, rede_id)          # valida antes de prender o lock
    if not _lock.acquire(blocking=False):
        atual = em_andamento() or {}
        onde = (atual.get("rede") or {}).get("cidr")
        raise RuntimeError("ja existe uma varredura rodando%s — espere ela terminar"
                           % (" em %s" % onde if onde else ""))

    def alvo():
        try:
            varrer(app, rede_id, modo_portas)
        finally:
            _lock.release()

    try:
        threading.Thread(target=alvo, name="scan", daemon=True).start()
    except Exception:
        _lock.release()
        raise
