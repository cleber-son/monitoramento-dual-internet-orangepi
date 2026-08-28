"""Persistencia do netmon: schema, escritor unico, rollups e retencao.

Regras de concorrencia:
  - amostras passam por uma fila e sao gravadas em lote pela thread `Writer`
  - eventos e config sao gravados na hora, com lock proprio (baixa frequencia)
  - leituras da API abrem conexao read-only por thread; WAL nunca bloqueia leitor
"""

import collections
import json
import logging
import os
import queue
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "netmon.db")

# Intervalo entre ciclos de sondagem, em segundos. Vive aqui porque tanto as
# sondas quanto o calculo de uptime do servidor precisam do mesmo numero.
SAMPLE_INTERVAL = 2

log = logging.getLogger("netmon.db")

# Retencao por camada (segundos)
RAW_KEEP = 48 * 3600            # amostras de 5s: 48h
MINUTE_KEEP = 90 * 86400        # agregado por minuto: 90 dias
HOUR_KEEP = 5 * 365 * 86400     # agregado por hora: 5 anos

AGG_COLS = """
  ts INTEGER NOT NULL, link_id INTEGER NOT NULL,
  rtt_avg REAL, rtt_min REAL, rtt_max REAL, jitter_avg REAL,
  loss_avg REAL, loss_max REAL, dns_avg REAL, tcp_avg REAL,
  n_samples INTEGER NOT NULL, n_up INTEGER NOT NULL,
  n_degraded INTEGER NOT NULL, n_down INTEGER NOT NULL,
  PRIMARY KEY (ts, link_id)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL UNIQUE,
  iface   TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS samples (
  ts      INTEGER NOT NULL,
  link_id INTEGER NOT NULL,
  state   TEXT    NOT NULL,
  loss    REAL    NOT NULL,
  rtt_min REAL, rtt_avg REAL, rtt_max REAL, jitter REAL,
  gw_ok   INTEGER NOT NULL,
  gw_rtt  REAL,
  dns_ms  REAL,
  tcp_ms  REAL,
  http_ok INTEGER,
  PRIMARY KEY (ts, link_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_minute (%s) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg_hour   (%s) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id    INTEGER NOT NULL,
  type       TEXT NOT NULL,
  cause      TEXT,
  started_at INTEGER NOT NULL,
  ended_at   INTEGER,
  duration_s INTEGER,
  flapping   INTEGER NOT NULL DEFAULT 0,
  details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events ON events(link_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_open ON events(ended_at) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS speedtests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id    INTEGER NOT NULL,
  ts         INTEGER NOT NULL,
  down_mbps  REAL,
  up_mbps    REAL,
  ping_ms    REAL,
  jitter_ms  REAL,
  bytes_down INTEGER,
  bytes_up   INTEGER,
  dur_down   REAL,
  dur_up     REAL,
  servidor   TEXT,
  erro       TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed ON speedtests(link_id, ts DESC);

CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""" % (AGG_COLS, AGG_COLS)

DEFAULT_CONFIG = {
    "webhook_url": "",
    "webhook_enabled": "0",
    # 80 ms e agressivo de proposito: os dois links ficam em ~4 ms, entao
    # qualquer coisa acima de 80 ms ja e anomalia gritante e nao ruido
    "lat_limiar_ms": "80",
    "loss_limiar_pct": "20",
    "jitter_limiar_ms": "60",
    "som_habilitado": "1",
    "cooldown_s": "300",
}

# A identidade de cada link e a INTERFACE, nunca um IP: o gateway e o endereco
# sao redetectados em runtime, entao trocar o cabo de rede nao quebra nada.
#   eth0             -> ONT da GIGA (192.168.18.1)
#   enx00e04c534458  -> adaptador USB ligado ao roteador da IMPACTO (192.168.17.1)
LINKS = [(1, "GIGA", "eth0"), (2, "IMPACTO", "enx00e04c534458")]


def _tune(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


def connect(readonly=False):
    if readonly:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        _tune(conn)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for lid, name, iface in LINKS:
            conn.execute(
                "INSERT INTO links(id,name,iface) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, iface=excluded.iface",
                (lid, name, iface),
            )
        for k, v in DEFAULT_CONFIG.items():
            conn.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)", (k, v))
        conn.commit()
        log.info("banco pronto em %s", DB_PATH)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Config (sincrono, lock proprio)
# --------------------------------------------------------------------------
_cfg_lock = threading.Lock()
_cfg_cache = {}


def load_config():
    global _cfg_cache
    conn = connect(readonly=True)
    try:
        rows = conn.execute("SELECT key,value FROM config").fetchall()
        _cfg_cache = {r["key"]: r["value"] for r in rows}
        return dict(_cfg_cache)
    finally:
        conn.close()


def get_config():
    return dict(_cfg_cache) if _cfg_cache else load_config()


def cfg_int(key, default):
    try:
        return int(float(get_config().get(key, default)))
    except (TypeError, ValueError):
        return default


def set_config(pairs):
    with _cfg_lock:
        conn = connect()
        try:
            for k, v in pairs.items():
                conn.execute(
                    "INSERT INTO config(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )
            conn.commit()
        finally:
            conn.close()
    return load_config()


# --------------------------------------------------------------------------
# Eventos (sincrono: precisamos do id de volta e nao podemos perder em crash)
# --------------------------------------------------------------------------
_ev_lock = threading.Lock()


def open_event(link_id, type_, started_at, cause=None, details=None, flapping=0):
    with _ev_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO events(link_id,type,cause,started_at,flapping,details) "
                "VALUES(?,?,?,?,?,?)",
                (link_id, type_, cause, int(started_at), int(flapping),
                 json.dumps(details, ensure_ascii=False) if details else None),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def close_event(event_id, ended_at, details=None):
    with _ev_lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT started_at, details FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            dur = max(0, int(ended_at) - int(row["started_at"]))
            merged = None
            if details:
                base = {}
                if row["details"]:
                    try:
                        base = json.loads(row["details"])
                    except ValueError:
                        base = {}
                base.update(details)
                merged = json.dumps(base, ensure_ascii=False)
            if merged is not None:
                conn.execute(
                    "UPDATE events SET ended_at=?, duration_s=?, details=? WHERE id=?",
                    (int(ended_at), dur, merged, event_id),
                )
            else:
                conn.execute(
                    "UPDATE events SET ended_at=?, duration_s=? WHERE id=?",
                    (int(ended_at), dur, event_id),
                )
            conn.commit()
            return dur
        finally:
            conn.close()


def mark_flapping(event_id):
    with _ev_lock:
        conn = connect()
        try:
            conn.execute("UPDATE events SET flapping=1 WHERE id=?", (event_id,))
            conn.commit()
        finally:
            conn.close()


def open_events():
    """Eventos ainda abertos - usado para reconciliar depois de um restart."""
    conn = connect(readonly=True)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE ended_at IS NULL ORDER BY started_at"
        ).fetchall()]
    finally:
        conn.close()


def count_recent_events(link_id, type_, since_ts):
    conn = connect(readonly=True)
    try:
        r = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE link_id=? AND type=? AND started_at>=?",
            (link_id, type_, int(since_ts)),
        ).fetchone()
        return r["c"]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Testes de velocidade (sincrono: sao raros e disparados a mao)
# --------------------------------------------------------------------------
SPEED_COLS = ("link_id", "ts", "down_mbps", "up_mbps", "ping_ms", "jitter_ms",
              "bytes_down", "bytes_up", "dur_down", "dur_up", "servidor", "erro")


def save_speedtest(row):
    with _ev_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO speedtests(%s) VALUES(%s)"
                % (",".join(SPEED_COLS), ",".join("?" * len(SPEED_COLS))),
                tuple(row.get(c) for c in SPEED_COLS),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_speedtests(link_id=None, limit=20):
    conn = connect(readonly=True)
    try:
        sql = ("SELECT s.*, l.name link FROM speedtests s JOIN links l ON l.id=s.link_id")
        params = []
        if link_id:
            sql += " WHERE s.link_id=?"
            params.append(link_id)
        sql += " ORDER BY s.ts DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def last_speedtests():
    """Ultimo teste util de cada link, para a pagina abrir ja preenchida.

    Vale o teste que mediu o download mesmo que o upload tenha falhado: meio
    numero e melhor que nenhum.
    """
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT s.*, l.name link FROM speedtests s JOIN links l ON l.id=s.link_id "
            "WHERE s.down_mbps IS NOT NULL AND s.id IN ("
            "  SELECT MAX(id) FROM speedtests WHERE down_mbps IS NOT NULL"
            "  GROUP BY link_id)"
        ).fetchall()
        return {r["link"]: dict(r) for r in rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Escritor de amostras
# --------------------------------------------------------------------------
SAMPLE_SQL = (
    "INSERT OR REPLACE INTO samples"
    "(ts,link_id,state,loss,rtt_min,rtt_avg,rtt_max,jitter,gw_ok,gw_rtt,dns_ms,tcp_ms,http_ok)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

FLUSH_EVERY = 30      # segundos
FLUSH_MAX = 100       # linhas

# Ponta ainda nao gravada. O commit em lote poupa o cartao, mas deixa ate 30s de
# amostras so na fila -- e justamente o que a janela de 30s do painel quer ver.
# Cabem ~10 min dos dois links; a leitura da API junta isto ao que ja esta no
# banco.
AMOSTRAS_RECENTES = collections.deque(maxlen=600)


def amostras_recentes(link_id, frm, to, depois_de=None):
    """Amostras ja coletadas que ainda nao foram para o banco."""
    return [s for s in list(AMOSTRAS_RECENTES)
            if s["link_id"] == link_id and frm <= s["ts"] <= to
            and (depois_de is None or s["ts"] > depois_de)]


class Writer(threading.Thread):
    """Unica thread que escreve amostras; commit em lote para poupar o cartao."""

    def __init__(self, stop_event):
        super().__init__(name="db-writer", daemon=True)
        self.q = queue.Queue(maxsize=5000)
        self.stop_event = stop_event

    def put(self, sample):
        AMOSTRAS_RECENTES.append(sample)
        try:
            self.q.put_nowait(sample)
        except queue.Full:
            log.warning("fila de amostras cheia; descartando")

    def run(self):
        conn = connect()
        buf = []
        last_flush = time.time()
        try:
            while not self.stop_event.is_set() or buf or not self.q.empty():
                timeout = max(0.5, FLUSH_EVERY - (time.time() - last_flush))
                try:
                    item = self.q.get(timeout=timeout)
                    buf.append(item)
                except queue.Empty:
                    pass
                due = (time.time() - last_flush) >= FLUSH_EVERY
                if buf and (due or len(buf) >= FLUSH_MAX):
                    self._flush(conn, buf)
                    buf = []
                    last_flush = time.time()
                if self.stop_event.is_set() and self.q.empty() and not buf:
                    break
            if buf:
                self._flush(conn, buf)
        finally:
            conn.close()
            log.info("db-writer encerrado")

    @staticmethod
    def _flush(conn, buf):
        rows = [
            (s["ts"], s["link_id"], s["state"], s["loss"], s.get("rtt_min"),
             s.get("rtt_avg"), s.get("rtt_max"), s.get("jitter"), int(s.get("gw_ok", 0)),
             s.get("gw_rtt"), s.get("dns_ms"), s.get("tcp_ms"),
             None if s.get("http_ok") is None else int(s["http_ok"]))
            for s in buf
        ]
        try:
            conn.executemany(SAMPLE_SQL, rows)
            conn.commit()
        except sqlite3.Error as exc:
            log.error("falha gravando %d amostras: %s", len(rows), exc)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass


# --------------------------------------------------------------------------
# Rollups e retencao
# --------------------------------------------------------------------------
ROLL_MIN_SQL = """
INSERT OR REPLACE INTO agg_minute
SELECT (ts/60)*60, link_id,
       AVG(rtt_avg), MIN(rtt_min), MAX(rtt_max), AVG(jitter),
       AVG(loss), MAX(loss),
       AVG(CASE WHEN dns_ms>0 THEN dns_ms END),
       AVG(CASE WHEN tcp_ms>0 THEN tcp_ms END),
       COUNT(*),
       SUM(state='UP'), SUM(state='DEGRADED'), SUM(state IN ('DOWN','NO_LINK'))
FROM samples WHERE ts>=? AND ts<? GROUP BY (ts/60)*60, link_id
"""

ROLL_HOUR_SQL = """
INSERT OR REPLACE INTO agg_hour
SELECT (ts/3600)*3600, link_id,
       SUM(rtt_avg*n_samples)/NULLIF(SUM(CASE WHEN rtt_avg IS NOT NULL THEN n_samples END),0),
       MIN(rtt_min), MAX(rtt_max),
       SUM(jitter_avg*n_samples)/NULLIF(SUM(CASE WHEN jitter_avg IS NOT NULL THEN n_samples END),0),
       SUM(loss_avg*n_samples)/NULLIF(SUM(n_samples),0), MAX(loss_max),
       AVG(dns_avg), AVG(tcp_avg),
       SUM(n_samples), SUM(n_up), SUM(n_degraded), SUM(n_down)
FROM agg_minute WHERE ts>=? AND ts<? GROUP BY (ts/3600)*3600, link_id
"""


def rollup_minutes(now=None):
    now = int(now or time.time())
    end = (now // 60) * 60          # so minutos ja fechados
    start = end - 10 * 60           # refaz os ultimos 10 min (idempotente)
    conn = connect()
    try:
        conn.execute(ROLL_MIN_SQL, (start, end))
        conn.commit()
    finally:
        conn.close()


def rollup_hours(now=None):
    now = int(now or time.time())
    end = (now // 3600) * 3600
    start = end - 3 * 3600
    conn = connect()
    try:
        conn.execute(ROLL_HOUR_SQL, (start, end))
        conn.commit()
    finally:
        conn.close()


def reset(escopo="historico"):
    """Apaga o historico. escopo='tudo' tambem devolve a config ao padrao.

    Destrutivo e irreversivel - a rota HTTP exige confirmacao explicita.
    """
    conn = connect()
    try:
        for tabela in ("samples", "agg_minute", "agg_hour", "events", "speedtests"):
            conn.execute("DELETE FROM %s" % tabela)
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('events','speedtests')")
        except sqlite3.Error:
            pass                      # a tabela so existe se ja houve AUTOINCREMENT
        if escopo == "tudo":
            conn.execute("DELETE FROM config")
            for k, v in DEFAULT_CONFIG.items():
                conn.execute("INSERT INTO config(key,value) VALUES(?,?)", (k, v))
        conn.commit()
        conn.execute("VACUUM")        # devolve o espaco ao disco
        AMOSTRAS_RECENTES.clear()     # senao o painel reexibiria o que foi apagado
        log.warning("RESET executado (escopo=%s)", escopo)
    finally:
        conn.close()
    load_config()


def purge(now=None):
    now = int(now or time.time())
    conn = connect()
    try:
        conn.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_KEEP,))
        conn.execute("DELETE FROM agg_minute WHERE ts < ?", (now - MINUTE_KEEP,))
        conn.execute("DELETE FROM agg_hour WHERE ts < ?", (now - HOUR_KEEP,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
