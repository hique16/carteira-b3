"""
Carteira B3 — Servidor Web para Render.com
Lê Google Sheets + Yahoo Finance e serve o dashboard online.
"""

import json, os, time, threading
from datetime import datetime
from pathlib import Path
from flask import Flask, Response

# ── Dependências ──────────────────────────────────────────────────────────────
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

REFRESH_MIN = 30
BASE_DIR    = Path(__file__).parent
_cache      = {"html": "<h2 style='font-family:sans-serif;padding:2rem'>Carregando dashboard...</h2>", "lock": threading.Lock()}

# ── Variáveis de ambiente (configuradas no Render) ────────────────────────────
SHEET_ID    = os.environ.get("SHEET_ID", "")
CREDS_JSON  = os.environ.get("GOOGLE_CREDS_JSON", "")   # conteúdo do credenciais.json como string

# ── Google Sheets ─────────────────────────────────────────────────────────────
def connect_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds_dict = json.loads(CREDS_JSON)
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)

def parse_quant(s):
    if not s: return 0
    s = str(s).strip().replace("R$","").replace(" ","").replace(".","").replace(",","")
    try: return int(s)
    except: return 0

def parse_price(s):
    if not s: return 0.0
    s = str(s).strip().replace("R$","").replace(" ","")
    if "," in s:
        s = s.replace(".","").replace(",",".")
    try: return float(s)
    except: return 0.0

def read_from_sheets(sheet):
    portfolio = []; divs_received = []; stops = {}; cal_map = {}

    # Central
    try:
        ws   = sheet.worksheet("Central")
        rows = ws.get_all_values()
        for row in rows[2:]:
            if len(row) < 4: continue
            setor  = str(row[0]).strip() or "Outros"
            ticker = str(row[1]).strip().upper()
            if not ticker or not (4 <= len(ticker) <= 7) or not ticker.replace("_","").isalnum(): continue
            quant = parse_quant(row[2])
            pm    = parse_price(row[3])
            if quant <= 0 or pm <= 0: continue
            if any(a["t"] == ticker for a in portfolio): continue
            portfolio.append({"t":ticker,"name":ticker,"sector":setor,"q":quant,"pm":round(pm,6)})
    except Exception as e:
        print(f"Erro Central: {e}")

    # Registro
    try:
        ws   = sheet.worksheet("Registro")
        rows = ws.get_all_values()
        for row in rows[1:]:
            if len(row) < 13: continue
            div_t = row[8].strip().upper() if len(row)>8 else ""
            div_v = row[12].strip()        if len(row)>12 else ""
            if div_t and len(div_t)>=4 and div_v:
                try:
                    v = float(div_v.replace(",","."))
                    if v > 0: divs_received.append({"t":div_t,"val":round(v,2)})
                except: pass
            if len(row) > 27:
                st = row[24].strip().upper(); sv = row[25].strip(); sa = row[27].strip()
                if st and len(st)>=4:
                    try:
                        svf = float(sv.replace(",",".")) if sv else None
                        saf = float(sa.replace(",",".")) if sa else None
                        if svf or saf: stops[st] = {"stop":svf,"alvo":saf}
                    except: pass
    except Exception as e:
        print(f"Erro Registro: {e}")

    # Calculadora
    try:
        ws   = sheet.worksheet("Calculadora")
        rows = ws.get_all_values()
        for row in rows[1:]:
            if len(row)>=3 and row[0] and row[2]:
                cal_map[row[0].strip().upper()] = row[2].strip()
    except: pass

    return portfolio, divs_received, stops, cal_map

# ── Cotações ──────────────────────────────────────────────────────────────────
def fetch_quotes(portfolio):
    tickers = [a["t"]+".SA" for a in portfolio]
    raw = yf.download(tickers=tickers, period="7d", interval="1d",
                      auto_adjust=True, progress=False, threads=True)
    close_df = raw["Close"] if hasattr(raw.columns,"levels") else raw[["Close"]]
    quotes = {}
    for a in portfolio:
        sym = a["t"]+".SA"
        try:
            s  = close_df[sym].dropna()
            px = float(s.iloc[-1])
            pr = float(s.iloc[-2]) if len(s)>=2 else px
            pw = float(s.iloc[-5]) if len(s)>=5 else float(s.iloc[0])
            quotes[a["t"]] = {"px":round(px,2),"varDay":round((px-pr)/pr*100,2),"varWeek":round((px-pw)/pw*100,2)}
        except:
            quotes[a["t"]] = {"px":None,"varDay":None,"varWeek":None}
    return quotes

# ── Cálculos ──────────────────────────────────────────────────────────────────
def compute(portfolio, quotes, divs_received):
    rows = []; total_inv = total_mkt = 0.0
    for a in portfolio:
        q=quotes.get(a["t"],{}); px=q.get("px"); inv=a["q"]*a["pm"]
        mkt=a["q"]*px if px is not None else None
        pl=(mkt-inv) if mkt is not None else None
        plp=(pl/inv*100) if pl is not None else None
        dy=DY_REF.get(a["t"],0); total_inv+=inv
        if mkt is not None: total_mkt+=mkt
        rows.append({**a,"px":px,"inv":inv,"mkt":mkt,"pl":pl,"plp":plp,"dy":dy,
                     "annualDiv":(px or a["pm"])*dy/100,"varDay":q.get("varDay"),"varWeek":q.get("varWeek")})
    total_pl=total_mkt-total_inv; total_plp=(total_pl/total_inv*100) if total_inv else 0
    wm_dy=sum(r["dy"]*r["inv"]/total_inv for r in rows) if total_inv else 0
    return rows,{"totalInv":total_inv,"totalMkt":total_mkt,"totalPL":total_pl,"totalPLp":total_plp,
                 "wmDY":wm_dy,"divsReceived":sum(d["val"] for d in divs_received),"nAtivos":len(rows)}

# ── Constantes ────────────────────────────────────────────────────────────────
DY_REF = {"ALOS3":8.24,"AXIA3":6.64,"BBDC4":5.50,"BBSE3":7.10,"CMIG4":8.00,
           "ITSA4":5.80,"KLBN11":5.10,"LEVE3":8.59,"ODPV3":4.20,"PETR4":12.00,
           "PRIO3":2.10,"UNIP6":16.35,"VALE3":8.50,"BRSR6":9.80,"HASH11":0.0,
           "GRND3":5.20,"ISAE4":4.50}
SECTOR_COLORS = {"Shopping":"#7c6af7","Energia":"#4e9eff","Banco":"#f05252",
                 "Seguradora":"#22c87a","Celulose":"#f0b429","Metalúrgica":"#e879a0",
                 "Saúde":"#38d9a9","Petróleo":"#fd7e14","Química":"#a9e34b",
                 "Mineração":"#66d9e8","ETF":"#aaa","Outros":"#888"}
SECTOR_ICONS  = {"Shopping":"SH","Energia":"EN","Banco":"BK","Seguradora":"SG",
                 "Celulose":"CL","Metalúrgica":"MT","Saúde":"SD","Petróleo":"PT",
                 "Química":"QM","Mineração":"MN","ETF":"EF","Outros":"??"}

def brl(v):
    if v is None: return "—"
    s = f"{abs(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
    return ("−" if v < 0 else "") + "R$ " + s

def brlk(v, sign=False):
    if v is None: return "—"
    pfx = ("+" if v>=0 else "−") if sign else ("−" if v<0 else "")
    av  = abs(v)
    num = (f"{av/1e6:.2f}M" if av>=1e6 else f"{av/1e3:.1f}k" if av>=1e3 else f"{av:,.0f}").replace(".",",")
    return pfx + "R$ " + num

def chip(v):
    if v is None: return "chip-gray"
    return "chip-green" if v >= 0 else "chip-red"

# ── Gera HTML ─────────────────────────────────────────────────────────────────
def generate_html(rows, kpis, stops, cal_map, divs_received, updated_at):
    trows = ""
    for r in rows:
        ic=SECTOR_ICONS.get(r["sector"],"??"); col=SECTOR_COLORS.get(r["sector"],"#888")
        c_d=chip(r["varDay"]); c_w=chip(r["varWeek"]); c_pl=chip(r["plp"])
        c_dy="chip-green" if r["dy"]>=8 else ("chip-yellow" if r["dy"]>=5 else "chip-gray")
        plc="#22c87a" if (r["pl"] or 0)>=0 else "#f05252"
        vd=f"{r['varDay']:+.2f}%" if r["varDay"] is not None else "—"
        vw=f"{r['varWeek']:+.2f}%" if r["varWeek"] is not None else "—"
        vpl=f"{r['plp']:+.2f}%" if r["plp"] is not None else "—"
        trows += f"""
        <tr>
          <td><div class="tc"><div class="ti" style="background:{col}20;color:{col}">{ic}</div>
            <div><div class="tn">{r['t']}</div><div class="ts2">{r['sector']}</div></div></div></td>
          <td class="mono">{r['q']:,}</td><td class="mono">{brl(r['pm'])}</td>
          <td class="mono fw">{brl(r['px'])}</td>
          <td><span class="chip {c_d}">{vd}</span></td><td><span class="chip {c_w}">{vw}</span></td>
          <td class="mono">{brlk(r['inv'])}</td><td class="mono">{brlk(r['mkt'])}</td>
          <td class="mono" style="color:{plc}">{brlk(r['pl'],sign=True)}</td>
          <td><span class="chip {c_pl}">{vpl}</span></td>
          <td><span class="chip {c_dy}">{r['dy']:.1f}%</span></td>
        </tr>"""

    pls=sorted(rows,key=lambda r:r["pl"] or 0,reverse=True)
    pl_labels=json.dumps([r["t"] for r in pls])
    pl_values=json.dumps([round(r["pl"] or 0) for r in pls])
    pl_colors=json.dumps(["rgba(34,200,122,.75)" if (r["pl"] or 0)>=0 else "rgba(240,82,82,.75)" for r in pls])
    sm={}
    for r in rows: sm[r["sector"]]=sm.get(r["sector"],0)+(r["mkt"] or r["inv"])
    s_labels=json.dumps(list(sm.keys())); s_values=json.dumps([round(v) for v in sm.values()])
    s_colors=json.dumps(list(SECTOR_COLORS.values())[:len(sm)])
    dys=sorted(rows,key=lambda r:r["dy"],reverse=True)
    dy_labels=json.dumps([r["t"] for r in dys]); dy_values=json.dumps([r["dy"] for r in dys])
    dy_colors=json.dumps(["rgba(34,200,122,.9)" if r["dy"]>=10 else ("rgba(34,200,122,.5)" if r["dy"]>=6 else "rgba(255,255,255,.15)") for r in dys])
    max_dy=max((r["dy"] for r in rows),default=1) or 1
    dcards=""
    for r in sorted(rows,key=lambda r:r["dy"],reverse=True):
        if r["dy"]==0: continue
        c="chip-green" if r["dy"]>=8 else ("chip-yellow" if r["dy"]>=5 else "chip-gray")
        mes=cal_map.get(r["t"],"—")
        dcards+=f"""<div class="div-card">
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div><div class="div-ticker">{r['t']}</div><div class="div-co">{r['sector']}</div></div>
            <span class="chip {c}">{r['dy']:.2f}% a.a.</span></div>
          <div class="div-val">{brl(r['annualDiv'])}/ação</div>
          <div class="div-sub">Est. anual: {brlk(r['q']*r['annualDiv'])}</div>
          <div class="div-sub">Pagamento: {mes}</div>
          <div class="div-bar-w"><div class="div-bar" style="width:{r['dy']/max_dy*100:.0f}%"></div></div>
        </div>"""
    db={}
    for d in divs_received: db[d["t"]]=db.get(d["t"],0)+d["val"]
    div_rows="".join(f'<tr><td style="font-weight:500">{t}</td><td class="mono">{brl(v)}</td></tr>'
                     for t,v in sorted(db.items(),key=lambda x:x[1],reverse=True))
    stop_rows=""
    for t,s in stops.items():
        px=next((r["px"] for r in rows if r["t"]==t),None)
        dist=f"{(px-s['stop'])/s['stop']*100:+.1f}%" if px and s.get("stop") else "—"
        dc="#22c87a" if px and s.get("stop") and px>s["stop"] else "#f05252"
        stop_rows+=f'<tr><td style="font-weight:500">{t}</td><td class="mono" style="color:#f05252">{brl(s.get("stop"))}</td><td class="mono" style="color:#22c87a">{brl(s.get("alvo"))}</td><td class="mono" style="color:{dc}">{dist}</td></tr>'

    pl_sign="+" if kpis["totalPL"]>=0 else ""
    pl_class="pos" if kpis["totalPL"]>=0 else "neg"
    refresh_ms=REFRESH_MIN*60*1000

    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Carteira B3</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0e0f11;--bg2:#16181c;--bg3:#1e2026;--bg4:#252830;--border:rgba(255,255,255,.07);--text:#f0f1f3;--text2:#8b8f9a;--text3:#555963;--green:#22c87a;--gd:rgba(34,200,122,.12);--red:#f05252;--rd:rgba(240,82,82,.12);--yellow:#f0b429;--yd:rgba(240,180,41,.1);--accent:#7c6af7;--r:10px;--rs:6px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);font-size:14px}}
.shell{{max-width:1300px;margin:0 auto;padding:24px 20px 60px}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}}
.logo{{display:flex;align-items:center;gap:10px}}
.lm{{width:34px;height:34px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:13px;font-weight:500;color:#fff}}
.lt{{font-size:17px;font-weight:600;letter-spacing:-.3px}}.ls{{font-size:12px;color:var(--text2)}}
.hbadge{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.badge,.refresh-badge{{font-size:12px;color:var(--text2);background:var(--bg3);border:1px solid var(--border);padding:6px 12px;border-radius:var(--rs)}}
.badge span{{color:var(--green);font-weight:500}}
.refresh-badge{{display:flex;align-items:center;gap:6px}}
.rdot{{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.kgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:10px;margin-bottom:20px}}
.kpi{{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}}
.kl{{font-size:11px;font-weight:500;color:var(--text2);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
.kv{{font-size:24px;font-weight:600;letter-spacing:-.5px}}.ks{{font-size:12px;color:var(--text2);margin-top:3px}}
.pos{{color:var(--green)}}.neg{{color:var(--red)}}
.sh{{margin:24px 0 10px;font-size:12px;font-weight:500;color:var(--text2);text-transform:uppercase;letter-spacing:.07em}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}}
.cb{{padding:16px 18px}}.tw{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse}}
thead tr{{border-bottom:1px solid var(--border)}}
th{{padding:10px 14px;font-size:11px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;text-align:right;white-space:nowrap}}
th:first-child{{text-align:left}}
td{{padding:11px 14px;border-bottom:1px solid var(--border);font-size:13px;color:var(--text);text-align:right;white-space:nowrap}}
td:first-child{{text-align:left}}
tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:var(--bg3)}}
.tc{{display:flex;align-items:center;gap:8px}}
.ti{{width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:9px;font-weight:500;flex-shrink:0}}
.tn{{font-weight:500;font-size:13px}}.ts2{{font-size:11px;color:var(--text2)}}
.mono{{font-family:'DM Mono',monospace}}.fw{{font-weight:600}}
.chip{{display:inline-flex;align-items:center;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:500;font-family:'DM Mono',monospace}}
.chip-green{{background:var(--gd);color:var(--green)}}.chip-red{{background:var(--rd);color:var(--red)}}
.chip-yellow{{background:var(--yd);color:var(--yellow)}}.chip-gray{{background:var(--bg4);color:var(--text2)}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
@media(max-width:800px){{.two{{grid-template-columns:1fr}}}}
.dgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:10px}}
.div-card{{background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);padding:14px 16px;display:flex;flex-direction:column;gap:5px}}
.div-ticker{{font-size:12px;font-weight:600;font-family:'DM Mono',monospace}}.div-co{{font-size:11px;color:var(--text2)}}
.div-val{{font-size:19px;font-weight:600;color:var(--green);font-family:'DM Mono',monospace}}.div-sub{{font-size:11px;color:var(--text2)}}
.div-bar-w{{height:3px;background:var(--bg4);border-radius:2px;margin-top:4px}}
.div-bar{{height:3px;background:var(--green);border-radius:2px}}
#countdown{{font-family:'DM Mono',monospace;color:var(--green)}}
footer{{text-align:center;font-size:11px;color:var(--text3);margin-top:40px}}
</style></head><body>
<div class="shell">
<header>
  <div class="logo"><div class="lm">C$</div>
    <div><div class="lt">Carteira B3</div><div class="ls">Google Sheets · ao vivo</div></div></div>
  <div class="hbadge">
    <div class="badge">Atualizado: <span>{updated_at}</span></div>
    <div class="refresh-badge"><div class="rdot"></div>Próxima atualização: <span id="countdown">--:--</span></div>
  </div>
</header>
<div class="kgrid">
  <div class="kpi"><div class="kl">Valor investido</div><div class="kv">{brlk(kpis['totalInv'])}</div><div class="ks">custo médio</div></div>
  <div class="kpi"><div class="kl">Valor de mercado</div><div class="kv">{brlk(kpis['totalMkt'])}</div><div class="ks">cotações ao vivo</div></div>
  <div class="kpi"><div class="kl">P&amp;L total</div><div class="kv {pl_class}">{pl_sign}{brlk(kpis['totalPL'])}</div><div class="ks {pl_class}">{kpis['totalPLp']:+.2f}%</div></div>
  <div class="kpi"><div class="kl">DY médio pond.</div><div class="kv">{kpis['wmDY']:.1f}%</div><div class="ks">últimos 12 meses</div></div>
  <div class="kpi"><div class="kl">Dividendos recebidos</div><div class="kv">{brlk(kpis['divsReceived'])}</div><div class="ks">registrados</div></div>
  <div class="kpi"><div class="kl">Ativos</div><div class="kv">{kpis['nAtivos']}</div><div class="ks">renda variável B3</div></div>
</div>
<div class="sh">Posições</div>
<div class="card"><div class="tw"><table>
  <thead><tr><th>Ativo</th><th>Qtd</th><th>Preço médio</th><th>Cotação</th><th>Var. dia</th><th>Var. semana</th><th>Val. investido</th><th>Val. mercado</th><th>P&amp;L (R$)</th><th>P&amp;L (%)</th><th>DY 12m</th></tr></thead>
  <tbody>{trows}</tbody>
</table></div></div>
<div class="sh">Análise visual</div>
<div class="two">
  <div class="card"><div class="cb" style="padding-bottom:8px">
    <div style="font-size:12px;color:var(--text2);margin-bottom:12px">P&amp;L por ativo (R$)</div>
    <div style="position:relative;height:320px"><canvas id="plChart"></canvas></div></div></div>
  <div class="card"><div class="cb" style="padding-bottom:8px">
    <div style="font-size:12px;color:var(--text2);margin-bottom:12px">Composição setorial</div>
    <div style="position:relative;height:220px"><canvas id="setorChart"></canvas></div>
    <div id="sl" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;font-size:11px;color:var(--text2)"></div>
  </div></div>
</div>
<div class="sh">Dividend yield — últimos 12 meses</div>
<div class="card"><div class="cb" style="padding-bottom:8px">
  <div style="position:relative;height:240px"><canvas id="dyChart"></canvas></div></div></div>
<div class="sh">Rendimento em dividendos</div>
<div class="dgrid">{dcards}</div>
<div class="sh">Dividendos recebidos &amp; Stops</div>
<div class="two">
  <div class="card"><div class="tw"><table>
    <thead><tr><th>Ativo</th><th>Total recebido</th></tr></thead><tbody>{div_rows}</tbody></table></div></div>
  <div class="card"><div class="tw"><table>
    <thead><tr><th>Ativo</th><th>Stop</th><th>Alvo</th><th>Dist. stop</th></tr></thead><tbody>{stop_rows}</tbody></table></div></div>
</div>
<footer>Google Sheets + Yahoo Finance · Atualiza a cada {REFRESH_MIN} min</footer>
</div>
<script>
const PL_L={pl_labels},PL_V={pl_values},PL_C={pl_colors};
const SL={s_labels},SV={s_values},SC={s_colors};
const DL={dy_labels},DV={dy_values},DC={dy_colors};
const REFRESH_MS={refresh_ms};
new Chart(document.getElementById('plChart'),{{type:'bar',data:{{labels:PL_L,datasets:[{{data:PL_V,backgroundColor:PL_C,borderRadius:4,borderSkipped:false}}]}},options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:function(d){{return ' R$ '+Number(d.raw).toLocaleString('pt-BR');}} }} }}}},scales:{{x:{{grid:{{color:'rgba(255,255,255,.05)'}},ticks:{{color:'#555963',callback:function(v){{return 'R$'+Math.round(v/1e3)+'k';}} }}}},y:{{grid:{{display:false}},ticks:{{color:'#8b8f9a',font:{{size:11,family:'DM Mono'}}}}}}}}}}}}}});
const sc=new Chart(document.getElementById('setorChart'),{{type:'doughnut',data:{{labels:SL,datasets:[{{data:SV,backgroundColor:SC,borderWidth:0}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'68%',plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:d=>` ${{d.label}}: R$${{Math.round(d.raw/1e3)}}k (${{(d.raw/SV.reduce((a,b)=>a+b,0)*100).toFixed(1)}}%)`}}}}}}}}}});
const tot=SV.reduce((a,b)=>a+b,0);
const leg=document.getElementById('sl');
SL.forEach((s,i)=>leg.innerHTML+=`<span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:${{SC[i]}}"></span>${{s}} ${{(SV[i]/tot*100).toFixed(0)}}%</span>`);
new Chart(document.getElementById('dyChart'),{{type:'bar',data:{{labels:DL,datasets:[{{label:'DY',data:DV,backgroundColor:DC,borderRadius:4}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:d=>d.raw.toFixed(2)+'%'}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{color:'#8b8f9a',font:{{size:11,family:'DM Mono'}}}}}},y:{{grid:{{color:'rgba(255,255,255,.05)'}},ticks:{{color:'#555963',callback:v=>v+'%'}}}}}}}}}});
let remaining=REFRESH_MS;
const cd=document.getElementById('countdown');
setInterval(()=>{{
  remaining-=1000;
  if(remaining<=0){{location.reload();return;}}
  const m=String(Math.floor(remaining/60000)).padStart(2,'0');
  const s=String(Math.floor((remaining%60000)/1000)).padStart(2,'0');
  cd.textContent=m+':'+s;
}},1000);
</script></body></html>"""

# ── Worker de atualização em background ───────────────────────────────────────
def update_loop():
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M')}] Atualizando…")
            sheet = connect_sheets()
            portfolio, divs_received, stops, cal_map = read_from_sheets(sheet)
            if portfolio:
                quotes = fetch_quotes(portfolio)
                rows, kpis = compute(portfolio, quotes, divs_received)
                updated_at = datetime.now().strftime("%d/%m/%Y %H:%M")
                html = generate_html(rows, kpis, stops, cal_map, divs_received, updated_at)
                with _cache["lock"]:
                    _cache["html"] = html
                print(f"[{datetime.now().strftime('%H:%M')}] OK — {len(portfolio)} ativos")
            else:
                print("Nenhum ativo encontrado.")
        except Exception as e:
            print(f"Erro no update: {e}")
        time.sleep(REFRESH_MIN * 60)

# ── Rotas Flask ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with _cache["lock"]:
        html = _cache["html"]
    return Response(html, mimetype="text/html")

@app.route("/health")
def health():
    return "ok"

# ── Inicialização ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SHEET_ID or not CREDS_JSON:
        print("❌  Configure as variáveis de ambiente SHEET_ID e GOOGLE_CREDS_JSON")
    else:
        t = threading.Thread(target=update_loop, daemon=True)
        t.start()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
