"""Gerador de PDF minimo, em Python puro.

Nao ha reportlab, nem pip, nem navegador headless neste aparelho, entao o PDF e
montado na mao: PDF 1.4, fontes base-14 (Helvetica), texto em WinAnsi e
graficos vetoriais. E o suficiente para um relatorio com tabelas e graficos.

Sistema de coordenadas: a API usa origem no CANTO SUPERIOR ESQUERDO (como todo
mundo espera); a conversao para o eixo do PDF acontece na hora de escrever.
"""

A4 = (595.28, 841.89)          # pontos (72 dpi)

# larguras Helvetica em milesimos de em, para ASCII imprimivel
_W_REG = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
).split()
_W_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
).split()


def _largura(texto, tamanho, negrito=False):
    tab = _W_BOLD if negrito else _W_REG
    total = 0
    for ch in texto:
        c = ord(ch)
        total += int(tab[c - 32]) if 32 <= c <= 126 else 556
    return total * tamanho / 1000.0


def _esc(texto):
    """Escapa e converte para latin-1 (cobre os acentos do portugues)."""
    out = []
    for ch in texto:
        if ch in "()\\":
            out.append("\\" + ch)
        elif ord(ch) < 32:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out).encode("latin-1", "replace")


class Pagina:
    def __init__(self, largura, altura):
        self.largura = largura
        self.altura = altura
        self.ops = []

    # -- helpers de coordenada ------------------------------------------
    def _y(self, y):
        return self.altura - y

    # -- primitivas ------------------------------------------------------
    def texto(self, x, y, s, tamanho=10, negrito=False, cor=(0, 0, 0),
              alinhamento="esq"):
        if s is None:
            s = ""
        s = str(s)
        larg = _largura(s, tamanho, negrito)
        if alinhamento == "dir":
            x -= larg
        elif alinhamento == "centro":
            x -= larg / 2
        fonte = "F2" if negrito else "F1"
        self.ops.append(
            b"BT /%s %.2f Tf %.3f %.3f %.3f rg 1 0 0 1 %.2f %.2f Tm (" %
            (fonte.encode(), tamanho, cor[0], cor[1], cor[2], x, self._y(y + tamanho * 0.78))
            + _esc(s) + b") Tj ET"
        )
        return larg

    def retangulo(self, x, y, larg, alt, preenche=None, borda=None, espessura=0.6):
        if preenche:
            self.ops.append(b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" %
                            (preenche[0], preenche[1], preenche[2],
                             x, self._y(y + alt), larg, alt))
        if borda:
            self.ops.append(b"%.3f %.3f %.3f RG %.2f w %.2f %.2f %.2f %.2f re S" %
                            (borda[0], borda[1], borda[2], espessura,
                             x, self._y(y + alt), larg, alt))

    def linha(self, x1, y1, x2, y2, cor=(0, 0, 0), espessura=0.6):
        self.ops.append(b"%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S" %
                        (cor[0], cor[1], cor[2], espessura,
                         x1, self._y(y1), x2, self._y(y2)))

    def polilinha(self, pontos, cor=(0, 0, 0), espessura=1.2):
        """pontos: lista de (x, y) ou None para quebrar o traco."""
        seg = []
        for p in pontos + [None]:
            if p is None:
                if len(seg) > 1:
                    d = b"%.3f %.3f %.3f RG %.2f w %.2f %.2f m " % (
                        cor[0], cor[1], cor[2], espessura, seg[0][0], self._y(seg[0][1]))
                    d += b" ".join(b"%.2f %.2f l" % (x, self._y(y)) for x, y in seg[1:])
                    self.ops.append(d + b" S")
                seg = []
            else:
                seg.append(p)

    def poligono(self, pontos, cor=(0, 0, 0), opacidade_estado=None):
        if len(pontos) < 3:
            return
        d = b""
        if opacidade_estado:
            d += b"/%s gs " % opacidade_estado.encode()
        d += b"%.3f %.3f %.3f rg %.2f %.2f m " % (
            cor[0], cor[1], cor[2], pontos[0][0], self._y(pontos[0][1]))
        d += b" ".join(b"%.2f %.2f l" % (x, self._y(y)) for x, y in pontos[1:])
        self.ops.append(d + b" f")

    def salvar_estado(self):
        self.ops.append(b"q")

    def restaurar_estado(self):
        self.ops.append(b"Q")

    def recortar(self, x, y, larg, alt):
        self.ops.append(b"%.2f %.2f %.2f %.2f re W n" % (x, self._y(y + alt), larg, alt))

    def conteudo(self):
        return b"\n".join(self.ops)


class PDF:
    def __init__(self, tamanho=A4, titulo="Relatorio", autor="netmon"):
        self.largura, self.altura = tamanho
        self.paginas = []
        self.titulo = titulo
        self.autor = autor

    def nova_pagina(self):
        p = Pagina(self.largura, self.altura)
        self.paginas.append(p)
        return p

    def bytes(self):
        objetos = []          # cada item: bytes do corpo do objeto

        def add(corpo):
            objetos.append(corpo)
            return len(objetos)          # numero do objeto (1-based)

        n_paginas = len(self.paginas)
        # reservamos: 1 catalogo, 2 pages, depois 2 objetos por pagina, 2 fontes,
        # 1 estado grafico (transparencia), 1 info
        id_catalogo = 1
        id_pages = 2
        id_font_reg = 3
        id_font_bold = 4
        id_gstate = 5
        prim_pagina = 6

        ids_paginas = [prim_pagina + i * 2 for i in range(n_paginas)]

        objetos.append(b"<< /Type /Catalog /Pages %d 0 R >>" % id_pages)
        kids = b" ".join(b"%d 0 R" % i for i in ids_paginas)
        objetos.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n_paginas))
        objetos.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                       b"/Encoding /WinAnsiEncoding >>")
        objetos.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                       b"/Encoding /WinAnsiEncoding >>")
        objetos.append(b"<< /Type /ExtGState /ca 0.18 >>")

        for i, pag in enumerate(self.paginas):
            id_pag = ids_paginas[i]
            id_cont = id_pag + 1
            objetos.append(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
                b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> "
                b"/ExtGState << /GT %d 0 R >> >> /Contents %d 0 R >>" %
                (id_pages, self.largura, self.altura, id_font_reg, id_font_bold,
                 id_gstate, id_cont))
            fluxo = pag.conteudo()
            objetos.append(b"<< /Length %d >>\nstream\n" % len(fluxo) + fluxo +
                           b"\nendstream")

        id_info = len(objetos) + 1
        objetos.append(b"<< /Title (" + _esc(self.titulo) + b") /Author (" +
                       _esc(self.autor) + b") /Producer (netmon) >>")

        # --- montagem com xref -----------------------------------------
        saida = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for num, corpo in enumerate(objetos, start=1):
            offsets.append(len(saida))
            saida += b"%d 0 obj\n" % num + corpo + b"\nendobj\n"

        inicio_xref = len(saida)
        total = len(objetos) + 1
        saida += b"xref\n0 %d\n" % total
        saida += b"0000000000 65535 f \n"
        for off in offsets:
            saida += b"%010d 00000 n \n" % off
        saida += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                  % (total, id_catalogo, id_info, inicio_xref))
        return bytes(saida)
