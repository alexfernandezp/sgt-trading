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
    "BRENT":      {"yf_ticker": "BZ=F",      "fallback": "CL=F", "description": "Brent Crude Oil"},
    "BRLUSD":     {"yf_ticker": "BRL=X",     "fallback": None,   "description": "Brazilian Real / USD"},
}

# Ruta a la carpeta data en OneDrive donde se depositan los CSVs de Barchart
BARCHART_DATA_PATH = os.getenv(
    "BARCHART_DATA_PATH",
    r"C:\Users\alejandro.fernandez\OneDrive - Sugar Global Trading\sgt_trading\data"
)

DATABENTO_API_KEY  = os.getenv("DATABENTO_API_KEY", "")

# ── USDA FAS PSD API ──────────────────────────────────────────────────────────
# Clave gratuita en: https://api.data.gov/signup/
# Sin clave: el script usa descarga bulk Excel como fallback automático.
USDA_API_KEY       = os.getenv("USDA_API_KEY", "")
USDA_PSD_BASE_URL  = "https://api.fas.usda.gov/api/psd"

# Azúcar centrífugo crudo No.11 (USDA PSD commodity code)
USDA_SUGAR_CODE    = "0612000"

# Países clave para balance global
USDA_COUNTRIES = {
    "WB": "World",
    "BR": "Brazil",
    "IN": "India",
    "TH": "Thailand",
    "EU": "European Union",
    "AU": "Australia",
    "CN": "China",
}

CFTC_API_URL       = "https://publicreporting.cftc.gov/resource/j83k-qyrd.json"
CFTC_SUGAR_MARKET  = "SUGAR NO. 11 - ICE FUTURES U.S."

# ── Google Earth Engine ───────────────────────────────────────────────────────
GEE_PROJECT_ID     = os.getenv("GEE_PROJECT_ID", "")

# ── Climate / ENSO ────────────────────────────────────────────────────────────
# Estaciones cinturón azucarero São Paulo para Open-Meteo
CLIMATE_STATIONS = [
    {"name": "ribeirao_preto", "lat": -21.1767, "lon": -47.8208},
    {"name": "piracicaba",     "lat": -22.7253, "lon": -47.6492},
]

# ICE Sugar No.11 — coste económico total de almacenamiento (full carry real)
# ICE Rule 11.20 almacén certificado: $0.0202/MT/día (warehouse fee puro)
# + seguro (~$0.2/ton/año) + deterioro calidad + operativa ≈ $0.037/MT/día
# Los operadores comerciales usan este valor total, no solo la tasa ICE listada
ICE_STORAGE_USD_TON_DAY = float(os.getenv("ICE_STORAGE_USD_TON_DAY", "0.037"))

# SOFR rate por defecto (actualizar trimestralmente o via FRED fetch)
SOFR_DEFAULT_PCT        = float(os.getenv("SOFR_DEFAULT_PCT", "4.30"))
