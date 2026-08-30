"""NordVPN Meshnet: estado e liga/desliga, sem sudo.

Aqui a NordVPN nao serve para trocar de IP -- serve para alcancar este Orange Pi
de fora, pelo Meshnet. Por isso esta secao mostra o Meshnet, e nao "conectar a
um servidor VPN": o que interessa e se o acesso remoto esta de pe.

Duas particularidades que moldaram este modulo:

  * O CLI `nordvpn` conversa com um daemon por soquete e cada chamada custa
    varios segundos. Chamar isso dentro do handler HTTP travaria a pagina, entao
    o estado e lido por uma thread em segundo plano e servido de um cache.
  * O usuario pertence ao grupo `nordvpn`, entao `nordvpn set meshnet on|off`
    funciona sem senha -- o que torna o botao possivel, e tambem perigoso: o
    painel nao tem login. Desligar o Meshnet corta o proprio caminho de acesso
    remoto a este aparelho. Por isso DESLIGAR exige confirmacao explicita na
    API, enquanto ligar nao.
"""

import logging
import re
import subprocess
import threading
import time

log = logging.getLogger("netmon.mesh")

BIN = "nordvpn"
TIMEOUT = 25
INTERVALO = 45              # o estado do Meshnet muda devagar; nao vale insistir

# tira cores/controles caso o CLI decida enfeitar a saida
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _run(args, timeout=TIMEOUT):
    try:
        return subprocess.run([BIN] + args, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        log.warning("nordvpn %s passou de %ds", " ".join(args), timeout)
        return None
    except OSError as exc:
        log.warning("nordvpn %s falhou: %s", " ".join(args), exc)
        return None


def _texto(res):
    if not res or res.returncode != 0:
        return ""
    return RE_ANSI.sub("", res.stdout or "")


def _pares(bloco):
    """'Chave: valor' por linha -> dict com as chaves em minusculo."""
    fora = {}
    for linha in bloco.splitlines():
        chave, sep, valor = linha.partition(":")
        if sep and chave.strip():
            fora[chave.strip().lower()] = valor.strip()
    return fora


def _parse_peers(texto):
    """Separa 'This device' dos pares, por secao e por bloco em branco."""
    este, locais, externos = {}, [], []
    secao = None
    for bloco in texto.split("\n\n"):
        bloco = bloco.strip()
        if not bloco:
            continue
        primeira = bloco.splitlines()[0].strip()
        # o cabecalho da secao pode vir grudado no primeiro bloco dela
        if primeira in ("This device:", "Local Peers:", "External Peers:"):
            secao = primeira
            bloco = "\n".join(bloco.splitlines()[1:])
            if not bloco.strip():
                continue
        dados = _pares(bloco)
        if not dados:
            continue
        if secao == "This device:":
            este = dados
        elif secao == "External Peers:":
            externos.append(dados)
        else:
            locais.append(dados)
    return este, locais, externos


def _peer(d):
    return {
        "nickname": d.get("nickname") or None,
        "hostname": d.get("hostname") or None,
        "ip": d.get("ip") or None,
        "status": (d.get("status") or "").lower() or None,
        "os": d.get("os") or None,
        "distribuicao": d.get("distribution") or None,
    }


def ler_estado():
    """Estado completo do Meshnet. Nunca levanta: devolve `erro` preenchido."""
    fora = {
        "disponivel": True, "meshnet": None, "vpn": None, "versao": None,
        "este_aparelho": None, "pares_locais": [], "pares_externos": [],
        "erro": None, "ts": int(time.time()),
    }

    v = _run(["--version"], timeout=10)
    if v is None:
        fora.update(disponivel=False, erro="o comando `nordvpn` nao existe neste aparelho")
        return fora
    fora["versao"] = _texto(v).strip() or None

    cfg = _texto(_run(["settings"]))
    if cfg:
        m = re.search(r"^Meshnet:\s*(\w+)", cfg, re.M)
        if m:
            fora["meshnet"] = m.group(1).lower() == "enabled"
    else:
        fora["erro"] = "nao consegui ler as configuracoes do nordvpn"

    st = _texto(_run(["status"]))
    m = re.search(r"^Status:\s*(.+)$", st, re.M)
    if m:
        fora["vpn"] = m.group(1).strip()

    if fora["meshnet"]:
        peers = _texto(_run(["meshnet", "peer", "list"]))
        if peers:
            este, locais, externos = _parse_peers(peers)
            fora["este_aparelho"] = _peer(este) if este else None
            fora["pares_locais"] = [_peer(d) for d in locais]
            fora["pares_externos"] = [_peer(d) for d in externos]
        else:
            fora["erro"] = "Meshnet ligado, mas nao consegui listar os pares"
    return fora


def definir_meshnet(ligado):
    """Liga ou desliga o Meshnet. (ok, mensagem)."""
    alvo = "on" if ligado else "off"
    res = _run(["set", "meshnet", alvo], timeout=40)
    if res is None:
        return False, "o comando `nordvpn` nao respondeu"
    saida = RE_ANSI.sub("", (res.stdout or "") + (res.stderr or "")).strip()
    saida = " ".join(saida.split())[:300]
    if res.returncode != 0:
        # "Meshnet is already enabled." sai com codigo != 0, mas nao e falha:
        # o estado pedido ja e o estado atual. Tratar como erro faria o botao
        # piscar vermelho ao clicar duas vezes.
        if re.search(r"\balready\b", saida, re.I):
            return True, saida
        return False, saida or "o nordvpn recusou o comando"
    log.warning("Meshnet %s pela pagina", "ligado" if ligado else "DESLIGADO")
    return True, saida or ("Meshnet ligado" if ligado else "Meshnet desligado")


class SondaMesh:
    """Le o estado do Meshnet em segundo plano e serve de cache.

    Existe por causa do custo: cada chamada ao CLI conversa com o daemon e leva
    segundos. Sem o cache, abrir a pagina penduraria a requisicao.
    """

    def __init__(self, stop):
        self.stop = stop
        self.estado = {"disponivel": None, "erro": None, "ts": 0}
        self._lock = threading.Lock()
        self._acordar = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="mesh",
                                       daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self._lock:
            return dict(self.estado)

    def atualizar_agora(self):
        """Pede uma releitura imediata -- usado logo apos ligar/desligar."""
        self._acordar.set()

    def _loop(self):
        while not self.stop.is_set():
            try:
                novo = ler_estado()
                with self._lock:
                    self.estado = novo
            except Exception:
                log.exception("falha lendo o estado do Meshnet")
            self._acordar.clear()
            # acorda cedo se alguem mexeu no botao
            for _ in range(INTERVALO):
                if self.stop.is_set() or self._acordar.wait(1):
                    break
