"""Crea tabla shadow_trades. Correr una vez."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import SessionLocal
from sqlalchemy import text

DDL = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id              SERIAL PRIMARY KEY,
    signal_date     DATE        NOT NULL,
    signal_ts       TIMESTAMP   NOT NULL DEFAULT NOW(),
    instrument      VARCHAR(20) NOT NULL DEFAULT 'SBN26',

    direction       VARCHAR(10),
    decision        VARCHAR(20),
    score_total     INTEGER,
    score_max       INTEGER DEFAULT 12,
    l1_long         INTEGER,
    l1_short        INTEGER,
    l2_long         INTEGER,
    l2_short        INTEGER,
    l2_valid        INTEGER,
    veto            BOOLEAN DEFAULT FALSE,

    entry_price     NUMERIC(10,4),
    sl_price        NUMERIC(10,4),
    tp1_price       NUMERIC(10,4),
    tp2_price       NUMERIC(10,4),

    cot_pct         NUMERIC(6,1),
    vwap_sigma      NUMERIC(6,2),
    fundamental_dir VARCHAR(10),
    fundamental_bias NUMERIC(6,3),

    close_1d        NUMERIC(10,4),
    close_5d        NUMERIC(10,4),
    close_10d       NUMERIC(10,4),
    close_20d       NUMERIC(10,4),
    ret_1d          NUMERIC(9,5),
    ret_5d          NUMERIC(9,5),
    ret_10d         NUMERIC(9,5),
    ret_20d         NUMERIC(9,5),
    dir_ret_1d      NUMERIC(9,5),
    dir_ret_5d      NUMERIC(9,5),
    dir_ret_10d     NUMERIC(9,5),
    dir_ret_20d     NUMERIC(9,5),

    UNIQUE(signal_date, instrument)
);
"""

with SessionLocal() as s:
    s.execute(text(DDL))
    s.commit()
print("shadow_trades: OK")
