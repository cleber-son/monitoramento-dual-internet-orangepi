/* netmon — front-end. Sem bibliotecas externas: tudo precisa funcionar
   justamente quando a internet cair. */
'use strict';

const CORES = { GIGA: '#3987e5', IMPACTO: '#d95926' };
const STATUS = { good: '#0ca30c', warning: '#fab219', critical: '#d03b3b', sem: '#3a3a38' };
const ROTULO_ESTADO = { UP: 'NO AR', DEGRADED: 'DEGRADADO', DOWN: 'FORA DO AR', NO_LINK: 'SEM LINK' };
const CAUSA = {
  provedor: 'provável queda do provedor',
  roteador_local: 'roteador local não responde',
  cabo: 'sem link físico — verifique o cabo',
  teste_velocidade: 'link saturado pelo teste de velocidade (não conta contra a operadora)',
};

const estado = {
  span: 86400,
  links: [],
  eventos: [],
  offset: 0,
  total: 0,
  porta: null,
  somOn: true,
  silenciado: false,
  audioCtx: null,
  piscando: null,
  tituloBase: document.title,
  ultimoDesenho: 0,
  // limiares vindos de /api/config: as cores das estatísticas seguem eles
  limiares: { lat: 80, loss: 20, jit: 60 },
  vel: { ultimos: {}, historico: {}, rodando: null },
  timers: {},
};

/* Períodos curtos (≤ 5 min) pedem outra cadência: o gráfico se atualiza a cada
   ciclo de sondagem, senão a janela de 30 s ficaria parada por um minuto. */
const SPAN_CURTO = 300;
const INTERVALO_AMOSTRA = 2;

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ formatação */
const nf = (n, d = 1) =>
  n === null || n === undefined || Number.isNaN(n)
    ? '—'
    : Number(n).toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d });

function fmtHora(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString('pt-BR', { hour12: false });
}
function fmtDataHora(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit', year: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}
function fmtDur(s) {
  if (s === null || s === undefined) return '—';
  s = Math.max(0, Math.floor(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'min ' + (s % 60) + 's';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'min';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}
function fmtDurLonga(s) {
  if (s === null || s === undefined) return '—';
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), seg = s % 60;
  if (d) return `${d}d ${h}h ${m}min`;
  if (h) return `${h}h ${m}min`;
  if (m) return `${m}min ${seg}s`;
  return `${seg}s`;
}

/* ------------------------------------------------------------ SVG */
function svgEl(tag, attrs) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
  return e;
}
function limpar(no) { while (no.firstChild) no.removeChild(no.firstChild); }

/* Mede o SVG pela caixa real. `clientWidth` arredonda e vale 0 enquanto o
   layout não assentou — e um viewBox medido errado desenha o gráfico em escala
   errada, ocupando só um pedaço do quadro. */
function medir(svg, wPadrao, hPadrao) {
  const r = svg.getBoundingClientRect();
  return { W: Math.round(r.width) || svg.clientWidth || wPadrao,
           H: Math.round(r.height) || svg.clientHeight || hPadrao };
}

/* ------------------------------------------------------------ cards */
function renderCards(links) {
  const box = $('cards');
  const existentes = new Map([...box.children].map((c) => [c.dataset.link, c]));
  links.forEach((l) => {
    let card = existentes.get(l.name);
    if (!card) {
      card = document.createElement('article');
      card.className = 'card';
      card.dataset.link = l.name;
      box.appendChild(card);
    }
    card.style.setProperty('--cor-link', CORES[l.name] || 'var(--muted)');
    const st = l.state || 'NO_LINK';
    const rtt = l.rtt_avg !== null && l.rtt_avg !== undefined ? l.rtt_avg : l.rtt_ewma;
    const up = l.uptime || {};
    const uq = l.ultima_queda;
    const desde = l.state_since ? fmtDurLonga(Math.floor(Date.now() / 1000) - l.state_since) : '—';
    const rotDesde = st === 'UP' ? 'estável há' : 'nesse estado há';

    card.innerHTML = `
      <div class="card-topo">
        <span class="bolinha b-${st}"></span>
        <span class="card-nome" style="color:${CORES[l.name] || '#fff'}">${l.name}</span>
        <span class="card-estado e-${st}">${ROTULO_ESTADO[st] || st}</span>
      </div>
      <div class="card-metricas">
        <div class="met"><div class="met-valor">${st === 'DOWN' || st === 'NO_LINK' ? '—' : nf(rtt, 1)}<small> ms</small></div><div class="met-rot">latência</div></div>
        <div class="met"><div class="met-valor">${nf(l.loss, 0)}<small> %</small></div><div class="met-rot">perda</div></div>
        <div class="met"><div class="met-valor">${nf(l.jitter, 1)}<small> ms</small></div><div class="met-rot">jitter</div></div>
        <div class="met"><div class="met-valor pequeno">${up.h24 == null ? '—' : nf(up.h24, 2) + '%'}</div><div class="met-rot">uptime 24h</div></div>
      </div>
      <svg class="card-spark" data-spark="${l.name}"></svg>
      <div class="card-rodape">
        <span>${rotDesde} <b>${desde}</b></span>
        <span>gateway <b>${l.gateway || '—'}</b></span>
        <span>IP <b>${l.ip || '—'}</b></span>
        <span>iface <b>${l.iface || '—'}</b></span>
        <span>gw <b>${l.gw_ok ? 'ok' : 'sem resposta'}</b></span>
        ${l.dns_ms > 0 ? `<span>DNS <b>${nf(l.dns_ms, 1)} ms</b></span>` : ''}
        <span>uptime 7d <b>${up.d7 == null ? '—' : nf(up.d7, 2) + '%'}</b></span>
        <span>uptime 30d <b>${up.d30 == null ? '—' : nf(up.d30, 2) + '%'}</b></span>
        ${uq ? `<span>última queda <b>${fmtDataHora(uq.started_at)}</b> (durou <b>${fmtDur(uq.duration_s)}</b>)</span>` : '<span>sem quedas registradas</span>'}
      </div>`;
  });
}

function desenharSpark(nome, pontos) {
  const svg = document.querySelector(`[data-spark="${nome}"]`);
  if (!svg) return;
  const { W: w, H: h } = medir(svg, 300, 44);
  limpar(svg);
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const vals = pontos.filter((p) => p[1] !== null);
  if (vals.length < 2) return;
  const t0 = pontos[0][0], t1 = pontos[pontos.length - 1][0] || t0 + 1;
  const ys = vals.map((p) => p[1]);
  const ymax = Math.max(...ys) * 1.15 || 1;
  const X = (t) => ((t - t0) / Math.max(1, t1 - t0)) * w;
  const Y = (v) => h - 3 - (v / ymax) * (h - 6);
  let d = '', aberto = false;
  pontos.forEach((p) => {
    if (p[1] === null) { aberto = false; return; }
    d += (aberto ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1) + ' ';
    aberto = true;
  });
  svg.appendChild(svgEl('path', { d, fill: 'none', stroke: CORES[nome], 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: .9 }));
}

/* ------------------------------------------------------------ eixos */
function ticksY(max) {
  const alvo = 5;
  const bruto = max / alvo;
  const mag = Math.pow(10, Math.floor(Math.log10(bruto || 1)));
  const passo = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= bruto) || mag * 10;
  const out = [];
  for (let v = 0; v <= max * 1.0001; v += passo) out.push(v);
  return out;
}

function rotuloTempo(ts, span) {
  const d = new Date(ts * 1000);
  if (span <= 900) {
    // janelas de segundos: sem os segundos no eixo, todos os rótulos ficariam iguais
    return d.toLocaleTimeString('pt-BR', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }
  if (span <= 48 * 3600) {
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
}

/* Antes do primeiro dado não houve monitoramento — e um gráfico que simplesmente
   começa no meio parece quebrado. A faixa cinza diz que ali não faltou internet:
   faltou monitor. */
function primeiroDado(series) {
  let menor = null;
  for (const nome in series) {
    for (const p of series[nome]) {
      if (p[1] === null || p[1] === undefined) continue;
      if (menor === null || p[0] < menor) menor = p[0];
      break;
    }
  }
  return menor;
}

function faixaSemDados(svg, series, X, M, ih, t0, t1, comTexto) {
  const inicio = primeiroDado(series);
  if (inicio === null || inicio <= t0 + (t1 - t0) * 0.02) return;
  const largura = X(inicio) - X(t0);
  svg.appendChild(svgEl('rect', {
    x: X(t0), y: M.t, width: Math.max(0, largura), height: ih,
    fill: STATUS.sem, opacity: .35,
  }));
  if (comTexto && largura > 150) {
    const tx = svgEl('text', {
      x: X(t0) + largura / 2, y: M.t + ih / 2, 'text-anchor': 'middle',
      fill: '#898781', 'font-size': 12,
    });
    tx.textContent = 'sem monitoramento neste trecho';
    svg.appendChild(tx);
  }
}

/* ------------------------------------------------------------ latência */
function desenharLatencia(series, eventos, t0, t1) {
  const svg = $('g-lat');
  const { W, H } = medir(svg, 900, 280);
  const M = { t: 12, r: 14, b: 26, l: W < 420 ? 36 : 48 };   // eixo mais enxuto no celular
  limpar(svg);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  let ymax = 0;
  for (const nome in series) for (const p of series[nome]) if (p[3] !== null && p[3] !== undefined) ymax = Math.max(ymax, p[3]);
  if (!ymax) ymax = 50;
  ymax *= 1.12;

  const X = (t) => M.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
  const Y = (v) => M.t + ih - (v / ymax) * ih;

  faixaSemDados(svg, series, X, M, ih, t0, t1, true);

  // faixas de queda ao fundo
  eventos.filter((e) => e.type === 'QUEDA').forEach((e) => {
    const a = Math.max(e.started_at, t0), b = Math.min(e.ended_at || t1, t1);
    if (b <= a) return;
    svg.appendChild(svgEl('rect', {
      x: X(a), y: M.t, width: Math.max(1.5, X(b) - X(a)), height: ih,
      fill: STATUS.critical, opacity: .22,
    }));
  });

  // grade + eixo Y
  ticksY(ymax).forEach((v) => {
    svg.appendChild(svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? '#383835' : '#2c2c2a', 'stroke-width': 1 }));
    const tx = svgEl('text', { x: M.l - 8, y: Y(v) + 4, 'text-anchor': 'end', fill: '#898781', 'font-size': 11 });
    tx.textContent = nf(v, v < 10 ? 1 : 0);
    svg.appendChild(tx);
  });
  const un = svgEl('text', { x: M.l - 8, y: M.t - 2, 'text-anchor': 'end', fill: '#898781', 'font-size': 10 });
  un.textContent = 'ms';
  svg.appendChild(un);

  // eixo X
  const nX = Math.max(2, Math.min(7, Math.floor(iw / 120)));
  for (let i = 0; i <= nX; i++) {
    const t = t0 + ((t1 - t0) * i) / nX;
    const tx = svgEl('text', { x: X(t), y: H - 8, 'text-anchor': i === 0 ? 'start' : i === nX ? 'end' : 'middle', fill: '#898781', 'font-size': 11 });
    tx.textContent = rotuloTempo(t, t1 - t0);
    svg.appendChild(tx);
  }

  // séries: banda min–max + linha da média
  for (const nome in series) {
    const pts = series[nome];
    const cor = CORES[nome];
    let banda = '', volta = [], aberto = false;
    pts.forEach((p) => {
      const [ts, avg, mn, mx] = p;
      if (mn === null || mx === null || mn === undefined || mx === undefined) { aberto = false; return; }
      banda += (aberto ? 'L' : 'M') + X(ts).toFixed(1) + ' ' + Y(mx).toFixed(1) + ' ';
      volta.push([X(ts), Y(mn)]);
      aberto = true;
    });
    if (volta.length > 1) {
      for (let i = volta.length - 1; i >= 0; i--) banda += 'L' + volta[i][0].toFixed(1) + ' ' + volta[i][1].toFixed(1) + ' ';
      banda += 'Z';
      svg.appendChild(svgEl('path', { d: banda, fill: cor, opacity: .16, stroke: 'none' }));
    }
    let d = '', ab = false;
    pts.forEach((p) => {
      if (p[1] === null || p[1] === undefined) { ab = false; return; }
      d += (ab ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1) + ' ';
      ab = true;
    });
    if (d) svg.appendChild(svgEl('path', { d, fill: 'none', stroke: cor, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  }

  ligarCrosshair(svg, $('tt-lat'), $('wrap-lat'), series, X, M, ih, t0, t1, 'ms', 1);
}

/* ------------------------------------------------------------ perda */
function desenharPerda(series, t0, t1) {
  const svg = $('g-perda');
  const { W, H } = medir(svg, 900, 150);
  const M = { t: 10, r: 14, b: 24, l: W < 420 ? 36 : 48 };
  limpar(svg);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const X = (t) => M.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
  const Y = (v) => M.t + ih - (v / 100) * ih;

  faixaSemDados(svg, series, X, M, ih, t0, t1, false);

  [0, 50, 100].forEach((v) => {
    svg.appendChild(svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? '#383835' : '#2c2c2a', 'stroke-width': 1 }));
    const tx = svgEl('text', { x: M.l - 8, y: Y(v) + 4, 'text-anchor': 'end', fill: '#898781', 'font-size': 11 });
    tx.textContent = v + '%';
    svg.appendChild(tx);
  });

  const nomes = Object.keys(series);
  const total = Math.max(1, Math.max(...nomes.map((n) => series[n].length)));
  const lg = Math.max(1.2, (iw / total) * 0.42);
  nomes.forEach((nome, idx) => {
    const cor = CORES[nome];
    series[nome].forEach((p) => {
      const perda = p[4];
      if (!perda) return;
      const alt = Math.max(1.5, (perda / 100) * ih);
      // 2px de folga entre as barras dos dois links (regra do spacer)
      const x = X(p[0]) + (idx === 0 ? -lg - 1 : 1);
      svg.appendChild(svgEl('rect', { x, y: M.t + ih - alt, width: lg, height: alt, fill: cor, rx: Math.min(2, lg / 2), opacity: .92 }));
    });
  });

  const nX = Math.max(2, Math.min(6, Math.floor(iw / 130)));
  for (let i = 0; i <= nX; i++) {
    const t = t0 + ((t1 - t0) * i) / nX;
    const tx = svgEl('text', { x: X(t), y: H - 7, 'text-anchor': i === 0 ? 'start' : i === nX ? 'end' : 'middle', fill: '#898781', 'font-size': 11 });
    tx.textContent = rotuloTempo(t, t1 - t0);
    svg.appendChild(tx);
  }

  ligarCrosshair(svg, $('tt-perda'), $('wrap-perda'), series, X, M, ih, t0, t1, '%', 4);
}

/* ------------------------------------------------------------ crosshair */
function ligarCrosshair(svg, tt, wrap, series, X, M, ih, t0, t1, unidade, campo) {
  const linha = svgEl('line', { y1: M.t, y2: M.t + ih, stroke: '#c3c2b7', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0 });
  svg.appendChild(linha);

  function mover(ev) {
    const r = svg.getBoundingClientRect();
    const px = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) * (svg.viewBox.baseVal.width / r.width);
    const frac = (px - M.l) / Math.max(1, X(t1) - X(t0));
    const t = t0 + frac * (t1 - t0);
    if (t < t0 || t > t1) return esconder();

    const linhas = [];
    let melhorX = null;
    for (const nome in series) {
      const pts = series[nome];
      if (!pts.length) continue;
      let melhor = null, dist = Infinity;
      for (const p of pts) {
        const dd = Math.abs(p[0] - t);
        if (dd < dist) { dist = dd; melhor = p; }
      }
      if (!melhor || dist > (t1 - t0) / 40) continue;
      melhorX = melhorX === null ? X(melhor[0]) : melhorX;
      const v = melhor[campo];
      linhas.push(`<div class="tt-linha"><i style="background:${CORES[nome]}"></i>${nome}: <strong>${v === null || v === undefined ? 'sem resposta' : nf(v, 1) + ' ' + unidade}</strong></div>`);
      if (campo === 1 && melhor[4] > 0) linhas[linhas.length - 1] = linhas[linhas.length - 1].replace('</div>', ` <span style="color:#ff9b9b">perda ${nf(melhor[4], 0)}%</span></div>`);
    }
    if (!linhas.length) return esconder();

    linha.setAttribute('x1', melhorX);
    linha.setAttribute('x2', melhorX);
    linha.setAttribute('opacity', .6);

    let alvo = t0;
    for (const nome in series) for (const p of series[nome]) if (Math.abs(p[0] - t) < Math.abs(alvo - t)) alvo = p[0];
    tt.innerHTML = `<div class="tt-tit">${fmtDataHora(alvo)}</div>${linhas.join('')}`;
    tt.classList.remove('oculto');
    const wr = wrap.getBoundingClientRect();
    const left = Math.min(Math.max(0, (ev.touches ? ev.touches[0].clientX : ev.clientX) - wr.left + 14), wr.width - tt.offsetWidth - 4);
    tt.style.left = left + 'px';
    tt.style.top = '8px';
  }
  function esconder() {
    tt.classList.add('oculto');
    linha.setAttribute('opacity', 0);
  }
  svg.addEventListener('mousemove', mover);
  svg.addEventListener('touchmove', mover, { passive: true });
  svg.addEventListener('mouseleave', esconder);
  svg.addEventListener('touchend', esconder);
}

/* ------------------------------------------------------------ timeline */
function desenharTimeline(series, eventos, t0, t1) {
  const box = $('timelines');
  limpar(box);
  // mesmo corte do style.css; em janelas curtas o número de barras cai para o
  // de amostras existentes, senão a maioria apareceria como "sem dados"
  const teto = window.innerWidth < 700 ? 48 : 90;
  const N = Math.max(8, Math.min(teto, Math.round((t1 - t0) / INTERVALO_AMOSTRA)));
  const passo = (t1 - t0) / N;

  ['GIGA', 'IMPACTO'].forEach((nome) => {
    const pts = series[nome] || [];
    const cobertos = new Set();
    pts.forEach((p) => cobertos.add(Math.floor((p[0] - t0) / passo)));

    const linha = document.createElement('div');
    linha.className = 'tl-linha';
    const evs = eventos.filter((e) => e.link === nome);
    let html = `<div class="tl-rot"><span style="color:${CORES[nome]}">${nome}</span><span class="muted">${rotuloTempo(t0, t1 - t0)} → agora</span></div><div class="tl-barras">`;
    for (let i = 0; i < N; i++) {
      const a = t0 + i * passo, b = a + passo;
      const queda = evs.some((e) => e.type === 'QUEDA' && e.started_at < b && (e.ended_at || t1) > a);
      const deg = evs.some((e) => e.type === 'LATENCIA_ALTA' && e.started_at < b && (e.ended_at || t1) > a);
      let cor = STATUS.sem, txt = 'sem dados';
      if (queda) { cor = STATUS.critical; txt = 'fora do ar'; }
      else if (deg) { cor = STATUS.warning; txt = 'degradado'; }
      else if (cobertos.has(i)) { cor = STATUS.good; txt = 'no ar'; }
      html += `<div class="tl-b" style="background:${cor}" title="${fmtDataHora(Math.floor(a))} — ${txt}"></div>`;
    }
    linha.innerHTML = html + '</div>';
    box.appendChild(linha);
  });
}

/* ------------------------------------------------------------ dados */
async function pegar(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(url + ' → ' + r.status);
  return r.json();
}

/* Redesenha do cache, sem ir à rede. Serve para o ResizeObserver: se a primeira
   pintura pegou o quadro com o tamanho errado, a segunda conserta sozinha. */
function redesenhar() {
  const g = estado.ultimoGrafico;
  if (!g) return;
  desenharLatencia(g.series, g.eventos, g.t0, g.t1);
  desenharPerda(g.series, g.t0, g.t1);
  desenharTimeline(g.series, g.eventos, g.t0, g.t1);
  desenharSpark('GIGA', (g.series.GIGA || []).slice(-180));
  desenharSpark('IMPACTO', (g.series.IMPACTO || []).slice(-180));
}

async function carregarGraficos() {
  const t1 = Math.floor(Date.now() / 1000);
  const t0 = t1 - estado.span;
  try {
    const [g, i, ev] = await Promise.all([
      pegar(`/api/samples?link=GIGA&from=${t0}&to=${t1}`),
      pegar(`/api/samples?link=IMPACTO&from=${t0}&to=${t1}`),
      pegar(`/api/events?from=${t0 - 86400}&limit=500`),
    ]);
    const series = { GIGA: g.points, IMPACTO: i.points };
    estado.serieAtual = series;
    estado.ultimoGrafico = { series, eventos: ev.events, t0, t1 };
    redesenhar();
  } catch (e) {
    console.error('falha carregando gráficos', e);
  }
}

/* ------------------------------------------------------------ estatísticas
   Duas famílias de cor convivem aqui, e cada uma tem um trabalho só:
     · a cor do link (azul/laranja) diz DE QUEM é o número — identidade;
     · a cor de status (verde/âmbar/vermelho) diz se o número está bom —
       sempre acompanhada de um símbolo, nunca cor sozinha.
   Os limiares de qualidade saem das Configurações, não de números chutados. */
const FAIXAS = { bom: '✔', medio: '!', ruim: '✕', neutro: '–' };

const escala = (v, bom, medio) => (v <= bom ? 'bom' : v <= medio ? 'medio' : 'ruim');

const GRUPOS = [
  {
    titulo: 'Disponibilidade', icone: '🟢', linhas: [
      { k: 'uptime_pct', rot: 'Uptime', fmt: (v) => nf(v, 3) + '%', forte: true,
        melhor: 'maior', q: (v) => (v >= 99.9 ? 'bom' : v >= 99 ? 'medio' : 'ruim') },
      { k: 'quedas', rot: 'Quedas no período', fmt: (v) => nf(v, 0), barra: true,
        melhor: 'menor', q: (v) => escala(v, 0, 2) },
      { k: 'downtime_s', rot: 'Tempo total fora do ar', fmt: fmtDur, barra: true,
        melhor: 'menor', q: (v) => escala(v, 0, 60) },
      { k: 'maior_queda_s', rot: 'Maior queda', fmt: fmtDur, barra: true,
        melhor: 'menor', q: (v) => escala(v, 0, 60) },
      { k: 'degradacoes', rot: 'Períodos de latência alta', fmt: (v) => nf(v, 0),
        barra: true, melhor: 'menor', q: (v) => escala(v, 0, 2) },
    ],
  },
  {
    titulo: 'Latência', icone: '⏱️', linhas: [
      { k: 'rtt_avg', rot: 'Latência média', fmt: (v) => nf(v, 2) + ' ms', forte: true,
        barra: true, melhor: 'menor',
        q: (v) => escala(v, estado.limiares.lat * 0.5, estado.limiares.lat) },
      { k: 'rtt_min', rot: 'Latência mínima', fmt: (v) => nf(v, 2) + ' ms', barra: true,
        melhor: 'menor',
        q: (v) => escala(v, estado.limiares.lat * 0.5, estado.limiares.lat) },
      { k: 'rtt_max', rot: 'Latência máxima', fmt: (v) => nf(v, 2) + ' ms', barra: true,
        melhor: 'menor',
        q: (v) => escala(v, estado.limiares.lat, estado.limiares.lat * 3) },
      { k: 'jitter_avg', rot: 'Jitter médio', fmt: (v) => nf(v, 2) + ' ms', barra: true,
        melhor: 'menor',
        q: (v) => escala(v, estado.limiares.jit * 0.4, estado.limiares.jit) },
    ],
  },
  {
    titulo: 'Qualidade do sinal', icone: '📶', linhas: [
      { k: 'loss_avg', rot: 'Perda média', fmt: (v) => nf(v, 2) + '%', barra: true,
        melhor: 'menor', q: (v) => escala(v, 0.1, estado.limiares.loss) },
      { k: 'dns_avg', rot: 'Resolução DNS média', fmt: (v) => nf(v, 2) + ' ms',
        barra: true, melhor: 'menor', q: (v) => escala(v, 30, 80) },
    ],
  },
  {
    titulo: 'Cobertura da medição', icone: '🧭', linhas: [
      { k: 'amostras', rot: 'Amostras coletadas', fmt: (v) => nf(v, 0), barra: true },
      { k: 'cobertura_pct', rot: 'Cobertura do monitoramento', fmt: (v) => nf(v, 1) + '%',
        melhor: 'maior', q: (v) => (v >= 95 ? 'bom' : v >= 70 ? 'medio' : 'ruim') },
    ],
  },
];

function celulaResumo(def, nome, valor, vals) {
  if (valor === null || valor === undefined) {
    return `<td class="cel"><span class="cel-vazio">—</span></td>`;
  }
  const qual = def.q ? def.q(valor) : 'neutro';
  const validos = vals.filter((v) => v !== null && v !== undefined);
  const teto = Math.max(...validos.map(Math.abs), 0);
  const largura = def.barra && teto > 0 ? Math.round((Math.abs(valor) / teto) * 100) : 0;
  let melhor = false;
  if (def.melhor && validos.length === 2 && validos[0] !== validos[1]) {
    const alvo = def.melhor === 'maior' ? Math.max(...validos) : Math.min(...validos);
    melhor = valor === alvo;
  }
  return `<td class="cel${melhor ? ' cel-melhor' : ''}">
      ${largura ? `<span class="cel-barra" style="width:${largura}%;background:${CORES[nome]}"></span>` : ''}
      <span class="cel-conteudo">
        <span class="chip q-${qual}" title="${qual}">${FAIXAS[qual]}</span>
        <span class="valor${def.forte ? ' destaque' : ''}">${def.fmt(valor)}</span>
        ${melhor ? '<span class="pill-melhor">melhor</span>' : ''}
      </span></td>`;
}

async function carregarResumo() {
  const t1 = Math.floor(Date.now() / 1000), t0 = t1 - estado.span;
  try {
    const r = await pegar(`/api/summary?period=custom&from=${t0}&to=${t1}`);
    const tb = $('tab-resumo').querySelector('tbody');
    limpar(tb);
    GRUPOS.forEach((g) => {
      const cab = document.createElement('tr');
      cab.className = 'linha-grupo';
      cab.innerHTML = `<th colspan="3" scope="colgroup"><span class="g-icone">${g.icone}</span>${g.titulo}</th>`;
      tb.appendChild(cab);

      g.linhas.forEach((def) => {
        const vals = ['GIGA', 'IMPACTO'].map((n) => (r.links[n] ? r.links[n][def.k] : null));
        const tr = document.createElement('tr');
        tr.innerHTML = `<td class="metrica">${def.rot}</td>` +
          ['GIGA', 'IMPACTO'].map((n, i) => celulaResumo(def, n, vals[i], vals)).join('');
        tb.appendChild(tr);
      });
    });
    $('resumo-sub').textContent =
      `De ${fmtDataHora(t0)} até ${fmtDataHora(t1)} · uptime calculado sobre o tempo efetivamente monitorado.`;
  } catch (e) { console.error(e); }
}

/* ------------------------------------------------------------ velocidade */
const FASES = {
  preparando: 'preparando…', ping: 'medindo o ping…',
  download: 'baixando…', upload: 'enviando…',
};

/* Uma barra só para o teste inteiro: ping é rápido, download e upload valem
   metade cada um do que sobra. */
function pctGlobal(r) {
  const p = r.pct || 0;
  if (r.fase === 'download') return 0.1 + 0.45 * p;
  if (r.fase === 'upload') return 0.55 + 0.45 * p;
  if (r.fase === 'ping') return 0.08;
  return 0.02;
}

function velNum(rotulo, valor, unidade, cor, vivo) {
  return `<div class="vel-num${vivo ? ' vivo' : ''}">
      <div class="vel-valor" style="color:${cor}">${valor}<small> ${unidade}</small></div>
      <div class="met-rot">${rotulo}</div>
    </div>`;
}

function velHistorico(nome) {
  const lista = (estado.vel.historico[nome] || [])
    .filter((t) => t.down_mbps !== null && t.down_mbps !== undefined)
    .slice(0, 6).reverse();
  if (lista.length < 2) return '';
  const teto = Math.max(...lista.map((t) => t.down_mbps || 0)) || 1;
  const barras = lista.map((t) => {
    const h = Math.max(8, Math.round(((t.down_mbps || 0) / teto) * 100));
    return `<i style="height:${h}%;background:${CORES[nome]}" title="${fmtDataHora(t.ts)} — ${nf(t.down_mbps, 1)} Mbps ↓ / ${nf(t.up_mbps, 1)} Mbps ↑"></i>`;
  }).join('');
  return `<div class="vel-hist" aria-label="testes anteriores"><span class="met-rot">testes anteriores ↓</span><div class="vel-hist-barras">${barras}</div></div>`;
}

function renderVelocidade() {
  const box = $('vel-grid');
  limpar(box);
  ['GIGA', 'IMPACTO'].forEach((nome) => {
    const cor = CORES[nome];
    const rod = estado.vel.rodando && estado.vel.rodando.link === nome ? estado.vel.rodando : null;
    const ok = estado.vel.ultimos[nome] || null;
    const ultimo = (estado.vel.historico[nome] || [])[0] || null;
    const parcial = rod && rod.mbps ? nf(rod.mbps, 1) : null;

    const down = rod && rod.fase === 'download' && parcial ? parcial
      : ok ? nf(ok.down_mbps, 1) : '—';
    const up = rod && rod.fase === 'upload' && parcial ? parcial
      : ok ? nf(ok.up_mbps, 1) : '—';
    const ping = rod && rod.ping_ms ? nf(rod.ping_ms, 1) : ok ? nf(ok.ping_ms, 1) : '—';

    const art = document.createElement('article');
    art.className = 'vel' + (rod ? ' rodando' : '');
    art.style.setProperty('--cor-link', cor);
    art.innerHTML = `
      <div class="vel-topo">
        <span class="ponto-link" style="background:${cor}"></span>
        <b class="vel-nome" style="color:${cor}">${nome}</b>
        ${rod ? `<span class="vel-fase">${FASES[rod.fase] || rod.fase}</span>` : ''}
        <button class="btn btn-primario vel-btn" type="button" data-link="${nome}"
          ${estado.vel.rodando ? 'disabled' : ''}>${rod ? 'testando…' : 'Testar velocidade'}</button>
      </div>
      <div class="vel-nums">
        ${velNum('↓ download', down, 'Mbps', cor, rod && rod.fase === 'download')}
        ${velNum('↑ upload', up, 'Mbps', cor, rod && rod.fase === 'upload')}
        ${velNum('↔ ping', ping, 'ms', 'var(--ink)', rod && rod.fase === 'ping')}
      </div>
      <div class="vel-prog${rod ? '' : ' oculto'}"><i style="width:${rod ? Math.round(pctGlobal(rod) * 100) : 0}%;background:${cor}"></i></div>
      <div class="vel-rodape">
        ${ultimo && ultimo.erro && ultimo.down_mbps === null
          ? `<span class="vel-erro">❌ último teste falhou: ${ultimo.erro}</span>`
          : ok
            ? `<span>último teste <b>${fmtDataHora(ok.ts)}</b></span>`
              + `<span>via <b>${ok.servidor || '—'}</b></span>`
              + (ok.erro ? `<span class="vel-erro">⚠️ ${ok.erro}</span>` : '')
            : '<span>nenhum teste feito ainda</span>'}
      </div>
      ${velHistorico(nome)}`;
    box.appendChild(art);
  });
}

function agruparVel(lista) {
  const out = { GIGA: [], IMPACTO: [] };
  lista.forEach((t) => { if (out[t.link]) out[t.link].push(t); });
  return out;
}

async function carregarVelocidade() {
  try {
    const r = await pegar('/api/speedtest?limit=40');
    estado.vel.rodando = r.rodando;
    estado.vel.ultimos = r.ultimos || {};
    estado.vel.historico = agruparVel(r.historico || []);
    renderVelocidade();
  } catch (e) { console.error(e); }
}

async function testarVelocidade(nome) {
  const msg = $('v-msg');
  msg.textContent = `iniciando o teste da ${nome}…`;
  msg.style.color = '';
  try {
    const r = await fetch('/api/speedtest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link: nome, dur: parseFloat($('v-dur').value) }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    estado.vel.rodando = { link: nome, fase: 'preparando', pct: 0 };
    renderVelocidade();
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ff9b9b';
  }
}

async function carregarEventos() {
  const link = $('f-link').value, tipo = $('f-tipo').value;
  const q = new URLSearchParams({ limit: '25', offset: String(estado.offset) });
  if (link) q.set('link', link);
  if (tipo) q.set('tipo', tipo);
  try {
    const r = await pegar('/api/events?' + q);
    estado.total = r.total;
    const tb = $('tab-eventos').querySelector('tbody');
    limpar(tb);
    if (!r.events.length) {
      tb.innerHTML = '<tr><td colspan="6" class="muted vazio">Nenhum evento registrado — ótimo sinal.</td></tr>';
    }
    // data-rot vira o rótulo das linhas empilhadas no celular (ver style.css)
    r.events.forEach((e) => {
      const tr = document.createElement('tr');
      const aberto = !e.ended_at;
      tr.innerHTML = `
        <td data-rot="Link"><span class="ponto-link" style="background:${CORES[e.link] || '#888'}"></span>${e.link}</td>
        <td data-rot="Tipo"><span class="tag tag-${e.type}">${e.type === 'QUEDA' ? 'Queda' : 'Latência alta'}</span>${e.flapping ? ' <span class="tag tag-aberto">instável</span>' : ''}</td>
        <td data-rot="Início">${fmtDataHora(e.started_at)}</td>
        <td data-rot="Fim">${aberto ? '<span class="tag tag-aberto">em andamento</span>' : fmtDataHora(e.ended_at)}</td>
        <td data-rot="Duração" class="destaque">${aberto ? fmtDur(Math.floor(Date.now() / 1000) - e.started_at) : fmtDur(e.duration_s)}</td>
        <td data-rot="Causa">${CAUSA[e.cause] || e.cause || '—'}</td>`;
      tb.appendChild(tr);
    });
    const ini = estado.total ? estado.offset + 1 : 0;
    $('ev-info').textContent = `${ini}–${Math.min(estado.offset + 25, estado.total)} de ${estado.total}`;
    $('ev-prev').disabled = estado.offset <= 0;
    $('ev-next').disabled = estado.offset + 25 >= estado.total;
  } catch (e) { console.error(e); }
}

/* ------------------------------------------------------------ alertas */
function tocarBipe() {
  if (!estado.somOn || estado.silenciado) return;
  try {
    if (!estado.audioCtx) estado.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = estado.audioCtx;
    if (ctx.state === 'suspended') ctx.resume();
    [0, 0.28, 0.56].forEach((atraso) => {
      const osc = ctx.createOscillator(), gan = ctx.createGain();
      osc.type = 'square';
      osc.frequency.value = 880;
      gan.gain.setValueAtTime(0.0001, ctx.currentTime + atraso);
      gan.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + atraso + 0.02);
      gan.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + atraso + 0.2);
      osc.connect(gan); gan.connect(ctx.destination);
      osc.start(ctx.currentTime + atraso);
      osc.stop(ctx.currentTime + atraso + 0.22);
    });
  } catch (e) { /* sem áudio disponível */ }
}

function piscarTitulo(texto) {
  // idempotente: atualizarBanner roda a cada segundo e reiniciar o intervalo
  // aqui faria o título nunca chegar a alternar
  if (estado.piscando && estado.piscaTexto === texto) return;
  pararPisca();
  estado.piscaTexto = texto;
  let on = false;
  estado.piscando = setInterval(() => {
    document.title = (on = !on) ? texto : estado.tituloBase;
  }, 1000);
}
function pararPisca() {
  if (estado.piscando) clearInterval(estado.piscando);
  estado.piscando = null;
  estado.piscaTexto = null;
  document.title = estado.tituloBase;
}

function notificar(titulo, corpo) {
  try {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(titulo, { body: corpo, tag: 'netmon', renotify: true });
    }
  } catch (e) { /* navegador bloqueou */ }
}

/* Deriva o banner do estado atual: se você abrir a página com um link caído,
   o aviso aparece do mesmo jeito — não depende de ter recebido o evento SSE. */
function atualizarBanner(links) {
  const caidos = links.filter((l) => l.state === 'DOWN' || l.state === 'NO_LINK');
  const degradados = links.filter((l) => l.state === 'DEGRADED');
  const barra = $('alerta');

  if (caidos.length) {
    const l = caidos[0];
    const desde = l.evento_aberto ? l.evento_aberto.started_at : l.state_since;
    const causa = l.evento_aberto && l.evento_aberto.cause ? CAUSA[l.evento_aberto.cause] : null;
    barra.className = 'alerta';
    $('alerta-icone').textContent = '🔴';
    $('alerta-titulo').textContent =
      caidos.length > 1
        ? `OS DOIS LINKS ESTÃO FORA DO AR (${caidos.map((x) => x.name).join(' e ')})`
        : `${l.name} CAIU às ${fmtHora(desde)}`;
    $('alerta-detalhe').textContent =
      `fora do ar há ${fmtDurLonga(Math.floor(Date.now() / 1000) - desde)}` + (causa ? ` · ${causa}` : '');
    piscarTitulo('🔴 QUEDA — ' + caidos.map((x) => x.name).join(', '));
    return;
  }
  if (degradados.length) {
    const l = degradados[0];
    barra.className = 'alerta aviso';
    $('alerta-icone').textContent = '🟡';
    $('alerta-titulo').textContent = `${l.name} com latência alta`;
    $('alerta-detalhe').textContent =
      `${nf(l.rtt_ewma || l.rtt_avg, 0)} ms · perda ${nf(l.loss, 0)}% · há ${fmtDurLonga(Math.floor(Date.now() / 1000) - l.state_since)}`;
    pararPisca();
    return;
  }
  barra.className = 'alerta oculto';
  estado.silenciado = false;
  pararPisca();
}

/* ------------------------------------------------------------ SSE */
function conectar() {
  const es = new EventSource('/api/stream');

  es.addEventListener('open', () => {
    $('conexao').textContent = 'ao vivo';
    $('conexao').className = 'pill pill-on';
  });
  es.addEventListener('error', () => {
    $('conexao').textContent = 'reconectando…';
    $('conexao').className = 'pill pill-off';
  });

  es.addEventListener('status', (ev) => {
    const d = JSON.parse(ev.data);
    // mantém uptime/última queda quando o tick vier sem eles (só chegam a cada 60s)
    d.links.forEach((novo) => {
      const antigo = estado.links.find((x) => x.name === novo.name);
      if (antigo) {
        if (!novo.uptime) novo.uptime = antigo.uptime;
        if (!novo.ultima_queda) novo.ultima_queda = antigo.ultima_queda;
      }
    });
    estado.links = d.links;
    estado.porta = d.porta;
    renderCards(d.links);
    atualizarBanner(d.links);
    if (estado.serieAtual) {
      desenharSpark('GIGA', (estado.serieAtual.GIGA || []).slice(-180));
      desenharSpark('IMPACTO', (estado.serieAtual.IMPACTO || []).slice(-180));
    }
  });

  es.addEventListener('speedtest', (ev) => {
    const d = JSON.parse(ev.data);
    const msg = $('v-msg');
    if (d.fase === 'fim' || d.fase === 'erro') {
      estado.vel.rodando = null;
      if (d.fase === 'fim') {
        msg.textContent = `✅ ${d.link}: ${nf(d.down_mbps, 1)} Mbps de download · `
          + `${nf(d.up_mbps, 1)} Mbps de upload · ping ${nf(d.ping_ms, 1)} ms`;
        msg.style.color = '#7ee07e';
      } else {
        msg.textContent = `❌ ${d.link}: ${d.erro}`;
        msg.style.color = '#ff9b9b';
      }
      carregarVelocidade();
      return;
    }
    estado.vel.rodando = d;
    if (msg.textContent.startsWith('iniciando')) msg.textContent = '';
    renderVelocidade();
  });

  es.addEventListener('alerta', (ev) => {
    const a = JSON.parse(ev.data);
    if (a.event === 'queda') {
      estado.silenciado = false;
      tocarBipe();
      notificar('🔴 ' + a.link + ' caiu', a.mensagem);
    } else if (a.event === 'recuperacao') {
      notificar('🟢 ' + a.link + ' voltou', a.mensagem);
    }
    carregarEventos();
    carregarResumo();
    setTimeout(carregarGraficos, 1500);
  });
}

/* ------------------------------------------------------------ config */
async function carregarConfig() {
  try {
    const c = await pegar('/api/config');
    $('c-webhook').value = c.webhook_url || '';
    $('c-webhook-on').checked = c.webhook_enabled === '1';
    $('c-lat').value = parseFloat(c.lat_limiar_ms);
    $('c-loss').value = parseFloat(c.loss_limiar_pct);
    $('c-jit').value = parseFloat(c.jitter_limiar_ms);
    $('c-cool').value = parseFloat(c.cooldown_s);
    $('c-som').checked = c.som_habilitado === '1';
    estado.somOn = c.som_habilitado === '1';
    estado.limiares = {
      lat: parseFloat(c.lat_limiar_ms) || 80,
      loss: parseFloat(c.loss_limiar_pct) || 20,
      jit: parseFloat(c.jitter_limiar_ms) || 60,
    };
  } catch (e) { console.error(e); }
}

async function salvarConfig() {
  const corpo = {
    webhook_url: $('c-webhook').value.trim(),
    webhook_enabled: $('c-webhook-on').checked ? '1' : '0',
    lat_limiar_ms: $('c-lat').value,
    loss_limiar_pct: $('c-loss').value,
    jitter_limiar_ms: $('c-jit').value,
    cooldown_s: $('c-cool').value,
    som_habilitado: $('c-som').checked ? '1' : '0',
  };
  const msg = $('c-msg');
  try {
    const r = await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(corpo),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    estado.somOn = j.som_habilitado === '1';
    estado.limiares = {
      lat: parseFloat(j.lat_limiar_ms) || 80,
      loss: parseFloat(j.loss_limiar_pct) || 20,
      jit: parseFloat(j.jitter_limiar_ms) || 60,
    };
    carregarResumo();                 // as cores da tabela seguem os limiares
    msg.textContent = '✅ salvo';
    msg.style.color = '#7ee07e';
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ff9b9b';
  }
  setTimeout(() => { msg.textContent = ''; }, 4000);
}

async function testarWebhook() {
  const msg = $('c-msg');
  msg.textContent = 'enviando…';
  msg.style.color = '';
  try {
    const r = await fetch('/api/webhook/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ webhook_url: $('c-webhook').value.trim() }),
    });
    const j = await r.json();
    if (j.ok) { msg.textContent = `✅ entregue (HTTP ${j.status_code})`; msg.style.color = '#7ee07e'; }
    else { msg.textContent = `❌ falhou: ${j.erro || j.status_code || j.erro}`; msg.style.color = '#ff9b9b'; }
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ff9b9b';
  }
}

/* ------------------------------------------------------------ manutenção */
function msgManut(txt, cor) {
  const m = $('m-msg');
  m.textContent = txt;
  m.style.color = cor || '';
}

function nomeDoCabecalho(resp, padrao) {
  const m = /filename="([^"]+)"/.exec(resp.headers.get('Content-Disposition') || '');
  return m ? m[1] : padrao;
}

/* Baixa via fetch+blob em vez de navegar até a URL: assim dá para mostrar
   "gerando…" e, principalmente, exibir o erro do servidor em vez de abrir
   uma aba em branco. */
async function baixar(url, padrao, rotulo, botao) {
  const original = botao.textContent;
  botao.disabled = true;
  botao.textContent = 'gerando…';
  msgManut('');
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) {
      let erro = 'HTTP ' + r.status;
      try { erro = (await r.json()).erro || erro; } catch (_) { /* corpo não-JSON */ }
      throw new Error(erro);
    }
    const blob = await r.blob();
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = nomeDoCabecalho(r, padrao);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
    msgManut(`✅ ${rotulo} baixado (${(blob.size / 1024).toFixed(0)} KB)`, '#7ee07e');
  } catch (e) {
    msgManut('❌ ' + e.message, '#ff9b9b');
  } finally {
    botao.disabled = false;
    botao.textContent = original;
  }
}

async function resetar() {
  const btn = $('btn-reset-ok');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'apagando…';
  try {
    const r = await fetch('/api/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmar: 'APAGAR', escopo: $('r-escopo').value }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    msgManut('✅ ' + j.mensagem, '#7ee07e');
    $('caixa-confirma').classList.add('oculto');
    $('r-confirma').value = '';
    estado.offset = 0;
    await carregarConfig();
    await Promise.all([carregarGraficos(), carregarResumo(), carregarEventos()]);
  } catch (e) {
    msgManut('❌ ' + e.message, '#ff9b9b');
  } finally {
    btn.disabled = true;               // volta travado: exige digitar APAGAR de novo
    btn.textContent = original;
  }
}

/* ------------------------------------------------------------ init */
function ligarEventos() {
  document.querySelectorAll('.btn-per').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.btn-per').forEach((x) => x.classList.remove('ativo'));
      b.classList.add('ativo');
      estado.span = parseInt(b.dataset.span, 10);
      agendarAtualizacoes();
      carregarGraficos();
      carregarResumo();
    });
  });

  $('vel-grid').addEventListener('click', (ev) => {
    const btn = ev.target.closest('.vel-btn');
    if (btn && !btn.disabled) testarVelocidade(btn.dataset.link);
  });

  $('btn-silenciar').addEventListener('click', () => {
    estado.silenciado = true;
    $('btn-silenciar').textContent = 'Som silenciado';
    setTimeout(() => { $('btn-silenciar').textContent = 'Silenciar som'; }, 3000);
  });
  $('btn-fechar-alerta').addEventListener('click', () => {
    $('alerta').classList.add('oculto');
    pararPisca();
  });

  $('btn-notif').addEventListener('click', async () => {
    if (!('Notification' in window)) { $('btn-notif').textContent = 'sem suporte'; return; }
    const p = await Notification.requestPermission();
    $('btn-notif').textContent = p === 'granted' ? '🔔 notificações ativas' : '🔕 bloqueadas';
    if (p === 'granted') notificar('netmon', 'Notificações ativadas. Você será avisado nas quedas.');
    if (!estado.audioCtx) tocarBipe();   // destrava o áudio no gesto do usuário
  });

  $('f-link').addEventListener('change', () => { estado.offset = 0; carregarEventos(); });
  $('f-tipo').addEventListener('change', () => { estado.offset = 0; carregarEventos(); });
  $('ev-prev').addEventListener('click', () => { estado.offset = Math.max(0, estado.offset - 25); carregarEventos(); });
  $('ev-next').addEventListener('click', () => { estado.offset += 25; carregarEventos(); });

  $('c-salvar').addEventListener('click', salvarConfig);
  $('c-testar').addEventListener('click', testarWebhook);

  $('btn-pdf').addEventListener('click', (ev) =>
    baixar('/api/report.pdf?period=' + $('r-periodo').value,
           'relatorio-netmon.pdf', 'Relatório', ev.currentTarget));
  document.querySelectorAll('.btn-pdf-link').forEach((b) => {
    b.addEventListener('click', (ev) => {
      const link = ev.currentTarget.dataset.link;
      const per = $('r-periodo').value;
      baixar(`/api/report.pdf?period=${per}&link=${link}`,
             `quedas-${link.toLowerCase()}.pdf`, `Relatório da ${link}`, ev.currentTarget);
    });
  });
  $('btn-logs').addEventListener('click', (ev) =>
    baixar('/api/logs', 'netmon.log', 'Log', ev.currentTarget));

  $('btn-reset').addEventListener('click', () => {
    const cx = $('caixa-confirma');
    cx.classList.toggle('oculto');
    if (!cx.classList.contains('oculto')) $('r-confirma').focus();
  });
  $('btn-reset-cancela').addEventListener('click', () => {
    $('caixa-confirma').classList.add('oculto');
    $('r-confirma').value = '';
    $('btn-reset-ok').disabled = true;
    msgManut('');
  });
  $('r-confirma').addEventListener('input', (ev) => {
    $('btn-reset-ok').disabled = ev.target.value.trim().toUpperCase() !== 'APAGAR';
  });
  $('btn-reset-ok').addEventListener('click', resetar);

  let redraw;
  window.addEventListener('resize', () => {
    clearTimeout(redraw);
    redraw = setTimeout(carregarGraficos, 250);
  });

  // a caixa do gráfico pode mudar sem a janela mudar (fonte carregando, barra de
  // rolagem aparecendo, painel abrindo). Aí o desenho antigo fica em escala errada
  if ('ResizeObserver' in window) {
    const obs = new ResizeObserver(() => {
      clearTimeout(estado.timers.redesenho);
      estado.timers.redesenho = setTimeout(redesenhar, 80);
    });
    ['wrap-lat', 'wrap-perda'].forEach((id) => obs.observe($(id)));
  }

  if ('Notification' in window && Notification.permission === 'granted') {
    $('btn-notif').textContent = '🔔 notificações ativas';
  }
}

/* Cadência de recarga: uma janela de 30 s precisa ser redesenhada a cada ciclo
   de sondagem; uma de 30 dias, não — seria só gastar CPU do Orange Pi. */
function agendarAtualizacoes() {
  clearInterval(estado.timers.graficos);
  clearInterval(estado.timers.resumo);
  const curto = estado.span <= SPAN_CURTO;
  estado.timers.graficos = setInterval(carregarGraficos, curto ? 2000 : 60000);
  estado.timers.resumo = setInterval(carregarResumo, curto ? 5000 : 60000);
}

function relogio() {
  $('relogio').textContent = new Date().toLocaleTimeString('pt-BR', { hour12: false });
  if (estado.links.length) atualizarBanner(estado.links);
}

async function iniciar() {
  ligarEventos();
  await carregarConfig();
  try {
    const s = await pegar('/api/status');
    estado.links = s.links;
    estado.porta = s.porta;
    renderCards(s.links);
    atualizarBanner(s.links);
    $('rodape-info').textContent =
      `netmon · servidor no ar há ${fmtDurLonga(s.servidor_uptime_s)} · porta ${s.porta}` +
      (s.port_fallback ? ' (porta 666 indisponível — veja o README)' : '');
  } catch (e) { console.error(e); }
  await Promise.all([carregarGraficos(), carregarResumo(), carregarEventos(),
                     carregarVelocidade()]);
  conectar();
  setInterval(relogio, 1000);
  setInterval(carregarEventos, 60000);
  agendarAtualizacoes();
}

document.addEventListener('DOMContentLoaded', iniciar);
