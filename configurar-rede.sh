#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Configura a rede do Orange Pi para o netmon, de um jeito que sobrevive a
# troca de cabo entre placas.
#
# O problema que este script resolve: os papeis (GIGA, IMPACTO, rede local)
# estavam presos ao NOME da placa (eth0, enx00e0...). Trocar o cabo de lugar
# embaralhava tudo -- a rota default ia parar na internet errada e o IP fixo do
# Pi-hole sumia. Aqui o papel passa a ser decidido pelo GATEWAY que o DHCP
# entrega, que e o unico dado que pertence de verdade a cada operadora:
#
#   192.168.18.1    -> GIGA     -> rota default (metrica 100)
#   192.168.17.1    -> IMPACTO  -> reserva      (metrica 700)
#   192.168.200.x   -> rede local -> nunca rota default, IP fixo
#
# Um dispatcher do NetworkManager reaplica essa regra sozinho toda vez que uma
# placa sobe ou renova o DHCP. Ou seja: da proxima vez que o cabo mudar de
# porta, nao ha nada para configurar.
#
# Rode uma vez:   sudo /home/orangepi/netmon/configurar-rede.sh
# ---------------------------------------------------------------------------
set -euo pipefail

CONF=/etc/netmon-rede.conf
DISPATCHER=/etc/NetworkManager/dispatcher.d/50-netmon-metricas

# A rede local precisa de IP FIXO: este aparelho e o servidor DNS (Pi-hole) da
# rede 192.168.200.0/24 e o painel do netmon mora nele. Se o endereco mudar a
# cada troca de cabo, a casa inteira fica sem resolver nome.
LAN_IFACE=${LAN_IFACE:-eth0}
LAN_IP=${LAN_IP:-192.168.200.249}
LAN_MASCARA=${LAN_MASCARA:-24}


# Escreve um arquivo de forma ATOMICA e durável: conteudo pela entrada padrao,
# para um temporario no mesmo sistema de arquivos, com fsync, e so entao o
# rename. Sem isso o ext4 pode registrar o arquivo e perder o conteudo se a
# maquina reiniciar logo depois -- foi exatamente o que aconteceu em 30/08:
# este script rodou, o usuario reiniciou em seguida, e tanto o $CONF quanto o
# $DISPATCHER ficaram com ZERO BYTE. O dispatcher vazio nunca aplicou metrica
# nenhuma, e a rota preferida foi parar na operadora errada sem ninguem notar.
gravar_atomico() {
    destino=$1
    modo=$2
    tmp="${destino}.tmp.$$"
    cat > "$tmp"
    chmod "$modo" "$tmp"
    # fsync do arquivo e do diretorio: o rename so vale se o conteudo ja estiver
    # no disco. `sync` do coreutils faz fsync no arquivo informado.
    sync "$tmp" 2>/dev/null || sync
    mv -f "$tmp" "$destino"
    sync "$(dirname "$destino")" 2>/dev/null || sync
    if [ ! -s "$destino" ]; then
        echo "!! falha gravando $destino (ficou vazio)" >&2
        exit 1
    fi
}

if [ "$(id -u)" -ne 0 ]; then
    echo "Precisa de root. Rode:  sudo $0" >&2
    exit 1
fi

echo "==> 1/4  tabela de papeis por gateway em $CONF"
# -s e nao -f: arquivo VAZIO precisa ser refeito. E o grep garante que uma
# tabela da versao antiga (sem a coluna de IP de servico) tambem seja refeita.
if [ ! -s "$CONF" ] || ! grep -q '^[0-9].*/[0-9]' "$CONF"; then
    gravar_atomico "$CONF" 644 <<'EOF'
# Papel de cada rede, pelo gateway que o DHCP entrega.
#   <gateway> <metrica|lan> [ip-de-servico/prefixo]
#
# metrica menor = caminho preferido para a internet.
# "lan" = rede local: nunca vira rota default e nao entrega DNS ao sistema.
#
# O terceiro campo e um endereco que este aparelho precisa TER naquela rede,
# independente da placa em que o cabo estiver. Aqui e o IP do Pi-hole: o
# roteador da GIGA anuncia 192.168.18.2 como servidor DNS para a rede inteira,
# entao esse endereco tem que seguir o cabo da GIGA. Deixa-lo preso ao perfil de
# uma placa foi o que derrubou o DNS da casa quando o adaptador USB mudou.
192.168.18.1    100  192.168.18.2/24   # GIGA - principal + IP do Pi-hole
192.168.17.1    700                    # IMPACTO - reserva
192.168.200.254 lan                    # roteador de casa
EOF
    echo "    criado"
else
    echo "    ja existe e tem conteudo, mantido como esta"
fi

echo "==> 2/4  dispatcher do NetworkManager em $DISPATCHER"
gravar_atomico "$DISPATCHER" 755 <<'EOF'
#!/usr/bin/env bash
# Reaplica as metricas de rota do netmon quando uma placa sobe ou renova DHCP.
# Instalado por /home/orangepi/netmon/configurar-rede.sh -- nao edite aqui.
set -u
CONF=/etc/netmon-rede.conf
IFACE="${1:-}"
ACAO="${2:-}"

case "$ACAO" in
    up|dhcp4-change|connectivity-change) ;;
    *) exit 0 ;;
esac
[ -n "$IFACE" ] || exit 0
[ -f "$CONF" ] || exit 0

log() { logger -t netmon-rede -- "$*"; }

GW=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')

# A placa da rede local nao tem rota default -- de proposito. Sem este bloco o
# dispatcher saia aqui e a regra "lan" do $CONF nunca chegava a ser aplicada.
# Entao, quando nao ha default, procuramos no $CONF um gateway que esteja na
# MESMA sub-rede desta placa. Todas as redes daqui sao /24, entao comparar os
# tres primeiros octetos basta e evita aritmetica de mascara em shell.
if [ -z "${GW:-}" ]; then
    PREFIXO=$(ip -4 -o addr show dev "$IFACE" 2>/dev/null \
              | awk '{print $4}' | cut -d/ -f1 | head -1 \
              | awk -F. '{print $1"."$2"."$3}')
    if [ -n "${PREFIXO:-}" ]; then
        GW=$(awk -v p="$PREFIXO." '$1 ~ "^"p {print $1; exit}' "$CONF")
    fi
fi
[ -n "${GW:-}" ] || exit 0

PAPEL=$(awk -v gw="$GW" '$1==gw {print $2; exit}' "$CONF" | tr -d '[:space:]')
[ -n "${PAPEL:-}" ] || exit 0

# --- endereco de servico: segue o cabo, nunca a placa ----------------------
# Terceiro campo da linha deste gateway. Se existir, esta placa PRECISA ter esse
# endereco, e nenhuma outra pode te-lo -- duas placas com o mesmo IP em redes
# diferentes quebraria o roteamento de forma silenciosa.
SERV=$(awk -v gw="$GW" '$1==gw && $3 ~ /\// {print $3; exit}' "$CONF")
if [ -n "${SERV:-}" ]; then
    SERV_IP=${SERV%%/*}
    for OUTRA in $(ls /sys/class/net); do
        [ "$OUTRA" = "$IFACE" ] && continue
        if ip -4 -o addr show dev "$OUTRA" 2>/dev/null | grep -q " $SERV_IP/"; then
            ip addr del "$SERV" dev "$OUTRA" 2>/dev/null || true
            log "$OUTRA: $SERV removido (o cabo daquela rede mudou de placa)"
        fi
    done
    if ! ip -4 -o addr show dev "$IFACE" | grep -q " $SERV_IP/"; then
        ip addr add "$SERV" dev "$IFACE" 2>/dev/null \
            && log "$IFACE (gw $GW): $SERV adicionado" \
            || log "$IFACE: NAO consegui adicionar $SERV"
    fi
fi

UUID=$(nmcli -g GENERAL.CON-UUID device show "$IFACE" 2>/dev/null | head -1)

if [ "$PAPEL" = "lan" ]; then
    # rede local: nao pode virar caminho de internet nem entregar resolvedor
    ip -4 route show default dev "$IFACE" | while read -r _ _ g _; do
        ip route del default via "$g" dev "$IFACE" 2>/dev/null || true
    done
    [ -n "$UUID" ] && nmcli connection modify "$UUID" \
        ipv4.never-default yes ipv4.ignore-auto-dns yes 2>/dev/null || true
    log "$IFACE (gw $GW): rede local, fora da rota default"
    exit 0
fi

# internet: uma unica rota default, com a metrica do papel
for M in $(ip -4 route show default dev "$IFACE" | grep -oE 'metric [0-9]+' | awk '{print $2}'); do
    [ "$M" = "$PAPEL" ] && continue
    ip route del default via "$GW" dev "$IFACE" metric "$M" 2>/dev/null || true
done
ip route replace default via "$GW" dev "$IFACE" metric "$PAPEL" 2>/dev/null || true
# grava tambem no perfil, senao o proximo boot volta com a metrica automatica
[ -n "$UUID" ] && nmcli connection modify "$UUID" \
    ipv4.route-metric "$PAPEL" ipv4.never-default no 2>/dev/null || true
log "$IFACE (gw $GW): rota default com metrica $PAPEL"
exit 0
EOF
echo "    instalado ($(wc -c < "$DISPATCHER") bytes)"

echo "==> 2.5/4  tirando os IPs de servico dos perfis do NetworkManager"
# Quem manda no endereco de servico passa a ser o dispatcher, pelo gateway. Se
# ele continuasse gravado no perfil de uma placa, o NetworkManager o aplicaria
# na placa errada assim que o cabo mudasse de porta -- e o endereco apareceria
# numa rede onde ele nao existe, enquanto a rede certa ficaria sem ele.
awk '$3 ~ /\// {print $3}' "$CONF" | while read -r SERV; do
    [ -n "$SERV" ] || continue
    nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1}' \
    | while read -r PERFIL; do
        ATUAIS=$(nmcli -g ipv4.addresses connection show "$PERFIL" 2>/dev/null || true)
        case ",$ATUAIS," in
            *"$SERV"*)
                nmcli connection modify "$PERFIL" -ipv4.addresses "$SERV" 2>/dev/null \
                    && echo "    removido $SERV do perfil \"$PERFIL\"" ;;
        esac
    done
done
echo "    o dispatcher passa a aplica-los pelo gateway"

echo "==> 3/4  IP fixo da rede local em $LAN_IFACE ($LAN_IP/$LAN_MASCARA)"
UUID_LAN=$(nmcli -g GENERAL.CON-UUID device show "$LAN_IFACE" 2>/dev/null | head -1 || true)
if [ -z "${UUID_LAN:-}" ]; then
    echo "    !! $LAN_IFACE nao tem conexao ativa; criando um perfil novo"
    nmcli connection add type ethernet ifname "$LAN_IFACE" con-name netmon-lan >/dev/null
    UUID_LAN=$(nmcli -g connection.uuid connection show netmon-lan)
fi
nmcli connection modify "$UUID_LAN" \
    ipv4.method manual \
    ipv4.addresses "$LAN_IP/$LAN_MASCARA" \
    ipv4.gateway "" \
    ipv4.dns "" \
    ipv4.ignore-auto-dns yes \
    ipv4.never-default yes \
    ipv6.method ignore \
    connection.autoconnect yes
# DE PROPOSITO nao ativamos o perfil agora: enquanto o cabo da rede local nao
# estiver em $LAN_IFACE, subir o IP fixo criaria uma segunda rota para
# 192.168.200.0/24 apontando para a placa errada -- e derrubaria na hora a
# sessao SSH de quem esta rodando este script. O perfil vale no proximo boot,
# que e justamente quando o cabo ja vai estar no lugar certo.
echo "    gravado no perfil (vale no proximo boot)"
echo "    depois de reiniciar, o painel mora em http://$LAN_IP:666/ para sempre"

echo "==> 4/4  reaplicando as metricas em todas as placas"
for DEV in $(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="ethernet"{print $1}'); do
    if [ "$DEV" = "$LAN_IFACE" ]; then
        # $LAN_IFACE ainda esta com o cabo antigo. Nao chamamos o dispatcher
        # nele para nao sobrescrever o perfil de rede local que acabou de ser
        # gravado; so tiramos a rota default que ele ainda carrega, senao
        # ficariam duas rotas default com a mesma metrica disputando a saida.
        while read -r _ _ G _; do
            [ -n "${G:-}" ] && ip route del default via "$G" dev "$DEV" 2>/dev/null || true
        done < <(ip -4 route show default dev "$DEV")
        echo "    $DEV: rota default removida (vira rede local no proximo boot)"
        continue
    fi
    "$DISPATCHER" "$DEV" up || true
done

echo
echo "--- rotas agora ---"
ip -4 route show default
echo
echo "Pronto. A partir daqui, trocar o cabo de porta nao exige mais nada:"
echo "o dispatcher reconhece a rede pelo gateway e ajusta sozinho."
