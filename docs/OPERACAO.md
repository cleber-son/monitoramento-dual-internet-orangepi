# Manual de operação — a instalação de casa

> Documento do dia a dia desta instalação específica: endereços, comandos,
> webhook do Discord passo a passo e o formato exato do payload.
> Para a visão geral do projeto, veja o [README](../README.md).

Feito **só com a biblioteca padrão do Python 3.10** — não há Node, nem pip, nem
dependência externa. A página também não usa CDN: ela precisa abrir justamente
quando a internet cair.

## Acesso

    http://192.168.18.2:666/        (eth0 / GIGA — endereço estável)
    http://192.168.200.249:666/     (adaptador USB na rede 192.168.200.0)
    http://192.168.17.3:666/        (adaptador USB na rede da IMPACTO)

O servidor escuta em `0.0.0.0`, então responde em **qualquer** IP do aparelho.
Sem login: qualquer um na LAN acessa.

> Se a porta 666 não estiver liberada, o serviço cai sozinho para a **8666**
> e a página avisa no rodapé.

## Os dois comandos que exigem root

Rodam uma vez só; depois disso o sistema se vira sozinho.

**1. Rota da IMPACTO.** O DHCP entrega IP na interface da Impacto mas não instala
rota default, porque a conexão `enx-lan` está com `ipv4.never-default: yes`.
Sem rota default naquela interface, nenhuma sonda consegue sair por ela:

    sudo nmcli connection modify enx-lan ipv4.never-default no ipv4.route-metric 700
    sudo nmcli connection up enx-lan

A métrica 700 é alta de propósito: a IMPACTO entra como **backup**, a GIGA
continua sendo o caminho padrão de tudo. Nada muda no uso normal da rede.

**2. Porta 666.** O kernel reserva portas abaixo de 1024:

    echo 'net.ipv4.ip_unprivileged_port_start=666' | sudo tee /etc/sysctl.d/90-netmon.conf
    sudo sysctl --system

Depois é só reiniciar o serviço que ele sobe na 666.

## Operação

    # ver se está no ar
    curl -s http://127.0.0.1:666/api/health

    # logs
    tail -f /home/orangepi/netmon/netmon.log

    # reiniciar (o cron reergue em até 1 minuto)
    kill $(cat /home/orangepi/netmon/netmon.pid)

    # parar de vez
    crontab -l | grep -v netmon/run.sh | crontab -
    kill $(cat /home/orangepi/netmon/netmon.pid)

Sobe sozinho no boot e se recupera de travamento via cron — não há `sudo` sem
senha aqui, então systemd de sistema está fora de alcance:

    @reboot sleep 25; /home/orangepi/netmon/run.sh
    * * * * * /home/orangepi/netmon/run.sh

O `run.sh` usa `flock`: se ninguém segura o lock ele vira o serviço; se já há
uma instância, ele apenas confere `/api/health` e, após 3 falhas seguidas, mata
o processo travado para o minuto seguinte reerguer.

## Como cada link é identificado

Pela **interface**, nunca por IP:

| Link | Interface | Rede hoje |
|---|---|---|
| GIGA | `eth0` | 192.168.18.2, gw 192.168.18.1 (ONT Huawei) |
| IMPACTO | `enx00e04c534458` | 192.168.17.3, gw 192.168.17.1 |

O IP e o gateway são redetectados a cada 60 s. **Trocar o cabo de rede de lugar
não quebra nada** — o painel passa a mostrar o endereço novo sozinho.

Toda sonda é presa à interface (`ping -I`, `SO_BINDTODEVICE`), então nada vaza
pela rota default nem pela NordVPN.

Os alvos de ping são `9.9.9.9` e `208.67.222.222` de propósito: existem rotas
estáticas fixando `1.1.1.1` e `8.8.8.8` em `dev eth0`, o que falsearia a medição
da IMPACTO.

## O que é medido, a cada 2 segundos

As três sondas ICMP de cada ciclo (alvo primário, alvo alternativo e gateway)
rodam **em paralelo**. Em série, um ciclo com o link caído custaria ~4,6 s de
timeouts somados e o alerta se arrastaria; em paralelo custa ~1,5 s, que é o que
permite confirmar uma queda em 3–4 s.

| Sonda | Frequência | Para quê |
|---|---|---|
| ICMP internet (2 alvos × 3 pacotes) | 2 s | latência min/méd/máx, jitter, perda |
| ICMP gateway (2 pacotes) | 2 s | separar "caiu o provedor" de "caiu o roteador" |
| DNS (consulta A crua, UDP) | 30 s | resolução real pelo link, sem passar no Pi-hole |
| TCP handshake :443 | 30 s | confirma que não é só ICMP passando |
| HTTP 204 | 60 s | pega bloqueio/portal cativo |
| Redetecção de IP e gateway | 60 s | sobrevive a troca de cabo e de rede |

## Janelas do painel

Os gráficos e as estatísticas seguem o período escolhido: **30 s · 1 min · 1 h ·
6 h · 24 h · 7 d · 30 d**. Até 5 min a página redesenha a cada 2 s (o mesmo
ritmo da sondagem); acima disso, a cada minuto — o Orange Pi não precisa
redesenhar 30 dias de série toda hora.

## Quando dispara alerta

- **QUEDA**: 2 ciclos seguidos com 100% de perda nos **dois** alvos — alerta em
  **~4 s**. A hora registrada é a do **primeiro ciclo ruim**, não a da
  confirmação: é a hora real em que caiu.
- **RETORNO**: 3 ciclos bons seguidos (~6 s). Histerese assimétrica: é mais
  difícil voltar do que continuar caído, para não anunciar retorno em link
  instável.
- **LATÊNCIA ALTA**: média móvel acima do limiar (padrão **80 ms**) por ~10 s, ou
  perda ≥ 20% por ~6 s, ou jitter > 60 ms. Um pico acima de **3× o limiar**
  (240 ms) alerta em ~4 s. Só sai de degradado após 20 s abaixo de 80% do limiar.

O limiar de 80 ms é agressivo de propósito: os dois links ficam em ~4 ms, então
qualquer coisa acima disso já é anomalia gritante, não ruído.
- **INSTÁVEL**: 3+ quedas em 10 min marcam o evento como *flapping* e seguram os
  webhooks até estabilizar.

Limiares editáveis na própria página, em *Configurações*.

## Webhook

POST JSON para a URL configurada. Se a GIGA (rota padrão) estiver caída, o envio
é repetido **preso à interface do outro link** — senão o aviso de queda nunca
sairia.

O payload é **traduzido conforme o destino**, porque cada serviço exige um
formato próprio:

| URL contém | Formato enviado |
|---|---|
| `discord.com/api/webhooks` | embed do Discord (título, cor por tipo de evento, campos) |
| `hooks.slack.com` | `{"text": "…"}` |
| qualquer outra | o JSON genérico abaixo |

### Discord — passo a passo

1. No servidor do Discord: **Editar canal** (engrenagem ao lado do canal) →
   **Integrações** → **Webhooks** → **Novo webhook**.
2. Dê um nome (ex.: `netmon`), escolha o canal e clique em **Copiar URL do webhook**.
   Ela tem a forma `https://discord.com/api/webhooks/<id>/<token>`.
3. Na página do netmon: **Configurações** → cole em *URL do webhook* → marque
   *Enviar alertas para o webhook* → **Salvar** → **Testar webhook**.
4. Deve aparecer um cartão azul no canal em segundos.

A URL do webhook é uma credencial: quem tiver ela escreve no seu canal. Ela fica
guardada no banco local e aparece truncada no arquivo de logs. Se vazar, use
**Excluir webhook** no Discord e gere outro.

Cores do embed: vermelho na queda, verde no retorno, amarelo na latência alta.

```json
{"source":"netmon","host":"orangepi3-lts","event":"queda",
 "link":"IMPACTO","iface":"enx00e04c534458","estado":"DOWN",
 "causa":"provedor","inicio":"2026-08-28T14:03:22-03:00","fim":null,
 "duracao_s":null,"duracao_txt":null,"flapping":false,
 "metricas":{"rtt_avg":null,"loss":100,"gw_ok":true,
             "ip":"192.168.17.3","gateway":"192.168.17.1"},
 "mensagem":"🔴 IMPACTO CAIU as 14:03:22 (provavel queda do provedor)"}
```

`event`: `queda` · `recuperacao` · `latencia_alta` · `latencia_normalizada` · `teste`

## Teste de velocidade

Botão por link, no painel **Teste de velocidade**. Mede download e upload reais
contra `speed.cloudflare.com` — e, se ela responder `429` (muitos testes do
mesmo IP), o download cai automaticamente para `cachefly.cachefly.net`, com o
soquete preso à interface — o número é
daquele link, não da rota padrão. Até o nome do servidor é resolvido por uma
consulta DNS presa à interface, senão testar a IMPACTO com a GIGA caída falharia
logo na resolução.

Feito com `socket` + `ssl` da biblioteca padrão: neste aparelho o Python
sustenta ~450 Mbps, bem acima dos links. O primeiro trecho de cada conexão é
descartado (TCP em *slow start* ainda mente), e cada conexão de download baixa
no máximo 50 MB porque o servidor recusa pedidos maiores.

| | |
|---|---|
| Duração | 3, 5 ou 10 s por direção (escolhida na página) |
| Teto por teste | 150 MB de download · 250 MB de upload |
| Simultaneidade | um teste por vez no aparelho (`409` se pedir outro) |
| Progresso | pelo SSE, evento `speedtest` |

Enquanto o teste roda, o link fica cheio **de propósito** — por isso o alerta de
latência alta daquele link fica suspenso durante o teste e por mais 40 s. A
detecção de **queda** continua valendo normalmente.

Se as duas fontes recusarem, o painel mostra o motivo. Quando só o upload
falha, o download medido é guardado do mesmo jeito, com o aviso ao lado.
Os resultados ficam na tabela `speedtests` e aparecem no painel como histórico.

## Banco e retenção

SQLite em WAL, escritor único, commit em lote a cada 30 s para poupar o cartão.
A ponta ainda não gravada fica numa fila em memória e é servida junto pela API —
sem isso a janela de 30 s do painel apareceria vazia.

| Camada | Retenção | Tamanho |
|---|---|---|
| amostras de 2 s | 48 h | ~15 MB |
| média por minuto | 90 dias | ~25 MB |
| média por hora | 5 anos | ~17 MB |
| eventos de queda | para sempre | < 1 MB |
| testes de velocidade | para sempre | < 1 MB |

Estabiliza abaixo de **70 MB** — há 20 GB livres. Purga e checkpoint às 4h.

Custo em produção: **20 MB de RAM e ~2,5% de CPU**.

## API

| Rota | O que devolve |
|---|---|
| `GET /api/status` | estado atual dos 2 links, uptime 24h/7d/30d, evento aberto |
| `GET /api/samples?link=GIGA&from=&to=&res=auto` | série temporal (`raw`/`minute`/`hour`) |
| `GET /api/events?link=&tipo=&limit=&offset=` | histórico de quedas com duração |
| `GET /api/summary?period=24h` | uptime, nº de quedas, downtime, rtt, jitter, perda |
| `GET /api/speedtest?link=&limit=` | último teste de cada link, histórico e o que está rodando |
| `POST /api/speedtest` | dispara o teste: `{"link":"GIGA","dur":5}` — `409` se já houver um |
| `GET /api/config` · `POST /api/config` | limiares, webhook, som |
| `POST /api/webhook/test` | dispara um payload de teste |
| `GET /api/stream` | SSE ao vivo (`status` a cada 2 s, `alerta` na hora) |
| `GET /api/health` | usado pelo watchdog |
| `GET /api/report.pdf?period=24h` | relatório em PDF (gerado em Python puro) |
| `GET /api/logs` | pacote de diagnóstico: estado, config, eventos e o log |
| `POST /api/reset` | apaga o histórico — exige `{"confirmar":"APAGAR"}` |

### Reset

`escopo: "historico"` apaga amostras, agregados, eventos e testes de velocidade, mantendo suas
configurações. `escopo: "tudo"` também devolve limiares e webhook ao padrão.
Roda `VACUUM` para devolver o espaço ao disco e reinicia a máquina de estados
das duas sondas. **É irreversível** — a interface exige digitar `APAGAR`.

### Relatório PDF

Gerado por `pdf.py`, um escritor de PDF 1.4 escrito à mão (não há reportlab nem
navegador headless neste aparelho): fontes base-14, texto em WinAnsi e gráficos
vetoriais. Contém resumo executivo, tabela de métricas lado a lado, gráfico de
latência com as faixas de queda marcadas, linha de disponibilidade e o histórico
paginado de eventos.

## Arquivos

    netmon.py    entrypoint, threads, sinais
    speedtest.py teste de download/upload preso a interface
    db.py        schema, escritor único, rollups, retenção
    probe.py     sondas e máquina de estados
    alerts.py    regras de alerta, webhook, barramento SSE
    server.py    API HTTP, SSE, estáticos
    run.sh       lock de instância única + watchdog
    static/      index.html, app.js, style.css
    data/        netmon.db
