import os
from dotenv import load_dotenv

load_dotenv()

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME", "sgt_trading")

DATABASE_URL       = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN_DATABASE_URL = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/postgres"

ACCOUNT_SIZE_USD    = 500_000
RISK_PER_TRADE_PCT  = 0.01
RISK_PER_TRADE_USD  = ACCOUNT_SIZE_USD * RISK_PER_TRADE_PCT   # $5,000
MAX_LOTS            = 20
CONTRACT_SIZE_LBS   = 112_000
TICK_SIZE           = 0.01
TICK_VALUE          = CONTRACT_SIZE_LBS * TICK_SIZE / 100      # $11.20

INSTRUMENTS = {
    "SBN26":      {"yf_ticker": "SBN26.NYB", "fallback": None,   "description": "Sugar No.11 Jul 2026"},
    "SBV26":      {"yf_ticker": "SBV26.NYB", "fallback": None,   "description": "Sugar No.11 Oct 2026"},
    "SB_CONT":    {"yf_ticker": "SB=F",      "fallback": None,   "description": "Sugar No.11 continuo"},
    "WHITE_SUGAR":{"yf_ticker": "SW=F",      "fallback": None,   "description": "White Sugar No.5 London"},
    "BRENT":      {"yf_ticker": "BZ=F",      "fallback": "CL=F", "description": "Brent Crude Oil"},
    "BRLUSD":     {"yf_ticker": "BRL=X",     "fallback": None,   "description": "Brazilian Real / USD"},
}

# Ruta a la carpeta data en OneDrive donde se depositan los CSVs de Barchart
BARCHART_DATA_PATH = os.getenv(
    "BARCHART_DATA_PATH",
    r"C:\Users\alejandro.fernandez\OneDrive - Sugar Global Trading\sgt_trading\data"
)

DATABENTO_API_KEY  = os.getenv("DATABENTO_API_KEY", "")

CFTC_API_URL       = "https://publicreporting.cftc.gov/resource/j83k-qyrd.json"
CFTC_SUGAR_MARKET  = "SUGAR NO. 11 - ICE FUTURES U.S."
