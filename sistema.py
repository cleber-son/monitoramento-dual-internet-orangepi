"""Saude do proprio Orange Pi: temperatura da CPU, carga e clock.

Existe porque o painel vigia a internet e nao vigiava o aparelho que faz a
vigilancia. Um Orange Pi 3 LTS sem cooler passa dos 75 C num dia quente, e ai
o kernel comeca a cortar o clock: a sonda que devia sair de 2 em 2 segundos
atrasa, o jitter medido sobe e o painel acusa um problema de internet que e,
na verdade, febre do proprio medidor. A temperatura no cabecalho e o aviso
antes disso.

Tudo sai de `/sys`, sem root e sem comando externo -- ler dois arquivos custa
menos que um `cat` e pode rodar a cada difusao SSE.

Os limiares NAO sao chutados: vem dos trip points que o proprio driver termico
declara. Neste aparelho o primeiro `passive` e 75 C (onde o kernel comeca a
reduzir o clock) e o `critical` e 105 C (onde ele desliga).
"""

import logging
import os
import time

log = logging.getLogger("sistema")

ZONAS = "/sys/class/thermal"
FREQ = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
FREQ_MAX = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"

# Sem trip point nenhum caimos nestes, que sao os do SoC Allwinner H6.
QUENTE_PADRAO = 75.0
CRITICO_PADRAO = 105.0

# A temperatura nao muda de figura em 2 s e o cabecalho nao precisa disso:
# um cache curto evita reler /sys a cada cliente SSE conectado.
_CACHE_S = 3
_cache = {"ts": 0.0, "dado": None}
_zonas = None
_limiares = None


def _ler(caminho):
    try:
        with open(caminho, "r") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _ler_num(caminho):
    v = _ler(caminho)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _achar_zonas():
    """Mapeia tipo -> caminho uma vez so; zona termica nao aparece em runtime."""
    global _zonas
    if _zonas is not None:
        return _zonas
    achadas = {}
    try:
        for nome in sorted(os.listdir(ZONAS)):
            if not nome.startswith("thermal_zone"):
                continue
            caminho = os.path.join(ZONAS, nome)
            tipo = (_ler(os.path.join(caminho, "type")) or "").lower()
            if tipo:
                achadas[tipo] = caminho
    except OSError:
        pass
    _zonas = achadas
    if not achadas:
        log.warning("nenhuma zona termica em %s: o cabecalho fica sem temperatura", ZONAS)
    return achadas


def _zona(*prefixos):
    """A zona cujo tipo comeca por um dos prefixos ('cpu', 'gpu')."""
    zonas = _achar_zonas()
    for p in prefixos:
        for tipo, caminho in zonas.items():
            if tipo.startswith(p):
                return caminho
    return None


def limiares():
    """(quente, critico) em C, lidos dos trip points do proprio driver.

    `quente` e o primeiro trip `passive`: dali para cima o kernel ja esta
    cortando clock, entao e a temperatura a partir da qual o numero no
    cabecalho importa. `critico` fica 15 C acima dele, sem nunca passar do
    trip `critical` -- pintar de vermelho so aos 105 C seria pintar de
    vermelho quando o aparelho ja desligou.
    """
    global _limiares
    if _limiares is not None:
        return _limiares
    zona = _zona("cpu", "soc")
    passivos, critico = [], None
    if zona:
        for i in range(16):
            tipo = _ler(os.path.join(zona, "trip_point_%d_type" % i))
            if tipo is None:
                break
            t = _ler_num(os.path.join(zona, "trip_point_%d_temp" % i))
            if t is None:
                continue
            t /= 1000.0
            if tipo == "passive":
                passivos.append(t)
            elif tipo == "critical":
                critico = t
    quente = min(passivos) if passivos else QUENTE_PADRAO
    teto = critico or CRITICO_PADRAO
    _limiares = (quente, min(quente + 15.0, teto))
    return _limiares


def nivel(c, quente, critico):
    """Quatro faixas, e cada uma diz uma coisa diferente ao usuario."""
    if c is None:
        return "sem"
    if c >= critico:
        return "critico"       # perto do desligamento termico
    if c >= quente:
        return "quente"        # o kernel ja esta cortando o clock
    if c >= quente - 10:
        return "morno"         # ainda sem prejuizo, mas subindo
    return "ok"


def snapshot(forcar=False):
    """Retrato da saude do aparelho, com cache de 3 s."""
    agora = time.time()
    if not forcar and _cache["dado"] and agora - _cache["ts"] < _CACHE_S:
        return _cache["dado"]

    cpu = _ler_num(os.path.join(_zona("cpu", "soc") or "", "temp"))
    gpu = _ler_num(os.path.join(_zona("gpu") or "", "temp"))
    cpu = round(cpu / 1000.0, 1) if cpu is not None else None
    gpu = round(gpu / 1000.0, 1) if gpu is not None else None
    quente, critico = limiares()

    mhz = _ler_num(FREQ)
    mhz_max = _ler_num(FREQ_MAX)
    try:
        carga = os.getloadavg()[0]
    except OSError:
        carga = None

    d = {
        "cpu_c": cpu,
        "gpu_c": gpu,
        "quente_c": quente,
        "critico_c": critico,
        "nivel": nivel(cpu, quente, critico),
        "mhz": int(mhz / 1000) if mhz else None,
        "mhz_max": int(mhz_max / 1000) if mhz_max else None,
        # carga por nucleo: "1.4" num aparelho de 4 nucleos nao quer dizer nada
        # sozinho, e o usuario nao tem de saber quantos nucleos o Pi tem
        "carga": round(carga, 2) if carga is not None else None,
        "nucleos": os.cpu_count() or 1,
    }
    _cache["ts"] = agora
    _cache["dado"] = d
    return d
