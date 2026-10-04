#!/usr/bin/env python3
"""Envia whitelist.txt + blacklist-apostas.txt para as "Regras do usuario" do AdGuard DNS.

A janela do painel so aceita um dominio por vez; a API aceita a lista inteira.
As listas sao lidas do GitHub (a mesma URL raw que o painel usaria), entao basta
editar o arquivo no repo e rodar de novo -- ou deixar no cron.

Chave da API: crie em https://adguard-dns.io/en/dashboard/user-settings/api-keys
e grave em ~/.adguard-api-key (chmod 600).

Uso:
  ./sync-adguard.py              # mostra o que mudaria, nao envia nada
  ./sync-adguard.py --aplicar    # envia para o servidor "Principal"
  ./sync-adguard.py --aplicar --servidor "Outro"

ATENCAO: a API substitui as regras do usuario inteiras. Regras criadas a mao no
painel somem -- coloque-as nos arquivos do repo.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.adguard-dns.io/oapi/v1"
RAW = "https://raw.githubusercontent.com/cleber-son/monitoramento-dual-internet-orangepi/main/adguard/"
LISTAS = ["whitelist.txt", "blacklist-apostas.txt"]
LIMITE = 1000  # plano atual; o gratis e 100
AQUI = os.path.dirname(os.path.abspath(__file__))


def ler_lista(nome):
    try:
        with urllib.request.urlopen(RAW + nome, timeout=20) as r:
            texto = r.read().decode()
    except (urllib.error.URLError, OSError) as e:
        print(f"  {nome}: GitHub falhou ({e}), usando a copia local", file=sys.stderr)
        with open(os.path.join(AQUI, nome)) as f:
            texto = f.read()
    return [l.strip() for l in texto.splitlines()
            if l.strip() and not l.lstrip().startswith(("!", "#"))]


def api(metodo, caminho, chave, corpo=None):
    req = urllib.request.Request(
        API + caminho, method=metodo,
        data=json.dumps(corpo).encode() if corpo is not None else None,
        headers={"Authorization": f"ApiKey {chave}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            dados = r.read()
            return json.loads(dados) if dados else None
    except urllib.error.HTTPError as e:
        sys.exit(f"API {metodo} {caminho}: HTTP {e.code} {e.read().decode()[:300]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="envia de verdade")
    ap.add_argument("--servidor", default="Principal", help="nome do servidor DNS no painel")
    args = ap.parse_args()

    try:
        with open(os.path.expanduser("~/.adguard-api-key")) as f:
            chave = f.read().strip()
    except FileNotFoundError:
        sys.exit("Falta a chave: grave em ~/.adguard-api-key")

    regras = []
    for nome in LISTAS:
        lidas = ler_lista(nome)
        print(f"{nome}: {len(lidas)} regras")
        regras += [r for r in lidas if r not in regras]
    if len(regras) > LIMITE:
        sys.exit(f"{len(regras)} regras passa do limite de {LIMITE} do plano")

    servidores = api("GET", "/dns_servers", chave)
    alvo = next((s for s in servidores if s["name"] == args.servidor), None)
    if not alvo:
        sys.exit(f"Servidor '{args.servidor}' nao existe. Ha: {[s['name'] for s in servidores]}")

    # a listagem traz rules vazio; so o GET do servidor traz as regras
    atuais = api("GET", f"/dns_servers/{alvo['id']}", chave)["settings"]["user_rules_settings"]["rules"]
    novas, saem = set(regras) - set(atuais), set(atuais) - set(regras)
    print(f"Servidor '{alvo['name']}' ({alvo['id']}): {len(atuais)} regras hoje -> {len(regras)}"
          f"  (+{len(novas)} / -{len(saem)})")
    for r in sorted(saem):
        print(f"  sai: {r}")

    if not args.aplicar:
        print("Nada enviado. Rode com --aplicar para enviar.")
        return
    api("PUT", f"/dns_servers/{alvo['id']}/settings", chave,
        {"user_rules_settings": {"enabled": True, "rules": regras}})
    print("Enviado. Regras do usuario ligadas.")


if __name__ == "__main__":
    main()
