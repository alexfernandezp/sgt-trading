import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import SessionLocal
from services.trade_setup import compute_trade_setup
from services.anchored_vwap import get_vwap_bands
from services.volume_profile import get_multiframe_vp

session = SessionLocal()
vwap  = get_vwap_bands(session, 'SBN26')
vp    = get_multiframe_vp(session, 'SBN26')

s = compute_trade_setup(session, 'SHORT', 'REDUCED',
    entry_price=15.05, vwap_bands=vwap, vp_dict=vp, cluster_stop=None)

if s:
    print("Entry       : %.4f c/lb" % s['entry'])
    print("Stop 2xATR  : %.4f  (+%.4fc)" % (s['stop_atr_30m'], s['stop_atr_30m'] - s['entry']))
    print("Stop Final  : %.4f  (+%.4fc)  [%s]" % (s['stop_final'], s['stop_final'] - s['entry'], s['stop_type']))
    print("Riesgo/lote : $%.2f" % s['risk_per_lot_usd'])
    print("Lotes       : %d   Riesgo total: $%.0f  (%.2f%% cuenta)" % (
        s['lots_final'], s['total_risk_usd'], s['pct_account']))
    print()
    print("T1  [%-16s]  %.4f  R/R=%.1fx  P&L +$%.0f" % (s['t1_name'], s['t1'], s['rr_t1'], s['t1_usd']))
    print("T2  [%-16s]  %.4f  R/R=%.1fx  P&L +$%.0f  <- objetivo" % (s['t2_name'], s['t2'], s['rr_t2'], s['t2_usd']))
    print("T3  [%-16s]  %.4f  R/R=%.1fx  P&L +$%.0f" % (s['t3_name'], s['t3'], s['rr_t3'], s['t3_usd']))
    print()
    print("Gate R/R    : %s" % ("OK" if s['rr_gate_passed'] else "BLOQUEADO - " + str(s['rr_gate_reason'])))
    print()
    print("Escenario adverso si stop alcanzado:")
    for a in s['adverse']:
        print("  %.4f  %-20s (+%.4fc del stop)" % (a['price'], a['name'], a['dist_from_stop']))

session.close()
