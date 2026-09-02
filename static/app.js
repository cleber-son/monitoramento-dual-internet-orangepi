/* netmon — front-end. Sem bibliotecas externas: tudo precisa funcionar
   justamente quando a internet cair. */
'use strict';

/* O ROTEADOR era violeta (#a98bff) e o azul da GIGA (#4da3ff) ficava a ΔE 1,5
   dele para quem tem deuteranopia — as duas linhas do gráfico eram literalmente
   a mesma cor, e mesmo com visão normal a distância (ΔE 11,5) ficava abaixo do
   piso de 15. O ciano abre as duas: ΔE 16,8 normal e 15,9 sob simulação, e
   ainda fica longe do verde de "no ar". */
const CORES = { GIGA: '#4da3ff', IMPACTO: '#ff7a45', ROTEADOR: '#37d6d6' };
/* Um link novo (outra operadora, outra placa) não pode sair sem cor: a paleta
   entra pela ordem em que o back-end devolve os links. */
const PALETA = ['#4da3ff', '#ff7a45', '#37d6d6', '#c8e04d', '#f78fb3'];
const corLink = (nome) =>
  CORES[nome] || PALETA[Math.max(0, nomesLinks().indexOf(nome)) % PALETA.length];

/* O painel não sabe quantos links existem: quem manda é o /api/status. Enquanto
   a primeira resposta não chega, os dois links de internet servem de palpite. */
function nomesLinks() {
  return estado.links.length ? estado.links.map((l) => l.name) : ['GIGA', 'IMPACTO'];
}
function linksPorTipo(kind) {
  return estado.links.filter((l) => l.kind === kind).map((l) => l.name);
}
function linksDeInternet() {
  const n = linksPorTipo('internet');
  return n.length ? n : ['GIGA', 'IMPACTO'];
}
const ehLan = (nome) => estado.links.some((l) => l.name === nome && l.kind === 'lan');
const STATUS = { good: '#2ecc71', warning: '#ffc531', critical: '#ff5a5a', sem: '#33415c' };
const ROTULO_ESTADO = { UP: 'NO AR', DEGRADED: 'DEGRADADO', DOWN: 'FORA DO AR', NO_LINK: 'SEM LINK' };
const CAUSA = {
  provedor: 'provável queda do provedor',
  roteador_local: 'roteador local não responde',
  cabo: 'sem link físico — verifique o cabo',
  teste_velocidade: 'link saturado pelo teste de velocidade (não conta contra a operadora)',
  troca_placa: 'placa de rede do link trocada nas configurações (não conta contra a operadora)',
};

const estado = {
  periodo: 'vivo',
  span: 120,
  inicioDados: null,
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
  vel: { ultimos: {}, historico: {}, rodando: null, log: [] },
  trace: {},
  ifaces: [],
  cfgScanRede: '',
  cfgLinks: [],
  scan: { redes: [], rede: null, ultimo: null, rodando: null, conhecidos: {},
          log: [], auto: null },
  timers: {},
};

/* ------------------------------------------------------------ período
   Um seletor só, no alto da página, mandando em todas as seções que falam de
   tempo: latência, perda, estatísticas, linha do tempo e histórico de quedas.
   Antes ele vivia dentro do painel de latência e parecia mandar só ali. */
const PERIODOS = [
  { id: 'vivo', rot: '● Ao vivo', curto: 'ao vivo', span: 120, vivo: true },
  { id: '1m',   rot: '1 min',     curto: '1 min',   span: 60 },
  { id: '10m',  rot: '10 min',    curto: '10 min',  span: 600 },
  { id: '30m',  rot: '30 min',    curto: '30 min',  span: 1800 },
  { id: '1h',   rot: '1 h',       curto: '1 hora',  span: 3600 },
  { id: '2h',   rot: '2 h',       curto: '2 horas', span: 7200 },
  { id: '24h',  rot: '24 h',      curto: '24 horas', span: 86400 },
  { id: '2d',   rot: '2 dias',    curto: '2 dias',  span: 172800 },
  { id: '7d',   rot: '7 dias',    curto: '7 dias',  span: 604800 },
  { id: '30d',  rot: '30 dias',   curto: '30 dias', span: 2592000 },
  { id: 'all',  rot: 'Tudo',      curto: 'tudo',    span: null },
];
const CHAVE_PERIODO = 'netmon.periodo';

function periodoAtual() {
  return PERIODOS.find((p) => p.id === estado.periodo) || PERIODOS[0];
}

/* A janela em epoch. "Tudo" começa na amostra mais antiga que o servidor ainda
   guarda (/api/status diz qual é); enquanto ele não responder, 30 dias servem
   de palpite — melhor um gráfico curto do que um gráfico vazio. */
function janela() {
  const t1 = Math.floor(Date.now() / 1000);
  const p = periodoAtual();
  const t0 = p.span ? t1 - p.span : (estado.inicioDados || t1 - 2592000);
  return { t0, t1, span: Math.max(30, t1 - t0), vivo: !!p.vivo };
}

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

/* ------------------------------------------------- recolher painéis */
// Toda seção vira recolhível pelo mesmo mecanismo. Antes algumas eram <details>
// nativos e outras não, o que dava dois comportamentos diferentes na mesma
// página — e o <details> não serve para as seções cujo cabeçalho tem campos (o
// traceroute tem <select> e <input> ali), porque clicar num campo dentro de um
// <summary> fecha a seção.
const RECOLHIDOS_PADRAO = [
  'O que está sendo medido',
  'NordVPN · Meshnet',
  'Histórico de quedas e alertas',
  'Configurações e alertas',
  'Relatórios e manutenção',
];
const CHAVE_RECOLHIDOS = 'netmon.recolhidos';

function lerRecolhidos() {
  // localStorage pode lançar (janela anônima, dados de site bloqueados): sem ele
  // a página continua funcionando, só não lembra o que estava fechado
  try {
    const cru = localStorage.getItem(CHAVE_RECOLHIDOS);
    if (cru) return JSON.parse(cru);
  } catch (e) { /* segue com o padrão */ }
  return null;
}

function gravarRecolhidos(lista) {
  try { localStorage.setItem(CHAVE_RECOLHIDOS, JSON.stringify(lista)); }
  catch (e) { /* sem persistência, e tudo bem */ }
}

function estadoRecolhidos() {
  const salvos = lerRecolhidos();
  return new Set(Array.isArray(salvos) ? salvos : RECOLHIDOS_PADRAO);
}

function secaoRecolhida(titulo) {
  return estadoRecolhidos().has(titulo);
}

function montarRecolhiveis() {
  const fechados = estadoRecolhidos();
  document.querySelectorAll('main > .painel').forEach((sec) => {
    if (sec.querySelector(':scope > .painel-cabeca')) return;   // já montado
    const h2 = sec.querySelector('h2');
    if (!h2) return;
    const titulo = h2.textContent.trim();

    const corpo = document.createElement('div');
    corpo.className = 'painel-corpo';
    while (sec.firstChild) corpo.appendChild(sec.firstChild);

    const cabeca = document.createElement('div');
    cabeca.className = 'painel-cabeca';
    cabeca.appendChild(h2);                       // sai do corpo, vai para a cabeça

    // um resumo curto continua visível com a seção fechada
    const resumo = corpo.querySelector('.resumo-cabeca');
    if (resumo) cabeca.appendChild(resumo);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'painel-toggle';
    btn.textContent = '▾';
    cabeca.appendChild(btn);

    // um .painel-topo que só existia para segurar o título fica vazio depois da
    // mudança e deixaria uma linha em branco no corpo
    corpo.querySelectorAll('.painel-topo').forEach((topo) => {
      if (!topo.textContent.trim() && !topo.querySelector('input, select, button')) {
        topo.remove();
      }
    });

    sec.appendChild(cabeca);
    sec.appendChild(corpo);

    const aplicar = (recolhido) => {
      sec.classList.toggle('recolhido', recolhido);
      btn.setAttribute('aria-expanded', String(!recolhido));
      btn.setAttribute('aria-label', (recolhido ? 'Abrir ' : 'Recolher ') + titulo);
    };
    aplicar(fechados.has(titulo));

    cabeca.addEventListener('click', () => {
      const agoraRecolhido = !sec.classList.contains('recolhido');
      aplicar(agoraRecolhido);
      const atual = estadoRecolhidos();
      if (agoraRecolhido) atual.add(titulo); else atual.delete(titulo);
      gravarRecolhidos([...atual]);
      // um SVG dentro de seção fechada mede 0 px: ao abrir, o desenho antigo
      // estaria na escala errada e precisa ser refeito
      if (!agoraRecolhido) {
        setTimeout(redesenhar, 30);
        // a lista de placas só é relida quando a seção abre: são vários `ip`
        // por vez, caros neste aparelho
        if (titulo === 'Configurações e alertas') carregarIfaces();
      }
    });
  });
}

/* ------------------------------------------- DNS da LAN (o Pi-hole) */
// Fica no topo, junto do estado da conexão, e não dentro de um card de link:
// não é métrica de link nenhum. Em 30/08 os três links apareciam verdes com a
// casa inteira sem resolver nome, porque o que quebrou foi a rota até os
// upstreams do Pi-hole — caminho que nenhuma sonda de link percorre.
function renderDnsLan(d) {
  const el = $('dns-lan');
  if (!el) return;
  const lista = (d && d.servidores) || [];
  if (!d || d.ok === null || d.ok === undefined) {
    el.className = 'pill pill-neutro';
    el.textContent = lista.length ? 'DNS da LAN: medindo…' : 'DNS da LAN: —';
  } else if (d.ok) {
    el.className = 'pill pill-on';
    el.textContent = `DNS da LAN: ok${d.ms != null ? ' · ' + nf(d.ms, 0) + ' ms' : ''}`;
  } else {
    el.className = 'pill pill-off';
    // nomeia QUAL endereço caiu: o aparelho serve DNS em vários, e saber em
    // qual parou é o que diz quem da casa ficou sem navegar
    el.textContent = `DNS da LAN: SEM RESOLVER em ${(d.falhando || []).join(', ')}`;
  }
  const linhas = lista.map((s) => {
    const est = s.ok === false ? '✖ ' + (s.erro || 'sem resposta')
      : s.ok ? '✔ ' + (s.ms != null ? nf(s.ms, 0) + ' ms' : 'ok')
      : '… medindo';
    return `${s.servidor} — ${s.papel}: ${est}`;
  });
  el.title = 'Resolvedores que este aparelho serve, medidos pela rota normal com '
    + 'um nome aleatório (o cache não mascara a falha).'
    + (linhas.length ? '\n\n' + linhas.join('\n') : '');
}

/* ---------------------------------------------------------- meshnet */
// O Meshnet é o caminho de acesso remoto a este aparelho. Por isso o botão de
// DESLIGAR pede confirmação: a página não tem login, e desligar daqui tranca o
// dono do lado de fora. Ligar não tem risco, então vai direto.
const MESH_OS = { linux: '🐧', windows: '🪟', macos: '🍎', ios: '📱', android: '🤖' };

function renderMesh(d) {
  const pill = $('mesh-pill');
  const btn = $('mesh-toggle');
  const msg = $('mesh-msg');
  const info = $('mesh-info');
  const tb = document.querySelector('#tab-mesh tbody');
  if (!pill || !d) return;

  if (d.disponivel === false) {
    pill.className = 'pill pill-neutro';
    pill.textContent = 'Meshnet: indisponível';
    btn.disabled = true;
    btn.textContent = '—';
    msg.textContent = d.erro || 'o comando nordvpn não existe neste aparelho';
    msg.style.color = '#ffd76a';
    info.innerHTML = '';
    tb.innerHTML = '';
    return;
  }
  if (d.meshnet === null || d.meshnet === undefined) {
    pill.className = 'pill pill-neutro';
    pill.textContent = 'Meshnet: lendo…';
    btn.disabled = true;
    return;
  }

  pill.className = 'pill ' + (d.meshnet ? 'pill-on' : 'pill-off');
  pill.textContent = 'Meshnet: ' + (d.meshnet ? 'ativo' : 'desligado');
  btn.disabled = false;
  btn.textContent = d.meshnet ? 'Desligar Meshnet' : 'Ligar Meshnet';
  btn.className = 'btn' + (d.meshnet ? '' : ' btn-primario');
  btn.dataset.alvo = d.meshnet ? 'off' : 'on';

  if (d.erro) { msg.textContent = '⚠️ ' + d.erro; msg.style.color = '#ffd76a'; }
  else if (!msg.dataset.fixo) { msg.textContent = ''; msg.style.color = ''; }

  const item = (rot, valor, obs) => `<div class="alvo">
    <div class="alvo-rot">${rot}</div>
    <div class="alvo-val">${escTxt(valor)}</div>
    ${obs ? `<div class="alvo-obs">${escTxt(obs)}</div>` : ''}
  </div>`;

  const eu = d.este_aparelho || {};
  const saida = d.saida || {};
  const conectados = (d.pares_locais || []).filter((p) => p.status === 'connected').length;
  info.innerHTML = [
    item('Este aparelho no Meshnet', eu.nickname || eu.hostname || '—', eu.hostname || ''),
    item('IP do Meshnet', eu.ip || '—', 'é por este endereço que você chega aqui de fora'),
    item('Saindo por', saida.link || saida.iface || '—',
         saida.gateway ? `gateway ${saida.gateway} · métrica ${saida.metrica}` : ''),
    item('VPN (túnel de saída)', d.vpn || '—',
         'desconectado é o esperado: aqui a NordVPN é só para o Meshnet'),
    item('Aparelhos conectados', `${conectados} de ${(d.pares_locais || []).length}`,
         d.versao || ''),
  ].join('');

  const pares = (d.pares_locais || []).concat(d.pares_externos || []);
  tb.innerHTML = pares.length ? pares.map((p) => {
    const on = p.status === 'connected';
    const nome = p.nickname && p.nickname !== '-' ? p.nickname : (p.hostname || '—');
    return `<tr>
      <td>${MESH_OS[p.os] || '💻'} ${escTxt(nome)}</td>
      <td class="muted">${escTxt(p.ip || '—')}</td>
      <td class="muted">${escTxt(p.distribuicao || p.os || '—')}</td>
      <td><span class="bolinha b-${on ? 'UP' : 'DOWN'}"></span>${on ? 'conectado' : 'desconectado'}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="4" class="muted">Nenhum aparelho no Meshnet.</td></tr>';
}

async function carregarMesh() {
  try { renderMesh(await pegar('/api/mesh')); } catch (e) { console.error(e); }
}

async function alternarMesh() {
  const btn = $('mesh-toggle');
  const msg = $('mesh-msg');
  const ligar = btn.dataset.alvo === 'on';
  const corpo = { meshnet: ligar };
  if (!ligar) {
    const ok = confirm(
      'Desligar o Meshnet corta o acesso remoto a este Orange Pi.\n\n'
      + 'Você só conseguirá religar estando na rede local. Continuar?');
    if (!ok) return;
    corpo.confirmar = 'DESLIGAR';
  }
  btn.disabled = true;
  msg.dataset.fixo = '1';
  msg.textContent = ligar ? 'ligando…' : 'desligando…';
  msg.style.color = '';
  try {
    const r = await fetch('/api/mesh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(corpo),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    msg.textContent = '✅ ' + j.mensagem;
    msg.style.color = '#8ef0b6';
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
  } finally {
    delete msg.dataset.fixo;
    // o daemon leva alguns segundos para refletir a mudança
    setTimeout(carregarMesh, 2500);
    setTimeout(carregarMesh, 8000);
  }
}

/* ------------------------------------------------------------ alvos */
// Pedido explícito: mostrar na página o alvo do ping e o IP do servidor DNS.
// O resumo fica no próprio summary, para os dois números aparecerem sem precisar
// abrir a seção.
function renderAlvos(d) {
  const grid = $('alvos-grid');
  const resumo = $('alvos-resumo');
  if (!grid || !d) return;
  const s = d.sondas || {};
  const lan = (d.dns_lan && d.dns_lan.servidores) || [];
  const principal = lan.find((x) => /DHCP/i.test(x.papel || '')) || lan[0];

  if (resumo) {
    resumo.textContent = `ping ${(s.ping || [])[0] || '—'} · DNS da LAN ${principal ? principal.servidor : '—'}`;
  }

  const item = (rot, valor, obs) => `<div class="alvo">
    <div class="alvo-rot">${rot}</div>
    <div class="alvo-val">${escTxt(valor)}</div>
    ${obs ? `<div class="alvo-obs">${escTxt(obs)}</div>` : ''}
  </div>`;

  const linhas = [
    item('Alvo do ping', (s.ping || []).join('  ·  ') || '—',
         `a cada ${s.ping_intervalo_s}s, preso à placa de cada link`),
    item('DNS medido por link', s.dns_servidor || '—',
         `resolve ${s.dns_nome} a cada ${s.dns_a_cada_s}s — resolvedor público, nunca o Pi-hole: aqui se mede o LINK`),
    item('Porta TCP', s.tcp || '—', `a cada ${s.tcp_a_cada_s}s`),
    item('Página de teste', s.http || '—', `a cada ${s.http_a_cada_s}s`),
    item('IP externo', s.ip_externo || '—', `a cada ${s.ip_externo_a_cada_s}s`),
  ];

  const linhasDns = lan.map((x) => item(
    `DNS da LAN · ${x.servidor}`,
    x.ok === false ? 'SEM RESOLVER' : x.ok ? `ok · ${nf(x.ms, 0)} ms` : 'medindo…',
    x.papel));

  grid.innerHTML = linhas.join('') + linhasDns.join('')
    + item('Nome consultado no teste do DNS da LAN',
           '(aleatório).' + (d.dns_lan ? d.dns_lan.zona : ''),
           'muda a cada consulta de propósito: um nome fixo viria do cache e esconderia a falha');
}

async function carregarAlvos() {
  try { renderAlvos(await pegar('/api/alvos')); } catch (e) { console.error(e); }
}

/* ------------------------------------------------------- traceroute */
// O valor do traceroute aqui é a comparação: o mesmo destino traçado pelas duas
// operadoras. Por isso o seletor "sair por" é o primeiro campo, e o resultado
// anterior de cada link fica guardado no servidor — ao trocar o link, a tabela
// mostra na hora o traçado passado daquele caminho.
function escTxt(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function renderTrace(d) {
  const tb = document.querySelector('#tab-trace tbody');
  const msg = $('tr-msg');
  if (!tb) return;
  if (!d) {
    tb.innerHTML = '<tr><td colspan="7" class="muted">Nenhum traçado ainda — escolha o link e clique em Traçar.</td></tr>';
    msg.textContent = '—';
    msg.style.color = '';
    return;
  }
  const saltos = d.saltos || [];
  tb.innerHTML = saltos.length ? saltos.map((s) => {
    const ms = (s.ms || []).filter((x) => x != null);
    const nulo = '<span class="muted">*</span>';
    // salto que não responde é rotina: muito roteador de trânsito não gera ICMP
    // por política. Some do caminho, não quer dizer perda.
    const melhor = ms.length ? nf(Math.min(...ms), 1) : null;
    const pior = ms.length ? nf(Math.max(...ms), 1) : null;
    const media = ms.length ? nf(ms.reduce((x, y) => x + y, 0) / ms.length, 1) : null;
    const extra = s.outros_ips && s.outros_ips.length
      ? ` <small class="muted" title="o mesmo salto respondeu de mais de um endereço: o roteador tem caminhos paralelos">+${s.outros_ips.length}</small>` : '';
    // sem bandeira quando o salto é de rede privada — não tem país nenhum
    const pais = s.bandeira
      ? `<span class="tr-bandeira" title="${escTxt(s.pais || s.cc)}">${s.bandeira}</span><span class="muted">${escTxt(s.cc)}</span>`
      : (s.ip ? '<span class="muted" title="rede privada ou país desconhecido">—</span>' : '');
    return `<tr>
      <td>${s.n}</td>
      <td>${s.ip ? escTxt(s.ip) + extra : nulo}</td>
      <td class="muted">${s.host ? escTxt(s.host) : (s.aviso ? escTxt(s.aviso) : '—')}</td>
      <td class="tr-pais">${pais}</td>
      <td>${melhor == null ? nulo : melhor + ' ms'}</td>
      <td>${media == null ? nulo : media + ' ms'}</td>
      <td>${pior == null ? nulo : pior + ' ms'}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">traçando…</td></tr>';

  const alvo = `${escTxt(d.destino)}${d.destino_ip && d.destino_ip !== d.destino ? ' (' + escTxt(d.destino_ip) + ')' : ''}`;
  if (d.fase === 'erro') {
    msg.textContent = `❌ ${d.link}: ${d.erro}`;
    msg.style.color = '#ffb3b3';
  } else if (d.fase === 'fim') {
    msg.textContent = (d.chegou ? '✅ ' : '⚠️ ')
      + `${d.link} → ${alvo} · ${saltos.length} saltos`
      + (d.duracao_s != null ? ` · ${d.duracao_s}s` : '')
      + (d.chegou ? '' : ` · ${d.erro || 'o destino não respondeu'}`);
    msg.style.color = d.chegou ? '#8ef0b6' : '#ffd76a';
  } else {
    msg.textContent = `traçando ${d.link} → ${alvo}… salto ${saltos.length}`;
    msg.style.color = '';
  }
}

async function carregarTrace() {
  try {
    const d = await pegar('/api/traceroute');
    estado.trace = d.ultimos || {};
    const sel = $('tr-link');
    if (sel && !sel.options.length) {
      // só saída pela internet: traçar até o próprio roteador seria um salto só
      nomesLinks().filter((n) => !ehLan(n)).forEach((n) => {
        const o = document.createElement('option');
        o.value = n; o.textContent = n;
        sel.appendChild(o);
      });
    }
    if (d.rodando) renderTrace(d.rodando);
    else renderTrace(estado.trace[sel && sel.value] || null);
  } catch (e) { console.error(e); }
}

async function rodarTrace() {
  const link = $('tr-link').value;
  const destino = $('tr-destino').value.trim();
  const max_hops = Number($('tr-hops').value);
  const msg = $('tr-msg');
  if (!destino) { msg.textContent = 'informe um destino'; msg.style.color = '#ffd76a'; return; }
  msg.textContent = 'iniciando…';
  msg.style.color = '';
  document.querySelector('#tab-trace tbody').innerHTML = '';
  try {
    const r = await fetch('/api/traceroute', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link, destino, max_hops }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
  }
}

/* ------------------------------------------------------------ cards */
function renderCards(links) {
  const box = $('cards');
  const existentes = new Map([...box.children].map((c) => [c.dataset.link, c]));
  links.forEach((l) => {
    let card = existentes.get(l.name);
    if (!card) {
      card = document.createElement('article');
      card.dataset.link = l.name;
      box.appendChild(card);
    }
    // o link de LAN ocupa a linha inteira, embaixo dos links de internet: ele é
    // apoio de diagnóstico, não um terceiro provedor concorrendo por atenção
    card.className = 'card' + (l.kind === 'lan' ? ' card-lan' : '');
    card.style.setProperty('--cor-link', corLink(l.name));
    const st = l.state || 'NO_LINK';
    const rtt = l.rtt_avg !== null && l.rtt_avg !== undefined ? l.rtt_avg : l.rtt_ewma;
    const up = l.uptime || {};
    const uq = l.ultima_queda;
    const desde = l.state_since ? fmtDurLonga(Math.floor(Date.now() / 1000) - l.state_since) : '—';
    const rotDesde = st === 'UP' ? 'estável há' : 'nesse estado há';

    card.innerHTML = `
      <div class="card-topo">
        <span class="bolinha b-${st}"></span>
        <span class="card-nome" style="color:${corLink(l.name)}">${l.name}</span>
        ${l.kind === 'lan' ? '<span class="tag tag-lan" title="rede local: mede a latência até o seu roteador, não a internet">LAN</span>' : ''}
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
        <span>${l.kind === 'lan' ? 'alvo' : 'gateway'} <b>${(l.kind === 'lan' ? l.target : l.gateway) || '—'}</b></span>
        <span>IP local <b>${l.ip || '—'}</b></span>
        ${l.kind === 'lan' ? ''
          : `<span class="ip-externo" title="${l.ip_externo_ts ? 'visto em ' + fmtDataHora(l.ip_externo_ts) : 'ainda não consultado'}">IP externo <b>${l.ip_externo || '—'}</b></span>`}
        ${l.kind === 'lan' ? `<span>placa <b>${l.iface || '—'}</b></span>` : ''}
        ${l.kind === 'lan' ? '' : `<span>gw <b>${l.gw_ok ? 'ok' : 'sem resposta'}</b></span>`}
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
  svg.appendChild(svgEl('path', { d, fill: 'none', stroke: corLink(nome), 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: .9 }));
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
      fill: '#93a3c0', 'font-size': 12,
    });
    tx.textContent = 'sem monitoramento neste trecho';
    svg.appendChild(tx);
  }
}

/* ------------------------------------------------------------ latência
   O gráfico ficou maior e mudou de modelo. Antes era linha fina sobre uma
   faixa mín–máx e mais nada: para saber quanto cada link está marcando agora
   era preciso passar o mouse, e a legenda no topo obrigava a ir e voltar entre
   a cor e o nome. Agora:

     · área com degradê sob cada linha — o volume separa os links de relance,
       coisa que duas linhas de 2px encostadas não fazem;
     · rótulo direto na ponta direita, com o nome do link e o valor de agora,
       de modo que a identidade nunca dependa só da cor;
     · marcador na última medida, que é onde o olho procura primeiro;
     · linha tracejada no limiar de latência alta, para o número ter régua.

   A faixa mín–máx continua: ela é o que mostra que 4 ms de média esconderam
   um pico de 300 ms dentro do intervalo. */

/* Quebra a série nos buracos. Um link que ficou 20 min fora não pode virar uma
   reta atravessando o gráfico como se tivesse respondido o tempo todo. */
function segmentos(pts, ...campos) {
  const fora = [];
  let atual = null;
  pts.forEach((p) => {
    // a faixa exige mínimo E máximo: com só um dos dois, `Y(null)` daria zero
    // e a faixa desceria até o chão desenhando uma perda que não houve
    if (campos.some((c) => p[c] === null || p[c] === undefined)) { atual = null; return; }
    if (!atual) { atual = []; fora.push(atual); }
    atual.push(p);
  });
  return fora;
}

/* Hermite monótona: suaviza sem inventar picos que não existem nos dados
   (a spline ingênua "estoura" a curva entre dois pontos e desenha uma latência
   que nunca foi medida). Só entra quando os pontos são esparsos — em 30 dias
   cada ponto é uma hora e a linha reta fica em ziguezague; ao vivo, com um
   ponto a cada 2 s, não há o que suavizar e o custo não se paga. */
function caminhoSuave(pontos) {
  const n = pontos.length;
  if (n < 3) return pontos.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const dx = [], dy = [], m = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pontos[i + 1][0] - pontos[i][0]);
    dy.push(pontos[i + 1][1] - pontos[i][1]);
  }
  const s = dx.map((d, i) => (d ? dy[i] / d : 0));
  m.push(s[0]);
  for (let i = 1; i < n - 1; i++) {
    m.push(s[i - 1] * s[i] <= 0 ? 0 : (s[i - 1] + s[i]) / 2);
  }
  m.push(s[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (s[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / s[i], b = m[i + 1] / s[i];
    const h = Math.hypot(a, b);
    if (h > 3) { m[i] = (3 / h) * a * s[i]; m[i + 1] = (3 / h) * b * s[i]; }
  }
  let d = 'M' + pontos[0][0].toFixed(1) + ' ' + pontos[0][1].toFixed(1);
  for (let i = 0; i < n - 1; i++) {
    const t = dx[i] / 3;
    d += ' C' + (pontos[i][0] + t).toFixed(1) + ' ' + (pontos[i][1] + m[i] * t).toFixed(1)
       + ' ' + (pontos[i + 1][0] - t).toFixed(1) + ' ' + (pontos[i + 1][1] - m[i + 1] * t).toFixed(1)
       + ' ' + pontos[i + 1][0].toFixed(1) + ' ' + pontos[i + 1][1].toFixed(1);
  }
  return d;
}

function caminho(pontos, suave) {
  if (!pontos.length) return '';
  if (!suave) {
    return pontos.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  }
  return caminhoSuave(pontos);
}

/* Os rótulos da ponta direita se empilham quando dois links marcam quase o
   mesmo valor — e eles marcam, quase sempre. Empurra um de cada vez até
   caberem, sem sair da área do gráfico. */
function arrumarRotulos(itens, topo, base, altura) {
  itens.sort((a, b) => a.y - b.y);
  for (let i = 1; i < itens.length; i++) {
    if (itens[i].y - itens[i - 1].y < altura) itens[i].y = itens[i - 1].y + altura;
  }
  const excesso = itens.length ? itens[itens.length - 1].y - base : 0;
  if (excesso > 0) for (const it of itens) it.y = Math.max(topo, it.y - excesso);
  return itens;
}

function desenharLatencia(series, eventos, t0, t1) {
  const svg = $('g-lat');
  const { W, H } = medir(svg, 900, 400);
  // a direita deixa de ser só respiro: é onde moram os rótulos diretos. Em
  // tela estreita eles não cabem e a legenda do topo volta a ser a única
  const comRotulos = W >= 560;
  const M = { t: 16, r: comRotulos ? 96 : 14, b: 28, l: W < 420 ? 38 : 52 };
  limpar(svg);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  let ymax = 0;
  for (const nome in series) for (const p of series[nome]) if (p[3] !== null && p[3] !== undefined) ymax = Math.max(ymax, p[3]);
  if (!ymax) ymax = 50;
  ymax *= 1.12;

  const X = (t) => M.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
  const Y = (v) => M.t + ih - (Math.min(v, ymax) / ymax) * ih;

  // um <defs> por desenho: o degradê de cada link precisa da cor dele, e o id
  // some junto com o resto do SVG a cada redesenho
  const defs = svgEl('defs', {});
  nomesLinks().forEach((nome, i) => {
    const g = svgEl('linearGradient', { id: 'grad' + i, x1: 0, y1: 0, x2: 0, y2: 1 });
    g.appendChild(svgEl('stop', { offset: '0%', 'stop-color': corLink(nome), 'stop-opacity': .34 }));
    g.appendChild(svgEl('stop', { offset: '100%', 'stop-color': corLink(nome), 'stop-opacity': .02 }));
    defs.appendChild(g);
  });
  svg.appendChild(defs);

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

  // grade recessiva: só as horizontais. As verticais competiam com as próprias
  // linhas de dados num gráfico que agora tem área pintada
  ticksY(ymax).forEach((v) => {
    svg.appendChild(svgEl('line', {
      x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v),
      stroke: v === 0 ? '#4a4a46' : '#2e2e2c', 'stroke-width': 1 }));
    const tx = svgEl('text', { x: M.l - 9, y: Y(v) + 4, 'text-anchor': 'end', fill: '#a5a39c', 'font-size': 11 });
    tx.textContent = nf(v, v < 10 ? 1 : 0);
    svg.appendChild(tx);
  });
  const un = svgEl('text', { x: M.l - 9, y: M.t - 3, 'text-anchor': 'end', fill: '#a5a39c', 'font-size': 10 });
  un.textContent = 'ms';
  svg.appendChild(un);

  // régua do limiar: só aparece se couber na escala atual. Esticar o eixo até
  // 80 ms para mostrar a linha achataria contra o chão os 4 ms do dia a dia
  const lim = estado.limiares.lat;
  if (lim > 0 && lim < ymax * 0.97) {
    svg.appendChild(svgEl('line', {
      x1: M.l, x2: W - M.r, y1: Y(lim), y2: Y(lim), stroke: STATUS.warning,
      'stroke-width': 1, 'stroke-dasharray': '5 4', opacity: .55 }));
    const tl = svgEl('text', { x: W - M.r - 6, y: Y(lim) - 5, 'text-anchor': 'end',
                               fill: STATUS.warning, 'font-size': 10, opacity: .9 });
    tl.textContent = `latência alta · ${nf(lim, 0)} ms`;
    svg.appendChild(tl);
  }

  // eixo X
  const nX = Math.max(2, Math.min(7, Math.floor(iw / 120)));
  for (let i = 0; i <= nX; i++) {
    const t = t0 + ((t1 - t0) * i) / nX;
    const tx = svgEl('text', { x: X(t), y: H - 9, 'text-anchor': i === 0 ? 'start' : i === nX ? 'end' : 'middle', fill: '#a5a39c', 'font-size': 11 });
    tx.textContent = rotuloTempo(t, t1 - t0);
    svg.appendChild(tx);
  }

  // séries: faixa mín–máx, área com degradê, linha, e a ponta marcada
  const rotulos = [];
  nomesLinks().forEach((nome, i) => {
    const pts = series[nome];
    if (!pts || !pts.length) return;
    const cor = corLink(nome);
    const suave = pts.length < iw / 6;

    segmentos(pts, 2, 3).forEach((seg) => {
      if (seg.length < 2) return;
      const alto = seg.map((p) => [X(p[0]), Y(p[3])]);
      const baixo = seg.map((p) => [X(p[0]), Y(p[2])]).reverse();
      // as duas bordas da faixa seguem a mesma curva da linha da média: uma
      // borda suave contra outra angular no mesmo preenchimento fica torta
      const d = caminho(alto, suave) + ' ' + caminho(baixo, suave).replace(/^M/, 'L') + ' Z';
      svg.appendChild(svgEl('path', { d, fill: cor, opacity: .13, stroke: 'none' }));
    });

    segmentos(pts, 1).forEach((seg) => {
      if (!seg.length) return;
      const linha = seg.map((p) => [X(p[0]), Y(p[1])]);
      const d = caminho(linha, suave);
      if (seg.length > 1) {
        const chao = M.t + ih;
        svg.appendChild(svgEl('path', {
          d: d + ` L${linha[linha.length - 1][0].toFixed(1)} ${chao} L${linha[0][0].toFixed(1)} ${chao} Z`,
          fill: `url(#grad${i})`, stroke: 'none' }));
      }
      svg.appendChild(svgEl('path', {
        d, fill: 'none', stroke: cor, 'stroke-width': 2.2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    });

    // última medida: o ponto que responde "e agora?"
    const vivos = pts.filter((p) => p[1] !== null && p[1] !== undefined);
    const fim = vivos[vivos.length - 1];
    if (!fim) return;
    const px = X(fim[0]), py = Y(fim[1]);
    // anel da cor do fundo: sem ele, dois pontos encostados viram uma mancha só
    svg.appendChild(svgEl('circle', { cx: px, cy: py, r: 5.5, fill: '#0f0f0e', opacity: .85 }));
    svg.appendChild(svgEl('circle', { cx: px, cy: py, r: 4, fill: cor }));
    if (comRotulos) rotulos.push({ nome, cor, y: py, valor: fim[1], lan: ehLan(nome) });
  });

  // rótulo direto na ponta: nome + valor de agora, sem depender da cor
  arrumarRotulos(rotulos, M.t + 8, M.t + ih - 4, 30).forEach((r) => {
    const x = W - M.r + 8;
    const g = svgEl('g', {});
    g.appendChild(svgEl('rect', {
      x: x - 4, y: r.y - 15, width: M.r - 10, height: 30, rx: 7,
      fill: r.cor, opacity: .14 }));
    g.appendChild(svgEl('rect', { x: x - 4, y: r.y - 15, width: 3, height: 30, rx: 1.5, fill: r.cor }));
    const n1 = svgEl('text', { x: x + 5, y: r.y - 3, fill: r.cor, 'font-size': 10.5, 'font-weight': 700 });
    n1.textContent = r.nome + (r.lan ? ' ·' : '');
    g.appendChild(n1);
    const n2 = svgEl('text', { x: x + 5, y: r.y + 10, fill: '#e6e5e0', 'font-size': 11.5, 'font-weight': 600 });
    n2.textContent = nf(r.valor, 1) + ' ms';
    g.appendChild(n2);
    svg.appendChild(g);
  });

  ligarCrosshair(svg, $('tt-lat'), $('wrap-lat'), series, X, M, ih, t0, t1, 'ms', 1);
}

/* ------------------------------------------------------------ perda */
// Perda não é uma grandeza contínua como latência: fica em zero quase o tempo
// todo e o que interessa é QUANDO e QUÃO GRAVE foi o episódio. Num gráfico de
// linha isso vira um traço rente ao eixo, invisível justamente no dia em que
// importa. A faixa por link resolve: cada bloco é um intervalo, a cor diz a
// gravidade, e um episódio de 2 minutos em 24 h continua sendo um bloco visível.
const PERDA_FAIXAS = [
  { lim: 0,   cls: 'p0', rot: 'sem perda' },
  { lim: 2,   cls: 'p1', rot: 'até 2%' },
  { lim: 10,  cls: 'p2', rot: '2 a 10%' },
  { lim: 50,  cls: 'p3', rot: '10 a 50%' },
  { lim: 101, cls: 'p4', rot: 'acima de 50%' },
];
const PERDA_COR = { p0: '#2f8f5b', p1: '#e0b52e', p2: '#ff8f43', p3: '#ff5a5a', p4: '#b81f3a', sem: '#33415c' };

function classePerda(v) {
  if (v == null) return 'sem';
  if (v <= 0) return 'p0';
  for (const f of PERDA_FAIXAS) if (v <= f.lim) return f.cls;
  return 'p4';
}

// Quantos blocos cabem: um por pixel seria ilegível e pesado no Orange Pi.
function nBlocos(largura) {
  return Math.max(24, Math.min(180, Math.floor(largura / 7)));
}

function desenharPerda(series, t0, t1) {
  const box = $('perda-faixas');
  if (!box) return;
  const nomes = Object.keys(series);
  const largura = box.clientWidth || 700;
  const N = nBlocos(largura);
  const passo = Math.max(1, (t1 - t0) / N);

  box.innerHTML = nomes.map((nome) => {
    // agrupa as amostras em N intervalos e guarda o PIOR de cada um: numa
    // janela de 30 dias a média esconderia exatamente o pico que se procura
    const baldes = new Array(N).fill(null);
    (series[nome] || []).forEach((p) => {
      const i = Math.min(N - 1, Math.floor((p[0] - t0) / passo));
      if (i < 0) return;
      const v = p[4];
      if (v == null) return;
      baldes[i] = baldes[i] == null ? v : Math.max(baldes[i], v);
    });
    const vistos = baldes.filter((v) => v != null);
    const pior = vistos.length ? Math.max(...vistos) : null;
    const comPerda = vistos.filter((v) => v > 0).length;

    const blocos = baldes.map((v, i) => {
      const cls = classePerda(v);
      const ini = t0 + i * passo;
      const titulo = v == null
        ? `${fmtDataHora(ini)} — sem monitoramento`
        : `${fmtDataHora(ini)} — perda ${nf(v, v < 10 ? 1 : 0)}%`;
      return `<div class="perda-bloco" style="background:${PERDA_COR[cls]}" title="${titulo}"></div>`;
    }).join('');

    const resumo = pior == null ? 'sem dados'
      : pior <= 0 ? 'nenhuma perda'
      : `pior ${nf(pior, pior < 10 ? 1 : 0)}% · ${comPerda} ${comPerda === 1 ? 'intervalo' : 'intervalos'}`;

    return `<div class="perda-linha">
      <span class="perda-nome" style="color:${corLink(nome)}">${nome}</span>
      <div class="perda-barra">${blocos}</div>
      <span class="perda-resumo">${resumo}</span>
    </div>`;
  }).join('') + `<div class="perda-eixo"><span>${rotuloTempo(t0, t1 - t0)}</span><span>${rotuloTempo(t1, t1 - t0)}</span></div>`;
}

/* ------------------------------------------------------------ crosshair */
function ligarCrosshair(svg, tt, wrap, series, X, M, ih, t0, t1, unidade, campo) {
  const linha = svgEl('line', { y1: M.t, y2: M.t + ih, stroke: '#cbd6ea', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0 });
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
      linhas.push(`<div class="tt-linha"><i style="background:${corLink(nome)}"></i>${nome}: <strong>${v === null || v === undefined ? 'sem resposta' : nf(v, 1) + ' ' + unidade}</strong></div>`);
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

  nomesLinks().forEach((nome) => {
    const pts = series[nome] || [];
    const cobertos = new Set();
    pts.forEach((p) => cobertos.add(Math.floor((p[0] - t0) / passo)));

    const linha = document.createElement('div');
    linha.className = 'tl-linha';
    const evs = eventos.filter((e) => e.link === nome);
    let html = `<div class="tl-rot"><span style="color:${corLink(nome)}">${nome}</span><span class="muted">${rotuloTempo(t0, t1 - t0)} → agora</span></div><div class="tl-barras">`;
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

/* A legenda acompanha os links que existem de fato — inclusive o de LAN, que
   entra identificado para ninguém confundir 0,5 ms de roteador com internet. */
function montarLegenda() {
  const box = $('legenda');
  if (!box) return;
  const itens = nomesLinks().map((n) =>
    `<span class="leg"><i class="sw" style="background:${corLink(n)}"></i>${n}${
      ehLan(n) ? ' <small class="muted">(rede local)</small>' : ''}</span>`);
  itens.push('<span class="leg"><i class="sw sw-queda"></i>faixa de queda</span>');
  itens.push(`<span class="leg"><i class="sw" style="background:${STATUS.sem}"></i>sem monitoramento</span>`);
  box.innerHTML = itens.join('');
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
  nomesLinks().forEach((n) => desenharSpark(n, (g.series[n] || []).slice(-180)));
}

async function carregarGraficos() {
  const { t0, t1 } = janela();
  const nomes = nomesLinks();
  try {
    const respostas = await Promise.all(
      nomes.map((n) => pegar(`/api/samples?link=${encodeURIComponent(n)}&from=${t0}&to=${t1}`))
        .concat([pegar(`/api/events?from=${t0 - 86400}&limit=500`)]));
    const ev = respostas.pop();
    const series = {};
    nomes.forEach((n, i) => { series[n] = respostas[i].points; });
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

/* `vals` são todos os links (escala das barras); `valsComp` são só os links
   comparáveis entre si. O selo "melhor" nunca vai para o link de LAN: 0,5 ms
   até o roteador venceria a GIGA em toda linha da tabela e não diria nada. */
function celulaResumo(def, nome, valor, vals, valsComp) {
  if (valor === null || valor === undefined) {
    return `<td class="cel"><span class="cel-vazio">—</span></td>`;
  }
  const qual = def.q ? def.q(valor) : 'neutro';
  const validos = vals.filter((v) => v !== null && v !== undefined);
  const teto = Math.max(...validos.map(Math.abs), 0);
  const largura = def.barra && teto > 0 ? Math.round((Math.abs(valor) / teto) * 100) : 0;
  let melhor = false;
  const comp = (valsComp || []).filter((v) => v !== null && v !== undefined);
  if (def.melhor && comp.length >= 2 && new Set(comp).size > 1) {
    const alvo = def.melhor === 'maior' ? Math.max(...comp) : Math.min(...comp);
    melhor = valor === alvo;
  }
  return `<td class="cel${melhor ? ' cel-melhor' : ''}">
      ${largura ? `<span class="cel-barra" style="width:${largura}%;background:${corLink(nome)}"></span>` : ''}
      <span class="cel-conteudo">
        <span class="chip q-${qual}" title="${qual}">${FAIXAS[qual]}</span>
        <span class="valor${def.forte ? ' destaque' : ''}">${def.fmt(valor)}</span>
        ${melhor ? '<span class="pill-melhor">melhor</span>' : ''}
      </span></td>`;
}

async function carregarResumo() {
  const { t0, t1 } = janela();
  const nomes = nomesLinks();
  const internet = linksDeInternet();
  try {
    const r = await pegar(`/api/summary?period=custom&from=${t0}&to=${t1}`);
    $('resumo-cab').innerHTML = '<th scope="col">Métrica</th>' + nomes.map((n) =>
      `<th scope="col" style="color:${corLink(n)}">${n}${
        ehLan(n) ? '<small class="muted"> (LAN)</small>' : ''}</th>`).join('');
    const tb = $('tab-resumo').querySelector('tbody');
    limpar(tb);
    GRUPOS.forEach((g) => {
      const cab = document.createElement('tr');
      cab.className = 'linha-grupo';
      cab.innerHTML = `<th colspan="${nomes.length + 1}" scope="colgroup"><span class="g-icone">${g.icone}</span>${g.titulo}</th>`;
      tb.appendChild(cab);

      g.linhas.forEach((def) => {
        const val = (n) => (r.links[n] ? r.links[n][def.k] : null);
        const vals = nomes.map(val);
        const valsComp = internet.map(val);
        const tr = document.createElement('tr');
        tr.innerHTML = `<td class="metrica">${def.rot}</td>` +
          nomes.map((n, i) => celulaResumo(def, n, vals[i], vals,
                                           internet.includes(n) ? valsComp : null)).join('');
        tb.appendChild(tr);
      });
    });
    $('resumo-sub').textContent =
      `De ${fmtDataHora(t0)} até ${fmtDataHora(t1)} · uptime calculado sobre o tempo efetivamente monitorado.`;
    renderQuedasPeriodo(r);
  } catch (e) { console.error(e); }
}


/* ------------------------------------------- destaque das quedas do período
   A pergunta que o usuário faz quando escolhe um período é sempre a mesma:
   "caiu? quantas vezes? quanto tempo?". Isso estava diluído no meio de uma
   tabela de 13 métricas — aqui vira a primeira coisa que se lê, em segundos
   cheios (é assim que a operadora conta) e com a duração humana ao lado. */
function cartaoQueda(nome, r) {
  const link = estado.links.find((l) => l.name === nome);
  const lan = ehLan(nome);
  const caido = link && (link.state === 'DOWN' || link.state === 'NO_LINK');
  const agora = Math.floor(Date.now() / 1000);
  const cor = corLink(nome);
  const cab = `<div class="qd-topo"><span class="ponto-link" style="background:${cor}"></span>
      <b style="color:${cor}">${nome}</b>${lan ? '<span class="tag tag-lan">LAN</span>' : ''}</div>`;

  if (!r || !r.amostras) {
    return `<div class="qd qd-vazio">${cab}
      <p class="qd-frase">sem medição neste período</p></div>`;
  }
  const seg = Math.round(r.downtime_s || 0);
  const n = r.quedas || 0;
  const houve = seg > 0 || n > 0;
  const agoraTxt = caido
    ? `<p class="qd-agora">🔴 fora do ar AGORA há ${fmtDurLonga(agora - (link.evento_aberto ? link.evento_aberto.started_at : link.state_since))}</p>`
    : '';
  if (!houve) {
    return `<div class="qd qd-ok">${cab}
      <p class="qd-frase">✅ nenhuma queda no período</p>
      <div class="qd-rodape"><span>uptime <b>${r.uptime_pct == null ? '—' : nf(r.uptime_pct, 3) + '%'}</b></span>
        ${r.degradacoes ? `<span>${r.degradacoes} período(s) de latência alta</span>` : ''}</div>
      ${agoraTxt}</div>`;
  }
  return `<div class="qd qd-caiu">${cab}
    <div class="qd-numeros">
      <div class="qd-num"><b>${nf(seg, 0)}</b><span>segundos fora do ar</span>
        ${seg >= 60 ? `<small>${fmtDurLonga(seg)}</small>` : ''}</div>
      <div class="qd-num"><b>${nf(n, 0)}</b><span>${n === 1 ? 'queda' : 'quedas'} no período</span>
        ${r.maior_queda_s ? `<small>maior: ${fmtDur(r.maior_queda_s)}</small>` : ''}</div>
    </div>
    <div class="qd-rodape"><span>uptime <b>${r.uptime_pct == null ? '—' : nf(r.uptime_pct, 3) + '%'}</b></span>
      ${r.degradacoes ? `<span>${r.degradacoes} período(s) de latência alta</span>` : ''}</div>
    ${agoraTxt}</div>`;
}

function renderQuedasPeriodo(resumo) {
  const box = $('quedas-destaque');
  if (!box) return;
  // os números vêm do /api/summary, que já deixa de fora as quedas causadas
  // pelo próprio teste de velocidade e pela troca de placa: em nenhuma das duas
  // a operadora tem culpa
  const links = resumo && resumo.links ? resumo.links : {};
  box.innerHTML = nomesLinks().map((n) => cartaoQueda(n, links[n])).join('');
}

/* Rótulo da janela em texto, repetido no painel de latência: quem rola a
   página até um gráfico precisa saber de que pedaço de tempo ele fala. */
function renderPeriodo() {
  const p = periodoAtual();
  const { t0, t1 } = janela();
  document.querySelectorAll('.btn-per').forEach((b) =>
    b.classList.toggle('ativo', b.dataset.per === p.id));
  const pill = $('periodo-pill');
  if (pill) {
    pill.textContent = p.curto;
    pill.className = 'pill resumo-cabeca ' + (p.vivo ? 'pill-on' : 'pill-neutro');
  }
  const janelaTxt = p.vivo
    ? `últimos ${p.span} segundos, redesenhando a cada sondagem (2 s)`
    : (p.span
        ? `de ${fmtDataHora(t0)} até agora`
        : (estado.inicioDados
            ? `desde o começo da coleta, ${fmtDataHora(t0)} (${fmtDurLonga(t1 - t0)} de histórico)`
            : 'todo o histórico disponível'));
  const alvo = $('periodo-janela');
  if (alvo) alvo.textContent = janelaTxt;
  const eco = $('eco-latencia');
  if (eco) eco.textContent = p.curto;
  const evp = $('ev-periodo');
  if (evp) evp.textContent = p.vivo ? 'ao vivo (últimos 2 minutos)' : p.curto;
}

function escolherPeriodo(id, salvar = true) {
  if (!PERIODOS.some((p) => p.id === id)) return;
  estado.periodo = id;
  estado.span = janela().span;
  if (salvar) {
    try { localStorage.setItem(CHAVE_PERIODO, id); } catch (e) { /* sem persistência */ }
  }
  renderPeriodo();
  agendarAtualizacoes();
  estado.offset = 0;
  carregarGraficos();
  carregarResumo();
  carregarEventos();
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
    return `<i style="height:${h}%;background:${corLink(nome)}" title="${fmtDataHora(t.ts)} — ${nf(t.down_mbps, 1)} Mbps ↓ / ${nf(t.up_mbps, 1)} Mbps ↑"></i>`;
  }).join('');
  return `<div class="vel-hist" aria-label="testes anteriores"><span class="met-rot">testes anteriores ↓</span><div class="vel-hist-barras">${barras}</div></div>`;
}

function renderVelocidade() {
  const box = $('vel-grid');
  limpar(box);
  linksDeInternet().forEach((nome) => {
    const cor = corLink(nome);
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
  const out = {};
  linksDeInternet().forEach((n) => { out[n] = []; });
  lista.forEach((t) => { (out[t.link] = out[t.link] || []).push(t); });
  return out;
}

async function carregarVelocidade() {
  try {
    const filtro = $('f-vel-link') ? $('f-vel-link').value : '';
    const r = await pegar('/api/speedtest?limit=200'
      + (filtro ? '&link=' + encodeURIComponent(filtro) : ''));
    estado.vel.rodando = r.rodando;
    estado.vel.ultimos = r.ultimos || {};
    estado.vel.log = r.historico || [];
    // os cards mostram sempre os dois links; o filtro é só do log de baixo
    if (!filtro) estado.vel.historico = agruparVel(estado.vel.log);
    renderVelocidade();
    renderLogVelocidade();
  } catch (e) { console.error(e); }
}

/* Log dos testes: a mesma tabela que vira CSV. Um teste com erro continua na
   lista — saber que a medição falhou às 3h da manhã também é informação. */
function renderLogVelocidade() {
  const tb = $('tab-vel') && $('tab-vel').querySelector('tbody');
  if (!tb) return;
  const lista = estado.vel.log || [];
  limpar(tb);
  if (!lista.length) {
    tb.innerHTML = '<tr><td colspan="9" class="muted vazio">Nenhum teste de velocidade registrado ainda.</td></tr>';
    $('vel-log-info').textContent = '—';
    return;
  }
  lista.forEach((t) => {
    const falhou = t.down_mbps === null || t.down_mbps === undefined;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td data-rot="Quando">${fmtDataHora(t.ts)}</td>
      <td data-rot="Link"><span class="ponto-link" style="background:${corLink(t.link)}"></span>${t.link}</td>
      <td data-rot="Download" class="${falhou ? '' : 'destaque'}">${falhou ? '—' : nf(t.down_mbps, 1) + ' Mbps'}</td>
      <td data-rot="Upload">${t.up_mbps == null ? '—' : nf(t.up_mbps, 1) + ' Mbps'}</td>
      <td data-rot="Ping">${t.ping_ms == null ? '—' : nf(t.ping_ms, 1) + ' ms'}</td>
      <td data-rot="Jitter">${t.jitter_ms == null ? '—' : nf(t.jitter_ms, 1) + ' ms'}</td>
      <td data-rot="Origem">${t.origem === 'auto'
        ? '<span class="tag tag-auto" title="disparado pelo agendador diário">automático</span>'
        : '<span class="muted">manual</span>'}</td>
      <td data-rot="Servidor" class="muted">${t.servidor || '—'}</td>
      <td data-rot="Observação">${t.erro ? `<span class="vel-erro">${falhou ? '❌' : '⚠️'} ${t.erro}</span>` : '<span class="muted">ok</span>'}</td>`;
    tb.appendChild(tr);
  });
  const ok = lista.filter((t) => t.down_mbps != null);
  const media = ok.length ? ok.reduce((a, t) => a + t.down_mbps, 0) / ok.length : null;
  $('vel-log-info').textContent =
    `${lista.length} teste(s) listados · ${ok.length} com medição válida`
    + (media ? ` · média de download ${nf(media, 1)} Mbps` : '');
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
    msg.style.color = '#ffb3b3';
  }
}

async function carregarEventos() {
  const link = $('f-link').value, tipo = $('f-tipo').value;
  const { t0, t1 } = janela();
  const q = new URLSearchParams({ limit: '25', offset: String(estado.offset),
                                  from: String(t0), to: String(t1) });
  if (link) q.set('link', link);
  if (tipo) q.set('tipo', tipo);
  try {
    const r = await pegar('/api/events?' + q);
    estado.total = r.total;
    const tb = $('tab-eventos').querySelector('tbody');
    limpar(tb);
    if (!r.events.length) {
      // a lista segue o período do topo: "nada aqui" pode ser um minuto calmo,
      // e não a ausência de quedas na vida do link
      tb.innerHTML = `<tr><td colspan="6" class="muted vazio">Nenhuma queda ou alerta em ${periodoAtual().curto} — ótimo sinal.</td></tr>`;
    }
    // data-rot vira o rótulo das linhas empilhadas no celular (ver style.css)
    r.events.forEach((e) => {
      const tr = document.createElement('tr');
      const aberto = !e.ended_at;
      tr.innerHTML = `
        <td data-rot="Link"><span class="ponto-link" style="background:${corLink(e.link)}"></span>${e.link}</td>
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
    const mudou = d.links.map((l) => l.name).join('|') !== nomesLinks().join('|');
    estado.links = d.links;
    estado.porta = d.porta;
    if (mudou) { preencherFiltrosDeLink(); montarLegenda(); }
    renderCards(d.links);
    renderDnsLan(d.dns_lan);
    atualizarBanner(d.links);
    if (estado.serieAtual) {
      nomesLinks().forEach((n) =>
        desenharSpark(n, (estado.serieAtual[n] || []).slice(-180)));
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
        msg.style.color = '#8ef0b6';
      } else {
        msg.textContent = `❌ ${d.link}: ${d.erro}`;
        msg.style.color = '#ffb3b3';
      }
      carregarVelocidade();
      return;
    }
    estado.vel.rodando = d;
    if (msg.textContent.startsWith('iniciando')) msg.textContent = '';
    renderVelocidade();
  });

  es.addEventListener('traceroute', (ev) => {
    const d = JSON.parse(ev.data);
    if (d.fase === 'fim' || d.fase === 'erro') estado.trace[d.link] = d;
    // só pinta se o usuário ainda está olhando aquele link
    const sel = $('tr-link');
    if (!sel || !sel.value || sel.value === d.link) renderTrace(d);
  });

  es.addEventListener('varredura', (ev) => {
    const d = JSON.parse(ev.data);
    if (d.fase === 'fim' || d.fase === 'erro') {
      estado.scan.rodando = null;
      // no erro o resultado anterior continua na tela: apagar a tabela porque
      // uma varredura falhou seria perder o que já se sabia da rede
      if (d.fase === 'fim') estado.scan.ultimo = d;
      renderScan();
      carregarLogScan();      // a varredura que acabou de terminar entra no log
      if (d.fase === 'erro') {
        $('sc-msg').textContent = '❌ ' + (d.erro || 'a varredura falhou');
        $('sc-msg').style.color = '#ffb3b3';
      }
      return;
    }
    estado.scan.rodando = d;
    renderScan();
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

/* ------------------------------------------------ placas de rede (config)
   Cada link é uma placa, e a placa pode ser trocada: quando o adaptador USB for
   substituído, o nome da interface muda e é aqui que o link volta a apontar
   para o lugar certo — sem perder o histórico, que é do link e não da placa. */
function descreverIface(i) {
  const partes = [];
  if (i.ip) partes.push(i.ip);
  if (i.gateway) partes.push('gw ' + i.gateway);
  if (i.usb) partes.push('USB');
  if (i.mbps) partes.push(i.mbps + ' Mbps');
  partes.push(i.cabo === false ? 'sem cabo' : i.up ? 'ativa' : 'inativa');
  return partes.join(' · ');
}

function renderIfaces() {
  const box = $('ifaces-grid');
  if (!box) return;
  limpar(box);
  const lista = estado.ifaces;
  estado.cfgLinks.forEach((l) => {
    // a placa gravada pode não existir agora (adaptador fora do ar); ela entra
    // na lista assim mesmo, senão salvar a tela apagaria a escolha do usuário
    const conhecidas = lista.map((i) => i.iface);
    const opcoes = conhecidas.concat(
      conhecidas.includes(l.iface) ? [] : [l.iface]).map((nome) => {
      const i = lista.find((x) => x.iface === nome);
      const rot = i ? `${nome} — ${descreverIface(i)}` : `${nome} — ausente no sistema`;
      return `<option value="${nome}"${nome === l.iface ? ' selected' : ''}>${rot}</option>`;
    }).join('');

    const bloco = document.createElement('div');
    bloco.className = 'iface-item';
    bloco.style.setProperty('--cor-link', corLink(l.name));
    bloco.innerHTML = `
      <div class="iface-topo">
        <span class="ponto-link" style="background:${corLink(l.name)}"></span>
        <b style="color:${corLink(l.name)}">${l.name}</b>
        <span class="muted">${l.kind === 'lan' ? 'rede local' : 'internet'}</span>
      </div>
      <label class="campo campo-largo"><span>Placa de rede</span>
        <select data-link="${l.name}" class="sel-iface">${opcoes}</select>
      </label>
      ${l.kind === 'lan' ? `<label class="campo"><span>Endereço monitorado (seu roteador)</span>
        <input type="text" class="inp-alvo" data-link="${l.name}" value="${l.target || ''}"
               placeholder="192.168.200.254" inputmode="decimal" autocomplete="off"></label>` : ''}`;
    box.appendChild(bloco);
  });
}

async function carregarIfaces() {
  try {
    const r = await pegar('/api/links');
    estado.cfgLinks = r.links || [];
    estado.ifaces = r.ifaces || [];
    renderIfaces();
  } catch (e) { console.error(e); }
}

async function salvarIfaces() {
  const msg = $('c-if-msg');
  const corpo = { links: {} };
  document.querySelectorAll('.sel-iface').forEach((sel) => {
    corpo.links[sel.dataset.link] = { iface: sel.value };
  });
  document.querySelectorAll('.inp-alvo').forEach((inp) => {
    const alvo = corpo.links[inp.dataset.link];
    if (alvo) alvo.target = inp.value.trim();
  });
  msg.textContent = 'salvando…';
  msg.style.color = '';
  try {
    const r = await fetch('/api/links', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(corpo),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    estado.cfgLinks = j.links || estado.cfgLinks;
    if (j.aviso) { msg.textContent = '⚠️ ' + j.aviso; msg.style.color = '#ffd76a'; }
    else { msg.textContent = '✅ salvo — a medição já está usando a placa nova'; msg.style.color = '#8ef0b6'; }
    await carregarIfaces();
    // o histórico anterior continua no gráfico; o que muda é de onde vêm as
    // amostras daqui para a frente
    setTimeout(() => { carregarGraficos(); carregarResumo(); }, 2500);
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
  }
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
    $('c-auto-on').checked = c.auto_speed_enabled === '1';
    $('c-auto-hora').value = c.auto_speed_hora || '04:00';
    $('c-auto-dur').value = parseFloat(c.auto_speed_dur) || 5;
    $('c-scan-on').checked = c.auto_scan_enabled === '1';
    $('c-scan-hora').value = c.auto_scan_hora || '14:00';
    $('c-scan-portas').value = c.auto_scan_portas || 'rapido';
    estado.cfgScanRede = c.auto_scan_rede || '';
    renderRedesConfig();
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
    auto_speed_enabled: $('c-auto-on').checked ? '1' : '0',
    auto_speed_hora: $('c-auto-hora').value || '04:00',
    auto_speed_dur: $('c-auto-dur').value,
    auto_scan_enabled: $('c-scan-on').checked ? '1' : '0',
    auto_scan_hora: $('c-scan-hora').value || '14:00',
    auto_scan_portas: $('c-scan-portas').value,
    auto_scan_rede: $('c-scan-rede').value,
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
    estado.cfgScanRede = j.auto_scan_rede || '';
    carregarLogScan();                // o aviso do horário agendado é dali
    msg.textContent = '✅ salvo';
    msg.style.color = '#8ef0b6';
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
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
    if (j.ok) { msg.textContent = `✅ entregue (HTTP ${j.status_code})`; msg.style.color = '#8ef0b6'; }
    else { msg.textContent = `❌ falhou: ${j.erro || j.status_code || j.erro}`; msg.style.color = '#ffb3b3'; }
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
  }
}

/* ------------------------------------------------------------ manutenção */
function msgManut(txt, cor) {
  const m = $('m-msg');
  m.textContent = txt;
  m.style.color = cor || '';
  const v = $('vel-log-info');
  // o botão do CSV vive no painel de velocidade, longe do #m-msg lá embaixo
  if (v && txt && estado.msgNoLog) { v.textContent = txt; v.style.color = cor || ''; }
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
    msgManut(`✅ ${rotulo} baixado (${(blob.size / 1024).toFixed(0)} KB)`, '#8ef0b6');
  } catch (e) {
    msgManut('❌ ' + e.message, '#ffb3b3');
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
    msgManut('✅ ' + j.mensagem, '#8ef0b6');
    $('caixa-confirma').classList.add('oculto');
    $('r-confirma').value = '';
    estado.offset = 0;
    await carregarConfig();
    await Promise.all([carregarGraficos(), carregarResumo(), carregarEventos()]);
  } catch (e) {
    msgManut('❌ ' + e.message, '#ffb3b3');
  } finally {
    btn.disabled = true;               // volta travado: exige digitar APAGAR de novo
    btn.textContent = original;
  }
}

/* Os <select> de link são preenchidos a partir dos links que existem. */
function preencherFiltrosDeLink() {
  const nomes = nomesLinks();
  const op = (n) => `<option value="${n}">${n}</option>`;
  $('f-link').innerHTML = '<option value="">Todos</option>' + nomes.map(op).join('');
  const fv = $('f-vel-link');
  if (fv) fv.innerHTML = '<option value="">Todos</option>' + linksDeInternet().map(op).join('');
}

/* ------------------------------------------------------ varredura da rede
   O painel sabia tudo sobre os dois canos de internet e nada sobre a casa.
   Esta seção mostra o outro lado: quem está ligado aqui dentro.

   Duas coisas moldam o que aparece na tabela:
     · aparelho que não responde ao ping ainda aparece, porque para existir na
       rede ele precisa responder ao ARP — a maioria dos celulares está nesse
       caso, com firewall ligado;
     · cabo ou Wi-Fi é PALPITE pela assinatura da latência. Não existe como
       perguntar isso a um aparelho pela rede, e a página não finge que existe.
*/
const CONEXAO = {
  cabo: { chip: '🔌 cabo', cls: 'c-cabo' },
  wifi: { chip: '📶 Wi-Fi', cls: 'c-wifi' },
  indefinida: { chip: '? indefinida', cls: 'c-indefinida' },
  desconhecida: { chip: '— sem medida', cls: 'c-desconhecida' },
};

function linhaScan(h, modoPortas) {
  const con = CONEXAO[h.conexao] || CONEXAO.desconhecida;
  const nome = h.apelido || h.nome || h.tipo || '—';
  const marcas = [];
  if (h.eu) marcas.push('<span class="tag tag-eu">este aparelho</span>');
  if (h.gateway) marcas.push('<span class="tag tag-gw">roteador</span>');
  /* Duas novidades diferentes, e a distinção importa. `novo` é "estreou NESTA
     varredura" e some na próxima. `novo_semana` é "chegou nos últimos 7 dias" e
     é o que o usuário quer ver de relance — por isso é ele que ganha o ouro.
     Quem já estava aqui quando o netmon começou a olhar não conta como
     novidade: foi encontrado, não chegou. */
  if (h.novo) marcas.push('<span class="tag tag-novo">estreou agora</span>');
  else if (h.novo_semana) marcas.push(`<span class="tag tag-semana" title="primeira vez visto ${fmtDataHora(h.primeiro_visto)}">✨ novo na semana</span>`);
  if (h.mac_aleatorio && !h.eu) {
    marcas.push('<span class="tag tag-rand" title="MAC administrado localmente: '
      + 'o aparelho sorteia um endereço por rede, então o fabricante não pode ser '
      + 'identificado. É o padrão de celulares modernos.">MAC aleatório</span>');
  }
  const portas = (h.portas || []).length
    ? h.portas.map((p) => `<span class="porta" title="${escTxt(p.servico || '')}">${p.porta}${
        p.servico ? ` <small>${escTxt(p.servico)}</small>` : ''}</span>`).join('')
    : (modoPortas === 'nenhuma'
        ? '<span class="muted">não olhadas</span>'
        : '<span class="muted">nenhuma aberta entre as olhadas</span>');
  const lat = h.rtt_ms == null ? '<span class="muted">—</span>'
    : `${nf(h.rtt_ms, 2)} ms${h.jitter_ms == null ? '' : ` <small class="muted">±${nf(h.jitter_ms, 2)}</small>`}`;
  const desde = h.primeiro_visto
    ? `${fmtDataHora(h.primeiro_visto)}${h.idade_s != null && h.idade_s < 7 * 86400
        ? ` <small class="ouro-tx">(há ${fmtDur(h.idade_s)})</small>` : ''}`
    : '—';
  return `<tr class="${h.eu ? 'linha-eu' : ''}${h.novo || h.novo_semana ? ' linha-nova' : ''}">
    <td data-rot="Aparelho"><button class="sc-nome" type="button" data-mac="${escTxt(h.mac || '')}"
          title="clique para dar um apelido a este aparelho">${escTxt(nome)}</button>
      ${h.apelido && h.tipo ? `<small class="muted">${escTxt(h.tipo)}</small>` : ''}
      ${marcas.join('')}</td>
    <td data-rot="IP"><code>${escTxt(h.ip)}</code></td>
    <td data-rot="MAC"><code class="mac">${escTxt(h.mac || '—')}</code></td>
    <td data-rot="Fabricante">${escTxt(h.vendor || '—')}</td>
    <td data-rot="Conexão"><span class="chip-con ${con.cls}" title="${escTxt(h.conexao_motivo || '')}">${con.chip}</span></td>
    <td data-rot="Latência">${lat}</td>
    <td data-rot="Portas abertas" class="cel-portas">${portas}</td>
    <td data-rot="Conhecido desde">${desde}</td>
  </tr>`;
}

function renderScan() {
  const tb = $('tab-scan') && $('tab-scan').querySelector('tbody');
  if (!tb) return;
  const r = estado.scan.rodando || estado.scan.ultimo;
  const hosts = (r && r.hosts) || [];
  tb.innerHTML = hosts.length
    ? hosts.map((h) => linhaScan(h, r.modo_portas)).join('')
    : '<tr><td colspan="8" class="muted vazio">Nenhuma varredura ainda — escolha a rede e clique em Varrer.</td></tr>';

  const msg = $('sc-msg');
  if (msg && r) {
    if (r.fase === 'erro') {
      msg.innerHTML = `❌ ${escTxt(r.erro || 'falhou')}`;
      msg.style.color = '#ffb3b3';
    } else if (r.fase === 'fim' || !estado.scan.rodando) {
      const wifi = hosts.filter((h) => h.conexao === 'wifi').length;
      const cabo = hosts.filter((h) => h.conexao === 'cabo').length;
      const novos = hosts.filter((h) => h.novo_semana || h.novo).length;
      msg.innerHTML = `${hosts.length} aparelho(s) em <b>${escTxt(r.rede ? r.rede.cidr : '')}</b>`
        + ` · ${cabo} por cabo, ${wifi} por Wi-Fi`
        + (novos ? ` · <b class="ouro-tx">${novos} novo(s) na semana</b>` : '')
        + ` · varredura de ${fmtDataHora(r.ts)}${r.duracao_s ? ` (levou ${r.duracao_s}s)` : ''}`
        + (r.origem === 'auto' ? ' <span class="tag tag-auto">agendada</span>' : '');
      msg.style.color = '';
    }
  }

  const prog = $('sc-progresso');
  const rodando = estado.scan.rodando;
  if (prog) {
    prog.classList.toggle('oculto', !rodando);
    if (rodando) {
      const p = rodando.progresso || {};
      const pct = p.total ? Math.min(100, Math.round((p.feito / p.total) * 100)) : 5;
      $('sc-barra-fill').style.width = pct + '%';
      $('sc-etapa').textContent = `${p.etapa || rodando.fase} — ${p.feito || 0}/${p.total || '?'}`;
    }
  }
  const btn = $('sc-rodar');
  if (btn) {
    btn.disabled = !!rodando;
    btn.textContent = rodando ? 'varrendo…' : 'Varrer a rede';
  }
}

/* O <select> da varredura automática só pode ser preenchido depois que
   /api/scan disser quais redes existem, e a config pode chegar antes dele —
   por isso os dois chamam esta função e ela aguenta ser chamada cedo. */
function renderRedesConfig() {
  const sel = $('c-scan-rede');
  if (!sel) return;
  const escolhida = estado.cfgScanRede || '';
  sel.innerHTML = '<option value="">a rede de casa (LAN), escolhida sozinha</option>'
    + (estado.scan.redes || []).map((r) =>
        `<option value="${escTxt(r.id)}">${escTxt(r.rotulo)}</option>`).join('');
  sel.value = escolhida;
  // rede gravada que sumiu (troca de placa): o back-end cai de volta para a
  // LAN, e o campo tem de dizer isso em vez de mostrar uma escolha que não vale
  if (sel.value !== escolhida) sel.value = '';
}

function renderRedes() {
  const sel = $('sc-rede');
  if (!sel) return;
  const redes = estado.scan.redes || [];
  sel.innerHTML = redes.map((r) =>
    `<option value="${escTxt(r.id)}"${r.id === estado.scan.rede ? ' selected' : ''}>${escTxt(r.rotulo)}</option>`
  ).join('') || '<option value="">nenhuma rede local encontrada</option>';
}

async function carregarScan(rede) {
  try {
    const q = rede ? '?rede=' + encodeURIComponent(rede) : '';
    const r = await pegar('/api/scan' + q);
    estado.scan.redes = r.redes || [];
    estado.scan.rede = r.rede || (r.redes && r.redes[0] ? r.redes[0].id : null);
    estado.scan.ultimo = r.ultimo || null;
    estado.scan.rodando = r.rodando || null;
    renderRedes();
    renderRedesConfig();
    renderScan();
  } catch (e) { console.error('varredura', e); }
}

/* O log é o que sobrevive ao retrato: `scan:ultimo:*` guarda só a varredura
   mais recente de cada rede, sobrescrita toda vez. Aqui fica o histórico —
   inclusive a prova de que a varredura agendada rodou ontem. */
function renderLogScan() {
  const tb = $('tab-scanlog') && $('tab-scanlog').querySelector('tbody');
  if (!tb) return;
  const lista = estado.scan.log || [];
  tb.innerHTML = lista.length ? lista.map((v) => {
    const quem = (v.novos || []).map((n) =>
      `<span class="porta" title="${escTxt(n.mac || '')} · ${escTxt(n.ip || '')}">${
        escTxt(n.nome || n.vendor || n.mac || '?')}</span>`).join('') || '<span class="muted">—</span>';
    return `<tr class="${v.n_novos ? 'linha-nova' : ''}">
      <td data-rot="Quando">${fmtDataHora(v.ts)}</td>
      <td data-rot="Origem"><span class="tag ${v.origem === 'auto' ? 'tag-auto' : 'tag-manual'}">${
        v.origem === 'auto' ? 'agendada' : 'manual'}</span></td>
      <td data-rot="Rede">${escTxt(v.rotulo || v.rede_id)}</td>
      <td data-rot="Aparelhos">${v.erro ? '<span class="muted">—</span>' : v.total}</td>
      <td data-rot="Novos">${v.n_novos ? `<b class="ouro-tx">${v.n_novos}</b>` : '<span class="muted">0</span>'}</td>
      <td data-rot="Duração">${v.duracao_s == null ? '—' : v.duracao_s + 's'}</td>
      <td data-rot="Quem apareceu" class="cel-portas">${
        v.erro ? `<span class="erro-tx">falhou: ${escTxt(v.erro)}</span>` : quem}</td>
    </tr>`;
  }).join('')
    : '<tr><td colspan="7" class="muted vazio">Nenhuma varredura registrada ainda.</td></tr>';

  const info = $('scanlog-info');
  if (info) {
    const auto = estado.scan.auto || {};
    info.innerHTML = `${lista.length} varredura(s) no histórico · `
      + (auto.ligado
          ? `varredura automática <b>ligada</b>, todo dia às <b>${escTxt(auto.hora || '')}</b>`
            + (auto.ultima ? ` · última rodada em ${escTxt(auto.ultima)}` : '')
          : 'varredura automática <b>desligada</b>');
  }
  const av = $('sc-auto');
  if (av) {
    const auto = estado.scan.auto || {};
    av.innerHTML = auto.ligado
      ? `🕑 Varredura automática todo dia às <b>${escTxt(auto.hora || '')}</b> (horário de Brasília).`
      : '🕑 Varredura automática desligada — ligue em Configurações para ter um retrato por dia.';
  }
}

async function carregarLogScan() {
  try {
    const r = await pegar('/api/scan/log?limit=100');
    estado.scan.log = r.scans || [];
    estado.scan.auto = r.auto || null;
    renderLogScan();
  } catch (e) { console.error('log da varredura', e); }
}

async function rodarScan() {
  const msg = $('sc-msg');
  msg.textContent = 'iniciando a varredura…';
  msg.style.color = '';
  try {
    const r = await fetch('/api/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rede: $('sc-rede').value,
                             portas: $('sc-portas').value }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'erro');
    estado.scan.rodando = { fase: 'descobrindo', hosts: [], progresso: {} };
    renderScan();
  } catch (e) {
    msg.textContent = '❌ ' + e.message;
    msg.style.color = '#ffb3b3';
  }
}

/* O apelido é o único jeito honesto de saber que aquele MAC é "a TV da sala":
   nesta rede não há PTR, NetBIOS nem mDNS respondendo. Fica guardado pelo MAC,
   então sobrevive a troca de IP. */
async function apelidarAparelho(mac) {
  if (!mac) return;
  const r = estado.scan.rodando || estado.scan.ultimo;
  const h = ((r && r.hosts) || []).find((x) => x.mac === mac);
  const atual = (h && h.apelido) || '';
  const nome = window.prompt('Nome deste aparelho (deixe vazio para tirar o apelido):', atual);
  if (nome === null) return;
  try {
    const resp = await fetch('/api/scan/nome', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac, nome }),
    });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.erro || 'erro');
    if (h) h.apelido = j.nome;
    renderScan();
  } catch (e) {
    $('sc-msg').textContent = '❌ ' + e.message;
    $('sc-msg').style.color = '#ffb3b3';
  }
}

/* ------------------------------------------------------------ init */
// Um `$('x')` que devolve null derruba ligarEventos() inteiro, e com ele toda a
// inicialização — foi o que aconteceu quando um <details> saiu do HTML e o
// listener dele ficou para trás: a página subiu sem cards, sem gráficos e sem
// SSE. `liga` transforma isso num aviso no console em vez de uma página morta.
function liga(id, evento, fn, opcoes) {
  const el = $(id);
  if (!el) {
    console.warn('netmon: elemento #' + id + ' não existe; evento ' + evento + ' ignorado');
    return null;
  }
  el.addEventListener(evento, fn, opcoes);
  return el;
}

function ligarEventos() {
  document.querySelectorAll('.btn-per').forEach((b) => {
    b.addEventListener('click', () => escolherPeriodo(b.dataset.per));
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
  $('c-salvar-ifaces').addEventListener('click', salvarIfaces);
  $('c-recarregar-ifaces').addEventListener('click', async () => {
    $('c-if-msg').textContent = 'relendo…';
    await carregarIfaces();
    $('c-if-msg').textContent = `${estado.ifaces.length} placa(s) encontradas`;
  });

  $('f-vel-link').addEventListener('change', carregarVelocidade);
  $('btn-vel-csv').addEventListener('click', (ev) => {
    const f = $('f-vel-link').value;
    estado.msgNoLog = true;
    baixar('/api/speedtest.csv' + (f ? '?link=' + encodeURIComponent(f) : ''),
           'testes-velocidade.csv', 'Log dos testes', ev.currentTarget)
      .finally(() => { estado.msgNoLog = false; });
  });

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

  liga('sc-rodar', 'click', rodarScan);
  liga('sc-pdf', 'click', (ev) => {
    const rede = $('sc-rede') ? $('sc-rede').value : '';
    baixar('/api/scan.pdf' + (rede ? '?rede=' + encodeURIComponent(rede) : ''),
           'aparelhos-na-rede.pdf', 'Inventário da rede', ev.currentTarget);
  });
  liga('sc-rede', 'change', () => carregarScan($('sc-rede').value));
  liga('tab-scan', 'click', (ev) => {
    const b = ev.target.closest('.sc-nome');
    if (b) apelidarAparelho(b.dataset.mac);
  });

  $('mesh-toggle').addEventListener('click', alternarMesh);
  $('tr-rodar').addEventListener('click', rodarTrace);
  $('tr-destino').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') rodarTrace();
  });
  // trocar o link mostra o traçado anterior daquele caminho, sem traçar de novo
  $('tr-link').addEventListener('change', () =>
    renderTrace(estado.trace[$('tr-link').value] || null));

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
    // a faixa de perda também: o número de blocos vem da largura em pixels
    ['wrap-lat', 'perda-faixas'].forEach((id) => {
      const el = $(id);
      if (el) obs.observe(el);
    });
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
  const span = janela().span;
  // três cadências: ao vivo acompanha a sondagem; janelas de até uma hora
  // envelhecem rápido o bastante para valer 10 s; o resto é história e não
  // muda de figura em um minuto
  const graf = span <= SPAN_CURTO ? 2000 : span <= 3600 ? 10000 : 60000;
  const res = span <= SPAN_CURTO ? 5000 : span <= 3600 ? 20000 : 60000;
  estado.timers.graficos = setInterval(carregarGraficos, graf);
  estado.timers.resumo = setInterval(carregarResumo, res);
}

function relogio() {
  $('relogio').textContent = new Date().toLocaleTimeString('pt-BR', { hour12: false });
  if (estado.links.length) atualizarBanner(estado.links);
}

async function iniciar() {
  // antes de tudo: reestrutura as seções em cabeça + corpo. Se rodasse depois
  // de pintar, os gráficos já teriam sido desenhados dentro de seções fechadas,
  // com 0 px de largura.
  // Cada etapa isolada: uma falha ao ligar eventos não pode impedir os dados de
  // carregarem. Foi assim que a página ficou sem cards, sem gráficos e sem SSE
  // por causa de um único listener apontando para um elemento que saiu do HTML.
  try { montarRecolhiveis(); } catch (e) { console.error('recolhíveis:', e); }
  try { ligarEventos(); } catch (e) { console.error('eventos:', e); }
  // o período escolhido sobrevive ao F5; sem escolha nenhuma, "ao vivo"
  try {
    const salvo = localStorage.getItem(CHAVE_PERIODO);
    if (salvo && PERIODOS.some((p) => p.id === salvo)) estado.periodo = salvo;
  } catch (e) { /* segue no padrão */ }
  estado.span = janela().span;
  renderPeriodo();
  await carregarConfig();
  try {
    const s = await pegar('/api/status');
    estado.links = s.links;
    estado.porta = s.porta;
    estado.inicioDados = s.inicio_dados || null;
    renderPeriodo();
    // tudo que depende de QUAIS links existem vem depois desta resposta
    preencherFiltrosDeLink();
    montarLegenda();
    renderCards(s.links);
    renderDnsLan(s.dns_lan);
    atualizarBanner(s.links);
    $('rodape-info').textContent =
      `netmon · servidor no ar há ${fmtDurLonga(s.servidor_uptime_s)} · porta ${s.porta}` +
      (s.port_fallback ? ' (porta 666 indisponível — veja o README)' : '');
  } catch (e) { console.error(e); }
  await Promise.all([carregarGraficos(), carregarResumo(), carregarEventos(),
                     carregarVelocidade(), carregarTrace(), carregarAlvos(),
                     carregarMesh(), carregarScan(), carregarLogScan()]);
  conectar();
  if (!secaoRecolhida('Configurações e alertas')) carregarIfaces();
  setInterval(relogio, 1000);
  setInterval(carregarEventos, 60000);
  setInterval(carregarAlvos, 60000);
  setInterval(carregarMesh, 60000);
  agendarAtualizacoes();
}

document.addEventListener('DOMContentLoaded', iniciar);
