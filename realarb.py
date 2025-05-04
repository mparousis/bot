#!/usr/bin/env python3
# coding: utf-8
# Triangular Arbitrage Live-Trading GUI Bot for Gate.io
# Includes auto triangle discovery, real/paper trades, balance display, stop button,
# automatic GT accumulation, live GT balance, and rollback safeguards

import time
import hmac
import hashlib
import threading
import requests
import tkinter as tk
from tkinter import ttk
from datetime import datetime

# --- CONFIGURATION ---
API_TICKERS     = "https://api.gateio.ws/api/v4/spot/tickers"
API_ORDER       = "https://api.gateio.ws/api/v4/spot/orders"
API_ACCOUNTS    = "https://api.gateio.ws/api/v4/spot/accounts"
# Insert your actual Gate.io API key below (keep the quotes)
API_KEY         = "8b7d682cb13c82d0a693bd4d9f495651"
API_SECRET      = "8c7c0571247d85b5c07c24ca8d3d6e93803c710c16e81194cd5ad549e608deaf"
FEE             = 0.0009       # 0.09% taker fee
THRESHOLD       = 0.002        # 0.2% profit threshold
CACHE_TTL       = 0.5          # seconds to cache tickers
ORDER_INTERVAL  = 0.25         # seconds between REST calls (~10/sec limit)
SCAN_INTERVAL   = 1.0          # seconds between arbitrage scans
GT_PURCHASE_USD = 5.0          # every $5 profit triggers GT purchase
GT_PURCHASE_QTY = 2.0          # buy 2 GT per tranche
GT_PAIR         = "GT_USDT"

# --- STATE ---
balance_usdt    = 0.0
balance_gt      = 0.0
profit_since_gt = 0.0          # accumulate profit towards GT purchase
live_trading    = False
position        = {}

tickers_cache   = []
t0              = 0
valid_loops     = []
disabled_loops  = set()
_run_scanner    = True

# --- GUI SETUP ---
root = tk.Tk()
root.title("Gate.io Triangular Arb")

# Balances display
balance_var_usdt = tk.StringVar(); balance_var_gt = tk.StringVar()
balance_var_usdt.set(f"USDT: {balance_usdt:.2f}")
balance_var_gt.set(f"GT:   {balance_gt:.2f}")
frame = ttk.Frame(root); frame.pack(pady=5)
ttk.Label(frame, textvariable=balance_var_usdt, font=(None,12,'bold')).grid(row=0,column=0,padx=5)
ttk.Label(frame, textvariable=balance_var_gt,   font=(None,12,'bold')).grid(row=0,column=1,padx=5)

# Stop button
def stop_trading():
    global _run_scanner, live_trading
    _run_scanner = False
    live_trading = False
    stop_button.config(state='disabled')
    gui_log("Trading stopped by user.")
stop_button = ttk.Button(root, text="Stop Trading", command=stop_trading)
stop_button.pack(pady=5)

cols = ("loop","profit_pct","status")
tree = ttk.Treeview(root, columns=cols, show="headings")
for c in cols:
    tree.heading(c, text=c.upper())
tree.pack(fill="both", expand=True)
log_txt = tk.Text(root, height=8, state="disabled")
log_txt.pack(fill="both")

# --- GUI HELPERS ---
def gui_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    def _append():
        log_txt.configure(state="normal")
        log_txt.insert("end", f"[{ts}] {msg}\n")
        log_txt.configure(state="disabled")
        log_txt.see("end")
    root.after(0, _append)

def gui_update_loop(loop, profit_pct, status):
    key = "→".join(loop)
    def _():
        if not tree.exists(key):
            tree.insert("","end",iid=key,values=(key,f"{profit_pct*100:.2f}%",status))
        else:
            tree.set(key,"profit_pct",f"{profit_pct*100:.2f}%")
            tree.set(key,"status",status)
    root.after(0, _)

# --- AUTH & ORDERS ---
def sign(params: dict) -> str:
    payload = '&'.join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(API_SECRET.encode('utf-8'), payload.encode('utf-8'), hashlib.sha512).hexdigest()

def test_credentials():
    global live_trading, balance_usdt, balance_gt
    try:
        # Added +1 second compensation for clock drift
        ts = str(int(time.time() + 1))
        params = {"time": ts}
        sig = sign(params)
        headers = {
            "KEY": API_KEY,
            "SIGN": sig,
            "Timestamp": ts,
            "Content-Type": "application/json"  # Added content-type
        }
        resp = requests.get(API_ACCOUNTS, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        for acct in data:
            curr = acct.get("currency"); avail = float(acct.get("available",0))
            if curr=="USDT": balance_usdt = avail; balance_var_usdt.set(f"USDT: {balance_usdt:.2f}")
            if curr=="GT":   balance_gt   = avail; balance_var_gt.set(f"GT:   {balance_gt:.2f}")
        live_trading = True
        gui_log(f"Live trading enabled. Balances USDT={balance_usdt:.2f}, GT={balance_gt:.2f}")
    except Exception as e:
        gui_log(f"Credential test failed: {e}. Using $10 test balance, 0 GT.")
        live_trading = False
        balance_usdt = 10.0
        balance_gt    = 0.0
        balance_var_usdt.set(f"USDT: {balance_usdt:.2f}")
        balance_var_gt.set(f"GT:   {balance_gt:.2f}")

# Place order with IOC
def place_order(pair, side, amount, price, order_type="limit", tif="IOC"):
    ts = str(int(time.time() + 1))  # Added +1 second compensation
    params={"currency_pair":pair,"type":order_type,"account":"spot","side":side,
            "amount":str(amount),"price":str(price),"time":ts,"timeInForce":tif}
    sig=sign(params)
    headers={
        "KEY":API_KEY,
        "SIGN":sig,
        "Timestamp":ts,
        "Content-Type":"application/json"  # Added content-type
    }
    resp = requests.post(API_ORDER, params=params, headers=headers, timeout=5)
    resp.raise_for_status()
    return resp.json().get("id")

# Get order status
def get_order_status(order_id):
    ts = str(int(time.time() + 1))  # Added +1 second compensation
    params={"time":ts}
    sig = sign(params)
    headers={
        "KEY":API_KEY,
        "SIGN":sig,
        "Timestamp":ts,
        "Content-Type":"application/json"  # Added content-type
    }
    resp = requests.get(f"{API_ORDER}/{order_id}", params=params, headers=headers, timeout=5)
    resp.raise_for_status()
    return resp.json()

def wait_for_fill(order_id, timeout=2.0):
    start=time.time(); filled=0.0
    while time.time()-start<timeout:
        try:
            o = get_order_status(order_id)
            filled = float(o.get("filled_total", 0))
            if o.get("status")=="closed": break
        except: pass
        time.sleep(0.1)
    return filled

# --- TICKERS & TRIANGLES ---
def fetch_tickers():
    global tickers_cache, t0
    now=time.time()
    if now - t0 > CACHE_TTL:
        try:
            resp=requests.get(API_TICKERS, timeout=5)
            resp.raise_for_status()
            tickers_cache = resp.json()
            t0 = now
        except Exception as e:
            gui_log(f"Error fetching tickers: {e}")

def discover_triangles():
    fetch_tickers()
    pairs = {t['currency_pair'] for t in tickers_cache}
    split = {t['currency_pair']: t['currency_pair'].split('_') for t in tickers_cache}
    loops=[]
    for cp in pairs:
        b,q = split[cp]
        if q!='USDT': continue
        for cp2 in pairs:
            b2,q2 = split[cp2]
            if q2!=b: continue
            loop=[f"{b}_USDT", f"{b2}_{b}", f"{b2}_USDT"]
            if all(p in pairs for p in loop): loops.append(loop)
    unique=[]; seen=set()
    for l in loops:
        tpl=tuple(l)
        if tpl not in seen:
            seen.add(tpl); unique.append(l)
    return unique

# --- PRICE ---
def get_book(pair):
    fetch_tickers()
    for t in tickers_cache:
        if t['currency_pair']==pair:
            bid,ask = t.get('highest_bid'), t.get('lowest_ask')
            if not bid or not ask: raise ValueError('Empty price')
            return float(bid), float(ask)
    raise ValueError(f"Pair {pair} not found")

# --- ROLLBACK ---
def rollback(fills):
    gui_log("Initiating rollback...")
    # reverse fills in reverse leg order
    if 'leg2' in fills:
        try:
            bid2,_ = get_book(fills['pair2'])
            pid=place_order(fills['pair2'], 'sell', fills['leg2'], bid2)
            gui_log(f"Rolled back leg2 ({fills['pair2']}): order {pid}")
        except Exception as e:
            gui_log(f"Rollback error leg2: {e}")
    if 'leg1' in fills:
        try:
            bid1,_ = get_book(fills['pair1'])
            pid=place_order(fills['pair1'], 'sell', fills['leg1'], bid1)
            gui_log(f"Rolled back leg1 ({fills['pair1']}): order {pid}")
        except Exception as e:
            gui_log(f"Rollback error leg1: {e}")

# --- SCAN & TRADE ---
def scan():
    global balance_usdt, balance_gt, profit_since_gt, _run_scanner
    while _run_scanner:
        if balance_usdt <= 0:
            gui_log("Zero USDT balance; stopping scan.")
            break
        for loop in valid_loops:
            tpl=tuple(loop)
            if tpl in disabled_loops: continue
            try:
                bid1,ask1 = get_book(loop[0])
                bid2,ask2 = get_book(loop[1])
                bid3,ask3 = get_book(loop[2])
            except Exception as e:
                gui_log(f"Disabling {loop}: {e}")
                disabled_loops.add(tpl)
                continue
            qty_a = balance_usdt / ask1 * (1 - FEE)
            qty_b = qty_a / ask2     * (1 - FEE)
            end_usd= qty_b * bid3     * (1 - FEE)
            profit= end_usd - balance_usdt; pct=profit / balance_usdt if balance_usdt else 0
            gui_update_loop(loop, pct, 'Ready')
            if pct > THRESHOLD:
                fills={'pair1':loop[0],'pair2':loop[1]}
                if live_trading:
                    try:
                        id1=place_order(loop[0],'buy',balance_usdt/ask1,ask1,'limit','IOC')
                        fills['leg1']=wait_for_fill(id1)
                        time.sleep(ORDER_INTERVAL)
                        id2=place_order(loop[1],'buy',fills['leg1']/ask2,ask2,'limit','IOC')
                        fills['leg2']=wait_for_fill(id2)
                        time.sleep(ORDER_INTERVAL)
                        id3=place_order(loop[2],'sell',fills['leg2'],bid3,'limit','IOC')
                        wait_for_fill(id3)
                        balance_usdt=end_usd; balance_var_usdt.set(f"USDT: {balance_usdt:.2f}")
                        profit_since_gt += profit
                        # GT purchase
                        while profit_since_gt >= GT_PURCHASE_USD:
                            bid_gt,ask_gt = get_book(GT_PAIR)
                            pid=place_order(GT_PAIR,'buy',GT_PURCHASE_QTY,ask_gt,'limit','IOC')
                            balance_gt += GT_PURCHASE_QTY; balance_var_gt.set(f"GT: {balance_gt:.2f}")
                            profit_since_gt -= GT_PURCHASE_USD
                            gui_log(f"Purchased {GT_PURCHASE_QTY} GT: order {pid}")
                        gui_log(f"Executed {loop}: {id1},{id2},{id3}")
                        gui_update_loop(loop,pct,'Executed')
                    except Exception as e:
                        gui_log(f"Order error {loop}: {e}")
                        rollback(fills)
                else:
                    # simulation
                    balance_usdt=end_usd; balance_var_usdt.set(f"USDT: {balance_usdt:.2f}")
                    profit_since_gt += profit
                    while profit_since_gt >= GT_PURCHASE_USD:
                        balance_gt     += GT_PURCHASE_QTY
                        balance_var_gt.set(f"GT: {balance_gt:.2f}")
                        profit_since_gt -= GT_PURCHASE_USD
                        gui_log(f"Simulated GT purchase: +{GT_PURCHASE_QTY} GT")
                    gui_log(f"Simulated trade {loop}: +{pct*100:.2f}% -> {balance_usdt:.2f}")
                    gui_update_loop(loop,pct,'Simulated')
        time.sleep(SCAN_INTERVAL)

if __name__=='__main__':
    test_credentials()
    valid_loops = discover_triangles()
    allowlist_pairs = sorted({p for loop in valid_loops for p in loop} | {GT_PAIR})
    gui_log("API Key Allowlist pairs:\n" + ", ".join(allowlist_pairs))
    gui_log(f"Discovered {len(valid_loops)} loops")
    threading.Thread(target=scan, daemon=True).start()
    root.mainloop()