"""Test rapido del trade card sin interaccion."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from services.trade_setup import compute_trade_setup
from services.anchored_vwap import get_vwap_bands
from services.volume_profile import get_multiframe_vp
from services.mtf_alignment import compute_mtf_alignment
from services.entry_zone import compute_entry_zone
from services.scoring import get_current_price
from services.backtest import estimate_win_rate
from scripts.score_today import print_trade_card

session = SessionLocal()
direction = sys.argv[1].upper() if len(sys.argv) > 1 else "SHORT"
decision  = sys.argv[2].upper() if len(sys.argv) > 2 else "REDUCED"

price     = get_current_price(session, "SBN26")
vwap_data = get_vwap_bands(session)
vp_dict   = get_multiframe_vp(session)
mtf       = compute_mtf_alignment(session, direction)
ez        = compute_entry_zone(session, direction)
bt        = estimate_win_rate(session, direction, atr_mult_stop=1.0)
setup     = compute_trade_setup(session, direction, decision, price, vwap_data, vp_dict)

if setup:
    print_trade_card(setup, bt, mtf, ez, vp_dict)
else:
    print("ERROR: compute_trade_setup devolvio None")

session.close()
