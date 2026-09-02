"""Monta o relatorio PDF do netmon a partir dos dados do banco."""

import collections
import time
from datetime import datetime

import alerts
import db
import pdf

# Paleta em versao CLARA: o PDF e impresso/lido sobre branco, entao usamos os
# passos claros do sistema de design, nao os do tema escuro da tela.
# dict com padrao: se um link for renomeado, o PDF sai em cinza em vez de quebrar
COR = collections.defaultdict(lambda: (0.35, 0.35, 0.33), {
    "GIGA": (0.165, 0.471, 0.839),      # #2a78d6
    "IMPACTO": (0.922, 0.408, 0.204),   # #eb6834
})
TINTA = (0.043, 0.043, 0.043)           # #0b0b0b
TINTA2 = (0.322, 0.318, 0.306)          # #52514e
FRACO = (0.537, 0.529, 0.506)           # #898781
GRADE = (0.882, 0.878, 0.851)           # #e1e0d9
CRITICO = (0.816, 0.231, 0.231)         # #d03b3b
BOM = (0.047, 0.639, 0.047)             # #0ca30c
ALERTA = (0.980, 0.698, 0.098)          # #fab219
SEM_DADO = (0.898, 0.894, 0.878)

MARGEM = 42
LARG_UTIL = pdf.A4[0] - 2 * MARGEM
# fixo de proposito: a contagem "pagina X de Y" do cabecalho e calculada antes
# de desenhar, entao a quebra tem que ser deterministica, nao por altura
LINHAS_POR_PAGINA = 40

PERIODOS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
NOME_PERIODO = {"1h": "ultima hora", "6h": "ultimas 6 horas", "24h": "ultimas 24 horas",
                "7d": "ultimos 7 dias", "30d": "ultimos 30 dias"}


def _dt(ts, fmt="%d/%m/%Y %H:%M:%S"):
    return datetime.fromtimestamp(int(ts), alerts.TZ).strftime(fmt)


def _n(v, casas=2, sufixo=""):
    if v is None:
        return "-"
    return ("%.*f" % (casas, v)).replace(".", ",") + sufixo


def _serie(link_id, frm, to):
    """(ts, rtt_avg, loss) na resolucao adequada ao tamanho da janela."""
    import server
    res = server.choose_res(to - frm)
    conn = db.connect(readonly=True)
    try:
        if res == "raw":
            rows = conn.execute(
                "SELECT ts, rtt_avg, loss FROM samples WHERE link_id=? AND ts>=? AND ts<=? "
                "ORDER BY ts", (link_id, frm, to)).fetchall()
        else:
            tabela = "agg_minute" if res == "minute" else "agg_hour"
            rows = conn.execute(
                "SELECT ts, rtt_avg, loss_avg loss FROM %s WHERE link_id=? AND ts>=? "
                "AND ts<=? ORDER BY ts" % tabela, (link_id, frm, to)).fetchall()
        return [(r["ts"], r["rtt_avg"], r["loss"]) for r in rows]
    finally:
        conn.close()


def _eventos(frm, to, link=None):
    conn = db.connect(readonly=True)
    try:
        # nem a queda provocada pelo nosso teste de velocidade nem a da troca de
        # placa entram: o relatorio existe para provar falha da operadora, e
        # essas duas foram nossas
        sql = ("SELECT e.*, l.name link FROM events e JOIN links l ON l.id=e.link_id "
               "WHERE e.started_at<=? AND COALESCE(e.ended_at,?)>=? "
               "AND COALESCE(e.cause,'') NOT IN ('teste_velocidade','troca_placa') ")
        params = [to, int(time.time()), frm]
        if link:
            sql += "AND l.name=? "
            params.append(link)
        rows = conn.execute(sql + "ORDER BY e.started_at DESC", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
def _cabecalho(pag, titulo, sub, pagina, total):
    pag.texto(MARGEM, 38, titulo, 17, negrito=True, cor=TINTA)
    pag.texto(MARGEM, 62, sub, 9.5, cor=TINTA2)
    pag.linha(MARGEM, 82, pdf.A4[0] - MARGEM, 82, GRADE, 0.8)
    pag.texto(pdf.A4[0] - MARGEM, 38, "pagina %d de %d" % (pagina, total), 8.5,
              cor=FRACO, alinhamento="dir")


def _rodape(pag, link=None):
    y = pdf.A4[1] - 34
    pag.linha(MARGEM, y - 10, pdf.A4[0] - MARGEM, y - 10, GRADE, 0.8)
    # num relatorio de um link so, citar a outra operadora no rodape e ruido
    assunto = ("do link %s" % link) if link else ("dos links %s" % " e ".join(
        sorted(links_do_relatorio())))
    pag.texto(MARGEM, y, "netmon - monitoramento continuo %s" % assunto, 8, cor=FRACO)
    pag.texto(pdf.A4[0] - MARGEM, y, "gerado em %s" % _dt(time.time()), 8,
              cor=FRACO, alinhamento="dir")


LINHAS = [
    ("Uptime", lambda r: _n(r["uptime_pct"], 3, "%")),
    ("Quedas no periodo", lambda r: str(r["quedas"])),
    ("Tempo total fora do ar", lambda r: alerts.fmt_dur(r["downtime_s"])),
    ("Maior queda", lambda r: alerts.fmt_dur(r["maior_queda_s"])),
    ("Periodos de latencia alta", lambda r: str(r["degradacoes"])),
    ("Latencia media", lambda r: _n(r["rtt_avg"], 2, " ms")),
    ("Latencia minima", lambda r: _n(r["rtt_min"], 2, " ms")),
    ("Latencia maxima", lambda r: _n(r["rtt_max"], 2, " ms")),
    ("Jitter medio", lambda r: _n(r["jitter_avg"], 2, " ms")),
    ("Perda media", lambda r: _n(r["loss_avg"], 2, "%")),
    ("Resolucao DNS media", lambda r: _n(r["dns_avg"], 2, " ms")),
    ("Amostras coletadas", lambda r: str(r["amostras"])),
    ("Cobertura do monitoramento", lambda r: _n(r["cobertura_pct"], 1, "%")),
]


def _tabela_resumo(pag, y, resumos, nomes):
    """Uma coluna por link. No relatorio de um link so, ela ocupa o lugar da dupla."""
    col_rot = MARGEM
    colunas = ([(MARGEM + 380, nomes[0])] if len(nomes) == 1
               else [(MARGEM + 250, nomes[0]), (MARGEM + 380, nomes[1])])

    pag.retangulo(MARGEM, y - 4, LARG_UTIL, 22, preenche=(0.965, 0.965, 0.955))
    pag.texto(col_rot + 6, y + 2, "METRICA", 8, negrito=True, cor=FRACO)
    for col, nome in colunas:
        pag.texto(col + 60, y + 2, nome, 9.5, negrito=True, cor=COR[nome],
                  alinhamento="dir")
    y += 24

    for i, (rot, fn) in enumerate(LINHAS):
        if i % 2 == 0:
            pag.retangulo(MARGEM, y - 3, LARG_UTIL, 17, preenche=(0.984, 0.984, 0.980))
        destaque = rot == "Uptime"
        pag.texto(col_rot + 6, y, rot, 9, negrito=destaque, cor=TINTA2)
        for col, nome in colunas:
            pag.texto(col + 60, y, fn(resumos[nome]), 9.5, negrito=destaque,
                      cor=TINTA if destaque else TINTA2, alinhamento="dir")
        y += 17
    return y + 6


def _grafico_latencia(pag, y, series, eventos, frm, to, altura=170):
    x0, x1 = MARGEM + 34, pdf.A4[0] - MARGEM
    y1 = y + altura

    pag.texto(MARGEM, y - 14, "Latencia ao longo do periodo (ms)", 10.5,
              negrito=True, cor=TINTA)

    ymax = 0
    for nome in series:
        for _, rtt, _ in series[nome]:
            if rtt:
                ymax = max(ymax, rtt)
    ymax = (ymax or 50) * 1.15

    X = lambda t: x0 + (t - frm) / max(1, to - frm) * (x1 - x0)
    Y = lambda v: y1 - (v / ymax) * altura

    # faixas de queda ao fundo
    pag.salvar_estado()
    for e in eventos:
        if e["type"] != "QUEDA":
            continue
        a, b = max(e["started_at"], frm), min(e["ended_at"] or to, to)
        if b <= a:
            continue
        xa, xb = X(a), X(b)
        pag.poligono([(xa, y), (max(xb, xa + 1.2), y), (max(xb, xa + 1.2), y1), (xa, y1)],
                     cor=CRITICO, opacidade_estado="GT")
    pag.restaurar_estado()

    # grade
    for i in range(5):
        v = ymax * i / 4
        yy = Y(v)
        pag.linha(x0, yy, x1, yy, GRADE, 0.5)
        pag.texto(x0 - 6, yy - 4, _n(v, 0 if ymax > 20 else 1), 7.5, cor=FRACO,
                  alinhamento="dir")

    # eixo x
    for i in range(5):
        t = frm + (to - frm) * i / 4
        fmt = "%H:%M" if (to - frm) <= 172800 else "%d/%m"
        pag.texto(X(t), y1 + 5, _dt(t, fmt), 7.5, cor=FRACO,
                  alinhamento="esq" if i == 0 else ("dir" if i == 4 else "centro"))

    for nome, pts in series.items():
        caminho = []
        for ts, rtt, _ in pts:
            caminho.append(None if rtt is None else (X(ts), Y(min(rtt, ymax))))
        pag.polilinha(caminho, cor=COR[nome], espessura=1.1)

    # legenda
    lx = x0
    for nome in series:
        pag.retangulo(lx, y1 + 20, 9, 9, preenche=COR[nome])
        pag.texto(lx + 13, y1 + 20, nome, 8.5, cor=TINTA2)
        lx += 22 + pdf._largura(nome, 8.5)
    pag.retangulo(lx, y1 + 20, 9, 9, preenche=(0.953, 0.855, 0.855), borda=CRITICO, espessura=0.5)
    pag.texto(lx + 13, y1 + 20, "periodo de queda", 8.5, cor=TINTA2)

    return y1 + 40


def _timeline(pag, y, series, eventos, frm, to, nomes=None):
    nomes = nomes or sorted(series)
    pag.texto(MARGEM, y, "Disponibilidade", 10.5, negrito=True, cor=TINTA)
    y += 16
    N = 80
    passo = (to - frm) / N
    larg = LARG_UTIL / N

    for nome in nomes:
        pag.texto(MARGEM, y, nome, 8.5, negrito=True, cor=COR[nome])
        evs = [e for e in eventos if e["link"] == nome]
        cobertos = set()
        for ts, _, _ in series.get(nome, []):
            cobertos.add(int((ts - frm) / passo))
        yy = y + 12
        for i in range(N):
            a, b = frm + i * passo, frm + (i + 1) * passo
            queda = any(e["type"] == "QUEDA" and e["started_at"] < b
                        and (e["ended_at"] or to) > a for e in evs)
            deg = any(e["type"] == "LATENCIA_ALTA" and e["started_at"] < b
                      and (e["ended_at"] or to) > a for e in evs)
            cor = CRITICO if queda else ALERTA if deg else (BOM if i in cobertos else SEM_DADO)
            pag.retangulo(MARGEM + i * larg, yy, max(1.0, larg - 0.7), 13, preenche=cor)
        y = yy + 24
    return y


def _tabela_eventos(doc, eventos, titulo_sub, pagina_ini, total_paginas, link=None):
    """Devolve a lista de paginas geradas (pode ser mais de uma)."""
    cols = [(MARGEM, "LINK", "esq"), (MARGEM + 72, "TIPO", "esq"),
            (MARGEM + 168, "INICIO", "esq"), (MARGEM + 288, "FIM", "esq"),
            (MARGEM + 400, "DURACAO", "dir"), (MARGEM + 470, "CAUSA", "esq")]
    CAUSA = {"provedor": "provedor", "roteador_local": "roteador local",
             "cabo": "sem link fisico"}
    paginas = []
    idx = 0
    n_pag = pagina_ini
    while True:
        pag = doc.nova_pagina()
        paginas.append(pag)
        _cabecalho(pag, "Historico de eventos", titulo_sub, n_pag, total_paginas)
        y = 104
        if not eventos:
            pag.texto(MARGEM, y, "Nenhuma queda ou alerta registrado no periodo.",
                      10, cor=TINTA2)
            _rodape(pag, link)
            break

        pag.retangulo(MARGEM, y - 4, LARG_UTIL, 20, preenche=(0.965, 0.965, 0.955))
        for x, rot, al in cols:
            pag.texto(x + (62 if al == "dir" else 4), y + 1, rot, 7.5,
                      negrito=True, cor=FRACO, alinhamento=al)
        y += 22

        nesta_pagina = 0
        while idx < len(eventos) and nesta_pagina < LINHAS_POR_PAGINA:
            e = eventos[idx]
            if idx % 2 == 0:
                pag.retangulo(MARGEM, y - 3, LARG_UTIL, 16, preenche=(0.984, 0.984, 0.980))
            aberto = not e["ended_at"]
            dur = (int(time.time()) - e["started_at"]) if aberto else e["duration_s"]
            pag.retangulo(MARGEM + 4, y + 2, 7, 7, preenche=COR.get(e["link"], FRACO))
            pag.texto(MARGEM + 15, y, e["link"], 8.5, cor=TINTA2)
            tipo = "Queda" if e["type"] == "QUEDA" else "Latencia alta"
            pag.texto(cols[1][0] + 4, y, tipo, 8.5,
                      cor=CRITICO if e["type"] == "QUEDA" else (0.65, 0.45, 0.02))
            pag.texto(cols[2][0] + 4, y, _dt(e["started_at"]), 8.5, cor=TINTA2)
            pag.texto(cols[3][0] + 4, y, "em andamento" if aberto else _dt(e["ended_at"]),
                      8.5, cor=TINTA2)
            pag.texto(cols[4][0] + 62, y, alerts.fmt_dur(dur), 8.5, negrito=True,
                      cor=TINTA, alinhamento="dir")
            pag.texto(cols[5][0] + 4, y, CAUSA.get(e["cause"], e["cause"] or "-"),
                      8.5, cor=TINTA2)
            y += 16
            idx += 1
            nesta_pagina += 1

        _rodape(pag, link)
        n_pag += 1
        if idx >= len(eventos):
            break
    return paginas


# ---------------------------------------------------------------------------
# O relatorio e o documento que vai para a OPERADORA: so entram links de
# internet. A latencia ate o roteador de casa e diagnostico interno e nao prova
# nada contra o provedor.
def links_do_relatorio():
    return db.link_ids("internet")


def _caixa_prova(pag, y, nome, r, eventos):
    """O quadro que a operadora vai ler primeiro: quantas quedas e quando."""
    quedas = [e for e in eventos if e["type"] == "QUEDA"]
    altura = 62
    pag.retangulo(MARGEM, y, LARG_UTIL, altura, preenche=(0.975, 0.975, 0.968),
                  borda=GRADE, espessura=0.8)
    pag.retangulo(MARGEM, y, 4, altura, preenche=COR[nome])

    if not quedas:
        pag.texto(MARGEM + 16, y + 20, "Nenhuma queda registrada no periodo.", 12,
                  negrito=True, cor=BOM)
        pag.texto(MARGEM + 16, y + 40,
                  "Monitoramento ativo a cada %d segundos, cobertura de %s do periodo."
                  % (db.SAMPLE_INTERVAL, _n(r["cobertura_pct"], 1, "%")), 9, cor=TINTA2)
        return y + altura + 18

    pag.texto(MARGEM + 16, y + 20, "%d queda(s) do link %s no periodo"
              % (r["quedas"], nome), 12, negrito=True, cor=CRITICO)
    pag.texto(MARGEM + 16, y + 40,
              "Tempo total fora do ar: %s   |   Maior queda: %s   |   Disponibilidade: %s"
              % (alerts.fmt_dur(r["downtime_s"]), alerts.fmt_dur(r["maior_queda_s"]),
                 _n(r["uptime_pct"], 3, "%")), 9.5, cor=TINTA2)
    ultima = quedas[0]
    pag.texto(pdf.A4[0] - MARGEM - 16, y + 40,
              "Ultima queda: %s" % _dt(ultima["started_at"]), 9.5, cor=TINTA2,
              alinhamento="dir")
    return y + altura + 18


def gerar(period="24h", frm=None, to=None, link=None):
    import server
    agora = int(time.time())
    if frm is None or to is None:
        span = PERIODOS.get(period, 86400)
        to, frm = agora, agora - span
    rotulo = NOME_PERIODO.get(period, "periodo personalizado")
    ids = links_do_relatorio()
    nomes = [link] if link else sorted(ids, key=lambda n: ids[n])

    resumos = {n: server.resumo_link(ids[n], frm, to) for n in nomes}
    eventos = _eventos(frm, to, link)
    series = {n: _serie(ids[n], frm, to) for n in nomes}

    # mesma aritmetica que _tabela_eventos usa para quebrar as paginas
    total_pag = 1 + max(1, -(-len(eventos) // LINHAS_POR_PAGINA))

    if link:
        titulo = "Relatorio de Quedas - Link %s" % link
        doc_titulo = "Relatorio de quedas %s - %s" % (link, rotulo)
    else:
        titulo = "Relatorio de Monitoramento de Internet"
        doc_titulo = "Relatorio netmon - %s" % rotulo

    doc = pdf.PDF(titulo=doc_titulo)
    sub = "%s  |  de %s ate %s" % (rotulo.capitalize(), _dt(frm), _dt(to))

    pag = doc.nova_pagina()
    _cabecalho(pag, titulo, sub, 1, total_pag)

    y = 100
    if link:
        # relatorio de um link so: o veredito vem antes de qualquer tabela
        y = _caixa_prova(pag, y - 6, link, resumos[link], eventos)
    else:
        # resumo executivo em uma frase por link
        for nome in nomes:
            r = resumos[nome]
            if r["uptime_pct"] is None:
                frase = "%s: sem dados suficientes no periodo." % nome
            elif r["quedas"] == 0:
                frase = ("%s: nenhuma queda. Uptime de %s, latencia media de %s."
                         % (nome, _n(r["uptime_pct"], 3, "%"), _n(r["rtt_avg"], 2, " ms")))
            else:
                frase = ("%s: %d queda(s), %s fora do ar no total (maior: %s). Uptime de %s."
                         % (nome, r["quedas"], alerts.fmt_dur(r["downtime_s"]),
                            alerts.fmt_dur(r["maior_queda_s"]), _n(r["uptime_pct"], 3, "%")))
            pag.retangulo(MARGEM, y - 4, 3, 15, preenche=COR[nome])
            pag.texto(MARGEM + 10, y, frase, 9.5, cor=TINTA2)
            y += 20

    y = _tabela_resumo(pag, y + 10, resumos, nomes)
    y = _grafico_latencia(pag, y + 26, series, eventos, frm, to)
    y = _timeline(pag, y + 6, series, eventos, frm, to, nomes)
    if link:
        pag.texto(MARGEM, y + 6,
                  "Medicao feita na interface do link %s, a cada %d segundos, por sonda"
                  " ICMP com dois alvos independentes." % (link, db.SAMPLE_INTERVAL),
                  8.5, cor=FRACO)
    _rodape(pag, link)

    _tabela_eventos(doc, eventos, sub, 2, total_pag, link)
    return doc.bytes()


# ---------------------------------------------------------------------------
# Inventario dos aparelhos da rede
#
# Documento diferente do relatorio de quedas: aquele vai para a operadora e so
# fala de internet; este e para o usuario -- quem esta ligado na casa, e
# principalmente quem apareceu ESTA SEMANA. Por isso a novidade tem cor propria
# (ouro) e uma etiqueta escrita: no papel preto-e-branco, ou para quem nao
# distingue as cores, a cor sozinha nao diria nada.
# ---------------------------------------------------------------------------
OURO = (0.647, 0.451, 0.008)            # #a57302 - tinta do destaque
OURO_FUNDO = (0.996, 0.957, 0.847)      # #fef4d8 - fundo da linha nova
OURO_BARRA = (0.851, 0.616, 0.031)      # #d99d08 - barra lateral

# Larguras somam LARG_UTIL (511 pt). As portas abertas nao cabem numa coluna
# propria sem espremer o resto: elas descem para uma segunda linha do aparelho.
SCAN_COLS = [
    ("APARELHO", 128, "esq"),
    ("IP", 68, "esq"),
    ("MAC", 88, "esq"),
    ("FABRICANTE", 92, "esq"),
    ("CONEXAO", 47, "esq"),
    ("LATENCIA", 47, "dir"),
    ("VISTO DESDE", 41, "esq"),
]
CONEXAO_ROT = {"cabo": "cabo", "wifi": "Wi-Fi", "indefinida": "indefinida",
               "desconhecida": "sem medida"}


def _corta(texto, largura, tamanho, negrito=False):
    """Corta com reticencias para o texto nunca invadir a coluna vizinha."""
    texto = str(texto or "")
    if pdf._largura(texto, tamanho, negrito) <= largura:
        return texto
    while texto and pdf._largura(texto + "...", tamanho, negrito) > largura:
        texto = texto[:-1]
    return texto + "..."


def _idade(pv, agora):
    if not pv:
        return "-"
    dias = max(0, (agora - int(pv)) // 86400)
    if dias == 0:
        return "hoje"
    if dias == 1:
        return "ontem"
    if dias < 30:
        return "ha %d dias" % dias
    return _dt(pv, "%d/%m/%Y")


def _scan_cabecalho_tabela(pag, y):
    x = MARGEM
    pag.retangulo(MARGEM, y - 4, LARG_UTIL, 20, preenche=(0.965, 0.965, 0.955))
    for rot, larg, al in SCAN_COLS:
        pag.texto(x + (larg - 4 if al == "dir" else 4), y + 1, rot, 7.5,
                  negrito=True, cor=FRACO, alinhamento=al)
        x += larg
    return y + 22


def _scan_linha(pag, y, h, agora, zebra):
    novo = bool(h.get("novo_semana"))
    portas = ", ".join(
        "%s%s" % (p.get("porta"), (" (%s)" % p["servico"]) if p.get("servico") else "")
        for p in (h.get("portas") or []))
    alt = 16 + (10 if portas else 0)

    if novo:
        pag.retangulo(MARGEM, y - 3, LARG_UTIL, alt, preenche=OURO_FUNDO)
        pag.retangulo(MARGEM, y - 3, 3, alt, preenche=OURO_BARRA)
    elif zebra:
        pag.retangulo(MARGEM, y - 3, LARG_UTIL, alt, preenche=(0.984, 0.984, 0.980))

    nome = h.get("apelido") or h.get("nome") or h.get("tipo") or "(sem nome)"
    if h.get("gateway"):
        nome = nome + " [roteador]"
    elif h.get("eu"):
        nome = nome + " [este monitor]"
    tinta = OURO if novo else TINTA2

    vals = [
        (nome, True if novo else False),
        (h.get("ip") or "-", False),
        (h.get("mac") or "-", False),
        (h.get("vendor") or "-", False),
        (CONEXAO_ROT.get(h.get("conexao"), "-"), False),
        (_n(h.get("rtt_ms"), 2, " ms") if h.get("rtt_ms") is not None else "-", False),
        (_idade(h.get("primeiro_visto"), agora), False),
    ]
    x = MARGEM
    for (rot, larg, al), (val, negrito) in zip(SCAN_COLS, vals):
        pad = 8 if x == MARGEM else 4
        util = larg - pad - 4
        pag.texto(x + (larg - 4 if al == "dir" else pad), y,
                  _corta(val, util, 8.5, negrito), 8.5, negrito=negrito,
                  cor=TINTA if negrito else tinta, alinhamento=al)
        x += larg
    if novo:
        # a etiqueta escrita e obrigatoria: a cor sozinha nao sobrevive a
        # impressao em preto-e-branco nem a quem nao distingue amarelo
        pag.texto(MARGEM + 8, y + 10, "NOVO NA SEMANA", 6.5, negrito=True, cor=OURO)
    if portas:
        pag.texto(MARGEM + (120 if novo else 8), y + 10,
                  _corta("portas abertas: " + portas, LARG_UTIL - 130, 7.5),
                  7.5, cor=FRACO)
    return y + alt


def _scan_resumo(pag, y, hosts, rede, agora):
    novos = [h for h in hosts if h.get("novo_semana")]
    cabo = sum(1 for h in hosts if h.get("conexao") == "cabo")
    wifi = sum(1 for h in hosts if h.get("conexao") == "wifi")
    caixas = [
        ("APARELHOS", str(len(hosts)), TINTA),
        ("POR CABO", str(cabo), TINTA2),
        ("POR WI-FI", str(wifi), TINTA2),
        ("NOVOS NA SEMANA", str(len(novos)), OURO if novos else TINTA2),
    ]
    larg = (LARG_UTIL - 3 * 8) / 4
    x = MARGEM
    for rot, val, cor in caixas:
        destaque = rot == "NOVOS NA SEMANA" and novos
        pag.retangulo(x, y, larg, 46,
                      preenche=OURO_FUNDO if destaque else (0.972, 0.972, 0.965))
        if destaque:
            pag.retangulo(x, y, 3, 46, preenche=OURO_BARRA)
        pag.texto(x + 10, y + 8, rot, 7, negrito=True, cor=FRACO)
        pag.texto(x + 10, y + 20, val, 19, negrito=True, cor=cor)
        x += larg + 8
    y += 58
    if novos:
        quem = ", ".join((n.get("apelido") or n.get("nome") or n.get("tipo")
                          or n.get("vendor") or n.get("mac") or "?")
                         for n in novos[:6])
        if len(novos) > 6:
            quem += " e mais %d" % (len(novos) - 6)
        pag.texto(MARGEM, y, _corta("Apareceram nos ultimos 7 dias: " + quem,
                                    LARG_UTIL, 9), 9, cor=OURO)
        y += 16
    return y


def _scan_log(doc, historico, sub, pagina, total):
    pag = doc.nova_pagina()
    _cabecalho(pag, "Log das varreduras", sub, pagina, total)
    y = 104
    pag.texto(MARGEM, y, "Quando a rede foi varrida, por quem, e o que apareceu "
                         "de novo em cada varredura.", 9, cor=TINTA2)
    y += 22
    cols = [("QUANDO", 118), ("ORIGEM", 60), ("REDE", 165), ("APARELHOS", 60),
            ("NOVOS", 50), ("DUROU", 58)]
    x = MARGEM
    pag.retangulo(MARGEM, y - 4, LARG_UTIL, 20, preenche=(0.965, 0.965, 0.955))
    for rot, larg in cols:
        pag.texto(x + 4, y + 1, rot, 7.5, negrito=True, cor=FRACO)
        x += larg
    y += 22
    if not historico:
        pag.texto(MARGEM, y, "Nenhuma varredura registrada ainda.", 9.5, cor=TINTA2)
    for i, v in enumerate(historico):
        if y > pdf.A4[1] - 90:
            break
        if i % 2 == 0:
            pag.retangulo(MARGEM, y - 3, LARG_UTIL, 16, preenche=(0.984, 0.984, 0.980))
        vals = [
            _dt(v["ts"]),
            "agendada" if v["origem"] == "auto" else "manual",
            _corta(v.get("rotulo") or v["rede_id"], 160, 8.5),
            "-" if v.get("erro") else str(v["total"]),
            str(v["n_novos"]) if v["n_novos"] else "-",
            ("%ds" % v["duracao_s"]) if v.get("duracao_s") is not None else "-",
        ]
        x = MARGEM
        for (rot, larg), val in zip(cols, vals):
            destaque = rot == "NOVOS" and v["n_novos"]
            pag.texto(x + 4, y, val, 8.5, negrito=bool(destaque),
                      cor=OURO if destaque else TINTA2)
            x += larg
        y += 16
        if v.get("erro"):
            pag.texto(MARGEM + 8, y - 2, _corta("falhou: " + v["erro"], 400, 7.5),
                      7.5, cor=CRITICO)
            y += 11
        elif v.get("novos"):
            # QUEM apareceu naquele dia e a informacao que o log existe para
            # guardar: a contagem sozinha nao serve de nada daqui a um mes
            quem = ", ".join(
                "%s (%s)" % (n.get("nome") or n.get("vendor") or n.get("mac") or "?",
                             n.get("ip") or n.get("mac") or "-")
                for n in v["novos"])
            pag.texto(MARGEM + 8, y - 2,
                      _corta("apareceram: " + quem, LARG_UTIL - 20, 7.5), 7.5, cor=OURO)
            y += 11
    _rodape_scan(pag)
    return pag


def _rodape_scan(pag):
    y = pdf.A4[1] - 34
    pag.linha(MARGEM, y - 10, pdf.A4[0] - MARGEM, y - 10, GRADE, 0.8)
    pag.texto(MARGEM, y, "netmon - inventario da rede local", 8, cor=FRACO)
    pag.texto(pdf.A4[0] - MARGEM, y, "gerado em %s" % _dt(time.time()), 8,
              cor=FRACO, alinhamento="dir")


def gerar_scan(dados, historico=None):
    """PDF dos aparelhos da rede a partir do retorno de scan.ultimo()."""
    agora = int(time.time())
    hosts = dados.get("hosts") or []
    rede = dados.get("rede") or {}
    rotulo = rede.get("rotulo") or rede.get("cidr") or "rede local"

    # cabe 1 ou 2 linhas por aparelho: a paginacao tem de ser calculada antes
    # de desenhar, senao a contagem "pagina X de Y" do cabecalho sai errada
    alturas = [16 + (10 if (h.get("portas") or []) else 0) for h in hosts]
    limite = pdf.A4[1] - 96          # onde o rodape comeca
    paginas, atual, y = [], [], 0.0
    topo_1a, topo_n = 0.0, 0.0
    for h, alt in zip(hosts, alturas):
        if not atual:
            y = 0.0
        if y + alt > (limite - (300 if not paginas else 126)):
            paginas.append(atual)
            atual, y = [], 0.0
        atual.append(h)
        y += alt
    if atual or not paginas:
        paginas.append(atual)
    total_pag = len(paginas) + 1     # + a pagina do log

    doc = pdf.PDF(titulo="Aparelhos na rede - %s" % rotulo)
    sub = "%s  |  varredura de %s" % (rotulo, _dt(dados.get("ts") or agora))

    for i, lote in enumerate(paginas):
        pag = doc.nova_pagina()
        _cabecalho(pag, "Aparelhos na Rede", sub, i + 1, total_pag)
        y = 100
        if i == 0:
            y = _scan_resumo(pag, y, hosts, rede, agora)
            pag.texto(MARGEM, y + 4,
                      "Cabo ou Wi-Fi e palpite pela assinatura da latencia: nao existe "
                      "como perguntar isso a um aparelho pela rede.", 8.5, cor=FRACO)
            y += 24
        y = _scan_cabecalho_tabela(pag, y)
        if not lote:
            pag.texto(MARGEM, y, "Nenhum aparelho encontrado nesta rede.", 9.5, cor=TINTA2)
        for j, h in enumerate(lote):
            y = _scan_linha(pag, y, h, agora, zebra=(j % 2 == 0))
        _rodape_scan(pag)

    _scan_log(doc, historico or [], sub, total_pag, total_pag)
    return doc.bytes()
